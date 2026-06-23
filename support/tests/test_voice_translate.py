"""Tests for GeminiVoiceTranslator (audio → transcript + translation in one call)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from support.ai.voice_translate import GeminiVoiceTranslator


def _gemini_resp(text):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    return resp


def _client_returning(resp):
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_parses_transcript_and_translation():
    resp = _gemini_resp('{"transcript":"你好，订单没收到","translation":"Hi, order not received"}')
    with patch("httpx.AsyncClient", return_value=_client_returning(resp)):
        out = await GeminiVoiceTranslator(api_key="k").transcribe_and_translate(
            b"audiobytes", "audio/ogg", "English")
    assert out == ("你好，订单没收到", "Hi, order not received")


@pytest.mark.asyncio
async def test_strips_markdown_code_fences():
    resp = _gemini_resp('```json\n{"transcript":"a","translation":"b"}\n```')
    with patch("httpx.AsyncClient", return_value=_client_returning(resp)):
        out = await GeminiVoiceTranslator(api_key="k").transcribe_and_translate(
            b"x", "audio/ogg", "English")
    assert out == ("a", "b")


@pytest.mark.asyncio
async def test_returns_none_on_http_error():
    import httpx
    resp = MagicMock()
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=MagicMock()))
    with patch("httpx.AsyncClient", return_value=_client_returning(resp)):
        out = await GeminiVoiceTranslator(api_key="k").transcribe_and_translate(
            b"x", "audio/ogg", "English")
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_on_unparseable_response():
    resp = _gemini_resp("sorry, I can't do that")
    with patch("httpx.AsyncClient", return_value=_client_returning(resp)):
        out = await GeminiVoiceTranslator(api_key="k").transcribe_and_translate(
            b"x", "audio/ogg", "English")
    assert out is None


@pytest.mark.asyncio
async def test_no_api_key_returns_none_without_calling():
    out = await GeminiVoiceTranslator(api_key="").transcribe_and_translate(
        b"x", "audio/ogg", "English")
    assert out is None


@pytest.mark.asyncio
async def test_parses_json_wrapped_in_prose():
    resp = _gemini_resp('Sure! Here is the result:\n{"transcript":"a","translation":"b"}\nHope it helps.')
    with patch("httpx.AsyncClient", return_value=_client_returning(resp)):
        out = await GeminiVoiceTranslator(api_key="k").transcribe_and_translate(
            b"x", "audio/ogg", "English")
    assert out == ("a", "b")
