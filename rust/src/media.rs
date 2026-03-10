//! Rich media normalization — unified attachment model across all platforms.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use tracing::warn;

use crate::middleware::{Handler, HandlerResult, Middleware};
use crate::types::{ContentType, UnifiedMessage};

/// Kind of media attachment.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MediaType {
    Image,
    Video,
    Audio,
    Voice,
    Document,
    Sticker,
    Location,
    Contact,
}

/// Unified attachment model across all platforms.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Attachment {
    #[serde(rename = "type")]
    pub media_type: MediaType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(skip)]
    pub data: Option<Vec<u8>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filename: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mime_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub width: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub height: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thumbnail_url: Option<String>,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

impl Attachment {
    /// Create a new attachment with the given media type.
    pub fn new(media_type: MediaType) -> Self {
        Self {
            media_type,
            url: None,
            data: None,
            filename: None,
            mime_type: None,
            size: None,
            width: None,
            height: None,
            duration: None,
            thumbnail_url: None,
            metadata: HashMap::new(),
        }
    }
}

/// Extension-to-media-type mapping.
static EXT_MAP: &[(&str, MediaType)] = &[
    (".jpg", MediaType::Image),
    (".jpeg", MediaType::Image),
    (".png", MediaType::Image),
    (".gif", MediaType::Image),
    (".webp", MediaType::Image),
    (".bmp", MediaType::Image),
    (".svg", MediaType::Image),
    (".mp4", MediaType::Video),
    (".mov", MediaType::Video),
    (".avi", MediaType::Video),
    (".mkv", MediaType::Video),
    (".webm", MediaType::Video),
    (".mp3", MediaType::Audio),
    (".ogg", MediaType::Audio),
    (".wav", MediaType::Audio),
    (".flac", MediaType::Audio),
    (".m4a", MediaType::Audio),
    (".aac", MediaType::Audio),
    (".pdf", MediaType::Document),
    (".doc", MediaType::Document),
    (".docx", MediaType::Document),
    (".xls", MediaType::Document),
    (".xlsx", MediaType::Document),
    (".zip", MediaType::Document),
    (".tar", MediaType::Document),
    (".gz", MediaType::Document),
];

/// Detect media type from MIME type, filename, or URL.
///
/// Priority: mime_type > filename extension > URL path extension > Document fallback.
pub fn detect_media_type(
    mime_type: Option<&str>,
    filename: Option<&str>,
    url: Option<&str>,
) -> MediaType {
    // 1. Try MIME type prefix
    if let Some(mime) = mime_type {
        let prefix = mime.split('/').next().unwrap_or("");
        match prefix {
            "image" => return MediaType::Image,
            "video" => return MediaType::Video,
            "audio" => return MediaType::Audio,
            _ => {}
        }
        if mime.contains("sticker") || mime == "application/x-tgsticker" {
            return MediaType::Sticker;
        }
    }

    // 2. Try filename extension
    if let Some(fname) = filename {
        if let Some(media_type) = ext_lookup(fname) {
            return media_type;
        }
    }

    // 3. Try URL path extension
    if let Some(u) = url {
        // Extract path from URL (simple parsing)
        let path = if let Some(idx) = u.find("://") {
            let after_scheme = &u[idx + 3..];
            let path_start = after_scheme.find('/').unwrap_or(after_scheme.len());
            let path_end = after_scheme.find('?').unwrap_or(after_scheme.len());
            &after_scheme[path_start..path_end]
        } else {
            u
        };
        if let Some(media_type) = ext_lookup(path) {
            return media_type;
        }
    }

    // 4. Fallback
    MediaType::Document
}

/// Look up extension from a filename/path string.
fn ext_lookup(name: &str) -> Option<MediaType> {
    let lower = name.to_lowercase();
    // Find last dot
    if let Some(dot_pos) = lower.rfind('.') {
        let ext = &lower[dot_pos..];
        for (e, mt) in EXT_MAP {
            if *e == ext {
                return Some(mt.clone());
            }
        }
    }
    None
}

/// Normalize a raw attachment dict (e.g., from platform JSON) into an [`Attachment`].
pub fn normalize_attachment(
    raw: &HashMap<String, serde_json::Value>,
    _channel: &str,
) -> Attachment {
    let raw_type = raw
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_lowercase();

    // Special types
    if raw_type == "location" {
        let mut att = Attachment::new(MediaType::Location);
        if let Some(lat) = raw.get("latitude") {
            att.metadata
                .insert("latitude".into(), lat.clone());
        }
        if let Some(lon) = raw.get("longitude") {
            att.metadata
                .insert("longitude".into(), lon.clone());
        }
        return att;
    }
    if raw_type == "contact" {
        let mut att = Attachment::new(MediaType::Contact);
        for key in &["phone_number", "first_name", "last_name"] {
            if let Some(v) = raw.get(*key) {
                att.metadata.insert(key.to_string(), v.clone());
            }
        }
        return att;
    }
    if raw_type == "voice" {
        let mut att = Attachment::new(MediaType::Voice);
        att.url = get_str(raw, "url").or_else(|| get_str(raw, "file_url"));
        att.mime_type = get_str(raw, "mime_type").or_else(|| get_str(raw, "content_type"));
        att.size = get_u64(raw, "file_size").or_else(|| get_u64(raw, "size"));
        att.duration = get_f64(raw, "duration");
        if let Some(fid) = get_str(raw, "file_id") {
            att.metadata
                .insert("file_id".into(), serde_json::Value::String(fid));
        }
        return att;
    }
    if raw_type == "sticker" {
        let mut att = Attachment::new(MediaType::Sticker);
        att.url = get_str(raw, "url").or_else(|| get_str(raw, "file_url"));
        att.filename = get_str(raw, "file_name").or_else(|| get_str(raw, "filename"));
        att.mime_type = get_str(raw, "mime_type").or_else(|| get_str(raw, "content_type"));
        att.width = get_u32(raw, "width");
        att.height = get_u32(raw, "height");
        if let Some(fid) = get_str(raw, "file_id") {
            att.metadata
                .insert("file_id".into(), serde_json::Value::String(fid));
        }
        return att;
    }

    // Generic normalization
    let url = get_str(raw, "url")
        .or_else(|| get_str(raw, "file_url"))
        .or_else(|| get_str(raw, "proxy_url"));
    let mime = get_str(raw, "mime_type").or_else(|| get_str(raw, "content_type"));
    let fname = get_str(raw, "file_name").or_else(|| get_str(raw, "filename"));
    let media_type = detect_media_type(mime.as_deref(), fname.as_deref(), url.as_deref());

    let mut att = Attachment::new(media_type);
    att.url = url;
    att.filename = fname;
    att.mime_type = mime;
    att.size = get_u64(raw, "file_size").or_else(|| get_u64(raw, "size"));
    att.width = get_u32(raw, "width");
    att.height = get_u32(raw, "height");
    att.duration = get_f64(raw, "duration");
    att.thumbnail_url = get_str(raw, "thumbnail_url").or_else(|| get_str(raw, "thumb_url"));
    if let Some(fid) = get_str(raw, "file_id") {
        att.metadata
            .insert("file_id".into(), serde_json::Value::String(fid));
    }
    att
}

// Helper accessors for HashMap<String, Value>
fn get_str(m: &HashMap<String, serde_json::Value>, key: &str) -> Option<String> {
    m.get(key).and_then(|v| v.as_str()).map(String::from)
}

fn get_u64(m: &HashMap<String, serde_json::Value>, key: &str) -> Option<u64> {
    m.get(key).and_then(|v| v.as_u64())
}

fn get_u32(m: &HashMap<String, serde_json::Value>, key: &str) -> Option<u32> {
    m.get(key).and_then(|v| v.as_u64()).map(|v| v as u32)
}

fn get_f64(m: &HashMap<String, serde_json::Value>, key: &str) -> Option<f64> {
    m.get(key).and_then(|v| v.as_f64())
}

/// Middleware that normalizes incoming media attachments to unified [`Attachment`] format.
pub struct MediaNormalizerMiddleware {
    /// Whether to download remote media.
    pub download_media: bool,
    /// Maximum attachment size in bytes.
    pub max_size: u64,
}

impl MediaNormalizerMiddleware {
    /// Create with default settings (no download, 50MB max).
    pub fn new() -> Self {
        Self {
            download_media: false,
            max_size: 50_000_000,
        }
    }

    /// Builder: enable media downloading.
    pub fn with_download(mut self, download: bool) -> Self {
        self.download_media = download;
        self
    }

    /// Builder: set max attachment size.
    pub fn with_max_size(mut self, max_size: u64) -> Self {
        self.max_size = max_size;
        self
    }
}

impl Default for MediaNormalizerMiddleware {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Middleware for MediaNormalizerMiddleware {
    async fn process(&self, mut msg: UnifiedMessage, next: &Handler) -> HandlerResult {
        let mut attachments: Vec<Attachment> = Vec::new();

        // 1. Extract from msg.content if media type
        if msg.content.content_type == ContentType::Media {
            if let Some(ref media_url) = msg.content.media_url {
                let media_type = detect_media_type(
                    msg.content.media_type.as_deref(),
                    None,
                    Some(media_url.as_str()),
                );
                let mut att = Attachment::new(media_type);
                att.url = Some(media_url.clone());
                att.mime_type = msg.content.media_type.clone();
                attachments.push(att);
            }
        }

        // 2. Extract from msg.raw if it has an "attachments" array
        if let Some(ref raw) = msg.raw {
            if let Some(raw_atts) = raw.get("attachments").and_then(|v| v.as_array()) {
                for raw_att in raw_atts {
                    if let Some(obj) = raw_att.as_object() {
                        let map: HashMap<String, serde_json::Value> =
                            obj.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
                        let att = normalize_attachment(&map, &msg.channel);
                        if let Some(size) = att.size {
                            if size > self.max_size {
                                warn!(
                                    size = size,
                                    max = self.max_size,
                                    "attachment exceeds max_size, skipping"
                                );
                                continue;
                            }
                        }
                        attachments.push(att);
                    }
                }
            }
        }

        if !attachments.is_empty() {
            let json = serde_json::to_value(&attachments).unwrap_or_default();
            msg.metadata.insert("attachments".to_string(), json);
        }

        next(msg).await
    }
}
