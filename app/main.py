import base64
import copy
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_conn, init_db, row_to_drive_account, row_to_source, row_to_subscription
from .baidu import BaiduClient, BaiduNativeError, parse_baidu_share_url
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
TVBOX_JAR_PATH = BASE_DIR / "artifacts" / "colvins-tvbox-spider.jar"
TVBOX_SPIDER_TXT_PATH = BASE_DIR / "artifacts" / "colvins-tvbox-spider.txt"
DRIVE_PARSE_CACHE_TTL_SECONDS = 300
DRIVE_FILES_CACHE_TTL_SECONDS = 300
TVBOX_DETAIL_CACHE_TTL_SECONDS = 120
_drive_parse_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_drive_files_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
_tvbox_detail_cache: dict[tuple[int, str], tuple[float, Any]] = {}
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def startup() -> None:
    init_db()


def _cache_get(cache: dict[Any, tuple[float, Any]], key: Any) -> Any | None:
    entry = cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if expires_at <= time.monotonic():
        cache.pop(key, None)
        return None
    return copy.deepcopy(value)


def _cache_set(cache: dict[Any, tuple[float, Any]], key: Any, ttl_seconds: int, value: Any) -> Any:
    cache[key] = (time.monotonic() + ttl_seconds, copy.deepcopy(value))
    return value


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


def baidu_client_from_account(row: sqlite3.Row | None) -> BaiduClient | None:
    if row is None or not row["cookie"]:
        return None
    return BaiduClient(row["cookie"], row["user_agent"])


def drive_bridge_enabled() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'drive_bridge_mode'").fetchone()
    return bool(row and row["value"] == "omnibox-fallback")


def _quark_payload_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = ((payload.get("data") or {}).get("list") or [])
    return [item for item in items if isinstance(item, dict)]


def _baidu_payload_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("list") or ((payload.get("data") or {}).get("list") or [])
    return [item for item in items if isinstance(item, dict)]


def _baidu_find_file(payload: dict[str, Any], fid: str) -> dict[str, Any] | None:
    target = str(fid or "")
    for item in _baidu_payload_items(payload):
        if str(item.get("fs_id") or item.get("fsid") or item.get("fid") or "") == target:
            return item
    return None


def _quark_find_child(client: QuarkClient, parent_fid: str, name: str) -> dict[str, Any] | None:
    target = (name or "").strip()
    if not target:
        return None
    for item in _quark_payload_items(client.list_files(parent_fid)):
        if str(item.get("file_name") or item.get("name") or "").strip() == target:
            return item
    return None


def _quark_saved_fids_from_task(client: QuarkClient, save: dict[str, Any]) -> list[str]:
    task_id = (save.get("data") or {}).get("task_id")
    if not task_id:
        raise QuarkNativeError("Quark save task id is empty.")
    task = client.wait_task(str(task_id))
    saved_fids = (((task.get("data") or {}).get("save_as") or {}).get("save_as_top_fids") or [])
    if not saved_fids:
        raise QuarkNativeError("Quark save task did not return saved file ids.")
    return [str(fid) for fid in saved_fids]


def _quark_play_candidates_for_saved_fid(client: QuarkClient, saved_fid: str, fallback_name: str) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    headers = {'User-Agent': client.user_agent, 'Referer': 'https://pan.quark.cn/', 'Cookie': client.cookie}
    video_play: dict[str, Any] = {'colvins': {}}
    video_error = ""
    try:
        video_play, headers = client.video_play_urls(saved_fid)
    except QuarkNativeError as error:
        video_error = str(error)[:240]
        video_play.setdefault('colvins', {})['videoPlayError'] = video_error

    play_candidates: list[dict[str, Any]] = []
    try:
        play_candidates.append(
            {
                'name': 'raw',
                'url': client.download_url(saved_fid),
                'header': headers,
            }
        )
    except QuarkNativeError as error:
        video_play.setdefault('colvins', {})['rawError'] = str(error)[:240]

    video_data = video_play.get('data') or {}
    video_items = video_data.get('video_list') or video_data.get('videoList') or []
    for item in video_items:
        if isinstance(item, dict):
            video_info = item.get('video_info') if isinstance(item.get('video_info'), dict) else {}
            play_candidates.append(
                {
                    'name': item.get('quality') or item.get('resolution') or item.get('format') or fallback_name or 'transcoded',
                    'url': item.get('url') or video_info.get('url') or '',
                    'header': headers,
                }
            )

    if not play_candidates and video_error:
        raise QuarkNativeError(video_error)
    return play_candidates, headers, video_play


def _quark_cached_saved_fid(provider: str, share_id: str, share_fid: str, file_path: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT saved_fid
            FROM drive_file_cache
            WHERE provider = ? AND share_id = ? AND share_fid = ? AND file_path = ?
            """,
            (provider, share_id, share_fid, file_path or ""),
        ).fetchone()
    return str(row["saved_fid"]) if row else ""


def _quark_store_saved_fid(provider: str, share_id: str, share_fid: str, file_path: str, saved_fid: str, save_mode: str) -> None:
    if not saved_fid:
        return
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO drive_file_cache (provider, share_id, share_fid, file_path, saved_fid, save_mode)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, share_id, share_fid, file_path)
            DO UPDATE SET saved_fid = excluded.saved_fid,
                          save_mode = excluded.save_mode,
                          updated_at = CURRENT_TIMESTAMP
            """,
            (provider, share_id, share_fid, file_path or "", saved_fid, save_mode),
        )


def _quark_save_ancestor_and_resolve_fid(
    client: QuarkClient,
    share_id: str,
    stoken: str,
    file_path: str,
) -> tuple[str, dict[str, Any]]:
    path_parts = [part for part in (file_path or "").split("/") if part]
    if len(path_parts) < 2:
        raise QuarkNativeError("Nested Quark fallback requires filePath from the share file list.")

    root_name = path_parts[0]
    root_item = next(
        (
            item
            for item in _quark_payload_items(client.list_share_files(share_id, stoken, "0"))
            if str(item.get("file_name") or "").strip() == root_name
        ),
        None,
    )
    if root_item is None:
        raise QuarkNativeError(f"Quark share root item not found: {root_name}")

    save = client.save_share_file(
        share_id,
        stoken,
        str(root_item.get("fid") or ""),
        str(root_item.get("share_fid_token") or ""),
        "0",
        "0",
    )
    saved_fid = _quark_saved_fids_from_task(client, save)[0]
    current_fid = saved_fid
    for part in path_parts[1:]:
        child = _quark_find_child(client, current_fid, part)
        if child is None:
            raise QuarkNativeError(f"Saved Quark child not found: {part}")
        current_fid = str(child.get("fid") or "")
    return current_fid, save


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
    spider = tvbox_spider_with_md5(base_url)
    sites = []
    for source in subscription["sources"]:
        sites.append(
            {
                "key": f"csp_source_{source['id']}",
                "name": source["name"],
                "type": 3,
                "api": "csp_ColvinsTvBox",
                "jar": spider,
                "searchable": 1,
                "changeable": 1,
                "quickSearch": 1,
                "filterable": 1,
                "ext": urlencode(
                    {
                        "baseUrl": base_url,
                        "sourceId": str(source["id"]),
                        "sourceName": source["name"],
                    }
                ),
            }
        )
    return {
        "sites": sites,
        "lives": [],
        "parses": [],
        "flags": [],
        "spider": spider,
        "ijk": {},
    }


def tvbox_spider_with_md5(base_url: str) -> str:
    if TVBOX_SPIDER_TXT_PATH.exists():
        digest = hashlib.md5(TVBOX_SPIDER_TXT_PATH.read_bytes()).hexdigest()
        return f"{base_url}/api/tvbox/spider/colvins-tvbox-spider.txt;md5;{digest}"
    if TVBOX_JAR_PATH.exists():
        digest = hashlib.md5(TVBOX_JAR_PATH.read_bytes()).hexdigest()
        return f"{base_url}/api/tvbox/jar/colvins-tvbox-spider.jar;md5;{digest}"
    return ""


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


def _normalize_vod_play_sources(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_normalize_vod_play_sources(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    normalized = {key: _normalize_vod_play_sources(value) for key, value in payload.items()}
    sources = normalized.get("vod_play_sources")
    if isinstance(sources, list) and sources and not normalized.get("vod_play_from") and not normalized.get("vod_play_url"):
        flags: list[str] = []
        groups: list[str] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            name = str(source.get("name") or source.get("flag") or "").strip()
            episodes = source.get("episodes")
            if not name or not isinstance(episodes, list):
                continue
            encoded_episodes: list[str] = []
            for episode in episodes:
                if not isinstance(episode, dict):
                    continue
                episode_name = str(episode.get("name") or episode.get("title") or "").strip()
                play_id = str(episode.get("playId") or episode.get("id") or episode.get("url") or "").strip()
                if episode_name and play_id:
                    encoded_episodes.append(f"{episode_name}${play_id}")
            if encoded_episodes:
                flags.append(name)
                groups.append("#".join(encoded_episodes))
        if flags and groups:
            normalized["vod_play_from"] = "$$$".join(flags)
            normalized["vod_play_url"] = "$$$".join(groups)
    return normalized


def _normalize_play_result(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_normalize_play_result(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    normalized = {key: _normalize_play_result(value) for key, value in payload.items()}
    urls = normalized.get("urls")
    url_value = normalized.get("url")
    url_is_playable_string = isinstance(url_value, str) and bool(url_value.strip())
    if not url_is_playable_string and isinstance(url_value, list):
        urls = url_value if not isinstance(urls, list) else urls
    if isinstance(urls, list) and urls and not url_is_playable_string:
        first = next((item for item in urls if isinstance(item, dict) and item.get("url")), None)
        if first is None:
            first_text = next((item for item in urls if isinstance(item, str) and item.strip()), None)
            if first_text:
                normalized["url"] = first_text
        if first:
            normalized["url"] = first.get("url")
            if not normalized.get("header") and isinstance(first.get("header"), dict):
                normalized["header"] = first.get("header")
            if not normalized.get("headers") and isinstance(first.get("headers"), dict):
                normalized["headers"] = first.get("headers")
    if normalized.get("url") and "proxyStreaming" not in normalized:
        normalized["proxyStreaming"] = False
    return normalized


def _normalize_runtime_payload(payload: Any) -> Any:
    return _normalize_play_result(_normalize_vod_play_sources(payload))


def _runtime_envelope(payload: dict[str, Any]) -> JSONResponse:
    payload = _normalize_runtime_payload(payload)
    if isinstance(payload, dict) and "data" in payload:
        return JSONResponse(payload)
    return JSONResponse({"data": payload})


def _runtime_raw(source_id: int, action: str, params: dict[str, Any], request: Request) -> JSONResponse:
    try:
        with get_conn() as conn:
            payload = _normalize_runtime_payload(execute_source(conn, source_id, action, params, _base_url_from_request(request)))
    except RuntimeError as error:
        if str(error) == "source not found":
            raise HTTPException(status_code=404, detail="source not found") from error
        raise
    if action in ("home", "category", "search"):
        payload = _normalize_tvbox_listing_payload(payload, action, request)
    return JSONResponse(payload)


def _normalize_tvbox_item(item: dict[str, Any], request: Request | None = None) -> dict[str, Any]:
    normalized = dict(item)
    defaults = {
        "vod_id": "",
        "vod_name": "",
        "vod_pic": "",
        "vod_content": "",
        "vod_year": "",
        "vod_area": "",
        "vod_lang": "",
        "vod_actor": "",
        "vod_director": "",
        "vod_play_url": "",
        "vod_play_from": "",
        "type_name": "",
        "vod_time": "",
        "vod_remarks": "",
        "vod_sub": "",
        "vod_tag": "",
        "vod_class": "",
    }
    for key, value in defaults.items():
        normalized.setdefault(key, value)
    return normalized


def _normalize_tvbox_listing_payload(payload: Any, action: str, request: Request) -> Any:
    if not isinstance(payload, dict):
        return payload

    items = payload.get("list")
    if not isinstance(items, list):
        return payload

    normalized_items = [
        _normalize_tvbox_item(item, request) if isinstance(item, dict) else item
        for item in items
    ]

    normalized = dict(payload)
    normalized["list"] = normalized_items

    total = normalized.get("total")
    try:
        total_value = int(total)
    except (TypeError, ValueError):
        total_value = 0
    if total_value <= 0:
        total_value = len(normalized_items)

    page = normalized.get("page")
    try:
        page_value = int(page)
    except (TypeError, ValueError):
        page_value = 1
    if page_value <= 0:
        page_value = 1

    pagecount = normalized.get("pagecount")
    try:
        pagecount_value = int(pagecount)
    except (TypeError, ValueError):
        pagecount_value = 0

    if action == "home":
        home_payload = {
            "class": normalized.get("class") or [],
            "list": normalized_items,
        }
        if "filters" in normalized:
            home_payload["filters"] = normalized.get("filters")
        return home_payload

    if pagecount_value < 0:
        pagecount_value = 0

    listing_payload = {
        "list": normalized_items,
        "page": page_value,
        "pagecount": pagecount_value,
        "total": total_value,
    }
    return listing_payload


def _tvbox_detail_payload(source_id: int, payload: Any, request: Request) -> Any:
    if not isinstance(payload, dict):
        return payload

    items = payload.get("list")
    if not isinstance(items, list):
        return payload

    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            normalized_items.append(item)
            continue
        normalized_item = _normalize_tvbox_item(item, request)
        sources = item.get("vod_play_sources")
        if isinstance(sources, list) and sources:
            grouped_sources: dict[str, list[str]] = {}
            for source in sources:
                if not isinstance(source, dict):
                    continue
                name = str(source.get("name") or source.get("flag") or "").strip()
                episodes = source.get("episodes")
                if not name or not isinstance(episodes, list):
                    continue
                base_name = name
                for suffix in ("-本地代理", "-服务端代理", "-直连"):
                    if base_name.endswith(suffix):
                        base_name = base_name[: -len(suffix)].strip() or base_name
                        break
                if base_name.endswith("1") and "网盘" in base_name:
                    base_name = base_name[:-1].strip() or base_name
                encoded_episodes = grouped_sources.setdefault(base_name, [])
                for episode in episodes:
                    if not isinstance(episode, dict):
                        continue
                    episode_name = str(episode.get("name") or episode.get("title") or "").strip()
                    play_id = str(episode.get("playId") or episode.get("id") or episode.get("url") or "").strip()
                    if episode_name and play_id:
                        encoded = f"{episode_name}${play_id}"
                        if encoded not in encoded_episodes:
                            encoded_episodes.append(encoded)
            flags = []
            groups = []
            for name, episodes in grouped_sources.items():
                if episodes:
                    flags.append(name)
                    groups.append("#".join(episodes))
            if flags and groups:
                normalized_item["vod_play_from"] = "$$$".join(flags)
                normalized_item["vod_play_url"] = "$$$".join(groups)
        normalized_items.append(normalized_item)

    normalized = dict(payload)
    normalized["list"] = normalized_items
    return normalized


def _tvbox_episode_group(source_id: int, flag: str, group: str, request: Request) -> str:
    episodes: list[str] = []
    base_url = _base_url_from_request(request)
    for episode in group.split("#"):
        if "$" not in episode:
            continue
        name, play_id = episode.split("$", 1)
        episodes.append(
            f"{name}${base_url}/api/tvbox/source/{source_id}/play-url?flag={quote_url_value(flag)}&play={quote_url_value(play_id)}"
        )
    return "#".join(episodes)


def _tvbox_unwrap_play_id(play_id: str) -> str:
    from urllib.parse import parse_qs, unquote, urlparse

    text = str(play_id or "").strip()
    if not text.startswith(("http://", "https://")):
        return text
    try:
        parsed = urlparse(text)
    except Exception:
        return text
    if not parsed.path.endswith("/play-url"):
        return text
    query = parse_qs(parsed.query, keep_blank_values=True)
    value = query.get("play") or query.get("playId") or query.get("id")
    if not value:
        return text
    return unquote(str(value[0] or "")).strip() or text


def _tvbox_direct_play_url(play_id: str) -> str | None:
    from urllib.parse import unquote

    candidate = unquote(str(play_id or "").split("|||", 1)[0]).strip()
    if candidate.startswith("push://"):
        candidate = candidate.removeprefix("push://").strip()
    if not candidate.startswith(("http://", "https://")):
        return None
    lowered = candidate.lower()
    if "127.0.0.1" in lowered or "localhost" in lowered or "/proxy" in lowered:
        return None
    if "pan.quark.cn/s/" in lowered or "pan.baidu.com/" in lowered:
        return None
    return candidate


def _tvbox_candidate_headers(item: dict[str, Any], fallback: dict[str, str]) -> dict[str, str]:
    header_value = item.get("headers")
    if isinstance(header_value, dict) and header_value:
        return {str(key): str(value) for key, value in header_value.items() if key and value is not None}
    header_value = item.get("header")
    if isinstance(header_value, dict) and header_value:
        return {str(key): str(value) for key, value in header_value.items() if key and value is not None}
    return dict(fallback)


def _tvbox_collect_candidate(candidates: list[dict[str, Any]], name: str, url: str, headers: dict[str, str], format_name: str) -> None:
    direct_url = _tvbox_direct_play_url(url)
    if not direct_url:
        return
    entry = {
        "name": (name or "直连").strip() or "直连",
        "url": direct_url,
        "headers": headers,
        "format": format_name or "",
    }
    if any(item["name"] == entry["name"] and item["url"] == entry["url"] for item in candidates):
        return
    candidates.append(entry)


def _tvbox_play_options(payload: Any) -> dict[str, Any]:
    normalized = _normalize_runtime_payload(payload)
    if not isinstance(normalized, dict):
        return {"candidates": [], "message": "invalid play payload", "proxyStreaming": False}

    default_headers = {}
    if isinstance(normalized.get("header"), dict):
        default_headers = {str(key): str(value) for key, value in normalized["header"].items() if key and value is not None}
    elif isinstance(normalized.get("headers"), dict):
        default_headers = {str(key): str(value) for key, value in normalized["headers"].items() if key and value is not None}

    format_name = str(normalized.get("format") or "")
    candidates: list[dict[str, Any]] = []

    urls_value = normalized.get("urls")
    if isinstance(urls_value, list):
        if all(isinstance(item, dict) for item in urls_value):
            for item in urls_value:
                _tvbox_collect_candidate(
                    candidates,
                    str(item.get("name") or item.get("quality") or item.get("format") or "直连"),
                    str(item.get("url") or ""),
                    _tvbox_candidate_headers(item, default_headers),
                    str(item.get("format") or format_name),
                )
        elif len(urls_value) >= 2:
            pairs = iter(urls_value)
            for name, url in zip(pairs, pairs):
                _tvbox_collect_candidate(candidates, str(name), str(url), dict(default_headers), format_name)

    url_value = normalized.get("url")
    if isinstance(url_value, str) and not candidates:
        _tvbox_collect_candidate(candidates, str(normalized.get("name") or "直连"), url_value, dict(default_headers), format_name)
    elif isinstance(url_value, list) and len(url_value) >= 2 and not candidates:
        pairs = iter(url_value)
        for name, url in zip(pairs, pairs):
            _tvbox_collect_candidate(candidates, str(name), str(url), dict(default_headers), format_name)

    return {
        "candidates": candidates,
        "headers": default_headers,
        "format": format_name,
        "proxyStreaming": False,
        "message": str(normalized.get("message") or normalized.get("msg") or ""),
    }


def quote_url_value(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value or ""), safe="")


def _decode_colvins_file_ref(value: str) -> dict[str, Any] | None:
    text = str(value or "")
    if not text.startswith("colvins:"):
        return None
    encoded = text.split(":", 1)[1]
    if not encoded:
        return None
    parts = encoded.split(":")
    if len(parts) >= 3:
        fid = str(parts[0] or "").strip()
        share_fid_token = str(parts[1] or "").strip()
        file_name = ":".join(parts[2:]).strip()
        if fid and share_fid_token:
            return {
                "fid": fid,
                "shareFidToken": share_fid_token,
                "name": file_name,
                "path": file_name,
                "parentFid": "0",
                "pdirFid": "0",
            }
    padding = "=" * (-len(encoded) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode((encoded + padding).encode()).decode())
    except Exception:
        return None


def _tvbox_drive_play_fallback(flag: str, play_id: str):
    parts = str(play_id or "").split("|")
    if len(parts) < 2:
        return None
    share_url = str(parts[0] or "").strip()
    parsed = parse_drive_share_url(share_url)
    if not isinstance(parsed, dict) or not parsed.get("isValid"):
        return None
    file_info = _decode_colvins_file_ref(parts[1])
    if not isinstance(file_info, dict):
        return None
    provider = str(parsed.get("provider") or "quark")

    def build_payload(info: dict[str, Any]) -> DrivePlayPayload:
        return DrivePlayPayload(
            shareURL=share_url,
            fid=str(info.get("fid") or ""),
            flag=str(flag or info.get("name") or ""),
            filePath=str(info.get("path") or info.get("name") or ""),
            shareFidToken=str(info.get("shareFidToken") or info.get("share_fid_token") or ""),
            pdirFid=str(info.get("parentFid") or info.get("pdirFid") or "0"),
            getTranscodeUrls=True,
        )

    payload = build_payload(file_info)
    result = drive_share_play(provider, payload)
    if provider == "quark" and not _tvbox_drive_play_result_ready(result):
        enriched = _resolve_drive_file_info(provider, share_url, file_info)
        if enriched is not None and enriched != file_info:
            payload = build_payload(enriched)
            result = drive_share_play(provider, payload)
    if isinstance(result, dict):
        raw = result.get("raw")
        play = result.get("play")
        raw_urls = raw.get("urls") if isinstance(raw, dict) else None
        if isinstance(raw_urls, list) and raw_urls:
            selected = play.get("selected") if isinstance(play, dict) else None
            top_header = {}
            if isinstance(selected, dict) and isinstance(selected.get("headers"), dict):
                top_header = {
                    str(key): str(value)
                    for key, value in selected.get("headers", {}).items()
                    if key and value is not None
                }
            elif isinstance(raw.get("header"), dict):
                top_header = {
                    str(key): str(value)
                    for key, value in raw.get("header", {}).items()
                    if key and value is not None
                }

            urls = []
            for item in raw_urls:
                if not isinstance(item, dict):
                    continue
                candidate_url = str(item.get("url") or "").strip()
                if not candidate_url:
                    continue
                urls.append(
                    {
                        "name": str(item.get("name") or item.get("quality") or item.get("format") or "播放"),
                        "url": candidate_url,
                    }
                )
            if urls:
                return {
                    "urls": urls,
                    "flag": share_url,
                    "header": top_header,
                    "parse": 0,
                    "danmaku": [],
                }
    return None


def _tvbox_drive_play_result_ready(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    raw = result.get("raw")
    if not isinstance(raw, dict):
        return False
    raw_urls = raw.get("urls")
    return isinstance(raw_urls, list) and bool(raw_urls)


def _resolve_drive_file_info(provider: str, share_url: str, file_info: dict[str, Any]) -> dict[str, Any] | None:
    if provider != "quark":
        return None
    target_fid = str(file_info.get("fid") or "").strip()
    target_token = str(file_info.get("shareFidToken") or file_info.get("share_fid_token") or "").strip()
    target_name = str(file_info.get("name") or file_info.get("path") or "").split("/")[-1].strip()
    try:
        result = drive_share_videos(
            provider,
            DriveVideosPayload(
                shareURL=share_url,
                pdirFid="0",
                recursive=True,
                maxDepth=8,
                maxItems=400,
            ),
        )
    except Exception:
        return None
    videos = result.get("videos") if isinstance(result, dict) else None
    if not isinstance(videos, list):
        return None

    def merge(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **file_info,
            "fid": str(item.get("fid") or file_info.get("fid") or ""),
            "name": str(item.get("name") or file_info.get("name") or ""),
            "path": str(item.get("path") or file_info.get("path") or item.get("name") or ""),
            "shareFidToken": str(item.get("shareFidToken") or file_info.get("shareFidToken") or file_info.get("share_fid_token") or ""),
            "parentFid": str(item.get("parentFid") or file_info.get("parentFid") or file_info.get("pdirFid") or "0"),
            "pdirFid": str(item.get("parentFid") or file_info.get("pdirFid") or file_info.get("parentFid") or "0"),
        }

    for item in videos:
        if isinstance(item, dict) and target_fid and str(item.get("fid") or "").strip() == target_fid:
            return merge(item)
    for item in videos:
        if isinstance(item, dict) and target_token and str(item.get("shareFidToken") or "").strip() == target_token:
            return merge(item)
    for item in videos:
        if isinstance(item, dict) and target_name and str(item.get("name") or "").strip() == target_name:
            return merge(item)
    return None


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
        "nativeResolver": "share-files" if provider in {"quark", "baidu"} else "planned",
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
    cache_key = ("auto", payload.shareURL)
    cached = _cache_get(_drive_parse_cache, cache_key)
    if cached is not None:
        return cached
    result = parse_drive_share_url(payload.shareURL)
    return _cache_set(_drive_parse_cache, cache_key, DRIVE_PARSE_CACHE_TTL_SECONDS, result)


@app.post("/api/drive/{provider}/share/parse")
def drive_parse_share(provider: str, payload: DriveSharePayload):
    try:
        provider = normalize_provider(provider)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    cache_key = (provider, payload.shareURL)
    cached = _cache_get(_drive_parse_cache, cache_key)
    if cached is not None:
        return cached
    if provider == "quark":
        result = parse_quark_share_url(payload.shareURL)
        return _cache_set(_drive_parse_cache, cache_key, DRIVE_PARSE_CACHE_TTL_SECONDS, result)
    if provider == "baidu":
        result = parse_baidu_share_url(payload.shareURL)
        return _cache_set(_drive_parse_cache, cache_key, DRIVE_PARSE_CACHE_TTL_SECONDS, result)
    result = parse_drive_share_url(payload.shareURL, provider)
    return _cache_set(_drive_parse_cache, cache_key, DRIVE_PARSE_CACHE_TTL_SECONDS, result)


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

    if provider == "baidu":
        with get_conn() as conn:
            client = baidu_client_from_account(fetch_active_drive_account_private(conn, "baidu"))
        if client is not None:
            try:
                share, detail = client.list_share_files(payload.shareURL)
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": share,
                    "data": detail,
                    "ready": True,
                }
            except BaiduNativeError as error:
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
    cache_key = (provider, payload.shareURL, payload.pdirFid)
    cached = _cache_get(_drive_files_cache, cache_key)
    if cached is not None:
        return cached
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
                result = {
                    "provider": provider,
                    "mode": "native",
                    "share": {**parsed, "stokenReady": True},
                    "data": detail,
                    "files": files,
                    "total": len(files),
                    "hasMore": False,
                }
                return _cache_set(_drive_files_cache, cache_key, DRIVE_FILES_CACHE_TTL_SECONDS, result)
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

    if provider == "baidu":
        with get_conn() as conn:
            client = baidu_client_from_account(fetch_active_drive_account_private(conn, "baidu"))
        if client is not None:
            try:
                share, detail = client.list_share_files(payload.shareURL)
                files = normalize_drive_files(detail)
                result = {
                    "provider": provider,
                    "mode": "native",
                    "share": share,
                    "data": detail,
                    "files": files,
                    "total": len(files),
                    "hasMore": False,
                }
                return _cache_set(_drive_files_cache, cache_key, DRIVE_FILES_CACHE_TTL_SECONDS, result)
            except BaiduNativeError as error:
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
    result = {
        "provider": provider,
        "mode": "omnibox-fallback",
        "share": parsed,
        "data": bridged,
        "files": normalize_drive_files(bridged),
    }
    return _cache_set(_drive_files_cache, cache_key, DRIVE_FILES_CACHE_TTL_SECONDS, result)


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
                save_mode = "file"
                cached_fid = _quark_cached_saved_fid(provider, parsed["shareId"], payload.fid, payload.filePath)
                cached_error = ""
                if cached_fid:
                    try:
                        raw_candidates, headers, video_play = _quark_play_candidates_for_saved_fid(
                            client,
                            cached_fid,
                            payload.flag,
                        )
                        return {
                            "provider": provider,
                            "mode": "native",
                            "share": {**parsed, "stokenReady": True},
                            "saveMode": "cached",
                            "savedFid": cached_fid,
                            "raw": {"urls": raw_candidates, "header": headers},
                            "play": normalize_quark_play_payload({"urls": raw_candidates, "header": headers}),
                        }
                    except QuarkNativeError as error:
                        cached_error = str(error)[:240]
                try:
                    save = client.save_share_file(
                        parsed["shareId"],
                        stoken,
                        payload.fid,
                        payload.shareFidToken,
                        "0",
                        payload.pdirFid,
                    )
                    saved_fid = _quark_saved_fids_from_task(client, save)[0]
                except QuarkNativeError as direct_save_error:
                    save_mode = "ancestor"
                    saved_fid, save = _quark_save_ancestor_and_resolve_fid(
                        client,
                        parsed["shareId"],
                        stoken,
                        payload.filePath,
                    )
                    save["directSaveError"] = str(direct_save_error)[:240]
                if cached_error:
                    save["cachedPlayError"] = cached_error
                raw_candidates, headers, video_play = _quark_play_candidates_for_saved_fid(
                    client,
                    saved_fid,
                    payload.flag,
                )
                _quark_store_saved_fid(provider, parsed["shareId"], payload.fid, payload.filePath, saved_fid, save_mode)
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": {**parsed, "stokenReady": True},
                    "saveMode": save_mode,
                    "savedFid": saved_fid,
                    "save": save,
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

    if provider == "baidu":
        with get_conn() as conn:
            client = baidu_client_from_account(fetch_active_drive_account_private(conn, "baidu"))
        if client is not None:
            try:
                share, detail = client.list_share_files(payload.shareURL)
                item = _baidu_find_file(detail, payload.fid)
                if item is None:
                    raise BaiduNativeError("Baidu file was not found in the share list.")
                url = str(item.get("dlink") or item.get("download_link") or "").strip()
                if not url:
                    raise BaiduNativeError("Baidu file list did not return a playable dlink.")
                headers = client.play_headers()
                raw_candidates = [{"name": payload.flag or item.get("server_filename") or item.get("name") or "Baidu", "url": url, "header": headers}]
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": share,
                    "raw": {"urls": raw_candidates, "header": headers},
                    "play": normalize_quark_play_payload({"urls": raw_candidates, "header": headers}),
                }
            except BaiduNativeError as error:
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

    if provider == "baidu":
        with get_conn() as conn:
            client = baidu_client_from_account(fetch_active_drive_account_private(conn, "baidu"))
        if client is not None:
            try:
                share, detail = client.list_share_files(payload.shareURL)
                videos = video_files_from_payload(detail)
                return {
                    "provider": provider,
                    "mode": "native",
                    "share": share,
                    "videoCount": len(videos),
                    "videos": videos[: payload.maxItems],
                }
            except BaiduNativeError as error:
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
        filePath=best_file.get("path", ""),
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


@app.get("/api/tvbox/jar/colvins-tvbox-spider.jar")
def tvbox_spider_jar():
    if not TVBOX_JAR_PATH.exists():
        raise HTTPException(status_code=404, detail="tvbox spider jar not built")
    return FileResponse(TVBOX_JAR_PATH, media_type="application/java-archive", filename="colvins-tvbox-spider.jar")


@app.get("/api/tvbox/spider/colvins-tvbox-spider.txt")
def tvbox_spider_txt():
    if not TVBOX_SPIDER_TXT_PATH.exists():
        raise HTTPException(status_code=404, detail="tvbox spider txt not built")
    return FileResponse(TVBOX_SPIDER_TXT_PATH, media_type="application/octet-stream", filename="colvins-tvbox-spider.txt")


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
        payload = execute_source(
            conn,
            source_id,
            "play",
            {"flag": flag, "playId": resolved_play_id, "id": resolved_play_id},
            _base_url_from_request(request),
        )
    options = _tvbox_play_options(payload)
    if options.get("candidates"):
        return _runtime_envelope(payload)
    fallback = _tvbox_drive_play_fallback(flag, resolved_play_id)
    if fallback is not None:
        return _runtime_envelope(fallback)
    return _runtime_envelope(payload)


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


@app.get("/api/tvbox/source/{source_id}")
def runtime_tvbox_json(
    source_id: int,
    request: Request,
    ac: str = "",
    t: str = "",
    tid: str = "",
    id: str = "",
    ids: str = "",
    play: str = "",
    flag: str = "",
    wd: str = "",
    keyword: str = "",
    pg: int = 1,
    page: int = 1,
):
    action = ac.lower().strip()
    resolved_page = page or pg or 1
    resolved_category_id = t or tid or id
    resolved_video_id = ids or id
    if wd or keyword:
        return runtime_tvbox_search(source_id, request, wd, keyword, pg, page)
    if action in ("videolist", "category"):
        return runtime_tvbox_category(source_id, request, resolved_category_id, resolved_category_id, pg, page)
    if action == "detail":
        if not resolved_video_id and resolved_category_id:
            return runtime_tvbox_category(source_id, request, resolved_category_id, resolved_category_id, pg, page)
        return runtime_tvbox_detail(source_id, request, resolved_video_id, resolved_video_id, resolved_video_id)
    if resolved_category_id and action not in ("play",):
        return runtime_tvbox_category(source_id, request, resolved_category_id, resolved_category_id, pg, page)
    if action == "play":
        resolved_play_id = play or id or ids
        return runtime_tvbox_play(source_id, request, flag, resolved_play_id, resolved_play_id, resolved_play_id)
    return runtime_tvbox_home(source_id, request)


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
    cache_key = (source_id, resolved_video_id)
    cached = _cache_get(_tvbox_detail_cache, cache_key)
    if cached is not None:
        return JSONResponse(cached)
    with get_conn() as conn:
        payload = execute_source(
            conn,
            source_id,
            "detail",
            {"videoId": resolved_video_id, "id": resolved_video_id, "ids": resolved_video_id},
            _base_url_from_request(request),
        )
    normalized = _tvbox_detail_payload(source_id, payload, request)
    _cache_set(_tvbox_detail_cache, cache_key, TVBOX_DETAIL_CACHE_TTL_SECONDS, normalized)
    return JSONResponse(normalized)


@app.get("/api/tvbox/source/{source_id}/play")
def runtime_tvbox_play(source_id: int, request: Request, flag: str = "", id: str = "", playId: str = "", play: str = ""):
    resolved_play_id = _tvbox_unwrap_play_id(play or playId or id)
    payload = _runtime_raw(
        source_id,
        "play",
        {"flag": flag, "playId": resolved_play_id, "id": resolved_play_id},
        request,
    )
    if not (_tvbox_play_options(payload).get("candidates") or []):
        fallback = _tvbox_drive_play_fallback(flag, resolved_play_id)
        if fallback is not None:
            return JSONResponse(fallback)
    return payload


@app.get("/api/tvbox/source/{source_id}/play-options")
def runtime_tvbox_play_options(source_id: int, request: Request, flag: str = "", id: str = "", playId: str = "", play: str = ""):
    resolved_play_id = _tvbox_unwrap_play_id(play or playId or id)
    with get_conn() as conn:
        payload = execute_source(
            conn,
            source_id,
            "play",
            {"flag": flag, "playId": resolved_play_id, "id": resolved_play_id},
            _base_url_from_request(request),
        )
    options = _tvbox_play_options(payload)
    if options.get("candidates"):
        return JSONResponse(options)
    fallback = _tvbox_drive_play_fallback(flag, resolved_play_id)
    if fallback is not None:
        return JSONResponse(_tvbox_play_options(fallback))
    return JSONResponse(options)


@app.get("/api/tvbox/source/{source_id}/play-url")
def runtime_tvbox_play_url(source_id: int, request: Request, flag: str = "", id: str = "", playId: str = "", play: str = ""):
    resolved_play_id = _tvbox_unwrap_play_id(play or playId or id)
    with get_conn() as conn:
        payload = _normalize_runtime_payload(execute_source(
            conn,
            source_id,
            "play",
            {"flag": flag, "playId": resolved_play_id, "id": resolved_play_id},
            _base_url_from_request(request),
        ))
    if isinstance(payload, dict):
        url = _tvbox_direct_play_url(str(payload.get("url") or ""))
        if not url:
            urls = payload.get("urls")
            if isinstance(urls, list):
                for item in urls:
                    if isinstance(item, dict):
                        url = _tvbox_direct_play_url(str(item.get("url") or ""))
                        if url:
                            break
        if url:
            return RedirectResponse(url, status_code=302)
    raise HTTPException(status_code=422, detail="no direct playable URL")
