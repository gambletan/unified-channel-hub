# Customer Voice Transcription + Translation — Design

**Status:** Approved + API-verified, pending spec review
**Date:** 2026-06-23

## Goal

When a customer sends a **voice/audio** message, transcribe it and translate it to
the agent's language, and show both in the agent's Telegram topic — so agents can
understand foreign-language voice notes without playing the audio.

Phase 1 is **customer → agent only**. Agent → customer voice translation is deferred.

## Verified facts (real API test, 2026-06-23)

A live call with the production key confirmed the whole approach:

- Model: **`gemini-flash-lite-latest`** (the `*-latest` alias survives model retirements;
  fixed names like `gemini-2.0-flash-lite-001` already 404). 
- Endpoint: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={KEY}`
- Request body: `{"contents":[{"parts":[{"text": <prompt>}, {"inline_data":{"mime_type": <mime>, "data": <base64>}}]}]}`
- Response: `candidates[0].content.parts[0].text` → the model returns the requested JSON.
- One call does **transcribe + translate together** (cheaper than STT-then-translate).
- Test input (Chinese voice) → `{"transcript":"你好，我的订单已经一个星期了...","translation":"Hello, my order has been a week..."}` ✓
- Key: **`GEMINI_API_KEY` now lives in support's own `~/unified-support-deploy/.env`** (copied from
  StyleDNA so support never reaches into another service's env at runtime).

## Current state

- Customer voice/audio arrives as a `ContentType.MEDIA` message and is forwarded to the
  topic by `topic_bridge._forward_media_to_topic` (the agent gets the raw audio, nothing else).
- The library has `voice.py` (STTProvider/WhisperLocalSTT/OpenAISTT) but it is NOT wired into
  support, and it only does STT, not translation. We do not use it — Gemini does both in one call.
- Translation of text already works via `ModelRouter` (MiniMax); voice was the gap.

## Architecture

### Component 1 — `support/support/ai/voice_translate.py`

A small, self-contained translator with one responsibility.

```
class GeminiVoiceTranslator:
    def __init__(self, api_key: str, model: str = "gemini-flash-lite-latest", timeout: float = 30)
    async def transcribe_and_translate(self, audio: bytes, mime: str, target_lang: str
        ) -> tuple[str, str] | None
        # → (transcript, translation), or None on any failure
```

- Builds the generateContent request (prompt + inline base64 audio), parses the JSON the model
  returns, and returns `(transcript, translation)`.
- Robust parsing: the model is asked for strict JSON; strip ```` ```json ```` fences if present;
  on any parse/HTTP error return `None` (caller falls back to audio-only).
- Depends only on `httpx` (already a dep) + the api_key. No coupling to topic_bridge.

### Component 2 — wiring in `topic_bridge`

In the customer-media path (`_forward_media_to_topic`), after the audio is forwarded:

```
if media_type in ("voice", "audio") and self._voice_translator:
    result = await self._voice_translator.transcribe_and_translate(
        audio_bytes, mime, target_lang=<agent's default_lang name>)
    if result:
        transcript, translation = result
        post to topic thread:
            🎤 [语音转写] {transcript}
            🌐 [译文] {translation}
```

- The audio bytes are already available in `_forward_media_to_topic` (it decodes the media for
  forwarding) — reuse them, no second download.
- Target language = the agents' `default_lang` (the language the support team reads).
- Fire-and-forget around the existing forward: a failure here never blocks or breaks the audio
  forward (which already happened).

### Component 3 — config + wiring

`config.yaml`:
```yaml
voice_translate:
  enabled: true
  model: gemini-flash-lite-latest
  # api key from env GEMINI_API_KEY
```

`app.py` builds a `GeminiVoiceTranslator` when `voice_translate.enabled` and a `GEMINI_API_KEY`
is present, and passes it to `TopicBridgeMiddleware` (new optional `voice_translator=` param,
defaulting to None so nothing breaks when unconfigured).

## Data flow

1. Customer sends a voice note → adapter → `ContentType.MEDIA`, media_type=voice → `_handle_customer_dm`.
2. `_forward_media_to_topic` decodes the audio, forwards it to the topic (unchanged).
3. If a voice translator is configured and it's voice/audio: call Gemini → post transcript + 译文
   into the same topic thread.
4. Agent reads the text; replies as usual (agent reply → translated to customer's language — existing).

## Error handling

- No key / disabled → `voice_translator` is None → step 3 skipped entirely (audio-only, today's behavior).
- Gemini HTTP error, timeout, or unparseable response → `transcribe_and_translate` returns None →
  no transcript posted, audio still forwarded. Logged at WARNING.
- Telegram voice is `audio/ogg` (opus); pass the real mime through. `audio/aiff` and `audio/ogg`
  both accepted by Gemini.

## Testing

- Unit: `GeminiVoiceTranslator.transcribe_and_translate` with a mocked httpx response — assert it
  parses `(transcript, translation)` from the JSON, strips code fences, and returns None on
  HTTP error / bad JSON.
- Integration: the real-API smoke test already passed (Chinese voice → EN transcript+translation).

## Out of scope (YAGNI)

- Agent → customer voice translation (Phase 2).
- TTS (speaking the reply back as audio).
- Streaming/partial transcription — support voice notes are short; one call is fine.
- Reusing library `voice.py` — Gemini's one-call transcribe+translate is simpler and cheaper.

## Operational note

`GEMINI_API_KEY` is patched into the server `.env` (copied from StyleDNA). Like other support
secrets it lives only in `~/unified-support-deploy/.env`, not in git. The Docker build-from-source
plan would inject it via env as designed.
