import json
import os
import sqlite3
import subprocess
import tempfile
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
APP_ROOT = BASE_DIR.parent
RUNTIME_DIR = Path(os.environ.get("COLVINS_RUNTIME_DIR", str(APP_ROOT / ".runtime")))
JS_SHIM_DIR = Path(os.environ.get("COLVINS_JS_SHIM_DIR", str(BASE_DIR / "runtime_node")))
PY_SHIM_DIR = Path(os.environ.get("COLVINS_PY_SHIM_DIR", str(BASE_DIR / "runtime_py")))
NODE_GLOBAL_MODULE_DIR = Path("/usr/local/lib/node_modules")
RUNTIME_CACHE_DIR = RUNTIME_DIR / "cache"

NODE_RUNTIME_PORT = int(os.environ.get("COLVINS_NODE_RUNTIME_PORT", "18900"))
NODE_RUNTIME_URL = f"http://127.0.0.1:{NODE_RUNTIME_PORT}/run"
NODE_SERVER_SCRIPT = BASE_DIR / "runtime_node_server.js"

_node_proc: subprocess.Popen | None = None
_node_lock = threading.Lock()


def _start_node_runtime() -> None:
    global _node_proc
    env = os.environ.copy()
    env["COLVINS_JS_SHIM_DIR"] = str(JS_SHIM_DIR)
    env["COLVINS_NODE_RUNTIME_PORT"] = str(NODE_RUNTIME_PORT)
    env["COLVINS_RUNTIME_CACHE_DIR"] = str(RUNTIME_CACHE_DIR)
    env["NODE_PATH"] = f"{JS_SHIM_DIR}:{NODE_GLOBAL_MODULE_DIR}:{env.get('NODE_PATH', '')}".rstrip(":")
    RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["node", str(NODE_SERVER_SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # 等待 ready 信号，最多 10 秒
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if "COLVINS_NODE_RUNTIME_READY" in line:
            _node_proc = proc
            return
        if proc.poll() is not None:
            err = proc.stderr.read()
            raise RuntimeError(f"node runtime failed to start: {err}")
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("node runtime did not signal ready within 10s")


def _ensure_node_runtime() -> None:
    global _node_proc
    with _node_lock:
        if _node_proc is not None and _node_proc.poll() is None:
            return
        _start_node_runtime()


def _call_node_runtime(script_content: str, action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    _ensure_node_runtime()
    payload = json.dumps({
        "scriptContent": script_content,
        "action": action,
        "params": params,
        "context": context,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        NODE_RUNTIME_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            if parsed.get("__error"):
                raise RuntimeError(parsed.get("message") or "javascript runtime error")
        except (json.JSONDecodeError, KeyError):
            pass
        raise RuntimeError(f"node runtime HTTP {error.code}: {body[:240]}")
    except urllib.error.URLError as error:
        # 连接失败时重启 node 进程再试一次
        with _node_lock:
            global _node_proc
            if _node_proc:
                _node_proc.kill()
            _node_proc = None
        _ensure_node_runtime()
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
    if isinstance(result, dict) and result.get("__error"):
        raise RuntimeError(result.get("message") or "javascript runtime error")
    return result


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
        return _call_node_runtime(script_content, action, params, context)
    if runtime_type == "python":
        return _execute_python(script_content, action, params, context)
    raise RuntimeError(f"unsupported runtime: {runtime_type}")


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
