//! Tests for ChannelManager: adapter registration, routing, status.

use async_trait::async_trait;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;

use unified_channel::adapter::{ChannelAdapter, MessageHandler};
use unified_channel::error::Result;
use unified_channel::manager::ChannelManager;
use unified_channel::middleware::*;
use unified_channel::types::*;

/// Mock adapter for testing.
struct MockAdapter {
    id: String,
    connected: Arc<AtomicBool>,
    send_count: Arc<AtomicUsize>,
    last_sent: Arc<Mutex<Option<OutboundMessage>>>,
    handler: Option<MessageHandler>,
}

impl MockAdapter {
    fn new(id: &str) -> Self {
        Self {
            id: id.to_string(),
            connected: Arc::new(AtomicBool::new(false)),
            send_count: Arc::new(AtomicUsize::new(0)),
            last_sent: Arc::new(Mutex::new(None)),
            handler: None,
        }
    }
}

#[async_trait]
impl ChannelAdapter for MockAdapter {
    fn channel_id(&self) -> &str {
        &self.id
    }

    async fn connect(&mut self) -> Result<()> {
        self.connected.store(true, Ordering::SeqCst);
        Ok(())
    }

    async fn disconnect(&mut self) -> Result<()> {
        self.connected.store(false, Ordering::SeqCst);
        Ok(())
    }

    fn on_message(&mut self, handler: MessageHandler) {
        self.handler = Some(handler);
    }

    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>> {
        self.send_count.fetch_add(1, Ordering::SeqCst);
        let mut last = self.last_sent.lock().await;
        *last = Some(msg);
        Ok(Some("mock_msg_id".to_string()))
    }

    async fn get_status(&self) -> ChannelStatus {
        ChannelStatus {
            connected: self.connected.load(Ordering::SeqCst),
            channel: self.id.clone(),
            account_id: Some("mock_bot".into()),
            error: None,
            last_activity: None,
        }
    }
}

#[tokio::test]
async fn test_manager_add_channel() {
    let mut mgr = ChannelManager::new();
    mgr.add_channel(Box::new(MockAdapter::new("test1")));
    mgr.add_channel(Box::new(MockAdapter::new("test2")));
    assert_eq!(mgr.channel_count(), 2);
    let mut ids = mgr.channel_ids();
    ids.sort();
    assert_eq!(ids, vec!["test1", "test2"]);
}

#[tokio::test]
async fn test_manager_send() {
    let adapter = MockAdapter::new("test");
    let send_count = Arc::clone(&adapter.send_count);
    let last_sent = Arc::clone(&adapter.last_sent);

    let mut mgr = ChannelManager::new();
    mgr.add_channel(Box::new(adapter));

    let result = mgr.send("test", "chat1", "hello", None, None).await;
    assert!(result.is_ok());
    assert_eq!(result.unwrap(), Some("mock_msg_id".to_string()));
    assert_eq!(send_count.load(Ordering::SeqCst), 1);

    let sent = last_sent.lock().await;
    assert_eq!(sent.as_ref().unwrap().chat_id, "chat1");
    assert_eq!(sent.as_ref().unwrap().text, "hello");
}

#[tokio::test]
async fn test_manager_send_unknown_channel() {
    let mgr = ChannelManager::new();
    let result = mgr.send("nonexistent", "chat1", "hi", None, None).await;
    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(err.to_string().contains("not registered"));
}

#[tokio::test]
async fn test_manager_get_status() {
    let mut mgr = ChannelManager::new();
    mgr.add_channel(Box::new(MockAdapter::new("ch1")));
    mgr.add_channel(Box::new(MockAdapter::new("ch2")));

    let statuses = mgr.get_status().await;
    assert_eq!(statuses.len(), 2);
    assert!(!statuses["ch1"].connected); // not connected yet
    assert!(!statuses["ch2"].connected);
}

#[tokio::test]
async fn test_manager_pipeline_with_middleware() {
    let mut mgr = ChannelManager::new();
    mgr.add_channel(Box::new(MockAdapter::new("test")));

    let commands = CommandMiddleware::new()
        .command("ping", |_| async { HandlerResult::Text("pong".into()) });
    mgr.add_middleware(commands);

    // Test command message
    let msg = UnifiedMessage::new(
        "test",
        Identity::new("u1"),
        MessageContent::command("/ping", "ping", vec![]),
    );
    let result = mgr.run_pipeline(msg).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "pong"));
}

#[tokio::test]
async fn test_manager_pipeline_fallback() {
    let mut mgr = ChannelManager::new();
    mgr.add_channel(Box::new(MockAdapter::new("test")));

    mgr.on_message(|msg: UnifiedMessage| async move {
        HandlerResult::Text(format!("echo: {}", msg.content.text))
    });

    let msg = UnifiedMessage::new(
        "test",
        Identity::new("u1"),
        MessageContent::text("hello"),
    );
    let result = mgr.run_pipeline(msg).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "echo: hello"));
}

#[tokio::test]
async fn test_manager_pipeline_no_handler() {
    let mgr = ChannelManager::new();
    let msg = UnifiedMessage::new(
        "test",
        Identity::new("u1"),
        MessageContent::text("hello"),
    );
    let result = mgr.run_pipeline(msg).await;
    assert!(result.is_none());
}

#[tokio::test]
async fn test_manager_broadcast() {
    let a1 = MockAdapter::new("ch1");
    let a2 = MockAdapter::new("ch2");
    let c1 = Arc::clone(&a1.send_count);
    let c2 = Arc::clone(&a2.send_count);

    let mut mgr = ChannelManager::new();
    mgr.add_channel(Box::new(a1));
    mgr.add_channel(Box::new(a2));

    let mut chat_ids = std::collections::HashMap::new();
    chat_ids.insert("ch1".to_string(), "room1".to_string());
    chat_ids.insert("ch2".to_string(), "room2".to_string());

    mgr.broadcast("hello all", &chat_ids).await;
    assert_eq!(c1.load(Ordering::SeqCst), 1);
    assert_eq!(c2.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn test_manager_default() {
    let mgr = ChannelManager::default();
    assert_eq!(mgr.channel_count(), 0);
}
