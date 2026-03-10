//! Cross-channel relay middleware — forward messages between channels.
//!
//! # Example
//! ```ignore
//! let mut relay = RelayMiddleware::new();
//! relay.add_rule("telegram", "slack", "general", None, None, true, false);
//! relay.add_rule("*", "telegram", "123456", Some(is_urgent), None, true, false);
//! manager.add_middleware(Box::new(relay));
//! ```

use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;
use tracing::{debug, warn};

use crate::adapter::ChannelAdapter;
use crate::middleware::{Handler, HandlerResult, Middleware};
use crate::types::{OutboundMessage, UnifiedMessage};

/// Filter function: returns true if the message should be relayed.
pub type FilterFn = Arc<dyn Fn(&UnifiedMessage) -> bool + Send + Sync>;

/// Transform function: converts a message into relay text.
pub type TransformFn = Arc<dyn Fn(&UnifiedMessage) -> String + Send + Sync>;

/// A single relay rule.
#[derive(Clone)]
pub struct RelayRule {
    /// Source channel ID, or "*" for all channels.
    pub source: String,
    /// Target channel ID.
    pub target: String,
    /// Chat ID in the target channel.
    pub target_chat_id: String,
    /// Optional filter — if set, must return true for the message to be relayed.
    pub filter_fn: Option<FilterFn>,
    /// Optional text transform — if set, produces the relay text.
    pub transform: Option<TransformFn>,
    /// Prepend sender info to the relayed text.
    pub include_sender: bool,
}

/// Cross-channel relay middleware.
///
/// Messages are relayed *after* the handler processes them, so the original
/// channel's response is not affected.
pub struct RelayMiddleware {
    rules: Vec<RelayRule>,
    /// Adapters map, set by the channel manager.
    adapters: Arc<Mutex<HashMap<String, Arc<Mutex<Box<dyn ChannelAdapter>>>>>>,
}

impl RelayMiddleware {
    /// Create an empty relay.
    pub fn new() -> Self {
        Self {
            rules: Vec::new(),
            adapters: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Give the relay access to channel adapters (called by manager).
    pub fn set_adapters(
        &mut self,
        adapters: Arc<Mutex<HashMap<String, Arc<Mutex<Box<dyn ChannelAdapter>>>>>>,
    ) {
        self.adapters = adapters;
    }

    /// Add a relay rule. Returns self for chaining.
    pub fn add_rule(
        &mut self,
        source: impl Into<String>,
        target: impl Into<String>,
        target_chat_id: impl Into<String>,
        filter_fn: Option<FilterFn>,
        transform: Option<TransformFn>,
        include_sender: bool,
        bidirectional: bool,
    ) -> &mut Self {
        let source = source.into();
        let target = target.into();
        let target_chat_id = target_chat_id.into();

        self.rules.push(RelayRule {
            source: source.clone(),
            target: target.clone(),
            target_chat_id: target_chat_id.clone(),
            filter_fn: filter_fn.clone(),
            transform: transform.clone(),
            include_sender,
        });

        if bidirectional {
            self.rules.push(RelayRule {
                source: target,
                target: source,
                target_chat_id,
                filter_fn,
                transform,
                include_sender,
            });
        }

        self
    }

    /// Broadcast from one source to multiple targets.
    pub fn add_broadcast(
        &mut self,
        source: impl Into<String>,
        targets: HashMap<String, String>,
        filter_fn: Option<FilterFn>,
        transform: Option<TransformFn>,
    ) -> &mut Self {
        let source = source.into();
        for (target_channel, chat_id) in targets {
            self.rules.push(RelayRule {
                source: source.clone(),
                target: target_channel,
                target_chat_id: chat_id,
                filter_fn: filter_fn.clone(),
                transform: transform.clone(),
                include_sender: true,
            });
        }
        self
    }

    /// Number of rules.
    pub fn rule_count(&self) -> usize {
        self.rules.len()
    }

    /// Relay a message according to a rule.
    async fn relay(&self, msg: &UnifiedMessage, rule: &RelayRule) {
        let text = if let Some(ref transform) = rule.transform {
            transform(msg)
        } else {
            msg.content.text.clone()
        };

        let text = if rule.include_sender {
            let sender_name = msg.sender.display();
            format!("[{}/{}] {}", msg.channel, sender_name, text)
        } else {
            text
        };

        let mut metadata = HashMap::new();
        metadata.insert(
            "relayed_from".to_string(),
            serde_json::Value::String(msg.channel.clone()),
        );
        metadata.insert(
            "original_id".to_string(),
            serde_json::Value::String(msg.id.clone()),
        );

        let outbound = OutboundMessage {
            chat_id: rule.target_chat_id.clone(),
            text,
            metadata,
            ..Default::default()
        };

        let adapters = self.adapters.lock().await;
        if let Some(adapter) = adapters.get(&rule.target) {
            let a = adapter.lock().await;
            match a.send(outbound).await {
                Ok(_) => debug!(
                    from = %msg.channel,
                    to = %rule.target,
                    chat_id = %rule.target_chat_id,
                    "relayed message"
                ),
                Err(e) => warn!(
                    from = %msg.channel,
                    to = %rule.target,
                    error = %e,
                    "relay failed"
                ),
            }
        } else {
            warn!(target = %rule.target, "relay target adapter not found");
        }
    }
}

impl Default for RelayMiddleware {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Middleware for RelayMiddleware {
    async fn process(&self, msg: UnifiedMessage, next: &Handler) -> HandlerResult {
        // Let the original handler process first
        let result = next(msg.clone()).await;

        // Then relay to matching targets
        let matching: Vec<_> = self
            .rules
            .iter()
            .filter(|r| r.source == "*" || r.source == msg.channel)
            .filter(|r| r.filter_fn.as_ref().map_or(true, |f| f(&msg)))
            .collect();

        for rule in matching {
            self.relay(&msg, rule).await;
        }

        result
    }
}
