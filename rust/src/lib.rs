//! **unified-channel** — a single API for every messaging platform.
//!
//! This crate provides a unified abstraction over Telegram, Discord, Slack,
//! Email, Matrix, Twilio, Home Assistant, IRC, Nostr, webhooks, and Mattermost.
//!
//! # Quick start
//!
//! ```ignore
//! use unified_channel::{ChannelManager, middleware};
//!
//! let mut manager = ChannelManager::new();
//! // manager.add_channel(Box::new(telegram_adapter));
//! // manager.add_middleware(Box::new(access_mw));
//! // manager.run().await?;
//! ```
//!
//! Enable adapters via Cargo feature flags:
//! `telegram`, `discord`, `slack`, `email`, `matrix`, `twilio`,
//! `homeassistant`, `irc-adapter`, `nostr`, `webhook`, `mattermost`.

pub mod adapter;
pub mod adapters;
pub mod error;
pub mod identity;
pub mod manager;
pub mod media;
pub mod middleware;
pub mod queue;
pub mod relay;
pub mod types;

// Re-export key items at crate root for ergonomic use.
pub use adapter::ChannelAdapter;
pub use error::{Error, Result};
pub use identity::IdentityRouter;
pub use manager::ChannelManager;
pub use media::{Attachment, MediaType};
pub use middleware::{AccessMiddleware, CommandMiddleware, HandlerResult, Middleware};
pub use queue::{InMemoryQueue, MessageQueue, QueueMiddleware};
pub use relay::RelayMiddleware;
pub use types::*;
