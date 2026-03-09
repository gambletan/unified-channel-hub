"""Tests for the AI router."""

import pytest
from support.ai.router import AIRouter, ESCALATION_PHRASES


class FakeLLM:
    async def complete(self, messages, system_prompt="", temperature=0.3, max_tokens=1024):
        return "I can help you with that!"


class FakeKB:
    async def search(self, query, top_k=3):
        return []

    def format_context(self, articles):
        return ""


@pytest.fixture
def router():
    return AIRouter(llm=FakeLLM(), kb=FakeKB())


def test_escalation_phrases():
    router = AIRouter(llm=FakeLLM(), kb=FakeKB())
    assert router.should_escalate("I want to talk to a human")
    assert router.should_escalate("转人工")
    assert router.should_escalate("找客服")
    assert not router.should_escalate("How much does it cost?")


def test_escalation_by_turn_count():
    router = AIRouter(llm=FakeLLM(), kb=FakeKB(), max_ai_turns=5)
    assert not router.should_escalate("hi", ai_turn_count=3)
    assert router.should_escalate("hi", ai_turn_count=5)
    assert router.should_escalate("hi", ai_turn_count=10)


@pytest.mark.asyncio
async def test_generate_reply(router):
    reply = await router.generate_reply("What is your return policy?")
    assert reply == "I can help you with that!"


@pytest.mark.asyncio
async def test_generate_reply_with_history(router):
    history = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ]
    reply = await router.generate_reply("What about pricing?", history)
    assert isinstance(reply, str)
