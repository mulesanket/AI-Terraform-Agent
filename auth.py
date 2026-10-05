# auth.py - GitHub OAuth authentication and session management
import os
import functools
import secrets
import logging

import requests as http_requests
from flask import (
    Blueprint, request, redirect, url_for, session,
    render_template, flash, jsonify,
)
from authlib.integrations.flask_client import OAuth

import db

logger = logging.getLogger("terraform-agent.auth")

auth_bp = Blueprint("auth", __name__)
oauth = OAuth()

# ---------- OAuth provider setup ----------

def init_oauth(app):
    """Register OAuth providers with the Flask app."""
    oauth.init_app(app)

    # GitHub OAuth — primary auth method
    if app.config.get("GITHUB_CLIENT_ID"):
        oauth.register(
            name="github",
            client_id=app.config["GITHUB_CLIENT_ID"],
            client_secret=app.config["GITHUB_CLIENT_SECRET"],
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "user:email repo"},
        )


# ---------- Session-based login decorator ----------

def _github_oauth_available():
    return bool(oauth._registry.get("github"))


def login_required(f):
    """Decorator: redirect to GitHub OAuth if user not authenticated."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            # Auto-redirect to GitHub OAuth
            if _github_oauth_available():
                session["next_url"] = request.path
                return redirect(url_for("auth.oauth_login", provider="github"))
            return redirect(url_for("auth.login_page", next=request.path))
        # Load user's config into env on each request
        db.apply_user_config_to_env(session["user_id"])
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator: require admin role."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if _github_oauth_available():
                return redirect(url_for("auth.oauth_login", provider="github"))
            return redirect(url_for("auth.login_page"))
        user = db.get_user_by_id(session["user_id"])
        if not user or user["role"] != "admin":
            return jsonify({"ok": False, "error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    """Get the currently logged-in user dict, or None."""
    uid = session.get("user_id")
    if uid:
        return db.get_user_by_id(uid)
    return None


# ---------- Login page (fallback when GitHub OAuth not configured) ----------

@auth_bp.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("auth.post_login_redirect_route"))
    # If GitHub OAuth is available, redirect directly
    if _github_oauth_available():
        return redirect(url_for("auth.oauth_login", provider="github"))
    return render_template("login.html",
                           github_enabled=False,
                           google_enabled=False,
                           mode="login")


@auth_bp.route("/signup")
def signup_page():
    if "user_id" in session:
        return redirect(url_for("auth.post_login_redirect_route"))
    if _github_oauth_available():
        return redirect(url_for("auth.oauth_login", provider="github"))
    return render_template("login.html",
                           github_enabled=False,
                           google_enabled=False,
                           mode="signup")


@auth_bp.route("/login", methods=["POST"])
def login_submit():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect(url_for("auth.login_page"))

    user = db.authenticate_user(username, password)
    if not user:
        flash("Invalid username or password.", "error")
        return redirect(url_for("auth.login_page"))

    _login_user(user)
    return redirect(url_for("auth.post_login_redirect_route"))


@auth_bp.route("/signup", methods=["POST"])
def signup_submit():
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not username or not password:
        flash("Username and password are required.", "error")
        return redirect(url_for("auth.signup_page"))

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("auth.signup_page"))

    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("auth.signup_page"))

    # First user becomes admin
    role = "admin" if db.get_user_count() == 0 else "user"
    user = db.create_user(username=username, email=email or None,
                          password=password, role=role)
    if not user:
        flash("Username or email already exists.", "error")
        return redirect(url_for("auth.signup_page"))

    _login_user(user)
    flash("Account created! Please configure your settings.", "success")
    return redirect(url_for("auth.setup_page"))


# ---------- OAuth flows ----------

@auth_bp.route("/oauth/<provider>")
def oauth_login(provider):
    """Initiate OAuth flow for GitHub or Google."""
    client = oauth.create_client(provider)
    if not client:
        flash(f"{provider} OAuth is not configured.", "error")
        return redirect(url_for("auth.login_page"))
    redirect_uri = url_for("auth.oauth_callback", provider=provider, _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route("/oauth/<provider>/callback")
def oauth_callback(provider):
    """Handle OAuth callback from GitHub or Google."""
    client = oauth.create_client(provider)
    if not client:
        flash(f"{provider} OAuth is not configured.", "error")
        return redirect(url_for("auth.login_page"))

    try:
        token = client.authorize_access_token()
    except Exception as e:
        logger.error("OAuth token error for %s: %s", provider, e)
        flash("Authentication failed. Please try again.", "error")
        return redirect(url_for("auth.login_page"))

    if provider == "github":
        resp = client.get("user", token=token)
        profile = resp.json()
        oauth_id = str(profile["id"])
        username = profile.get("login", "")
        display_name = profile.get("name") or username
        avatar_url = profile.get("avatar_url", "")
        github_access_token = token.get("access_token", "")
        # Get primary email
        emails_resp = client.get("user/emails", token=token)
        emails = emails_resp.json()
        email = next((e["email"] for e in emails if e.get("primary")), None)
    else:
        flash("Unsupported OAuth provider.", "error")
        return redirect(url_for("auth.login_page"))

    # Find or create user
    user = db.get_user_by_oauth(provider, oauth_id)
    if not user:
        # Check if email matches existing user - link accounts
        if email:
            user = db.get_user_by_email(email)
        if not user:
            role = "admin" if db.get_user_count() == 0 else "user"
            # Ensure unique username
            base_username = username
            counter = 1
            while db.get_user_by_username(username):
                username = f"{base_username}_{counter}"
                counter += 1
            user = db.create_user(
                username=username,
                email=email,
                oauth_provider=provider,
                oauth_id=oauth_id,
                display_name=display_name,
                avatar_url=avatar_url,
                role=role,
            )
            # If create failed (duplicate), try to find by username
            if not user:
                user = db.get_user_by_username(base_username)
        if user and not user.get("oauth_provider"):
            # Link OAuth to existing email/username-matched user
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE users SET oauth_provider=?, oauth_id=?, avatar_url=? WHERE id=?",
                    (provider, oauth_id, avatar_url, user["id"]),
                )
            user = db.get_user_by_id(user["id"])

    if not user:
        flash("Failed to create account. Please try again.", "error")
        return redirect(url_for("auth.login_page"))

    db._update_last_login(user["id"])
    _login_user(user)

    # Auto-save GitHub token and username so GitHub integration works immediately
    if provider == "github" and github_access_token:
        existing_config = db.get_user_config(user["id"])
        auto_config = {"GITHUB_PAT": github_access_token}
        # Don't overwrite GITHUB_REPO if user already set one
        if not existing_config.get("GITHUB_REPO"):
            auto_config["GITHUB_REPO"] = ""  # will be picked on setup
        db.save_user_config(user["id"], auto_config)
        # Store token in session for fetching repos on setup page
        session["github_token"] = github_access_token

    if not user.get("is_configured"):
        return redirect(url_for("auth.setup_page"))
    # Check for stored next_url
    next_url = session.pop("next_url", "/ui")
    return redirect(next_url)


# ---------- Setup / Onboarding ----------

@auth_bp.route("/setup")
@login_required
def setup_page():
    user = get_current_user()
    config = db.get_user_config(user["id"])
    # Fetch user's GitHub repos if we have a token
    github_repos = []
    gh_token = session.get("github_token") or config.get("GITHUB_PAT")
    if gh_token:
        try:
            github_repos = _fetch_github_repos(gh_token)
        except Exception as e:
            logger.warning("Failed to fetch GitHub repos: %s", e)
    return render_template("setup.html", user=user, config=config,
                           github_repos=github_repos, active="setup")


@auth_bp.route("/setup", methods=["POST"])
@login_required
def setup_submit():
    user = get_current_user()
    config = {}
    clearable_keys = {
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
        "TF_STATE_BUCKET",
        "TF_STATE_KEY",
        "TF_STATE_REGION",
        "GITHUB_PAT",
        "GITHUB_REPO",
        "SLACK_WEBHOOK_URL",
        "COST_THRESHOLD",
        "APPROVAL_MODE",
        "DRIFT_CHECK_INTERVAL_MINUTES",
    }
    for key in db.USER_CONFIG_KEYS:
        raw_val = request.form.get(key)
        if raw_val is None:
            continue
        val = raw_val.strip()
        if val or key in clearable_keys:
            config[key] = val

    if not config.get("AGENT_WORKDIR"):
        flash("Workspace directory is required.", "error")
        return redirect(url_for("auth.setup_page"))

    db.save_user_config(user["id"], config)
    db.mark_user_configured(user["id"])
    db.apply_user_config_to_env(user["id"])
    try:
        from routes.helpers import sync_tf_state_github_variables
        from config import Config
        token = config.get("GITHUB_PAT") or Config.GITHUB_PAT
        sync_tf_state_github_variables(token, config.get("GITHUB_REPO") or Config.GITHUB_REPO)
    except Exception as exc:
        logger.warning("Could not sync TF_STATE_* to GitHub Actions variables: %s", exc)

    flash("Configuration saved successfully!", "success")
    return redirect("/ui")


# ---------- Logout ----------

@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.logged_out_page"))


@auth_bp.route("/logged-out")
def logged_out_page():
    """Show a logged-out confirmation page instead of auto-redirecting to OAuth."""
    github_enabled = _github_oauth_available()
    return render_template("login.html",
                           github_enabled=github_enabled,
                           logged_out=True,
                           mode="login")


# ---------- Admin routes ----------

@auth_bp.route("/admin")
@login_required
@admin_required
def admin_page():
    users = db.list_users()
    return render_template("admin.html", users=users, active="admin")


@auth_bp.route("/admin/api/users")
@login_required
@admin_required
def admin_list_users():
    users = db.list_users()
    return jsonify({"ok": True, "data": users})


@auth_bp.route("/admin/api/users/<int:user_id>/role", methods=["POST"])
@login_required
@admin_required
def admin_update_role(user_id):
    body = request.json or {}
    role = body.get("role")
    if role not in ("admin", "user"):
        return jsonify({"ok": False, "error": "Invalid role"}), 400
    # Prevent self-demotion
    if user_id == session.get("user_id"):
        return jsonify({"ok": False, "error": "Cannot change your own role"}), 400
    db.update_user_role(user_id, role)
    return jsonify({"ok": True, "data": {"user_id": user_id, "role": role}})


@auth_bp.route("/admin/api/users/<int:user_id>", methods=["DELETE"])
@login_required
@admin_required
def admin_delete_user(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"ok": False, "error": "Cannot delete yourself"}), 400
    db.delete_user(user_id)
    return jsonify({"ok": True})


# ---------- Internal helpers ----------

def _login_user(user):
    """Set session for a logged-in user."""
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["display_name"] = user.get("display_name") or user["username"]
    session["role"] = user["role"]
    session["avatar_url"] = user.get("avatar_url") or ""


@auth_bp.route("/post-login")
@login_required
def post_login_redirect_route():
    """Redirect after login: setup if not configured, otherwise dashboard."""
    user = get_current_user()
    if user and not user.get("is_configured"):
        return redirect(url_for("auth.setup_page"))
    next_url = session.pop("next_url", request.args.get("next", "/ui"))
    return redirect(next_url)


# ---------- GitHub repos API ----------

def _fetch_github_repos(token, per_page=100):
    """Fetch the authenticated user's repos from GitHub API."""
    repos = []
    url = "https://api.github.com/user/repos"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    params = {"sort": "updated", "per_page": per_page, "type": "all"}
    resp = http_requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    for r in resp.json():
        repos.append({
            "full_name": r["full_name"],
            "private": r.get("private", False),
            "description": r.get("description") or "",
            "default_branch": r.get("default_branch", "main"),
        })
    return repos


@auth_bp.route("/api/github_repos")
@login_required
def api_github_repos():
    """Return the user's GitHub repos for the setup/settings dropdown."""
    user = get_current_user()
    config = db.get_user_config(user["id"])
    token = session.get("github_token") or config.get("GITHUB_PAT")
    if not token:
        return jsonify({"ok": False, "error": "No GitHub token available"}), 400
    try:
        repos = _fetch_github_repos(token)
        return jsonify({"ok": True, "data": repos})
    except Exception as e:
        logger.error("Failed to fetch repos: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
