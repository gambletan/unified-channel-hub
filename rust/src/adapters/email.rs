//! Email adapter using lettre (SMTP) and async-imap (IMAP).

use async_trait::async_trait;
use chrono::Utc;
use lettre::message::Mailbox;
use lettre::transport::smtp::authentication::Credentials;
use lettre::{AsyncSmtpTransport, AsyncTransport, Message, Tokio1Executor};

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// SMTP + IMAP configuration.
pub struct EmailConfig {
    /// SMTP server host.
    pub smtp_host: String,
    /// SMTP port (usually 587 for STARTTLS, 465 for implicit TLS).
    pub smtp_port: u16,
    /// IMAP server host.
    pub imap_host: String,
    /// IMAP port (usually 993 for TLS).
    pub imap_port: u16,
    /// Username (usually the email address).
    pub username: String,
    /// Password or app-specific password.
    pub password: String,
    /// From address for outbound messages.
    pub from_address: String,
    /// Display name for the From header.
    pub from_name: Option<String>,
    /// IMAP folder to watch (default "INBOX").
    pub watch_folder: String,
    /// Poll interval for IMAP IDLE fallback, in seconds.
    pub poll_interval_secs: u64,
}

impl EmailConfig {
    /// Create config with minimal required fields.
    pub fn new(
        smtp_host: impl Into<String>,
        imap_host: impl Into<String>,
        username: impl Into<String>,
        password: impl Into<String>,
        from_address: impl Into<String>,
    ) -> Self {
        Self {
            smtp_host: smtp_host.into(),
            smtp_port: 587,
            imap_host: imap_host.into(),
            imap_port: 993,
            username: username.into(),
            password: password.into(),
            from_address: from_address.into(),
            from_name: None,
            watch_folder: "INBOX".to_string(),
            poll_interval_secs: 30,
        }
    }
}

/// Email adapter for sending/receiving via SMTP and IMAP.
pub struct EmailAdapter {
    config: EmailConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Option<MessageHandler>,
    smtp: Option<AsyncSmtpTransport<Tokio1Executor>>,
}

impl EmailAdapter {
    /// Create a new email adapter.
    pub fn new(config: EmailConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: None,
            smtp: None,
        }
    }

    /// Parse a raw email into a UnifiedMessage.
    fn email_to_unified(
        from: &str,
        subject: &str,
        body: &str,
        message_id: &str,
        in_reply_to: Option<&str>,
    ) -> UnifiedMessage {
        let text = if subject.is_empty() {
            body.to_string()
        } else {
            format!("{}\n\n{}", subject, body)
        };

        UnifiedMessage {
            id: message_id.to_string(),
            channel: "email".to_string(),
            sender: Identity::new(from),
            content: MessageContent::text(text),
            timestamp: Utc::now(),
            thread_id: in_reply_to.map(String::from),
            reply_to_id: in_reply_to.map(String::from),
            chat_id: Some(from.to_string()),
            raw: None,
            metadata: Default::default(),
        }
    }
}

#[async_trait]
impl ChannelAdapter for EmailAdapter {
    fn channel_id(&self) -> &str {
        "email"
    }

    async fn connect(&mut self) -> Result<()> {
        // Set up SMTP transport
        let creds = Credentials::new(
            self.config.username.clone(),
            self.config.password.clone(),
        );

        let smtp = AsyncSmtpTransport::<Tokio1Executor>::relay(&self.config.smtp_host)
            .map_err(|e| Error::Connection(format!("SMTP relay error: {}", e)))?
            .credentials(creds.clone())
            .port(self.config.smtp_port)
            .build();

        self.smtp = Some(smtp);

        // Set up IMAP listener in background
        let imap_host = self.config.imap_host.clone();
        let imap_port = self.config.imap_port;
        let username = self.config.username.clone();
        let password = self.config.password.clone();
        let folder = self.config.watch_folder.clone();
        let poll_interval = self.config.poll_interval_secs;

        // TODO: Start IMAP IDLE loop in background task
        // The loop would:
        // 1. Connect to IMAP server with TLS
        // 2. SELECT the watch folder
        // 3. Use IDLE to wait for new messages
        // 4. On new message, FETCH and convert to UnifiedMessage
        // 5. Call self.handler with the unified message
        // 6. Fall back to polling if IDLE not supported

        self.connected = true;
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        self.smtp = None;
        self.connected = false;
        Ok(())
    }

    fn on_message(&mut self, handler: MessageHandler) {
        self.handler = Some(handler);
    }

    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>> {
        let smtp = self.smtp.as_ref().ok_or_else(|| {
            Error::Send("SMTP not connected".to_string())
        })?;

        let from_mailbox: Mailbox = if let Some(ref name) = self.config.from_name {
            format!("{} <{}>", name, self.config.from_address)
                .parse()
                .map_err(|e| Error::Send(format!("invalid from address: {}", e)))?
        } else {
            self.config
                .from_address
                .parse()
                .map_err(|e| Error::Send(format!("invalid from address: {}", e)))?
        };

        let to_mailbox: Mailbox = msg
            .chat_id
            .parse()
            .map_err(|e| Error::Send(format!("invalid to address: {}", e)))?;

        // Extract subject from first line or metadata
        let (subject, body) = if let Some(idx) = msg.text.find('\n') {
            (msg.text[..idx].to_string(), msg.text[idx + 1..].to_string())
        } else {
            (msg.text.clone(), String::new())
        };

        let mut builder = Message::builder()
            .from(from_mailbox)
            .to(to_mailbox)
            .subject(&subject);

        // Thread support via In-Reply-To header
        if let Some(ref reply_id) = msg.reply_to_id {
            builder = builder.in_reply_to(reply_id.to_string());
        }

        let email = builder
            .body(body)
            .map_err(|e| Error::Send(format!("failed to build email: {}", e)))?;

        let message_id = email
            .headers()
            .get_raw("Message-ID")
            .map(|v| String::from_utf8_lossy(v).to_string());

        smtp.send(email)
            .await
            .map_err(|e| Error::Send(format!("SMTP send failed: {}", e)))?;

        Ok(message_id)
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "email".to_string(),
            account_id: Some(self.config.from_address.clone()),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
