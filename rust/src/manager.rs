//! Channel manager — ties adapters and the middleware pipeline together.

use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::{Mutex, Notify};
use tracing::{error, info};

use crate::adapter::ChannelAdapter;
use crate::error::{Error, Result};
use crate::middleware::{ArcHandler, HandlerResult, Middleware};
use crate::types::{ChannelStatus, OutboundMessage, UnifiedMessage};

/// Central hub that manages channel adapters and a middleware pipeline.
pub struct ChannelManager {
    channels: HashMap<String, Arc<Mutex<Box<dyn ChannelAdapter>>>>,
    middlewares: Vec<Arc<dyn Middleware>>,
    fallback_handler: Option<ArcHandler>,
    shutdown: Arc<Notify>,
}

impl ChannelManager {
    /// Create an empty manager.
    pub fn new() -> Self {
        Self {
            channels: HashMap::new(),
            middlewares: Vec::new(),
            fallback_handler: None,
            shutdown: Arc::new(Notify::new()),
        }
    }

    /// Register a channel adapter. Returns self for chaining.
    pub fn add_channel(&mut self, adapter: Box<dyn ChannelAdapter>) -> &mut Self {
        let id = adapter.channel_id().to_string();
        self.channels.insert(id, Arc::new(Mutex::new(adapter)));
        self
    }

    /// Add a middleware to the pipeline. Returns self for chaining.
    pub fn add_middleware(&mut self, mw: impl Middleware + 'static) -> &mut Self {
        self.middlewares.push(Arc::new(mw));
        self
    }

    /// Set the fallback handler (called when no middleware handles the message).
    pub fn on_message<F, Fut>(&mut self, handler: F) -> &mut Self
    where
        F: Fn(UnifiedMessage) -> Fut + Send + Sync + 'static,
        Fut: std::future::Future<Output = HandlerResult> + Send + 'static,
    {
        self.fallback_handler = Some(Arc::new(move |msg: UnifiedMessage| {
            Box::pin(handler(msg))
                as std::pin::Pin<Box<dyn std::future::Future<Output = HandlerResult> + Send>>
        }));
        self
    }

    /// Get a reference to an adapter by channel ID.
    pub fn get_adapter(&self, channel: &str) -> Option<&Arc<Mutex<Box<dyn ChannelAdapter>>>> {
        self.channels.get(channel)
    }

    /// Send a message through a specific channel.
    pub async fn send(
        &self,
        channel: &str,
        chat_id: &str,
        text: &str,
        reply_to_id: Option<&str>,
        parse_mode: Option<&str>,
    ) -> Result<Option<String>> {
        let adapter = self
            .channels
            .get(channel)
            .ok_or_else(|| Error::ChannelNotFound(channel.to_string()))?;

        let msg = OutboundMessage {
            chat_id: chat_id.to_string(),
            text: text.to_string(),
            reply_to_id: reply_to_id.map(String::from),
            parse_mode: parse_mode.map(String::from),
            ..Default::default()
        };

        let adapter = adapter.lock().await;
        adapter.send(msg).await
    }

    /// Broadcast a message to multiple channels.
    pub async fn broadcast(&self, text: &str, chat_ids: &HashMap<String, String>) {
        let mut handles = Vec::new();
        for (channel, chat_id) in chat_ids {
            let channel = channel.clone();
            let chat_id = chat_id.clone();
            let text = text.to_string();
            if let Some(adapter) = self.channels.get(&channel) {
                let adapter = Arc::clone(adapter);
                handles.push(tokio::spawn(async move {
                    let msg = OutboundMessage::text(chat_id, text);
                    let a = adapter.lock().await;
                    let _ = a.send(msg).await;
                }));
            }
        }
        for h in handles {
            let _ = h.await;
        }
    }

    /// Get status of all registered channels.
    pub async fn get_status(&self) -> HashMap<String, ChannelStatus> {
        let mut statuses = HashMap::new();
        for (id, adapter) in &self.channels {
            let status = {
                let a = adapter.lock().await;
                a.get_status().await
            };
            statuses.insert(id.clone(), status);
        }
        statuses
    }

    /// Connect all adapters and start listening. Blocks until [`shutdown`] is called.
    pub async fn run(&mut self) -> Result<()> {
        if self.channels.is_empty() {
            return Err(Error::NoChannels);
        }

        for (id, adapter) in &self.channels {
            let mut a = adapter.lock().await;
            a.connect().await?;
            info!(channel = %id, "connected");
        }

        let channel_names: Vec<_> = self.channels.keys().cloned().collect();
        info!(channels = ?channel_names, "unified-channel started");

        // Wait for shutdown signal
        self.shutdown.notified().await;
        Ok(())
    }

    /// Signal shutdown and disconnect all adapters.
    pub async fn shutdown(&self) {
        self.shutdown.notify_one();
        for (id, adapter) in &self.channels {
            let mut a = adapter.lock().await;
            if let Err(e) = a.disconnect().await {
                error!(channel = %id, error = %e, "error disconnecting");
            }
        }
        info!("unified-channel shut down");
    }

    /// Run the middleware pipeline for a message and return the result.
    pub async fn run_pipeline(&self, msg: UnifiedMessage) -> HandlerResult {
        let fallback: ArcHandler = match self.fallback_handler {
            Some(ref handler) => Arc::clone(handler),
            None => Arc::new(|_: UnifiedMessage| {
                Box::pin(async { HandlerResult::None })
                    as std::pin::Pin<Box<dyn std::future::Future<Output = HandlerResult> + Send>>
            }),
        };

        crate::middleware::run_pipeline(self.middlewares.clone(), fallback, msg).await
    }

    /// Process an inbound message: run through pipeline, send reply if any.
    pub async fn handle_message(&self, channel: &str, msg: UnifiedMessage) {
        let chat_id = msg.chat_id.clone();
        let msg_id = msg.id.clone();
        let reply = self.run_pipeline(msg).await;

        if reply.is_none() || chat_id.is_none() {
            return;
        }

        let outbound = match reply {
            HandlerResult::Text(text) => OutboundMessage {
                chat_id: chat_id.unwrap_or_default(),
                text,
                reply_to_id: Some(msg_id),
                ..Default::default()
            },
            HandlerResult::Outbound(mut out) => {
                if out.chat_id.is_empty() {
                    out.chat_id = chat_id.unwrap_or_default();
                }
                out
            }
            HandlerResult::None => return,
        };

        if let Some(adapter) = self.channels.get(channel) {
            let a = adapter.lock().await;
            if let Err(e) = a.send(outbound).await {
                error!(channel = %channel, error = %e, "failed to send reply");
            }
        }
    }

    /// Number of registered channels.
    pub fn channel_count(&self) -> usize {
        self.channels.len()
    }

    /// List registered channel IDs.
    pub fn channel_ids(&self) -> Vec<String> {
        self.channels.keys().cloned().collect()
    }
}

impl Default for ChannelManager {
    fn default() -> Self {
        Self::new()
    }
}
