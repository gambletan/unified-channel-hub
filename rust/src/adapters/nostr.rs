//! Nostr adapter using the nostr-sdk crate.

use async_trait::async_trait;
use chrono::Utc;
use std::sync::Arc;
use tokio::sync::Mutex;

use nostr_sdk::prelude::*;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Nostr adapter configuration.
pub struct NostrConfig {
    /// Secret key (hex or bech32 nsec).
    pub secret_key: String,
    /// Relay URLs to connect to.
    pub relays: Vec<String>,
    /// Subscribe to specific pubkeys (hex). Empty means global feed.
    pub follow_pubkeys: Vec<String>,
}

impl NostrConfig {
    /// Create with minimal required fields.
    pub fn new(secret_key: impl Into<String>, relays: Vec<String>) -> Self {
        Self {
            secret_key: secret_key.into(),
            relays,
            follow_pubkeys: Vec::new(),
        }
    }
}

/// Nostr adapter.
pub struct NostrAdapter {
    config: NostrConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Arc<Mutex<Option<MessageHandler>>>,
    client: Option<nostr_sdk::Client>,
    pubkey: Option<String>,
}

impl NostrAdapter {
    /// Create a new Nostr adapter.
    pub fn new(config: NostrConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: Arc::new(Mutex::new(None)),
            client: None,
            pubkey: None,
        }
    }

    /// Convert a Nostr event to a UnifiedMessage.
    fn event_to_unified(event: &Event) -> UnifiedMessage {
        let text = event.content.clone();
        let is_cmd = text.starts_with('/');

        let content = if is_cmd {
            let parts: Vec<&str> = text[1..].split_whitespace().collect();
            let cmd = parts.first().unwrap_or(&"").to_string();
            let args: Vec<String> = parts[1..].iter().map(|s| s.to_string()).collect();
            MessageContent::command(&text, cmd, args)
        } else {
            MessageContent::text(&text)
        };

        // Determine chat_id from event kind
        let chat_id = match event.kind {
            Kind::EncryptedDirectMessage => {
                // p tag is the recipient
                event
                    .tags
                    .iter()
                    .find(|t| t.kind() == TagKind::p())
                    .and_then(|t| t.content())
                    .map(String::from)
            }
            _ => None,
        };

        let reply_to = event
            .tags
            .iter()
            .find(|t| t.kind() == TagKind::e())
            .and_then(|t| t.content())
            .map(String::from);

        UnifiedMessage {
            id: event.id.to_hex(),
            channel: "nostr".to_string(),
            sender: Identity::new(event.author().to_hex()),
            content,
            timestamp: Utc::now(),
            thread_id: None,
            reply_to_id: reply_to,
            chat_id,
            raw: None,
            metadata: Default::default(),
        }
    }
}

#[async_trait]
impl ChannelAdapter for NostrAdapter {
    fn channel_id(&self) -> &str {
        "nostr"
    }

    async fn connect(&mut self) -> Result<()> {
        let keys = Keys::parse(&self.config.secret_key)
            .map_err(|e| Error::Connection(format!("nostr key parse: {}", e)))?;

        self.pubkey = Some(keys.public_key().to_hex());
        let client = nostr_sdk::Client::new(keys);

        // Add relays
        for relay in &self.config.relays {
            client
                .add_relay(relay.as_str())
                .await
                .map_err(|e| Error::Connection(format!("nostr add relay: {}", e)))?;
        }

        // Connect to relays
        client.connect().await;

        // Subscribe to events
        let mut filters = vec![
            // Text notes mentioning our pubkey
            Filter::new()
                .kind(Kind::TextNote)
                .limit(50),
            // DMs to our pubkey
            Filter::new()
                .kind(Kind::EncryptedDirectMessage)
                .limit(50),
        ];

        // If following specific pubkeys, filter by author
        if !self.config.follow_pubkeys.is_empty() {
            let pubkeys: Vec<PublicKey> = self
                .config
                .follow_pubkeys
                .iter()
                .filter_map(|pk| PublicKey::from_hex(pk).ok())
                .collect();
            filters = vec![
                Filter::new()
                    .kind(Kind::TextNote)
                    .authors(pubkeys.clone())
                    .limit(50),
                Filter::new()
                    .kind(Kind::EncryptedDirectMessage)
                    .limit(50),
            ];
        }

        client
            .subscribe(filters, None)
            .await
            .map_err(|e| Error::Connection(format!("nostr subscribe: {}", e)))?;

        self.client = Some(client.clone());

        // Start event listener
        let handler = Arc::clone(&self.handler);
        tokio::spawn(async move {
            let mut notifications = client.notifications();
            while let Ok(notification) = notifications.recv().await {
                if let RelayPoolNotification::Event { event, .. } = notification {
                    let unified = NostrAdapter::event_to_unified(&event);
                    let h = handler.lock().await;
                    if let Some(ref callback) = *h {
                        callback(unified);
                    }
                }
            }
        });

        self.connected = true;
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        if let Some(ref client) = self.client {
            client.disconnect().await;
        }
        self.connected = false;
        Ok(())
    }

    fn on_message(&mut self, handler: MessageHandler) {
        let h = Arc::clone(&self.handler);
        tokio::task::block_in_place(|| {
            let rt = tokio::runtime::Handle::current();
            rt.block_on(async {
                let mut guard = h.lock().await;
                *guard = Some(handler);
            });
        });
    }

    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>> {
        let client = self.client.as_ref().ok_or_else(|| {
            Error::Send("nostr not connected".to_string())
        })?;

        // If chat_id looks like a pubkey, send as DM; otherwise publish as note
        let event_id = if msg.chat_id.len() == 64 && msg.chat_id.chars().all(|c| c.is_ascii_hexdigit()) {
            let pubkey = PublicKey::from_hex(&msg.chat_id)
                .map_err(|e| Error::Send(format!("invalid pubkey: {}", e)))?;
            // TODO: Send encrypted DM (NIP-04 or NIP-44)
            // For now, publish as a tagged note
            let builder = EventBuilder::text_note(&msg.text)
                .tag(Tag::public_key(pubkey));
            let output = client
                .send_event_builder(builder)
                .await
                .map_err(|e| Error::Send(format!("nostr send: {}", e)))?;
            output.id().to_hex()
        } else {
            // Publish as a text note
            let output = client
                .publish_text_note(&msg.text, [])
                .await
                .map_err(|e| Error::Send(format!("nostr publish: {}", e)))?;
            output.id().to_hex()
        };

        Ok(Some(event_id))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "nostr".to_string(),
            account_id: self.pubkey.clone(),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
