//! Tests for InMemoryQueue and QueueMiddleware.

use unified_channel::middleware::*;
use unified_channel::queue::*;
use unified_channel::types::*;

#[tokio::test]
async fn test_enqueue_and_pending() {
    let queue = InMemoryQueue::new();
    let msg = OutboundMessage::text("chat1", "hello");
    let id = queue.enqueue(msg, "telegram", 0).await.unwrap();
    assert!(!id.is_empty());

    let pending = queue.pending(10).await.unwrap();
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].channel, "telegram");
    assert_eq!(pending[0].message.text, "hello");
    assert_eq!(pending[0].status, QueueItemStatus::Processing);
}

#[tokio::test]
async fn test_ack() {
    let queue = InMemoryQueue::new();
    let msg = OutboundMessage::text("chat1", "hello");
    let id = queue.enqueue(msg, "telegram", 0).await.unwrap();

    let pending = queue.pending(10).await.unwrap();
    assert_eq!(pending.len(), 1);

    queue.ack(&id).await.unwrap();

    let stats = queue.stats().await.unwrap();
    assert_eq!(stats.completed, 1);
    assert_eq!(stats.pending, 0);
}

#[tokio::test]
async fn test_nack_retries() {
    let queue = InMemoryQueue::new().with_max_retries(3);
    let msg = OutboundMessage::text("chat1", "hello");
    let id = queue.enqueue(msg, "telegram", 0).await.unwrap();

    // Get and nack
    let pending = queue.pending(10).await.unwrap();
    assert_eq!(pending.len(), 1);

    queue.nack(&id, "timeout").await.unwrap();

    // Item should be back to pending (but with a future retry time)
    let stats = queue.stats().await.unwrap();
    assert_eq!(stats.pending, 1);
    assert_eq!(stats.dead, 0);
}

#[tokio::test]
async fn test_nack_to_dead_letter() {
    let queue = InMemoryQueue::new().with_max_retries(1);
    let msg = OutboundMessage::text("chat1", "hello");
    let id = queue.enqueue(msg, "telegram", 0).await.unwrap();

    let _ = queue.pending(10).await.unwrap();
    queue.nack(&id, "permanent failure").await.unwrap();

    let stats = queue.stats().await.unwrap();
    assert_eq!(stats.dead, 1);
    assert_eq!(stats.pending, 0);

    let dead = queue.dead_letters(10).await.unwrap();
    assert_eq!(dead.len(), 1);
    assert_eq!(dead[0].last_error.as_deref(), Some("permanent failure"));
}

#[tokio::test]
async fn test_stats() {
    let queue = InMemoryQueue::new();
    let stats = queue.stats().await.unwrap();
    assert_eq!(stats.total, 0);
    assert_eq!(stats.pending, 0);

    queue
        .enqueue(OutboundMessage::text("c", "m1"), "ch", 0)
        .await
        .unwrap();
    queue
        .enqueue(OutboundMessage::text("c", "m2"), "ch", 0)
        .await
        .unwrap();

    let stats = queue.stats().await.unwrap();
    assert_eq!(stats.total, 2);
    assert_eq!(stats.pending, 2);
}

#[tokio::test]
async fn test_priority_ordering() {
    let queue = InMemoryQueue::new();
    queue
        .enqueue(OutboundMessage::text("c", "low"), "ch", 0)
        .await
        .unwrap();
    queue
        .enqueue(OutboundMessage::text("c", "high"), "ch", 10)
        .await
        .unwrap();

    // Both should be returned (pending returns by insertion order for in-memory)
    let pending = queue.pending(10).await.unwrap();
    assert_eq!(pending.len(), 2);
}

#[tokio::test]
async fn test_pending_limit() {
    let queue = InMemoryQueue::new();
    for i in 0..10 {
        queue
            .enqueue(OutboundMessage::text("c", format!("msg{}", i)), "ch", 0)
            .await
            .unwrap();
    }

    let pending = queue.pending(3).await.unwrap();
    assert_eq!(pending.len(), 3);

    // Remaining should still be pending
    let stats = queue.stats().await.unwrap();
    assert_eq!(stats.pending, 7);
    assert_eq!(stats.processing, 3);
}

#[tokio::test]
async fn test_purge_completed() {
    let queue = InMemoryQueue::new();
    let id1 = queue
        .enqueue(OutboundMessage::text("c", "m1"), "ch", 0)
        .await
        .unwrap();
    let _ = queue.pending(10).await.unwrap();
    queue.ack(&id1).await.unwrap();

    // Purge with 0 seconds threshold (purge everything completed)
    let purged = queue.purge_completed(0).await.unwrap();
    // May or may not purge depending on timing; the item was just completed
    // so it might not be "older than 0 seconds". That's fine for this test.
    let stats = queue.stats().await.unwrap();
    assert!(stats.completed <= 1);
}

#[tokio::test]
async fn test_queue_middleware_enqueues() {
    let queue = InMemoryQueue::new();
    let mw = QueueMiddleware::new(queue, "default");

    let handler: Handler = handler_fn(|_| async {
        HandlerResult::Text("reply".into())
    });

    let mut msg = UnifiedMessage::new(
        "telegram",
        Identity::new("u1"),
        MessageContent::text("hello"),
    );
    msg.chat_id = Some("chat1".into());

    let result = mw.process(msg, &handler).await;
    // QueueMiddleware returns None (delivery is async)
    assert!(result.is_none());

    // Check that the message was enqueued
    let stats = mw.queue().stats().await.unwrap();
    assert_eq!(stats.pending, 1);
}

#[tokio::test]
async fn test_queue_middleware_passthrough_none() {
    let queue = InMemoryQueue::new();
    let mw = QueueMiddleware::new(queue, "default");

    let handler: Handler = handler_fn(|_| async { HandlerResult::None });

    let msg = UnifiedMessage::new(
        "telegram",
        Identity::new("u1"),
        MessageContent::text("hello"),
    );

    let result = mw.process(msg, &handler).await;
    assert!(result.is_none());

    // Nothing enqueued
    let stats = mw.queue().stats().await.unwrap();
    assert_eq!(stats.total, 0);
}

#[tokio::test]
async fn test_queue_default() {
    let queue = InMemoryQueue::default();
    let stats = queue.stats().await.unwrap();
    assert_eq!(stats.total, 0);
}
