# orchestrator.py - LLM-driven self-healing orchestration engine
import os
import glob
import json
import logging
import datetime

from config import Config
from llm import get_llm_client
from utils import run_cmd_capture, safe_read_file, safe_write_file
from notifications import notify

logger = logging.getLogger("terraform-agent.orchestrator")

SYSTEM_PROMPT = """You are a Terraform expert. Given a drift report showing infrastructure
changes detected by `terraform plan` and the current HCL source code, produce corrected
Terraform code that eliminates the drift and aligns the code with the actual desired state.

Rules:
- Return ONLY valid HCL code. No markdown fences, no explanations.
- Preserve existing resource names, structure, and formatting style.
- Only modify what is necessary to resolve the reported drift.
- Follow Terraform and AWS best practices (encryption, public access blocks, tagging).
"""


class HealingOrchestrator:
    """
    Full self-healing pipeline:
    detect_drift → generate_fix → validate → re-plan → apply or create PR
    """

    def __init__(self, workdir=None):
        self.workdir = workdir or Config.AGENT_WORKDIR
        self.client, self._llm_err = get_llm_client()

    # ------------------------------------------------------------------ #
    #  Step 1: Detect drift via terraform plan
    # ------------------------------------------------------------------ #
    def detect_drift(self):
        """Run terraform init + plan + show -json and extract resource changes."""
        logger.info("Detecting drift in %s", self.workdir)

        # Init
        rc, out, err = run_cmd_capture(
            ["terraform", "init", "-input=false"], cwd=self.workdir
        )
        if rc != 0:
            logger.error("terraform init failed: %s", err)
            return {"error": f"terraform init failed: {err}", "changes": []}

        # Plan
        rc, out, err = run_cmd_capture(
            ["terraform", "plan", "-out", "plan.out", "-input=false", "-detailed-exitcode"],
            cwd=self.workdir,
        )
        # rc=0 no changes, rc=2 changes present, rc=1 error
        if rc == 0:
            logger.info("No drift detected")
            return {"changes": [], "has_drift": False}
        if rc == 1:
            logger.error("terraform plan error: %s", err)
            return {"error": f"terraform plan failed: {err}", "changes": []}

        # Show JSON
        rc2, plan_json_str, err2 = run_cmd_capture(
            ["terraform", "show", "-json", "plan.out"], cwd=self.workdir
        )
        if rc2 != 0:
            return {"error": f"terraform show failed: {err2}", "changes": []}

        try:
            plan_data = json.loads(plan_json_str)
        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse plan JSON: {e}", "changes": []}

        changes = self._extract_changes(plan_data)
        logger.info("Drift detected: %d resource change(s)", len(changes))
        return {"changes": changes, "has_drift": True, "plan_data": plan_data}

    # ------------------------------------------------------------------ #
    #  Step 2: Ask LLM to generate corrective HCL
    # ------------------------------------------------------------------ #
    def generate_fix(self, drift_report, current_hcl):
        """Send drift report + current HCL to LLM and receive corrected code."""
        if not self.client:
            return None, self._llm_err or "LLM not configured"

        user_prompt = (
            f"## Drift Report\n```json\n{json.dumps(drift_report, indent=2, default=str)}\n```\n\n"
            f"## Current HCL Code\n```hcl\n{current_hcl}\n```\n\n"
            "Produce the corrected HCL code that resolves the drift shown above."
        )

        try:
            fix = self.client.chat_hcl(SYSTEM_PROMPT, user_prompt)
            logger.info("LLM generated fix (%d chars) via %s", len(fix), Config.LLM_PROVIDER)
            return fix, None
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return None, str(e)

    # ------------------------------------------------------------------ #
    #  Step 3: Validate the generated HCL
    # ------------------------------------------------------------------ #
    def validate_fix(self):
        """Run terraform validate on the workdir. Returns (success, output)."""
        rc, out, err = run_cmd_capture(
            ["terraform", "validate", "-json"], cwd=self.workdir
        )
        try:
            result = json.loads(out)
            valid = result.get("valid", False)
        except (json.JSONDecodeError, AttributeError):
            valid = rc == 0
            result = {"stdout": out, "stderr": err}

        if valid:
            logger.info("Terraform validation passed")
        else:
            logger.warning("Terraform validation failed: %s", result)
        return valid, result

    # ------------------------------------------------------------------ #
    #  Step 4: Classify risk level of changes
    # ------------------------------------------------------------------ #
    def classify_risk(self, changes):
        """
        Return 'low' for safe changes (update, tag changes).
        Return 'high' for destructive or security-impacting changes.
        """
        high_risk_actions = {"delete", "create"}
        high_risk_types = {
            "aws_security_group", "aws_iam_role", "aws_iam_policy",
            "aws_iam_role_policy_attachment", "aws_vpc", "aws_subnet",
            "aws_route_table", "aws_db_instance", "aws_rds_cluster",
        }

        for change in changes:
            actions = set(change.get("actions", []))
            resource_type = change.get("type", "")
            if actions & high_risk_actions:
                return "high"
            if resource_type in high_risk_types:
                return "high"
        return "low"

    # ------------------------------------------------------------------ #
    #  Step 5: Apply the fix
    # ------------------------------------------------------------------ #
    def apply_fix(self):
        """Run terraform apply on existing plan file."""
        rc, out, err = run_cmd_capture(
            ["terraform", "apply", "-input=false", "-auto-approve", "plan.out"],
            cwd=self.workdir,
        )
        success = rc == 0
        if success:
            logger.info("terraform apply succeeded")
        else:
            logger.error("terraform apply failed: %s", err)
        return success, {"rc": rc, "stdout": out, "stderr": err}

    # ------------------------------------------------------------------ #
    #  Step 6: Create a PR for human review
    # ------------------------------------------------------------------ #
    def create_pr(self, branch_name, commit_msg, changes_summary):
        """Git branch + commit + push + GitHub PR (or update existing)."""
        repo_path = self.workdir

        # Check for an existing open drift-fix PR to update
        existing_pr = None
        if Config.GITHUB_PAT and Config.GITHUB_REPO:
            try:
                from github import Github
                gh = Github(Config.GITHUB_PAT)
                repo = gh.get_repo(Config.GITHUB_REPO)
                open_prs = repo.get_pulls(state="open", sort="created", direction="desc")
                for pr in open_prs:
                    if pr.head.ref.startswith("drift-fix/"):
                        existing_pr = pr
                        branch_name = pr.head.ref
                        logger.info("Found existing drift-fix PR #%d on branch %s", pr.number, branch_name)
                        break
            except Exception as e:
                logger.warning("Could not search for existing PRs: %s", e)

        if existing_pr:
            # Switch to the existing branch
            cmds = [
                ["git", "fetch", "origin", branch_name],
                ["git", "checkout", branch_name],
                ["git", "add", "."],
                ["git", "commit", "-m", commit_msg],
                ["git", "push", "origin", branch_name],
            ]
        else:
            # Create a new branch
            cmds = [
                ["git", "checkout", "-b", branch_name],
                ["git", "add", "."],
                ["git", "commit", "-m", commit_msg],
                ["git", "push", "--set-upstream", "origin", branch_name],
            ]

        for cmd in cmds:
            rc, out, err = run_cmd_capture(cmd, cwd=repo_path)
            if rc != 0:
                logger.error("Git command failed: %s — %s", cmd, err)
                return {"error": f"Git failed at {cmd}: {err}"}

        # Create or update GitHub PR
        pr_url = None
        if Config.GITHUB_PAT and Config.GITHUB_REPO:
            try:
                from github import Github
                gh = Github(Config.GITHUB_PAT)
                repo = gh.get_repo(Config.GITHUB_REPO)

                if existing_pr:
                    existing_pr.create_issue_comment(
                        f"## Updated Drift Fix\n\n{changes_summary}\n\n"
                        f"New commit pushed to branch `{branch_name}`.\n\n"
                        f"*Auto-updated by Terraform Agent*"
                    )
                    pr_url = existing_pr.html_url
                    logger.info("PR updated: %s", pr_url)
                else:
                    pr = repo.create_pull(
                        title=f"[Auto-Heal] {commit_msg}",
                        body=f"## Automated Drift Fix\n\n{changes_summary}",
                        head=branch_name,
                        base="main",
                    )
                    pr_url = pr.html_url
                    logger.info("PR created: %s", pr_url)
                    existing_pr = pr

                # Post cost estimate as PR comment
                try:
                    from server import _run_cost_estimate_and_comment_pr
                    _run_cost_estimate_and_comment_pr(repo, existing_pr, self.workdir)
                except Exception as ic_err:
                    logger.warning("Cost estimate comment skipped: %s", ic_err)

            except Exception as e:
                logger.error("GitHub PR creation failed: %s", e)
                return {"error": f"PR creation failed: {e}", "branch": branch_name}

        return {"branch": branch_name, "pr_url": pr_url}

    # ------------------------------------------------------------------ #
    #  Step 7: Rollback on failure
    # ------------------------------------------------------------------ #
    def rollback(self, branch_name):
        """Attempt to revert the last commit and switch back to main."""
        logger.warning("Rolling back branch %s", branch_name)
        run_cmd_capture(["git", "checkout", "main"], cwd=self.workdir)
        run_cmd_capture(["git", "branch", "-D", branch_name], cwd=self.workdir)

    # ------------------------------------------------------------------ #
    #  Full healing pipeline
    # ------------------------------------------------------------------ #
    def heal(self):
        """
        Complete self-healing pipeline:
        1. Detect drift
        2. Read current HCL
        3. Generate LLM fix
        4. Write fix + validate
        5. Re-plan
        6. Classify risk → auto-apply or create PR
        """
        result = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "workdir": self.workdir,
            "steps": {},
        }

        # 1. Detect drift
        drift = self.detect_drift()
        result["steps"]["detect_drift"] = drift
        if not drift.get("has_drift"):
            result["outcome"] = "no_drift"
            return result

        notify(
            "drift_detected",
            {"summary": f"Drift detected: {len(drift['changes'])} change(s) in {self.workdir}"},
            Config.SLACK_WEBHOOK_URL,
        )

        # 2. Read current HCL files
        tf_files = glob.glob(os.path.join(self.workdir, "*.tf"))
        current_hcl = ""
        for tf_file in tf_files:
            content = safe_read_file(tf_file)
            current_hcl += f"# --- {os.path.basename(tf_file)} ---\n{content}\n\n"

        # 3. Generate fix via LLM
        fix, llm_error = self.generate_fix(drift["changes"], current_hcl)
        if llm_error:
            result["steps"]["generate_fix"] = {"error": llm_error}
            result["outcome"] = "llm_failed"
            notify("fix_failed", {"summary": f"LLM fix generation failed: {llm_error}"}, Config.SLACK_WEBHOOK_URL)
            return result

        result["steps"]["generate_fix"] = {"chars": len(fix)}
        notify("fix_generated", {"summary": f"Generated fix ({len(fix)} chars)"}, Config.SLACK_WEBHOOK_URL)

        # 4. Write fix to main.tf and validate
        main_tf = os.path.join(self.workdir, "main.tf")
        safe_write_file(main_tf, fix)

        valid, validation = self.validate_fix()
        result["steps"]["validate"] = {"valid": valid, "detail": validation}
        if not valid:
            result["outcome"] = "validation_failed"
            notify("fix_failed", {"summary": "Generated fix failed terraform validate"}, Config.SLACK_WEBHOOK_URL)
            return result

        # 5. Re-plan to confirm fix resolves drift
        replan = self.detect_drift()
        result["steps"]["replan"] = replan

        # 6. Classify risk and decide action
        risk = self.classify_risk(drift["changes"])
        result["steps"]["risk"] = risk

        timestamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        branch_name = f"auto-heal/{timestamp}"
        commit_msg = f"fix: auto-heal drift for {len(drift['changes'])} resource(s)"
        changes_summary = "\n".join(
            f"- `{c['address']}`: {c['actions']}" for c in drift["changes"]
        )

        if Config.APPROVAL_MODE == "auto" and risk == "low":
            # Auto-apply for low-risk changes
            success, apply_result = self.apply_fix()
            result["steps"]["apply"] = apply_result
            if success:
                result["outcome"] = "auto_applied"
                notify("fix_applied", {"summary": f"Auto-applied fix for {len(drift['changes'])} resource(s)"}, Config.SLACK_WEBHOOK_URL)
            else:
                result["outcome"] = "apply_failed"
                notify("fix_failed", {"summary": f"terraform apply failed"}, Config.SLACK_WEBHOOK_URL)
        else:
            # Create PR for review
            pr_result = self.create_pr(branch_name, commit_msg, changes_summary)
            result["steps"]["pr"] = pr_result
            if pr_result.get("pr_url"):
                result["outcome"] = "pr_created"
                notify("pr_created", {"summary": f"PR created: {pr_result['pr_url']}"}, Config.SLACK_WEBHOOK_URL)
            else:
                result["outcome"] = "pr_branch_created"
                notify("approval_needed", {"summary": f"Branch created: {branch_name}"}, Config.SLACK_WEBHOOK_URL)

        return result

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_changes(plan_data):
        """Extract meaningful resource changes from terraform plan JSON."""
        changes = []
        resources = plan_data.get("resource_changes", []) if isinstance(plan_data, dict) else []
        for rc in resources:
            actions = rc.get("change", {}).get("actions", [])
            if set(actions) - {"no-op", "read"}:
                changes.append({
                    "address": rc.get("address"),
                    "actions": actions,
                    "type": rc.get("type"),
                    "name": rc.get("name"),
                    "before": rc.get("change", {}).get("before"),
                    "after": rc.get("change", {}).get("after"),
                })
        return changes
