from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DRIVE_PROVIDERS: dict[str, dict[str, Any]] = {
    "quark": {
        "name": "Quark",
        "hosts": ("quark.cn",),
        "paths": ("s",),
        "auth": "cookie",
        "nativeStage": "share-parse",
    },
    "uc": {
        "name": "UC",
        "hosts": ("uc.cn", "ucdisk.cn", "drive.uc.cn"),
        "paths": ("s",),
        "auth": "cookie",
        "nativeStage": "planned",
    },
    "baidu": {
        "name": "Baidu",
        "hosts": ("pan.baidu.com", "yun.baidu.com"),
        "paths": ("s",),
        "auth": "cookie",
        "nativeStage": "planned",
    },
    "ali": {
        "name": "Ali",
        "hosts": ("aliyundrive.com", "alipan.com"),
        "paths": ("s",),
        "auth": "refresh-token",
        "nativeStage": "planned",
    },
    "115": {
        "name": "115",
        "hosts": ("115.com", "anxia.com"),
        "paths": ("s",),
        "auth": "cookie",
        "nativeStage": "planned",
    },
    "123": {
        "name": "123",
        "hosts": ("123pan.com", "123684.com", "123865.com", "123912.com"),
        "paths": ("s", "share"),
        "auth": "cookie",
        "nativeStage": "planned",
    },
    "thunder": {
        "name": "Thunder",
        "hosts": ("xunlei.com", "pan.xunlei.com"),
        "paths": ("s",),
        "auth": "cookie",
        "nativeStage": "planned",
    },
    "tianyi": {
        "name": "Tianyi",
        "hosts": ("cloud.189.cn",),
        "paths": ("t", "web", "s"),
        "auth": "cookie",
        "nativeStage": "planned",
    },
}


class DriveBridgeError(RuntimeError):
    pass


QUALITY_PRIORITY = (
    "4k",
    "uhd",
    "super",
    "hd",
    "1080",
    "high",
    "720",
    "low",
    "raw",
)

VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".m3u8",
    ".mov",
    ".avi",
    ".flv",
    ".ts",
    ".wmv",
    ".webm",
    ".mpg",
    ".mpeg",
)


@dataclass
class PlayCandidate:
    name: str
    url: str
    headers: dict[str, str]

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""


def is_direct_remote_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    return "/proxy" not in parsed.path.lower()


def _candidate_name(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("name", "label", "quality", "type"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _candidate_url(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("url", "playUrl", "href", "link"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _candidate_headers(item: Any, fallback_headers: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, Any] = {}
    if isinstance(item, dict):
        for key in ("header", "headers"):
            value = item.get(key)
            if isinstance(value, dict):
                headers.update(value)
    headers.update(fallback_headers)
    return {str(key): str(value) for key, value in headers.items() if value is not None}


def extract_play_candidates(payload: Any) -> list[PlayCandidate]:
    if isinstance(payload, str):
        payload = {"url": payload}

    if not isinstance(payload, dict):
        return []

    fallback_headers = {}
    for key in ("header", "headers"):
        value = payload.get(key)
        if isinstance(value, dict):
            fallback_headers.update(value)

    raw_items: list[Any] = []
    urls = payload.get("urls")
    if isinstance(urls, list):
        raw_items.extend(urls)
    for key in ("url", "playUrl"):
        value = payload.get(key)
        if isinstance(value, list):
            raw_items.extend(value)
        elif isinstance(value, str):
            raw_items.append(value)

    candidates: list[PlayCandidate] = []
    for item in raw_items:
        url = _candidate_url(item)
        if not url:
            continue
        if url.startswith("push://"):
            url = url.removeprefix("push://")
        candidates.append(
            PlayCandidate(
                name=_candidate_name(item),
                url=url,
                headers=_candidate_headers(item, fallback_headers),
            )
        )
    return candidates


def rank_play_candidates(candidates: list[PlayCandidate]) -> list[PlayCandidate]:
    def score(candidate: PlayCandidate) -> tuple[int, str]:
        text = f"{candidate.name} {candidate.url}".lower()
        for index, token in enumerate(QUALITY_PRIORITY):
            if token in text:
                return (index, candidate.name)
        return (len(QUALITY_PRIORITY) - 1, candidate.name)

    direct = [candidate for candidate in candidates if is_direct_remote_url(candidate.url)]
    return sorted(direct, key=score)


def best_direct_candidate(payload: Any) -> PlayCandidate | None:
    ranked = rank_play_candidates(extract_play_candidates(payload))
    return ranked[0] if ranked else None


def drive_providers() -> list[dict[str, Any]]:
    return [
        {
            "id": provider_id,
            "name": config["name"],
            "hosts": list(config["hosts"]),
            "auth": config["auth"],
            "nativeStage": config["nativeStage"],
        }
        for provider_id, config in DRIVE_PROVIDERS.items()
    ]


def normalize_provider(provider: str) -> str:
    value = (provider or "").strip().lower()
    if value not in DRIVE_PROVIDERS:
        raise ValueError(f"unsupported drive provider: {provider}")
    return value


def detect_drive_provider(share_url: str) -> str:
    host = (urlparse((share_url or "").strip()).hostname or "").lower()
    for provider_id, config in DRIVE_PROVIDERS.items():
        if any(host == item or host.endswith(f".{item}") for item in config["hosts"]):
            return provider_id
    return "other"


def parse_drive_share_url(share_url: str, provider: str | None = None) -> dict[str, Any]:
    parsed = urlparse((share_url or "").strip())
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    resolved_provider = provider or detect_drive_provider(share_url)
    share_id = ""

    if len(parts) >= 2 and parts[0] == "s":
        share_id = parts[1]
    elif len(parts) >= 2 and parts[0] in {"share", "t"}:
        share_id = parts[1]
    elif parts:
        share_id = parts[-1]

    query = parse_qs(parsed.query)
    password = (
        query.get("pwd", [""])[0]
        or query.get("password", [""])[0]
        or query.get("code", [""])[0]
        or query.get("passcode", [""])[0]
    )

    provider_config = DRIVE_PROVIDERS.get(resolved_provider, {})
    known_hosts = provider_config.get("hosts", ())
    is_known_provider = bool(known_hosts) and any(host == item or host.endswith(f".{item}") for item in known_hosts)

    return {
        "provider": resolved_provider,
        "providerName": provider_config.get("name", resolved_provider.title()),
        "shareURL": share_url,
        "host": host,
        "shareId": share_id,
        "password": password,
        "isValid": bool(share_id and (is_known_provider or resolved_provider == "other")),
        "nativeStage": provider_config.get("nativeStage", "planned"),
    }


def parse_quark_share_url(share_url: str) -> dict[str, Any]:
    result = parse_drive_share_url(share_url, "quark")
    result["isQuark"] = result["provider"] == "quark" and result["isValid"]
    return result


def call_omnibox_drive_bridge(endpoint: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    base_url = os.environ.get("OMNIBOX_API_URL", "").rstrip("/")
    if not base_url:
        return None

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url}{endpoint}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise DriveBridgeError(f"bridge HTTP {error.code}: {detail[:240]}") from error
    except URLError as error:
        raise DriveBridgeError(f"bridge unavailable: {error.reason}") from error
    result = json.loads(raw or "{}")
    if result.get("success") is False:
        raise DriveBridgeError(result.get("message") or f"bridge call failed: {endpoint}")
    return result.get("data") if isinstance(result, dict) and "data" in result else result


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def _raw_file_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("files", "list", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _raw_file_items(value)
            if nested:
                return nested
    return []


def normalize_drive_file(item: dict[str, Any], parent_path: str = "") -> dict[str, Any]:
    name = str(_first_value(item, ("name", "file_name", "filename", "title")) or "")
    fid = str(_first_value(item, ("fid", "fileId", "file_id", "id", "fs_id")) or "")
    raw_type = str(_first_value(item, ("type", "file_type", "category")) or "").lower()
    is_dir = bool(_first_value(item, ("isDir", "is_dir", "dir", "folder")))
    if raw_type in {"folder", "dir", "directory"}:
        is_dir = True
    path = f"{parent_path}/{name}".strip("/") if name else parent_path
    ext = f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""
    is_video = (not is_dir) and (ext in VIDEO_EXTENSIONS or raw_type in {"video", "movie"})
    return {
        "fid": fid,
        "name": name,
        "path": path,
        "size": _first_value(item, ("size", "file_size", "bytes")) or 0,
        "type": "folder" if is_dir else raw_type or "file",
        "isDir": is_dir,
        "isVideo": is_video,
        "shareFidToken": str(_first_value(item, ("share_fid_token", "shareFidToken", "fid_token")) or ""),
        "parentFid": str(_first_value(item, ("pdir_fid", "parent_fid", "parentFid")) or ""),
        "raw": item,
    }


def normalize_drive_files(payload: Any, parent_path: str = "") -> list[dict[str, Any]]:
    return [normalize_drive_file(item, parent_path) for item in _raw_file_items(payload)]


def video_files_from_payload(payload: Any, parent_path: str = "") -> list[dict[str, Any]]:
    return [item for item in normalize_drive_files(payload, parent_path) if item["isVideo"]]


def select_best_video_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    videos = [item for item in files if item.get("isVideo")]
    if not videos:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int, str]:
        name = str(item.get("name") or "").lower()
        priority = 5
        if "4k" in name or "2160" in name:
            priority = 0
        elif "1080" in name or "fhd" in name:
            priority = 1
        elif "720" in name:
            priority = 2
        size = int(item.get("size") or 0)
        return (priority, -size, name)

    return sorted(videos, key=score)[0]


def normalize_quark_play_payload(payload: Any) -> dict[str, Any]:
    candidates = extract_play_candidates(payload)
    ranked = rank_play_candidates(candidates)
    selected = best_direct_candidate(payload)
    return {
        "candidateCount": len(candidates),
        "directCandidateCount": len(ranked),
        "selected": None if selected is None else {
            "name": selected.name,
            "url": selected.url,
            "host": selected.host,
            "headers": selected.headers,
            "headerKeys": sorted(selected.headers.keys()),
        },
        "candidates": [
            {
                "name": candidate.name,
                "url": candidate.url,
                "host": candidate.host,
                "headers": candidate.headers,
                "headerKeys": sorted(candidate.headers.keys()),
            }
            for candidate in ranked
        ],
    }
