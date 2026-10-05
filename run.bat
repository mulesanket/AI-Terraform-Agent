@echo off
REM Self-Healing Terraform Agent - Windows Startup

REM Load .env if present (manual — Windows doesn't have source)
if exist .env (
    echo Loading .env configuration...
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
    )
)

REM Create virtual environment
python -m venv .venv
call .venv\Scripts\activate

REM Install dependencies
pip install -r requirements.txt

REM Default workdir
if not defined AGENT_WORKDIR set AGENT_WORKDIR=%cd%\example\aws\s3-basic

echo Starting Self-Healing Terraform Agent...
echo   Workdir: %AGENT_WORKDIR%
echo   Port:    %MCP_PORT%

python server.py
