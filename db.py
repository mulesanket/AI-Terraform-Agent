# db.py - SQLite database for multi-user authentication and per-user configuration
import os
import sqlite3
import hashlib
import secrets
import logging
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger("terraform-agent.db")

DB_PATH = os.environ.get("AUTH_DB_PATH", os.path.join(os.path.dirname(__file__), "agent_users.db"))


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE,
                password_hash TEXT,
                oauth_provider TEXT,
                oauth_id TEXT,
                display_name TEXT,
                avatar_url TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                is_configured INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_login TEXT
            );

            CREATE TABLE IF NOT EXISTS user_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                config_key TEXT NOT NULL,
                config_value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, config_key)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_oauth
                ON users(oauth_provider, oauth_id)
                WHERE oauth_provider IS NOT NULL;
        """)
    logger.info("Database initialized at %s", DB_PATH)


# ---------- Password helpers ----------

def _hash_password(password):
    """Hash password with a random salt using SHA-256."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"


def _verify_password(password, stored_hash):
    """Verify password against stored salt$hash."""
    if not stored_hash or "$" not in stored_hash:
        return False
    salt, h = stored_hash.split("$", 1)
    return hashlib.sha256((salt + password).encode()).hexdigest() == h


# ---------- User CRUD ----------

def create_user(username, email=None, password=None, oauth_provider=None,
                oauth_id=None, display_name=None, avatar_url=None, role="user"):
    """Create a new user. Returns user dict or None if duplicate."""
    pw_hash = _hash_password(password) if password else None
    with get_db() as conn:
        try:
            conn.execute(
                """INSERT INTO users
                   (username, email, password_hash, oauth_provider, oauth_id,
                    display_name, avatar_url, role)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (username, email, pw_hash, oauth_provider, oauth_id,
                 display_name or username, avatar_url, role),
            )
            return get_user_by_username(username)
        except sqlite3.IntegrityError:
            logger.warning("Duplicate user: %s", username)
            return None


def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_oauth(provider, oauth_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE oauth_provider = ? AND oauth_id = ?",
            (provider, oauth_id),
        ).fetchone()
        return dict(row) if row else None


def authenticate_user(username, password):
    """Verify username + password. Returns user dict or None."""
    user = get_user_by_username(username)
    if user and _verify_password(password, user.get("password_hash")):
        _update_last_login(user["id"])
        return user
    return None


def _update_last_login(user_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET last_login = datetime('now') WHERE id = ?",
            (user_id,),
        )


def mark_user_configured(user_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET is_configured = 1 WHERE id = ?", (user_id,),
        )


def list_users():
    """List all users (admin function)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, email, display_name, avatar_url, role, "
            "is_configured, oauth_provider, created_at, last_login FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_user_role(user_id, role):
    with get_db() as conn:
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def delete_user(user_id):
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---------- Per-user configuration ----------

# Keys that each user can configure
USER_CONFIG_KEYS = [
    "AGENT_WORKDIR",
    "LLM_PROVIDER",
    "BEDROCK_MODEL_ID",
    "BEDROCK_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_DEFAULT_REGION",
    "GITHUB_PAT",
    "GITHUB_REPO",
    "TF_STATE_BUCKET",
    "TF_STATE_KEY",
    "TF_STATE_REGION",
    "SLACK_WEBHOOK_URL",
    "COST_THRESHOLD",
    "APPROVAL_MODE",
    "DRIFT_CHECK_INTERVAL_MINUTES",
]

SECRET_CONFIG_KEYS = {
    "GITHUB_PAT",
    "SLACK_WEBHOOK_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}


def save_user_config(user_id, config_dict):
    """Save/update multiple config keys for a user."""
    with get_db() as conn:
        for key, value in config_dict.items():
            if key not in USER_CONFIG_KEYS:
                continue
            conn.execute(
                """INSERT INTO user_configs (user_id, config_key, config_value, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(user_id, config_key)
                   DO UPDATE SET config_value = excluded.config_value,
                                 updated_at = excluded.updated_at""",
                (user_id, key, str(value).strip()),
            )
    logger.info("Config saved for user_id=%s: %s", user_id, list(config_dict.keys()))


def get_user_config(user_id):
    """Get all config key-value pairs for a user."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT config_key, config_value FROM user_configs WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {row["config_key"]: row["config_value"] for row in rows}


def get_user_config_value(user_id, key, default=None):
    """Get a single config value for a user, falling back to default."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT config_value FROM user_configs WHERE user_id = ? AND config_key = ?",
            (user_id, key),
        ).fetchone()
        return row["config_value"] if row else default


_BEDROCK_ENV_KEYS = (
    "LLM_PROVIDER",
    "BEDROCK_MODEL_ID",
    "BEDROCK_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
)


def _bedrock_settings_from_dotenv():
    """Read Bedrock keys from .env so file edits are not overwritten by stale Settings."""
    from dotenv import dotenv_values

    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(__file__), ".env"),
    ]
    values = {}
    for path in candidates:
        if os.path.isfile(path):
            values = dotenv_values(path)
            if values:
                break
    out = {}
    for key in _BEDROCK_ENV_KEYS:
        val = (values.get(key) or "").strip()
        if val:
            out[key] = val
    return out


def _sync_bedrock_from_dotenv(user_id):
    """Persist .env Bedrock settings into user Settings when they differ."""
    env_vals = _bedrock_settings_from_dotenv()
    if not env_vals:
        return
    current = get_user_config(user_id)
    changed = {key: value for key, value in env_vals.items() if current.get(key) != value}
    if changed:
        save_user_config(user_id, changed)


def apply_user_config_to_env(user_id):
    """Load a user's config into os.environ so the rest of the app picks it up."""
    _sync_bedrock_from_dotenv(user_id)
    # Keep these as process env vars so downstream libraries can pick them up.
    _RUNTIME_ENV_KEYS = {
        "LLM_PROVIDER", "BEDROCK_MODEL_ID", "BEDROCK_REGION",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_DEFAULT_REGION",
        "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "TF_STATE_BUCKET", "TF_STATE_KEY", "TF_STATE_REGION", "GITHUB_REPO",
    }
    _CLEARABLE_AWS_KEYS = {
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
    }
    config = get_user_config(user_id)

    # Clear stale AWS auth vars first so previous users or older values do not leak.
    for key in _CLEARABLE_AWS_KEYS:
        if not config.get(key, "").strip():
            os.environ.pop(key, None)

    for key, value in config.items():
        if value and key in _RUNTIME_ENV_KEYS:
            os.environ[key] = value
    # Reload the Config class
    from config import Config

    # Keep Config in sync when a user intentionally clears AWS_PROFILE.
    if "AWS_PROFILE" in config:
        Config.AWS_PROFILE = config.get("AWS_PROFILE", "")
    if "AWS_BEARER_TOKEN_BEDROCK" in config:
        Config.AWS_BEARER_TOKEN_BEDROCK = config.get("AWS_BEARER_TOKEN_BEDROCK", "")
    if "BEDROCK_REGION" in config:
        Config.BEDROCK_REGION = config.get("BEDROCK_REGION", "") or "eu-west-2"
    if "BEDROCK_MODEL_ID" in config:
        Config.BEDROCK_MODEL_ID = config.get("BEDROCK_MODEL_ID", "") or Config.BEDROCK_MODEL_ID
    if "LLM_PROVIDER" in config and config.get("LLM_PROVIDER", "").strip():
        Config.LLM_PROVIDER = config.get("LLM_PROVIDER", "").strip()
    for key, value in config.items():
        if not value:
            continue
        if key == "MCP_PORT":
            Config.MCP_PORT = int(value)
        elif key == "DRIFT_CHECK_INTERVAL_MINUTES":
            Config.DRIFT_CHECK_INTERVAL_MINUTES = int(value)
        elif key == "COST_THRESHOLD":
            Config.COST_THRESHOLD = float(value)
        elif hasattr(Config, key):
            setattr(Config, key, value)
    return config


def get_user_count():
    """Return total number of users."""
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"]
