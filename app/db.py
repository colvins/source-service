import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path("/app/data/source_service.sqlite3")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                runtime_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                download_url TEXT NOT NULL DEFAULT '',
                dependencies TEXT NOT NULL DEFAULT '[]',
                script_content TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscription_sources (
                subscription_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (subscription_id, source_id),
                FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS source_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                runtime_type TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                template_body TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS drive_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                cookie TEXT NOT NULL DEFAULT '',
                token TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS drive_file_cache (
                provider TEXT NOT NULL,
                share_id TEXT NOT NULL,
                share_fid TEXT NOT NULL,
                file_path TEXT NOT NULL DEFAULT '',
                saved_fid TEXT NOT NULL,
                save_mode TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (provider, share_id, share_fid, file_path)
            );
            """
        )
        seed_defaults(conn)


def seed_defaults(conn: sqlite3.Connection) -> None:
    default_settings = {
        "service_name": "Colvins Source Service",
        "service_base_url": "http://127.0.0.1:8788",
        "catpaw_prefix": "/api/export",
        "tmdb_api_key": "",
        "danmu_api_url": "",
        "iptv_filter_rules": "",
        "drive_default_provider": "quark",
        "drive_quality_priority": "raw,original,原画,原码,4k,super,high,1080,720,low",
        "drive_bridge_mode": "disabled",
    }
    for key, value in default_settings.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    existing = conn.execute("SELECT COUNT(*) AS c FROM source_templates").fetchone()["c"]
    if existing:
        return

    templates = [
        (
            "采集站模板",
            "javascript",
            "CMS/资源站 API 模板",
            "// site spider template\nmodule.exports = { home, category, detail, search, play };\n",
        ),
        (
            "盘搜模板",
            "javascript",
            "盘搜模板",
            "// pansou template\nmodule.exports = { search, detail, play };\n",
        ),
        (
            "推送模板",
            "javascript",
            "推送模板",
            "// push template\nmodule.exports = { detail, play };\n",
        ),
        (
            "采集站模板(Python)",
            "python",
            "CMS/资源站 API 模板",
            'async def home(params, context):\n    return {"class": [], "list": []}\n',
        ),
    ]
    conn.executemany(
        "INSERT INTO source_templates (name, runtime_type, description, template_body) VALUES (?, ?, ?, ?)",
        templates,
    )


def row_to_source(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "runtimeType": row["runtime_type"],
        "enabled": bool(row["enabled"]),
        "sortOrder": row["sort_order"],
        "tags": json.loads(row["tags"] or "[]"),
        "notes": row["notes"],
        "description": row["description"],
        "version": row["version"],
        "downloadURL": row["download_url"],
        "dependencies": json.loads(row["dependencies"] or "[]"),
        "scriptContent": row["script_content"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_to_subscription(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "token": row["token"],
        "enabled": bool(row["enabled"]),
        "sortOrder": row["sort_order"],
        "notes": row["notes"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_to_drive_account(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "enabled": bool(row["enabled"]),
        "sortOrder": row["sort_order"],
        "hasCookie": bool(row["cookie"]),
        "hasToken": bool(row["token"]),
        "userAgent": row["user_agent"],
        "notes": row["notes"],
        "status": row["status"],
        "lastCheckedAt": row["last_checked_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
