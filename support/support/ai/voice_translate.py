"""Gemini-based voice transcription + translation.

Sends a voice/audio clip to Gemini and gets back, in ONE call, the original
transcript plus a translation into the agent's language. Used so support agents
can read foreign-language voice notes instead of playing the audio.
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

logger = logging.getLogger(__name__)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiVoiceTranslator:
    """Transcribe + translate an audio clip via Gemini generateContent."""

    def __init__(self, api_key: str, model: str = "gemini-flash-lite-latest", timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def transcribe_and_translate(
        self, audio: bytes, mime: str, target_lang: str, source_hint: str | None = None
    ) -> tuple[str, str] | None:
        """Return (transcript, translation), or None on any failure (caller falls back to audio-only).

        source_hint biases the transcription language (e.g. "Chinese") to avoid the
        model mis-hearing one CJK language as another on borderline audio.
        """
        if not self.api_key:
            return None
        hint = f"The audio is most likely spoken in {source_hint}. " if source_hint else ""
        prompt = (
            f"{hint}Transcribe the audio in its original language, then translate it to {target_lang}. "
            'Return ONLY compact JSON, no prose: '
            '{"transcript":"<original text>","translation":"<' + target_lang + ' text>"}'
        )
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode("ascii")}},
                ]
            }]
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    _GEMINI_URL.format(model=self.model),
                    params={"key": self.api_key},
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return self._parse(text)
        except Exception as e:
            logger.warning("voice transcribe/translate failed: %s", e)
            return None

    @staticmethod
    def _parse(text: str) -> tuple[str, str] | None:
        s = text.strip()
        if s.startswith("```"):
            # strip a ```json ... ``` fence the model sometimes adds
            parts = s.split("```")
            s = parts[1] if len(parts) >= 2 else s.strip("`")
            if s.lstrip().lower().startswith("json"):
                s = s.lstrip()[4:]
            s = s.strip()
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError, TypeError):
            # Lenient fallback: the model occasionally wraps the JSON in prose —
            # grab the outermost {...} and try again.
            start, end = s.find("{"), s.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                obj = json.loads(s[start:end + 1])
            except (json.JSONDecodeError, ValueError, TypeError):
                return None
        transcript = obj.get("transcript") if isinstance(obj, dict) else None
        translation = obj.get("translation") if isinstance(obj, dict) else None
        if not transcript or not translation:
            return None
        return (str(transcript), str(translation))
