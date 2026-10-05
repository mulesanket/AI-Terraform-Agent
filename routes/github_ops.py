# routes/github_ops.py - GitHub integration endpoints
import os
import json
import logging
import shutil
import re
import time
import datetime

from flask import Blueprint, request, session
import requests as http_requests

from .helpers import (
    require_api_key, respond_ok, respond_err, get_workdir, set_workdir,
    strip_ansi, normalize_repo, find_open_pr, resolve_workdir_for_repo,
    find_tf_files_recursive, force_rmtree,
)
from utils import safe_write_file, safe_read_file, run_cmd_capture

logger = logging.getLogger("terraform-agent")

github_bp = Blueprint("github_ops", __name__)


def _github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _resolve_github_auth_context(repo_override=None):
    """Resolve GitHub token and repo from user session config or env config."""
    from config import Config
    import db as user_db

    token = ""
    repo_name = repo_override or ""

    uid = session.get("user_id")
    if uid:
        cfg = user_db.get_user_config(uid) or {}
        token = (session.get("github_token") or cfg.get("GITHUB_PAT") or "").strip()
        if not repo_name:
            repo_name = (cfg.get("GITHUB_REPO") or "").strip()

    if not token:
        token = (os.environ.get("GITHUB_PAT") or Config.GITHUB_PAT or "").strip()
    if not repo_name:
        repo_name = (os.environ.get("GITHUB_REPO") or Config.GITHUB_REPO or "").strip()

    repo_name = normalize_repo(repo_name)
    return token, repo_name


def _sync_user_runtime_config():
    """Ensure per-user settings are loaded into environment before provider clients are created."""
    import db as user_db

    uid = session.get("user_id")
    if uid:
        user_db.apply_user_config_to_env(uid)


def _fetch_user_projects(token):
    url = "https://api.github.com/user/repos"
    params = {"sort": "updated", "per_page": 100, "type": "all"}
    resp = http_requests.get(url, headers=_github_headers(token), params=params, timeout=15)
    resp.raise_for_status()

    repos = []
    for item in resp.json():
        repos.append({
            "full_name": item.get("full_name"),
            "default_branch": item.get("default_branch", "main"),
            "private": bool(item.get("private", False)),
            "updated_at": item.get("updated_at", ""),
            "description": item.get("description") or "",
        })
    return repos


def _find_active_workflow_run(token, repo_name, workflow_file, branch=None):
    """Return an in-progress or queued dispatch run on this branch only."""
    url = f"https://api.github.com/repos/{repo_name}/actions/workflows/{workflow_file}/runs"
    for status in ("in_progress", "queued", "waiting", "pending"):
        params = {"event": "workflow_dispatch", "status": status, "per_page": 10}
        if branch:
            params["branch"] = branch
        resp = http_requests.get(url, headers=_github_headers(token), params=params, timeout=20)
        if resp.status_code >= 400:
            continue
        for run in resp.json().get("workflow_runs") or []:
            if branch and (run.get("head_branch") or "") != branch:
                continue
            return run
    return None


def _branch_head_sha(token, repo_name, branch):
    """Return the current commit SHA of a branch, or None."""
    if not branch:
        return None
    url = f"https://api.github.com/repos/{repo_name}/git/ref/heads/{branch}"
    resp = http_requests.get(url, headers=_github_headers(token), timeout=20)
    if resp.status_code >= 400:
        return None
    return ((resp.json() or {}).get("object") or {}).get("sha")


def _find_latest_successful_run(token, repo_name, workflow_file, branch=None):
    """Return the latest successful dispatch for this branch only (not main or other branches)."""
    url = f"https://api.github.com/repos/{repo_name}/actions/workflows/{workflow_file}/runs"
    params = {
        "event": "workflow_dispatch",
        "status": "completed",
        "per_page": 20,
    }
    if branch:
        params["branch"] = branch
    resp = http_requests.get(url, headers=_github_headers(token), params=params, timeout=20)
    if resp.status_code >= 400:
        return None
    for run in resp.json().get("workflow_runs") or []:
        if run.get("conclusion") != "success":
            continue
        if branch and (run.get("head_branch") or "") != branch:
            continue
        return run
    return None


def _dispatch_workflow(token, repo_name, workflow_file, branch="main", inputs=None):
    url = f"https://api.github.com/repos/{repo_name}/actions/workflows/{workflow_file}/dispatches"
    body = {"ref": branch}
    if inputs:
        body["inputs"] = inputs
    resp = http_requests.post(url, headers=_github_headers(token), json=body, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"Failed to dispatch workflow: {resp.status_code} {resp.text}")


def _repo_default_branch(token, repo_name):
    url = f"https://api.github.com/repos/{repo_name}"
    resp = http_requests.get(url, headers=_github_headers(token), timeout=20)
    resp.raise_for_status()
    return (resp.json() or {}).get("default_branch") or "main"


def _local_workflow_path(workflow_file):
    candidates = [
        os.path.join(os.getcwd(), ".github", "workflows", workflow_file),
        os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", workflow_file),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def ensure_workflow_on_default_branch(token, repo_name, workflow_file):
    """Create or update a workflow file on the target repo default branch so workflow_dispatch works."""
    import base64

    local_path = _local_workflow_path(workflow_file)
    if not local_path:
        raise RuntimeError(f"Local workflow file not found: {workflow_file}")
    with open(local_path, "r", encoding="utf-8") as handle:
        content = handle.read()
    default_branch = _repo_default_branch(token, repo_name)
    remote_path = f".github/workflows/{workflow_file}"
    get_url = f"https://api.github.com/repos/{repo_name}/contents/{remote_path}"
    resp = http_requests.get(
        get_url,
        headers=_github_headers(token),
        params={"ref": default_branch},
        timeout=20,
    )
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload = {
        "message": f"chore: sync {workflow_file} for Terraform Agent pipeline",
        "content": encoded,
        "branch": default_branch,
    }
    if resp.status_code == 200:
        existing = resp.json() or {}
        remote_text = base64.b64decode((existing.get("content") or "").encode("ascii")).decode("utf-8")
        if remote_text.replace("\r\n", "\n") == content.replace("\r\n", "\n"):
            return default_branch
        payload["sha"] = existing.get("sha")
    elif resp.status_code not in (404,):
        resp.raise_for_status()
    put_resp = http_requests.put(
        get_url,
        headers=_github_headers(token),
        json=payload,
        timeout=30,
    )
    if put_resp.status_code >= 400:
        raise RuntimeError(
            f"Failed to write {remote_path} on {default_branch}: {put_resp.status_code} {put_resp.text[:400]}"
        )
    logger.info("Synced workflow %s to %s:%s", workflow_file, repo_name, default_branch)
    return default_branch


def find_latest_deployed_feature_branch(token, repo_name, prefix="generate-infra-"):
    """Return the generate-infra-* branch from the latest successful deploy run."""
    url = f"https://api.github.com/repos/{repo_name}/actions/workflows/terraform-aws-deploy.yml/runs"
    params = {"event": "workflow_dispatch", "status": "completed", "per_page": 30}
    resp = http_requests.get(url, headers=_github_headers(token), params=params, timeout=20)
    if resp.status_code < 400:
        for run in resp.json().get("workflow_runs") or []:
            if run.get("conclusion") != "success":
                continue
            branch = (run.get("head_branch") or "").strip()
            if branch.startswith(prefix):
                return branch
    branches_url = f"https://api.github.com/repos/{repo_name}/branches"
    resp = http_requests.get(
        branches_url,
        headers=_github_headers(token),
        params={"per_page": 100},
        timeout=20,
    )
    if resp.status_code >= 400:
        return None
    names = [
        item.get("name") or ""
        for item in (resp.json() or [])
        if (item.get("name") or "").startswith(prefix)
    ]
    if not names:
        return None
    names.sort(reverse=True)
    return names[0]


def _find_dispatched_run(token, repo_name, workflow_file, branch="main", max_wait_seconds=60):
    """Find the workflow run created by the latest dispatch call."""
    starts_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=20)
    url = f"https://api.github.com/repos/{repo_name}/actions/workflows/{workflow_file}/runs"
    params = {"event": "workflow_dispatch", "branch": branch, "per_page": 20}

    for _ in range(max_wait_seconds // 5):
        resp = http_requests.get(url, headers=_github_headers(token), params=params, timeout=20)
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
        for run in runs:
            created_at = run.get("created_at")
            if not created_at:
                continue
            try:
                created_dt = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            if created_dt >= starts_at:
                return run
        time.sleep(5)
    return None


def _get_run_status(token, repo_name, run_id):
    url = f"https://api.github.com/repos/{repo_name}/actions/runs/{run_id}"
    resp = http_requests.get(url, headers=_github_headers(token), timeout=20)
    resp.raise_for_status()
    return resp.json()


def _get_run_jobs(token, repo_name, run_id):
    url = f"https://api.github.com/repos/{repo_name}/actions/runs/{run_id}/jobs"
    resp = http_requests.get(url, headers=_github_headers(token), params={"per_page": 100}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def _get_job_logs(token, repo_name, job_id):
    url = f"https://api.github.com/repos/{repo_name}/actions/jobs/{job_id}/logs"
    resp = http_requests.get(url, headers=_github_headers(token), timeout=60, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"Unable to fetch logs for job {job_id}: {resp.status_code} {resp.text}")
    return strip_ansi(resp.text or "")


def _normalize_log_line(line):
    """Strip common GitHub Actions log prefixes to recover raw command output."""
    if not line:
        return ""
    cleaned = line.rstrip("\r\n")
    # GitHub Actions API log format:
    # 2026-07-27T10:00:00.0000000Z <content>
    # Sometimes with stream labels: 2026-07-27T10:00:00.0000000Z stdout F <content>
    cleaned = re.sub(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s*",
        "",
        cleaned,
    )
    return cleaned


def _extract_plan_step_log(job_log_text):
    """Return only lines belonging to terraform plan step from GitHub Actions logs.

    GitHub Actions log structure per step:
        ##[group]Run <command>        ← step header start
        <command>
        shell: ...
        env: ...
        ##[endgroup]                  ← step header end
        <actual command output>       ← THIS is what we want
        ##[group]Run <next command>   ← next step starts
    """
    lines = (job_log_text or "").splitlines()
    if not lines:
        return ""

    # Phase 1: Identify step boundaries
    # Each step has: [group_start_idx, endgroup_idx, output_start_idx, output_end_idx]
    steps = []
    i = 0
    while i < len(lines):
        line = _normalize_log_line(lines[i])
        low = line.lower()

        # Detect start of a step header
        if "##[group]" in low:
            group_start = i
            step_name = low.replace("##[group]", "").strip()

            # Find corresponding ##[endgroup]
            endgroup_idx = None
            for j in range(i + 1, min(i + 30, len(lines))):
                if "##[endgroup]" in _normalize_log_line(lines[j]).lower():
                    endgroup_idx = j
                    break

            steps.append({
                "name": step_name,
                "group_start": group_start,
                "endgroup": endgroup_idx or group_start,
            })
        i += 1

    # Phase 2: Find the terraform plan step and extract output after ##[endgroup]
    plan_step_idx = None
    for idx, step in enumerate(steps):
        if "terraform plan" in step["name"]:
            plan_step_idx = idx
            break

    if plan_step_idx is None:
        # Fallback: look for plan output patterns directly
        return _fallback_extract_plan(lines)

    plan_step = steps[plan_step_idx]
    output_start = plan_step["endgroup"] + 1

    # Output ends at the next step's ##[group] or end of file
    if plan_step_idx + 1 < len(steps):
        output_end = steps[plan_step_idx + 1]["group_start"]
    else:
        output_end = len(lines)

    # Extract and normalize output lines
    output_lines = []
    for raw in lines[output_start:output_end]:
        normalized = _normalize_log_line(raw)
        # Skip empty timing lines like "0s", "1s"
        if re.match(r"^\d+s$", normalized.strip()):
            continue
        output_lines.append(normalized)

    return "\n".join(output_lines).strip()


def _extract_full_plan_log_from_pipeline_jobs(jobs_with_logs):
    """Find and return terraform plan output from pipeline job logs."""
    if not jobs_with_logs:
        return ""

    # Prefer plan-oriented jobs first.
    prioritized = sorted(
        jobs_with_logs,
        key=lambda j: 0 if "plan" in (j.get("name") or "").lower() else 1,
    )

    for job in prioritized:
        raw_logs = job.get("logs") or ""
        if not raw_logs:
            continue
        extracted = _extract_plan_step_log(raw_logs)
        if extracted:
            return extracted

    # Fallback: try all logs concatenated.
    combined = "\n".join((j.get("logs") or "") for j in jobs_with_logs)
    return _extract_plan_step_log(combined)


def _fallback_extract_plan(lines):
    """Fallback extraction when ##[group] markers don't match expected pattern."""
    filtered = []
    capturing = False
    for raw in lines:
        line = _normalize_log_line(raw)
        low = line.lower()

        # Start capturing at plan execution indicators
        if not capturing:
            if ("resource actions are indicated" in low
                    or "terraform will perform" in low
                    or "refreshing state" in low
                    or low.strip().startswith("# ") and "will be" in low):
                capturing = True

        if capturing:
            # Stop at next step group
            if "##[group]" in low:
                break
            if re.match(r"^\d+s$", line.strip()):
                continue
            filtered.append(line)

    if not filtered:
        # Last resort: keep lines with plan keywords
        for raw in lines:
            line = _normalize_log_line(raw)
            low = line.lower()
            if (low.strip().startswith("# ") and "will be" in low
                    or "resource actions are indicated" in low
                    or low.strip().startswith("plan:")
                    or "~ " in line or "+ " in line or "- " in line):
                filtered.append(line)

    return "\n".join(filtered).strip()


def _terraform_actions_from_text(action_text):
    text = (action_text or "").lower()
    if "replaced" in text:
        return ["delete", "create"]
    if "updated" in text:
        return ["update"]
    if "destroyed" in text:
        return ["delete"]
    if "created" in text:
        return ["create"]
    return [text] if text else []


def _parse_plan_logs(plan_text):
    """Extract resource actions and attribute-level hints from terraform plan logs."""
    resources = []
    current = None
    plan_counts = {"add": 0, "change": 0, "destroy": 0}

    header_re = re.compile(r"^\s*#\s+(.+?)\s+will be\s+(.+?)\s*$")
    attr_re = re.compile(r"^\s*[~+\-]\s+([\w\[\]\.\"-]+)\s*=\s*(.+)$")
    change_re = re.compile(r"^\s*(.+?)\s*->\s*(.+?)\s*$")
    plan_re = re.compile(r"^\s*plan:\s*(\d+)\s+to\s+add,\s*(\d+)\s+to\s+change,\s*(\d+)\s+to\s+destroy\.?\s*$", re.IGNORECASE)

    for raw in (plan_text or "").splitlines():
        line = _normalize_log_line(raw)

        pm = plan_re.match(line)
        if pm:
            plan_counts = {
                "add": int(pm.group(1)),
                "change": int(pm.group(2)),
                "destroy": int(pm.group(3)),
            }

        m = header_re.match(line)
        if m:
            if current:
                resources.append(current)
            current = {
                "address": m.group(1).strip(),
                "type": (m.group(1).strip().split(".")[0] if "." in m.group(1).strip() else "unknown"),
                "actions": _terraform_actions_from_text(m.group(2).strip()),
                "diffs": [],
            }
            continue

        if not current:
            continue

        ma = attr_re.match(line)
        if not ma:
            continue

        attr = ma.group(1).strip().strip('"')
        value_text = ma.group(2).strip()

        # Skip container openings like "tags = {" — not real attribute diffs
        if value_text in ("{", "[", "(", "{}", "[]"):
            continue

        cm = change_re.match(value_text)
        if cm:
            before = cm.group(1).strip().strip('"')
            after = cm.group(2).strip().strip('"')
        else:
            before = "(in plan log)"
            after = value_text.strip('"')

        current["diffs"].append({"attribute": attr, "before": before, "after": after})

    if current:
        resources.append(current)

    no_change = "No changes." in (plan_text or "")
    counts_drift = (plan_counts["add"] + plan_counts["change"] + plan_counts["destroy"]) > 0
    return {
        "has_drift": ((len(resources) > 0) or counts_drift) and (not no_change),
        "resources": resources,
        "plan_counts": plan_counts,
    }


def _summarize_drift_with_llm(plan_text, resources, plan_counts=None):
    from llm import get_llm_client

    _sync_user_runtime_config()

    def _local_summary(local_counts=None):
        local_counts = local_counts or plan_counts or {"add": 0, "change": 0, "destroy": 0}
        total = len(resources)
        creates = sum(1 for r in resources if "create" in (r.get("actions") or []))
        updates = sum(1 for r in resources if "update" in (r.get("actions") or []))
        deletes = sum(1 for r in resources if "delete" in (r.get("actions") or []))

        top_items = []
        for item in resources[:10]:
            addr = item.get("address", "unknown")
            actions = ", ".join(item.get("actions") or ["unknown"])
            diff_count = len(item.get("diffs") or [])
            top_items.append(f"- {addr}: {actions} ({diff_count} attribute change(s) captured)")

        if deletes > 0:
            resolution = (
                "Review destructive changes first (delete/replace). If drift is unintended, align cloud state back to code "
                "by applying reviewed Terraform. If drift is intended, update Terraform configuration and merge via PR."
            )
        elif creates > 0 or updates > 0:
            resolution = (
                "Validate whether manual changes were expected. For expected changes, update Terraform code to match and "
                "run plan again. For unexpected changes, apply reviewed Terraform to restore desired state."
            )
        else:
            resolution = "No actionable resource changes were parsed from logs."

        lines = [
            "Overview",
            f"Total parsed resources: {total} (create={creates}, update={updates}, delete={deletes})",
            f"Plan totals: add={local_counts.get('add', 0)}, change={local_counts.get('change', 0)}, destroy={local_counts.get('destroy', 0)}",
            "",
            "Resources Changed",
            "\n".join(top_items) if top_items else "- No parsed resources",
            "",
            "Resolution",
            resolution,
        ]
        return "\n".join(lines)

    client, err = get_llm_client()
    if not client:
        return {
            "summary": _local_summary(plan_counts),
            "error": err,
        }

    system_prompt = (
        "You are a Terraform drift analyst. Read GitHub Actions terraform plan logs and provide a concise, "
        "human-readable drift summary with likely cause and practical resolution. "
        "Keep output plain text with short sections: Overview, Resources Changed, Resolution."
    )
    # Keep prompt bounded to reduce Bedrock timeout and payload errors.
    user_prompt = (
        f"Plan totals extracted from logs: {json.dumps(plan_counts or {'add': 0, 'change': 0, 'destroy': 0})}\n\n"
        f"Repository resource change candidates:\n{json.dumps(resources[:50], indent=2)}\n\n"
        f"Terraform plan logs (truncated):\n{(plan_text or '')[:9000]}\n\n"
        "Explain what changed, why it might have changed outside Terraform, and exact remediation steps."
    )

    try:
        summary = client.chat(system_prompt, user_prompt, temperature=0.1, max_tokens=1500)
        return {"summary": summary.strip()}
    except Exception as ex:
        logger.warning("LLM drift summary failed: %s", ex)
        return {
            "summary": _local_summary(plan_counts),
            "error": str(ex),
        }


def _analyze_plan_against_requirement(requirement, plan_text, parsed_plan):
    """Evaluate whether terraform plan matches requested infra change and flag risks."""
    requirement = (requirement or "").strip()
    parsed_plan = parsed_plan or {}
    plan_counts = parsed_plan.get("plan_counts", {"add": 0, "change": 0, "destroy": 0})
    resources = parsed_plan.get("resources", [])

    def _fallback_analysis():
        issues = []
        if "error:" in (plan_text or "").lower():
            issues.append("Terraform plan logs contain error markers.")
        if plan_counts.get("destroy", 0) > 0:
            issues.append(f"Plan includes {plan_counts.get('destroy', 0)} destroy action(s).")

        verdict = "needs-review" if issues else "looks-good"
        lines = [
            "Verdict",
            f"{verdict}",
            "",
            "Summary",
            (
                f"Plan totals: add={plan_counts.get('add', 0)}, change={plan_counts.get('change', 0)}, "
                f"destroy={plan_counts.get('destroy', 0)}"
            ),
            f"Requested change: {requirement or 'Not provided'}",
            "",
            "Potential Issues",
            "\n".join(f"- {item}" for item in issues) if issues else "- No obvious issues detected from parsed plan output.",
            "",
            "Recommendation",
            (
                "Review destructive/unexpected changes and validate against requirement before apply."
                if issues else
                "Proceed after quick manual review of affected resources."
            ),
        ]
        return {
            "verdict": verdict,
            "analysis": "\n".join(lines),
            "error": None,
        }

    if not requirement:
        return _fallback_analysis()

    from llm import get_llm_client

    _sync_user_runtime_config()
    llm, llm_err = get_llm_client()
    if not llm:
        result = _fallback_analysis()
        result["error"] = llm_err
        return result

    system_prompt = (
        "You are a Terraform plan reviewer. Compare the user's required infrastructure change with actual terraform plan logs. "
        "Return plain text with sections: Verdict, Requirement Match, Potential Issues, Recommendation. "
        "Verdict must be exactly one of: looks-good, partial-match, mismatch, plan-error, needs-review. "
        "Be strict about destructive changes, missing requested changes, and security regressions."
    )
    user_prompt = (
        f"Requested change:\n{requirement}\n\n"
        f"Parsed plan summary:\n{json.dumps({'plan_counts': plan_counts, 'resources': resources[:40]}, indent=2)}\n\n"
        f"Terraform plan logs (truncated):\n{(plan_text or '')[:11000]}\n\n"
        "State if infra build appears aligned with requirement. If not aligned, explain gaps and exact corrective actions."
    )

    try:
        analysis = llm.chat(system_prompt, user_prompt, temperature=0.1, max_tokens=1800).strip()
        first_line = (analysis.splitlines()[1].strip() if len(analysis.splitlines()) > 1 else analysis.lower()).lower()
        verdict = "needs-review"
        for candidate in ("looks-good", "partial-match", "mismatch", "plan-error", "needs-review"):
            if candidate in first_line or analysis.lower().startswith(candidate):
                verdict = candidate
                break
        return {"verdict": verdict, "analysis": analysis, "error": None}
    except Exception as ex:
        result = _fallback_analysis()
        result["error"] = str(ex)
        return result


def _build_drift_result_from_run(token, repo_name, run_id):
    run_data = _get_run_status(token, repo_name, run_id)
    status = run_data.get("status", "unknown")
    conclusion = run_data.get("conclusion")
    completed = status == "completed"

    payload = {
        "repo": repo_name,
        "run_id": run_id,
        "run_number": run_data.get("run_number"),
        "status": status,
        "conclusion": conclusion,
        "html_url": run_data.get("html_url"),
        "completed": completed,
    }

    if not completed:
        return payload

    jobs = _get_run_jobs(token, repo_name, run_id)
    plan_job = None
    for job in jobs:
        name = (job.get("name") or "").lower()
        if "plan" in name:
            plan_job = job
            break
    if not plan_job and jobs:
        plan_job = jobs[0]

    if not plan_job:
        payload.update({
            "has_drift": False,
            "resources": [],
            "plan_text": "No job logs available in this workflow run.",
            "llm_summary": "No plan job was found in this workflow run.",
        })
        return payload

    job_logs = _get_job_logs(token, repo_name, plan_job.get("id"))
    plan_text = _extract_plan_step_log(job_logs)
    parsed = _parse_plan_logs(plan_text)
    llm_result = _summarize_drift_with_llm(
        plan_text,
        parsed.get("resources", []),
        parsed.get("plan_counts", {"add": 0, "change": 0, "destroy": 0}),
    )

    payload.update({
        "plan_job": {
            "id": plan_job.get("id"),
            "name": plan_job.get("name"),
            "status": plan_job.get("status"),
            "conclusion": plan_job.get("conclusion"),
        },
        "has_drift": parsed.get("has_drift", False),
        "resources": parsed.get("resources", []),
        "plan_counts": parsed.get("plan_counts", {"add": 0, "change": 0, "destroy": 0}),
        "plan_text": plan_text,
        "llm_summary": llm_result.get("summary", ""),
        "llm_error": llm_result.get("error"),
    })
    return payload


@github_bp.route("/github/projects", methods=["GET"])
@require_api_key
def github_projects():
    """Return authenticated user's GitHub repositories (projects)."""
    try:
        token, _ = _resolve_github_auth_context()
        if not token:
            return respond_err("No GitHub token available. Login with GitHub or set GITHUB_PAT.", 400)
        repos = _fetch_user_projects(token)
        return respond_ok({"projects": repos})
    except Exception as ex:
        logger.exception("Failed to list GitHub projects")
        return respond_err(f"Failed to fetch projects: {ex}")


@github_bp.route("/github/drift/start", methods=["POST"])
@require_api_key
def github_drift_start():
    """Trigger GitHub Actions Terraform workflow on main and return run info."""
    body = request.json or {}
    repo_override = body.get("repo") or body.get("project")
    workflow_file = (body.get("workflow_file") or "terraform-aws-deploy.yml").strip()

    try:
        token, repo_name = _resolve_github_auth_context(repo_override)
        if not token:
            return respond_err("GitHub token is missing. Please authenticate with GitHub.", 400)
        if not repo_name:
            return respond_err("GitHub repo is missing. Select a project first.", 400)

        _dispatch_workflow(token, repo_name, workflow_file, branch="main")
        run = _find_dispatched_run(token, repo_name, workflow_file, branch="main", max_wait_seconds=60)
        if not run:
            return respond_err("Workflow dispatched but run not found yet. Try again in a few seconds.", 504)

        return respond_ok({
            "repo": repo_name,
            "workflow_file": workflow_file,
            "run_id": run.get("id"),
            "run_number": run.get("run_number"),
            "status": run.get("status"),
            "html_url": run.get("html_url"),
        })
    except Exception as ex:
        logger.exception("Failed to start GitHub drift workflow")
        return respond_err(str(ex))


@github_bp.route("/github/drift/status", methods=["GET"])
@require_api_key
def github_drift_status():
    """Get status for an in-flight drift workflow run and return parsed drift when complete."""
    repo_name = normalize_repo(request.args.get("repo", ""))
    run_id_raw = request.args.get("run_id", "").strip()

    if not repo_name:
        return respond_err("repo query parameter is required", 400)
    if not run_id_raw.isdigit():
        return respond_err("run_id must be a numeric GitHub workflow run id", 400)

    try:
        token, _ = _resolve_github_auth_context(repo_override=repo_name)
        if not token:
            return respond_err("GitHub token is missing. Please authenticate with GitHub.", 400)

        result = _build_drift_result_from_run(token, repo_name, int(run_id_raw))
        return respond_ok(result)
    except Exception as ex:
        logger.exception("Failed to fetch GitHub drift status")
        return respond_err(str(ex))


@github_bp.route("/git_create_branch_commit_push", methods=["POST"])
@require_api_key
def git_create_branch_commit_push():
    """Create branch, commit all changes, and push."""
    body = request.json or {}
    repo = body.get("repo_path")
    branch = body.get("branch")
    msg = body.get("commit_msg", "chore: fix by agent")

    if not repo or not branch:
        return respond_err("repo_path and branch required", 400)

    cmds = [
        ["git", "checkout", "-b", branch],
        ["git", "add", "."],
        ["git", "commit", "-m", msg],
        ["git", "push", "--set-upstream", "origin", branch]
    ]

    results = []
    for cmd in cmds:
        rc, out, err = run_cmd_capture(cmd, cwd=repo)
        results.append({"cmd": cmd, "rc": rc, "stdout": out, "stderr": err})
        if rc != 0:
            break
    return respond_ok({"results": results})


@github_bp.route("/github_create_pr", methods=["POST"])
@require_api_key
def github_create_pr():
    """Create GitHub PR using PyGithub."""
    from github import Github
    body = request.json or {}
    token = os.environ.get("GITHUB_PAT")
    if not token:
        return respond_err("GITHUB_PAT env var required", 400)
    repo_name = body.get("repo")
    head = body.get("head")
    base = body.get("base", "main")
    title = body.get("title", "Auto PR from AI agent")
    b = body.get("body", "")
    if not repo_name or not head:
        return respond_err("repo and head required", 400)
    try:
        gh = Github(token)
        repo = gh.get_repo(normalize_repo(repo_name))
        pr = repo.create_pull(title=title, body=b, head=head, base=base)
        return respond_ok({"pr_url": pr.html_url})
    except Exception as e:
        return respond_err(str(e))


@github_bp.route("/github/test", methods=["POST"])
@require_api_key
def github_test_connection():
    """Test GitHub PAT and repo access."""
    from config import Config
    if not Config.GITHUB_PAT:
        return respond_err("GITHUB_PAT not set. Add it in Settings.", 400)
    repo_name = Config.GITHUB_REPO
    if not repo_name:
        return respond_err("GITHUB_REPO not set. Add it in Settings.", 400)
    try:
        from github import Github
        gh = Github(Config.GITHUB_PAT)
        repo = gh.get_repo(normalize_repo(repo_name))
        try:
            branches = [b.name for b in list(repo.get_branches())[:10]]
        except Exception:
            branches = []
        return respond_ok({
            "connected": True, "repo": repo.full_name,
            "default_branch": repo.default_branch,
            "branches": branches, "private": repo.private,
        })
    except Exception as e:
        import traceback
        logger.error("GitHub connection error: %s", traceback.format_exc())
        return respond_err(f"GitHub connection failed: {e}")


@github_bp.route("/github/sync", methods=["POST"])
@require_api_key
def github_sync_repo():
    """Always perform a fresh clone from GitHub."""
    from config import Config

    if not Config.GITHUB_PAT or not Config.GITHUB_REPO:
        return respond_err("GITHUB_PAT and GITHUB_REPO required", 400)

    workdir = resolve_workdir_for_repo(Config.GITHUB_REPO)

    body = request.json or {}
    branch = body.get("branch", "main")

    clone_url = (
        f"https://x-access-token:{Config.GITHUB_PAT}"
        f"@github.com/{normalize_repo(Config.GITHUB_REPO)}.git"
    )

    try:
        if os.path.exists(workdir):
            logger.info("Removing existing repository before fresh clone: %s", workdir)
            force_rmtree(workdir)

        parent = os.path.dirname(os.path.abspath(workdir))
        os.makedirs(parent, exist_ok=True)
        dirname = os.path.basename(os.path.abspath(workdir))

        rc, out, err = run_cmd_capture(["git", "clone", "-b", branch, clone_url, dirname], cwd=parent)
        logger.info("Fresh Git clone to %s completed with rc=%d", workdir, rc)

        return respond_ok({
            "action": "fresh_clone", "branch": branch,
            "repo": Config.GITHUB_REPO, "workdir": workdir,
            "stdout": strip_ansi(out), "stderr": strip_ansi(err), "rc": rc,
        })
    except Exception as e:
        logger.exception("Repository sync failed")
        return respond_err(f"Repository sync failed: {str(e)}", 500)


@github_bp.route("/github/validate_repo", methods=["POST"])
@require_api_key
def github_validate_repo():
    """Validate all .tf files in the workdir."""
    workdir = get_workdir()
    issues = []
    tf_files = find_tf_files_recursive(workdir)
    if not tf_files:
        return respond_err("No .tf files found in workdir", 400)

    rc_init, init_out, init_err = run_cmd_capture(["terraform", "init", "-input=false"], cwd=workdir)
    if rc_init != 0:
        issues.append({"severity": "error", "source": "terraform init", "message": strip_ansi(init_err)})

    rc_val, val_out, val_err = run_cmd_capture(["terraform", "validate", "-json"], cwd=workdir)
    validation = {}
    try:
        validation = json.loads(val_out)
    except (json.JSONDecodeError, TypeError):
        pass

    if not validation.get("valid", False):
        for diag in validation.get("diagnostics", []):
            loc = diag.get("range", {}).get("filename", "unknown")
            issues.append({
                "severity": diag.get("severity", "error"),
                "source": f"terraform validate ({loc})",
                "message": diag.get("summary", "") + ": " + diag.get("detail", ""),
            })

    rc_fmt, fmt_out, fmt_err = run_cmd_capture(
        ["terraform", "fmt", "-check", "-diff", "-recursive"], cwd=workdir
    )
    if rc_fmt != 0 and fmt_out:
        issues.append({
            "severity": "warning", "source": "terraform fmt",
            "message": "Files need formatting:\n" + strip_ansi(fmt_out),
        })

    return respond_ok({
        "tf_files": tf_files, "valid": validation.get("valid", False),
        "issues": issues, "validation": validation,
    })


@github_bp.route("/github/drift_diff", methods=["POST"])
@require_api_key
def github_drift_diff():
    """Backward-compatible drift endpoint now backed by GitHub Actions plan logs."""
    body = request.json or {}
    repo_override = body.get("repo") or body.get("project")
    workflow_file = (body.get("workflow_file") or "terraform-aws-deploy.yml").strip()
    timeout_seconds = int(body.get("timeout_seconds", 600))

    try:
        token, repo_name = _resolve_github_auth_context(repo_override)
        if not token:
            return respond_err("GitHub token is missing. Please authenticate with GitHub.", 400)
        if not repo_name:
            return respond_err("GitHub repo is missing. Select a project first.", 400)

        _dispatch_workflow(token, repo_name, workflow_file, branch="main")
        run = _find_dispatched_run(token, repo_name, workflow_file, branch="main", max_wait_seconds=60)
        if not run:
            return respond_err("Workflow dispatched but run was not detected in time.", 504)

        run_id = int(run.get("id"))
        start = time.time()
        while time.time() - start < timeout_seconds:
            result = _build_drift_result_from_run(token, repo_name, run_id)
            if result.get("completed"):
                return respond_ok(result)
            time.sleep(8)

        return respond_ok({
            "repo": repo_name,
            "run_id": run_id,
            "completed": False,
            "status": "in_progress",
            "message": "Workflow is still running. Use /github/drift/status with run_id to continue polling.",
        })
    except Exception as ex:
        logger.exception("GitHub workflow drift detection failed")
        return respond_err(str(ex))


@github_bp.route("/github/create_drift_pr", methods=["POST"])
@require_api_key
def github_create_drift_pr():
    """Create a PR with drift fix changes."""
    from config import Config
    from orchestrator import HealingOrchestrator
    import datetime

    if not Config.GITHUB_PAT or not Config.GITHUB_REPO:
        return respond_err("GITHUB_PAT and GITHUB_REPO required", 400)

    body = request.json or {}
    title = body.get("title", "")
    description = body.get("description", "")

    workdir = get_workdir()
    orch = HealingOrchestrator(workdir=workdir)

    drift = orch.detect_drift()
    if not drift.get("has_drift"):
        return respond_ok({"message": "No drift detected. No PR needed."})

    changes = drift.get("changes", [])
    change_summary = "\n".join(
        f"- `{c['address']}`: {', '.join(c.get('actions', []))}" for c in changes
    )

    fix_generated = False
    if orch.client:
        import glob
        tf_files = glob.glob(os.path.join(workdir, "*.tf"))
        current_hcl = ""
        for tf in tf_files:
            current_hcl += safe_read_file(tf, base_dir=workdir) + "\n"
        fix, fix_err = orch.generate_fix(changes, current_hcl)
        if fix:
            main_tf = os.path.join(workdir, "main.tf")
            safe_write_file(main_tf, fix, base_dir=workdir)
            fix_generated = True

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    branch_name = f"drift-fix/{timestamp}"
    if not title:
        title = f"[AI Agent] Drift fix — {len(changes)} resource change(s)"

    pr_body = f"""## Automated Drift Detection & Fix

**Detected {len(changes)} resource change(s):**
{change_summary}

{"**AI-generated fix applied to `main.tf`**" if fix_generated else "**Manual review required**"}

---
*Created by Self-Healing Terraform Agent*
"""
    if description:
        pr_body = description + "\n\n" + pr_body

    result = orch.create_pr(branch_name, title, pr_body)
    return respond_ok(result)


@github_bp.route("/github/save_settings", methods=["POST"])
@require_api_key
def github_save_settings():
    """Save GitHub PAT and repo to user config."""
    import db as user_db

    body = request.json or {}
    pat = body.get("github_pat", "").strip()
    repo = body.get("github_repo", "").strip()

    if not repo:
        return respond_err("github_repo is required", 400)

    if pat and not (pat.startswith("ghp_") or pat.startswith("github_pat_") or pat.startswith("gho_")):
        return respond_err("Invalid PAT format. Should start with ghp_, github_pat_, or gho_", 400)

    repo = normalize_repo(repo)
    if "/" not in repo or len(repo.split("/")) != 2:
        return respond_err("Repo must be in format 'owner/repo'", 400)

    uid = session.get("user_id")
    if uid:
        config_update = {"GITHUB_REPO": repo}
        if pat:
            config_update["GITHUB_PAT"] = pat
        user_db.save_user_config(uid, config_update)
        user_db.apply_user_config_to_env(uid)
    else:
        if not pat:
            return respond_err("github_pat is required", 400)
        os.environ["GITHUB_PAT"] = pat
        os.environ["GITHUB_REPO"] = repo
        import re as re_mod
        env_path = os.path.join(os.getcwd(), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                content = f.read()
            content = re_mod.sub(r'GITHUB_PAT=.*', f'GITHUB_PAT={pat}', content)
            content = re_mod.sub(r'GITHUB_REPO=.*', f'GITHUB_REPO={repo}', content)
            with open(env_path, "w") as f:
                f.write(content)

    from config import Config
    if pat:
        Config.GITHUB_PAT = pat
    Config.GITHUB_REPO = repo

    logger.info("GitHub settings saved: repo=%s", repo)
    return respond_ok({"github_repo": repo, "pat_set": bool(pat or Config.GITHUB_PAT)})


# ---------- Pipeline-based Deploy (Generate & Modify) ----------

@github_bp.route("/github/pipeline/deploy", methods=["POST"])
@require_api_key
def github_pipeline_deploy():
    """Push code to repo and trigger the deploy workflow. Returns run info for polling."""
    body = request.json or {}
    repo_override = body.get("repo") or body.get("project")
    workflow_file = (body.get("workflow_file") or "terraform-aws-deploy.yml").strip()
    branch = body.get("branch", "main")

    try:
        token, repo_name = _resolve_github_auth_context(repo_override)
        if not token:
            return respond_err("GitHub token is missing. Please authenticate with GitHub.", 400)
        if not repo_name:
            return respond_err("GitHub repo is missing. Select a project first.", 400)

        force = bool(body.get("force"))

        # Reuse only an in-progress run on this same branch.
        active = _find_active_workflow_run(token, repo_name, workflow_file, branch=branch)
        if active:
            logger.info(
                "Skipping extra dispatch; %s already %s (run #%s)",
                branch, active.get("status"), active.get("run_number"),
            )
            return respond_ok({
                "repo": repo_name,
                "workflow_file": workflow_file,
                "branch": active.get("head_branch") or branch,
                "run_id": active.get("id"),
                "run_number": active.get("run_number"),
                "status": active.get("status"),
                "html_url": active.get("html_url"),
                "reused_existing_run": True,
            })

        # Skip only if THIS branch already applied the current commit.
        # A successful run on main (or another branch) must not block a new feature branch.
        if not force:
            successful = _find_latest_successful_run(token, repo_name, workflow_file, branch=branch)
            head_sha = _branch_head_sha(token, repo_name, branch)
            same_commit = bool(
                successful
                and head_sha
                and successful.get("head_sha") == head_sha
            )
            if successful and same_commit:
                logger.info(
                    "Skipping extra dispatch; %s commit %s already succeeded as run #%s",
                    branch, head_sha[:12], successful.get("run_number"),
                )
                return respond_ok({
                    "repo": repo_name,
                    "workflow_file": workflow_file,
                    "branch": successful.get("head_branch") or branch,
                    "run_id": successful.get("id"),
                    "run_number": successful.get("run_number"),
                    "status": successful.get("status"),
                    "html_url": successful.get("html_url"),
                    "already_deployed": True,
                })

        _dispatch_workflow(token, repo_name, workflow_file, branch=branch)
        run = _find_dispatched_run(token, repo_name, workflow_file, branch=branch, max_wait_seconds=60)
        if not run:
            return respond_err("Workflow dispatched but run not found yet. Try again in a few seconds.", 504)

        return respond_ok({
            "repo": repo_name,
            "workflow_file": workflow_file,
            "branch": branch,
            "run_id": run.get("id"),
            "run_number": run.get("run_number"),
            "status": run.get("status"),
            "html_url": run.get("html_url"),
        })
    except Exception as ex:
        logger.exception("Failed to trigger deploy pipeline")
        return respond_err(str(ex))


def trigger_terraform_destroy_pipeline(repo_override=None, branch=None, confirm=False):
    """Dispatch terraform-aws-destroy.yml. Plan-only unless confirm=True."""
    token, repo_name = _resolve_github_auth_context(repo_override)
    if not token:
        return respond_err("GitHub token is missing. Please authenticate with GitHub.", 400)
    if not repo_name:
        return respond_err("GitHub repo is missing. Select a project first.", 400)

    workflow_file = "terraform-aws-destroy.yml"
    deploy_workflow = "terraform-aws-deploy.yml"
    feature_branch = (branch or "").strip()
    if not feature_branch.startswith("generate-infra-"):
        feature_branch = find_latest_deployed_feature_branch(token, repo_name) or ""
    if not feature_branch.startswith("generate-infra-"):
        return respond_err(
            "No deployed generate-infra-* feature branch found. Deploy via Pipeline first.",
            400,
        )

    default_branch = ensure_workflow_on_default_branch(token, repo_name, workflow_file)
    try:
        ensure_workflow_on_default_branch(token, repo_name, deploy_workflow)
    except Exception as exc:
        logger.warning("Could not sync deploy workflow before destroy: %s", exc)

    if not confirm:
        active = _find_active_workflow_run(token, repo_name, workflow_file, branch=default_branch)
        if active:
            return respond_ok({
                "repo": repo_name,
                "workflow_file": workflow_file,
                "branch": feature_branch,
                "dispatch_ref": default_branch,
                "run_id": active.get("id"),
                "run_number": active.get("run_number"),
                "status": active.get("status"),
                "html_url": active.get("html_url"),
                "reused_existing_run": True,
                "awaiting_confirmation": True,
                "confirm_destroy": False,
            })

    last_error = None
    for attempt in range(4):
        try:
            _dispatch_workflow(
                token,
                repo_name,
                workflow_file,
                branch=default_branch,
                inputs={
                    "terraform_ref": feature_branch,
                    "confirm_destroy": "true" if confirm else "false",
                },
            )
            last_error = None
            break
        except RuntimeError as exc:
            last_error = exc
            logger.warning("Destroy dispatch attempt %d failed: %s", attempt + 1, exc)
            time.sleep(3)
    if last_error:
        return respond_err(str(last_error))

    run = _find_dispatched_run(
        token, repo_name, workflow_file, branch=default_branch, max_wait_seconds=60
    )
    if not run:
        return respond_err("Destroy workflow dispatched but run not found yet. Try again in a few seconds.", 504)

    logger.info(
        "Destroy pipeline started for %s run #%s confirm=%s",
        feature_branch, run.get("run_number"), confirm,
    )
    return respond_ok({
        "repo": repo_name,
        "workflow_file": workflow_file,
        "branch": feature_branch,
        "dispatch_ref": default_branch,
        "run_id": run.get("id"),
        "run_number": run.get("run_number"),
        "status": run.get("status"),
        "html_url": run.get("html_url"),
        "awaiting_confirmation": not confirm,
        "confirm_destroy": bool(confirm),
    })


@github_bp.route("/github/pipeline/destroy", methods=["POST"])
@require_api_key
def github_pipeline_destroy():
    """Destroy AWS resources from the latest deployed generate-infra-* branch via GitHub Actions."""
    body = request.json or {}
    try:
        return trigger_terraform_destroy_pipeline(
            repo_override=body.get("repo") or body.get("project"),
            branch=body.get("branch"),
            confirm=bool(body.get("confirm") or body.get("confirm_destroy")),
        )
    except Exception as ex:
        logger.exception("Failed to trigger destroy pipeline")
        return respond_err(str(ex))


@github_bp.route("/github/pipeline/status", methods=["GET"])
@require_api_key
def github_pipeline_status():
    """Get pipeline run status with parsed logs when complete."""
    repo_name = normalize_repo(request.args.get("repo", ""))
    run_id_raw = request.args.get("run_id", "").strip()

    if not repo_name:
        return respond_err("repo query parameter is required", 400)
    if not run_id_raw.isdigit():
        return respond_err("run_id must be a numeric GitHub workflow run id", 400)

    requirement = (request.args.get("requirement", "") or "").strip()

    try:
        token, _ = _resolve_github_auth_context(repo_override=repo_name)
        if not token:
            return respond_err("GitHub token is missing.", 400)

        run_id = int(run_id_raw)
        run_data = _get_run_status(token, repo_name, run_id)
        status = run_data.get("status", "unknown")
        conclusion = run_data.get("conclusion")
        completed = status == "completed"

        payload = {
            "repo": repo_name,
            "run_id": run_id,
            "run_number": run_data.get("run_number"),
            "status": status,
            "conclusion": conclusion,
            "html_url": run_data.get("html_url"),
            "completed": completed,
        }

        if not completed:
            return respond_ok(payload)

        # Fetch job logs
        jobs = _get_run_jobs(token, repo_name, run_id)
        job_logs = {}
        jobs_with_logs = []
        job_summaries = []
        for job in jobs:
            job_id = job.get("id")
            job_name = job.get("name", "unknown")
            job_conclusion = job.get("conclusion", "unknown")
            try:
                logs = _get_job_logs(token, repo_name, job_id)
            except Exception:
                logs = ""
            job_logs[job_name] = logs
            jobs_with_logs.append({"id": job_id, "name": job_name, "logs": logs})
            job_summaries.append({
                "id": job_id,
                "name": job_name,
                "status": job.get("status"),
                "conclusion": job_conclusion,
            })

        # Extract relevant log sections
        all_logs = "\n".join(job_logs.values())
        plan_text = _extract_full_plan_log_from_pipeline_jobs(jobs_with_logs)
        parsed_plan = _parse_plan_logs(plan_text)
        plan_analysis = _analyze_plan_against_requirement(requirement, plan_text, parsed_plan)
        error_text = _extract_pipeline_errors(all_logs) if conclusion == "failure" else ""

        payload.update({
            "jobs": job_summaries,
            "logs": all_logs[-15000:] if len(all_logs) > 15000 else all_logs,
            "plan_text": plan_text,
            "plan_counts": parsed_plan.get("plan_counts", {"add": 0, "change": 0, "destroy": 0}),
            "plan_resources": parsed_plan.get("resources", []),
            "requirement": requirement,
            "plan_analysis": plan_analysis.get("analysis", ""),
            "plan_verdict": plan_analysis.get("verdict", "needs-review"),
            "plan_analysis_error": plan_analysis.get("error"),
            "error_text": error_text,
            "success": conclusion == "success",
        })
        return respond_ok(payload)
    except Exception as ex:
        logger.exception("Failed to fetch pipeline status")
        return respond_err(str(ex))


@github_bp.route("/github/pipeline/analyze_and_fix", methods=["POST"])
@require_api_key
def github_pipeline_analyze_and_fix():
    """Analyze pipeline errors using LLM, fix code, and push the fix back to the branch."""
    from llm import get_llm_client
    from .postprocessors import apply_all_postprocessors, ensure_undeclared_variables_in_files

    body = request.json or {}
    error_text = body.get("error_text", "").strip()
    repo_override = body.get("repo")
    branch = body.get("branch", "main")

    if not error_text:
        return respond_err("error_text is required", 400)

    try:
        token, repo_name = _resolve_github_auth_context(repo_override)
        if not token:
            return respond_err("GitHub token is missing.", 400)
        if not repo_name:
            return respond_err("GitHub repo is missing.", 400)

        _sync_user_runtime_config()

        llm, llm_err = get_llm_client()
        if not llm:
            return respond_err(f"LLM not configured: {llm_err}", 400)

        # Fetch current code from the branch
        from github import Github
        g = Github(token)
        repo = g.get_repo(normalize_repo(repo_name))

        contents = repo.get_contents("", ref=branch)
        all_files = []
        while contents:
            item = contents.pop(0)
            if item.type == "dir":
                contents.extend(repo.get_contents(item.path, ref=branch))
            else:
                all_files.append(item)

        tf_files = {}
        for f in all_files:
            if f.path.endswith(".tf"):
                try:
                    tf_files[f.path] = f.decoded_content.decode("utf-8")
                except Exception:
                    pass

        if not tf_files:
            return respond_err("No .tf files found in the repository branch.", 400)

        files_context = ""
        for fname, content in tf_files.items():
            files_context += f"\n# --- FILE: {fname} ---\n{content}\n"

        system_prompt = (
            "You are a senior Terraform engineer. Analyze the GitHub Actions pipeline error logs "
            "and fix the Terraform code. Return ONLY valid HCL code with file markers.\n"
            "Use the format: # --- FILE: filename.tf ---\n<content>\n\n"
            "Common issues:\n"
            "- InvalidAMIID: Use aws_ami data source instead of hardcoded IDs\n"
            "- Permission denied: Check IAM references\n"
            "- Resource already exists: keep the same names; do not add random suffixes or random_id\n"
            "- Deprecated arguments: Use current Terraform 1.0+ syntax\n"
        )

        user_prompt = (
            f"## Pipeline Error Logs\n{error_text[:4000]}\n\n"
            f"## Current Terraform Code\n```hcl{files_context}\n```\n\n"
            "Fix ALL errors and return the complete corrected code using # --- FILE: <filename> --- separators."
        )

        llm_result = llm.chat_hcl_with_metadata(system_prompt, user_prompt)
        fixed_code = llm_result["text"]

        # Parse multi-file output
        import re as re_mod
        parts = re_mod.split(r'^#\s*---\s*FILE:\s*(\S+)\s*---\s*$', fixed_code, flags=re_mod.MULTILINE)
        fixed_files = {}
        if len(parts) >= 3:
            for i in range(1, len(parts), 2):
                fname = parts[i].strip()
                content = parts[i + 1].strip() + "\n" if i + 1 < len(parts) else ""
                content = apply_all_postprocessors(content)
                fixed_files[fname] = content
        else:
            # Fallback: try --- FILE: format
            parts2 = re_mod.split(r'---\s*FILE:\s*([a-zA-Z0-9_\-\.\/]+\.tf)\s*---', fixed_code)
            if len(parts2) >= 3:
                for i in range(1, len(parts2), 2):
                    fname = parts2[i].strip()
                    content = parts2[i + 1].strip() + "\n" if i + 1 < len(parts2) else ""
                    content = apply_all_postprocessors(content)
                    fixed_files[fname] = content

        if not fixed_files:
            return respond_err("LLM did not produce parseable multi-file output.", 500)

        fixed_files = ensure_undeclared_variables_in_files(fixed_files)

        # Push fixed files to the branch
        pushed_files = []
        for fname, content in fixed_files.items():
            try:
                existing = repo.get_contents(fname, ref=branch)
                if content.strip() != existing.decoded_content.decode("utf-8").strip():
                    repo.update_file(fname, f"fix: auto-heal pipeline error in {fname}", content, existing.sha, branch=branch)
                    pushed_files.append(fname)
            except Exception:
                repo.create_file(fname, f"fix: auto-heal pipeline error in {fname}", content, branch=branch)
                pushed_files.append(fname)

        return respond_ok({
            "fixed_files": pushed_files,
            "total_files_analyzed": len(tf_files),
            "branch": branch,
            "repo": repo_name,
            "analysis": error_text[:500],
        })
    except Exception as ex:
        logger.exception("Pipeline analyze and fix failed")
        return respond_err(str(ex))


def _extract_pipeline_errors(log_text):
    """Extract error lines from GitHub Actions logs."""
    error_lines = []
    lines = (log_text or "").splitlines()
    capturing = False

    for raw in lines:
        line = _normalize_log_line(raw)
        low = line.lower()

        # Detect error patterns
        if ("error" in low and ("terraform" in low or "aws" in low or ":" in low)):
            capturing = True
        if "##[error]" in low:
            capturing = True
            line = line.replace("##[error]", "").strip()

        if capturing:
            if "##[group]" in low and error_lines:
                break
            error_lines.append(line)
            # Stop after reasonable amount
            if len(error_lines) > 80:
                break

        # Also capture explicit error blocks
        if "error:" in low or "│" in line:
            if not capturing:
                capturing = True
            error_lines.append(line)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for line in error_lines:
        if line.strip() and line not in seen:
            seen.add(line)
            unique.append(line)

    return "\n".join(unique[:60])
