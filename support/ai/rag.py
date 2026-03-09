"""Knowledge base indexer and search using SQLite FTS5."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database
from ..models import KBArticle

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """Index markdown files and search them for RAG."""

    def __init__(self, db: Database, knowledge_dir: str | Path = "knowledge"):
        self.db = db
        self.knowledge_dir = Path(knowledge_dir)

    async def reindex(self) -> int:
        """Re-index all markdown files in the knowledge directory."""
        await self.db.clear_kb()
        count = 0
        if not self.knowledge_dir.exists():
            logger.warning("Knowledge directory not found: %s", self.knowledge_dir)
            return 0

        for md_file in sorted(self.knowledge_dir.rglob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            title = md_file.stem.replace("-", " ").replace("_", " ").title()

            # Extract title from first heading if present
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("# "):
                    title = line[2:].strip()
                    break

            # Derive category from subdirectory
            rel = md_file.relative_to(self.knowledge_dir)
            category = str(rel.parent) if str(rel.parent) != "." else None

            article = KBArticle(
                title=title,
                content=content,
                category=category,
                tags=[],
                source_path=str(rel),
            )
            await self.db.index_article(article)
            count += 1
            logger.debug("Indexed: %s", rel)

        logger.info("Indexed %d knowledge base articles", count)
        return count

    async def search(self, query: str, top_k: int = 3) -> list[KBArticle]:
        """Search the knowledge base. Returns empty list if no match."""
        if not query.strip():
            return []
        try:
            return await self.db.search_kb(query, top_k)
        except Exception as e:
            logger.warning("KB search error: %s", e)
            return []

    def format_context(self, articles: list[KBArticle]) -> str:
        """Format KB articles as context for the LLM prompt."""
        if not articles:
            return ""
        parts = ["Relevant knowledge base articles:\n"]
        for i, a in enumerate(articles, 1):
            parts.append(f"[{i}] {a.title}")
            # Truncate long articles
            content = a.content[:2000] if len(a.content) > 2000 else a.content
            parts.append(content)
            parts.append("")
        return "\n".join(parts)
