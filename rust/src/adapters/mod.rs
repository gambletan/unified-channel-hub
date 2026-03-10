//! Platform-specific channel adapters.
//!
//! Each adapter is gated behind a feature flag so you only compile
//! the dependencies you actually need.

#[cfg(feature = "telegram")]
pub mod telegram;

#[cfg(feature = "discord")]
pub mod discord;

#[cfg(feature = "slack")]
pub mod slack;

#[cfg(feature = "email")]
pub mod email;

#[cfg(feature = "matrix")]
pub mod matrix;

#[cfg(feature = "twilio")]
pub mod twilio_sms;

#[cfg(feature = "twilio")]
pub mod twilio_voice;

#[cfg(feature = "homeassistant")]
pub mod homeassistant;

#[cfg(feature = "irc-adapter")]
pub mod irc;

#[cfg(feature = "nostr")]
pub mod nostr;

#[cfg(feature = "webhook")]
pub mod webhook;

#[cfg(feature = "mattermost")]
pub mod mattermost;
