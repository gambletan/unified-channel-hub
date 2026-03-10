//! Discord adapter using the serenity crate.

use async_trait::async_trait;
use chrono::Utc;
use std::collections::HashSet;
use std::sync::Arc;
use tokio::sync::Mutex;

use serenity::all::{
    ChannelId, Context, CreateMessage, EventHandler, GatewayIntents, Message, Ready,
};
use serenity::Client;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Configuration for the Discord adapter.
pub struct DiscordOptions {
    /// Restrict to specific channel IDs. None means all channels.
    pub allowed_channel_ids: Option<HashSet<String>>,
    /// Allow direct messages.
    pub allow_dm: bool,
    /// Command prefix (default "/").
    pub command_prefix: String,
}

impl Default for DiscordOptions {
    fn default() -> Self {
        Self {
            allowed_channel_ids: None,
            allow_dm: true,
            command_prefix: "/".to_string(),
        }
    }
}

/// Serenity event handler that forwards to the unified handler.
struct DiscordEventHandler {
    handler: Arc<Mutex<Option<MessageHandler>>>,
    options: Arc<DiscordOptions>,
}

#[async_trait]
impl EventHandler for DiscordEventHandler {
    async fn message(&self, _ctx: Context, msg: Message) {
        // Skip bot messages
        if msg.author.bot {
            return;
        }

        // DM check
        if msg.is_private() && !self.options.allow_dm {
            return;
        }

        // Channel allowlist
        if !msg.is_private() {
            if let Some(ref allowed) = self.options.allowed_channel_ids {
                if !allowed.contains(&msg.channel_id.to_string()) {
                    return;
                }
            }
        }

        let text = msg.content.clone();
        let prefix = &self.options.command_prefix;
        let is_cmd = text.starts_with(prefix);

        let content = if is_cmd {
            let stripped = &text[prefix.len()..];
            let parts: Vec<&str> = stripped.split_whitespace().collect();
            let cmd = parts.first().unwrap_or(&"").to_string();
            let args: Vec<String> = parts[1..].iter().map(|s| s.to_string()).collect();
            MessageContent::command(text, cmd, args)
        } else if !msg.attachments.is_empty() {
            let att = &msg.attachments[0];
            MessageContent::media(
                text,
                att.url.clone(),
                att.content_type.clone(),
            )
        } else {
            MessageContent::text(text)
        };

        let sender = Identity::new(msg.author.id.to_string())
            .with_username(msg.author.name.clone())
            .with_display_name(msg.author.global_name.clone().unwrap_or(msg.author.name.clone()));

        let unified = UnifiedMessage {
            id: msg.id.to_string(),
            channel: "discord".to_string(),
            sender,
            content,
            timestamp: *msg.timestamp,
            thread_id: None,
            reply_to_id: msg.referenced_message.as_ref().map(|r| r.id.to_string()),
            chat_id: Some(msg.channel_id.to_string()),
            raw: None,
            metadata: Default::default(),
        };

        let handler = self.handler.lock().await;
        if let Some(ref h) = *handler {
            h(unified);
        }
    }

    async fn ready(&self, _ctx: Context, ready: Ready) {
        tracing::info!(user = %ready.user.name, "discord connected");
    }
}

/// Discord adapter backed by serenity.
pub struct DiscordAdapter {
    token: String,
    options: Arc<DiscordOptions>,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    bot_user: Option<String>,
    handler: Arc<Mutex<Option<MessageHandler>>>,
    client: Option<Arc<Mutex<Client>>>,
}

impl DiscordAdapter {
    /// Create a new Discord adapter.
    pub fn new(token: impl Into<String>) -> Self {
        Self {
            token: token.into(),
            options: Arc::new(DiscordOptions::default()),
            connected: false,
            last_activity: None,
            bot_user: None,
            handler: Arc::new(Mutex::new(None)),
            client: None,
        }
    }

    /// Builder: set options.
    pub fn with_options(mut self, options: DiscordOptions) -> Self {
        self.options = Arc::new(options);
        self
    }
}

#[async_trait]
impl ChannelAdapter for DiscordAdapter {
    fn channel_id(&self) -> &str {
        "discord"
    }

    async fn connect(&mut self) -> Result<()> {
        let intents = GatewayIntents::GUILD_MESSAGES
            | GatewayIntents::MESSAGE_CONTENT
            | GatewayIntents::DIRECT_MESSAGES;

        let event_handler = DiscordEventHandler {
            handler: Arc::clone(&self.handler),
            options: Arc::clone(&self.options),
        };

        let client = Client::builder(&self.token, intents)
            .event_handler(event_handler)
            .await
            .map_err(|e| Error::Connection(e.to_string()))?;

        let client = Arc::new(Mutex::new(client));
        self.client = Some(Arc::clone(&client));

        // Start in background
        tokio::spawn(async move {
            let mut c = client.lock().await;
            if let Err(e) = c.start().await {
                tracing::error!(error = %e, "discord client error");
            }
        });

        self.connected = true;
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        if let Some(ref client) = self.client {
            let mut c = client.lock().await;
            c.shard_manager.shutdown_all().await;
        }
        self.connected = false;
        Ok(())
    }

    fn on_message(&mut self, handler: MessageHandler) {
        // We can't easily do async set here, so we use a blocking approach
        // since on_message is called before connect.
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
            Error::Send("not connected".to_string())
        })?;

        let channel_id: u64 = msg
            .chat_id
            .parse()
            .map_err(|_| Error::Send(format!("invalid channel_id: {}", msg.chat_id)))?;

        let c = client.lock().await;
        let http = c.http.clone();
        drop(c);

        let channel = ChannelId::new(channel_id);
        let builder = CreateMessage::new().content(&msg.text);
        let sent = channel
            .send_message(&http, builder)
            .await
            .map_err(|e| Error::Send(e.to_string()))?;

        Ok(Some(sent.id.to_string()))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "discord".to_string(),
            account_id: self.bot_user.clone(),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
