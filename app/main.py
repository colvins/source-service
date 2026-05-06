import hashlib
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_conn, init_db, row_to_source, row_to_subscription
from .models import SettingPayload, SourcePayload, SubscriptionPayload
from .runtime import execute_source

app = FastAPI(title="Colvins Source Service", version="0.1.0")
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def startup() -> None:
    init_db()


def fetch_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM sources ORDER BY sort_order ASC, id ASC").fetchall()
    return [row_to_source(row) for row in rows]


def fetch_subscriptions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM subscriptions ORDER BY sort_order ASC, id ASC").fetchall()
    items = []
    for row in rows:
        item = row_to_subscription(row)
        source_rows = conn.execute(
            """
            SELECT s.* FROM sources s
            JOIN subscription_sources ss ON ss.source_id = s.id
            WHERE ss.subscription_id = ? AND ss.enabled = 1
            ORDER BY ss.sort_order ASC, s.sort_order ASC, s.id ASC
            """,
            (row["id"],),
        ).fetchall()
        item["sources"] = [row_to_source(source_row) for source_row in source_rows]
        items.append(item)
    return items


def ensure_subscription_token(conn: sqlite3.Connection, subscription_id: int) -> str:
    row = conn.execute("SELECT token FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="subscription not found")
    token = row["token"]
    if token:
        return token
    token = secrets.token_urlsafe(24)
    conn.execute(
        "UPDATE subscriptions SET token = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (token, subscription_id),
    )
    return token


def export_catpaw(subscription: dict[str, Any], base_url: str) -> dict[str, Any]:
    sites = []
    for source in subscription["sources"]:
        sites.append(
            {
                "key": f"source_{source['id']}",
                "name": source["name"],
                "type": 3,
                    "api": f"{base_url}/api/v1/source/{source['id']}",
                "ext": {
                    "runtimeType": source["runtimeType"],
                    "dependencies": source["dependencies"],
                    "version": source["version"],
                    "enabled": source["enabled"],
                },
            }
        )
    return {
        "video": {"sites": sites},
        "color": [],
        "meta": {
            "subscriptionId": subscription["id"],
            "subscriptionName": subscription["name"],
        },
    }


def export_tvbox(subscription: dict[str, Any], base_url: str) -> dict[str, Any]:
    sites = []
    for source in subscription["sources"]:
        sites.append(
            {
                "key": f"source_{source['id']}",
                "name": source["name"],
                "type": 3,
                "api": f"{base_url}/api/tvbox/source/{source['id']}",
                "searchable": 1,
                "changeable": 1,
                "quickSearch": 1,
                "ext": source["downloadURL"] or "",
            }
        )
    return {
        "sites": sites,
        "lives": [],
        "parses": [],
        "flags": [],
        "spider": "",
        "ijk": {},
    }


def catpaw_index_script(subscription: dict[str, Any], base_url: str) -> str:
    descriptors = []
    for source in subscription["sources"]:
        api = f"{base_url}/api/v1/source/{source['id']}"
        key = f"source_{source['id']}"
        name = json.dumps(source["name"], ensure_ascii=False)
        descriptors.append(f'{{api:"{api}",meta:{{key:"{key}",name:{name}}}}}')
    return (
        "// Colvins Source Service CatPaw/Open compatibility bundle.\n"
        "// This file exposes source API metadata only; media bytes are never proxied.\n"
        f"var ColvinsSources=[{','.join(descriptors)}];\n"
        "module.exports={sources:ColvinsSources};\n"
    )


def catpaw_index_md5(subscription: dict[str, Any], base_url: str) -> str:
    body = catpaw_index_script(subscription, base_url).encode("utf-8")
    return hashlib.md5(body).hexdigest()


def _subscription_and_base_url(subscription_id: int) -> tuple[dict[str, Any], str]:
    with get_conn() as conn:
        subscription = next((item for item in fetch_subscriptions(conn) if item["id"] == subscription_id), None)
        if not subscription:
            raise HTTPException(status_code=404, detail="subscription not found")
        settings = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
        return subscription, settings.get("service_base_url", "http://127.0.0.1:8788").rstrip("/")


def _runtime_envelope(payload: dict[str, Any]) -> JSONResponse:
    if isinstance(payload, dict) and "data" in payload:
        return JSONResponse(payload)
    return JSONResponse({"data": payload})


def _runtime_raw(source_id: int, action: str, params: dict[str, Any], request: Request) -> JSONResponse:
    with get_conn() as conn:
        return JSONResponse(execute_source(conn, source_id, action, params, _base_url_from_request(request)))


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "colvins-source-service"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_conn() as conn:
        settings = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM settings ORDER BY key")
        }
        sources = fetch_sources(conn)
        subscriptions = fetch_subscriptions(conn)
        templates_data = conn.execute("SELECT * FROM source_templates ORDER BY id ASC").fetchall()
        templates_payload = [dict(row) for row in templates_data]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "settings": settings,
            "sources": sources,
            "subscriptions": subscriptions,
            "templates": templates_payload,
        },
    )


@app.get("/api/admin/sources")
def list_sources():
    with get_conn() as conn:
        return {"items": fetch_sources(conn)}


@app.post("/api/admin/sources")
def create_source(payload: SourcePayload):
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sources (name, runtime_type, enabled, sort_order, tags, notes, description, version, download_url, dependencies, script_content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.runtimeType,
                1 if payload.enabled else 0,
                payload.sortOrder,
                json.dumps(payload.tags, ensure_ascii=False),
                payload.notes,
                payload.description,
                payload.version,
                payload.downloadURL,
                json.dumps(payload.dependencies, ensure_ascii=False),
                payload.scriptContent,
            ),
        )
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_source(row)


@app.put("/api/admin/sources/{source_id}")
def update_source(source_id: int, payload: SourcePayload):
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="source not found")
        conn.execute(
            """
            UPDATE sources
            SET name=?, runtime_type=?, enabled=?, sort_order=?, tags=?, notes=?, description=?, version=?, download_url=?, dependencies=?, script_content=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                payload.name,
                payload.runtimeType,
                1 if payload.enabled else 0,
                payload.sortOrder,
                json.dumps(payload.tags, ensure_ascii=False),
                payload.notes,
                payload.description,
                payload.version,
                payload.downloadURL,
                json.dumps(payload.dependencies, ensure_ascii=False),
                payload.scriptContent,
                source_id,
            ),
        )
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return row_to_source(row)


@app.delete("/api/admin/sources/{source_id}")
def delete_source(source_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return {"ok": True}


@app.get("/api/admin/templates")
def list_templates():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM source_templates ORDER BY id ASC").fetchall()
        return {"items": [dict(row) for row in rows]}


@app.get("/api/admin/subscriptions")
def list_subscriptions():
    with get_conn() as conn:
        return {"items": fetch_subscriptions(conn)}


@app.post("/api/admin/subscriptions")
def create_subscription(payload: SubscriptionPayload):
    with get_conn() as conn:
        token = secrets.token_urlsafe(24)
        cur = conn.execute(
            """
            INSERT INTO subscriptions (name, token, enabled, sort_order, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                token,
                1 if payload.enabled else 0,
                payload.sortOrder,
                payload.notes,
            ),
        )
        subscription_id = cur.lastrowid
        for index, source_id in enumerate(payload.sourceIds):
            conn.execute(
                """
                INSERT OR REPLACE INTO subscription_sources (subscription_id, source_id, sort_order, enabled)
                VALUES (?, ?, ?, 1)
                """,
                (subscription_id, source_id, index),
            )
        items = fetch_subscriptions(conn)
        return next(item for item in items if item["id"] == subscription_id)


@app.put("/api/admin/subscriptions/{subscription_id}")
def update_subscription(subscription_id: int, payload: SubscriptionPayload):
    with get_conn() as conn:
        ensure_subscription_token(conn, subscription_id)
        conn.execute(
            """
            UPDATE subscriptions
            SET name=?, enabled=?, sort_order=?, notes=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                payload.name,
                1 if payload.enabled else 0,
                payload.sortOrder,
                payload.notes,
                subscription_id,
            ),
        )
        conn.execute("DELETE FROM subscription_sources WHERE subscription_id = ?", (subscription_id,))
        for index, source_id in enumerate(payload.sourceIds):
            conn.execute(
                """
                INSERT INTO subscription_sources (subscription_id, source_id, sort_order, enabled)
                VALUES (?, ?, ?, 1)
                """,
                (subscription_id, source_id, index),
            )
        items = fetch_subscriptions(conn)
        return next(item for item in items if item["id"] == subscription_id)


@app.delete("/api/admin/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
    return {"ok": True}


@app.get("/api/admin/settings")
def list_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {"items": [{row["key"]: row["value"]} for row in rows]}


@app.put("/api/admin/settings/{key}")
def update_setting(key: str, payload: SettingPayload):
    value = payload.value if isinstance(payload.value, str) else json.dumps(payload.value, ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, value),
        )
    return {"key": key, "value": value}


@app.get("/api/export/{subscription_id}/catpaw/config")
def export_catpaw_config(subscription_id: int):
    subscription, base_url = _subscription_and_base_url(subscription_id)
    return export_catpaw(subscription, base_url)


@app.get("/api/export/{subscription_id}/catpaw/index.js.md5", response_class=PlainTextResponse)
def export_catpaw_md5(subscription_id: int):
    subscription, base_url = _subscription_and_base_url(subscription_id)
    return PlainTextResponse(catpaw_index_md5(subscription, base_url))


@app.get("/api/export/{subscription_id}/catpaw/index.js", response_class=PlainTextResponse)
def export_catpaw_index(subscription_id: int):
    subscription, base_url = _subscription_and_base_url(subscription_id)
    return PlainTextResponse(catpaw_index_script(subscription, base_url), media_type="application/javascript")


@app.get("/api/subscription/{subscription_id}/catvod/index.js.md5", response_class=PlainTextResponse)
def export_subscription_catvod_md5(subscription_id: int):
    return export_catpaw_md5(subscription_id)


@app.get("/api/subscription/{subscription_id}/catvod/index.js", response_class=PlainTextResponse)
def export_subscription_catvod_index(subscription_id: int):
    return export_catpaw_index(subscription_id)


@app.get("/api/subscription/{subscription_id}/catvod/config")
def export_subscription_catvod_config(subscription_id: int):
    return export_catpaw_config(subscription_id)


@app.get("/api/export/{subscription_id}/tvbox")
def export_tvbox_config(subscription_id: int, token: str | None = None):
    with get_conn() as conn:
        subscription = next((item for item in fetch_subscriptions(conn) if item["id"] == subscription_id), None)
        if not subscription:
            raise HTTPException(status_code=404, detail="subscription not found")
        real_token = ensure_subscription_token(conn, subscription_id)
        if token and token != real_token:
            raise HTTPException(status_code=403, detail="invalid token")
        settings = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
        base_url = settings.get("service_base_url", "http://127.0.0.1:8788").rstrip("/")
        return export_tvbox(subscription, base_url)


@app.get("/api/subscription/{subscription_id}/tvbox")
def export_subscription_tvbox_config(subscription_id: int, token: str | None = None):
    return export_tvbox_config(subscription_id, token)


@app.get("/api/runtime/source/{source_id}")
def runtime_source_stub(source_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="source not found")
        source = row_to_source(row)
    return {
        "ok": True,
        "source": source,
        "message": "v1 scaffold: runtime execution kernel not wired yet",
    }


def _base_url_from_request(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/api/runtime/source/{source_id}/home")
def runtime_home(source_id: int, request: Request):
    with get_conn() as conn:
        return _runtime_envelope(execute_source(conn, source_id, "home", {}, _base_url_from_request(request)))


@app.get("/api/runtime/source/{source_id}/category")
def runtime_category(source_id: int, request: Request, categoryId: str = "", id: str = "", tid: str = "", page: int = 1, pg: int = 1):
    resolved_category_id = categoryId or id or tid
    resolved_page = page or pg or 1
    with get_conn() as conn:
        return _runtime_envelope(execute_source(
            conn,
            source_id,
            "category",
            {"categoryId": resolved_category_id, "id": resolved_category_id, "tid": resolved_category_id, "page": resolved_page, "pg": resolved_page},
            _base_url_from_request(request),
        ))


@app.get("/api/runtime/source/{source_id}/search")
def runtime_search(source_id: int, request: Request, keyword: str = "", wd: str = "", page: int = 1, pg: int = 1):
    resolved_keyword = keyword or wd
    resolved_page = page or pg or 1
    with get_conn() as conn:
        return _runtime_envelope(execute_source(
            conn,
            source_id,
            "search",
            {"keyword": resolved_keyword, "wd": resolved_keyword, "page": resolved_page, "pg": resolved_page},
            _base_url_from_request(request),
        ))


@app.get("/api/runtime/source/{source_id}/detail")
def runtime_detail(source_id: int, request: Request, videoId: str = "", id: str = "", ids: str = ""):
    resolved_video_id = videoId or id or ids
    with get_conn() as conn:
        return _runtime_envelope(execute_source(
            conn,
            source_id,
            "detail",
            {"videoId": resolved_video_id, "id": resolved_video_id, "ids": resolved_video_id},
            _base_url_from_request(request),
        ))


@app.get("/api/runtime/source/{source_id}/play")
def runtime_play(source_id: int, request: Request, flag: str = "", playId: str = "", id: str = ""):
    resolved_play_id = playId or id
    with get_conn() as conn:
        return _runtime_envelope(execute_source(
            conn,
            source_id,
            "play",
            {"flag": flag, "playId": resolved_play_id, "id": resolved_play_id},
            _base_url_from_request(request),
        ))


@app.get("/api/v1/source/{source_id}/home")
def runtime_v1_home(source_id: int, request: Request):
    return runtime_home(source_id, request)


@app.get("/api/v1/source/{source_id}/category")
def runtime_v1_category(source_id: int, request: Request, categoryId: str = "", id: str = "", tid: str = "", page: int = 1, pg: int = 1):
    return runtime_category(source_id, request, categoryId, id, tid, page, pg)


@app.get("/api/v1/source/{source_id}/search")
def runtime_v1_search(source_id: int, request: Request, keyword: str = "", wd: str = "", page: int = 1, pg: int = 1):
    return runtime_search(source_id, request, keyword, wd, page, pg)


@app.get("/api/v1/source/{source_id}/detail")
def runtime_v1_detail(source_id: int, request: Request, videoId: str = "", id: str = "", ids: str = ""):
    return runtime_detail(source_id, request, videoId, id, ids)


@app.get("/api/v1/source/{source_id}/play")
def runtime_v1_play(source_id: int, request: Request, flag: str = "", playId: str = "", id: str = ""):
    return runtime_play(source_id, request, flag, playId, id)


@app.get("/api/tvbox/source/{source_id}/home")
def runtime_tvbox_home(source_id: int, request: Request):
    return _runtime_raw(source_id, "home", {}, request)


@app.get("/api/tvbox/source/{source_id}/category")
def runtime_tvbox_category(source_id: int, request: Request, id: str = "", tid: str = "", pg: int = 1, page: int = 1):
    resolved_category_id = id or tid
    resolved_page = page or pg or 1
    return _runtime_raw(
        source_id,
        "category",
        {"categoryId": resolved_category_id, "id": resolved_category_id, "tid": resolved_category_id, "page": resolved_page, "pg": resolved_page},
        request,
    )


@app.get("/api/tvbox/source/{source_id}/search")
def runtime_tvbox_search(source_id: int, request: Request, wd: str = "", keyword: str = "", pg: int = 1, page: int = 1):
    resolved_keyword = keyword or wd
    resolved_page = page or pg or 1
    return _runtime_raw(
        source_id,
        "search",
        {"keyword": resolved_keyword, "wd": resolved_keyword, "page": resolved_page, "pg": resolved_page},
        request,
    )


@app.get("/api/tvbox/source/{source_id}/detail")
def runtime_tvbox_detail(source_id: int, request: Request, id: str = "", ids: str = "", videoId: str = ""):
    resolved_video_id = videoId or id or ids
    return _runtime_raw(
        source_id,
        "detail",
        {"videoId": resolved_video_id, "id": resolved_video_id, "ids": resolved_video_id},
        request,
    )


@app.get("/api/tvbox/source/{source_id}/play")
def runtime_tvbox_play(source_id: int, request: Request, flag: str = "", id: str = "", playId: str = ""):
    resolved_play_id = playId or id
    return _runtime_raw(
        source_id,
        "play",
        {"flag": flag, "playId": resolved_play_id, "id": resolved_play_id},
        request,
    )
