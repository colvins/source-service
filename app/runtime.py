import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
APP_ROOT = BASE_DIR.parent
RUNTIME_DIR = Path(os.environ.get("COLVINS_RUNTIME_DIR", str(APP_ROOT / ".runtime")))
JS_SHIM_DIR = Path(os.environ.get("COLVINS_JS_SHIM_DIR", str(BASE_DIR / "runtime_node")))
PY_SHIM_DIR = Path(os.environ.get("COLVINS_PY_SHIM_DIR", str(BASE_DIR / "runtime_py")))
NODE_GLOBAL_MODULE_DIR = Path("/usr/local/lib/node_modules")
RUNTIME_CACHE_DIR = RUNTIME_DIR / "cache"


def _source_row(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise RuntimeError("source not found")
    return row


def _context_payload(source_id: int, request_base_url: str) -> dict[str, Any]:
    return {
        "sourceId": source_id,
        "baseURL": request_base_url.rstrip("/"),
    }


def execute_source(
    conn: sqlite3.Connection,
    source_id: int,
    action: str,
    params: dict[str, Any],
    request_base_url: str,
) -> dict[str, Any]:
    row = _source_row(conn, source_id)
    runtime_type = row["runtime_type"]
    script_content = row["script_content"] or ""
    context = _context_payload(source_id, request_base_url)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    if runtime_type == "javascript":
        return _execute_js(script_content, action, params, context)
    if runtime_type == "python":
        return _execute_python(script_content, action, params, context)
    raise RuntimeError(f"unsupported runtime: {runtime_type}")


def _execute_js(script_content: str, action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    script_fd, script_path = tempfile.mkstemp(suffix=".js", dir=RUNTIME_DIR)
    os.close(script_fd)
    Path(script_path).write_text(script_content, encoding="utf-8")

    runner_path = Path(os.environ.get("COLVINS_JS_RUNNER", str(BASE_DIR / "runtime_js_runner.js")))
    env = os.environ.copy()
    env["NODE_PATH"] = f"{JS_SHIM_DIR}:{NODE_GLOBAL_MODULE_DIR}:{env.get('NODE_PATH', '')}".rstrip(":")
    env["COLVINS_ACTION"] = action
    env["COLVINS_PARAMS"] = json.dumps(params, ensure_ascii=False)
    env["COLVINS_CONTEXT"] = json.dumps(context, ensure_ascii=False)
    env["COLVINS_RUNTIME_CACHE_DIR"] = str(RUNTIME_CACHE_DIR)
    RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["node", str(runner_path), script_path],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "javascript runtime failed")
    return json.loads(result.stdout.strip() or "{}")


def _execute_python(script_content: str, action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    script_fd, script_path = tempfile.mkstemp(suffix=".py", dir=RUNTIME_DIR)
    os.close(script_fd)
    Path(script_path).write_text(script_content, encoding="utf-8")

    runner_path = Path(os.environ.get("COLVINS_PY_RUNNER", str(BASE_DIR / "runtime_py_runner.py")))
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{PY_SHIM_DIR}:{env.get('PYTHONPATH', '')}".rstrip(":")
    env["COLVINS_ACTION"] = action
    env["COLVINS_PARAMS"] = json.dumps(params, ensure_ascii=False)
    env["COLVINS_CONTEXT"] = json.dumps(context, ensure_ascii=False)

    try:
        result = subprocess.run(
            ["python3", str(runner_path), script_path],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "python runtime failed")
    return json.loads(result.stdout.strip() or "{}")
