# audit.py - Audit trail for all significant agent operations
"""
Logs every generate, deploy, heal, modify, and destroy action
with user context, timestamps, and outcome details.
"""
import os
import json
import logging
import sqlite3
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger("terraform-agent.audit")

AUDIT_DB_PATH = os.environ.get(
    "AUDIT_DB_PATH",
    os.path.join(os.path.dirname(__file__), "agent_users.db"),
)


@contextmanager
def _get_db():
    conn = sqlite3.connect(AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_audit_table():
    """Create the audit_log table if it doesn't exist."""
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                user_id INTEGER,
                username TEXT,
                action TEXT NOT NULL,
                target TEXT,
                outcome TEXT NOT NULL DEFAULT 'success',
                details TEXT,
                ip_address TEXT,
                duration_ms INTEGER
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_log(timestamp DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_action
                ON audit_log(action)
        """)
    logger.info("Audit table initialized")


# ---------- Action types ----------
ACTION_GENERATE = "generate"
ACTION_DEPLOY = "deploy"
ACTION_DESTROY = "destroy"
ACTION_MODIFY = "modify"
ACTION_HEAL = "heal"
ACTION_DRIFT_SCAN = "drift_scan"
ACTION_DRIFT_FIX = "drift_fix"
ACTION_PR_CREATE = "pr_create"
ACTION_PR_MERGE = "pr_merge"
ACTION_SETTINGS_CHANGE = "settings_change"
ACTION_LOGIN = "login"
ACTION_CHECKOV_SCAN = "checkov_scan"
ACTION_COST_ESTIMATE = "cost_estimate"

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_PARTIAL = "partial"


def log_action(action, target=None, outcome="success", details=None,
               user_id=None, username=None, ip_address=None, duration_ms=None):
    """Record an audit event.

    Args:
        action: One of the ACTION_* constants (e.g. "generate", "deploy").
        target: What was acted upon (e.g. prompt text, file path, PR URL).
        outcome: "success", "failure", or "partial".
        details: Dict or string with additional context (errors, plan summary, etc.).
        user_id: Numeric user ID from session (optional).
        username: Display name or login (optional).
        ip_address: Client IP (optional).
        duration_ms: How long the action took in milliseconds (optional).
    """
    details_str = None
    if details is not None:
        if isinstance(details, dict):
            details_str = json.dumps(details, default=str)
        else:
            details_str = str(details)[:5000]  # cap at 5KB

    # Truncate target to reasonable length
    if target and len(target) > 1000:
        target = target[:1000]

    try:
        with _get_db() as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (user_id, username, action, target, outcome, details, ip_address, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, username, action, target, outcome, details_str, ip_address, duration_ms),
            )
    except Exception as e:
        logger.error("Failed to write audit log: %s", e)


def get_audit_log(limit=100, offset=0, action=None, user_id=None, since=None):
    """Query the audit log with optional filters.

    Args:
        limit: Max entries to return (default 100).
        offset: Pagination offset.
        action: Filter by action type.
        user_id: Filter by user.
        since: ISO timestamp — only return entries after this time.

    Returns:
        List of audit log dicts.
    """
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []

    if action:
        query += " AND action = ?"
        params.append(action)
    if user_id:
        query += " AND user_id = ?"
        params.append(user_id)
    if since:
        query += " AND timestamp >= ?"
        params.append(since)

    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    try:
        with _get_db() as conn:
            rows = conn.execute(query, params).fetchall()
            results = []
            for row in rows:
                entry = dict(row)
                # Parse details JSON if present
                if entry.get("details"):
                    try:
                        entry["details"] = json.loads(entry["details"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                results.append(entry)
            return results
    except Exception as e:
        logger.error("Failed to read audit log: %s", e)
        return []


def get_audit_stats(days=30):
    """Get summary statistics for the audit log.

    Returns:
        Dict with action counts and outcomes over the specified period.
    """
    try:
        with _get_db() as conn:
            rows = conn.execute(
                """SELECT action, outcome, COUNT(*) as count
                   FROM audit_log
                   WHERE timestamp >= datetime('now', ?)
                   GROUP BY action, outcome
                   ORDER BY count DESC""",
                (f"-{days} days",),
            ).fetchall()

            stats = {}
            for row in rows:
                action = row["action"]
                if action not in stats:
                    stats[action] = {"total": 0, "success": 0, "failure": 0}
                stats[action]["total"] += row["count"]
                stats[action][row["outcome"]] = stats[action].get(row["outcome"], 0) + row["count"]

            total_row = conn.execute(
                "SELECT COUNT(*) as total FROM audit_log WHERE timestamp >= datetime('now', ?)",
                (f"-{days} days",),
            ).fetchone()

            return {
                "period_days": days,
                "total_actions": total_row["total"] if total_row else 0,
                "by_action": stats,
            }
    except Exception as e:
        logger.error("Failed to get audit stats: %s", e)
        return {"period_days": days, "total_actions": 0, "by_action": {}}


def clear_audit_log(before=None):
    """Clear audit log entries. If before is specified (ISO timestamp),
    only delete entries older than that date."""
    try:
        with _get_db() as conn:
            if before:
                conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (before,))
            else:
                conn.execute("DELETE FROM audit_log")
        logger.info("Audit log cleared (before=%s)", before or "all")
    except Exception as e:
        logger.error("Failed to clear audit log: %s", e)
