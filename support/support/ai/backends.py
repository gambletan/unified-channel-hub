"""LLM backend abstraction — supports Claude, DeepSeek, Qwen, GLM, OpenAI, and more."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, AsyncIterator, Callable, Coroutine, Protocol

import httpx

logger = logging.getLogger(__name__)


# Callback type: async fn(chunk_text, full_text_so_far)
StreamCallback = Callable[[str, str], Coroutine[Any, Any, None]]


# --- reasoning (<think>) stripping -------------------------------------------
# Reasoning models (e.g. MiniMax-M2.7) emit chain-of-thought inline as
# <think>...</think> in the response content. Customers must never see it.

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_TAGS = ("<think>", "</think>")


def strip_think(text: str) -> str:
    """Remove complete <think>...</think> blocks (and any unclosed trailing one)."""
    s = _THINK_BLOCK.sub("", text)
    idx = s.find("<think>")
    if idx != -1:
        s = s[:idx]
    return s.strip()


def _hold_partial_tag(s: str) -> str:
    """Trim a trailing fragment that could be the start of a <think>/<​/think> tag,
    so a half-arrived tag is never emitted mid-stream."""
    best = 0
    for tag in _THINK_TAGS:
        for k in range(min(len(s), len(tag) - 1), 0, -1):
            if s.endswith(tag[:k]):
                best = max(best, k)
                break
    return s[:-best] if best else s


class ThinkStreamFilter:
    """Stateful filter that suppresses <think>...</think> from a streamed reply,
    correctly handling tags split across chunks. feed() returns the new clean
    delta to emit; flush() returns any remainder after the stream ends."""

    def __init__(self) -> None:
        self._raw = ""
        self._emitted = 0

    def _clean(self, streaming: bool) -> str:
        s = _THINK_BLOCK.sub("", self._raw)
        idx = s.find("<think>")
        if idx != -1:          # unclosed think block — drop it and everything after
            s = s[:idx]
        if streaming:
            s = _hold_partial_tag(s)
        return s.lstrip()       # drop leading whitespace left by a removed leading block

    def feed(self, chunk: str) -> str:
        self._raw += chunk
        clean = self._clean(streaming=True)
        delta = clean[self._emitted:]
        self._emitted = len(clean)
        return delta

    def flush(self) -> str:
        clean = self._clean(streaming=False)
        delta = clean[self._emitted:]
        self._emitted = len(clean)
        return delta

    @property
    def text(self) -> str:
        return self._clean(streaming=False)


class LLMBackend(Protocol):
    """Protocol for LLM backends."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str: ...

    async def stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        on_chunk: StreamCallback | None = None,
    ) -> str: ...


# Pre-configured backends
BACKENDS: dict[str, dict[str, Any]] = {
    "claude": {
        "type": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
    },
    "deepseek": {
        "type": "openai",
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "qwen": {
        "type": "openai",
        "env_key": "QWEN_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-turbo",
    },
    "glm": {
        "type": "openai",
        "env_key": "GLM_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
    },
    "minimax": {
        "type": "openai",
        "env_key": "MINIMAX_API_KEY",
        "base_url": "https://api.minimaxi.com/v1",
        "default_model": "MiniMax-Text-01",
    },
    "openai": {
        "type": "openai",
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
}


class OpenAICompatibleBackend:
    """Works with any OpenAI-compatible API (GPT, DeepSeek, Qwen, GLM, MiniMax, Ollama, etc.)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        # Reuse client across requests (connection pooling)
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend(messages)

        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": all_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return strip_think(data["choices"][0]["message"]["content"])

    async def stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        on_chunk: StreamCallback | None = None,
    ) -> str:
        """Stream completion, calling on_chunk for each token. Returns full text."""
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend(messages)

        think = ThinkStreamFilter()   # suppress <think>...</think> from the live stream
        async with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": all_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        clean = think.feed(content)
                        if clean and on_chunk:
                            await on_chunk(clean, think.text)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        tail = think.flush()
        if tail and on_chunk:
            await on_chunk(tail, think.text)
        return think.text

    async def close(self) -> None:
        await self._client.aclose()


class AnthropicBackend:
    """Claude via Anthropic Messages API."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            body["system"] = system_prompt

        resp = await self._client.post(
            "https://api.anthropic.com/v1/messages",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        return strip_think(data["content"][0]["text"])

    async def stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        on_chunk: StreamCallback | None = None,
    ) -> str:
        """Stream completion via Anthropic SSE. Returns full text."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system_prompt:
            body["system"] = system_prompt

        think = ThinkStreamFilter()
        async with self._client.stream(
            "POST",
            "https://api.anthropic.com/v1/messages",
            json=body,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                    if event.get("type") == "content_block_delta":
                        content = event.get("delta", {}).get("text", "")
                        if content:
                            clean = think.feed(content)
                            if clean and on_chunk:
                                await on_chunk(clean, think.text)
                    elif event.get("type") == "message_stop":
                        break
                except (json.JSONDecodeError, KeyError):
                    continue

        tail = think.flush()
        if tail and on_chunk:
            await on_chunk(tail, think.text)
        return think.text

    async def close(self) -> None:
        await self._client.aclose()


def create_backend(
    backend_name: str = "openai",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> LLMBackend:
    """Create an LLM backend by name or custom config."""
    config = BACKENDS.get(backend_name, {})
    backend_type = config.get("type", "openai")

    resolved_key = api_key or os.getenv(config.get("env_key", ""), "")
    resolved_model = model or config.get("default_model", "gpt-4o-mini")
    resolved_url = base_url or config.get("base_url", "https://api.openai.com/v1")

    if backend_type == "anthropic":
        return AnthropicBackend(api_key=resolved_key, model=resolved_model)
    else:
        return OpenAICompatibleBackend(
            api_key=resolved_key, base_url=resolved_url, model=resolved_model
        )
