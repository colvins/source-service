from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


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


def parse_quark_share_url(share_url: str) -> dict[str, Any]:
    parsed = urlparse((share_url or "").strip())
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    share_id = ""

    if len(parts) >= 2 and parts[0] == "s":
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

    return {
        "provider": "quark",
        "shareURL": share_url,
        "host": host,
        "shareId": share_id,
        "password": password,
        "isQuark": "quark.cn" in host,
        "isValid": bool(share_id and "quark.cn" in host),
    }


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
    with urlopen(request, timeout=60) as response:
        raw = response.read().decode("utf-8")
    result = json.loads(raw or "{}")
    if result.get("success") is False:
        raise RuntimeError(result.get("message") or f"bridge call failed: {endpoint}")
    return result.get("data") if isinstance(result, dict) and "data" in result else result


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
