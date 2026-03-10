//! Tests for media types, detection, normalization, and middleware.

use std::collections::HashMap;
use unified_channel::media::*;
use unified_channel::middleware::*;
use unified_channel::types::*;

#[test]
fn test_detect_from_mime_image() {
    assert_eq!(detect_media_type(Some("image/png"), None, None), MediaType::Image);
    assert_eq!(detect_media_type(Some("image/jpeg"), None, None), MediaType::Image);
}

#[test]
fn test_detect_from_mime_video() {
    assert_eq!(detect_media_type(Some("video/mp4"), None, None), MediaType::Video);
}

#[test]
fn test_detect_from_mime_audio() {
    assert_eq!(detect_media_type(Some("audio/mpeg"), None, None), MediaType::Audio);
}

#[test]
fn test_detect_from_mime_sticker() {
    assert_eq!(
        detect_media_type(Some("application/x-tgsticker"), None, None),
        MediaType::Sticker
    );
}

#[test]
fn test_detect_from_filename() {
    assert_eq!(detect_media_type(None, Some("photo.jpg"), None), MediaType::Image);
    assert_eq!(detect_media_type(None, Some("video.mp4"), None), MediaType::Video);
    assert_eq!(detect_media_type(None, Some("song.mp3"), None), MediaType::Audio);
    assert_eq!(detect_media_type(None, Some("doc.pdf"), None), MediaType::Document);
}

#[test]
fn test_detect_from_url() {
    assert_eq!(
        detect_media_type(None, None, Some("https://example.com/img.png")),
        MediaType::Image
    );
    assert_eq!(
        detect_media_type(None, None, Some("https://cdn.example.com/video.webm?token=abc")),
        MediaType::Video
    );
}

#[test]
fn test_detect_fallback_document() {
    assert_eq!(detect_media_type(None, None, None), MediaType::Document);
    assert_eq!(
        detect_media_type(None, None, Some("https://example.com/file")),
        MediaType::Document
    );
}

#[test]
fn test_detect_mime_priority_over_filename() {
    // MIME says video, filename says image — MIME wins
    assert_eq!(
        detect_media_type(Some("video/mp4"), Some("photo.jpg"), None),
        MediaType::Video
    );
}

#[test]
fn test_attachment_new() {
    let att = Attachment::new(MediaType::Image);
    assert_eq!(att.media_type, MediaType::Image);
    assert!(att.url.is_none());
    assert!(att.data.is_none());
    assert!(att.filename.is_none());
}

#[test]
fn test_normalize_attachment_generic() {
    let mut raw = HashMap::new();
    raw.insert("url".to_string(), serde_json::json!("https://example.com/photo.jpg"));
    raw.insert("mime_type".to_string(), serde_json::json!("image/jpeg"));
    raw.insert("file_size".to_string(), serde_json::json!(12345));
    raw.insert("width".to_string(), serde_json::json!(800));
    raw.insert("height".to_string(), serde_json::json!(600));

    let att = normalize_attachment(&raw, "telegram");
    assert_eq!(att.media_type, MediaType::Image);
    assert_eq!(att.url.as_deref(), Some("https://example.com/photo.jpg"));
    assert_eq!(att.mime_type.as_deref(), Some("image/jpeg"));
    assert_eq!(att.size, Some(12345));
    assert_eq!(att.width, Some(800));
    assert_eq!(att.height, Some(600));
}

#[test]
fn test_normalize_attachment_location() {
    let mut raw = HashMap::new();
    raw.insert("type".to_string(), serde_json::json!("location"));
    raw.insert("latitude".to_string(), serde_json::json!(37.7749));
    raw.insert("longitude".to_string(), serde_json::json!(-122.4194));

    let att = normalize_attachment(&raw, "telegram");
    assert_eq!(att.media_type, MediaType::Location);
    assert!(att.metadata.contains_key("latitude"));
    assert!(att.metadata.contains_key("longitude"));
}

#[test]
fn test_normalize_attachment_contact() {
    let mut raw = HashMap::new();
    raw.insert("type".to_string(), serde_json::json!("contact"));
    raw.insert("phone_number".to_string(), serde_json::json!("+15551234"));
    raw.insert("first_name".to_string(), serde_json::json!("Alice"));

    let att = normalize_attachment(&raw, "telegram");
    assert_eq!(att.media_type, MediaType::Contact);
    assert!(att.metadata.contains_key("phone_number"));
    assert!(att.metadata.contains_key("first_name"));
}

#[test]
fn test_normalize_attachment_voice() {
    let mut raw = HashMap::new();
    raw.insert("type".to_string(), serde_json::json!("voice"));
    raw.insert("url".to_string(), serde_json::json!("https://example.com/voice.ogg"));
    raw.insert("duration".to_string(), serde_json::json!(5.5));
    raw.insert("file_id".to_string(), serde_json::json!("file123"));

    let att = normalize_attachment(&raw, "telegram");
    assert_eq!(att.media_type, MediaType::Voice);
    assert_eq!(att.duration, Some(5.5));
    assert_eq!(
        att.metadata.get("file_id"),
        Some(&serde_json::json!("file123"))
    );
}

#[test]
fn test_normalize_attachment_sticker() {
    let mut raw = HashMap::new();
    raw.insert("type".to_string(), serde_json::json!("sticker"));
    raw.insert("url".to_string(), serde_json::json!("https://example.com/sticker.webp"));
    raw.insert("width".to_string(), serde_json::json!(512));
    raw.insert("height".to_string(), serde_json::json!(512));

    let att = normalize_attachment(&raw, "telegram");
    assert_eq!(att.media_type, MediaType::Sticker);
    assert_eq!(att.width, Some(512));
}

#[tokio::test]
async fn test_media_normalizer_middleware_adds_attachments() {
    let mw = MediaNormalizerMiddleware::new();
    let handler: Handler = handler_fn(|msg: UnifiedMessage| async move {
        // Check that attachments were added to metadata
        if msg.metadata.contains_key("attachments") {
            HandlerResult::Text("has_attachments".into())
        } else {
            HandlerResult::Text("no_attachments".into())
        }
    });

    let mut msg = UnifiedMessage::new(
        "telegram",
        Identity::new("u1"),
        MessageContent::media("photo", "https://example.com/img.png", Some("image/png".into())),
    );

    let result = mw.process(msg, &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "has_attachments"));
}

#[tokio::test]
async fn test_media_normalizer_no_media_passthrough() {
    let mw = MediaNormalizerMiddleware::new();
    let handler: Handler = handler_fn(|msg: UnifiedMessage| async move {
        if msg.metadata.contains_key("attachments") {
            HandlerResult::Text("has_attachments".into())
        } else {
            HandlerResult::Text("no_attachments".into())
        }
    });

    let msg = UnifiedMessage::new(
        "telegram",
        Identity::new("u1"),
        MessageContent::text("just text"),
    );

    let result = mw.process(msg, &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "no_attachments"));
}

#[test]
fn test_attachment_serialize_roundtrip() {
    let mut att = Attachment::new(MediaType::Image);
    att.url = Some("https://example.com/img.png".into());
    att.mime_type = Some("image/png".into());
    att.size = Some(5000);

    let json = serde_json::to_string(&att).unwrap();
    let deserialized: Attachment = serde_json::from_str(&json).unwrap();
    assert_eq!(deserialized.media_type, MediaType::Image);
    assert_eq!(deserialized.url.as_deref(), Some("https://example.com/img.png"));
    assert_eq!(deserialized.size, Some(5000));
}
