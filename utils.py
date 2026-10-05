# utils.py
import subprocess
import os


def validate_path(path, base_dir):
    """Ensure the resolved path is within the allowed base directory."""
    real_path = os.path.realpath(path)
    real_base = os.path.realpath(base_dir)
    if not real_path.startswith(real_base + os.sep) and real_path != real_base:
        raise PermissionError(f"Access denied: path outside workspace ({real_path})")
    return real_path


def run_cmd_capture(cmd, cwd=None, timeout=120):
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as e:
        return 1, "", str(e)


def safe_write_file(path, content, base_dir=None):
    """Write file content. If base_dir is set, validates path stays within it."""
    if base_dir:
        validate_path(path, base_dir)
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def safe_read_file(path, base_dir=None):
    """Read file content. If base_dir is set, validates path stays within it."""
    if base_dir:
        validate_path(path, base_dir)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
