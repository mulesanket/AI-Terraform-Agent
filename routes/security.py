# routes/security.py - Security scanning and cost estimation endpoints
import os
import sys
import json

from flask import Blueprint, request

from .helpers import require_api_key, respond_ok, respond_err, get_workdir, strip_ansi, logger, find_tf_files_recursive
from utils import run_cmd_capture

security_bp = Blueprint("security", __name__)


@security_bp.route("/run_checkov", methods=["POST"])
@require_api_key
def run_checkov():
    body = request.json or {}
    path = body.get("path", get_workdir())
    cmd = [sys.executable, "-m", "checkov.main", "-d", path, "-o", "json", "--quiet", "--compact"]
    rc, out, err = run_cmd_capture(cmd, cwd=path, timeout=180)
    if rc != 0 and "No module named" in err:
        return respond_ok({
            "rc": rc, "stdout": "", "stderr": err, "parsed": None,
            "install_required": True,
            "message": "Checkov is not installed. Run: pip install checkov"
        })
    parsed = None
    try:
        json_start = out.find('[') if '[' in out else out.find('{')
        if json_start >= 0:
            parsed = json.loads(out[json_start:])
    except (json.JSONDecodeError, TypeError):
        pass
    return respond_ok({"rc": rc, "stdout": out, "stderr": err, "parsed": parsed})


@security_bp.route("/run_infracost", methods=["POST"])
@security_bp.route("/cost_estimate", methods=["POST"])
@require_api_key
def run_cost_estimate():
    """Run free AWS Pricing API cost estimation on Terraform plan."""
    body = request.json or {}
    path = body.get("path", get_workdir())

    gen_dir = os.path.join(path, ".generated") if os.path.isdir(os.path.join(path, ".generated")) else path

    try:
        from cost_estimator import estimate_from_workdir
        estimate = estimate_from_workdir(gen_dir)
        if estimate.get("error"):
            return respond_ok({
                "rc": 1,
                "error": estimate["error"],
                "total_monthly": 0,
                "resources": [],
            })
        return respond_ok(estimate)
    except Exception as e:
        logger.error("Cost estimation failed: %s", e)
        return respond_err(f"Cost estimation failed: {e}")


@security_bp.route("/run_opa_check", methods=["POST"])
@require_api_key
def run_opa_check():
    """Run OPA policy checks against Terraform plan or generated code."""
    body = request.json or {}
    path = body.get("path", get_workdir())
    policy_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "policies")

    if not os.path.isdir(policy_dir):
        return respond_ok({"rc": 0, "results": [], "message": "No policies directory found"})

    gen_dir = os.path.join(path, ".generated") if os.path.isdir(os.path.join(path, ".generated")) else path

    # Collect .tf files content for policy input (recursively)
    tf_code = ""
    tf_file_paths = find_tf_files_recursive(gen_dir)
    for rel_path in sorted(tf_file_paths):
        try:
            with open(os.path.join(gen_dir, rel_path), "r") as f:
                tf_code += f.read() + "\n"
        except Exception:
            pass

    if not tf_code:
        return respond_ok({"rc": 0, "results": [], "message": "No .tf files found to check"})

    # Simple regex-based policy checks (no OPA binary needed)
    results = []
    import re

    # Check for S3 encryption
    if "aws_s3_bucket" in tf_code:
        if "server_side_encryption_configuration" not in tf_code and "aws_s3_bucket_server_side_encryption" not in tf_code:
            results.append({
                "policy": "s3_encryption",
                "status": "FAIL",
                "message": "S3 bucket(s) missing server-side encryption configuration",
                "severity": "HIGH",
            })
        else:
            results.append({"policy": "s3_encryption", "status": "PASS", "message": "S3 encryption configured"})

    # Check for resource tagging
    tag_resources = re.findall(r'resource\s+"(aws_\w+)"', tf_code)
    untagged = []
    for res_type in set(tag_resources):
        # Find all blocks of this type and check for tags
        pattern = rf'resource\s+"{re.escape(res_type)}"\s+"[^"]+"\s*\{{[^}}]*?\}}'
        blocks = re.findall(pattern, tf_code, re.DOTALL)
        for block in blocks:
            if "tags" not in block:
                untagged.append(res_type)
                break

    if untagged:
        results.append({
            "policy": "tagging",
            "status": "FAIL",
            "message": f"Resources missing tags: {', '.join(untagged[:5])}",
            "severity": "MEDIUM",
        })
    elif tag_resources:
        results.append({"policy": "tagging", "status": "PASS", "message": "All resources have tags"})

    return respond_ok({"rc": 0, "results": results})
