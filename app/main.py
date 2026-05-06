import hashlib
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_conn, init_db, row_to_drive_account, row_to_source, row_to_subscription
from .drive import (
    DriveBridgeError,
    call_omnibox_drive_bridge,
    drive_providers,
    normalize_provider,
    normalize_quark_play_payload,
    normalize_drive_files,
    parse_drive_share_url,
    parse_quark_share_url,
    select_best_video_file,
    video_files_from_payload,
)
from .models import (
    DriveAccountPayload,
    DriveFileListPayload,
    DrivePlayNormalizePayload,
    DrivePlayPayload,
    QuarkQRCheckPayload,
    DriveSharePayload,
    DriveVideosPayload,
    QuarkFileListPayload,
    QuarkPlayPayload,
    QuarkSharePayload,
    SettingPayload,
    SourcePayload,
    SubscriptionPayload,
)
from .quark import (
    QuarkAuthRequired,
    QuarkClient,
    QuarkNativeError,
    quark_cookie_from_service_ticket,
    quark_has_mobile_auth,
    quark_qr_check,
    quark_qr_start,
)
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


def fetch_drive_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM drive_accounts ORDER BY sort_order ASC, id ASC").fetchall()
    return [row_to_drive_account(row) for row in rows]


def fetch_active_drive_account_private(conn: sqlite3.Connection, provider: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM drive_accounts
        WHERE provider = ? AND enabled = 1 AND (cookie <> '' OR token <> '')
        ORDER BY sort_order ASC, id ASC
        LIMIT 1
        """,
        (provider,),
    ).fetchone()


def quark_client_from_account(row: sqlite3.Row | None) -> QuarkClient | None:
    if row is None or not row["cookie"]:
        return None
    return QuarkClient(row["cookie"], row["user_agent"])


def drive_bridge_enabled() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'drive_bridge_mode'").fetchone()
    return bool(row and row["value"] == "omnibox-fallback")


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


def subscription_urls(subscription: dict[str, Any], base_url: str) -> dict[str, str]:
    token = subscription["token"]
    subscription_id = subscription["id"]
    return {
        "catpawMd5": f"{base_url}/api/subscription/{subscription_id}/catvod/index.js.md5",
        "catpawIndex": f"{base_url}/api/subscription/{subscription_id}/catvod/index.js",
        "catpawConfig": f"{base_url}/api/subscription/{subscription_id}/catvod/config",
        "tvbox": f"{base_url}/api/subscription/{subscription_id}/tvbox?token={token}",
        "tvboxExport": f"{base_url}/api/export/{subscription_id}/tvbox?token={token}",
    }


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
        for subscription in subscriptions:
            subscription["urls"] = subscription_urls(
                subscription,
                settings.get("service_base_url", "http://127.0.0.1:8788").rstrip("/"),
            )
        drive_accounts = fetch_drive_accounts(conn)
        templates_data = conn.execute("SELECT * FROM source_templates ORDER BY id ASC").fetchall()
        templates_payload = [dict(row) for row in templates_data]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "settings": settings,
            "sources": sources,
            "subscriptions": subscriptions,
            "driveAccounts": drive_accounts,
            "templates": templates_payload,
        },
    )


@app.post("/admin/drive-accounts")
def create_drive_account_form(
    name: str = Form(...),
    provider: str = Form("quark"),
    cookie: str = Form(""),
    user_agent: str = Form(""),
    notes: str = Form(""),
):
    payload = DriveAccountPayload(
        name=name,
        provider=provider,
        cookie=cookie,
        userAgent=user_agent,
        notes=notes,
    )
    create_drive_account(payload)
    return RedirectResponse(url="/#drive-accounts", status_code=303)


@app.post("/admin/settings")
def update_setting_form(key: str = Form(...), value: str = Form("")):
    update_setting(key, SettingPayload(value=value))
    return RedirectResponse(url="/#settings", status_code=303)


@app.get("/drive-test", response_class=HTMLResponse)
def drive_test_page(provider: str = "quark", shareURL: str = ""):
    if not shareURL:
        return HTMLResponse("<pre>Missing shareURL.</pre>", status_code=400)
    try:
        result = drive_share_play_best(
            provider,
            DriveVideosPayload(shareURL=shareURL, recursive=True, maxDepth=3, maxItems=80),
        )
        body = json.dumps(result, ensure_ascii=False, indent=2)
        return HTMLResponse(f"<pre>{body}</pre>")
    except Exception as error:
        body = json.dumps({"error": str(error)}, ensure_ascii=False, indent=2)
        return HTMLResponse(f"<pre>{body}</pre>", status_code=500)


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


@app.get("/api/admin/drive-accounts")
def list_drive_accounts():
    with get_conn() as conn:
        return {"items": fetch_drive_accounts(conn)}


@app.post("/api/admin/drive-accounts")
def create_drive_account(payload: DriveAccountPayload):
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO drive_accounts (name, provider, enabled, sort_order, cookie, token, user_agent, notes, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.provider,
                1 if payload.enabled else 0,
                payload.sortOrder,
                payload.cookie,
                payload.token,
                payload.userAgent,
                payload.notes,
                "configured" if payload.cookie or payload.token else "missing-auth",
            ),
        )
        row = conn.execute("SELECT * FROM drive_accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_drive_account(row)


@app.put("/api/admin/drive-accounts/{account_id}")
def update_drive_account(account_id: int, payload: DriveAccountPayload):
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM drive_accounts WHERE id = ?", (account_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="drive account not found")
        conn.execute(
            """
            UPDATE drive_accounts
            SET name=?, provider=?, enabled=?, sort_order=?, cookie=?, token=?, user_agent=?, notes=?,
                status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                payload.name,
                payload.provider,
                1 if payload.enabled else 0,
                payload.sortOrder,
                payload.cookie,
                payload.token,
                payload.userAgent,
                payload.notes,
                "configured" if payload.cookie or payload.token else "missing-auth",
                account_id,
            ),
        )
        row = conn.execute("SELECT * FROM drive_accounts WHERE id = ?", (account_id,)).fetchone()
        return row_to_drive_account(row)


@app.delete("/api/admin/drive-accounts/{account_id}")
def delete_drive_account(account_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM drive_accounts WHERE id = ?", (account_id,))
    return {"ok": True}


@app.post("/api/admin/drive-accounts/{account_id}/check")
def check_drive_account(account_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM drive_accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="drive account not found")
        status = "configured" if row["cookie"] or row["token"] else "missing-auth"
        if row["provider"] == "quark" and row["cookie"]:
            status = "configured-mobile" if quark_has_mobile_auth(row["cookie"]) else "configured"
        conn.execute(
            """
            UPDATE drive_accounts
            SET status=?, last_checked_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (status, account_id),
        )
        updated = conn.execute("SELECT * FROM drive_accounts WHERE id = ?", (account_id,)).fetchone()
        return row_to_drive_account(updated)


@app.post("/api/drive/quark/auth/qr/start")
def start_quark_qr_auth():
    try:
        return quark_qr_start()
    except QuarkNativeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.post("/api/drive/quark/auth/qr/check")
def check_quark_qr_auth(payload: QuarkQRCheckPayload):
    try:
        status = quark_qr_check(payload.token)
        if not status.get("ready"):
            return status
        cookie = quark_cookie_from_service_ticket(status["serviceTicket"])
    except QuarkNativeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    result = {
        "ready": True,
        "hasCookie": bool(cookie),
        "mobileAuthReady": quark_has_mobile_auth(cookie),
    }
    if payload.saveAccount:
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO drive_accounts (name, provider, enabled, sort_order, cookie, token, user_agent, notes, status)
                VALUES (?, 'quark', 1, 0, ?, '', ?, ?, ?)
                """,
                (
                    payload.name or "Quark",
                    cookie,
                    "",
                    "Created by Quark QR auth.",
                    "configured-mobile" if quark_has_mobile_auth(cookie) else "configured",
                ),
            )
            row = conn.execute("SELECT * FROM drive_accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
            result["account"] = row_to_drive_account(row)
    return result


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


@app.get("/api/drive/quark/status")
def quark_drive_status():
    return drive_provider_status("quark")


@app.get("/api/drive/providers")
def list_drive_providers():
    return {"items": drive_providers()}


@app.get("/api/drive/{provider}/status")
def drive_provider_status(provider: str):
    try:
        provider = normalize_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    with get_conn() as conn:
        accounts = [
            account
            for account in fetch_drive_accounts(conn)
            if account["provider"] == provider and account["enabled"]
        ]
        settings = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
        private_accounts = conn.execute(
            "SELECT cookie FROM drive_accounts WHERE provider = ? AND enabled = 1",
            (provider,),
        ).fetchall()
    return {
        "provider": provider,
        "nativeResolver": "share-files" if provider == "quark" else "planned",
        "bridgeMode": settings.get("drive_bridge_mode", "disabled"),
        "enabledAccounts": len(accounts),
        "ready": any(account["hasCookie"] or account["hasToken"] for account in accounts),
        "mobileAuthReady": any(quark_has_mobile_auth(row["cookie"]) for row in private_accounts),
    }


@app.post("/api/drive/quark/normalize-play")
def quark_normalize_play(payload: DrivePlayNormalizePayload):
    return normalize_quark_play_payload(payload.payload)


@app.post("/api/drive/quark/share/parse")
def quark_parse_share(payload: QuarkSharePayload):
    return drive_parse_share("quark", payload)


@app.post("/api/drive/quark/share/info")
def quark_share_info(payload: QuarkSharePayload):
    return drive_share_info("quark", payload)


@app.post("/api/drive/quark/share/files")
def quark_share_files(payload: QuarkFileListPayload):
    return drive_share_files("quark", payload)


@app.post("/api/drive/quark/share/play")
def quark_share_play(payload: QuarkPlayPayload):
    return drive_share_play("quark", payload)


@app.post("/api/drive/share/parse")
def auto_parse_drive_share(payload: DriveSharePayload):
    return parse_drive_share_url(payload.shareURL)


@app.post("/api/drive/{provider}/share/parse")
def drive_parse_share(provider: str, payload: DriveSharePayload):
    try:
        provider = normalize_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if provider == "quark":
        return parse_quark_share_url(payload.shareURL)
    return parse_drive_share_url(payload.shareURL, provider)


@app.post("/api/drive/{provider}/share/info")
def drive_share_info(provider: str, payload: DriveSharePayload):
    try:
        provider = normalize_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    parsed = parse_drive_share_url(payload.shareURL, provider)
    if not parsed["isValid"]:
        raise HTTPException(status_code=400, detail=f"invalid {provider} share URL")

    if provider == "quark":
        with get_conn() as conn:
            client = quark_client_from_account(fetch_active_drive_account_private(conn, "quark"))
        if client is not None:
            try:
                stoken = client.share_token(parsed["shareId"], parsed.get("password", ""))
                detail = client.list_share_files(parsed["shareId"], stoken, "0")
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": {**parsed, "stokenReady": True},
                    "data": detail,
                }
            except QuarkNativeError as error:
                return {"provider": provider, "mode": "native", "share": parsed, "ready": False, "message": str(error)}

    bridged = call_omnibox_drive_bridge("/drive/info", {"shareURL": payload.shareURL}) if drive_bridge_enabled() else None
    if bridged is None:
        return {
            "provider": provider,
            "mode": "native-scaffold",
            "share": parsed,
            "ready": False,
            "message": f"{provider} native share info resolver is not implemented yet. Configure OMNIBOX_API_URL for temporary fallback.",
        }
    return {"provider": provider, "mode": "omnibox-fallback", "share": parsed, "data": bridged}


@app.post("/api/drive/{provider}/share/files")
def drive_share_files(provider: str, payload: DriveFileListPayload):
    try:
        provider = normalize_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    parsed = parse_drive_share_url(payload.shareURL, provider)
    if not parsed["isValid"]:
        raise HTTPException(status_code=400, detail=f"invalid {provider} share URL")

    if provider == "quark":
        with get_conn() as conn:
            client = quark_client_from_account(fetch_active_drive_account_private(conn, "quark"))
        if client is not None:
            try:
                stoken = client.share_token(parsed["shareId"], parsed.get("password", ""))
                detail = client.list_share_files(parsed["shareId"], stoken, payload.pdirFid)
                files = normalize_drive_files(detail)
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": {**parsed, "stokenReady": True},
                    "data": detail,
                    "files": files,
                    "total": len(files),
                    "hasMore": False,
                }
            except QuarkNativeError as error:
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": parsed,
                    "files": [],
                    "total": 0,
                    "hasMore": False,
                    "ready": False,
                    "message": str(error),
                }

    try:
        bridged = call_omnibox_drive_bridge(
            "/drive/file-list",
            {"shareURL": payload.shareURL, "pdirFid": payload.pdirFid},
        ) if drive_bridge_enabled() else None
    except DriveBridgeError as error:
        return {
            "provider": provider,
            "mode": "omnibox-fallback",
            "share": parsed,
            "files": [],
            "total": 0,
            "hasMore": False,
            "ready": False,
            "message": str(error),
        }
    if bridged is None:
        return {
            "provider": provider,
            "mode": "native-scaffold",
            "share": parsed,
            "files": [],
            "total": 0,
            "hasMore": False,
            "message": f"{provider} native file listing is not implemented yet. Configure OMNIBOX_API_URL for temporary fallback.",
        }
    return {
        "provider": provider,
        "mode": "omnibox-fallback",
        "share": parsed,
        "data": bridged,
        "files": normalize_drive_files(bridged),
    }


@app.post("/api/drive/{provider}/share/play")
def drive_share_play(provider: str, payload: DrivePlayPayload):
    try:
        provider = normalize_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    parsed = parse_drive_share_url(payload.shareURL, provider)
    if not parsed["isValid"]:
        raise HTTPException(status_code=400, detail=f"invalid {provider} share URL")

    if provider == "quark":
        with get_conn() as conn:
            client = quark_client_from_account(fetch_active_drive_account_private(conn, "quark"))
        if client is not None:
            if not payload.shareFidToken:
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": parsed,
                    "ready": False,
                    "requiresShareFidToken": True,
                    "message": "Native Quark play requires shareFidToken from the file list response.",
                }
            try:
                stoken = client.share_token(parsed["shareId"], parsed.get("password", ""))
                save = client.save_share_file(
                    parsed["shareId"],
                    stoken,
                    payload.fid,
                    payload.shareFidToken,
                    "0",
                    payload.pdirFid,
                )
                task_id = (save.get("data") or {}).get("task_id")
                if not task_id:
                    return {
                        "provider": provider,
                        "mode": "native",
                        "share": {**parsed, "stokenReady": True},
                        "ready": False,
                        "save": save,
                        "message": "Quark save task id is empty.",
                    }
                task = client.wait_task(str(task_id))
                saved_fids = (((task.get("data") or {}).get("save_as") or {}).get("save_as_top_fids") or [])
                if not saved_fids:
                    return {
                        "provider": provider,
                        "mode": "native",
                        "share": {**parsed, "stokenReady": True},
                        "ready": False,
                        "save": save,
                        "task": task,
                        "message": "Quark save task did not return saved file ids.",
                    }
                saved_fid = str(saved_fids[0])
                video_play, headers = client.video_play_urls(saved_fid)
                raw_candidates = []
                video_data = video_play.get("data") or {}
                video_items = video_data.get("video_list") or video_data.get("videoList") or []
                for item in video_items:
                    if isinstance(item, dict):
                        raw_candidates.append(
                            {
                                "name": item.get("quality") or item.get("resolution") or item.get("format") or "transcoded",
                                "url": item.get("url") or "",
                                "header": headers,
                            }
                        )
                download, download_headers = client.download_urls([saved_fid])
                for item in download.get("data") or []:
                    if isinstance(item, dict):
                        raw_candidates.append(
                            {
                                "name": "RAW",
                                "url": item.get("download_url") or item.get("url") or "",
                                "header": download_headers,
                            }
                        )
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": {**parsed, "stokenReady": True},
                    "save": save,
                    "task": task,
                    "videoPlay": video_play,
                    "raw": {"urls": raw_candidates, "header": headers},
                    "play": normalize_quark_play_payload({"urls": raw_candidates, "header": headers}),
                }
            except QuarkAuthRequired as error:
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": parsed,
                    "ready": False,
                    "requiresMobileAuth": True,
                    "message": str(error),
                }
            except QuarkNativeError as error:
                return {"provider": provider, "mode": "native", "share": parsed, "ready": False, "message": str(error)}

    try:
        bridged = call_omnibox_drive_bridge(
            "/drive/video-play-info",
            {
                "shareURL": payload.shareURL,
                "fid": payload.fid,
                "flag": payload.flag,
                "getTranscodeUrls": payload.getTranscodeUrls,
            },
        ) if drive_bridge_enabled() else None
    except DriveBridgeError as error:
        return {
            "provider": provider,
            "mode": "omnibox-fallback",
            "share": parsed,
            "ready": False,
            "message": str(error),
        }
    if bridged is None:
        return {
            "provider": provider,
            "mode": "native-scaffold",
            "share": parsed,
            "ready": False,
            "message": f"{provider} native play resolver is not implemented yet. Configure OMNIBOX_API_URL for temporary fallback.",
        }
    return {
        "provider": provider,
        "mode": "omnibox-fallback",
        "share": parsed,
        "raw": bridged,
        "play": normalize_quark_play_payload(bridged),
    }


@app.post("/api/drive/quark/share/videos")
def quark_share_videos(payload: DriveVideosPayload):
    return drive_share_videos("quark", payload)


@app.post("/api/drive/{provider}/share/videos")
def drive_share_videos(provider: str, payload: DriveVideosPayload):
    try:
        provider = normalize_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    parsed = parse_drive_share_url(payload.shareURL, provider)
    if not parsed["isValid"]:
        raise HTTPException(status_code=400, detail=f"invalid {provider} share URL")

    if provider == "quark":
        with get_conn() as conn:
            client = quark_client_from_account(fetch_active_drive_account_private(conn, "quark"))
        if client is not None:
            try:
                stoken = client.share_token(parsed["shareId"], parsed.get("password", ""))
                collected: list[dict[str, Any]] = []
                visited: set[str] = set()

                def visit_native(folder_id: str, depth: int, parent_path: str = "") -> None:
                    if len(collected) >= payload.maxItems or depth > payload.maxDepth or folder_id in visited:
                        return
                    visited.add(folder_id)
                    detail = client.list_share_files(parsed["shareId"], stoken, folder_id)
                    files = normalize_drive_files(detail, parent_path)
                    for item in files:
                        if item["isVideo"]:
                            collected.append(item)
                            if len(collected) >= payload.maxItems:
                                return
                        elif payload.recursive and item["isDir"] and item["fid"]:
                            visit_native(str(item["fid"]), depth + 1, item["path"])

                visit_native(payload.pdirFid, 0)
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": {**parsed, "stokenReady": True},
                    "videoCount": len(collected),
                    "videos": collected[: payload.maxItems],
                }
            except QuarkNativeError as error:
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": parsed,
                    "videoCount": 0,
                    "videos": [],
                    "ready": False,
                    "message": str(error),
                }

    collected: list[dict[str, Any]] = []
    visited: set[str] = set()

    def visit(folder_id: str, depth: int, parent_path: str = "") -> None:
        if len(collected) >= payload.maxItems or depth > payload.maxDepth or folder_id in visited:
            return
        visited.add(folder_id)
        try:
            bridged = call_omnibox_drive_bridge(
                "/drive/file-list",
                {"shareURL": payload.shareURL, "pdirFid": folder_id},
            ) if drive_bridge_enabled() else None
        except DriveBridgeError:
            return
        if bridged is None:
            return
        files = normalize_drive_files(bridged, parent_path)
        for item in files:
            if item["isVideo"]:
                collected.append(item)
                if len(collected) >= payload.maxItems:
                    return
            elif payload.recursive and item["isDir"] and item["fid"]:
                visit(str(item["fid"]), depth + 1, item["path"])

    visit(payload.pdirFid, 0)
    return {
        "provider": provider,
        "mode": "omnibox-fallback",
        "share": parsed,
        "videoCount": len(collected),
        "videos": collected[: payload.maxItems],
    }


@app.post("/api/drive/quark/share/play-best")
def quark_share_play_best(payload: DriveVideosPayload):
    return drive_share_play_best("quark", payload)


@app.post("/api/drive/{provider}/share/play-best")
def drive_share_play_best(provider: str, payload: DriveVideosPayload):
    videos_payload = drive_share_videos(provider, payload)
    best_file = select_best_video_file(videos_payload["videos"])
    if best_file is None:
        return {
            **videos_payload,
            "ready": False,
            "message": "No playable video file found in this share.",
        }

    play_payload = DrivePlayPayload(
        shareURL=payload.shareURL,
        fid=best_file["fid"],
        flag=best_file["name"],
        shareFidToken=best_file.get("shareFidToken", ""),
        pdirFid=best_file.get("parentFid", "0"),
        getTranscodeUrls=True,
    )
    play = drive_share_play(provider, play_payload)
    return {
        **videos_payload,
        "selectedFile": best_file,
        "play": play["play"] if isinstance(play, dict) and "play" in play else play,
    }


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
