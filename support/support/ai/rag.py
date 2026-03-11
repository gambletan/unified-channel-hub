"""Knowledge base indexer and search using SQLite FTS5."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database
from ..models import KBArticle

logger = logging.getLogger(__name__)

# KB search cache TTL in seconds
_KB_CACHE_TTL = 300


class KnowledgeBase:
    """Index markdown files and search them for RAG."""

    def __init__(self, db: Database, knowledge_dir: str | Path = "knowledge"):
        self.db = db
        self.knowledge_dir = Path(knowledge_dir)
        # Search result cache: normalized_query -> (results, timestamp)
        self._cache: dict[str, tuple[list[KBArticle], float]] = {}

    async def reindex(self) -> int:
        """Re-index all markdown files in the knowledge directory."""
        self._cache.clear()  # Invalidate cache on reindex
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

    def _normalize_query(self, query: str) -> str:
        """Normalize query for cache key (lowercase, sorted tokens)."""
        tokens = sorted(set(query.lower().split()))
        return " ".join(tokens)

    async def search(self, query: str, top_k: int = 3) -> list[KBArticle]:
        """Search the knowledge base with caching. Returns empty list if no match."""
        if not query.strip():
            return []

        # Check cache
        cache_key = self._normalize_query(query)
        cached = self._cache.get(cache_key)
        if cached:
            results, ts = cached
            if time.monotonic() - ts < _KB_CACHE_TTL:
                logger.debug("KB cache hit: %s (%d results)", cache_key[:30], len(results))
                return results
            del self._cache[cache_key]

        try:
            results = await self.db.search_kb(query, top_k)
        except Exception as e:
            logger.warning("KB search error: %s", e)
            return []

        # Cache results
        self._cache[cache_key] = (results, time.monotonic())

        # Evict old entries if cache too large
        if len(self._cache) > 200:
            now = time.monotonic()
            self._cache = {
                k: v for k, v in self._cache.items()
                if now - v[1] < _KB_CACHE_TTL
            }

        return results

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
