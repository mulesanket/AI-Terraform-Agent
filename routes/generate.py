# routes/generate.py - AI infrastructure generation endpoints
import os
import re
import json
import logging
import glob

from flask import Blueprint, request

from .helpers import require_api_key, respond_ok, respond_err, get_workdir, strip_ansi, normalize_repo, find_open_pr, audit_action, write_s3_backend_tf
from .postprocessors import apply_all_postprocessors, ensure_undeclared_variables_in_files
from utils import safe_read_file, run_cmd_capture

logger = logging.getLogger("terraform-agent")


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
    """Return token pricing for the current Bedrock model family."""
    model = (model_id or "").lower()
    family = "sonnet"
    for key in ("haiku", "sonnet", "opus"):
        if key in model:
            family = key
            break
    rates = dict(_LLM_TOKEN_PRICING[family])
    rates["family"] = family
    rates["pricing_note"] = "heuristic pricing by model family"
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

def _load_all_instructions():
    """Load all instruction files from the instructions folder."""
    instructions_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "instructions"
    )
    combined = []
    if os.path.exists(instructions_dir):
        for filename in sorted(os.listdir(instructions_dir)):
            if filename.endswith('.md'):
                filepath = os.path.join(instructions_dir, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    combined.append(f"# {filename}\n{f.read()}")
    return "\n\n---\n\n".join(combined)

TERRAFORM_STANDARDS = _load_all_instructions()

# ---------- Multi-file parsing and writing ----------
def _parse_multi_file_response(response_text):
    """
    Parse LLM response containing multiple Terraform files.
    Expected format:
    --- FILE: filename.tf ---
    <content>
    --- FILE: another.tf ---
    <content>
    
    Returns dict: {filename: content}
    """
    files = {}
    
    # Pattern to match file markers: --- FILE: filename.tf ---
    pattern = r'---\s*FILE:\s*([a-zA-Z0-9_\-\.]+\.tf)\s*---'
    
    # Split by file markers
    parts = re.split(pattern, response_text)
    
    if len(parts) < 2:
        # No file markers found - treat as single file (backward compatible)
        return {"main.tf": response_text.strip()}
    
    # parts[0] is any content before first marker (usually empty or preamble)
    # parts[1], parts[3], parts[5]... are filenames
    # parts[2], parts[4], parts[6]... are file contents
    
    for i in range(1, len(parts), 2):
        if i + 1 < len(parts):
            filename = parts[i].strip()
            content = parts[i + 1].strip()
            if filename and content:
                files[filename] = content
    
    # If parsing resulted in empty files, fallback to single file
    if not files:
        return {"main.tf": response_text.strip()}
    
    return files


def _write_terraform_files(gen_dir, files_dict):
    """Write multiple Terraform files to the generation directory."""
    # Clean up old .tf files first (except terraform.tfstate files)
    for old_file in glob.glob(os.path.join(gen_dir, "*.tf")):
        try:
            os.remove(old_file)
            logger.debug("[Generate] Removed old file: %s", old_file)
        except Exception as e:
            logger.warning("[Generate] Could not remove %s: %s", old_file, e)
    
    processed = {}
    for filename, content in files_dict.items():
        content = content.encode('ascii', 'ignore').decode('ascii')
        processed[filename] = apply_all_postprocessors(content)
    processed = ensure_undeclared_variables_in_files(processed)

    written_files = []
    for filename, content in processed.items():
        filepath = os.path.join(gen_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        written_files.append(filename)
        logger.info("[Generate] Wrote %s (%d chars)", filename, len(content))
    
    return written_files


def _read_all_terraform_files(gen_dir):
    """Read all .tf files from generation directory and return combined content with markers."""
    combined = []
    tf_files = sorted(glob.glob(os.path.join(gen_dir, "*.tf")))
    
    for filepath in tf_files:
        filename = os.path.basename(filepath)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            combined.append(f"--- FILE: {filename} ---\n{content}")
        except Exception as e:
            logger.warning("[Generate] Could not read %s: %s", filepath, e)
    
    return "\n\n".join(combined)


def _get_generated_files_list(gen_dir):
    """Get list of generated .tf files."""
    tf_files = glob.glob(os.path.join(gen_dir, "*.tf"))
    return [os.path.basename(f) for f in sorted(tf_files)]


def _get_all_terraform_code(gen_dir):
    """Get dict of all terraform files and their contents."""
    files = {}
    tf_files = glob.glob(os.path.join(gen_dir, "*.tf"))
    for filepath in sorted(tf_files):
        filename = os.path.basename(filepath)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                files[filename] = f.read()
        except Exception as e:
            logger.warning("[Generate] Could not read %s: %s", filepath, e)
    return files

generate_bp = Blueprint("generate", __name__)

# ---------- System prompt for generation ----------
GENERATE_SYSTEM_PROMPT = f"""You are a senior Terraform engineer. Given a natural language description
of infrastructure, produce production-ready Terraform HCL code with PROPER FILE STRUCTURE.

## Repository Compliance Standards
{TERRAFORM_STANDARDS}

## File Structure Requirements
You MUST organize the Terraform code into multiple files following best practices:
- **providers.tf** - Provider configuration and required_providers block
- **variables.tf** - All input variable declarations
- **main.tf** - Main resource definitions OR split by resource type
- **data.tf** - All data sources (aws_ami, aws_availability_zones, etc.)
- **outputs.tf** - All output declarations
- **locals.tf** - Local values (if needed)

For larger infrastructures, split resources by type:
- **vpc.tf** - VPC, subnets, route tables, internet gateway
- **security.tf** or **sg.tf** - Security groups
- **ec2.tf** or **compute.tf** - EC2 instances, launch templates, ASG
- **s3.tf** - S3 buckets and related resources
- **iam.tf** - IAM roles and policies (only if explicitly requested)
- **rds.tf** - Database resources
- **alb.tf** or **lb.tf** - Load balancers

## Output Format
Return the code using this EXACT format with file markers:

--- FILE: providers.tf ---
<provider configuration here>

--- FILE: variables.tf ---
<variables here>

--- FILE: data.tf ---
<data sources here>

--- FILE: main.tf ---
<main resources here>

--- FILE: outputs.tf ---
<outputs here>

Use "--- FILE: filename.tf ---" markers to separate each file. Each file must contain valid HCL.

## Technical Rules
- Return ONLY valid HCL code with file markers. No markdown fences, no explanations.
- Generate COMPLETE, self-contained Terraform configuration that can run standalone.
- NEVER use `module` blocks. Do NOT reference external modules.
  All resources must be defined INLINE as resource blocks.
- Do NOT write backend.tf or a terraform backend block. The agent injects S3 remote state from Settings.
- Use variables for configurable values. ALWAYS provide sensible default values.
- Every var.NAME used in any file MUST have a matching variable "NAME" block with a default in variables.tf.
- For aws_kms_alias data sources, either hardcode name = "alias/aws/ebs" (or alias/aws/s3) or declare variable "kms_alias_name" with type string and default "alias/aws/ebs".
- Follow security best practices: encryption at rest, no public access unless requested, tags.
- Use current Terraform syntax (1.0+). Use separate resource blocks instead of inline deprecated blocks.
- For S3: aws_s3_bucket must ONLY contain bucket, tags, force_destroy. Use separate resources for versioning, encryption, public access block.
- Do NOT use random_id, timestamps, or numeric/hex suffixes on tags.Name, resource name, or S3 bucket. Use stable, human-readable names exactly as the user described.
- For aws_eip: Use `domain = "vpc"` instead of `vpc = true`.
- For EC2: NEVER hardcode AMI IDs. ALWAYS use aws_ami data source.
- For SSH key pairs: Use tls_private_key resource to generate keys dynamically.
- For aws_instance: Use `vpc_security_group_ids` (NOT `security_groups`).
- IAM Roles: Do NOT create unless user EXPLICITLY asks.
- Route53/DNS: Do NOT include unless user provides a REAL domain.

IMPORTANT: Your code will be run through `terraform init`, `terraform validate`, and `terraform plan`.
Triple-check attribute names, resource syntax, and references.
"""


def _heal_deploy_error(llm, gen_dir, error_text, attempt):
    """Send deploy/plan errors back to the LLM to fix the code (multi-file support)."""
    try:
        current_code = _read_all_terraform_files(gen_dir)

        heal_prompt = (
            "The following Terraform code FAILED during `terraform apply` (deployment). "
            "These are RUNTIME errors from the AWS API, not just validation errors. "
            "You MUST fix the root cause. Common issues:\n"
            "- InvalidAMIID.NotFound: NEVER hardcode AMI IDs. Use an aws_ami data source.\n"
            "- Key pair errors: Use tls_private_key resource instead of hardcoded public keys.\n"
            "- Resource already exists: keep the same names; do not add random suffixes or random_id.\n"
            "- Permission denied: Check IAM role/policy references.\n\n"
            "Fix ALL errors and return the COMPLETE corrected HCL code using the same multi-file format:\n"
            "--- FILE: filename.tf ---\n<content>\n\n"
            "Return ONLY valid HCL with file markers, no markdown fences or explanations.\n\n"
            f"## Deploy Errors\n{error_text[:3000]}\n\n"
            f"## Current Code\n{current_code}"
        )

        logger.info("[Deploy-Heal] Attempt %d — sending errors to LLM...", attempt)
        new_code = llm.chat_hcl(GENERATE_SYSTEM_PROMPT, heal_prompt)
        logger.info("[Deploy-Heal] LLM returned %d chars", len(new_code))

        # Parse and write multi-file response
        files_dict = _parse_multi_file_response(new_code)
        _write_terraform_files(gen_dir, files_dict)

        run_cmd_capture(["terraform", "init", "-input=false"], cwd=gen_dir)

        rc_val, val_out, _ = run_cmd_capture(["terraform", "validate", "-json"], cwd=gen_dir)
        try:
            validation = json.loads(val_out)
            valid = validation.get("valid", False)
        except (json.JSONDecodeError, TypeError):
            valid = rc_val == 0

        if valid:
            logger.info("[Deploy-Heal] Regenerated code passes validation")
            return True
        else:
            logger.warning("[Deploy-Heal] Regenerated code still has validation errors")
            return False
    except Exception as e:
        logger.error("[Deploy-Heal] Failed: %s", e)
        return False


def _suggest_next_steps(error_text):
    """Analyze apply/plan errors and return actionable next-step suggestions."""
    suggestions = []
    err_lower = error_text.lower()

    if "invalidamiid" in err_lower or ("does not exist" in err_lower and "ami-" in err_lower):
        suggestions.append({"title": "Invalid AMI ID", "detail": "The AMI ID is region-specific or no longer exists. Click 'Regenerate'.", "action": "regenerate"})
    if "invalidparametervalue" in err_lower and "publickeymaterial" in err_lower:
        suggestions.append({"title": "Invalid SSH Public Key", "detail": "The public key was a placeholder. Click 'Regenerate'.", "action": "regenerate"})
    if "already exists" in err_lower or "alreadyexists" in err_lower:
        suggestions.append({"title": "Resource Already Exists", "detail": "A resource with this name already exists in AWS. Import it, destroy the old stack, or pick a different name in the prompt.", "action": "regenerate"})
    if "accessdenied" in err_lower or "unauthorizedaccess" in err_lower or "not authorized" in err_lower:
        suggestions.append({"title": "Permission Denied", "detail": "Check your IAM role/policy and ensure required permissions.", "action": "check_permissions"})
    if "iam" in err_lower and ("createrole" in err_lower or "putrole" in err_lower or "createpolicy" in err_lower):
        suggestions.append({"title": "Cannot Create IAM Roles (SSO Restriction)", "detail": "Your SSO role cannot create IAM roles. Click 'Regenerate'.", "action": "regenerate"})
    if "expired" in err_lower and ("token" in err_lower or "credential" in err_lower):
        suggestions.append({"title": "AWS Credentials Expired", "detail": "Refresh your credentials in Settings.", "action": "sso_login"})
    if "quota" in err_lower or "limit exceeded" in err_lower or "limitexceeded" in err_lower:
        suggestions.append({"title": "Service Quota Exceeded", "detail": "Request a quota increase in AWS console.", "action": "manual"})
    if not suggestions:
        suggestions.append({"title": "Deployment Failed", "detail": "Review the error output. Try 'Regenerate' with a modified prompt.", "action": "regenerate"})
    return suggestions


@generate_bp.route("/generate", methods=["POST"])
@require_api_key
def generate_infra():
    """Generate NEW Terraform code from a natural language prompt."""
    from config import Config

    body = request.json or {}
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return respond_err("prompt is required", 400)
    if len(prompt) > 5000:
        return respond_err("prompt too long (max 5000 chars)", 400)

    logger.info("[Generate] Prompt received: %s", prompt[:120])

    from llm import get_llm_client
    llm, llm_err = get_llm_client()
    if not llm:
        logger.error("[Generate] LLM not available: %s", llm_err)
        return respond_err(f"LLM not configured: {llm_err}", 400)

    workdir = get_workdir()
    gen_dir = os.path.join(workdir, ".generated")
    os.makedirs(gen_dir, exist_ok=True)

    user_prompt = f"## User Request\n{prompt}\n\nGenerate the complete Terraform configuration with proper file structure."
    llm_model_id = getattr(llm, "model_id", Config.BEDROCK_MODEL_ID)
    token_calls = []

    try:
        logger.info("[Generate] Calling LLM (%s)...", Config.LLM_PROVIDER)
        llm_result = llm.chat_hcl_with_metadata(GENERATE_SYSTEM_PROMPT, user_prompt)
        code = llm_result["text"]
        token_calls.append(
            _build_token_utilization(
                model_id=llm_model_id,
                system_prompt=GENERATE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                output_text=code,
                usage=llm_result.get("usage"),
            )
        )
        logger.info("[Generate] LLM returned %d chars of code", len(code))

        # Parse multi-file response and write files
        files_dict = _parse_multi_file_response(code)
        written_files = _write_terraform_files(gen_dir, files_dict)
        backend_file = write_s3_backend_tf(gen_dir)
        if backend_file:
            written_files.append(backend_file)
        logger.info("[Generate] Wrote %d files: %s", len(written_files), written_files)

        # Get final list of files and their contents
        final_files = _get_all_terraform_code(gen_dir)
        final_file_list = list(final_files.keys())

        # Aggregate token usage
        token_utilization = None
        if token_calls:
            rates = _llm_rate_for_model(llm_model_id)
            agg_input = sum(c["input_tokens"] for c in token_calls)
            agg_output = sum(c["output_tokens"] for c in token_calls)
            agg_total = agg_input + agg_output
            token_utilization = {
                "model_id": llm_model_id,
                "model_family": rates["family"],
                "source": token_calls[0]["source"],
                "input_tokens": agg_input,
                "output_tokens": agg_output,
                "total_tokens": agg_total,
                "input_cost": round((agg_input / 1000.0) * rates["input_per_1k"], 6),
                "output_cost": round((agg_output / 1000.0) * rates["output_per_1k"], 6),
                "estimated_cost": round(
                    (agg_input / 1000.0) * rates["input_per_1k"]
                    + (agg_output / 1000.0) * rates["output_per_1k"], 6
                ),
                "note": "Estimated from Bedrock token usage when available; otherwise approximated from prompt/output length.",
            }

        audit_action("generate", target=prompt[:200],
                     outcome="success",
                     details={"files": final_file_list})

        return respond_ok({
            "code": _read_all_terraform_files(gen_dir),
            "files": final_files,
            "file_list": final_file_list,
            "prompt": prompt,
            "token_utilization": token_utilization,
        })

    except Exception as e:
        logger.error("Generate failed: %s", e)
        audit_action("generate", target=prompt[:200], outcome="failure", details={"error": str(e)})
        return respond_err(f"Generation failed: {e}")


@generate_bp.route("/generate/apply", methods=["POST"])
@require_api_key
def generate_apply():
    """Push generated code to repo and trigger GitHub Actions pipeline for deploy."""
    from config import Config

    workdir = get_workdir()
    gen_dir = os.path.join(workdir, ".generated")

    from .helpers import write_s3_backend_tf, normalize_repo, find_open_pr, get_tf_state_settings
    write_s3_backend_tf(gen_dir)

    tf_files = _get_all_terraform_code(gen_dir)
    if not tf_files:
        return respond_err("No Terraform files found. Run /generate first.", 400)
    tf_files = ensure_undeclared_variables_in_files(tf_files)
    for filename, content in tf_files.items():
        with open(os.path.join(gen_dir, filename), "w", encoding="utf-8") as handle:
            handle.write(content)

    if not Config.GITHUB_PAT or not Config.GITHUB_REPO:
        return respond_err("GitHub PAT and GITHUB_REPO must be configured in Settings.", 400)

    state_bucket, _, _ = get_tf_state_settings()
    if not state_bucket:
        return respond_err(
            "Set Terraform State S3 Bucket in Settings before Deploy via Pipeline.",
            400,
        )

    body = request.json or {}
    prompt_desc = body.get("prompt", "New infrastructure generation")
    target_folder = body.get("target_folder", "")
    workflow_file = body.get("workflow_file", "terraform-aws-deploy.yml")

    try:
        from github import Github
        import time as _time

        g = Github(Config.GITHUB_PAT)
        repo = g.get_repo(normalize_repo(Config.GITHUB_REPO))
        default_branch = repo.default_branch

        try:
            ref = repo.get_git_ref(f"heads/{default_branch}")
        except Exception:
            repo.create_file("README.md", "Initial commit — Terraform Agent",
                             "# Terraform Infrastructure\n\nManaged by AI Terraform Agent.\n",
                             branch=default_branch)
            ref = repo.get_git_ref(f"heads/{default_branch}")

        existing_pr, existing_branch = find_open_pr(repo, "generate-infra-")

        if existing_pr:
            branch_name = existing_branch
        else:
            branch_name = f"generate-infra-{int(_time.time())}"
            repo.create_git_ref(f"refs/heads/{branch_name}", ref.object.sha)

        # Push all generated files
        pushed_files = []
        for filename, content in tf_files.items():
            target_path = f"{target_folder}/{filename}" if target_folder else filename
            try:
                existing = repo.get_contents(target_path, ref=branch_name)
                repo.update_file(target_path, f"generate: {filename}", content, existing.sha, branch=branch_name)
            except Exception:
                repo.create_file(target_path, f"generate: {filename}", content, branch=branch_name)
            pushed_files.append(target_path)

        from .helpers import sync_tf_state_github_variables
        sync_tf_state_github_variables(Config.GITHUB_PAT, Config.GITHUB_REPO)
        try:
            from .github_ops import ensure_workflow_on_default_branch
            ensure_workflow_on_default_branch(Config.GITHUB_PAT, normalize_repo(Config.GITHUB_REPO), "terraform-aws-destroy.yml")
            ensure_workflow_on_default_branch(Config.GITHUB_PAT, normalize_repo(Config.GITHUB_REPO), "terraform-aws-deploy.yml")
        except Exception as exc:
            logger.warning("Could not sync GitHub Actions workflow files: %s", exc)

        # Create PR if not existing
        if not existing_pr:
            pr = repo.create_pull(
                title=f"Generate Infra: {prompt_desc[:80]}",
                body=(f"## Generated Infrastructure\n**Prompt:** {prompt_desc}\n\n"
                      f"### Files\n" + "\n".join(f"- `{f}`" for f in pushed_files) + "\n\n"
                      "Pipeline will run `terraform plan` and `terraform apply` automatically.\n\n"
                      "*Auto-generated by Terraform Agent*"),
                head=branch_name, base=default_branch,
            )
            pr_url = pr.html_url
            pr_number = pr.number
        else:
            existing_pr.create_issue_comment(
                f"## Updated Generation\n**Prompt:** {prompt_desc}\n\n"
                f"**Files:** {', '.join(pushed_files)}\n\n*Auto-updated by Terraform Agent*"
            )
            pr_url = existing_pr.html_url
            pr_number = existing_pr.number

        audit_action("generate_deploy", target=prompt_desc[:200],
                     outcome="pipeline_triggered",
                     details={"branch": branch_name, "files": pushed_files})

        return respond_ok({
            "action": "pipeline_triggered",
            "pr_url": pr_url,
            "pr_number": pr_number,
            "branch": branch_name,
            "pushed_files": pushed_files,
            "repo": Config.GITHUB_REPO,
            "workflow_file": workflow_file,
        })

    except Exception as e:
        logger.error("Generate apply (pipeline) failed: %s", e)
        return respond_err(f"Failed to push code and trigger pipeline: {e}")


@generate_bp.route("/generate/destroy", methods=["POST"])
@require_api_key
def generate_destroy():
    """Destroy the latest deployed generate-infra-* stack via GitHub Actions."""
    from .github_ops import trigger_terraform_destroy_pipeline

    body = request.json or {}
    try:
        return trigger_terraform_destroy_pipeline(
            repo_override=body.get("repo"),
            branch=body.get("branch"),
            confirm=bool(body.get("confirm") or body.get("confirm_destroy")),
        )
    except Exception as e:
        logger.exception("Generate destroy pipeline failed")
        return respond_err(f"Failed to trigger destroy pipeline: {e}")


@generate_bp.route("/generate/pr", methods=["POST"])
@require_api_key
def generate_create_pr():
    """Create a GitHub PR with the generated Terraform code (supports multiple files)."""
    from config import Config

    if not Config.GITHUB_PAT or not Config.GITHUB_REPO:
        return respond_err("GitHub PAT and GITHUB_REPO must be configured in Settings.", 400)

    workdir = get_workdir()
    gen_dir = os.path.join(workdir, ".generated")
    
    # Get all generated .tf files
    tf_files = _get_all_terraform_code(gen_dir)
    if not tf_files:
        return respond_err("No Terraform files found. Run /generate first.", 400)
    tf_files = ensure_undeclared_variables_in_files(tf_files)
    for filename, content in tf_files.items():
        with open(os.path.join(gen_dir, filename), "w", encoding="utf-8") as handle:
            handle.write(content)

    body = request.json or {}
    prompt_desc = body.get("prompt", "New infrastructure generation")
    target_folder = body.get("target_folder", "terraform")  # Default folder in repo

    try:
        from github import Github
        import time as _time

        g = Github(Config.GITHUB_PAT)
        repo = g.get_repo(normalize_repo(Config.GITHUB_REPO))
        default_branch = repo.default_branch

        # Handle empty repos
        try:
            ref = repo.get_git_ref(f"heads/{default_branch}")
        except Exception:
            repo.create_file("README.md", "Initial commit — Terraform Agent",
                             "# Terraform Infrastructure\n\nManaged by AI Terraform Agent.\n",
                             branch=default_branch)
            ref = repo.get_git_ref(f"heads/{default_branch}")

        existing_pr, existing_branch = find_open_pr(repo, "generate-infra-")

        if existing_pr:
            branch_name = existing_branch
            
            # Update/create each file
            for filename, content in tf_files.items():
                target_path = f"{target_folder}/{filename}" if target_folder else filename
                try:
                    existing = repo.get_contents(target_path, ref=branch_name)
                    repo.update_file(target_path, f"update: {filename}", content, existing.sha, branch=branch_name)
                except Exception:
                    repo.create_file(target_path, f"add: {filename}", content, branch=branch_name)

            file_list = ", ".join(tf_files.keys())
            existing_pr.create_issue_comment(
                f"## Updated Infrastructure\n**Change:** {prompt_desc}\n\n"
                f"**Files:** {file_list}\n\n"
                f"New commit pushed to branch `{branch_name}`.\n\n*Auto-updated by Terraform Agent*"
            )

            _run_cost_estimate_and_comment_pr(repo, existing_pr, workdir)

            return respond_ok({
                "action": "pr_updated", "pr_url": existing_pr.html_url,
                "pr_number": existing_pr.number, "branch": branch_name,
                "files": list(tf_files.keys()),
            })
        else:
            branch_name = f"generate-infra-{int(_time.time())}"
            repo.create_git_ref(f"refs/heads/{branch_name}", ref.object.sha)

            # Create each file
            for filename, content in tf_files.items():
                target_path = f"{target_folder}/{filename}" if target_folder else filename
                try:
                    existing = repo.get_contents(target_path, ref=branch_name)
                    repo.update_file(target_path, f"generate: {filename}", content, existing.sha, branch=branch_name)
                except Exception:
                    repo.create_file(target_path, f"generate: {filename}", content, branch=branch_name)

            file_list = ", ".join(tf_files.keys())
            pr = repo.create_pull(
                title=f"Generate Infra: {prompt_desc[:80]}",
                body=(f"## Generated Infrastructure\n{prompt_desc}\n\n"
                      f"**Files:** {file_list}\n\n"
                      "- Terraform validate: **passed**\n- Terraform plan: **generated**\n\n"
                      "*Auto-generated by Terraform Agent*"),
                head=branch_name, base=default_branch,
            )

            _run_cost_estimate_and_comment_pr(repo, pr, workdir)

            return respond_ok({
                "action": "pr_created", "pr_url": pr.html_url,
                "pr_number": pr.number, "branch": branch_name,
                "files": list(tf_files.keys()),
            })
    except Exception as e:
        logger.error("[Generate] PR creation failed: %s", e)
        return respond_err(f"PR creation failed: {e}")


def _run_cost_estimate_and_comment_pr(repo, pr, workdir):
    """Run free AWS Pricing API cost estimation and post as PR comment."""
    try:
        from cost_estimator import estimate_from_workdir, format_cost_markdown

        gen_dir = os.path.join(workdir, ".generated") if os.path.isdir(os.path.join(workdir, ".generated")) else workdir
        estimate = estimate_from_workdir(gen_dir)

        if estimate.get("error"):
            logger.warning("[CostEstimate] Estimation failed: %s", estimate["error"])
            return

        comment_body = format_cost_markdown(estimate)
        pr.create_issue_comment(comment_body)
        logger.info("[CostEstimate] Posted cost comment on PR #%d — $%.2f/month", pr.number, estimate["total_monthly"])
    except Exception as e:
        logger.error("[CostEstimate] Failed to post cost comment: %s", e)
