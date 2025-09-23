# api/routers/logs.py
from typing import Optional
from fastapi import APIRouter, Depends, Query
import psycopg2
from db import db_conn

router = APIRouter(tags=["logs"])

@router.get("/users/{user_id}/logs",
            summary="List logs for a user",
            description=(
                "Retrieves logs for a specific user, with optional filtering by category and pagination."
            )
)
def list_logs(
    user_id: str,
    category: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    conn: psycopg2.extensions.connection = Depends(db_conn),
):
    sql = """
        SELECT id, user_id, days_ago, category
        FROM logs
        WHERE user_id = %s AND (%s IS NULL OR category = %s)
        ORDER BY days_ago ASC
        LIMIT %s OFFSET %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (user_id, category, category, limit, offset))
        rows = cur.fetchall()
        return [{"id": r[0], "user_id": r[1], "days_ago": r[2], "category": r[3]} for r in rows]

@router.get(
    "/users/{user_id}/logs/within/summary",
    summary="List log summary within days",
    description=(
        "Counts log records for a user within the last `days`.\n\n"
        "- If `category` is omitted, returns a map `{category: count}`.\n"
        "- If `category` is provided, returns a single `{category, count, days}` object."
    ),
)
def list_log_summary_within_days(
    user_id: str,
    days: int = Query(30, ge=30, le=365, description="Look back N days (days_ago ≤ N)."),
    category: Optional[str] = Query(None, description="Optional category to filter."),
    conn: psycopg2.extensions.connection = Depends(db_conn),
):
    if category:
        sql = """
            SELECT COUNT(*)::int
            FROM logs
            WHERE user_id = %s
              AND days_ago <= %s
              AND category = %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, days, category))
            row = cur.fetchone()
        return {"user_id": user_id, "days": days, "category": category, "count": (row[0] if row else 0)}

    # No category -> group by category
    sql = """
        SELECT COALESCE(category, '(uncategorized)') AS cat, COUNT(*)::int AS n
        FROM logs
        WHERE user_id = %s
          AND days_ago <= %s
        GROUP BY 1
        ORDER BY n DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (user_id, days))
        rows = cur.fetchall()

    return {
        "user_id": user_id,
        "days": days,
        "by_category": {r[0]: r[1] for r in rows},
        "total": sum(r[1] for r in rows),
    }
