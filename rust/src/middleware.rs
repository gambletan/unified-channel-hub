//! Middleware layer — shared logic that channels don't re-implement.
//!
//! The middleware pipeline processes inbound messages and optionally produces
//! a reply (string or [`OutboundMessage`]).

use async_trait::async_trait;
use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use crate::types::{OutboundMessage, UnifiedMessage};

/// The result of processing a message through the pipeline.
#[derive(Debug, Clone)]
pub enum HandlerResult {
    /// A plain-text reply.
    Text(String),
    /// A structured outbound message.
    Outbound(OutboundMessage),
    /// No reply.
    None,
}

impl HandlerResult {
    /// Whether this result is `None`.
    pub fn is_none(&self) -> bool {
        matches!(self, HandlerResult::None)
    }
}

impl From<String> for HandlerResult {
    fn from(s: String) -> Self {
        HandlerResult::Text(s)
    }
}

impl From<&str> for HandlerResult {
    fn from(s: &str) -> Self {
        HandlerResult::Text(s.to_string())
    }
}

impl From<OutboundMessage> for HandlerResult {
    fn from(msg: OutboundMessage) -> Self {
        HandlerResult::Outbound(msg)
    }
}

impl From<Option<String>> for HandlerResult {
    fn from(opt: Option<String>) -> Self {
        match opt {
            Some(s) => HandlerResult::Text(s),
            None => HandlerResult::None,
        }
    }
}

/// Boxed async handler function.
pub type Handler = Box<
    dyn Fn(UnifiedMessage) -> Pin<Box<dyn Future<Output = HandlerResult> + Send>>
        + Send
        + Sync,
>;

/// Arc-wrapped async handler for `Send`-safe pipeline chaining.
pub type ArcHandler = Arc<
    dyn Fn(UnifiedMessage) -> Pin<Box<dyn Future<Output = HandlerResult> + Send>>
        + Send
        + Sync,
>;

/// Middleware trait — sits between inbound messages and the final handler.
#[async_trait]
pub trait Middleware: Send + Sync {
    /// Process a message, optionally delegating to `next`.
    async fn process(&self, msg: UnifiedMessage, next: &Handler) -> HandlerResult;
}

/// Gate messages by sender allowlist.
pub struct AccessMiddleware {
    allowed: Option<HashSet<String>>,
}

impl AccessMiddleware {
    /// Create with an explicit allowlist. `None` means allow all.
    pub fn new(allowed_user_ids: Option<impl IntoIterator<Item = String>>) -> Self {
        Self {
            allowed: allowed_user_ids.map(|ids| ids.into_iter().collect()),
        }
    }

    /// Allow all senders (no filtering).
    pub fn allow_all() -> Self {
        Self { allowed: None }
    }
}

#[async_trait]
impl Middleware for AccessMiddleware {
    async fn process(&self, msg: UnifiedMessage, next: &Handler) -> HandlerResult {
        if let Some(ref allowed) = self.allowed {
            if !allowed.contains(&msg.sender.id) {
                return HandlerResult::None;
            }
        }
        next(msg).await
    }
}

/// Async command handler function type.
pub type CommandHandler = Arc<
    dyn Fn(UnifiedMessage) -> Pin<Box<dyn Future<Output = HandlerResult> + Send>>
        + Send
        + Sync,
>;

/// Route `/commands` to registered handlers.
pub struct CommandMiddleware {
    commands: HashMap<String, CommandHandler>,
}

impl CommandMiddleware {
    /// Create an empty command router.
    pub fn new() -> Self {
        Self {
            commands: HashMap::new(),
        }
    }

    /// Register a command handler. Returns self for chaining.
    pub fn command<F, Fut>(mut self, name: impl Into<String>, handler: F) -> Self
    where
        F: Fn(UnifiedMessage) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = HandlerResult> + Send + 'static,
    {
        let handler = Arc::new(move |msg: UnifiedMessage| {
            Box::pin(handler(msg)) as Pin<Box<dyn Future<Output = HandlerResult> + Send>>
        });
        self.commands.insert(name.into(), handler);
        self
    }

    /// List registered command names.
    pub fn registered_commands(&self) -> Vec<&str> {
        self.commands.keys().map(|k| k.as_str()).collect()
    }
}

impl Default for CommandMiddleware {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Middleware for CommandMiddleware {
    async fn process(&self, msg: UnifiedMessage, next: &Handler) -> HandlerResult {
        if let Some(ref cmd) = msg.content.command {
            if let Some(handler) = self.commands.get(cmd.as_str()) {
                return handler(msg).await;
            }
        }
        next(msg).await
    }
}

/// Build a handler from a closure (convenience for the final handler in the pipeline).
pub fn handler_fn<F, Fut>(f: F) -> Handler
where
    F: Fn(UnifiedMessage) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = HandlerResult> + Send + 'static,
{
    Box::new(move |msg| Box::pin(f(msg)))
}

/// Build the chain handler for middleware at `index`.
fn build_chain(
    middlewares: Arc<Vec<Arc<dyn Middleware>>>,
    fallback: ArcHandler,
    index: usize,
) -> ArcHandler {
    if index >= middlewares.len() {
        return fallback;
    }
    let mw = Arc::clone(&middlewares[index]);
    let next = build_chain(Arc::clone(&middlewares), Arc::clone(&fallback), index + 1);
    Arc::new(move |msg: UnifiedMessage| {
        let mw = Arc::clone(&mw);
        let next = Arc::clone(&next);
        Box::pin(async move {
            let next_handler: Handler = Box::new(move |m: UnifiedMessage| {
                let next = Arc::clone(&next);
                Box::pin(async move { next(m).await })
                    as Pin<Box<dyn Future<Output = HandlerResult> + Send>>
            });
            mw.process(msg, &next_handler).await
        }) as Pin<Box<dyn Future<Output = HandlerResult> + Send>>
    })
}

/// A middleware pipeline that owns its middlewares and fallback handler.
///
/// This is the primary way to run messages through the middleware stack.
pub struct MiddlewarePipeline {
    middlewares: Arc<Vec<Arc<dyn Middleware>>>,
    fallback: ArcHandler,
}

impl MiddlewarePipeline {
    /// Create a new pipeline with the given fallback handler.
    pub fn new<F, Fut>(fallback: F) -> Self
    where
        F: Fn(UnifiedMessage) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = HandlerResult> + Send + 'static,
    {
        Self {
            middlewares: Arc::new(Vec::new()),
            fallback: Arc::new(move |msg: UnifiedMessage| {
                Box::pin(fallback(msg)) as Pin<Box<dyn Future<Output = HandlerResult> + Send>>
            }),
        }
    }

    /// Add a middleware to the end of the pipeline.
    pub fn add<M: Middleware + 'static>(&mut self, mw: M) {
        Arc::get_mut(&mut self.middlewares)
            .expect("pipeline already in use")
            .push(Arc::new(mw));
    }

    /// Process a message through the full pipeline.
    pub async fn process(&self, msg: UnifiedMessage) -> HandlerResult {
        let chain = build_chain(
            Arc::clone(&self.middlewares),
            Arc::clone(&self.fallback),
            0,
        );
        chain(msg).await
    }
}

/// Run a message through a middleware stack with a final handler.
///
/// Convenience function that wraps the middlewares in `Arc` for pipeline dispatch.
/// For repeated use, prefer [`MiddlewarePipeline`] to avoid re-wrapping on each call.
pub async fn run_pipeline(
    middlewares: Vec<Arc<dyn Middleware>>,
    fallback: ArcHandler,
    msg: UnifiedMessage,
) -> HandlerResult {
    let mws = Arc::new(middlewares);
    let chain = build_chain(mws, fallback, 0);
    chain(msg).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Identity, MessageContent};

    fn make_msg(text: &str) -> UnifiedMessage {
        UnifiedMessage::new("test", Identity::new("user1"), MessageContent::text(text))
    }

    fn make_cmd_msg(cmd: &str, args: Vec<&str>) -> UnifiedMessage {
        let text = format!("/{} {}", cmd, args.join(" "));
        UnifiedMessage::new(
            "test",
            Identity::new("user1"),
            MessageContent::command(
                text,
                cmd.to_string(),
                args.into_iter().map(String::from).collect(),
            ),
        )
    }

    #[tokio::test]
    async fn test_access_middleware_allows() {
        let mw = AccessMiddleware::new(Some(vec!["user1".to_string()]));
        let handler: Handler = handler_fn(|_| async { HandlerResult::Text("ok".into()) });
        let result = mw.process(make_msg("hi"), &handler).await;
        assert!(matches!(result, HandlerResult::Text(s) if s == "ok"));
    }

    #[tokio::test]
    async fn test_access_middleware_blocks() {
        let mw = AccessMiddleware::new(Some(vec!["other".to_string()]));
        let handler: Handler = handler_fn(|_| async { HandlerResult::Text("ok".into()) });
        let result = mw.process(make_msg("hi"), &handler).await;
        assert!(result.is_none());
    }

    #[tokio::test]
    async fn test_command_middleware_routes() {
        let mw = CommandMiddleware::new()
            .command("ping", |_| async { HandlerResult::Text("pong".into()) });
        let handler: Handler = handler_fn(|_| async { HandlerResult::Text("fallback".into()) });

        let result = mw.process(make_cmd_msg("ping", vec![]), &handler).await;
        assert!(matches!(result, HandlerResult::Text(s) if s == "pong"));
    }

    #[tokio::test]
    async fn test_command_middleware_passes_through() {
        let mw = CommandMiddleware::new()
            .command("ping", |_| async { HandlerResult::Text("pong".into()) });
        let handler: Handler = handler_fn(|_| async { HandlerResult::Text("fallback".into()) });

        let result = mw.process(make_msg("hello"), &handler).await;
        assert!(matches!(result, HandlerResult::Text(s) if s == "fallback"));
    }

    #[tokio::test]
    async fn test_pipeline_with_access_and_command() {
        let mut pipeline = MiddlewarePipeline::new(|_| async { HandlerResult::Text("default".into()) });
        pipeline.add(AccessMiddleware::new(Some(vec!["user1".to_string()])));
        pipeline.add(CommandMiddleware::new().command("ping", |_| async {
            HandlerResult::Text("pong".into())
        }));

        // Command gets routed
        let result = pipeline.process(make_cmd_msg("ping", vec![])).await;
        assert!(matches!(result, HandlerResult::Text(s) if s == "pong"));

        // Regular text falls through to default
        let result = pipeline.process(make_msg("hello")).await;
        assert!(matches!(result, HandlerResult::Text(s) if s == "default"));
    }

    #[tokio::test]
    async fn test_pipeline_access_blocks() {
        let mut pipeline = MiddlewarePipeline::new(|_| async { HandlerResult::Text("default".into()) });
        pipeline.add(AccessMiddleware::new(Some(vec!["admin".to_string()])));

        // user1 is blocked
        let msg = make_msg("hi");
        let result = pipeline.process(msg).await;
        assert!(result.is_none());
    }

    #[tokio::test]
    async fn test_empty_pipeline() {
        let pipeline = MiddlewarePipeline::new(|_| async { HandlerResult::Text("fallback".into()) });
        let result = pipeline.process(make_msg("hello")).await;
        assert!(matches!(result, HandlerResult::Text(s) if s == "fallback"));
    }
}
