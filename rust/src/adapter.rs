//! Channel adapter trait — each platform implements this interface.

use async_trait::async_trait;

use crate::error::Result;
use crate::types::{ChannelStatus, OutboundMessage, UnifiedMessage};

/// Callback type for inbound messages.
pub type MessageHandler = Box<dyn Fn(UnifiedMessage) + Send + Sync>;

/// The core trait every channel adapter must implement.
///
/// Adapters handle platform-specific connection, message sending, and
/// receiving, translating between platform-native formats and
/// [`UnifiedMessage`] / [`OutboundMessage`].
#[async_trait]
pub trait ChannelAdapter: Send + Sync {
    /// Unique identifier for this channel (e.g., "telegram", "discord").
    fn channel_id(&self) -> &str;

    /// Establish connection to the platform.
    async fn connect(&mut self) -> Result<()>;

    /// Gracefully disconnect.
    async fn disconnect(&mut self) -> Result<()>;

    /// Register a handler that will be called for each inbound message.
    fn on_message(&mut self, handler: MessageHandler);

    /// Send an outbound message. Returns the platform message ID on success.
    async fn send(&self, msg: OutboundMessage) -> Result<Option<String>>;

    /// Current connection status.
    async fn get_status(&self) -> ChannelStatus;
}
