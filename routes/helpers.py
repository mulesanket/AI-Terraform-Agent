# routes/helpers.py - Shared utilities for all route blueprints
import os
import re
import json
import stat
import time
import shutil
import logging
import functools

from flask import request, jsonify, session
from flask_limiter import Limiter

from config import Config
from utils import safe_write_file, safe_read_file, run_cmd_capture

logger = logging.getLogger("terraform-agent")


def _on_rm_error(_func, path, _exc_info):
    """Handle read-only files (e.g. .git pack objects) on Windows."""
    os.chmod(path, stat.S_IWRITE)
    os.unlink(path)


def force_rmtree(path):
    """shutil.rmtree that clears read-only flags on Windows before deleting."""
    shutil.rmtree(path, onerror=_on_rm_error)


# ---------- Recursive file helpers ----------

def find_tf_files_recursive(base_path, rel_prefix=""):
    """Recursively find all .tf files in a directory, returning relative paths."""
    tf_files = []
    try:
        for fname in os.listdir(base_path):
            fpath = os.path.join(base_path, fname)
            rel_path = os.path.join(rel_prefix, fname) if rel_prefix else fname
            if os.path.isfile(fpath) and fname.endswith(".tf"):
                tf_files.append(rel_path)
            elif os.path.isdir(fpath):
                tf_files.extend(find_tf_files_recursive(fpath, rel_path))
    except Exception:
        pass
    return tf_files


# ---------- Shared state ----------
# AGENT_WORKDIR is mutable (can change per-repo); access via get_workdir()
_workdir = os.environ.get("AGENT_WORKDIR", os.path.join(os.getcwd(), "workspace"))


def get_workdir():
    return _workdir


def set_workdir(path):
    global _workdir
    _workdir = path


def resolve_workdir_for_repo(repo_name):
    """Derive a per-repo workdir under <AGENT_WORKDIR>/<repo-name>/ and update global."""
    if not repo_name:
        return get_workdir()
    repo_basename = repo_name.strip().rstrip("/").split("/")[-1]
    if repo_basename.endswith(".git"):
        repo_basename = repo_basename[:-4]
    base = os.environ.get("AGENT_WORKDIR", os.path.join(os.getcwd(), "workspace"))
    new_workdir = os.path.join(base, repo_basename)
    os.makedirs(new_workdir, exist_ok=True)
    set_workdir(new_workdir)
    return new_workdir


# ---------- Response helpers ----------

def respond_ok(data):
    return jsonify({"ok": True, "data": data})


def respond_err(msg, code=500):
    return jsonify({"ok": False, "error": msg}), code


# ---------- Auth decorator ----------

def require_api_key(f):
    """Allow access via API key (Bearer token) OR active session."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" in session:
            return f(*args, **kwargs)
        api_key = os.environ.get("API_KEY")
        if api_key:
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            if token != api_key:
                logger.warning("Unauthorized request to %s", request.path)
                return respond_err("Unauthorized", 401)
        return f(*args, **kwargs)
    return decorated


# ---------- Text helpers ----------

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def strip_ansi(text):
    """Remove ANSI color/format escape codes from terminal output."""
    return ANSI_ESCAPE.sub('', text) if text else text


def normalize_repo(name):
    """Convert a full GitHub URL to owner/repo format if needed."""
    if not name:
        return name
    name = name.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name.endswith(".git"):
        name = name[:-4]
    return name


def find_open_pr(repo, branch_prefix):
    """Find an existing open PR whose head branch starts with the given prefix."""
    try:
        open_prs = repo.get_pulls(state="open", sort="created", direction="desc")
        for pr in open_prs:
            if pr.head.ref.startswith(branch_prefix):
                logger.info("Found existing open PR #%d on branch %s", pr.number, pr.head.ref)
                return pr, pr.head.ref
    except Exception as e:
        logger.warning("Could not search for existing PRs: %s", e)
    return None, None


def get_tf_state_settings():
    """Return S3 state bucket/key/region from env or Config."""
    bucket = (
        os.environ.get("TF_STATE_BUCKET")
        or getattr(Config, "TF_STATE_BUCKET", "")
        or ""
    ).strip()
    key = (
        os.environ.get("TF_STATE_KEY")
        or getattr(Config, "TF_STATE_KEY", "")
        or ""
    ).strip()
    region = (
        os.environ.get("TF_STATE_REGION")
        or getattr(Config, "TF_STATE_REGION", "")
        or "us-east-1"
    ).strip()
    repo = normalize_repo(
        os.environ.get("GITHUB_REPO") or getattr(Config, "GITHUB_REPO", "") or ""
    )
    if not key:
        key = f"{repo}/terraform.tfstate" if repo else "terraform.tfstate"
    return bucket, key, region


def render_s3_backend_tf(bucket, key, region):
    return (
        "# Managed by AI Terraform Agent — Terraform state in S3\n"
        "terraform {\n"
        '  backend "s3" {\n'
        f'    bucket  = "{bucket}"\n'
        f'    key     = "{key}"\n'
        f'    region  = "{region}"\n'
        "    encrypt = true\n"
        "  }\n"
        "}\n"
    )


def write_s3_backend_tf(directory):
    """Write backend.tf from Settings. Returns filename or None if bucket is unset."""
    bucket, key, region = get_tf_state_settings()
    path = os.path.join(directory, "backend.tf")
    if not bucket:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    current = handle.read()
                if "Managed by AI Terraform Agent" in current:
                    os.remove(path)
            except OSError:
                pass
        return None
    safe_write_file(path, render_s3_backend_tf(bucket, key, region), base_dir=directory)
    logger.info("Wrote backend.tf bucket=%s key=%s region=%s", bucket, key, region)
    return "backend.tf"


def upsert_github_actions_variable(token, repo_name, name, value):
    """Create or update a GitHub Actions repository variable (used by the deploy workflow)."""
    import requests as http_requests

    repo_name = normalize_repo(repo_name)
    value = (value or "").strip()
    if not token or not repo_name or not name or not value:
        return False
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    patch_url = f"https://api.github.com/repos/{repo_name}/actions/variables/{name}"
    resp = http_requests.patch(patch_url, headers=headers, json={"name": name, "value": value}, timeout=20)
    if resp.status_code == 404:
        resp = http_requests.post(
            f"https://api.github.com/repos/{repo_name}/actions/variables",
            headers=headers,
            json={"name": name, "value": value},
            timeout=20,
        )
    if resp.status_code >= 400:
        logger.warning(
            "Could not set GitHub Actions variable %s on %s: %s %s",
            name, repo_name, resp.status_code, resp.text[:300],
        )
        return False
    logger.info("Set GitHub Actions variable %s on %s", name, repo_name)
    return True


def sync_tf_state_github_variables(token, repo_name=None):
    """Push TF_STATE_BUCKET / key / region to the target repo Actions variables."""
    bucket, key, region = get_tf_state_settings()
    repo_name = normalize_repo(repo_name or os.environ.get("GITHUB_REPO") or getattr(Config, "GITHUB_REPO", "") or "")
    if not bucket or not repo_name:
        return
    upsert_github_actions_variable(token, repo_name, "TF_STATE_BUCKET", bucket)
    upsert_github_actions_variable(token, repo_name, "TF_STATE_KEY", key)
    upsert_github_actions_variable(token, repo_name, "TF_STATE_REGION", region)
    upsert_github_actions_variable(token, repo_name, "AWS_REGION", region)


# ---------- Audit logging helper ----------

def audit_action(action, target=None, outcome="success", details=None):
    """Log an audit event with current request context."""
    import audit
    user_id = session.get("user_id")
    username = session.get("display_name") or session.get("username")
    ip_address = request.remote_addr if request else None
    audit.log_action(
        action=action,
        target=target,
        outcome=outcome,
        details=details,
        user_id=user_id,
        username=username,
        ip_address=ip_address,
    )

