# Self-Healing Terraform Agent

An AI-driven automation platform with a full web UI that generates, deploys, and continuously monitors cloud infrastructure using LLM-powered Terraform code generation, drift detection, self-healing, and cost optimization — all without requiring manual IaC expertise.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Web Dashboard (Flask UI)                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │ Generate │ │  Modify  │ │  Drift   │ │ Security │ │ Git  │ │
│  │   Page   │ │   Page   │ │   Page   │ │ & Costs  │ │  Hub │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────┘ │
└───────────────────────────┬─────────────────────────────────────┘
                            │ REST API
┌───────────────────────────▼─────────────────────────────────────┐
│                    Flask Blueprint Server                         │
│                                                                   │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐   │
│  │ Scheduler│──>│  Detect  │──>│ Generate │──>│  Validate  │   │
│  │ (cron)   │   │  Drift   │   │ LLM Fix  │   │ + Self-Heal│   │
│  └──────────┘   └──────────┘   └──────────┘   └─────┬──────┘   │
│                                                       │          │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌─────▼──────┐  │
│  │  Notify  │<──│  Apply / │<──│  Policy  │<──│  Plan +    │   │
│  │  Slack   │   │ Create PR│   │  Gates   │   │State Lock  │   │
│  └──────────┘   └──────────┘   └──────────┘   └────────────┘   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐
│  Checkov    │  │ AWS Cost │  │   OPA    │  │   GitHub    │
│  Security   │  │  Pricing │  │  Policy  │  │ OAuth + API │
└─────────────┘  └──────────┘  └──────────┘  └─────────────┘
```

## Features

| Category | Feature |
|---|---|
| **Web Dashboard** | Full-featured UI with login, dashboard, generate, modify, drift, security, and settings pages |
| **AI Code Generation** | Natural language → production-ready Terraform via Amazon Bedrock (Claude/Nova) |
| **Self-Healing Loop** | Validation failures auto-sent back to LLM for correction (up to 3 retries) |
| **Deploy-Time Healing** | Apply failures trigger LLM re-generation and retry automatically |
| **21 Post-Processors** | Automated fixes for common LLM mistakes (deprecated attrs, hardcoded AMIs, placeholder SGs, etc.) |
| **Modify Existing Infra** | AI-powered modification of existing Terraform code with diff preview |
| **Drift Detection** | Periodic `terraform plan` with automatic JSON parsing and alerting |
| **Cost Estimation** | Free AWS Pricing API integration (no Infracost CLI needed) |
| **Security Scanning** | Checkov integration for policy compliance |
| **Policy-as-Code** | OPA/Rego policy gates before apply |
| **Plan Approval Workflow** | Gated approvals — plans require explicit approval before apply |
| **State Locking** | File-based state locks prevent concurrent terraform operations |
| **Audit Trail** | SQLite-backed audit log of all generate/deploy/heal/modify actions |
| **GitHub Integration** | OAuth login, PR creation, code sync, merge & deploy |
| **Notifications** | Slack webhook alerts for drift, fixes, failures |
| **Multi-User Auth** | GitHub OAuth + local accounts with admin/user roles |
| **Rate Limiting** | Per-endpoint rate limits on destructive operations |
| **Containerized** | Dockerfile + docker-compose for easy deployment |
| **CI/CD** | GitHub Actions pipeline (lint, security, test, Docker, cost estimates on PRs) |

## Prerequisites

- **Python 3.11+**
- **Terraform 1.5+**
- **AWS credentials** (Access Key + Secret, or SSO profile)
- **Amazon Bedrock access** (for LLM-driven code generation)
- **GitHub OAuth App** (for user authentication)
- **Git** (for PR-based approval workflow)
- Optional: Docker, Checkov, OPA

## Quick Start

### Local

```bash
# 1. Clone the repo
git clone https://github.com/AjayGite/ai-terraform-agent.git
cd ai-terraform-agent

# 2. Create virtual environment
python -m venv .venv
# Linux/Mac:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — at minimum set:
#   GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET (for OAuth login)
#   AWS_BEARER_TOKEN_BEDROCK (for AI generation)

# 5. Run
python server.py
# → Open http://localhost:8080
```

### Windows

```cmd
run.bat
```

### Linux/Mac

```bash
chmod +x run.sh && ./run.sh
```

### Docker

```bash
cp .env.example .env
# Edit .env with your settings
docker-compose up --build
```

## Configuration

All configuration is via environment variables (`.env` file):

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_WORKDIR` | No | `./workspace` | Base path for Terraform workspaces |
| `SECRET_KEY` | No | Auto-generated | Flask session secret |
| `GITHUB_CLIENT_ID` | For login | — | GitHub OAuth App Client ID |
| `GITHUB_CLIENT_SECRET` | For login | — | GitHub OAuth App Client Secret |
| `AWS_BEARER_TOKEN_BEDROCK` | For AI | — | Amazon Bedrock API bearer token |
| `BEDROCK_MODEL_ID` | No | `amazon.nova-pro-v1:0` | Bedrock model ID |
| `BEDROCK_REGION` | No | `us-east-1` | AWS region for Bedrock |
| `AWS_ACCESS_KEY_ID` | For deploy | — | AWS access key for Terraform |
| `AWS_SECRET_ACCESS_KEY` | For deploy | — | AWS secret key for Terraform |
| `AWS_SESSION_TOKEN` | No | — | Temporary session token (SSO) |
| `APPROVAL_MODE` | No | `pr` | `pr` (create PR) or `auto` (auto-apply) or `approval` (require explicit approval) |
| `GITHUB_PAT` | For PRs | — | GitHub Personal Access Token |
| `GITHUB_REPO` | For PRs | — | Target repo (`owner/repo`) |
| `DRIFT_CHECK_INTERVAL_MINUTES` | No | `30` | Scheduler interval |
| `SLACK_WEBHOOK_URL` | No | — | Slack webhook for notifications |
| `COST_THRESHOLD` | No | `100` | Max monthly cost (USD) before warning |
| `MCP_PORT` | No | `8080` | HTTP server port |
| `API_KEY` | No | — | Bearer token for programmatic API access |

## Web UI Pages

| Page | URL | Description |
|---|---|---|
| **Dashboard** | `/ui` | Resource overview, AWS status, recent activity |
| **Generate** | `/ui/generate` | Natural language → Terraform code with auto-validation |
| **Modify** | `/ui/modify` | AI-powered changes to existing infrastructure |
| **Drift Detection** | `/ui/drift` | Detect and remediate infrastructure drift |
| **Security & Costs** | `/ui/security` | Checkov scans + AWS cost estimation |
| **GitHub** | `/ui/github` | Repo sync, PR management, code validation |
| **Settings** | `/ui/settings` | AWS credentials, Bedrock config, GitHub PAT |
| **Admin** | `/auth/admin` | User management (admin role required) |

## API Reference

Endpoints accept `Authorization: Bearer <API_KEY>` or session cookie auth.

### Core Terraform

| Endpoint | Description |
|---|---|
| `GET /health` | Health check |
| `POST /terraform_init` | Run `terraform init` |
| `POST /terraform_plan` | Run `terraform plan` (auto-inits, state-locked) |
| `POST /terraform_validate` | Run `terraform validate` |
| `POST /terraform_apply` | Run `terraform apply` (state-locked, rate-limited) |
| `POST /terraform_destroy` | Run `terraform destroy` (state-locked, rate-limited) |
| `POST /terraform_show_json` | Export plan as JSON |

### AI Generation

| Endpoint | Description |
|---|---|
| `POST /generate` | Generate Terraform from natural language prompt |
| `POST /generate/apply` | Apply generated infrastructure (with deploy-time healing) |
| `POST /generate/destroy` | Destroy generated infrastructure |
| `POST /generate/pr` | Create/update a PR with generated code |

### Modify Existing

| Endpoint | Description |
|---|---|
| `POST /infra/modify` | AI-modify existing Terraform code |
| `POST /infra/modify/validate` | Validate proposed changes (SSE stream) |
| `POST /infra/modify/apply` | Apply or create PR with changes |
| `POST /infra/modify/discard` | Discard proposed changes |
| `POST /infra/modify/sync` | Sync code from GitHub repo |
| `POST /infra/modify/merge_and_deploy` | Merge PR and deploy (SSE stream) |

### Self-Healing & Drift

| Endpoint | Description |
|---|---|
| `POST /identify_drift` | Heuristic drift analysis from plan JSON |
| `POST /heal` | Full self-healing pipeline |
| `POST /scheduler/start` | Start background drift check scheduler |
| `POST /scheduler/stop` | Stop background scheduler |
| `GET /scheduler/status` | Get scheduler running status |
| `GET /drift/audit_log` | View drift detection history |

### Governance & Scanning

| Endpoint | Description |
|---|---|
| `POST /run_checkov` | Security scan with Checkov |
| `POST /cost_estimate` | Cost estimation via free AWS Pricing API |
| `POST /run_opa_check` | OPA policy evaluation |

### GitHub Integration

| Endpoint | Description |
|---|---|
| `POST /github/test` | Test GitHub PAT and repo access |
| `POST /github/sync` | Fresh clone from GitHub |
| `POST /github/validate_repo` | Validate all .tf files in workdir |
| `POST /github/drift_diff` | Run plan and show per-resource diffs |
| `POST /github/create_drift_pr` | Create PR with drift fixes |
| `POST /github/save_settings` | Save GitHub PAT and repo |

### Approval Workflow

| Endpoint | Description |
|---|---|
| `GET /ui/api/pending_plans` | List plans awaiting approval |
| `POST /ui/api/pending_plans/<id>/approve` | Approve a pending plan |
| `POST /ui/api/pending_plans/<id>/reject` | Reject a pending plan |
| `POST /ui/api/pending_plans/<id>/apply` | Apply an approved plan |

### Audit Trail

| Endpoint | Description |
|---|---|
| `GET /ui/api/audit_log` | Query audit log (filter by action, user, date) |
| `GET /ui/api/audit_stats` | Audit summary statistics |

### Example Requests

```bash
# Health check
curl http://127.0.0.1:8080/health

# Generate infrastructure (with API key)
curl -X POST http://127.0.0.1:8080/generate \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Create a t3.micro EC2 instance with a security group allowing SSH"}'

# Run cost estimate
curl -X POST http://127.0.0.1:8080/cost_estimate \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"path": "./workspace/.generated"}'

# Trigger self-healing
curl -X POST http://127.0.0.1:8080/heal \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## Self-Healing Workflow

```
1. DETECT    — terraform plan detects infrastructure drift
2. ANALYZE   — Parse plan JSON, identify changed resources
3. GENERATE  — Send drift report + current HCL to Amazon Bedrock LLM
4. VALIDATE  — terraform validate on generated code
5. POST-PROC — 21 automated post-processors fix common LLM mistakes
6. RE-PLAN   — Confirm the fix resolves the drift
7. GATE      — Check OPA policies, Checkov, cost thresholds
8. DECIDE    — Risk classification
   ├─ auto mode   → terraform apply (with state lock)
   ├─ pr mode     → git branch + PR for review
   └─ approval    → queue for manual approval
9. NOTIFY    — Slack alert with outcome
10. AUDIT    — Log action to audit trail
```

## Project Structure

```
ai-terraform-agent/
├── server.py              # App entry point (Flask init + blueprint registration)
├── routes/                # Flask Blueprints (modular route handlers)
│   ├── __init__.py        # Blueprint registration hub
│   ├── helpers.py         # Shared utilities (auth, response, audit)
│   ├── postprocessors.py  # 21 HCL post-processor functions
│   ├── generate.py        # /generate endpoints + self-healing loop
│   ├── modify.py          # /infra/modify/* endpoints
│   ├── terraform.py       # /terraform_* CLI wrappers
│   ├── github_ops.py      # /github/* + /git_* endpoints
│   ├── drift.py           # /identify_drift, /heal, /scheduler/*
│   ├── security.py        # /run_checkov, /cost_estimate, /run_opa_check
│   ├── ui.py              # UI pages, dashboard API, settings, audit, approvals
│   └── files.py           # /read_file, /write_file
├── orchestrator.py        # LLM-driven self-healing engine
├── llm.py                 # Amazon Bedrock LLM client
├── cost_estimator.py      # Free AWS Pricing API cost estimation
├── auth.py                # GitHub OAuth + session management
├── db.py                  # SQLite user database
├── audit.py               # Audit trail (SQLite)
├── approval.py            # Plan approval workflow (SQLite)
├── state_lock.py          # File-based Terraform state locking
├── scheduler.py           # APScheduler-based periodic healing
├── config.py              # Centralized configuration
├── notifications.py       # Slack/webhook notifications
├── utils.py               # File I/O + subprocess utilities
├── requirements.txt       # Python dependencies
├── Dockerfile             # Container image
├── docker-compose.yml     # Multi-service deployment
├── .env.example           # Environment variable template
├── run.sh / run.bat       # Startup scripts
├── templates/             # Jinja2 HTML templates
│   ├── base.html          # Layout template
│   ├── dashboard.html     # Main dashboard
│   ├── generate.html      # AI generation UI
│   ├── modify.html        # Modify existing infra UI
│   ├── drift.html         # Drift detection UI
│   ├── security.html      # Checkov + cost estimation UI
│   ├── github.html        # GitHub integration UI
│   ├── settings.html      # Configuration UI
│   └── login.html         # Authentication pages
├── static/                # CSS and JavaScript assets
│   ├── css/
│   └── js/
├── policies/              # OPA Rego policies
│   ├── s3_encryption.rego
│   └── tagging.rego
├── deploy/                # Deployment helpers
│   ├── ec2-setup.sh       # EC2 instance setup script
│   └── iam-policy.json    # Required IAM permissions
├── tests/                 # Unit & integration tests
│   ├── test_utils.py
│   ├── test_server.py
│   └── test_orchestrator.py
└── .github/workflows/
    ├── ci.yml             # CI pipeline (lint, test, Docker)
    └── cost-estimate.yml  # Auto cost estimation on PRs
```

## Security

- **GitHub OAuth** — User authentication via GitHub OAuth (Authlib)
- **Session management** — Secure HTTP-only cookies with 7-day expiry
- **Role-based access** — Admin and user roles with admin panel
- **Path traversal protection** — File operations sandboxed to `AGENT_WORKDIR`
- **API key authentication** — Bearer token for programmatic API access
- **Rate limiting** — Per-endpoint limits on destructive operations (3-10/min)
- **State locking** — Prevents concurrent terraform apply/destroy
- **Audit trail** — All actions logged with user, timestamp, outcome
- **No secrets in code** — All credentials via environment variables
- **Subprocess timeout** — All shell commands have configurable timeouts

## Testing

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=. --cov-report=term-missing
```

## Cost Estimation

The agent uses the **free AWS Pricing API** for cost estimation — no Infracost CLI or API key required.

To test:
1. Generate infrastructure (creates `plan.out` automatically)
2. Go to **Security & Cost Analysis** → click **Run Cost Estimate**
3. View per-resource cost breakdown with monthly totals

Cost estimates are also automatically posted as PR comments when creating PRs via `/generate/pr`.

## Deployment

### EC2 (Production)

```bash
# Use the provided setup script
bash deploy/ec2-setup.sh

# Or manually:
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:8080 server:app
```

### Docker

```bash
docker-compose up -d
```

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make changes and add tests
4. Run `pytest tests/ -v`
5. Submit a PR

## License

See [LICENSE.txt](LICENSE.txt) for details.
