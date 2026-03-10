//! Tests for middleware pipeline: AccessMiddleware, CommandMiddleware, pipeline execution.

use std::sync::Arc;
use unified_channel::middleware::*;
use unified_channel::types::*;

fn make_msg(sender_id: &str, text: &str) -> UnifiedMessage {
    UnifiedMessage::new(
        "test",
        Identity::new(sender_id),
        MessageContent::text(text),
    )
}

fn make_cmd_msg(sender_id: &str, cmd: &str, args: Vec<&str>) -> UnifiedMessage {
    let text = format!("/{} {}", cmd, args.join(" "));
    UnifiedMessage::new(
        "test",
        Identity::new(sender_id),
        MessageContent::command(text, cmd.to_string(), args.into_iter().map(String::from).collect()),
    )
}

#[tokio::test]
async fn test_access_allows_listed_user() {
    let mw = AccessMiddleware::new(Some(vec!["user1".to_string()]));
    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("ok".into()) });
    let result = mw.process(make_msg("user1", "hi"), &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "ok"));
}

#[tokio::test]
async fn test_access_blocks_unlisted_user() {
    let mw = AccessMiddleware::new(Some(vec!["user1".to_string()]));
    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("ok".into()) });
    let result = mw.process(make_msg("user2", "hi"), &handler).await;
    assert!(result.is_none());
}

#[tokio::test]
async fn test_access_allow_all() {
    let mw = AccessMiddleware::allow_all();
    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("ok".into()) });
    let result = mw.process(make_msg("anyone", "hi"), &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "ok"));
}

#[tokio::test]
async fn test_command_routes_to_handler() {
    let mw = CommandMiddleware::new()
        .command("ping", |_| async { HandlerResult::Text("pong".into()) })
        .command("help", |_| async { HandlerResult::Text("help text".into()) });

    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("fallback".into()) });

    let result = mw.process(make_cmd_msg("u1", "ping", vec![]), &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "pong"));

    let result = mw.process(make_cmd_msg("u1", "help", vec![]), &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "help text"));
}

#[tokio::test]
async fn test_command_passes_unknown_to_next() {
    let mw = CommandMiddleware::new()
        .command("ping", |_| async { HandlerResult::Text("pong".into()) });
    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("fallback".into()) });

    let result = mw.process(make_cmd_msg("u1", "unknown", vec![]), &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "fallback"));
}

#[tokio::test]
async fn test_command_passes_non_command_to_next() {
    let mw = CommandMiddleware::new()
        .command("ping", |_| async { HandlerResult::Text("pong".into()) });
    let handler: Handler = handler_fn(|_| async { HandlerResult::Text("fallback".into()) });

    let result = mw.process(make_msg("u1", "hello"), &handler).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "fallback"));
}

#[tokio::test]
async fn test_command_registered_commands() {
    let mw = CommandMiddleware::new()
        .command("ping", |_| async { HandlerResult::None })
        .command("help", |_| async { HandlerResult::None })
        .command("status", |_| async { HandlerResult::None });

    let mut cmds = mw.registered_commands();
    cmds.sort();
    assert_eq!(cmds, vec!["help", "ping", "status"]);
}

#[tokio::test]
async fn test_handler_result_conversions() {
    let r1: HandlerResult = "hello".into();
    assert!(matches!(r1, HandlerResult::Text(s) if s == "hello"));

    let r2: HandlerResult = OutboundMessage::text("c", "t").into();
    assert!(matches!(r2, HandlerResult::Outbound(_)));

    let r3: HandlerResult = None::<String>.into();
    assert!(r3.is_none());

    let r4: HandlerResult = Some("hi".to_string()).into();
    assert!(matches!(r4, HandlerResult::Text(s) if s == "hi"));
}

#[tokio::test]
async fn test_pipeline_single_middleware() {
    let access = AccessMiddleware::new(Some(vec!["admin".to_string()]));
    let middlewares: Vec<Arc<dyn Middleware>> = vec![Arc::new(access)];
    let fallback: ArcHandler = Arc::new(move |msg: UnifiedMessage| {
        Box::pin(async move { HandlerResult::Text("ok".into()) })
            as std::pin::Pin<Box<dyn std::future::Future<Output = HandlerResult> + Send>>
    });

    let msg = make_msg("admin", "hi");
    let result = run_pipeline(middlewares.clone(), fallback.clone(), msg).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "ok"));

    let msg = make_msg("intruder", "hi");
    let result = run_pipeline(middlewares, fallback, msg).await;
    assert!(result.is_none());
}

#[tokio::test]
async fn test_pipeline_chained_middlewares() {
    let access = AccessMiddleware::new(Some(vec!["admin".to_string()]));
    let commands = CommandMiddleware::new()
        .command("ping", |_| async { HandlerResult::Text("pong".into()) });

    let middlewares: Vec<Arc<dyn Middleware>> = vec![
        Arc::new(access),
        Arc::new(commands),
    ];
    let fallback: ArcHandler = Arc::new(move |_: UnifiedMessage| {
        Box::pin(async move { HandlerResult::Text("default".into()) })
            as std::pin::Pin<Box<dyn std::future::Future<Output = HandlerResult> + Send>>
    });

    // Admin + ping command -> pong
    let msg = make_cmd_msg("admin", "ping", vec![]);
    let result = run_pipeline(middlewares.clone(), fallback.clone(), msg).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "pong"));

    // Admin + non-command -> fallback
    let msg = make_msg("admin", "hello");
    let result = run_pipeline(middlewares.clone(), fallback.clone(), msg).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "default"));

    // Non-admin -> blocked
    let msg = make_cmd_msg("intruder", "ping", vec![]);
    let result = run_pipeline(middlewares, fallback, msg).await;
    assert!(result.is_none());
}

#[tokio::test]
async fn test_pipeline_empty() {
    let middlewares: Vec<Arc<dyn Middleware>> = vec![];
    let fallback: ArcHandler = Arc::new(move |msg: UnifiedMessage| {
        Box::pin(async move { HandlerResult::Text(format!("echo: {}", msg.content.text)) })
            as std::pin::Pin<Box<dyn std::future::Future<Output = HandlerResult> + Send>>
    });

    let msg = make_msg("u1", "hi");
    let result = run_pipeline(middlewares, fallback, msg).await;
    assert!(matches!(result, HandlerResult::Text(s) if s == "echo: hi"));
}
