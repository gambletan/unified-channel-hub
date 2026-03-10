//! Generic webhook adapter for LINE, WhatsApp, and other webhook-based platforms.

use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Webhook adapter configuration.
pub struct WebhookConfig {
    /// Unique channel name for this webhook (e.g., "line", "whatsapp", "custom").
    pub channel_name: String,
    /// Outbound webhook URL to POST messages to.
    pub outbound_url: String,
    /// HTTP headers to include in outbound requests (e.g., authorization).
    pub headers: HashMap<String, String>,
    /// Optional secret for verifying inbound webhook signatures.
    pub verify_secret: Option<String>,
    /// Template for the outbound JSON body. Use `{text}` and `{chat_id}` placeholders.
    pub body_template: Option<String>,
    /// JSON path for extracting the message ID from the response.
    pub response_id_path: Option<String>,
}

impl WebhookConfig {
    /// Create with minimal required fields.
    pub fn new(
        channel_name: impl Into<String>,
        outbound_url: impl Into<String>,
    ) -> Self {
        Self {
            channel_name: channel_name.into(),
            outbound_url: outbound_url.into(),
            headers: HashMap::new(),
            verify_secret: None,
            body_template: None,
            response_id_path: None,
        }
    }

    /// Builder: add a header.
    pub fn with_header(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.insert(key.into(), value.into());
        self
    }

    /// Builder: set authorization header.
    pub fn with_bearer_token(self, token: impl Into<String>) -> Self {
        self.with_header("Authorization", format!("Bearer {}", token.into()))
    }
}

/// Standardized inbound webhook payload (parsed from platform-specific JSON).
#[derive(Debug, Deserialize)]
pub struct InboundWebhookPayload {
    /// Sender identifier.
    pub sender_id: String,
    /// Optional sender name.
    pub sender_name: Option<String>,
    /// Message text.
    pub text: String,
    /// Message ID from the platform.
    pub message_id: Option<String>,
    /// Chat/conversation ID.
    pub chat_id: Option<String>,
    /// Optional media URL.
    pub media_url: Option<String>,
    /// Optional media MIME type.
    pub media_type: Option<String>,
    /// Optional reply-to message ID.
    pub reply_to_id: Option<String>,
    /// Raw JSON from the platform.
    pub raw: Option<serde_json::Value>,
}

/// Generic webhook adapter.
pub struct WebhookAdapter {
    config: WebhookConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Option<MessageHandler>,
    http: Client,
}

impl WebhookAdapter {
    /// Create a new webhook adapter.
    pub fn new(config: WebhookConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: None,
            http: Client::new(),
        }
    }

    /// Convert an inbound webhook payload to a UnifiedMessage.
    pub fn payload_to_unified(&self, payload: InboundWebhookPayload) -> UnifiedMessage {
        let content = if let Some(ref url) = payload.media_url {
            MessageContent::media(
                &payload.text,
                url.clone(),
                payload.media_type.clone(),
            )
        } else {
            MessageContent::text(&payload.text)
        };

        let mut sender = Identity::new(&payload.sender_id);
        if let Some(ref name) = payload.sender_name {
            sender = sender.with_display_name(name.clone());
        }

        UnifiedMessage {
            id: payload
                .message_id
                .unwrap_or_else(|| uuid::Uuid::new_v4().to_string()),
            channel: self.config.channel_name.clone(),
            sender,
            content,
            timestamp: Utc::now(),
            thread_id: None,
            reply_to_id: payload.reply_to_id,
            chat_id: payload.chat_id,
            raw: payload.raw,
            metadata: Default::default(),
        }
    }

    /// Process an inbound webhook request. Call this from your HTTP handler.
    pub fn process_inbound(&self, payload: InboundWebhookPayload) {
        if let Some(ref handler) = self.handler {
            let unified = self.payload_to_unified(payload);
            handler(unified);
        }
    }

    /// Build the outbound JSON body from the template or default format.
    fn build_outbound_body(&self, msg: &OutboundMessage) -> serde_json::Value {
        if let Some(ref template) = self.config.body_template {
            let body = template
                .replace("{text}", &msg.text)
                .replace("{chat_id}", &msg.chat_id);
            serde_json::from_str(&body).unwrap_or_else(|_| {
                serde_json::json!({
                    "chat_id": msg.chat_id,
                    "text": msg.text,
                })
            })
        } else {
            let mut body = serde_json::json!({
                "chat_id": msg.chat_id,
                "text": msg.text,
            });
            if let Some(ref reply_id) = msg.reply_to_id {
                body["reply_to_id"] = serde_json::Value::String(reply_id.clone());
            }
            if let Some(ref media_url) = msg.media_url {
                body["media_url"] = serde_json::Value::String(media_url.clone());
            }
            body
        }
    }
}

#[async_trait]
impl ChannelAdapter for WebhookAdapter {
    fn channel_id(&self) -> &str {
        &self.config.channel_name
    }

    async fn connect(&mut self) -> Result<()> {
        // Webhook adapters are "always connected" — they receive via HTTP push
        // and send via HTTP POST. Verify the outbound URL is reachable.
        // For inbound, the caller must set up an HTTP server externally.
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
        let body = self.build_outbound_body(&msg);

        let mut request = self.http.post(&self.config.outbound_url).json(&body);

        for (key, value) in &self.config.headers {
            request = request.header(key.as_str(), value.as_str());
        }

        let resp = request
            .send()
            .await
            .map_err(|e| Error::Send(format!("webhook send: {}", e)))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Send(format!(
                "webhook send failed: HTTP {} - {}",
                status, body
            )));
        }

        // Try to extract message ID from response
        if let Some(ref path) = self.config.response_id_path {
            if let Ok(json) = resp.json::<serde_json::Value>().await {
                let id = json
                    .pointer(path)
                    .and_then(|v| v.as_str())
                    .map(String::from);
                return Ok(id);
            }
        }

        Ok(None)
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: self.config.channel_name.clone(),
            account_id: None,
            error: None,
            last_activity: self.last_activity,
        }
    }
}
