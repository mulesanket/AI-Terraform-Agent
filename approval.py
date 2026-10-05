# approval.py - Plan approval workflow for production safety
"""
Implements a gated approval system: when APPROVAL_MODE="approval",
terraform apply requires an explicit approval before running.
Plans are stored with a pending state, and must be approved via the UI/API.
"""
import os
import json
import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger("terraform-agent.approval")

APPROVAL_DB_PATH = os.environ.get(
    "APPROVAL_DB_PATH",
    os.path.join(os.path.dirname(__file__), "agent_users.db"),
)


@contextmanager
def _get_db():
    conn = sqlite3.connect(APPROVAL_DB_PATH)
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


def init_approval_table():
    """Create the pending_plans table if it doesn't exist."""
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                workdir TEXT NOT NULL,
                plan_file TEXT NOT NULL,
                plan_summary TEXT,
                prompt TEXT,
                requested_by TEXT,
                requested_by_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                approved_by TEXT,
                approved_at TEXT,
                rejected_reason TEXT,
                applied_at TEXT,
                apply_rc INTEGER,
                apply_output TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_status
                ON pending_plans(status)
        """)
    logger.info("Approval table initialized")


# Status constants
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_EXPIRED = "expired"


def create_pending_plan(workdir, plan_file, plan_summary=None,
                        prompt=None, requested_by=None, requested_by_id=None):
    """Create a new pending plan awaiting approval.

    Returns:
        plan_id (int) for tracking.
    """
    with _get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO pending_plans
               (workdir, plan_file, plan_summary, prompt, requested_by, requested_by_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (workdir, plan_file, plan_summary, prompt, requested_by, requested_by_id, STATUS_PENDING),
        )
        plan_id = cursor.lastrowid
    logger.info("Pending plan #%d created (workdir=%s, by=%s)", plan_id, workdir, requested_by)
    return plan_id


def approve_plan(plan_id, approved_by=None):
    """Approve a pending plan.

    Returns:
        True if approved, False if not found or already processed.
    """
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM pending_plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            return False
        if row["status"] != STATUS_PENDING:
            logger.warning("Plan #%d cannot be approved — status is '%s'", plan_id, row["status"])
            return False
        conn.execute(
            "UPDATE pending_plans SET status = ?, approved_by = ?, approved_at = datetime('now') WHERE id = ?",
            (STATUS_APPROVED, approved_by, plan_id),
        )
    logger.info("Plan #%d approved by %s", plan_id, approved_by)
    return True


def reject_plan(plan_id, rejected_by=None, reason=None):
    """Reject a pending plan.

    Returns:
        True if rejected, False if not found or already processed.
    """
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM pending_plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            return False
        if row["status"] != STATUS_PENDING:
            return False
        conn.execute(
            "UPDATE pending_plans SET status = ?, approved_by = ?, rejected_reason = ?, approved_at = datetime('now') WHERE id = ?",
            (STATUS_REJECTED, rejected_by, reason, plan_id),
        )
    logger.info("Plan #%d rejected by %s: %s", plan_id, rejected_by, reason)
    return True


def mark_applied(plan_id, rc, output=None):
    """Mark an approved plan as applied."""
    with _get_db() as conn:
        conn.execute(
            "UPDATE pending_plans SET status = ?, applied_at = datetime('now'), apply_rc = ?, apply_output = ? WHERE id = ?",
            (STATUS_APPLIED, rc, (output or "")[:5000], plan_id),
        )
    logger.info("Plan #%d marked as applied (rc=%d)", plan_id, rc)


def get_plan(plan_id):
    """Get a single plan by ID."""
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM pending_plans WHERE id = ?", (plan_id,)).fetchone()
        return dict(row) if row else None


def list_pending_plans(limit=50):
    """Get all pending plans."""
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_plans WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (STATUS_PENDING, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_plans(status=None, limit=50, offset=0):
    """Get plans with optional status filter."""
    with _get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM pending_plans WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pending_plans ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def is_approval_required():
    """Check if the current config requires plan approval before apply."""
    from config import Config
    return Config.APPROVAL_MODE == "approval"
