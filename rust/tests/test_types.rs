//! Tests for core types: UnifiedMessage, MessageContent, Identity, etc.

use unified_channel::types::*;

#[test]
fn test_identity_new() {
    let id = Identity::new("user123");
    assert_eq!(id.id, "user123");
    assert_eq!(id.username, None);
    assert_eq!(id.display_name, None);
}

#[test]
fn test_identity_builder() {
    let id = Identity::new("u1")
        .with_username("alice")
        .with_display_name("Alice Smith");
    assert_eq!(id.username.as_deref(), Some("alice"));
    assert_eq!(id.display_name.as_deref(), Some("Alice Smith"));
}

#[test]
fn test_identity_display_priority() {
    // display_name > username > id
    let id1 = Identity::new("u1")
        .with_username("alice")
        .with_display_name("Alice Smith");
    assert_eq!(id1.display(), "Alice Smith");

    let id2 = Identity::new("u2").with_username("bob");
    assert_eq!(id2.display(), "bob");

    let id3 = Identity::new("u3");
    assert_eq!(id3.display(), "u3");
}

#[test]
fn test_message_content_text() {
    let c = MessageContent::text("hello");
    assert_eq!(c.content_type, ContentType::Text);
    assert_eq!(c.text, "hello");
    assert!(c.command.is_none());
}

#[test]
fn test_message_content_command() {
    let c = MessageContent::command("/ping arg1", "ping", vec!["arg1".into()]);
    assert_eq!(c.content_type, ContentType::Command);
    assert_eq!(c.command.as_deref(), Some("ping"));
    assert_eq!(c.args.as_ref().unwrap(), &["arg1".to_string()]);
}

#[test]
fn test_message_content_media() {
    let c = MessageContent::media("photo", "https://example.com/img.png", Some("image/png".into()));
    assert_eq!(c.content_type, ContentType::Media);
    assert_eq!(c.media_url.as_deref(), Some("https://example.com/img.png"));
    assert_eq!(c.media_type.as_deref(), Some("image/png"));
}

#[test]
fn test_message_content_callback() {
    let c = MessageContent::callback("btn_ok");
    assert_eq!(c.content_type, ContentType::Callback);
    assert_eq!(c.callback_data.as_deref(), Some("btn_ok"));
    assert_eq!(c.text, "btn_ok");
}

#[test]
fn test_unified_message_new() {
    let msg = UnifiedMessage::new(
        "telegram",
        Identity::new("u1"),
        MessageContent::text("hi"),
    );
    assert_eq!(msg.channel, "telegram");
    assert_eq!(msg.sender.id, "u1");
    assert_eq!(msg.content.text, "hi");
    assert!(!msg.id.is_empty());
    assert!(msg.chat_id.is_none());
}

#[test]
fn test_outbound_message_text() {
    let out = OutboundMessage::text("chat123", "hello");
    assert_eq!(out.chat_id, "chat123");
    assert_eq!(out.text, "hello");
    assert!(out.reply_to_id.is_none());
    assert!(out.buttons.is_none());
}

#[test]
fn test_button_callback() {
    let b = Button::callback("OK", "ok_data");
    assert_eq!(b.label, "OK");
    assert_eq!(b.callback_data.as_deref(), Some("ok_data"));
    assert!(b.url.is_none());
}

#[test]
fn test_button_link() {
    let b = Button::link("Visit", "https://example.com");
    assert_eq!(b.label, "Visit");
    assert!(b.callback_data.is_none());
    assert_eq!(b.url.as_deref(), Some("https://example.com"));
}

#[test]
fn test_channel_status_connected() {
    let s = ChannelStatus::connected("telegram");
    assert!(s.connected);
    assert_eq!(s.channel, "telegram");
    assert!(s.error.is_none());
}

#[test]
fn test_channel_status_error() {
    let s = ChannelStatus::error("discord", "timeout");
    assert!(!s.connected);
    assert_eq!(s.error.as_deref(), Some("timeout"));
}

#[test]
fn test_types_serialize_roundtrip() {
    let msg = OutboundMessage {
        chat_id: "123".into(),
        text: "hello".into(),
        buttons: Some(vec![vec![Button::callback("Yes", "yes"), Button::link("Link", "https://x.com")]]),
        ..Default::default()
    };
    let json = serde_json::to_string(&msg).unwrap();
    let deserialized: OutboundMessage = serde_json::from_str(&json).unwrap();
    assert_eq!(deserialized.chat_id, "123");
    assert_eq!(deserialized.text, "hello");
    let btns = deserialized.buttons.unwrap();
    assert_eq!(btns[0].len(), 2);
    assert_eq!(btns[0][0].label, "Yes");
    assert_eq!(btns[0][1].url.as_deref(), Some("https://x.com"));
}
