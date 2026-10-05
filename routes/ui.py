# routes/ui.py - Frontend UI routes and settings/dashboard API
import os
import json
import logging

from flask import Blueprint, request, redirect, render_template, session

from .helpers import require_api_key, respond_ok, respond_err, get_workdir, strip_ansi
from utils import safe_read_file, run_cmd_capture

logger = logging.getLogger("terraform-agent")

ui_bp = Blueprint("ui", __name__)


def _login_required(f):
    """Lazy import of login_required to avoid circular imports at module load time."""
    import functools
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        from auth import login_required
        # Apply the decorator at call time
        decorated = login_required(f)
        return decorated(*args, **kwargs)
    return wrapper


# ---------- Page routes ----------

@ui_bp.route("/")
def root():
    return redirect("/ui")


@ui_bp.route("/ui")
@_login_required
def ui_dashboard():
    # Determine agent status: online only when Bedrock bearer token is configured
    agent_online = False
    try:
        from config import Config
        token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or Config.AWS_BEARER_TOKEN_BEDROCK
        if token:
            from llm import get_llm_client
            client, err = get_llm_client()
            if not err:
                agent_online = True
    except Exception:
        pass
    return render_template("dashboard.html", active="dashboard", agent_online=agent_online)


@ui_bp.route("/ui/generate")
@_login_required
def ui_generate():
    return render_template("generate.html", active="generate")


@ui_bp.route("/ui/drift")
@_login_required
def ui_drift():
    return render_template("drift.html", active="drift")


@ui_bp.route("/ui/heal")
@_login_required
def ui_heal():
    return render_template("heal.html", active="heal")


@ui_bp.route("/ui/security")
@_login_required
def ui_security():
    return render_template("security.html", active="security")


@ui_bp.route("/ui/settings")
@_login_required
def ui_settings():
    return render_template("settings.html", active="settings")


@ui_bp.route("/ui/modify")
@_login_required
def ui_modify():
    return render_template("modify.html", active="modify")


@ui_bp.route("/ui/github")
@_login_required
def ui_github():
    return render_template("github.html", active="github")


# ---------- API routes ----------

@ui_bp.route("/ui/api/config")
@_login_required
def ui_api_config():
    """Expose config for the settings page."""
    from config import Config
    import db as user_db

    uid = session.get("user_id")
    user_cfg = user_db.get_user_config(uid) if uid else {}

    def _mask_key(val):
        if not val:
            return ""
        if len(val) <= 8:
            return val[:2] + "\u2022" * (len(val) - 2)
        return val[:4] + "\u2022" * (len(val) - 8) + val[-4:]

    aws_access = user_cfg.get("AWS_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", ""))
    aws_secret = user_cfg.get("AWS_SECRET_ACCESS_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
    aws_token = user_cfg.get("AWS_SESSION_TOKEN", os.environ.get("AWS_SESSION_TOKEN", ""))
    bedrock_token = user_cfg.get("AWS_BEARER_TOKEN_BEDROCK") or Config.AWS_BEARER_TOKEN_BEDROCK or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    bedrock_model = user_cfg.get("BEDROCK_MODEL_ID", Config.BEDROCK_MODEL_ID)
    bedrock_region = user_cfg.get("BEDROCK_REGION", Config.BEDROCK_REGION)
    llm_provider = user_cfg.get("LLM_PROVIDER", Config.LLM_PROVIDER)

    return respond_ok({
        "port": Config.MCP_PORT,
        "agent_workdir": user_cfg.get("AGENT_WORKDIR", Config.AGENT_WORKDIR),
        "api_key_set": bool(Config.API_KEY),
        "llm_provider": llm_provider,
        "bedrock_model_id": bedrock_model,
        "bedrock_region": bedrock_region,
        "bedrock_auth": "bearer-token" if bedrock_token else "default-chain",
        "approval_mode": user_cfg.get("APPROVAL_MODE", Config.APPROVAL_MODE),
        "interval_minutes": int(user_cfg.get("DRIFT_CHECK_INTERVAL_MINUTES", Config.DRIFT_CHECK_INTERVAL_MINUTES)),
        "slack_webhook_set": bool(user_cfg.get("SLACK_WEBHOOK_URL", Config.SLACK_WEBHOOK_URL)),
        "github_repo": user_cfg.get("GITHUB_REPO", Config.GITHUB_REPO) or "",
        "github_pat_set": bool(user_cfg.get("GITHUB_PAT", Config.GITHUB_PAT)),
        "aws_access_key_set": bool(aws_access),
        "aws_secret_key_set": bool(aws_secret),
        "aws_session_token_set": bool(aws_token),
        "aws_access_key_masked": _mask_key(aws_access),
        "aws_secret_key_masked": _mask_key(aws_secret),
        "aws_session_token_masked": _mask_key(aws_token),
        "bedrock_token_set": bool(bedrock_token),
        "bedrock_token_masked": _mask_key(bedrock_token),
        "aws_region": user_cfg.get("AWS_DEFAULT_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
        "tf_state_bucket": user_cfg.get("TF_STATE_BUCKET", os.environ.get("TF_STATE_BUCKET", Config.TF_STATE_BUCKET or "")),
        "tf_state_key": user_cfg.get("TF_STATE_KEY", os.environ.get("TF_STATE_KEY", Config.TF_STATE_KEY or "")),
        "tf_state_region": user_cfg.get("TF_STATE_REGION", os.environ.get("TF_STATE_REGION", Config.TF_STATE_REGION or "us-east-1")),
        "cost_threshold": float(user_cfg.get("COST_THRESHOLD", Config.COST_THRESHOLD)),
    })


@ui_bp.route("/ui/api/save_settings", methods=["POST"])
@require_api_key
def ui_api_save_settings():
    """Save settings to user config and reload into runtime."""
    import db as user_db

    body = request.json or {}
    settings = body.get("settings")
    if not settings or not isinstance(settings, dict):
        return respond_err("settings dict required", 400)

    ALLOWED_KEYS = {
        "AGENT_WORKDIR", "APPROVAL_MODE", "LLM_PROVIDER",
        "BEDROCK_MODEL_ID", "BEDROCK_REGION", "AWS_BEARER_TOKEN_BEDROCK",
        "GITHUB_PAT", "GITHUB_REPO",
        "TF_STATE_BUCKET", "TF_STATE_KEY", "TF_STATE_REGION",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION",
        "DRIFT_CHECK_INTERVAL_MINUTES", "SLACK_WEBHOOK_URL", "COST_THRESHOLD",
    }

    uid = session.get("user_id")
    filtered = {k: v for k, v in settings.items() if k in ALLOWED_KEYS}

    AWS_ENV_KEYS = {
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION", "AWS_BEARER_TOKEN_BEDROCK",
        "TF_STATE_BUCKET", "TF_STATE_KEY", "TF_STATE_REGION",
    }
    aws_filtered = {k: v for k, v in filtered.items() if k in AWS_ENV_KEYS}
    non_aws_filtered = {k: v for k, v in filtered.items() if k not in AWS_ENV_KEYS}

    # Write AWS keys to .env
    if aws_filtered:
        import re as re_mod
        env_path = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                content = f.read()
            for key, value in aws_filtered.items():
                value = str(value).strip()
                pattern = rf'^{re_mod.escape(key)}=.*$'
                if re_mod.search(pattern, content, re_mod.MULTILINE):
                    content = re_mod.sub(pattern, f'{key}={value}', content, flags=re_mod.MULTILINE)
                else:
                    content += f'\n{key}={value}'
                os.environ[key] = value
            with open(env_path, "w") as f:
                f.write(content)
        from config import Config
        for key in aws_filtered:
            if hasattr(Config, key):
                setattr(Config, key, os.environ.get(key, ""))

    if uid:
        if filtered:
            # Persist all user settings including AWS credential keys.
            # Otherwise subsequent requests can clear env vars and UI shows "not set".
            user_db.save_user_config(uid, filtered)
            user_db.apply_user_config_to_env(uid)
        updated_keys = list(filtered.keys())
    else:
        remaining = {k: v for k, v in non_aws_filtered.items()}
        if remaining:
            import re as re_mod
            env_path = os.path.join(os.getcwd(), ".env")
            if not os.path.exists(env_path):
                return respond_err(".env file not found", 500)
            with open(env_path, "r") as f:
                content = f.read()
            for key, value in remaining.items():
                value = str(value).strip()
                pattern = rf'^{re_mod.escape(key)}=.*$'
                if re_mod.search(pattern, content, re_mod.MULTILINE):
                    content = re_mod.sub(pattern, f'{key}={value}', content, flags=re_mod.MULTILINE)
                else:
                    content += f'\n{key}={value}'
                os.environ[key] = value
            with open(env_path, "w") as f:
                f.write(content)
        updated_keys = list(filtered.keys())

        from config import Config
        for key in updated_keys:
            val = os.environ.get(key, "")
            if key == "MCP_PORT":
                Config.MCP_PORT = int(val or 5000)
            elif key == "DRIFT_CHECK_INTERVAL_MINUTES":
                Config.DRIFT_CHECK_INTERVAL_MINUTES = int(val or 30)
            elif key == "COST_THRESHOLD":
                Config.COST_THRESHOLD = float(val or 100)
            elif hasattr(Config, key):
                setattr(Config, key, val)

    logger.info("Settings updated for user %s: %s", uid or "api-key", updated_keys)
    if any(k in filtered for k in ("TF_STATE_BUCKET", "TF_STATE_KEY", "TF_STATE_REGION", "AWS_DEFAULT_REGION", "GITHUB_REPO")):
        try:
            from config import Config
            from .helpers import sync_tf_state_github_variables
            token = (user_db.get_user_config(uid).get("GITHUB_PAT") if uid else None) or Config.GITHUB_PAT
            sync_tf_state_github_variables(token, Config.GITHUB_REPO)
        except Exception as exc:
            logger.warning("Could not sync TF_STATE_BUCKET to GitHub Actions variables: %s", exc)
    return respond_ok({"updated": updated_keys})


# ---------- AWS / Bedrock status ----------

@ui_bp.route("/ui/api/aws_status", methods=["GET"])
@require_api_key
def ui_api_aws_status():
    """Check if AWS credentials are valid."""
    try:
        import boto3
        import db as user_db

        uid = session.get("user_id")
        user_cfg = user_db.get_user_config(uid) if uid else {}

        sess = boto3.Session(
            aws_access_key_id=user_cfg.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=user_cfg.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY"),
            aws_session_token=user_cfg.get("AWS_SESSION_TOKEN") or os.environ.get("AWS_SESSION_TOKEN") or None,
            region_name=user_cfg.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        sts = sess.client("sts")
        identity = sts.get_caller_identity()
        return respond_ok({"authenticated": True, "account": identity.get("Account", ""), "arn": identity.get("Arn", "")})
    except Exception as e:
        return respond_ok({"authenticated": False, "error": str(e)})


@ui_bp.route("/ui/api/bedrock_status", methods=["GET"])
@require_api_key
def ui_api_bedrock_status():
    """Check if Bedrock LLM is configured and ready."""
    import db as user_db
    from config import Config

    uid = session.get("user_id")
    user_cfg = user_db.get_user_config(uid) if uid else {}

    token = user_cfg.get("AWS_BEARER_TOKEN_BEDROCK") or Config.AWS_BEARER_TOKEN_BEDROCK or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    model = user_cfg.get("BEDROCK_MODEL_ID", Config.BEDROCK_MODEL_ID)
    region = user_cfg.get("BEDROCK_REGION", Config.BEDROCK_REGION)

    # Ensure the runtime client reads the exact user-selected values.
    os.environ["BEDROCK_MODEL_ID"] = model
    os.environ["BEDROCK_REGION"] = region
    if token:
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token

    if not token:
        return respond_ok({"ready": False, "error": "Bearer token not configured", "model": model, "region": region})

    try:
        from llm import get_llm_client
        client, err = get_llm_client()
        if err:
            return respond_ok({"ready": False, "error": err, "model": model, "region": region})
        return respond_ok({"ready": True, "model": model, "region": region, "auth": "bearer-token"})
    except Exception as e:
        return respond_ok({"ready": False, "error": str(e), "model": model, "region": region})


# ---------- Dashboard Summary ----------

@ui_bp.route("/ui/api/dashboard_summary", methods=["GET"])
def ui_api_dashboard_summary():
    """Aggregate real-time data for the dashboard."""
    from config import Config

    workdir = get_workdir()

    do_refresh = request.args.get("refresh", "").lower() in ("true", "1", "yes")
    if do_refresh:
        run_cmd_capture(["terraform", "init", "-input=false"], cwd=workdir, timeout=60)
        run_cmd_capture(["terraform", "refresh", "-input=false"], cwd=workdir, timeout=120)

    # Sync user config into env (same as login_required does) so LLMClient picks it up
    import db as user_db
    uid = session.get("user_id")
    if uid:
        user_db.apply_user_config_to_env(uid)

    # Agent is online only when LLM client initializes successfully
    agent_status = "offline"
    try:
        from llm import get_llm_client
        client, err = get_llm_client()
        if not err:
            agent_status = "online"
        else:
            logger.warning("Agent status offline - LLM error: %s", err)
    except Exception as e:
        logger.warning("Agent status offline - exception: %s", e)

    summary = {
        "agent": {"workdir": workdir, "status": agent_status},
        "aws": {"status": "unknown", "account": None, "arn": None},
        "terraform": {"resources": [], "resource_count": 0, "state_exists": False},
        "generated": {"has_code": False, "file_count": 0, "files": []},
        "github": {"configured": bool(Config.GITHUB_PAT and Config.GITHUB_REPO), "repo": Config.GITHUB_REPO or ""},
        "config": {
            "approval_mode": Config.APPROVAL_MODE,
            "llm_provider": Config.LLM_PROVIDER,
            "bedrock_model": Config.BEDROCK_MODEL_ID,
            "aws_profile": "",
        },
    }

    # AWS identity
    try:
        import boto3
        sess = boto3.Session(
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            aws_session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        sts = sess.client("sts")
        identity = sts.get_caller_identity()
        summary["aws"] = {
            "status": "authenticated",
            "account": identity.get("Account", ""),
            "arn": identity.get("Arn", ""),
            "user_id": identity.get("UserId", ""),
        }
    except Exception:
        summary["aws"]["status"] = "not_authenticated"

    # Terraform state
    main_state = os.path.join(workdir, "terraform.tfstate")
    gen_state = os.path.join(workdir, ".generated", "terraform.tfstate")
    state_file = main_state if os.path.exists(main_state) else (gen_state if os.path.exists(gen_state) else None)
    if state_file:
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            resources = []
            for r in state.get("resources", []):
                rtype = r.get("type", "")
                rname = r.get("name", "")
                mode = r.get("mode", "managed")
                attrs = {}
                if r.get("instances"):
                    attrs = r["instances"][0].get("attributes", {})
                res_info = {"type": rtype, "name": rname, "mode": mode, "address": f"{rtype}.{rname}"}
                if rtype == "aws_instance":
                    res_info["instance_id"] = attrs.get("id", "")
                    res_info["instance_type"] = attrs.get("instance_type", "")
                    res_info["public_ip"] = attrs.get("public_ip", "")
                    res_info["state"] = attrs.get("instance_state", "")
                    res_info["tags"] = attrs.get("tags", {})
                elif rtype == "aws_s3_bucket":
                    res_info["bucket"] = attrs.get("bucket", "")
                    res_info["region"] = attrs.get("region", "")
                    res_info["arn"] = attrs.get("arn", "")
                elif rtype == "aws_key_pair":
                    res_info["key_name"] = attrs.get("key_name", "")
                    res_info["fingerprint"] = attrs.get("fingerprint", "")
                elif rtype == "aws_lambda_function":
                    res_info["function_name"] = attrs.get("function_name", "")
                    res_info["runtime"] = attrs.get("runtime", "")
                    res_info["arn"] = attrs.get("arn", "")
                resources.append(res_info)

            # Live AWS verification
            aws_session = None
            try:
                aws_session = boto3.Session(
                    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                    aws_session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
                    region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                )
            except Exception:
                pass

            if aws_session:
                ec2_resources = [r for r in resources if r["type"] == "aws_instance" and r.get("instance_id")]
                if ec2_resources:
                    try:
                        ec2 = aws_session.client("ec2", region_name=Config.BEDROCK_REGION or "us-east-1")
                        ids = [r["instance_id"] for r in ec2_resources]
                        resp = ec2.describe_instances(InstanceIds=ids)
                        live_states = {}
                        for resv in resp.get("Reservations", []):
                            for inst in resv.get("Instances", []):
                                live_states[inst["InstanceId"]] = inst["State"]["Name"]
                        for r in ec2_resources:
                            iid = r["instance_id"]
                            if iid in live_states:
                                r["state"] = live_states[iid]
                                r["live_verified"] = True
                            else:
                                r["state"] = "not_found"
                                r["live_verified"] = True
                    except Exception as e:
                        err_str = str(e)
                        for r in ec2_resources:
                            if "InvalidInstanceID.NotFound" in err_str:
                                r["state"] = "terminated"
                                r["live_verified"] = True
                            else:
                                r["live_verified"] = False

                s3_resources = [r for r in resources if r["type"] == "aws_s3_bucket" and r.get("bucket")]
                if s3_resources:
                    try:
                        s3 = aws_session.client("s3")
                        existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
                        for r in s3_resources:
                            r["live_verified"] = True
                            r["state"] = "active" if r["bucket"] in existing else "deleted"
                    except Exception:
                        for r in s3_resources:
                            r["live_verified"] = False

            summary["terraform"] = {
                "resources": resources,
                "resource_count": len([r for r in resources if r["mode"] == "managed"]),
                "data_source_count": len([r for r in resources if r["mode"] == "data"]),
                "state_exists": True,
                "state_source": "main" if state_file == main_state else "generated",
            }
        except Exception:
            summary["terraform"]["state_exists"] = False

    # All files in workdir - recursively walk the directory to capture folder structure
    def list_files_recursive(base_path, rel_prefix=""):
        files = []
        try:
            for fname in os.listdir(base_path):
                fpath = os.path.join(base_path, fname)
                rel_path = os.path.join(rel_prefix, fname) if rel_prefix else fname
                if os.path.isfile(fpath):
                    files.append({"name": rel_path, "size": os.path.getsize(fpath)})
                elif os.path.isdir(fpath):
                    files.extend(list_files_recursive(fpath, rel_path))
        except Exception:
            pass
        return files

    tf_files = list_files_recursive(workdir)

    gen_dir = os.path.join(workdir, ".generated")
    gen_files = []
    if os.path.isdir(gen_dir):
        gen_files = list_files_recursive(gen_dir)

    summary["generated"] = {"has_code": len(gen_files) > 0, "file_count": len(gen_files), "files": gen_files}
    summary["workdir_files"] = tf_files

    # Recent activity
    all_tf = []
    for f in tf_files:
        fpath = os.path.join(workdir, f["name"])
        try:
            mtime = os.path.getmtime(fpath)
            all_tf.append({"name": f["name"], "mtime": mtime, "source": "workdir"})
        except Exception:
            pass
    for f in gen_files:
        fpath = os.path.join(gen_dir, f["name"])
        try:
            mtime = os.path.getmtime(fpath)
            all_tf.append({"name": f["name"], "mtime": mtime, "source": ".generated"})
        except Exception:
            pass
    all_tf.sort(key=lambda x: x["mtime"], reverse=True)
    summary["recent_activity"] = all_tf[:5]

    return respond_ok(summary)


# ---------- Health ----------

@ui_bp.route("/health", methods=["GET"])
def health():
    return respond_ok({"status": "ok", "workdir": get_workdir()})


# ---------- Audit Trail API ----------

@ui_bp.route("/ui/api/audit_log", methods=["GET"])
@require_api_key
def ui_api_audit_log():
    """Query the audit trail. Supports ?limit=N&offset=N&action=X&since=ISO."""
    import audit
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    action = request.args.get("action")
    since = request.args.get("since")
    user_id = request.args.get("user_id", type=int)

    entries = audit.get_audit_log(limit=limit, offset=offset, action=action, user_id=user_id, since=since)
    return respond_ok({"entries": entries, "count": len(entries)})


@ui_bp.route("/ui/api/audit_stats", methods=["GET"])
@require_api_key
def ui_api_audit_stats():
    """Get audit log summary statistics."""
    import audit
    days = request.args.get("days", 30, type=int)
    stats = audit.get_audit_stats(days=days)
    return respond_ok(stats)


# ---------- Plan Approval API ----------

@ui_bp.route("/ui/api/pending_plans", methods=["GET"])
@require_api_key
def ui_api_pending_plans():
    """List pending plans awaiting approval."""
    import approval
    status = request.args.get("status")
    limit = request.args.get("limit", 50, type=int)
    plans = approval.list_plans(status=status, limit=limit)
    return respond_ok({"plans": plans, "approval_required": approval.is_approval_required()})


@ui_bp.route("/ui/api/pending_plans/<int:plan_id>", methods=["GET"])
@require_api_key
def ui_api_get_plan(plan_id):
    """Get a specific plan by ID."""
    import approval
    plan = approval.get_plan(plan_id)
    if not plan:
        return respond_err("Plan not found", 404)
    return respond_ok(plan)


@ui_bp.route("/ui/api/pending_plans/<int:plan_id>/approve", methods=["POST"])
@require_api_key
def ui_api_approve_plan(plan_id):
    """Approve a pending plan."""
    import approval
    from .helpers import audit_action

    username = session.get("display_name") or session.get("username") or "api"
    ok = approval.approve_plan(plan_id, approved_by=username)
    if not ok:
        return respond_err("Plan not found or already processed", 400)

    audit_action("approve_plan", target=f"plan#{plan_id}", outcome="success",
                 details={"approved_by": username})
    return respond_ok({"plan_id": plan_id, "status": "approved", "approved_by": username})


@ui_bp.route("/ui/api/pending_plans/<int:plan_id>/reject", methods=["POST"])
@require_api_key
def ui_api_reject_plan(plan_id):
    """Reject a pending plan."""
    import approval
    from .helpers import audit_action

    body = request.json or {}
    reason = body.get("reason", "")
    username = session.get("display_name") or session.get("username") or "api"
    ok = approval.reject_plan(plan_id, rejected_by=username, reason=reason)
    if not ok:
        return respond_err("Plan not found or already processed", 400)

    audit_action("reject_plan", target=f"plan#{plan_id}", outcome="success",
                 details={"rejected_by": username, "reason": reason})
    return respond_ok({"plan_id": plan_id, "status": "rejected", "rejected_by": username})


@ui_bp.route("/ui/api/pending_plans/<int:plan_id>/apply", methods=["POST"])
@require_api_key
def ui_api_apply_approved_plan(plan_id):
    """Apply an approved plan (runs terraform apply)."""
    import approval
    from state_lock import state_lock, StateLockError
    from .helpers import audit_action

    plan = approval.get_plan(plan_id)
    if not plan:
        return respond_err("Plan not found", 404)
    if plan["status"] != "approved":
        return respond_err(f"Plan must be approved before applying (current: {plan['status']})", 400)

    workdir = plan["workdir"]
    plan_file = plan["plan_file"]
    username = session.get("display_name") or session.get("username") or "api"

    cmd = ["terraform", "apply", "-auto-approve", "-input=false", plan_file]
    try:
        with state_lock(workdir, "approval_apply", user=username):
            rc, out, err = run_cmd_capture(cmd, cwd=workdir, timeout=300)
    except StateLockError as e:
        return respond_err(f"State locked: {e}", 423)

    approval.mark_applied(plan_id, rc, output=strip_ansi(out + "\n" + err)[:5000])
    audit_action("deploy", target=f"plan#{plan_id}", outcome="success" if rc == 0 else "failure",
                 details={"plan_id": plan_id, "rc": rc})

    return respond_ok({
        "plan_id": plan_id, "status": "applied",
        "rc": rc, "stdout": strip_ansi(out), "stderr": strip_ansi(err),
    })
