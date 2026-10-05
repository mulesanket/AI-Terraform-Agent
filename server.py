#!/usr/bin/env python3
# server.py - Self-Healing Terraform Agent HTTP Server
# Routes are in the routes/ package (Flask blueprints).
import os
import logging

from dotenv import load_dotenv
load_dotenv()

# boto3 crashes if AWS_PROFILE is set to an empty string in the environment.
if not os.environ.get("AWS_PROFILE"):
    os.environ.pop("AWS_PROFILE", None)

from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("terraform-agent")

# ---------- App ----------
app = Flask(__name__)

# Session / auth configuration
from config import Config
app.secret_key = Config.SECRET_KEY
app.config["GITHUB_CLIENT_ID"] = Config.GITHUB_CLIENT_ID
app.config["GITHUB_CLIENT_SECRET"] = Config.GITHUB_CLIENT_SECRET
app.config["GOOGLE_CLIENT_ID"] = Config.GOOGLE_CLIENT_ID
app.config["GOOGLE_CLIENT_SECRET"] = Config.GOOGLE_CLIENT_SECRET
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=7)

# Initialize database and auth
import db as user_db
user_db.init_db()

# Initialize audit trail
import audit
audit.init_audit_table()

# Initialize approval workflow
import approval
approval.init_approval_table()

from auth import auth_bp, init_oauth
init_oauth(app)
app.register_blueprint(auth_bp, url_prefix="/auth")

# Rate limiter
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["60 per minute"])

# ---------- Register route blueprints ----------
from routes import register_all
register_all(app)

# Apply rate limits to specific blueprint endpoints
limiter.limit("10 per minute")(app.view_functions["generate.generate_infra"])
limiter.limit("5 per minute")(app.view_functions["generate.generate_apply"])
limiter.limit("3 per minute")(app.view_functions["generate.generate_destroy"])
limiter.limit("5 per minute")(app.view_functions["generate.generate_create_pr"])
limiter.limit("5 per minute")(app.view_functions["modify.infra_modify"])
limiter.limit("5 per minute")(app.view_functions["modify.infra_modify_apply"])
limiter.limit("3 per minute")(app.view_functions["modify.infra_modify_merge_and_deploy"])
limiter.limit("5 per minute")(app.view_functions["drift.heal"])
limiter.limit("5 per minute")(app.view_functions["github_ops.github_create_drift_pr"])

# ---------- Run server ----------
if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", 8080))
    host = os.environ.get("SERVER_HOST", "0.0.0.0")
    app.run(host=host, port=port)
