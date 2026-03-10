//! Tests for IdentityRouter: register, send, defaults, validation.

use async_trait::async_trait;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;

use unified_channel::adapter::{ChannelAdapter, MessageHandler};
use unified_channel::error::Result;
use unified_channel::identity::IdentityRouter;
use unified_channel::types::*;

/// Mock adapter for identity router tests.
struct MockAdapter {
    id: String,
    send_count: Arc<AtomicUsize>,
    last_text: Arc<Mutex<String>>,
}

impl MockAdapter {
    fn new(id: &str) -> Self {
        Self {
            id: id.to_string(),
            send_count: Arc::new(AtomicUsize::new(0)),
            last_text: Arc::new(Mutex::new(String::new())),
        }
    }
}

#[async_trait]
impl ChannelAdapter for MockAdapter {
    fn channel_id(&self) -> &str {
        &self.id
    }
    async fn connect(&mut self) -> Result<()> {
        Ok(())
    }
    async fn disconnect(&mut self) -> Result<()> {
        Ok(())
    }
    fn on_message(&mut self, _handler: MessageHandler) {}
    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>> {
        self.send_count.fetch_add(1, Ordering::SeqCst);
        let mut last = self.last_text.lock().await;
        *last = msg.text.clone();
        Ok(Some("sent_id".into()))
    }
    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus::connected(&self.id)
    }
}

#[tokio::test]
async fn test_register_and_send() {
    let adapter = MockAdapter::new("telegram");
    let count = Arc::clone(&adapter.send_count);

    let mut router = IdentityRouter::new();
    router
        .register("telegram:personal", Box::new(adapter))
        .unwrap();

    let msg = OutboundMessage::text("chat1", "hello");
    let result = router.send("telegram:personal", msg).await;
    assert!(result.is_ok());
    assert_eq!(count.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn test_register_invalid_id() {
    let mut router = IdentityRouter::new();
    let result = router.register("invalid-format", Box::new(MockAdapter::new("x")));
    assert!(result.is_err());
    let err = result.err().unwrap();
    assert!(err.to_string().contains("invalid identity_id"));
}

#[tokio::test]
async fn test_register_duplicate() {
    let mut router = IdentityRouter::new();
    router
        .register("telegram:one", Box::new(MockAdapter::new("telegram")))
        .unwrap();

    let result = router.register("telegram:one", Box::new(MockAdapter::new("telegram")));
    assert!(result.is_err());
    let err = result.err().unwrap();
    assert!(err.to_string().contains("already registered"));
}

#[tokio::test]
async fn test_send_unknown_identity() {
    let router = IdentityRouter::new();
    let msg = OutboundMessage::text("chat1", "hello");
    let result = router.send("telegram:unknown", msg).await;
    assert!(result.is_err());
}

#[tokio::test]
async fn test_set_default_and_send() {
    let adapter = MockAdapter::new("telegram");
    let count = Arc::clone(&adapter.send_count);

    let mut router = IdentityRouter::new();
    router
        .register("telegram:personal", Box::new(adapter))
        .unwrap();
    router.set_default("telegram", "telegram:personal").unwrap();

    let msg = OutboundMessage::text("chat1", "via default");
    let result = router.send_default("telegram", msg).await;
    assert!(result.is_ok());
    assert_eq!(count.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn test_set_default_unregistered_identity() {
    let mut router = IdentityRouter::new();
    let result = router.set_default("telegram", "telegram:missing");
    assert!(result.is_err());
}

#[tokio::test]
async fn test_set_default_channel_mismatch() {
    let mut router = IdentityRouter::new();
    router
        .register("discord:main", Box::new(MockAdapter::new("discord")))
        .unwrap();

    let result = router.set_default("telegram", "discord:main");
    assert!(result.is_err());
    let err = result.err().unwrap();
    assert!(err.to_string().contains("does not belong"));
}

#[tokio::test]
async fn test_send_default_no_default_set() {
    let mut router = IdentityRouter::new();
    router
        .register("telegram:one", Box::new(MockAdapter::new("telegram")))
        .unwrap();

    let msg = OutboundMessage::text("chat1", "hello");
    let result = router.send_default("telegram", msg).await;
    assert!(result.is_err());
    assert!(result.unwrap_err().to_string().contains("no default"));
}

#[tokio::test]
async fn test_unregister() {
    let mut router = IdentityRouter::new();
    router
        .register("telegram:one", Box::new(MockAdapter::new("telegram")))
        .unwrap();
    assert_eq!(router.len(), 1);

    router.unregister("telegram:one").unwrap();
    assert_eq!(router.len(), 0);
    assert!(router.is_empty());
}

#[tokio::test]
async fn test_unregister_cleans_default() {
    let mut router = IdentityRouter::new();
    router
        .register("telegram:one", Box::new(MockAdapter::new("telegram")))
        .unwrap();
    router.set_default("telegram", "telegram:one").unwrap();
    router.unregister("telegram:one").unwrap();

    let msg = OutboundMessage::text("chat1", "hello");
    let result = router.send_default("telegram", msg).await;
    assert!(result.is_err()); // default was cleaned up
}

#[tokio::test]
async fn test_get_identities() {
    let mut router = IdentityRouter::new();
    router
        .register("telegram:a", Box::new(MockAdapter::new("telegram")))
        .unwrap();
    router
        .register("telegram:b", Box::new(MockAdapter::new("telegram")))
        .unwrap();
    router
        .register("discord:c", Box::new(MockAdapter::new("discord")))
        .unwrap();

    let all = router.get_identities(None);
    assert_eq!(all.len(), 3);

    let tg = router.get_identities(Some("telegram"));
    assert_eq!(tg.len(), 2);

    let dc = router.get_identities(Some("discord"));
    assert_eq!(dc.len(), 1);

    let slack = router.get_identities(Some("slack"));
    assert_eq!(slack.len(), 0);
}

#[tokio::test]
async fn test_get_status_all() {
    let mut router = IdentityRouter::new();
    router
        .register("telegram:a", Box::new(MockAdapter::new("telegram")))
        .unwrap();
    router
        .register("discord:b", Box::new(MockAdapter::new("discord")))
        .unwrap();

    let statuses = router.get_status_all().await;
    assert_eq!(statuses.len(), 2);
    // Both should show connected (our mock returns connected)
    assert!(statuses["telegram:a"].connected);
    assert!(statuses["discord:b"].connected);
}
