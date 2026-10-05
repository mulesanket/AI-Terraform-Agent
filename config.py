# config.py - Centralized configuration for the Self-Healing Terraform Agent
import os
import secrets
from dotenv import load_dotenv

load_dotenv()

# boto3 crashes if AWS_PROFILE is an empty string in the environment.
# Remove it so boto3 falls back to explicit credentials / default chain.
if not os.environ.get("AWS_PROFILE"):
    os.environ.pop("AWS_PROFILE", None)


class Config:
    # Server
    MCP_PORT = int(os.environ.get("MCP_PORT", 8080))
    API_KEY = os.environ.get("API_KEY")

    # Flask session secret (auto-generated if not provided)
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    # OAuth - GitHub
    GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
    GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")

    # OAuth - Google
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    # Workspace
    AGENT_WORKDIR = os.environ.get(
        "AGENT_WORKDIR", os.path.join(os.getcwd(), "workspace")
    )

    # Approval mode: "pr" = create PR for review, "auto" = auto-approve non-destructive
    APPROVAL_MODE = os.environ.get("APPROVAL_MODE", "pr")

    # LLM Provider: 'bedrock'
    LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "bedrock")

    # AWS SSO / Profile — for Terraform deployments (per-user)
    AWS_PROFILE = os.environ.get("AWS_PROFILE", "")

    # Amazon Bedrock — Bearer token auth (shared, server-level)
    # Set via: export AWS_BEARER_TOKEN_BEDROCK=<your-api-key>
    AWS_BEARER_TOKEN_BEDROCK = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    BEDROCK_MODEL_ID = os.environ.get(
        "BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-5"
    )
    BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-2")

    # GitHub
    GITHUB_PAT = os.environ.get("GITHUB_PAT")
    GITHUB_REPO = os.environ.get("GITHUB_REPO")

    # Terraform remote state (S3)
    TF_STATE_BUCKET = os.environ.get("TF_STATE_BUCKET", "")
    TF_STATE_KEY = os.environ.get("TF_STATE_KEY", "")
    TF_STATE_REGION = os.environ.get("TF_STATE_REGION", "us-east-1")

    # Scheduler
    DRIFT_CHECK_INTERVAL_MINUTES = int(
        os.environ.get("DRIFT_CHECK_INTERVAL_MINUTES", 30)
    )

    # Notifications
    SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

    # Infracost
    INFRACOST_API_KEY = os.environ.get("INFRACOST_API_KEY")

    # Cost threshold (dollars) - block apply if estimated cost exceeds this
    COST_THRESHOLD = float(os.environ.get("COST_THRESHOLD", 100))
