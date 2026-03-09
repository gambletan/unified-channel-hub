"""Tests for knowledge base RAG."""

import pytest
import pytest_asyncio

from support.ai.rag import KnowledgeBase
from support.db import Database


@pytest_asyncio.fixture
async def kb(tmp_path):
    # Create knowledge dir with test articles
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "pricing.md").write_text("# Pricing\nOur product costs $10/month for basic plan.\n$50/month for pro.")
    (kdir / "returns.md").write_text("# Return Policy\nYou can return items within 30 days.\nFull refund guaranteed.")
    subdir = kdir / "guides"
    subdir.mkdir()
    (subdir / "getting-started.md").write_text("# Getting Started\nStep 1: Sign up\nStep 2: Configure")

    db = Database(tmp_path / "test.db")
    await db.connect()
    kb = KnowledgeBase(db, kdir)
    yield kb
    await db.close()


@pytest.mark.asyncio
async def test_reindex(kb):
    count = await kb.reindex()
    assert count == 3


@pytest.mark.asyncio
async def test_search(kb):
    await kb.reindex()
    results = await kb.search("pricing cost")
    assert len(results) >= 1
    assert any("Pricing" in r.title for r in results)


@pytest.mark.asyncio
async def test_search_empty_query(kb):
    await kb.reindex()
    results = await kb.search("")
    assert results == []


@pytest.mark.asyncio
async def test_format_context(kb):
    await kb.reindex()
    results = await kb.search("return refund")
    context = kb.format_context(results)
    assert "Return Policy" in context


@pytest.mark.asyncio
async def test_format_context_empty(kb):
    assert kb.format_context([]) == ""


@pytest.mark.asyncio
async def test_search_no_dir(tmp_path):
    db = Database(tmp_path / "test2.db")
    await db.connect()
    kb = KnowledgeBase(db, tmp_path / "nonexistent")
    count = await kb.reindex()
    assert count == 0
    await db.close()
