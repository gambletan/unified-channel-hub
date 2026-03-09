"""Analytics — response times, resolution rates, CSAT scores."""

from __future__ import annotations

from ..db import Database


class Analytics:
    """Compute support metrics from analytics events."""

    def __init__(self, db: Database):
        self.db = db

    async def summary(self) -> dict:
        """Get a summary of key metrics."""
        db = self.db.db

        # Ticket counts by status
        async with db.execute(
            "SELECT status, COUNT(*) as cnt FROM tickets GROUP BY status"
        ) as cur:
            status_counts = {r["status"]: r["cnt"] for r in await cur.fetchall()}

        # Average first response time
        async with db.execute(
            """SELECT AVG(value_ms) as avg_ms FROM analytics_events
               WHERE event_type = 'first_response'"""
        ) as cur:
            row = await cur.fetchone()
            avg_first_response_ms = row["avg_ms"] if row and row["avg_ms"] else 0

        # Escalation rate
        total = sum(status_counts.values()) or 1
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM analytics_events WHERE event_type = 'escalated'"
        ) as cur:
            row = await cur.fetchone()
            escalation_count = row["cnt"] if row else 0

        # Average CSAT
        async with db.execute(
            "SELECT AVG(rating) as avg_rating, COUNT(*) as cnt FROM satisfaction_ratings"
        ) as cur:
            row = await cur.fetchone()
            avg_csat = round(row["avg_rating"], 1) if row and row["avg_rating"] else None
            csat_count = row["cnt"] if row else 0

        return {
            "tickets": status_counts,
            "total_tickets": total,
            "avg_first_response_ms": round(avg_first_response_ms),
            "escalation_rate": round(escalation_count / total * 100, 1),
            "avg_csat": avg_csat,
            "csat_responses": csat_count,
        }
