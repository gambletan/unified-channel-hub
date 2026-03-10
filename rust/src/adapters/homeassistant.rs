//! Home Assistant adapter via WebSocket + REST API.

use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client as HttpClient;
use serde::{Deserialize, Serialize};

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Home Assistant configuration.
pub struct HomeAssistantConfig {
    /// Home Assistant base URL (e.g., "http://homeassistant.local:8123").
    pub base_url: String,
    /// Long-lived access token.
    pub access_token: String,
    /// Subscribe to specific event types (default: all state_changed events).
    pub event_types: Vec<String>,
}

impl HomeAssistantConfig {
    /// Create with minimal required fields.
    pub fn new(base_url: impl Into<String>, access_token: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            access_token: access_token.into(),
            event_types: vec!["state_changed".to_string()],
        }
    }
}

#[derive(Serialize)]
struct HaServiceCall {
    entity_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    title: Option<String>,
}

#[derive(Deserialize)]
struct HaState {
    entity_id: String,
    state: String,
    #[serde(default)]
    attributes: serde_json::Value,
    last_changed: String,
}

/// Home Assistant adapter.
pub struct HomeAssistantAdapter {
    config: HomeAssistantConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Option<MessageHandler>,
    http: HttpClient,
    ws_shutdown: Option<tokio::sync::oneshot::Sender<()>>,
}

impl HomeAssistantAdapter {
    /// Create a new Home Assistant adapter.
    pub fn new(config: HomeAssistantConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: None,
            http: HttpClient::new(),
            ws_shutdown: None,
        }
    }

    /// Convert a state change event to a UnifiedMessage.
    fn state_change_to_unified(
        entity_id: &str,
        old_state: &str,
        new_state: &str,
    ) -> UnifiedMessage {
        let text = format!("{}: {} → {}", entity_id, old_state, new_state);
        UnifiedMessage {
            id: uuid::Uuid::new_v4().to_string(),
            channel: "homeassistant".to_string(),
            sender: Identity::new(entity_id),
            content: MessageContent::text(text),
            timestamp: Utc::now(),
            thread_id: None,
            reply_to_id: None,
            chat_id: Some(entity_id.to_string()),
            raw: None,
            metadata: Default::default(),
        }
    }

    /// Build the WebSocket URL from the base URL.
    fn ws_url(&self) -> String {
        let base = self.config.base_url.trim_end_matches('/');
        let scheme = if base.starts_with("https") {
            "wss"
        } else {
            "ws"
        };
        let host = base
            .trim_start_matches("https://")
            .trim_start_matches("http://");
        format!("{}://{}/api/websocket", scheme, host)
    }
}

#[async_trait]
impl ChannelAdapter for HomeAssistantAdapter {
    fn channel_id(&self) -> &str {
        "homeassistant"
    }

    async fn connect(&mut self) -> Result<()> {
        // Verify REST API access
        let url = format!("{}/api/", self.config.base_url.trim_end_matches('/'));
        let resp = self
            .http
            .get(&url)
            .header(
                "Authorization",
                format!("Bearer {}", self.config.access_token),
            )
            .send()
            .await
            .map_err(|e| Error::Connection(format!("HA API check: {}", e)))?;

        if !resp.status().is_success() {
            return Err(Error::Connection(format!(
                "HA API auth failed: HTTP {}",
                resp.status()
            )));
        }

        // TODO: Establish WebSocket connection for real-time events
        // 1. Connect to ws_url()
        // 2. Receive auth_required message
        // 3. Send auth message with access_token
        // 4. Subscribe to events
        // 5. Forward state_changed events as UnifiedMessages

        let (tx, _rx) = tokio::sync::oneshot::channel();
        self.ws_shutdown = Some(tx);
        self.connected = true;
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        if let Some(tx) = self.ws_shutdown.take() {
            let _ = tx.send(());
        }
        self.connected = false;
        Ok(())
    }

    fn on_message(&mut self, handler: MessageHandler) {
        self.handler = Some(handler);
    }

    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>> {
        // "Sending" to HA means calling a service.
        // chat_id is the entity_id, text is the service call data.
        // We detect the domain and service from the entity_id.
        let entity_id = &msg.chat_id;
        let domain = entity_id
            .split('.')
            .next()
            .unwrap_or("notify");

        // For notify domain, call notify service
        // For other domains, call the appropriate service
        let (service_domain, service_name) = match domain {
            "notify" => ("notify", entity_id.split('.').nth(1).unwrap_or("notify")),
            "light" => ("light", "turn_on"),
            "switch" => ("switch", "toggle"),
            "script" => ("script", entity_id.split('.').nth(1).unwrap_or("script")),
            "automation" => ("automation", "trigger"),
            _ => ("homeassistant", "turn_on"),
        };

        let url = format!(
            "{}/api/services/{}/{}",
            self.config.base_url.trim_end_matches('/'),
            service_domain,
            service_name
        );

        let payload = if domain == "notify" {
            serde_json::json!({
                "message": msg.text,
                "title": msg.metadata.get("title").and_then(|v| v.as_str()).unwrap_or(""),
            })
        } else {
            serde_json::json!({
                "entity_id": entity_id,
            })
        };

        let resp = self
            .http
            .post(&url)
            .header(
                "Authorization",
                format!("Bearer {}", self.config.access_token),
            )
            .json(&payload)
            .send()
            .await
            .map_err(|e| Error::Send(format!("HA service call: {}", e)))?;

        if !resp.status().is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Send(format!("HA service call failed: {}", body)));
        }

        Ok(Some(uuid::Uuid::new_v4().to_string()))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "homeassistant".to_string(),
            account_id: Some(self.config.base_url.clone()),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
