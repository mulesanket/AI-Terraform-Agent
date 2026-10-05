# routes/modify.py - Modify existing infrastructure endpoints
import os
import json
import logging
import difflib

from flask import Blueprint, request, Response, stream_with_context

from .helpers import (
    require_api_key, respond_ok, respond_err, get_workdir, set_workdir,
    strip_ansi, normalize_repo, find_open_pr, resolve_workdir_for_repo,
    force_rmtree, write_s3_backend_tf,
)
from .postprocessors import apply_all_postprocessors
from utils import safe_write_file, safe_read_file, run_cmd_capture

logger = logging.getLogger("terraform-agent")

modify_bp = Blueprint("modify", __name__)


_LLM_TOKEN_PRICING = {
    "haiku": {"input_per_1k": 0.00025, "output_per_1k": 0.00125, "label": "Claude Haiku"},
    "sonnet": {"input_per_1k": 0.00300, "output_per_1k": 0.01500, "label": "Claude Sonnet"},
    "opus": {"input_per_1k": 0.01500, "output_per_1k": 0.07500, "label": "Claude Opus"},
}


def _approx_tokens(text):
    """Cheap token estimate used when Bedrock usage metadata is unavailable."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _llm_rate_for_model(model_id):
    model = (model_id or "").lower()
    family = "sonnet"
    for key in ("haiku", "sonnet", "opus"):
        if key in model:
            family = key
            break
    rates = dict(_LLM_TOKEN_PRICING[family])
    rates["family"] = family
    return rates


def _build_token_utilization(model_id, system_prompt, user_prompt, output_text, usage):
    usage = usage or {}
    rates = _llm_rate_for_model(model_id)
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    source = "bedrock-usage"

    if input_tokens is None or output_tokens is None:
        source = "heuristic"
        input_tokens = _approx_tokens(system_prompt) + _approx_tokens(user_prompt)
        output_tokens = _approx_tokens(output_text)
        total_tokens = input_tokens + output_tokens
    elif total_tokens is None:
        total_tokens = input_tokens + output_tokens

    input_cost = (input_tokens / 1000.0) * rates["input_per_1k"]
    output_cost = (output_tokens / 1000.0) * rates["output_per_1k"]
    total_cost = input_cost + output_cost

    return {
        "model_id": model_id,
        "model_family": rates["family"],
        "source": source,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "estimated_cost": round(total_cost, 6),
        "note": "Estimated from Bedrock token usage when available; otherwise approximated from prompt/output length.",
    }


def _find_tf_files_recursive(base_path, rel_prefix=""):
    """Recursively find all .tf files in a directory, returning relative paths."""
    tf_files = []
    try:
        for fname in os.listdir(base_path):
            fpath = os.path.join(base_path, fname)
            rel_path = os.path.join(rel_prefix, fname) if rel_prefix else fname
            if os.path.isfile(fpath) and fname.endswith(".tf"):
                tf_files.append(rel_path)
            elif os.path.isdir(fpath):
                tf_files.extend(_find_tf_files_recursive(fpath, rel_path))
    except Exception:
        pass
    return tf_files


def _find_proposed_files_recursive(base_path, rel_prefix=""):
    """Recursively find all .tf.proposed files in a directory, returning relative paths."""
    proposed_files = []
    try:
        for fname in os.listdir(base_path):
            fpath = os.path.join(base_path, fname)
            rel_path = os.path.join(rel_prefix, fname) if rel_prefix else fname
            if os.path.isfile(fpath) and fname.endswith(".tf.proposed"):
                proposed_files.append(rel_path)
            elif os.path.isdir(fpath):
                proposed_files.extend(_find_proposed_files_recursive(fpath, rel_path))
    except Exception:
        pass
    return proposed_files


MODIFY_SYSTEM_PROMPT = """You are a senior Terraform engineer. You are given existing Terraform HCL files
and a user's requested change in natural language.

Rules:
- Return ONLY valid HCL code that represents the COMPLETE modified file(s). No markdown fences, no explanations.
- Preserve all existing resources, variables, and outputs that are NOT affected by the change.
- Make ONLY the changes the user requested. Do not add extra resources, tags, or refactoring.
- Do not add random_id or numeric/hex suffixes to tags.Name, resource name, or S3 bucket names.
- Use current Terraform syntax (1.0+). No deprecated arguments.
- If the change touches multiple files, separate them with a line: # --- FILE: <filename> ---
- Follow security best practices: encryption, least privilege, no unnecessary public access.
"""


def _parse_multi_file_output(code, existing_files):
    """Parse LLM output that may contain # --- FILE: name.tf --- separators."""
    import re as re_mod
    parts = re_mod.split(r'^#\s*---\s*FILE:\s*(\S+)\s*---\s*$', code, flags=re_mod.MULTILINE)

    if len(parts) >= 3:
        result = {}
        for i in range(1, len(parts), 2):
            fname = parts[i].strip()
            content = parts[i + 1].strip() + "\n" if i + 1 < len(parts) else ""
            result[fname] = content
        return result

    if len(existing_files) == 1:
        fname = list(existing_files.keys())[0]
        return {fname: code.strip() + "\n"}

    return {"main.tf": code.strip() + "\n"}


def _cleanup_proposed(workdir):
    """Remove all .tf.proposed staging files recursively."""
    proposed_files = _find_proposed_files_recursive(workdir)
    for rel_path in proposed_files:
        os.remove(os.path.join(workdir, rel_path))


@modify_bp.route("/infra/modify", methods=["POST"])
@require_api_key
def infra_modify():
    """Read current TF code, send to LLM with change request, return diff + modified code."""
    body = request.json or {}
    change_request = body.get("change_request", "").strip()
    if not change_request:
        return respond_err("change_request is required", 400)
    if len(change_request) > 5000:
        return respond_err("change_request too long (max 5000 chars)", 400)

    from llm import get_llm_client
    llm, llm_err = get_llm_client()
    if not llm:
        return respond_err(f"LLM not configured: {llm_err}", 400)

    workdir = get_workdir()

    # Read all current .tf files recursively
    existing_files = {}
    tf_file_paths = _find_tf_files_recursive(workdir)
    for rel_path in sorted(tf_file_paths):
        try:
            content = safe_read_file(os.path.join(workdir, rel_path), base_dir=workdir)
            existing_files[rel_path] = content
        except Exception:
            pass

    if not existing_files:
        return respond_err("No .tf files found in the working directory. Generate infrastructure first.", 400)

    files_context = ""
    for fname, content in existing_files.items():
        files_context += f"\n# --- FILE: {fname} ---\n{content}\n"

    user_prompt = (
        f"## Current Terraform Code\n```hcl{files_context}\n```\n\n"
        f"## Requested Change\n{change_request}\n\n"
        "Apply ONLY the requested change and return the complete updated Terraform code. "
        "Use the # --- FILE: <filename> --- separator between files."
    )

    try:
        llm_result = llm.chat_hcl_with_metadata(MODIFY_SYSTEM_PROMPT, user_prompt)
        modified_code = llm_result["text"]
        logger.info("Modified code (%d chars) for: %s", len(modified_code), change_request[:80])
        token_utilization = _build_token_utilization(
            model_id=getattr(llm, "model_id", ""),
            system_prompt=MODIFY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            output_text=modified_code,
            usage=llm_result.get("usage"),
        )

        modified_files = _parse_multi_file_output(modified_code, existing_files)

        diffs = {}
        for fname, new_content in modified_files.items():
            old_content = existing_files.get(fname, "")
            old_lines = old_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{fname}", tofile=f"b/{fname}", lineterm=""))
            if diff:
                diffs[fname] = "\n".join(diff)

        # Write proposed files (ensure parent directories exist for nested paths)
        for fname, content in modified_files.items():
            proposed_path = os.path.join(workdir, fname + ".proposed")
            parent_dir = os.path.dirname(proposed_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            safe_write_file(proposed_path, content, base_dir=workdir)

        return respond_ok({
            "change_request": change_request,
            "original_files": existing_files,
            "modified_files": modified_files,
            "diffs": diffs,
            "files_changed": list(diffs.keys()),
            "token_utilization": token_utilization,
        })

    except Exception as e:
        logger.error("Infra modify failed: %s", e)
        return respond_err(f"Modification failed: {e}")


@modify_bp.route("/infra/modify/validate", methods=["POST"])
@require_api_key
def infra_modify_validate():
    """Apply proposed changes, run terraform init+validate+plan with SSE streaming."""
    workdir = get_workdir()

    proposed_files = []
    backups = {}
    proposed_paths = _find_proposed_files_recursive(workdir)
    for rel_path in proposed_paths:
        real_name = rel_path.replace(".proposed", "")
        real_path = os.path.join(workdir, real_name)
        proposed_path = os.path.join(workdir, rel_path)
        if os.path.exists(real_path):
            backups[real_name] = safe_read_file(real_path, base_dir=workdir)
        proposed_content = safe_read_file(proposed_path, base_dir=workdir)
        proposed_content = apply_all_postprocessors(proposed_content)
        # Ensure parent directory exists for nested paths
        parent_dir = os.path.dirname(real_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        safe_write_file(real_path, proposed_content, base_dir=workdir)
        proposed_files.append(real_name)

    if not proposed_files:
        return respond_err("No proposed changes found. Run /infra/modify first.", 400)

    def _sse(event, data):
        payload = json.dumps(data) if isinstance(data, dict) else str(data)
        return f"event: {event}\ndata: {payload}\n\n"

    def generate():
        try:
            yield _sse("progress", {"step": "apply_proposed", "status": "done",
                                    "message": f"Applied {len(proposed_files)} proposed file(s): {', '.join(proposed_files)}"})

            yield _sse("progress", {"step": "init", "status": "running",
                                    "message": "Running terraform init..."})
            rc_init, out_init, err_init = run_cmd_capture(
                ["terraform", "init", "-input=false", "-upgrade"], cwd=workdir
            )
            if rc_init != 0:
                yield _sse("progress", {"step": "init", "status": "error",
                                        "message": f"terraform init failed: {strip_ansi(err_init)[:300]}"})
                for rn, c in backups.items():
                    safe_write_file(os.path.join(workdir, rn), c, base_dir=workdir)
                yield _sse("result", {"valid": False, "validation": {}, "plan_summary": "",
                                      "files_applied": proposed_files,
                                      "error": f"terraform init failed: {strip_ansi(err_init)[:200]}"})
                return
            yield _sse("progress", {"step": "init", "status": "done", "message": "terraform init completed"})

            yield _sse("progress", {"step": "validate", "status": "running", "message": "Running terraform validate..."})
            rc_val, val_out, val_err = run_cmd_capture(["terraform", "validate", "-json"], cwd=workdir)
            valid = False
            validation = {}
            try:
                validation = json.loads(val_out)
                valid = validation.get("valid", False)
            except (json.JSONDecodeError, TypeError):
                valid = rc_val == 0

            if valid:
                yield _sse("progress", {"step": "validate", "status": "done", "message": "Validation passed"})
            else:
                diag = ""
                if validation.get("diagnostics"):
                    diag = "; ".join(d.get("summary", "") for d in validation["diagnostics"][:3])
                yield _sse("progress", {"step": "validate", "status": "error",
                                        "message": f"Validation failed: {diag or strip_ansi(val_err)[:200]}"})
                for rn, c in backups.items():
                    safe_write_file(os.path.join(workdir, rn), c, base_dir=workdir)
                yield _sse("result", {"valid": False, "validation": validation, "plan_summary": "",
                                      "files_applied": proposed_files})
                return

            yield _sse("progress", {"step": "plan", "status": "running",
                                    "message": "Running terraform plan..."})
            rc_plan, plan_out, plan_err = run_cmd_capture(
                ["terraform", "plan", "-out", "plan.out", "-input=false"], cwd=workdir,
            )
            plan_summary = strip_ansi(plan_out)

            if rc_plan != 0:
                yield _sse("progress", {"step": "plan", "status": "error",
                                        "message": f"terraform plan failed: {strip_ansi(plan_err)[:200]}"})
            else:
                summary_line = ""
                for line in plan_summary.splitlines():
                    if "Plan:" in line and "to add" in line:
                        summary_line = line.strip()
                        break
                yield _sse("progress", {"step": "plan", "status": "done",
                                        "message": summary_line or "Plan generated successfully"})

            yield _sse("result", {
                "valid": valid, "validation": validation,
                "plan_summary": plan_summary, "files_applied": proposed_files,
            })

        except Exception as e:
            logger.error("Validate stream error: %s", e)
            for rn, c in backups.items():
                safe_write_file(os.path.join(workdir, rn), c, base_dir=workdir)
            yield _sse("result", {"valid": False, "validation": {}, "plan_summary": "",
                                  "files_applied": proposed_files, "error": str(e)})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@modify_bp.route("/infra/modify/apply", methods=["POST"])
@require_api_key
def infra_modify_apply():
    """Apply the validated plan. If mode is 'pr', create a PR instead."""
    from config import Config

    workdir = get_workdir()

    body = request.json or {}
    mode = body.get("mode", Config.APPROVAL_MODE)

    if mode == "pr" and Config.GITHUB_PAT and Config.GITHUB_REPO:
        try:
            from github import Github
            import time as _time

            g = Github(Config.GITHUB_PAT)
            repo = g.get_repo(normalize_repo(Config.GITHUB_REPO))
            default_branch = repo.default_branch
            change_desc = body.get("change_request", "Infrastructure modification")
            requested_branch = (body.get("branch") or "").strip()

            proposed_paths = _find_proposed_files_recursive(workdir)
            tf_file_paths = proposed_paths if proposed_paths else _find_tf_files_recursive(workdir)
            if not tf_file_paths:
                return respond_err("No Terraform files found to create a PR from.", 400)

            try:
                ref = repo.get_git_ref(f"heads/{default_branch}")
            except Exception:
                repo.create_file("README.md", "Initial commit — Terraform Agent",
                                 "# Terraform Infrastructure\n\nManaged by AI Terraform Agent.\n",
                                 branch=default_branch)
                ref = repo.get_git_ref(f"heads/{default_branch}")

            existing_pr = None
            existing_branch = None
            if requested_branch:
                try:
                    repo.get_git_ref(f"heads/{requested_branch}")
                    existing_branch = requested_branch
                except Exception:
                    repo.create_git_ref(f"refs/heads/{requested_branch}", ref.object.sha)
                    existing_branch = requested_branch
                try:
                    existing_pr, _ = find_open_pr(repo, requested_branch)
                except Exception:
                    existing_pr = None
            else:
                existing_pr, existing_branch = find_open_pr(repo, "infra-modify-")

            if existing_pr:
                branch_name = existing_branch
            elif requested_branch:
                branch_name = requested_branch
            else:
                branch_name = f"infra-modify-{int(_time.time())}"
                repo.create_git_ref(f"refs/heads/{branch_name}", ref.object.sha)

            pushed_files = []
            backend_name = write_s3_backend_tf(workdir)
            if backend_name and backend_name not in tf_file_paths:
                tf_file_paths.append(backend_name)
            for rel_path in tf_file_paths:
                is_proposed = rel_path.endswith(".proposed")
                real_path = rel_path.replace(".proposed", "") if is_proposed else rel_path
                fpath = os.path.join(workdir, rel_path)
                content = safe_read_file(fpath, base_dir=workdir)
                if is_proposed:
                    content = apply_all_postprocessors(content)
                try:
                    existing = repo.get_contents(real_path, ref=branch_name)
                    repo_content = existing.decoded_content.decode("utf-8")
                    if content.strip() == repo_content.strip():
                        continue
                    repo.update_file(real_path, f"modify: {change_desc[:60]}", content, existing.sha, branch=branch_name)
                except Exception:
                    repo.create_file(real_path, f"modify: {change_desc[:60]}", content, branch=branch_name)
                pushed_files.append(real_path)

            if not pushed_files:
                _cleanup_proposed(workdir)
                return respond_err("No actual changes to push.", 400)

            if existing_pr:
                existing_pr.create_issue_comment(
                    f"## Updated Modification\n**Change:** {change_desc}\n\n"
                    f"### Files Changed\n" + "\n".join(f"- `{f}`" for f in pushed_files) + "\n\n"
                    f"*Auto-updated by Terraform Agent*"
                )
                _cleanup_proposed(workdir)
                return respond_ok({
                    "action": "pr_updated", "pr_url": existing_pr.html_url,
                    "pr_number": existing_pr.number, "branch": branch_name,
                })
            else:
                pr = repo.create_pull(
                    title=f"Infra Modify: {change_desc[:80]}",
                    body=(f"## Requested Change\n{change_desc}\n\n"
                          f"### Files Changed\n" + "\n".join(f"- `{f}`" for f in pushed_files) + "\n\n"
                          "- Terraform validate: **not run yet**\n- Terraform plan: **not run yet**\n\n"
                          "*Auto-generated by Terraform Agent*"),
                    head=branch_name, base=default_branch,
                )
                _cleanup_proposed(workdir)
                return respond_ok({
                    "action": "pr_created", "pr_url": pr.html_url,
                    "pr_number": pr.number, "branch": branch_name,
                })
        except Exception as e:
            logger.error("PR creation failed: %s", e)
            return respond_err(f"PR creation failed: {e}")
    else:
        plan_path = os.path.join(workdir, "plan.out")
        if not os.path.exists(plan_path):
            return respond_err("No plan.out found. Run /infra/modify/validate first.", 400)

        rc, out, err = run_cmd_capture(
            ["terraform", "apply", "-input=false", "-auto-approve", "plan.out"], cwd=workdir,
        )
        _cleanup_proposed(workdir)
        return respond_ok({"action": "applied", "rc": rc, "stdout": strip_ansi(out), "stderr": strip_ansi(err)})


@modify_bp.route("/infra/modify/discard", methods=["POST"])
@require_api_key
def infra_modify_discard():
    """Discard proposed changes."""
    workdir = get_workdir()
    restored = []
    proposed_paths = _find_proposed_files_recursive(workdir)
    for rel_path in proposed_paths:
        os.remove(os.path.join(workdir, rel_path))
        restored.append(rel_path.replace(".proposed", ""))
    return respond_ok({"discarded": restored})


@modify_bp.route("/infra/modify/sync", methods=["POST"])
@require_api_key
def infra_modify_sync():
    """Fetch the latest Terraform code from the GitHub repo into workdir."""
    from config import Config
    import shutil

    if not Config.GITHUB_PAT or not Config.GITHUB_REPO:
        return respond_err("GITHUB_PAT and GITHUB_REPO must be configured in Settings.", 400)

    workdir = resolve_workdir_for_repo(Config.GITHUB_REPO)

    body = request.json or {}
    branch = body.get("branch", "")

    try:
        from github import Github
        g = Github(Config.GITHUB_PAT)
        repo = g.get_repo(normalize_repo(Config.GITHUB_REPO))

        if not branch:
            branch = repo.default_branch

        try:
            contents = repo.get_contents("", ref=branch)
        except Exception as e:
            return respond_err(f"Cannot read repo branch '{branch}': {e}", 400)

        all_files = []
        while contents:
            item = contents.pop(0)
            if item.type == "dir":
                contents.extend(repo.get_contents(item.path, ref=branch))
            else:
                all_files.append(item)

        if not all_files:
            return respond_err(f"No files found in repo on branch '{branch}'.", 400)

        # Clean existing files/folders
        os.makedirs(workdir, exist_ok=True)
        for fname in os.listdir(workdir):
            fpath = os.path.join(workdir, fname)
            if os.path.isfile(fpath):
                os.remove(fpath)
            elif os.path.isdir(fpath):
                force_rmtree(fpath)

        # Write all repo files preserving directory structure
        synced = []
        for f in all_files:
            rel_path = f.path  # Preserve full relative path from repo
            full_path = os.path.join(workdir, rel_path)
            # Create parent directories if needed
            parent_dir = os.path.dirname(full_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            try:
                content = f.decoded_content.decode("utf-8")
            except Exception:
                # Skip binary files that can't be decoded
                continue
            safe_write_file(full_path, content, base_dir=workdir)
            synced.append(rel_path)

        # Migrate state if needed
        gen_state = os.path.join(workdir, ".generated", "terraform.tfstate")
        main_state = os.path.join(workdir, "terraform.tfstate")
        state_migrated = False
        if os.path.exists(gen_state):
            if os.path.exists(main_state):
                shutil.copy2(main_state, main_state + ".bak")
            shutil.copy2(gen_state, main_state)
            state_migrated = True

        rc_init, _, err_init = run_cmd_capture(["terraform", "init", "-input=false"], cwd=workdir)

        return respond_ok({
            "synced_files": synced, "branch": branch,
            "repo": Config.GITHUB_REPO, "init_ok": rc_init == 0,
            "state_migrated": state_migrated,
        })

    except Exception as e:
        logger.error("[Modify-Sync] Failed: %s", e)
        return respond_err(f"Sync failed: {e}")


@modify_bp.route("/infra/modify/pr_status", methods=["POST"])
@require_api_key
def infra_modify_pr_status():
    """Check if a PR has been merged."""
    from config import Config
    if not Config.GITHUB_PAT or not Config.GITHUB_REPO:
        return respond_err("GitHub not configured", 400)

    body = request.json or {}
    pr_number = body.get("pr_number")
    if not pr_number:
        return respond_err("pr_number is required", 400)

    try:
        from github import Github
        g = Github(Config.GITHUB_PAT)
        repo = g.get_repo(normalize_repo(Config.GITHUB_REPO))
        pr = repo.get_pull(int(pr_number))
        return respond_ok({
            "pr_number": pr.number, "state": pr.state,
            "merged": pr.merged, "mergeable": pr.mergeable,
            "title": pr.title, "html_url": pr.html_url,
        })
    except Exception as e:
        return respond_err(f"Failed to check PR: {e}")


@modify_bp.route("/infra/modify/merge_and_deploy", methods=["POST"])
@require_api_key
def infra_modify_merge_and_deploy():
    """Merge PR and trigger GitHub Actions pipeline for deployment."""
    from config import Config
    if not Config.GITHUB_PAT or not Config.GITHUB_REPO:
        return respond_err("GitHub not configured", 400)

    body = request.json or {}
    pr_number = body.get("pr_number")
    workflow_file = body.get("workflow_file", "terraform-aws-deploy.yml")
    if not pr_number:
        return respond_err("pr_number is required", 400)

    try:
        from github import Github

        g = Github(Config.GITHUB_PAT)
        repo = g.get_repo(normalize_repo(Config.GITHUB_REPO))
        pr = repo.get_pull(int(pr_number))

        # Merge the PR
        if pr.merged:
            merge_status = "already_merged"
        elif pr.state == "closed":
            return respond_err(f"PR #{pr_number} is closed and cannot be merged.", 400)
        else:
            merge_result = pr.merge(commit_message=f"Merge: {pr.title}", merge_method="squash")
            if not merge_result.merged:
                return respond_err(f"Merge failed: {merge_result.message}", 500)
            merge_status = "merged"

        return respond_ok({
            "action": "merged_pipeline_pending",
            "merge_status": merge_status,
            "pr_number": pr.number,
            "pr_url": pr.html_url,
            "repo": Config.GITHUB_REPO,
            "workflow_file": workflow_file,
            "message": "PR merged. Pipeline will be triggered automatically on push to main, or trigger it manually.",
        })

    except Exception as e:
        logger.error("[Merge-Deploy] Failed: %s", e)
        return respond_err(str(e))
