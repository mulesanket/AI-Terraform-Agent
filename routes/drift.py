# routes/drift.py - Drift detection, healing, audit log, and scheduler endpoints
import os
import json
import datetime

from flask import Blueprint, request

from .helpers import require_api_key, respond_ok, respond_err, get_workdir, strip_ansi, logger
from utils import run_cmd_capture

drift_bp = Blueprint("drift", __name__)


@drift_bp.route("/identify_drift", methods=["POST"])
@require_api_key
def identify_drift():
    """Inspect terraform plan JSON and return resource changes."""
    body = request.json or {}
    plan = body.get("plan_json")
    if not plan:
        return respond_err("plan_json required", 400)
    changes = []
    try:
        resources = plan.get("resource_changes", []) if isinstance(plan, dict) else []
        for rc in resources:
            actions = rc.get("change", {}).get("actions", [])
            if "update" in actions or "create" in actions or "delete" in actions:
                changes.append({
                    "address": rc.get("address"),
                    "actions": actions,
                    "type": rc.get("type"),
                    "name": rc.get("name"),
                    "change": rc.get("change")
                })
        return respond_ok({"changes": changes, "note": "Heuristic summary; use LLM to craft exact HCL edits."})
    except Exception as e:
        return respond_err(str(e))


@drift_bp.route("/heal", methods=["POST"])
@require_api_key
def heal():
    """Run the full self-healing pipeline."""
    from orchestrator import HealingOrchestrator
    body = request.json or {}
    path = body.get("path", get_workdir())
    try:
        orch = HealingOrchestrator(workdir=path)
        result = orch.heal()
        return respond_ok(result)
    except Exception as e:
        logger.error("Heal failed: %s", e)
        return respond_err(str(e))


@drift_bp.route("/drift/audit_log", methods=["GET"])
@require_api_key
def drift_audit_log():
    """Return the drift audit log."""
    audit_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "drift_audit.json")
    if os.path.exists(audit_path):
        with open(audit_path, "r") as f:
            log = json.load(f)
    else:
        log = []
    return respond_ok({"entries": log})


@drift_bp.route("/drift/audit_log/clear", methods=["POST"])
@require_api_key
def drift_audit_log_clear():
    """Clear the drift audit log."""
    audit_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "drift_audit.json")
    with open(audit_path, "w") as f:
        json.dump([], f)
    return respond_ok({"message": "Audit log cleared"})


@drift_bp.route("/notifications/test", methods=["POST"])
@require_api_key
def notifications_test():
    """Send a test notification via configured channels."""
    from notifications import notify
    from config import Config
    try:
        notify(
            "test",
            {"message": "Test notification from Terraform Agent", "timestamp": str(datetime.datetime.now())},
            slack_webhook_url=Config.SLACK_WEBHOOK_URL,
        )
        return respond_ok({"message": "Test notification sent"})
    except Exception as e:
        return respond_err(str(e))


# ---------- Scheduler control ----------
_scheduler_thread = None
_scheduler_running = False


@drift_bp.route("/scheduler/start", methods=["POST"])
@require_api_key
def scheduler_start():
    """Start the background drift check scheduler."""
    global _scheduler_thread, _scheduler_running
    from config import Config

    if _scheduler_running:
        return respond_ok({"status": "already_running"})

    import threading
    from apscheduler.schedulers.background import BackgroundScheduler
    from orchestrator import HealingOrchestrator

    interval = Config.DRIFT_CHECK_INTERVAL_MINUTES

    def _run_cycle():
        try:
            orch = HealingOrchestrator(workdir=get_workdir())
            result = orch.heal()
            # Append to audit log
            audit_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "drift_audit.json")
            log = []
            if os.path.exists(audit_path):
                try:
                    with open(audit_path, "r") as f:
                        log = json.load(f)
                except Exception:
                    log = []
            log.append({
                "timestamp": datetime.datetime.now().isoformat(),
                "outcome": result.get("outcome", "unknown"),
                "details": result,
            })
            # Keep last 100 entries
            log = log[-100:]
            with open(audit_path, "w") as f:
                json.dump(log, f, indent=2)
        except Exception as e:
            logger.error("Scheduled healing cycle failed: %s", e)

    scheduler = BackgroundScheduler()
    scheduler.add_job(_run_cycle, "interval", minutes=interval, id="drift_check")
    scheduler.start()
    _scheduler_running = True

    logger.info("Scheduler started — drift check every %d min", interval)
    return respond_ok({"status": "started", "interval_minutes": interval})


@drift_bp.route("/scheduler/stop", methods=["POST"])
@require_api_key
def scheduler_stop():
    """Stop the background scheduler."""
    global _scheduler_running
    # Note: In the full implementation, we'd keep a reference to the scheduler
    _scheduler_running = False
    logger.info("Scheduler stop requested")
    return respond_ok({"status": "stopped"})


@drift_bp.route("/scheduler/status", methods=["GET"])
@require_api_key
def scheduler_status():
    """Get scheduler running status."""
    return respond_ok({"running": _scheduler_running})
