# Ubuntu EC2 Deployment Runbook

This repository is an AI-assisted Terraform agent. It runs a Flask web app that can generate Terraform with Amazon Bedrock, store user/settings data in SQLite, create GitHub PRs, and trigger Terraform deployment workflows.

## What to push

Push source code and templates:

- `.github/`
- `deploy/`
- `example/`
- `instructions/`
- `policies/`
- `routes/`
- `static/`
- `templates/`
- `tests/`
- `workspace/.gitkeep`
- root Python files such as `server.py`, `config.py`, `auth.py`, `db.py`, `llm.py`, `orchestrator.py`
- `requirements.txt`
- `Dockerfile`
- `docker-compose.yml`
- `run.sh`
- `run.bat`
- `.env.example`
- `.gitignore`
- `README.md`
- `LICENSE.txt`

Do not push local runtime data:

- `.env`
- `agent_users.db`
- `.venv/`
- `__pycache__/`
- `workspace/` contents other than `.gitkeep`
- `.terraform/`
- `*.tfstate`
- `plan.out`
- `aws/dist/`

## Prepare Git Locally

Because the old `.git` directory was removed, initialize a fresh repository:

```bash
git init
git add .
git status
git commit -m "Initial AI Terraform Agent setup"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

Check `git status` before committing. If `.env`, `agent_users.db`, `aws/dist/`, or generated Terraform files appear, stop and fix `.gitignore` or delete those local files first.

## EC2 Requirements

Recommended instance:

- Ubuntu 24.04 LTS
- t3.small or larger
- 20 GB gp3 root volume
- Security group allowing SSH `22` from your IP and app port `8080` from your IP

AWS access:

- Attach an EC2 instance role if you want the app/Terraform to use AWS without static access keys.
- For GitHub Actions deploy/destroy workflows, configure `AWS_ROLE_ARN` as a GitHub secret in the target repository.

## Install on EC2

SSH into the instance, then run:

```bash
sudo apt-get update -y
sudo apt-get install -y git curl
git clone https://github.com/<your-user>/<your-repo>.git /tmp/ai-terraform-agent
cd /tmp/ai-terraform-agent
REPO_URL=https://github.com/<your-user>/<your-repo>.git bash deploy/ec2-setup.sh
```

The setup script installs Docker, Python, Terraform, app dependencies, creates `/opt/terraform-agent`, creates a systemd service, and copies `.env.example` to `.env`.

## Configure `.env` on EC2

Edit:

```bash
nano /opt/terraform-agent/.env
```

Minimum values to fill:

```env
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
AWS_BEARER_TOKEN_BEDROCK=
TF_STATE_BUCKET=
```

For PR and pipeline automation, also fill:

```env
GITHUB_PAT=
GITHUB_REPO=
```

Use the EC2 callback URL in your GitHub OAuth app:

```text
http://<EC2-PUBLIC-IP>:8080/auth/oauth/github/callback
```

## Start and Verify

```bash
sudo systemctl start terraform-agent
sudo systemctl status terraform-agent
journalctl -u terraform-agent -f
```

Open:

```text
http://<EC2-PUBLIC-IP>:8080
```

## Why these configs matter

- `API_KEY`: protects API access outside browser login.
- `SECRET_KEY`: keeps Flask sessions valid and secure across restarts.
- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`: enables web login.
- `GITHUB_PAT` / `GITHUB_REPO`: lets the app create PRs and sync repositories.
- `AWS_BEARER_TOKEN_BEDROCK`: lets the app call Bedrock for Terraform generation.
- `TF_STATE_BUCKET`: enables S3 remote state for generated Terraform.
- `AGENT_WORKDIR`: stores generated and synced Terraform code outside source files.
