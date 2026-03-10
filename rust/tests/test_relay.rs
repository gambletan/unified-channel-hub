//! Tests for RelayMiddleware: rules, filters, transforms, broadcast, bidirectional.

use std::collections::HashMap;
use std::sync::Arc;

use unified_channel::middleware::*;
use unified_channel::relay::*;
use unified_channel::types::*;

fn make_msg(channel: &str, sender: &str, text: &str) -> UnifiedMessage {
    let mut msg = UnifiedMessage::new(
        channel,
        Identity::new(sender).with_display_name(sender.to_string()),
        MessageContent::text(text),
    );
    msg.chat_id = Some("chat1".to_string());
    msg
}

#[tokio::test]
async fn test_relay_basic_rule() {
    let mut relay = RelayMiddleware::new();
    relay.add_rule("telegram", "slack", "general", None, None, true, false);
    assert_eq!(relay.rule_count(), 1);
}

#[tokio::test]
async fn test_relay_bidirectional_adds_two_rules() {
    let mut relay = RelayMiddleware::new();
    relay.add_rule("telegram", "slack", "general", None, None, true, true);
    assert_eq!(relay.rule_count(), 2);
}

#[tokio::test]
async fn test_relay_broadcast_adds_multiple_rules() {
    let mut relay = RelayMiddleware::new();
    let mut targets = HashMap::new();
    targets.insert("slack".to_string(), "general".to_string());
    targets.insert("discord".to_string(), "lobby".to_string());
    targets.insert("email".to_string(), "team@co.com".to_string());
    relay.add_broadcast("telegram", targets, None, None);
    assert_eq!(relay.rule_count(), 3);
}

#[tokio::test]
async fn test_relay_passes_through_result() {
    // RelayMiddleware should pass through the next handler's result
    let mut relay = RelayMiddleware::new();
    relay.add_rule("telegram", "slack", "general", None, None, true, false);

    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("original".into()) });
    let msg = make_msg("telegram", "alice", "hello");
    let result = relay.process(msg, &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "original"));
}

#[tokio::test]
async fn test_relay_with_filter() {
    let filter: FilterFn = Arc::new(|msg: &UnifiedMessage| {
        msg.content.text.contains("urgent")
    });

    let mut relay = RelayMiddleware::new();
    relay.add_rule("telegram", "slack", "alerts", Some(filter), None, true, false);
    assert_eq!(relay.rule_count(), 1);

    // The relay processes but the filter is checked internally
    let handler: Handler = handler_fn(|_| async { HandlerResult::None });

    // Non-matching message
    let msg = make_msg("telegram", "alice", "hello");
    let result = relay.process(msg, &handler).await;
    assert!(result.is_none());

    // Matching message
    let msg = make_msg("telegram", "alice", "urgent: server down");
    let result = relay.process(msg, &handler).await;
    assert!(result.is_none()); // relay doesn't change the handler result
}

#[tokio::test]
async fn test_relay_with_transform() {
    let transform: TransformFn = Arc::new(|msg: &UnifiedMessage| {
        format!("SUMMARY: {}", msg.content.text.chars().take(20).collect::<String>())
    });

    let mut relay = RelayMiddleware::new();
    relay.add_rule("telegram", "slack", "digests", None, Some(transform), false, false);
    assert_eq!(relay.rule_count(), 1);
}

#[tokio::test]
async fn test_relay_wildcard_source() {
    let mut relay = RelayMiddleware::new();
    relay.add_rule("*", "telegram", "log_chat", None, None, true, false);
    assert_eq!(relay.rule_count(), 1);

    // Wildcard should match any channel
    let handler: Handler = handler_fn(|_| async { HandlerResult::None });

    let msg = make_msg("discord", "bob", "hello");
    let result = relay.process(msg, &handler).await;
    assert!(result.is_none());

    let msg = make_msg("slack", "carol", "world");
    let result = relay.process(msg, &handler).await;
    assert!(result.is_none());
}

#[tokio::test]
async fn test_relay_no_matching_rules() {
    let mut relay = RelayMiddleware::new();
    relay.add_rule("slack", "telegram", "123", None, None, true, false);

    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("ok".into()) });

    // Message from discord, no rules match
    let msg = make_msg("discord", "user", "hello");
    let result = relay.process(msg, &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "ok"));
}

#[tokio::test]
async fn test_relay_default() {
    let relay = RelayMiddleware::default();
    assert_eq!(relay.rule_count(), 0);
}

#[tokio::test]
async fn test_relay_chained_add_rules() {
    let mut relay = RelayMiddleware::new();
    relay
        .add_rule("telegram", "slack", "general", None, None, true, false)
        .add_rule("slack", "email", "team@co.com", None, None, true, false)
        .add_rule("*", "telegram", "logs", None, None, false, false);
    assert_eq!(relay.rule_count(), 3);
}
