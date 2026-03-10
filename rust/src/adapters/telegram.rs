//! Telegram adapter using the teloxide crate.

use async_trait::async_trait;
use chrono::{TimeZone, Utc};
use std::sync::Arc;
use tokio::sync::Mutex;

use teloxide::prelude::*;
use teloxide::types::{
    InlineKeyboardButton, InlineKeyboardMarkup, MessageId, ParseMode as TgParseMode,
    ReplyParameters,
};

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Telegram adapter backed by teloxide.
pub struct TelegramAdapter {
    token: String,
    parse_mode: String,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    bot_username: Option<String>,
    handler: Option<MessageHandler>,
    bot: Option<Bot>,
    shutdown_tx: Option<tokio::sync::oneshot::Sender<()>>,
}

impl TelegramAdapter {
    /// Create a new Telegram adapter with the given bot token.
    pub fn new(token: impl Into<String>) -> Self {
        Self {
            token: token.into(),
            parse_mode: "Markdown".to_string(),
            connected: false,
            last_activity: None,
            bot_username: None,
            handler: None,
            bot: None,
            shutdown_tx: None,
        }
    }

    /// Builder: set default parse mode.
    pub fn with_parse_mode(mut self, mode: impl Into<String>) -> Self {
        self.parse_mode = mode.into();
        self
    }

    /// Convert a teloxide Message to a UnifiedMessage.
    fn to_unified(msg: &teloxide::types::Message) -> Option<UnifiedMessage> {
        let from = msg.from.as_ref()?;
        let text = msg.text().unwrap_or("").to_string();
        let is_cmd = text.starts_with('/');

        let content = if is_cmd {
            let parts: Vec<&str> = text[1..].split_whitespace().collect();
            let cmd = parts
                .first()
                .unwrap_or(&"")
                .split('@')
                .next()
                .unwrap_or("")
                .to_string();
            let args: Vec<String> = parts[1..].iter().map(|s| s.to_string()).collect();
            MessageContent::command(text.clone(), cmd, args)
        } else if msg.photo().is_some() || msg.video().is_some() || msg.document().is_some() {
            // Has media
            let media_url = None; // File URLs require an API call to resolve
            MessageContent::media(text.clone(), media_url.unwrap_or_default(), None)
        } else {
            MessageContent::text(text)
        };

        let display_name = [
            Some(from.first_name.clone()),
            from.last_name.clone(),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join(" ");

        let sender = Identity::new(from.id.to_string())
            .with_username(from.username.clone().unwrap_or_default())
            .with_display_name(display_name);

        let timestamp = Utc
            .timestamp_opt(msg.date.timestamp(), 0)
            .single()
            .unwrap_or_else(Utc::now);

        let reply_to_id = msg
            .reply_to_message()
            .map(|r| r.id.0.to_string());

        Some(UnifiedMessage {
            id: msg.id.0.to_string(),
            channel: "telegram".to_string(),
            sender,
            content,
            timestamp,
            thread_id: msg.thread_id.map(|t| t.to_string()),
            reply_to_id,
            chat_id: Some(msg.chat.id.to_string()),
            raw: None,
            metadata: Default::default(),
        })
    }

    /// Build teloxide InlineKeyboardMarkup from button rows.
    fn build_keyboard(buttons: &[Vec<Button>]) -> InlineKeyboardMarkup {
        let rows: Vec<Vec<InlineKeyboardButton>> = buttons
            .iter()
            .map(|row| {
                row.iter()
                    .map(|b| {
                        if let Some(ref url) = b.url {
                            InlineKeyboardButton::url(b.label.clone(), url.parse().unwrap())
                        } else {
                            InlineKeyboardButton::callback(
                                b.label.clone(),
                                b.callback_data.clone().unwrap_or_default(),
                            )
                        }
                    })
                    .collect()
            })
            .collect();
        InlineKeyboardMarkup::new(rows)
    }

    /// Parse a parse_mode string to teloxide enum.
    fn parse_mode_from_str(s: &str) -> Option<TgParseMode> {
        match s.to_lowercase().as_str() {
            "markdown" | "markdownv2" => Some(TgParseMode::MarkdownV2),
            "html" => Some(TgParseMode::Html),
            _ => None,
        }
    }
}

#[async_trait]
impl ChannelAdapter for TelegramAdapter {
    fn channel_id(&self) -> &str {
        "telegram"
    }

    async fn connect(&mut self) -> Result<()> {
        let bot = Bot::new(&self.token);

        // Fetch bot info
        let me = bot
            .get_me()
            .await
            .map_err(|e| Error::Connection(e.to_string()))?;
        self.bot_username = me.username.clone();
        self.bot = Some(bot.clone());

        // Set up message listener in a background task
        let handler = self.handler.take();
        let (tx, mut rx) = tokio::sync::oneshot::channel::<()>();
        self.shutdown_tx = Some(tx);

        if let Some(handler) = handler {
            let handler = Arc::new(handler);
            tokio::spawn(async move {
                let handler_clone = Arc::clone(&handler);
                let listener = teloxide::repl(bot, move |msg: Message, _bot: Bot| {
                    let h = Arc::clone(&handler_clone);
                    async move {
                        if let Some(unified) = TelegramAdapter::to_unified(&msg) {
                            h(unified);
                        }
                        Ok(())
                    }
                });

                tokio::select! {
                    _ = listener => {}
                    _ = &mut rx => {}
                }
            });
        }

        self.connected = true;
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
        self.connected = false;
        Ok(())
    }

    fn on_message(&mut self, handler: MessageHandler) {
        self.handler = Some(handler);
    }

    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>> {
        let bot = self.bot.as_ref().ok_or_else(|| {
            Error::Send("not connected".to_string())
        })?;

        let chat_id: i64 = msg
            .chat_id
            .parse()
            .map_err(|_| Error::Send(format!("invalid chat_id: {}", msg.chat_id)))?;

        let mut request = bot.send_message(ChatId(chat_id), &msg.text);

        // Parse mode
        let pm_str = msg
            .parse_mode
            .as_deref()
            .unwrap_or(&self.parse_mode);
        if let Some(pm) = Self::parse_mode_from_str(pm_str) {
            request = request.parse_mode(pm);
        }

        // Reply
        if let Some(ref reply_id) = msg.reply_to_id {
            if let Ok(mid) = reply_id.parse::<i32>() {
                request = request.reply_parameters(ReplyParameters::new(MessageId(mid)));
            }
        }

        // Keyboard
        if let Some(ref buttons) = msg.buttons {
            request = request.reply_markup(Self::build_keyboard(buttons));
        }

        let sent = request
            .await
            .map_err(|e| Error::Send(e.to_string()))?;

        Ok(Some(sent.id.0.to_string()))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "telegram".to_string(),
            account_id: self.bot_username.clone(),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
