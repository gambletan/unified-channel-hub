//! Slack adapter using the slack-morphism crate.

use async_trait::async_trait;
use chrono::Utc;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Slack adapter configuration.
pub struct SlackConfig {
    /// Bot OAuth token (xoxb-...).
    pub bot_token: String,
    /// App-level token for socket mode (xapp-...).
    pub app_token: Option<String>,
    /// Signing secret for webhook verification.
    pub signing_secret: Option<String>,
}

/// Slack adapter backed by slack-morphism.
pub struct SlackAdapter {
    config: SlackConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    bot_user_id: Option<String>,
    handler: Option<MessageHandler>,
}

impl SlackAdapter {
    /// Create a new Slack adapter.
    pub fn new(config: SlackConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            bot_user_id: None,
            handler: None,
        }
    }

    /// Convert a Slack event payload into a UnifiedMessage.
    fn slack_event_to_unified(
        user: &str,
        text: &str,
        channel: &str,
        ts: &str,
        thread_ts: Option<&str>,
    ) -> UnifiedMessage {
        let is_cmd = text.starts_with('/');
        let content = if is_cmd {
            let parts: Vec<&str> = text[1..].split_whitespace().collect();
            let cmd = parts.first().unwrap_or(&"").to_string();
            let args: Vec<String> = parts[1..].iter().map(|s| s.to_string()).collect();
            MessageContent::command(text, cmd, args)
        } else {
            MessageContent::text(text)
        };

        UnifiedMessage {
            id: ts.to_string(),
            channel: "slack".to_string(),
            sender: Identity::new(user),
            content,
            timestamp: Utc::now(),
            thread_id: thread_ts.map(String::from),
            reply_to_id: None,
            chat_id: Some(channel.to_string()),
            raw: None,
            metadata: Default::default(),
        }
    }
}

#[async_trait]
impl ChannelAdapter for SlackAdapter {
    fn channel_id(&self) -> &str {
        "slack"
    }

    async fn connect(&mut self) -> Result<()> {
        use slack_morphism::prelude::*;

        let client = SlackClient::new(SlackClientHyperConnector::new()?);
        let token = SlackApiToken::new(self.config.bot_token.clone().into());
        let session = client.open_session(&token);

        // Verify connection by fetching auth info
        let auth = session
            .auth_test(&SlackApiAuthTestRequest::new())
            .await
            .map_err(|e| Error::Connection(format!("slack auth_test failed: {}", e)))?;

        self.bot_user_id = auth.user_id.map(|id| id.to_string());
        self.connected = true;

        // TODO: Set up Socket Mode or Events API listener for real-time messages
        // For socket mode: use app_token to establish WebSocket connection
        // For Events API: start HTTP server to receive event callbacks

        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        self.connected = false;
        Ok(())
    }

    fn on_message(&mut self, handler: MessageHandler) {
        self.handler = Some(handler);
    }

    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>> {
        use slack_morphism::prelude::*;

        let client = SlackClient::new(
            SlackClientHyperConnector::new()
                .map_err(|e| Error::Send(e.to_string()))?,
        );
        let token = SlackApiToken::new(self.config.bot_token.clone().into());
        let session = client.open_session(&token);

        let mut request = SlackApiChatPostMessageRequest::new(
            msg.chat_id.clone().into(),
            SlackMessageContent::new().with_text(msg.text.clone()),
        );

        // Thread support
        if let Some(ref thread_id) = msg.thread_id {
            request = request.with_thread_ts(thread_id.clone().into());
        }

        let response = session
            .chat_post_message(&request)
            .await
            .map_err(|e| Error::Send(format!("slack post_message failed: {}", e)))?;

        self.last_activity.map(|_| Utc::now());

        Ok(response.ts.map(|ts| ts.to_string()))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "slack".to_string(),
            account_id: self.bot_user_id.clone(),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
