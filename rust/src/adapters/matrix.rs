//! Matrix adapter using the matrix-sdk crate.

use async_trait::async_trait;
use chrono::Utc;
use std::sync::Arc;
use tokio::sync::Mutex;

use matrix_sdk::{
    config::SyncSettings,
    room::Room,
    ruma::{
        events::room::message::{
            MessageType, OriginalSyncRoomMessageEvent, RoomMessageEventContent,
        },
        OwnedRoomId, RoomId,
    },
    Client,
};

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Matrix adapter configuration.
pub struct MatrixConfig {
    /// Homeserver URL (e.g., "https://matrix.org").
    pub homeserver_url: String,
    /// Username (localpart, e.g., "bot" for @bot:matrix.org).
    pub username: String,
    /// Password.
    pub password: String,
    /// Optional device display name.
    pub device_name: Option<String>,
}

/// Matrix adapter backed by matrix-sdk.
pub struct MatrixAdapter {
    config: MatrixConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    user_id: Option<String>,
    handler: Arc<Mutex<Option<MessageHandler>>>,
    client: Option<Client>,
}

impl MatrixAdapter {
    /// Create a new Matrix adapter.
    pub fn new(config: MatrixConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            user_id: None,
            handler: Arc::new(Mutex::new(None)),
            client: None,
        }
    }
}

#[async_trait]
impl ChannelAdapter for MatrixAdapter {
    fn channel_id(&self) -> &str {
        "matrix"
    }

    async fn connect(&mut self) -> Result<()> {
        let client = Client::builder()
            .homeserver_url(&self.config.homeserver_url)
            .build()
            .await
            .map_err(|e| Error::Connection(format!("matrix client build: {}", e)))?;

        client
            .matrix_auth()
            .login_username(&self.config.username, &self.config.password)
            .device_display_name(
                self.config
                    .device_name
                    .as_deref()
                    .unwrap_or("unified-channel"),
            )
            .await
            .map_err(|e| Error::Connection(format!("matrix login: {}", e)))?;

        self.user_id = client.user_id().map(|id| id.to_string());
        self.client = Some(client.clone());

        // Register message handler
        let handler = Arc::clone(&self.handler);
        client.add_event_handler(
            move |event: OriginalSyncRoomMessageEvent, room: Room| {
                let handler = Arc::clone(&handler);
                async move {
                    let h = handler.lock().await;
                    if let Some(ref callback) = *h {
                        let text = match &event.content.msgtype {
                            MessageType::Text(t) => t.body.clone(),
                            MessageType::Notice(n) => n.body.clone(),
                            _ => return,
                        };

                        let unified = UnifiedMessage {
                            id: event.event_id.to_string(),
                            channel: "matrix".to_string(),
                            sender: Identity::new(event.sender.to_string()),
                            content: MessageContent::text(text),
                            timestamp: Utc::now(),
                            thread_id: None,
                            reply_to_id: None,
                            chat_id: Some(room.room_id().to_string()),
                            raw: None,
                            metadata: Default::default(),
                        };
                        callback(unified);
                    }
                }
            },
        );

        // Start sync in background
        let sync_client = client.clone();
        tokio::spawn(async move {
            sync_client.sync(SyncSettings::default()).await;
        });

        self.connected = true;
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        // matrix-sdk doesn't have an explicit disconnect; dropping the sync task suffices
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
            Error::Send("not connected".to_string())
        })?;

        let room_id: OwnedRoomId = RoomId::parse(&msg.chat_id)
            .map_err(|e| Error::Send(format!("invalid room_id: {}", e)))?;

        let room = client
            .get_room(&room_id)
            .ok_or_else(|| Error::Send(format!("room not found: {}", msg.chat_id)))?;

        let content = RoomMessageEventContent::text_plain(&msg.text);
        let response = room
            .send(content)
            .await
            .map_err(|e| Error::Send(format!("matrix send: {}", e)))?;

        Ok(Some(response.event_id.to_string()))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "matrix".to_string(),
            account_id: self.user_id.clone(),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
