//! Twilio SMS adapter via REST API.

use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use serde::Deserialize;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Twilio SMS configuration.
pub struct TwilioSmsConfig {
    /// Twilio Account SID.
    pub account_sid: String,
    /// Twilio Auth Token.
    pub auth_token: String,
    /// Twilio phone number (E.164 format, e.g., "+15551234567").
    pub from_number: String,
    /// Optional webhook URL for incoming messages.
    pub webhook_url: Option<String>,
}

#[derive(Deserialize)]
struct TwilioMessageResponse {
    sid: String,
    status: String,
}

/// Twilio SMS adapter.
pub struct TwilioSmsAdapter {
    config: TwilioSmsConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Option<MessageHandler>,
    http: Client,
}

impl TwilioSmsAdapter {
    /// Create a new Twilio SMS adapter.
    pub fn new(config: TwilioSmsConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: None,
            http: Client::new(),
        }
    }

    /// Convert an incoming Twilio webhook payload to a UnifiedMessage.
    pub fn webhook_to_unified(
        from: &str,
        body: &str,
        message_sid: &str,
        media_url: Option<&str>,
    ) -> UnifiedMessage {
        let content = if let Some(url) = media_url {
            MessageContent::media(body, url, None)
        } else {
            MessageContent::text(body)
        };

        UnifiedMessage {
            id: message_sid.to_string(),
            channel: "twilio_sms".to_string(),
            sender: Identity::new(from),
            content,
            timestamp: Utc::now(),
            thread_id: None,
            reply_to_id: None,
            chat_id: Some(from.to_string()),
            raw: None,
            metadata: Default::default(),
        }
    }

    /// Twilio API base URL for sending messages.
    fn api_url(&self) -> String {
        format!(
            "https://api.twilio.com/2010-04-01/Accounts/{}/Messages.json",
            self.config.account_sid
        )
    }
}

#[async_trait]
impl ChannelAdapter for TwilioSmsAdapter {
    fn channel_id(&self) -> &str {
        "twilio_sms"
    }

    async fn connect(&mut self) -> Result<()> {
        // Verify credentials by fetching account info
        let url = format!(
            "https://api.twilio.com/2010-04-01/Accounts/{}.json",
            self.config.account_sid
        );
        let resp = self
            .http
            .get(&url)
            .basic_auth(&self.config.account_sid, Some(&self.config.auth_token))
            .send()
            .await
            .map_err(|e| Error::Connection(format!("twilio auth check: {}", e)))?;

        if !resp.status().is_success() {
            return Err(Error::Connection(format!(
                "twilio auth failed: HTTP {}",
                resp.status()
            )));
        }

        // TODO: Set up webhook server for incoming SMS if webhook_url is configured

        self.connected = true;
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
        let params = [
            ("From", self.config.from_number.as_str()),
            ("To", msg.chat_id.as_str()),
            ("Body", msg.text.as_str()),
        ];

        let resp = self
            .http
            .post(&self.api_url())
            .basic_auth(&self.config.account_sid, Some(&self.config.auth_token))
            .form(&params)
            .send()
            .await
            .map_err(|e| Error::Send(format!("twilio send: {}", e)))?;

        if !resp.status().is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Send(format!("twilio send failed: {}", body)));
        }

        let response: TwilioMessageResponse = resp
            .json()
            .await
            .map_err(|e| Error::Send(format!("twilio response parse: {}", e)))?;

        Ok(Some(response.sid))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "twilio_sms".to_string(),
            account_id: Some(self.config.from_number.clone()),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
