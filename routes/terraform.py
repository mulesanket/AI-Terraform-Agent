# routes/terraform.py - Terraform CLI operation endpoints
import os
import json

from flask import Blueprint, request, session

from .helpers import require_api_key, respond_ok, respond_err, get_workdir, strip_ansi, logger, audit_action
from utils import run_cmd_capture
from state_lock import state_lock, StateLockError

terraform_bp = Blueprint("terraform", __name__)


def _get_user():
    return session.get("display_name") or session.get("username") or "api"


@terraform_bp.route("/terraform_init", methods=["POST"])
@require_api_key
def terraform_init():
    body = request.json or {}
    path = body.get("path", get_workdir())
    cmd = ["terraform", "init", "-input=false"]
    rc, out, err = run_cmd_capture(cmd, cwd=path)
    logger.info("terraform init in %s — rc=%d", path, rc)
    return respond_ok({"rc": rc, "stdout": strip_ansi(out), "stderr": strip_ansi(err)})


@terraform_bp.route("/terraform_plan", methods=["POST"])
@require_api_key
def terraform_plan():
    body = request.json or {}
    path = body.get("path", get_workdir())
    out_file = body.get("out_file", "plan.out")
    # Ensure terraform is initialized first
    init_rc, init_out, init_err = run_cmd_capture(
        ["terraform", "init", "-input=false"], cwd=path
    )
    if init_rc != 0:
        logger.error("terraform init failed before plan: %s", init_err)
        return respond_err(f"terraform init failed: {init_err}")
    cmd = ["terraform", "plan", "-out", out_file, "-input=false", "-detailed-exitcode"]
    rc, out, err = run_cmd_capture(cmd, cwd=path)
    logger.info("terraform plan in %s — rc=%d", path, rc)
    return respond_ok({"rc": rc, "stdout": strip_ansi(out), "stderr": strip_ansi(err), "plan_file": os.path.join(path, out_file)})


@terraform_bp.route("/terraform_show_json", methods=["POST"])
@require_api_key
def terraform_show_json():
    body = request.json or {}
    path = body.get("path", get_workdir())
    plan_file = body.get("plan_file", os.path.join(path, "plan.out"))
    cmd = ["terraform", "show", "-json", plan_file]
    rc, out, err = run_cmd_capture(cmd, cwd=path)
    if rc != 0:
        return respond_err(f"terraform show failed: {err}")
    try:
        parsed = json.loads(out)
    except Exception as e:
        return respond_err("Failed to parse plan JSON: " + str(e))
    return respond_ok({"plan_json": parsed})


@terraform_bp.route("/terraform_validate", methods=["POST"])
@require_api_key
def terraform_validate():
    body = request.json or {}
    path = body.get("path", get_workdir())
    # Init first
    run_cmd_capture(["terraform", "init", "-input=false"], cwd=path)
    cmd = ["terraform", "validate", "-json"]
    rc, out, err = run_cmd_capture(cmd, cwd=path)
    try:
        result = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        result = {"valid": rc == 0, "raw_output": out}
    return respond_ok({"rc": rc, "result": result})


@terraform_bp.route("/terraform_apply", methods=["POST"])
@require_api_key
def terraform_apply():
    body = request.json or {}
    path = body.get("path", get_workdir())
    plan_file = body.get("plan_file", "plan.out")
    cmd = ["terraform", "apply", "-auto-approve", "-input=false"]
    if os.path.exists(os.path.join(path, plan_file)):
        cmd = ["terraform", "apply", "-auto-approve", "-input=false", plan_file]
    try:
        with state_lock(path, "terraform_apply", user=_get_user()):
            rc, out, err = run_cmd_capture(cmd, cwd=path, timeout=300)
    except StateLockError as e:
        return respond_err(f"State locked: {e}", 423)
    logger.info("terraform apply in %s — rc=%d", path, rc)
    audit_action("deploy", target=path, outcome="success" if rc == 0 else "failure")
    return respond_ok({"rc": rc, "stdout": strip_ansi(out), "stderr": strip_ansi(err)})


@terraform_bp.route("/terraform_destroy", methods=["POST"])
@require_api_key
def terraform_destroy():
    body = request.json or {}
    path = body.get("path", get_workdir())
    cmd = ["terraform", "destroy", "-auto-approve", "-input=false"]
    try:
        with state_lock(path, "terraform_destroy", user=_get_user()):
            rc, out, err = run_cmd_capture(cmd, cwd=path, timeout=300)
    except StateLockError as e:
        return respond_err(f"State locked: {e}", 423)
    logger.info("terraform destroy in %s — rc=%d", path, rc)
    audit_action("destroy", target=path, outcome="success" if rc == 0 else "failure")
    return respond_ok({"rc": rc, "stdout": strip_ansi(out), "stderr": strip_ansi(err)})
