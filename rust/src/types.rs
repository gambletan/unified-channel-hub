//! Core types shared across all channels: messages, identities, content, buttons.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use uuid::Uuid;

/// The kind of content a message carries.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ContentType {
    Text,
    Command,
    Media,
    Reaction,
    Edit,
    Callback,
}

impl Default for ContentType {
    fn default() -> Self {
        Self::Text
    }
}

/// Sender / user identity.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Identity {
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub username: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
}

impl Identity {
    /// Create a new identity with just an id.
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            username: None,
            display_name: None,
        }
    }

    /// Builder: set username.
    pub fn with_username(mut self, username: impl Into<String>) -> Self {
        self.username = Some(username.into());
        self
    }

    /// Builder: set display name.
    pub fn with_display_name(mut self, name: impl Into<String>) -> Self {
        self.display_name = Some(name.into());
        self
    }

    /// Best available display string.
    pub fn display(&self) -> &str {
        self.display_name
            .as_deref()
            .or(self.username.as_deref())
            .unwrap_or(&self.id)
    }
}

/// Structured message content.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MessageContent {
    #[serde(rename = "type")]
    pub content_type: ContentType,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub command: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub args: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub media_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub callback_data: Option<String>,
}

impl MessageContent {
    /// Create plain text content.
    pub fn text(text: impl Into<String>) -> Self {
        Self {
            content_type: ContentType::Text,
            text: text.into(),
            ..Default::default()
        }
    }

    /// Create command content.
    pub fn command(text: impl Into<String>, command: impl Into<String>, args: Vec<String>) -> Self {
        Self {
            content_type: ContentType::Command,
            text: text.into(),
            command: Some(command.into()),
            args: Some(args),
            ..Default::default()
        }
    }

    /// Create media content.
    pub fn media(text: impl Into<String>, url: impl Into<String>, mime: Option<String>) -> Self {
        Self {
            content_type: ContentType::Media,
            text: text.into(),
            media_url: Some(url.into()),
            media_type: mime,
            ..Default::default()
        }
    }

    /// Create callback content.
    pub fn callback(data: impl Into<String>) -> Self {
        let d: String = data.into();
        Self {
            content_type: ContentType::Callback,
            text: d.clone(),
            callback_data: Some(d),
            ..Default::default()
        }
    }
}

/// A unified inbound message from any channel.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UnifiedMessage {
    pub id: String,
    pub channel: String,
    pub sender: Identity,
    pub content: MessageContent,
    pub timestamp: DateTime<Utc>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reply_to_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chat_id: Option<String>,
    #[serde(skip)]
    pub raw: Option<serde_json::Value>,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

impl UnifiedMessage {
    /// Create a minimal message for testing / programmatic use.
    pub fn new(
        channel: impl Into<String>,
        sender: Identity,
        content: MessageContent,
    ) -> Self {
        Self {
            id: Uuid::new_v4().to_string(),
            channel: channel.into(),
            sender,
            content,
            timestamp: Utc::now(),
            thread_id: None,
            reply_to_id: None,
            chat_id: None,
            raw: None,
            metadata: HashMap::new(),
        }
    }
}

/// An inline button (for keyboards / action rows).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Button {
    pub label: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub callback_data: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
}

impl Button {
    /// Create a callback button.
    pub fn callback(label: impl Into<String>, data: impl Into<String>) -> Self {
        Self {
            label: label.into(),
            callback_data: Some(data.into()),
            url: None,
        }
    }

    /// Create a URL button.
    pub fn link(label: impl Into<String>, url: impl Into<String>) -> Self {
        Self {
            label: label.into(),
            callback_data: None,
            url: Some(url.into()),
        }
    }
}

/// An outbound message to be sent through a channel.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct OutboundMessage {
    pub chat_id: String,
    pub text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reply_to_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub media_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parse_mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub buttons: Option<Vec<Vec<Button>>>,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

impl OutboundMessage {
    /// Create a simple text outbound message.
    pub fn text(chat_id: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            chat_id: chat_id.into(),
            text: text.into(),
            ..Default::default()
        }
    }
}

/// Connection status of a channel adapter.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChannelStatus {
    pub connected: bool,
    pub channel: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub account_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_activity: Option<DateTime<Utc>>,
}

impl ChannelStatus {
    /// Create a connected status.
    pub fn connected(channel: impl Into<String>) -> Self {
        Self {
            connected: true,
            channel: channel.into(),
            account_id: None,
            error: None,
            last_activity: None,
        }
    }

    /// Create a disconnected status.
    pub fn disconnected(channel: impl Into<String>) -> Self {
        Self {
            connected: false,
            channel: channel.into(),
            account_id: None,
            error: None,
            last_activity: None,
        }
    }

    /// Create an error status.
    pub fn error(channel: impl Into<String>, err: impl Into<String>) -> Self {
        Self {
            connected: false,
            channel: channel.into(),
            account_id: None,
            error: Some(err.into()),
            last_activity: None,
        }
    }
}
