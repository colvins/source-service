from __future__ import annotations

import json
import re
import time
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import HTTPCookieProcessor, Request, build_opener


PAN_APP_ID = "250528"
PAN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BaiduNativeError(RuntimeError):
    pass


def parse_baidu_share_url(share_url: str) -> dict[str, Any]:
    parsed = urlparse((share_url or "").strip())
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    feature = ""
    if len(parts) >= 2 and parts[0] == "s":
        feature = parts[1]
    elif parts and parts[-1] == "init" and query.get("surl"):
        feature = "1" + query.get("surl", [""])[0]
    elif query.get("surl"):
        feature = "1" + query.get("surl", [""])[0]
    password = query.get("pwd", [""])[0] or query.get("password", [""])[0]
    return {
        "provider": "baidu",
        "providerName": "Baidu",
        "shareURL": share_url,
        "host": parsed.hostname or "",
        "shareId": feature,
        "password": password,
        "isValid": bool(feature and feature.startswith("1")),
        "nativeStage": "share-parse",
    }


class BaiduClient:
    def __init__(self, cookie: str, user_agent: str = ""):
        self.cookie = (cookie or "").strip()
        self.user_agent = (user_agent or PAN_UA).strip()
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def _headers(self, referer: str = "https://pan.baidu.com/disk/home") -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Referer": referer,
            "Accept": "application/json,text/plain,*/*",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _request_text(
        self,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        referer: str = "https://pan.baidu.com/disk/home",
    ) -> str:
        encoded = urlencode(data or {}).encode("utf-8") if data is not None else None
        headers = self._headers(referer)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        request = Request(url, data=encoded, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=45) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise BaiduNativeError(f"Baidu HTTP {error.code}: {detail[:240]}") from error
        except URLError as error:
            raise BaiduNativeError(f"Baidu network error: {error.reason}") from error

    def access_share_page(self, feature: str, first: bool = True) -> dict[str, str]:
        referer = "https://pan.baidu.com/disk/home" if first else f"https://pan.baidu.com/share/init?surl={feature[1:]}"
        body = self._request_text(f"https://pan.baidu.com/s/{feature}", referer=referer)
        if "platform-non-found" in body or "error-404" in body:
            raise BaiduNativeError("Baidu share is unavailable or expired.")
        match = re.search(r"(\{.+?loginstate.+?\})\);", body, re.S)
        if not match:
            raise BaiduNativeError("Baidu share page did not expose login tokens. Check BDUSS/STOKEN cookie.")
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise BaiduNativeError("Baidu share token payload is not JSON.") from error
        return {
            "bdstoken": str(payload.get("bdstoken") or ""),
            "uk": str(payload.get("uk") or ""),
            "share_uk": str(payload.get("share_uk") or ""),
            "shareid": str(payload.get("shareid") or ""),
        }

    def verify_share(self, feature: str, tokens: dict[str, str], password: str) -> None:
        if not password:
            return
        url = "https://pan.baidu.com/share/verify?" + urlencode(
            {
                "shareid": tokens["shareid"],
                "uk": tokens["share_uk"],
                "t": int(time.time() * 1000),
                "channel": "chunlei",
                "web": "1",
                "app_id": PAN_APP_ID,
                "bdstoken": tokens["bdstoken"],
                "clienttype": "1",
            }
        )
        raw = self._request_text(
            url,
            method="POST",
            data={"pwd": password, "vcode": "null", "vcode_str": "null", "bdstoken": tokens["bdstoken"]},
            referer=f"https://pan.baidu.com/s/{feature}",
        )
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise BaiduNativeError("Baidu verify response is not JSON.") from error
        errno = payload.get("errno")
        if errno not in (0, "0", None):
            raise BaiduNativeError(payload.get("errmsg") or f"Baidu share verify failed: {errno}")

    def verify_share_shorturl(self, feature: str, password: str) -> None:
        if not password:
            return
        url = "https://pan.baidu.com/share/verify?" + urlencode(
            {
                "surl": feature[1:],
                "t": int(time.time() * 1000),
                "channel": "chunlei",
                "web": "1",
                "app_id": PAN_APP_ID,
                "clienttype": "0",
            }
        )
        raw = self._request_text(
            url,
            method="POST",
            data={"pwd": password, "vcode": "", "vcode_str": ""},
            referer=f"https://pan.baidu.com/share/init?surl={feature[1:]}",
        )
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise BaiduNativeError("Baidu verify response is not JSON.") from error
        errno = payload.get("errno")
        if errno not in (0, "0", None):
            raise BaiduNativeError(payload.get("err_msg") or payload.get("errmsg") or f"Baidu share verify failed: {errno}")

    def list_share_files_shorturl(self, parsed: dict[str, Any], directory: str = "") -> dict[str, Any]:
        feature = parsed["shareId"]
        self.verify_share_shorturl(feature, parsed.get("password", ""))
        params = {
            "shorturl": feature[1:],
            "root": "1" if not directory else "0",
            "web": "1",
            "app_id": PAN_APP_ID,
            "channel": "chunlei",
            "clienttype": "0",
        }
        if directory:
            params["dir"] = directory
        raw = self._request_text(
            "https://pan.baidu.com/share/list?" + urlencode(params),
            referer=f"https://pan.baidu.com/s/{feature}",
        )
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as error:
            raise BaiduNativeError("Baidu share list response is not JSON.") from error
        errno = payload.get("errno")
        if errno not in (0, "0", None):
            message = payload.get("show_msg") or payload.get("errmsg") or f"Baidu share list failed: {errno}"
            raise BaiduNativeError(message)
        return payload

    def list_share_files(self, share_url: str, directory: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
        parsed = parse_baidu_share_url(share_url)
        if not parsed["isValid"]:
            raise BaiduNativeError("Invalid Baidu share URL.")
        feature = parsed["shareId"]
        try:
            tokens = self.access_share_page(feature, True)
            self.verify_share(feature, tokens, parsed.get("password", ""))
            if parsed.get("password"):
                tokens = self.access_share_page(feature, False)
            params = {
                "bdstoken": tokens["bdstoken"],
                "root": "1" if not directory else "0",
                "web": "5",
                "app_id": PAN_APP_ID,
                "shorturl": feature[1:],
                "channel": "chunlei",
            }
            if directory:
                params["dir"] = directory
            raw = self._request_text("https://pan.baidu.com/share/list?" + urlencode(params), referer=f"https://pan.baidu.com/s/{feature}")
            payload = json.loads(raw or "{}")
            errno = payload.get("errno")
            if errno not in (0, "0", None):
                raise BaiduNativeError(payload.get("errmsg") or f"Baidu share list failed: {errno}")
            return parsed, payload
        except (BaiduNativeError, json.JSONDecodeError):
            return parsed, self.list_share_files_shorturl(parsed, directory)

    def play_headers(self) -> dict[str, str]:
        # Large Baidu files often require this UA plus auth cookies when following dlink redirects.
        return {
            "User-Agent": "pan.baidu.com",
            "Referer": "https://pan.baidu.com/",
            "Cookie": self.cookie,
        }
