//! Identity router — manages multiple adapter instances and routes by identity.
//!
//! Enables multi-identity support: multiple adapters of the same channel type
//! (e.g., two Telegram accounts) each addressed by a unique `identity_id`.

use regex::Regex;
use std::collections::HashMap;
use std::sync::{Arc, LazyLock};
use tokio::sync::Mutex;
use tracing::{error, info};

use crate::adapter::ChannelAdapter;
use crate::error::{Error, Result};
use crate::types::{ChannelStatus, OutboundMessage};

/// Pattern: identity_id must be "channel:label" (alphanumeric + underscore).
static IDENTITY_PATTERN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-zA-Z0-9_]+:[a-zA-Z0-9_]+$").unwrap());

/// Extract the channel part from an identity_id.
fn channel_from_id(identity_id: &str) -> &str {
    identity_id.split(':').next().unwrap_or(identity_id)
}

/// Validate the identity_id format.
fn validate_identity_id(identity_id: &str) -> Result<()> {
    if !IDENTITY_PATTERN.is_match(identity_id) {
        return Err(Error::InvalidIdentityId {
            id: identity_id.to_string(),
        });
    }
    Ok(())
}

/// Multi-identity adapter router.
///
/// # Example
/// ```ignore
/// let mut router = IdentityRouter::new();
/// router.register("telegram:personal", personal_adapter)?;
/// router.register("telegram:work", work_adapter)?;
/// router.send("telegram:personal", msg).await?;
/// ```
pub struct IdentityRouter {
    adapters: HashMap<String, Arc<Mutex<Box<dyn ChannelAdapter>>>>,
    defaults: HashMap<String, String>,
}

impl IdentityRouter {
    /// Create an empty router.
    pub fn new() -> Self {
        Self {
            adapters: HashMap::new(),
            defaults: HashMap::new(),
        }
    }

    /// Register an adapter with a unique identity_id.
    pub fn register(
        &mut self,
        identity_id: impl Into<String>,
        adapter: Box<dyn ChannelAdapter>,
    ) -> Result<&mut Self> {
        let identity_id = identity_id.into();
        validate_identity_id(&identity_id)?;

        if self.adapters.contains_key(&identity_id) {
            return Err(Error::IdentityAlreadyRegistered(identity_id));
        }

        self.adapters
            .insert(identity_id, Arc::new(Mutex::new(adapter)));
        Ok(self)
    }

    /// Remove an adapter by identity_id.
    pub fn unregister(&mut self, identity_id: &str) -> Result<&mut Self> {
        if !self.adapters.contains_key(identity_id) {
            return Err(Error::IdentityNotFound(identity_id.to_string()));
        }
        self.adapters.remove(identity_id);

        // Clean up default if this identity was the default
        let channel = channel_from_id(identity_id);
        if self.defaults.get(channel).map(|s| s.as_str()) == Some(identity_id) {
            self.defaults.remove(channel);
        }
        Ok(self)
    }

    /// Send a message via a specific identity.
    pub async fn send(&self, identity_id: &str, msg: OutboundMessage) -> Result<Option<String>> {
        let adapter = self
            .adapters
            .get(identity_id)
            .ok_or_else(|| Error::IdentityNotFound(identity_id.to_string()))?;
        let a = adapter.lock().await;
        a.send(msg).await
    }

    /// Set the default identity for a channel type.
    pub fn set_default(&mut self, channel: &str, identity_id: &str) -> Result<&mut Self> {
        if !self.adapters.contains_key(identity_id) {
            return Err(Error::IdentityNotFound(identity_id.to_string()));
        }
        if channel_from_id(identity_id) != channel {
            return Err(Error::IdentityChannelMismatch {
                identity_id: identity_id.to_string(),
                channel: channel.to_string(),
            });
        }
        self.defaults
            .insert(channel.to_string(), identity_id.to_string());
        Ok(self)
    }

    /// Send a message via the default identity for a channel.
    pub async fn send_default(
        &self,
        channel: &str,
        msg: OutboundMessage,
    ) -> Result<Option<String>> {
        let identity_id = self
            .defaults
            .get(channel)
            .ok_or_else(|| Error::NoDefaultIdentity(channel.to_string()))?;
        self.send(identity_id, msg).await
    }

    /// Connect all registered adapters.
    pub async fn connect_all(&self) -> Result<()> {
        for (iid, adapter) in &self.adapters {
            let mut a = adapter.lock().await;
            a.connect().await.map_err(|e| {
                error!(identity = %iid, error = %e, "failed to connect");
                e
            })?;
            info!(identity = %iid, "connected");
        }
        Ok(())
    }

    /// Disconnect all registered adapters.
    pub async fn disconnect_all(&self) {
        for (iid, adapter) in &self.adapters {
            let mut a = adapter.lock().await;
            if let Err(e) = a.disconnect().await {
                error!(identity = %iid, error = %e, "error disconnecting");
            } else {
                info!(identity = %iid, "disconnected");
            }
        }
    }

    /// List registered identity IDs, optionally filtered by channel type.
    pub fn get_identities(&self, channel: Option<&str>) -> Vec<String> {
        match channel {
            None => self.adapters.keys().cloned().collect(),
            Some(ch) => self
                .adapters
                .keys()
                .filter(|iid| channel_from_id(iid) == ch)
                .cloned()
                .collect(),
        }
    }

    /// Get status of all registered identities.
    pub async fn get_status_all(&self) -> HashMap<String, ChannelStatus> {
        let mut statuses = HashMap::new();
        for (iid, adapter) in &self.adapters {
            let a = adapter.lock().await;
            let status = a.get_status().await;
            statuses.insert(iid.clone(), status);
        }
        statuses
    }

    /// Number of registered identities.
    pub fn len(&self) -> usize {
        self.adapters.len()
    }

    /// Whether the router has no registered identities.
    pub fn is_empty(&self) -> bool {
        self.adapters.is_empty()
    }
}

impl Default for IdentityRouter {
    fn default() -> Self {
        Self::new()
    }
}
