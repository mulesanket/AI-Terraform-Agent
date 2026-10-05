# routes/files.py - File read/write endpoints
from flask import Blueprint, request

from .helpers import require_api_key, respond_ok, respond_err, get_workdir, logger
from utils import safe_read_file, safe_write_file

files_bp = Blueprint("files", __name__)


@files_bp.route("/read_file", methods=["POST"])
@require_api_key
def read_file():
    body = request.json or {}
    path = body.get("path")
    if not path:
        return respond_err("path required", 400)
    try:
        content = safe_read_file(path, base_dir=get_workdir())
        logger.info("Read file: %s", path)
        return respond_ok({"path": path, "content": content})
    except PermissionError as e:
        return respond_err(str(e), 403)
    except Exception as e:
        return respond_err(str(e), 500)


@files_bp.route("/write_file", methods=["POST"])
@require_api_key
def write_file():
    body = request.json or {}
    path = body.get("path")
    content = body.get("content", "")
    if not path:
        return respond_err("path required", 400)
    try:
        safe_write_file(path, content, base_dir=get_workdir())
        logger.info("Wrote file: %s", path)
        return respond_ok({"path": path})
    except PermissionError as e:
        return respond_err(str(e), 403)
    except Exception as e:
        return respond_err(str(e), 500)
