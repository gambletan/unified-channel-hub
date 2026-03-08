# Contributing to unified-channel

Thanks for your interest in contributing! This project aims to be the go-to messaging abstraction layer for AI agents.

## Getting Started

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Make your changes in the relevant language directory (`python/`, `typescript/`, or `java/`)
4. Add tests for new functionality
5. Ensure all tests pass (see below)
6. Submit a pull request

## Development Setup

### Python

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

### TypeScript

```bash
cd typescript
npm install
npm run build
npm test
```

### Java

```bash
cd java
mvn test
```

## Pull Request Guidelines

- **One PR per feature/fix** — keep changes focused
- **Add tests** — new adapters need at least constructor + message parsing tests
- **Update docs** — if you add a channel, update the README table
- **Follow existing patterns** — look at existing adapters for the style guide
- **Cross-language consistency** — if adding a new adapter, consider adding it to all three languages (or at minimum, document it)

## Adding a New Channel Adapter

Each adapter should:

1. Implement the `ChannelAdapter` interface
2. Use dynamic/lazy imports for SDK dependencies
3. Parse incoming messages into `UnifiedMessage` format
4. Handle command prefix detection (configurable via constructor)
5. Include at least basic tests (constructor, channelId, message parsing)

Template for a new adapter (TypeScript example):

```typescript
export class MyChannelAdapter implements ChannelAdapter {
  readonly channelId = "mychannel";
  // ... implement connect(), disconnect(), onMessage(), send(), getStatus()
}
```

## Code Style

- **Python**: Follow PEP 8, use type hints, async/await for I/O
- **TypeScript**: ESM, strict TypeScript, no `any` in public APIs
- **Java**: Java 17+ features (records, sealed interfaces, var), follow Maven conventions

## Testing

- All PRs must pass CI (tests + type-check/lint for the affected language)
- Adapter tests should work without the actual SDK installed (test construction, channelId, message format)
- Integration tests with real SDKs are optional and run separately

## Reporting Issues

- Use GitHub Issues
- Include: language, version, channel, error message, minimal reproduction
- For security issues, email directly (do not open a public issue)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
