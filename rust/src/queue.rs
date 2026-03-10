//! In-memory message queue with retry semantics.
//!
//! For production use, swap [`InMemoryQueue`] for a persistent backend
//! (SQLite, Redis, etc.) implementing the same [`MessageQueue`] trait.

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use tokio::sync::Mutex;
use tracing::debug;
use uuid::Uuid;

use crate::error::Result;
use crate::middleware::{Handler, HandlerResult, Middleware};
use crate::types::{OutboundMessage, UnifiedMessage};

/// Status of a queue item.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QueueItemStatus {
    Pending,
    Processing,
    Completed,
    Dead,
}

/// A single item in the queue.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueueItem {
    pub id: String,
    pub message: OutboundMessage,
    pub channel: String,
    pub priority: i32,
    pub status: QueueItemStatus,
    pub retries: u32,
    pub max_retries: u32,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub next_retry_at: Option<DateTime<Utc>>,
    pub last_error: Option<String>,
}

/// Queue statistics.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct QueueStats {
    pub pending: usize,
    pub processing: usize,
    pub completed: usize,
    pub dead: usize,
    pub total: usize,
}

/// Trait for message queue backends.
#[async_trait]
pub trait MessageQueue: Send + Sync {
    /// Enqueue a message. Returns the item ID.
    async fn enqueue(
        &self,
        msg: OutboundMessage,
        channel: &str,
        priority: i32,
    ) -> Result<String>;

    /// Get pending items ready for delivery, transitioning them to `Processing`.
    async fn pending(&self, limit: usize) -> Result<Vec<QueueItem>>;

    /// Acknowledge successful delivery.
    async fn ack(&self, item_id: &str) -> Result<()>;

    /// Mark delivery as failed, schedule retry or move to dead letter.
    async fn nack(&self, item_id: &str, error: &str) -> Result<()>;

    /// Get dead-letter items.
    async fn dead_letters(&self, limit: usize) -> Result<Vec<QueueItem>>;

    /// Queue statistics.
    async fn stats(&self) -> Result<QueueStats>;

    /// Purge completed items older than the given duration.
    async fn purge_completed(&self, older_than_secs: u64) -> Result<usize>;
}

/// In-memory queue implementation (for development / testing).
pub struct InMemoryQueue {
    items: Mutex<VecDeque<QueueItem>>,
    max_retries: u32,
    retry_delay_secs: f64,
}

impl InMemoryQueue {
    /// Create a new in-memory queue.
    pub fn new() -> Self {
        Self {
            items: Mutex::new(VecDeque::new()),
            max_retries: 5,
            retry_delay_secs: 60.0,
        }
    }

    /// Builder: set max retries.
    pub fn with_max_retries(mut self, max_retries: u32) -> Self {
        self.max_retries = max_retries;
        self
    }

    /// Builder: set base retry delay in seconds.
    pub fn with_retry_delay(mut self, secs: f64) -> Self {
        self.retry_delay_secs = secs;
        self
    }
}

impl Default for InMemoryQueue {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl MessageQueue for InMemoryQueue {
    async fn enqueue(
        &self,
        msg: OutboundMessage,
        channel: &str,
        priority: i32,
    ) -> Result<String> {
        let now = Utc::now();
        let item = QueueItem {
            id: Uuid::new_v4().to_string(),
            message: msg,
            channel: channel.to_string(),
            priority,
            status: QueueItemStatus::Pending,
            retries: 0,
            max_retries: self.max_retries,
            created_at: now,
            updated_at: now,
            next_retry_at: Some(now),
            last_error: None,
        };
        let id = item.id.clone();
        let mut items = self.items.lock().await;
        items.push_back(item);
        Ok(id)
    }

    async fn pending(&self, limit: usize) -> Result<Vec<QueueItem>> {
        let now = Utc::now();
        let mut items = self.items.lock().await;
        let mut result = Vec::new();

        for item in items.iter_mut() {
            if result.len() >= limit {
                break;
            }
            if item.status == QueueItemStatus::Pending {
                let ready = item
                    .next_retry_at
                    .map_or(true, |next| next <= now);
                if ready {
                    item.status = QueueItemStatus::Processing;
                    item.updated_at = now;
                    result.push(item.clone());
                }
            }
        }

        Ok(result)
    }

    async fn ack(&self, item_id: &str) -> Result<()> {
        let mut items = self.items.lock().await;
        if let Some(item) = items.iter_mut().find(|i| i.id == item_id) {
            item.status = QueueItemStatus::Completed;
            item.updated_at = Utc::now();
        }
        Ok(())
    }

    async fn nack(&self, item_id: &str, error: &str) -> Result<()> {
        let now = Utc::now();
        let mut items = self.items.lock().await;
        if let Some(item) = items.iter_mut().find(|i| i.id == item_id) {
            item.retries += 1;
            item.last_error = Some(error.to_string());
            item.updated_at = now;

            if item.retries >= item.max_retries {
                item.status = QueueItemStatus::Dead;
            } else {
                item.status = QueueItemStatus::Pending;
                let delay_secs = self.retry_delay_secs * (2.0_f64).powi(item.retries as i32);
                item.next_retry_at =
                    Some(now + chrono::Duration::milliseconds((delay_secs * 1000.0) as i64));
            }
        }
        Ok(())
    }

    async fn dead_letters(&self, limit: usize) -> Result<Vec<QueueItem>> {
        let items = self.items.lock().await;
        let result: Vec<_> = items
            .iter()
            .filter(|i| i.status == QueueItemStatus::Dead)
            .take(limit)
            .cloned()
            .collect();
        Ok(result)
    }

    async fn stats(&self) -> Result<QueueStats> {
        let items = self.items.lock().await;
        let mut stats = QueueStats::default();
        for item in items.iter() {
            match item.status {
                QueueItemStatus::Pending => stats.pending += 1,
                QueueItemStatus::Processing => stats.processing += 1,
                QueueItemStatus::Completed => stats.completed += 1,
                QueueItemStatus::Dead => stats.dead += 1,
            }
        }
        stats.total = items.len();
        Ok(stats)
    }

    async fn purge_completed(&self, older_than_secs: u64) -> Result<usize> {
        let cutoff =
            Utc::now() - chrono::Duration::seconds(older_than_secs as i64);
        let mut items = self.items.lock().await;
        let before = items.len();
        items.retain(|i| {
            !(i.status == QueueItemStatus::Completed && i.updated_at < cutoff)
        });
        Ok(before - items.len())
    }
}

/// Middleware that queues outbound messages for reliable delivery.
pub struct QueueMiddleware<Q: MessageQueue> {
    queue: Q,
    default_channel: String,
}

impl<Q: MessageQueue> QueueMiddleware<Q> {
    /// Create a queue middleware with the given backend.
    pub fn new(queue: Q, default_channel: impl Into<String>) -> Self {
        Self {
            queue,
            default_channel: default_channel.into(),
        }
    }

    /// Get a reference to the underlying queue.
    pub fn queue(&self) -> &Q {
        &self.queue
    }
}

#[async_trait]
impl<Q: MessageQueue + 'static> Middleware for QueueMiddleware<Q> {
    async fn process(&self, msg: UnifiedMessage, next: &Handler) -> HandlerResult {
        let result = next(msg.clone()).await;
        if result.is_none() {
            return result;
        }

        let outbound = match &result {
            HandlerResult::Text(text) => {
                let chat_id = match msg.chat_id {
                    Some(ref id) => id.clone(),
                    None => return result,
                };
                OutboundMessage::text(chat_id, text.clone())
            }
            HandlerResult::Outbound(out) => out.clone(),
            HandlerResult::None => return HandlerResult::None,
        };

        let channel = if msg.channel.is_empty() {
            &self.default_channel
        } else {
            &msg.channel
        };

        match self.queue.enqueue(outbound, channel, 0).await {
            Ok(id) => debug!(item_id = %id, channel = %channel, "queued message"),
            Err(e) => tracing::error!(error = %e, "failed to enqueue message"),
        }

        // Return None since delivery happens asynchronously
        HandlerResult::None
    }
}
