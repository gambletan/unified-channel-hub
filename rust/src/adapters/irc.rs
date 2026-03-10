//! IRC adapter using the irc crate.

use async_trait::async_trait;
use chrono::Utc;
use std::sync::Arc;
use tokio::sync::Mutex;

use irc::client::prelude::*;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// IRC adapter configuration.
pub struct IrcConfig {
    /// IRC server hostname.
    pub server: String,
    /// IRC server port (default 6697 for TLS).
    pub port: u16,
    /// Use TLS.
    pub use_tls: bool,
    /// Nickname.
    pub nickname: String,
    /// Username (ident).
    pub username: Option<String>,
    /// Real name / GECOS.
    pub realname: Option<String>,
    /// Channels to join on connect.
    pub channels: Vec<String>,
    /// NickServ password for identification.
    pub nickserv_password: Option<String>,
}

impl IrcConfig {
    /// Create with minimal required fields.
    pub fn new(server: impl Into<String>, nickname: impl Into<String>) -> Self {
        Self {
            server: server.into(),
            port: 6697,
            use_tls: true,
            nickname: nickname.into(),
            username: None,
            realname: None,
            channels: Vec::new(),
            nickserv_password: None,
        }
    }

    /// Builder: add a channel to auto-join.
    pub fn with_channel(mut self, channel: impl Into<String>) -> Self {
        self.channels.push(channel.into());
        self
    }
}

/// IRC adapter.
pub struct IrcAdapter {
    config: IrcConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Arc<Mutex<Option<MessageHandler>>>,
    client: Option<Arc<Mutex<irc::client::Client>>>,
    shutdown_tx: Option<tokio::sync::oneshot::Sender<()>>,
}

impl IrcAdapter {
    /// Create a new IRC adapter.
    pub fn new(config: IrcConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: Arc::new(Mutex::new(None)),
            client: None,
            shutdown_tx: None,
        }
    }

    /// Convert an IRC message to a UnifiedMessage.
    fn irc_to_unified(prefix: Option<&str>, target: &str, text: &str) -> UnifiedMessage {
        let nick = prefix
            .and_then(|p| p.split('!').next())
            .unwrap_or("unknown");

        let is_cmd = text.starts_with('!') || text.starts_with('.');
        let content = if is_cmd {
            let stripped = &text[1..];
            let parts: Vec<&str> = stripped.split_whitespace().collect();
            let cmd = parts.first().unwrap_or(&"").to_string();
            let args: Vec<String> = parts[1..].iter().map(|s| s.to_string()).collect();
            MessageContent::command(text, cmd, args)
        } else {
            MessageContent::text(text)
        };

        UnifiedMessage {
            id: uuid::Uuid::new_v4().to_string(),
            channel: "irc".to_string(),
            sender: Identity::new(nick),
            content,
            timestamp: Utc::now(),
            thread_id: None,
            reply_to_id: None,
            chat_id: Some(target.to_string()),
            raw: None,
            metadata: Default::default(),
        }
    }
}

#[async_trait]
impl ChannelAdapter for IrcAdapter {
    fn channel_id(&self) -> &str {
        "irc"
    }

    async fn connect(&mut self) -> Result<()> {
        let irc_config = Config {
            nickname: Some(self.config.nickname.clone()),
            username: self.config.username.clone(),
            realname: self.config.realname.clone(),
            server: Some(self.config.server.clone()),
            port: Some(self.config.port),
            use_tls: Some(self.config.use_tls),
            channels: self.config.channels.clone(),
            nick_password: self.config.nickserv_password.clone(),
            ..Config::default()
        };

        let client = irc::client::Client::from_config(irc_config)
            .await
            .map_err(|e| Error::Connection(format!("IRC connect: {}", e)))?;

        client
            .identify()
            .map_err(|e| Error::Connection(format!("IRC identify: {}", e)))?;

        let client = Arc::new(Mutex::new(client));
        self.client = Some(Arc::clone(&client));

        // Start message listener
        let handler = Arc::clone(&self.handler);
        let (tx, mut rx) = tokio::sync::oneshot::channel::<()>();
        self.shutdown_tx = Some(tx);

        tokio::spawn(async move {
            use futures::StreamExt;
            let mut stream = {
                let c = client.lock().await;
                c.stream().unwrap()
            };

            loop {
                tokio::select! {
                    msg = stream.next() => {
                        match msg {
                            Some(Ok(message)) => {
                                if let Command::PRIVMSG(ref target, ref text) = message.command {
                                    let prefix = message.prefix.as_ref().map(|p| p.to_string());
                                    let unified = IrcAdapter::irc_to_unified(
                                        prefix.as_deref(),
                                        target,
                                        text,
                                    );
                                    let h = handler.lock().await;
                                    if let Some(ref callback) = *h {
                                        callback(unified);
                                    }
                                }
                            }
                            Some(Err(e)) => {
                                tracing::error!(error = %e, "IRC stream error");
                                break;
                            }
                            None => break,
                        }
                    }
                    _ = &mut rx => break,
                }
            }
        });

        self.connected = true;
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        if let Some(ref client) = self.client {
            let c = client.lock().await;
            c.send_quit("unified-channel shutdown")
                .map_err(|e| Error::Connection(format!("IRC quit: {}", e)))?;
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
            Error::Send("IRC not connected".to_string())
        })?;

        let c = client.lock().await;
        c.send_privmsg(&msg.chat_id, &msg.text)
            .map_err(|e| Error::Send(format!("IRC send: {}", e)))?;

        Ok(None) // IRC doesn't return message IDs
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "irc".to_string(),
            account_id: Some(self.config.nickname.clone()),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
