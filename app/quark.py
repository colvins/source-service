from __future__ import annotations

import json
import random
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class QuarkNativeError(RuntimeError):
    pass


class QuarkAuthRequired(QuarkNativeError):
    pass


PC_BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
MOBILE_BASE_URL = "https://drive-m.quark.cn/1/clouddrive"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 "
    "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
)


def quark_cookie_mobile_params(cookie: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for key in ("kps", "sign", "vcode"):
        match = re.search(rf"(?<!\w){key}=([a-zA-Z0-9%+/=]+)[;&]?", cookie or "")
        if match:
            params[key] = match.group(1).replace("%25", "%")
    return params


def quark_has_mobile_auth(cookie: str) -> bool:
    params = quark_cookie_mobile_params(cookie)
    return all(params.get(key) for key in ("kps", "sign", "vcode"))


class QuarkClient:
    def __init__(self, cookie: str, user_agent: str = ""):
        self.cookie = cookie.strip()
        self.user_agent = (user_agent or DEFAULT_UA).strip()
        self.mobile_params = quark_cookie_mobile_params(cookie)

    @property
    def has_mobile_auth(self) -> bool:
        return all(self.mobile_params.get(key) for key in ("kps", "sign", "vcode"))

    def _headers(self, include_cookie: bool = True) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }
        if include_cookie and self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _params(self, mobile: bool = False, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "__t": int(time.time() * 1000),
            "__dt": 1000,
        }
        if mobile:
            params.update(
                {
                    "device_model": "M2011K2C",
                    "entry": "default_clouddrive",
                    "_t_group": "0%3A_s_vp%3A1",
                    "dmn": "Mi%2B11",
                    "fr": "android",
                    "pf": "3300",
                    "bi": "35937",
                    "ve": "7.4.5.680",
                    "ss": "411x875",
                    "mi": "M2011K2C",
                    "nt": "5",
                    "nw": "0",
                    "kt": "4",
                    "sv": "release",
                    "dt": "phone",
                    "data_from": "ucapi",
                    "kps": self.mobile_params.get("kps", ""),
                    "sign": self.mobile_params.get("sign", ""),
                    "vcode": self.mobile_params.get("vcode", ""),
                    "app": "clouddrive",
                    "kkkk": "1",
                    "__t": time.time(),
                    "__dt": int(random.uniform(1, 5) * 60 * 1000),
                }
            )
        params.update(extra)
        return params

    def _request(
        self,
        method: str,
        path: str,
        *,
        base_url: str = PC_BASE_URL,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        mobile: bool = False,
    ) -> dict[str, Any]:
        if mobile:
            if not self.has_mobile_auth:
                raise QuarkAuthRequired("Quark mobile auth params are missing: kps/sign/vcode.")
            base_url = MOBILE_BASE_URL
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}?{urlencode(self._params(mobile, **(params or {})))}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(url, data=data, headers=self._headers(include_cookie=not mobile), method=method)
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise QuarkNativeError(f"Quark HTTP {error.code}: {detail[:240]}") from error
        except URLError as error:
            raise QuarkNativeError(f"Quark network error: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise QuarkNativeError("Quark response is not JSON.") from error

        code = payload.get("code")
        status = payload.get("status")
        if code not in (None, 0) or (isinstance(status, int) and status >= 400):
            raise QuarkNativeError(payload.get("message") or f"Quark API failed: {payload}")
        return payload

    def share_token(self, pwd_id: str, passcode: str = "") -> str:
        payload = self._request(
            "POST",
            "share/sharepage/token",
            body={
                "pwd_id": pwd_id,
                "passcode": passcode or "",
                "support_visit_limit_private_share": True,
            },
        )
        token = (payload.get("data") or {}).get("stoken")
        if not token:
            raise QuarkNativeError("Quark share token is empty.")
        return str(token)

    def share_detail(self, pwd_id: str, stoken: str, pdir_fid: str = "0", page: int = 1, size: int = 50) -> dict[str, Any]:
        return self._request(
            "GET",
            "share/sharepage/detail",
            params={
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": pdir_fid or "0",
                "force": "0",
                "_page": page,
                "_size": size,
                "_fetch_banner": "0",
                "_fetch_share": "1",
                "_fetch_total": "1",
                "_sort": "file_type:asc,updated_at:desc",
                "ver": "2",
                "fetch_share_full_path": "1",
            },
        )

    def list_share_files(self, pwd_id: str, stoken: str, pdir_fid: str = "0") -> dict[str, Any]:
        merged: list[dict[str, Any]] = []
        page = 1
        last: dict[str, Any] = {}
        while True:
            last = self.share_detail(pwd_id, stoken, pdir_fid, page=page)
            data = last.get("data") or {}
            items = data.get("list") or []
            if not items:
                break
            merged.extend(item for item in items if isinstance(item, dict))
            total = (last.get("metadata") or {}).get("_total") or len(merged)
            if len(merged) >= int(total):
                break
            page += 1
        last.setdefault("data", {})["list"] = merged
        return last

    def save_share_file(self, pwd_id: str, stoken: str, fid: str, fid_token: str, to_pdir_fid: str = "0") -> dict[str, Any]:
        return self._request(
            "POST",
            "share/sharepage/save",
            params={"app": "clouddrive"},
            body={
                "fid_list": [fid],
                "fid_token_list": [fid_token] if fid_token else [],
                "to_pdir_fid": to_pdir_fid or "0",
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "pdir_save_all": False,
                "exclude_fids": [],
                "scene": "link",
            },
            mobile=True,
        )

    def query_task(self, task_id: str, retry_index: int = 0) -> dict[str, Any]:
        return self._request(
            "GET",
            "task",
            params={
                "task_id": task_id,
                "retry_index": retry_index,
                "__dt": int(random.uniform(1, 5) * 60 * 1000),
                "__t": time.time(),
            },
        )

    def download_urls(self, fids: list[str]) -> tuple[dict[str, Any], dict[str, str]]:
        payload = self._request("POST", "file/download", body={"fids": fids})
        return payload, {"User-Agent": self.user_agent, "Referer": "https://pan.quark.cn/", "Cookie": self.cookie}
