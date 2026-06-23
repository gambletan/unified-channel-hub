"""Tests for ticket middleware."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from support.db import Database
from support.models import Ticket, TicketMessage, TicketStatus
from support.tickets.manager import TicketMiddleware


class FakeMessage:
    def __init__(self, channel="telegram", chat_id="123", sender_id="u1", text="Hello"):
        self.channel = channel
        self.chat_id = chat_id
        self.content = type("C", (), {"text": text, "type": "text"})()
        self.sender = type("S", (), {
            "id": sender_id, "display_name": "Test User", "username": "testuser"
        })()
        self.metadata = {}
        self.id = "msg1"
        self.timestamp = datetime.now(timezone.utc)
        self.thread_id = None
        self.raw = None


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_ticket_created_on_first_message(db):
    mw = TicketMiddleware(db)
    msg = FakeMessage()
    handler = AsyncMock(return_value="Got it!")

    result = await mw.process(msg, handler)

    assert result == "Got it!"
    handler.assert_called_once()

    # Ticket should be created
    ticket = await db.find_ticket_by_chat("telegram", "123")
    assert ticket is not None
    assert ticket.status == TicketStatus.OPEN
    assert ticket.customer_name == "Test User"


@pytest.mark.asyncio
async def test_same_chat_reuses_ticket(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    await mw.process(FakeMessage(text="First message"), handler)
    await mw.process(FakeMessage(text="Second message"), handler)

    tickets = await db.list_tickets()
    assert len(tickets) == 1  # Same ticket reused

    msgs = await db.get_messages(tickets[0].id)
    assert len(msgs) == 4  # 2 customer + 2 AI replies


@pytest.mark.asyncio
async def test_different_chats_create_different_tickets(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    await mw.process(FakeMessage(chat_id="a"), handler)
    await mw.process(FakeMessage(chat_id="b"), handler)

    tickets = await db.list_tickets()
    assert len(tickets) == 2


@pytest.mark.asyncio
async def test_messages_stored(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="Hello! How can I help?")

    await mw.process(FakeMessage(text="I need help"), handler)

    tickets = await db.list_tickets()
    msgs = await db.get_messages(tickets[0].id)
    assert len(msgs) == 2
    assert msgs[0].role == "customer"
    assert msgs[0].content == "I need help"
    assert msgs[1].role == "ai"
    assert msgs[1].content == "Hello! How can I help?"


@pytest.mark.asyncio
async def test_subject_extraction(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    await mw.process(FakeMessage(text="My order #12345 is missing"), handler)
    ticket = await db.find_ticket_by_chat("telegram", "123")
    assert ticket.subject == "My order #12345 is missing"


@pytest.mark.asyncio
async def test_long_subject_truncated(db):
    mw = TicketMiddleware(db)
    handler = AsyncMock(return_value="OK")

    long_text = "A" * 100
    await mw.process(FakeMessage(text=long_text), handler)
    ticket = await db.find_ticket_by_chat("telegram", "123")
    assert len(ticket.subject) <= 53  # 50 + "..."


# ---------------------------------------------------------------------------
# TopicBridge: webchat offline redelivery (agent reply → user)
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock
from support.tickets.topic_bridge import TopicBridgeMiddleware


def _make_bridge(db, send_fn=None):
    tg = MagicMock()
    tg._app.bot = AsyncMock()
    router = MagicMock()
    bridge = TopicBridgeMiddleware(
        db, tg, group_chat_id=-100, router=router, agent_ids={"a1"}, send_fn=send_fn,
    )
    return bridge


@pytest.mark.asyncio
async def test_deliver_agent_reply_online_marks_delivered(db):
    """When the customer's channel accepts the message, it's stored delivered + acked ✅."""
    sent = []
    async def send_fn(channel, chat_id, text, *, media_url=None, media_type=None, filename=None):
        sent.append((channel, chat_id, text))
        return "ok"  # non-None = delivered
    bridge = _make_bridge(db, send_fn=send_fn)
    bridge._customer_channel["s1"] = "webchat"
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1")
    await db.create_ticket(t)

    delivered = await bridge._deliver_agent_reply(
        customer_chat_id="s1", customer_channel="webchat",
        text="hello there", sender_id="a1", thread_id=5,
    )

    assert delivered is True
    assert sent == [("webchat", "s1", "hello there")]
    msgs = await db.get_messages(t.id)
    assert msgs[-1].role == "agent" and msgs[-1].delivered is True
    ack = bridge.bot.send_message.call_args.kwargs["text"]
    assert "✅" in ack


@pytest.mark.asyncio
async def test_deliver_agent_reply_offline_queues_no_false_ack(db):
    """When the webchat socket is gone (send_fn → None), store delivered=False and ack ⚠️, not ✅."""
    async def send_fn(channel, chat_id, text, *, media_url=None, media_type=None, filename=None):
        return None  # delivery failed — socket not found or closed
    bridge = _make_bridge(db, send_fn=send_fn)
    bridge._customer_channel["s1"] = "webchat"
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1")
    await db.create_ticket(t)

    delivered = await bridge._deliver_agent_reply(
        customer_chat_id="s1", customer_channel="webchat",
        text="are you there?", sender_id="a1", thread_id=5,
    )

    assert delivered is False
    pending = await db.get_undelivered_agent_messages(t.id)
    assert [m.content for m in pending] == ["are you there?"]
    ack = bridge.bot.send_message.call_args.kwargs["text"]
    assert "✅" not in ack and "⚠️" in ack


@pytest.mark.asyncio
async def test_flush_redelivers_pending_on_reconnect(db):
    """When the customer reconnects, queued agent replies are sent and marked delivered."""
    sent = []
    async def send_fn(channel, chat_id, text, *, media_url=None, media_type=None, filename=None):
        sent.append(text)
        return "ok"
    bridge = _make_bridge(db, send_fn=send_fn)
    bridge._customer_channel["s1"] = "webchat"
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1")
    await db.create_ticket(t)
    await db.add_message(TicketMessage(ticket_id=t.id, role="agent", content="reply 1", delivered=False))
    await db.add_message(TicketMessage(ticket_id=t.id, role="agent", content="reply 2", delivered=False))

    n = await bridge.flush_pending_for_customer("s1")

    assert n == 2
    assert sent == ["reply 1", "reply 2"]
    assert await db.get_undelivered_agent_messages(t.id) == []


@pytest.mark.asyncio
async def test_flush_keeps_messages_when_still_offline(db):
    """If redelivery still fails, messages stay queued (not lost)."""
    async def send_fn(channel, chat_id, text, *, media_url=None, media_type=None, filename=None):
        return None
    bridge = _make_bridge(db, send_fn=send_fn)
    bridge._customer_channel["s1"] = "webchat"
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1")
    await db.create_ticket(t)
    await db.add_message(TicketMessage(ticket_id=t.id, role="agent", content="still waiting", delivered=False))

    n = await bridge.flush_pending_for_customer("s1")

    assert n == 0
    assert len(await db.get_undelivered_agent_messages(t.id)) == 1


# ---------------------------------------------------------------------------
# Agent → customer MEDIA (photo/video) forwarding + offline queue
# ---------------------------------------------------------------------------

def _media_bridge(db):
    """Bridge whose send_fn records media kwargs and is toggled online/offline."""
    state = {"online": True, "calls": []}
    async def send_fn(channel, chat_id, text, *, media_url=None, media_type=None, filename=None):
        state["calls"].append({"channel": channel, "chat_id": chat_id, "text": text,
                               "media_url": media_url, "media_type": media_type, "filename": filename})
        return "ok" if state["online"] else None
    bridge = _make_bridge(db, send_fn=send_fn)
    bridge._customer_channel["s1"] = "webchat"
    return bridge, state


@pytest.mark.asyncio
async def test_deliver_agent_media_online(db):
    """Agent media to an online webchat customer is sent with media + stored delivered."""
    bridge, state = _media_bridge(db)
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1")
    await db.create_ticket(t)

    delivered = await bridge._deliver_agent_reply(
        customer_chat_id="s1", customer_channel="webchat", text="look",
        sender_id="a1", thread_id=7,
        media_url="data:image/jpeg;base64,ZZZ", media_type="photo", filename="p.jpg",
    )

    assert delivered is True
    call = state["calls"][-1]
    assert call["media_url"] == "data:image/jpeg;base64,ZZZ" and call["media_type"] == "photo"
    m = (await db.get_messages(t.id))[-1]
    assert m.media_url == "data:image/jpeg;base64,ZZZ" and m.media_type == "photo" and m.delivered is True


@pytest.mark.asyncio
async def test_deliver_agent_media_offline_queues_with_media(db):
    """If the customer is offline, the media reply is queued (delivered=False) WITH its media."""
    bridge, state = _media_bridge(db)
    state["online"] = False
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1")
    await db.create_ticket(t)

    delivered = await bridge._deliver_agent_reply(
        customer_chat_id="s1", customer_channel="webchat", text="watch",
        sender_id="a1", thread_id=7,
        media_url="data:video/mp4;base64,VVV", media_type="video",
    )

    assert delivered is False
    pending = await db.get_undelivered_agent_messages(t.id)
    assert len(pending) == 1
    assert pending[0].media_url == "data:video/mp4;base64,VVV" and pending[0].media_type == "video"


@pytest.mark.asyncio
async def test_flush_redelivers_media_on_reconnect(db):
    """On reconnect, a queued media reply is redelivered WITH its media and marked delivered."""
    bridge, state = _media_bridge(db)
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1")
    await db.create_ticket(t)
    await db.add_message(TicketMessage(
        ticket_id=t.id, role="agent", content="cap",
        media_url="data:image/png;base64,PPP", media_type="photo", media_filename="x.png",
        delivered=False,
    ))

    n = await bridge.flush_pending_for_customer("s1")

    assert n == 1
    call = state["calls"][-1]
    assert call["media_url"] == "data:image/png;base64,PPP"
    assert call["media_type"] == "photo"
    assert call["filename"] == "x.png"
    assert await db.get_undelivered_agent_messages(t.id) == []


def test_extract_tg_media_photo_and_video():
    """The Telegram media extractor maps photo/video to (type, file_id, filename, mime)."""
    photo_msg = MagicMock(spec=["photo"])
    photo_msg.photo = [MagicMock(file_id="small"), MagicMock(file_id="big")]
    assert TopicBridgeMiddleware._extract_tg_media(photo_msg) == ("photo", "big", None, "image/jpeg")

    video_msg = MagicMock(spec=["video"])
    video_msg.video = MagicMock(file_id="vid1", file_name="clip.mp4", mime_type="video/mp4")
    assert TopicBridgeMiddleware._extract_tg_media(video_msg) == ("video", "vid1", "clip.mp4", "video/mp4")

    assert TopicBridgeMiddleware._extract_tg_media(None) == (None, None, None, None)


@pytest.mark.asyncio
async def test_voice_translation_posts_transcript_and_translation(db):
    """A customer voice note → bot posts 🎤 transcript + 🌐 translation into the topic."""
    vt = MagicMock()
    vt.transcribe_and_translate = AsyncMock(return_value=("你好，订单没收到", "Hi, order not received"))
    tg = MagicMock(); tg._app.bot = AsyncMock()
    bridge = TopicBridgeMiddleware(
        db, tg, group_chat_id=-100, router=MagicMock(), agent_ids={"a1"}, voice_translator=vt)
    msg = MagicMock()
    msg.content.media_url = "data:audio/ogg;base64,AAAA"  # tiny valid base64
    msg.raw = None
    await bridge._post_voice_translation(msg, topic_id=5)
    vt.transcribe_and_translate.assert_awaited_once()
    posted = bridge.bot.send_message.call_args.kwargs["text"]
    assert "🎤" in posted and "你好，订单没收到" in posted and "Hi, order not received" in posted


@pytest.mark.asyncio
async def test_agent_voice_sent_to_customer_as_translated_text(db):
    """Agent sends a voice note → customer receives TEXT in their language, no audio."""
    vt = MagicMock()
    vt.transcribe_and_translate = AsyncMock(return_value=("你好，已发货", "Hi, shipped"))
    sent = []
    async def send_fn(channel, chat_id, text, *, media_url=None, media_type=None, filename=None):
        sent.append({"text": text, "media_url": media_url}); return "ok"
    tg = MagicMock(); tg._app.bot = AsyncMock()
    file_mock = MagicMock(); file_mock.download_as_bytearray = AsyncMock(return_value=bytearray(b"audio"))
    tg._app.bot.get_file = AsyncMock(return_value=file_mock)
    bridge = TopicBridgeMiddleware(
        db, tg, group_chat_id=-100, router=MagicMock(),
        agent_ids={"a1"}, send_fn=send_fn, voice_translator=vt)
    bridge._customer_channel["s1"] = "webchat"
    bridge._user_lang["s1"] = "en"
    t = Ticket(channel="webchat", chat_id="s1", customer_id="u1"); await db.create_ticket(t)

    raw_msg = MagicMock(spec=["voice"]); raw_msg.voice = MagicMock(file_id="v1", mime_type="audio/ogg")
    msg = MagicMock(); msg.raw.message = raw_msg; msg.content.text = ""; msg.content.media_type = "voice"

    await bridge._forward_agent_media(msg, "s1", thread_id=7, sender_id="a1")

    vt.transcribe_and_translate.assert_awaited_once()
    assert vt.transcribe_and_translate.call_args.args[2] == "English"  # customer's language
    assert any(c["text"] == "Hi, shipped" for c in sent)              # got the translation
    assert all(c["media_url"] is None for c in sent)                  # NO audio forwarded
