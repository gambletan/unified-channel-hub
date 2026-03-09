"""LLM backend abstraction — supports Claude, DeepSeek, Qwen, GLM, OpenAI, and more."""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class LLMBackend(Protocol):
    """Protocol for LLM backends."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
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

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": all_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class AnthropicBackend:
    """Claude via Anthropic Messages API."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model

    async def complete(
        self,
        messages: list[dict[str, str]],
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if system_prompt:
                body["system"] = system_prompt

            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]


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
