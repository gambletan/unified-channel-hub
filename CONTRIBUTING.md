# Contributing to unified-channel

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/gambletan/unified-channel.git
cd unified-channel
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest -v
```

## Adding a New Channel Adapter

1. Create `unified_channel/adapters/<channel>.py` implementing `ChannelAdapter`
2. Implement the 5 required methods: `connect`, `disconnect`, `receive`, `send`, `get_status`
3. Add the adapter to `unified_channel/__init__.py` exports
4. Add unit tests in `tests/test_adapters_unit.py`
5. Add documentation to `README.md`
6. If the adapter needs an external SDK, add it as an optional dependency in `pyproject.toml`

## Code Style

- Type hints on all public functions
- Async/await for all I/O operations
- Keep adapters in a single file each

## Pull Requests

- Keep PRs focused on a single change
- Include tests for new functionality
- Update README if adding user-facing features
- CI must pass (Python 3.10, 3.11, 3.12)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
