//! Twilio Voice adapter via REST API (TwiML-based).

use async_trait::async_trait;
use chrono::Utc;
use reqwest::Client;
use serde::Deserialize;

use crate::adapter::{ChannelAdapter, MessageHandler};
use crate::error::{Error, Result};
use crate::types::*;

/// Twilio Voice configuration.
pub struct TwilioVoiceConfig {
    /// Twilio Account SID.
    pub account_sid: String,
    /// Twilio Auth Token.
    pub auth_token: String,
    /// Twilio phone number (E.164 format).
    pub from_number: String,
    /// TwiML application SID or URL for call handling.
    pub twiml_url: Option<String>,
    /// Voice to use for text-to-speech (e.g., "alice", "man", "woman").
    pub voice: String,
    /// Language for TTS.
    pub language: String,
}

impl TwilioVoiceConfig {
    /// Create with minimal required fields.
    pub fn new(
        account_sid: impl Into<String>,
        auth_token: impl Into<String>,
        from_number: impl Into<String>,
    ) -> Self {
        Self {
            account_sid: account_sid.into(),
            auth_token: auth_token.into(),
            from_number: from_number.into(),
            twiml_url: None,
            voice: "alice".to_string(),
            language: "en-US".to_string(),
        }
    }
}

#[derive(Deserialize)]
struct TwilioCallResponse {
    sid: String,
}

/// Twilio Voice adapter — initiate and manage voice calls.
pub struct TwilioVoiceAdapter {
    config: TwilioVoiceConfig,
    connected: bool,
    last_activity: Option<chrono::DateTime<Utc>>,
    handler: Option<MessageHandler>,
    http: Client,
}

impl TwilioVoiceAdapter {
    /// Create a new Twilio Voice adapter.
    pub fn new(config: TwilioVoiceConfig) -> Self {
        Self {
            config,
            connected: false,
            last_activity: None,
            handler: None,
            http: Client::new(),
        }
    }

    /// Build TwiML for text-to-speech.
    fn build_say_twiml(&self, text: &str) -> String {
        format!(
            r#"<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{}" language="{}">{}</Say></Response>"#,
            self.config.voice,
            self.config.language,
            xml_escape(text),
        )
    }

    /// Convert an incoming call status webhook to a UnifiedMessage.
    pub fn webhook_to_unified(
        from: &str,
        call_sid: &str,
        status: &str,
        speech_result: Option<&str>,
    ) -> UnifiedMessage {
        let text = speech_result.unwrap_or(status);
        let content = MessageContent::text(text);

        UnifiedMessage {
            id: call_sid.to_string(),
            channel: "twilio_voice".to_string(),
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

    fn api_url(&self) -> String {
        format!(
            "https://api.twilio.com/2010-04-01/Accounts/{}/Calls.json",
            self.config.account_sid
        )
    }
}

/// Minimal XML escaping for TwiML.
fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

#[async_trait]
impl ChannelAdapter for TwilioVoiceAdapter {
    fn channel_id(&self) -> &str {
        "twilio_voice"
    }

    async fn connect(&mut self) -> Result<()> {
        // Verify credentials
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
        // For voice, "send" initiates a call with TTS
        let twiml = self.build_say_twiml(&msg.text);

        let mut params = vec![
            ("From".to_string(), self.config.from_number.clone()),
            ("To".to_string(), msg.chat_id.clone()),
            ("Twiml".to_string(), twiml),
        ];

        // Use TwiML URL if configured instead of inline TwiML
        if let Some(ref url) = self.config.twiml_url {
            params.retain(|p| p.0 != "Twiml");
            params.push(("Url".to_string(), url.clone()));
        }

        let resp = self
            .http
            .post(&self.api_url())
            .basic_auth(&self.config.account_sid, Some(&self.config.auth_token))
            .form(&params)
            .send()
            .await
            .map_err(|e| Error::Send(format!("twilio call: {}", e)))?;

        if !resp.status().is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::Send(format!("twilio call failed: {}", body)));
        }

        let response: TwilioCallResponse = resp
            .json()
            .await
            .map_err(|e| Error::Send(format!("twilio response parse: {}", e)))?;

        Ok(Some(response.sid))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected,
            channel: "twilio_voice".to_string(),
            account_id: Some(self.config.from_number.clone()),
            error: None,
            last_activity: self.last_activity,
        }
    }
}
