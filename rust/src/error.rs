//! Error types for the unified-channel crate.

use thiserror::Error;

/// Top-level error type for unified-channel operations.
#[derive(Error, Debug)]
pub enum Error {
    /// Channel not found by ID.
    #[error("channel not registered: {0}")]
    ChannelNotFound(String),

    /// Identity not found by ID.
    #[error("identity not registered: {0}")]
    IdentityNotFound(String),

    /// Identity already registered.
    #[error("identity already registered: {0}")]
    IdentityAlreadyRegistered(String),

    /// Invalid identity ID format.
    #[error("invalid identity_id {id:?}: must match 'channel:label' (alphanumeric/underscore only)")]
    InvalidIdentityId { id: String },

    /// Identity does not belong to the expected channel.
    #[error("identity {identity_id:?} does not belong to channel {channel:?}")]
    IdentityChannelMismatch {
        identity_id: String,
        channel: String,
    },

    /// No default identity set for a channel.
    #[error("no default identity set for channel: {0}")]
    NoDefaultIdentity(String),

    /// No channels registered in the manager.
    #[error("no channels registered")]
    NoChannels,

    /// Adapter-level connection error.
    #[error("connection error: {0}")]
    Connection(String),

    /// Send failed.
    #[error("send error: {0}")]
    Send(String),

    /// Serialization / deserialization error.
    #[error("serde error: {0}")]
    Serde(#[from] serde_json::Error),

    /// Queue error.
    #[error("queue error: {0}")]
    Queue(String),

    /// Generic / adapter-specific error.
    #[error("{0}")]
    Other(String),
}

/// Convenience result alias.
pub type Result<T> = std::result::Result<T, Error>;
