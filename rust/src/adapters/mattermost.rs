//! Mattermost adapter via WebSocket + REST API.

use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client as HttpClient;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::Mutex;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Mattermost configuration.
pub struct MattermostConfig {
    /// Server URL (e.g., "https://mattermost.example.com").
    pub server_url: String,
    /// Personal access token or bot token.
    pub access_token: String,
    /// Team ID to listen to (optional; listens to all if not set).
    pub team_id: Option<String>,
}

impl MattermostConfig {
    /// Create with minimal required fields.
    pub fn new(server_url: impl Into<String>, access_token: impl Into<String>) -> Self {
        Self {
            server_url: server_url.into(),
            access_token: access_token.into(),
            team_id: None,
        }
    }
}

#[derive(Deserialize)]
struct MmUser {
    id: String,
    username: String,
    #[serde(default)]
    nickname: String,
}

#[derive(Deserialize)]
struct MmPost {
    id: String,
    channel_id: String,
    user_id: String,
    message: String,
    #[serde(default)]
    root_id: String,
    create_at: i64,
}

#[derive(Serialize)]
struct MmCreatePost {
    channel_id: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    root_id: Option<String>,
}

#[derive(Deserialize)]
struct MmCreatePostResponse {
    id: String,
}

/// Mattermost WebSocket event payload.
#[derive(Deserialize)]
struct MmWsEvent {
    event: String,
    data: serde_json::Value,
}

/// Mattermost adapter.
pub struct MattermostAdapter {
    config: MattermostConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Arc<Mutex<Option<MessageHandler>>>,
    http: HttpClient,
    user_id: Option<String>,
    ws_shutdown: Option<tokio::sync::oneshot::Sender<()>>,
}

impl MattermostAdapter {
    /// Create a new Mattermost adapter.
    pub fn new(config: MattermostConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: Arc::new(Mutex::new(None)),
            http: HttpClient::new(),
            user_id: None,
            ws_shutdown: None,
        }
    }

    /// REST API base URL.
    fn api_url(&self, path: &str) -> String {
        format!(
            "{}/api/v4{}",
            self.config.server_url.trim_end_matches('/'),
            path
        )
    }

    /// WebSocket URL.
    fn ws_url(&self) -> String {
        let base = self.config.server_url.trim_end_matches('/');
        let scheme = if base.starts_with("https") {
            "wss"
        } else {
            "ws"
        };
        let host = base
            .trim_start_matches("https://")
            .trim_start_matches("http://");
        format!("{}://{}/api/v4/websocket", scheme, host)
    }

    /// Convert a Mattermost post to a UnifiedMessage.
    fn post_to_unified(post: &MmPost, username: &str) -> UnifiedMessage {
        let text = &post.message;
        let is_cmd = text.starts_with('/');

        let content = if is_cmd {
            let parts: Vec<&str> = text[1..].split_whitespace().collect();
            let cmd = parts.first().unwrap_or(&"").to_string();
            let args: Vec<String> = parts[1..].iter().map(|s| s.to_string()).collect();
            MessageContent::command(text, cmd, args)
        } else {
            MessageContent::text(text)
        };

        let sender = Identity::new(&post.user_id).with_username(username.to_string());

        let thread_id = if post.root_id.is_empty() {
            None
        } else {
            Some(post.root_id.clone())
        };

        UnifiedMessage {
            id: post.id.clone(),
            channel: "mattermost".to_string(),
            sender,
            content,
            timestamp: chrono::DateTime::from_timestamp_millis(post.create_at)
                .unwrap_or_else(Utc::now),
            thread_id,
            reply_to_id: None,
            chat_id: Some(post.channel_id.clone()),
            raw: None,
            metadata: Default::default(),
        }
    }
}

#[async_trait]
impl ChannelAdapter for MattermostAdapter {
    fn channel_id(&self) -> &str {
        "mattermost"
    }

    async fn connect(&mut self) -> Result<()> {
        // Verify auth by fetching current user
        let resp = self
            .http
            .get(&self.api_url("/users/me"))
            .header(
                "Authorization",
                format!("Bearer {}", self.config.access_token),
            )
            .send()
            .await
            .map_err(|e| Error::Connection(format!("mattermost auth: {}", e)))?;

        if !resp.status().is_success() {
            return Err(Error::Connection(format!(
                "mattermost auth failed: HTTP {}",
                resp.status()
            )));
        }

        let user: MmUser = resp
            .json()
            .await
            .map_err(|e| Error::Connection(format!("mattermost user parse: {}", e)))?;
        self.user_id = Some(user.id.clone());

        // TODO: Establish WebSocket connection for real-time events
        // 1. Connect to ws_url()
        // 2. Send auth challenge with access_token
        // 3. Listen for "posted" events
        // 4. Parse post JSON and convert to UnifiedMessage
        // 5. Call handler

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
        let post = MmCreatePost {
            channel_id: msg.chat_id.clone(),
            message: msg.text.clone(),
            root_id: msg.thread_id.clone().or(msg.reply_to_id.clone()),
        };

        let resp = self
            .http
            .post(&self.api_url("/posts"))
            .header(
                "Authorization",
                format!("Bearer {}", self.config.access_token),
            )
            .json(&post)
            .send()
            .await
            .map_err(|e| Error::Send(format!("mattermost send: {}", e)))?;

        if !resp.status().is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Send(format!("mattermost send failed: {}", body)));
        }

        let response: MmCreatePostResponse = resp
            .json()
            .await
            .map_err(|e| Error::Send(format!("mattermost response parse: {}", e)))?;

        Ok(Some(response.id))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "mattermost".to_string(),
            account_id: self.user_id.clone(),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
