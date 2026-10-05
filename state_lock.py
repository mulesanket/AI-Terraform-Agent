# state_lock.py - Terraform state locking to prevent concurrent operations
"""
File-based state locking that prevents concurrent terraform operations
on the same working directory. Uses an atomic lock file with timeout.
"""
import os
import json
import time
import logging
import contextlib
from datetime import datetime

logger = logging.getLogger("terraform-agent.lock")

LOCK_TIMEOUT_SECONDS = int(os.environ.get("TF_LOCK_TIMEOUT", 300))  # 5 min default
LOCK_FILENAME = ".terraform.agent.lock"


class StateLockError(Exception):
    """Raised when a state lock cannot be acquired."""
    def __init__(self, message, lock_info=None):
        super().__init__(message)
        self.lock_info = lock_info


def _lock_path(workdir):
    return os.path.join(workdir, LOCK_FILENAME)


def _read_lock(workdir):
    """Read current lock file. Returns dict or None if no lock."""
    path = _lock_path(workdir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _write_lock(workdir, lock_info):
    """Write lock file atomically."""
    path = _lock_path(workdir)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(lock_info, f, indent=2)
    # Atomic rename (on Windows this may fail if target exists, so remove first)
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp_path, path)


def _remove_lock(workdir):
    """Remove lock file."""
    path = _lock_path(workdir)
    try:
        os.remove(path)
    except OSError:
        pass


def acquire_lock(workdir, operation, user=None, force=False):
    """Acquire a state lock for the given workdir.

    Args:
        workdir: The terraform working directory to lock.
        operation: What operation is being performed (e.g. "terraform_apply").
        user: Who is performing the operation (optional).
        force: If True, break an existing lock (for admin use).

    Returns:
        lock_id (str) to pass to release_lock.

    Raises:
        StateLockError: If the lock is already held and not expired.
    """
    existing = _read_lock(workdir)

    if existing and not force:
        # Check if lock has expired
        created_at = existing.get("created_at", "")
        try:
            lock_time = datetime.fromisoformat(created_at)
            elapsed = (datetime.now() - lock_time).total_seconds()
            if elapsed < LOCK_TIMEOUT_SECONDS:
                raise StateLockError(
                    f"State is locked by '{existing.get('user', 'unknown')}' "
                    f"for operation '{existing.get('operation', '?')}' "
                    f"(started {int(elapsed)}s ago, timeout={LOCK_TIMEOUT_SECONDS}s)",
                    lock_info=existing,
                )
            else:
                logger.warning(
                    "Breaking expired lock (held %ds > timeout %ds): %s",
                    int(elapsed), LOCK_TIMEOUT_SECONDS, existing,
                )
        except (ValueError, TypeError):
            # Invalid timestamp — break the lock
            logger.warning("Breaking lock with invalid timestamp: %s", existing)

    lock_id = f"{int(time.time() * 1000)}-{os.getpid()}"
    lock_info = {
        "lock_id": lock_id,
        "operation": operation,
        "user": user or "api",
        "created_at": datetime.now().isoformat(),
        "pid": os.getpid(),
        "workdir": workdir,
    }

    _write_lock(workdir, lock_info)
    logger.info("Lock acquired: %s (operation=%s, user=%s)", lock_id, operation, user)
    return lock_id


def release_lock(workdir, lock_id=None):
    """Release a state lock.

    Args:
        workdir: The terraform working directory.
        lock_id: The lock_id returned by acquire_lock. If provided, only
                 release if the current lock matches (prevents releasing
                 someone else's lock).
    """
    if lock_id:
        existing = _read_lock(workdir)
        if existing and existing.get("lock_id") != lock_id:
            logger.warning(
                "Not releasing lock — lock_id mismatch (ours=%s, current=%s)",
                lock_id, existing.get("lock_id"),
            )
            return False

    _remove_lock(workdir)
    logger.info("Lock released: %s", lock_id or "(forced)")
    return True


def get_lock_status(workdir):
    """Get current lock status for a workdir.

    Returns:
        Dict with lock info or {"locked": False}.
    """
    existing = _read_lock(workdir)
    if not existing:
        return {"locked": False}

    created_at = existing.get("created_at", "")
    try:
        lock_time = datetime.fromisoformat(created_at)
        elapsed = (datetime.now() - lock_time).total_seconds()
        expired = elapsed >= LOCK_TIMEOUT_SECONDS
    except (ValueError, TypeError):
        elapsed = 0
        expired = True

    return {
        "locked": True,
        "expired": expired,
        "elapsed_seconds": int(elapsed),
        "timeout_seconds": LOCK_TIMEOUT_SECONDS,
        **existing,
    }


@contextlib.contextmanager
def state_lock(workdir, operation, user=None):
    """Context manager for state locking.

    Usage:
        with state_lock(workdir, "terraform_apply", user="ajay"):
            # run terraform apply
            pass

    Raises:
        StateLockError if the lock cannot be acquired.
    """
    lock_id = acquire_lock(workdir, operation, user=user)
    try:
        yield lock_id
    finally:
        release_lock(workdir, lock_id)
