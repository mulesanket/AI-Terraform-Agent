# routes/__init__.py - Blueprint registration
"""
Flask Blueprints for the Self-Healing Terraform Agent.

Blueprint structure:
  routes/terraform.py  — terraform init/plan/validate/apply/destroy
  routes/generate.py   — /generate, /generate/apply, /generate/destroy, /generate/pr
  routes/modify.py     — /infra/modify/*
  routes/github_ops.py — /github/*, /git_*
  routes/drift.py      — /identify_drift, /heal, /drift/audit_log, /scheduler/*
  routes/security.py   — /run_checkov, /cost_estimate, /run_opa_check
  routes/ui.py         — /ui/*, template renders, settings API, dashboard
  routes/files.py      — /read_file, /write_file
"""

from .terraform import terraform_bp
from .generate import generate_bp
from .modify import modify_bp
from .github_ops import github_bp
from .drift import drift_bp
from .security import security_bp
from .ui import ui_bp
from .files import files_bp

ALL_BLUEPRINTS = [
    (terraform_bp, None),
    (generate_bp, None),
    (modify_bp, None),
    (github_bp, None),
    (drift_bp, None),
    (security_bp, None),
    (ui_bp, None),
    (files_bp, None),
]


def register_all(app):
    """Register all route blueprints with the Flask app."""
    for bp, prefix in ALL_BLUEPRINTS:
        app.register_blueprint(bp, url_prefix=prefix)
