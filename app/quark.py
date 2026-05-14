from __future__ import annotations

import json
import gzip
import random
import re
import time
import uuid
import zlib
from io import BytesIO
from typing import Any
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


class QuarkNativeError(RuntimeError):
    pass


class QuarkAuthRequired(QuarkNativeError):
    pass


PC_BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
PC_PLAY_BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
MOBILE_BASE_URL = "https://drive-m.quark.cn/1/clouddrive"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 "
    "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
)
PC_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://pan.quark.cn",
    "Referer": "https://pan.quark.cn/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

PC_PLAY_HEADERS = {
    "Referer": "https://pan.quark.cn/",
}



def normalize_quark_download_url(url: str) -> str:
    """Make Quark OSS signed URLs safe for Java/Android without changing signed values."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    normalized: list[str] = []
    for segment in parts.query.split("&"):
        key, separator, value = segment.partition("=")
        if not separator:
            normalized.append(segment)
            continue
        if key == "response-content-disposition":
            if value.lower().startswith("attachment%3b"):
                value = value.replace("+", "%2B")
            elif "%" in value:
                value = quote(value, safe="")
            else:
                value = value.replace(" ", "%20").replace("+", "%2B")
        else:
            value = value.replace("+", "%2B")
        normalized.append(f"{key}={value}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(normalized), parts.fragment))

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


def quark_qr_start() -> dict[str, str]:
    request_id = str(uuid.uuid4())
    url = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin?" + urlencode(
        {"client_id": "532", "v": "1.2", "request_id": request_id}
    )
    request = Request(url, headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (HTTPError, URLError, json.JSONDecodeError) as error:
        raise QuarkNativeError(f"Quark QR start failed: {error}") from error
    token = (((payload.get("data") or {}).get("members") or {}).get("token") or "").strip()
    if payload.get("status") != 2000000 or not token:
        raise QuarkNativeError(payload.get("message") or "Quark QR token is empty.")
    qr_url = "https://su.quark.cn/4_eMHBJ?" + urlencode(
        {
            "token": token,
            "client_id": "532",
            "ssb": "weblogin",
            "uc_param_str": "",
            "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
        }
    )
    return {"token": token, "qrURL": qr_url, "qrSVG": quark_qr_svg(qr_url)}


def quark_qr_svg(qr_url: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError as error:
        raise QuarkNativeError("QR code dependency is not installed.") from error

    image = qrcode.make(qr_url, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def quark_qr_check(token: str) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    url = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken?" + urlencode(
        {"client_id": "532", "v": "1.2", "token": token, "request_id": request_id}
    )
    request = Request(url, headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (HTTPError, URLError, json.JSONDecodeError) as error:
        raise QuarkNativeError(f"Quark QR check failed: {error}") from error
    ticket = (((payload.get("data") or {}).get("members") or {}).get("service_ticket") or "").strip()
    if payload.get("status") == 2000000 and ticket:
        return {"ready": True, "serviceTicket": ticket, "rawStatus": payload.get("status")}
    return {"ready": False, "rawStatus": payload.get("status"), "message": payload.get("message", "waiting")}


def quark_cookie_from_service_ticket(service_ticket: str) -> str:
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    url = "https://pan.quark.cn/account/info?" + urlencode({"st": service_ticket, "lw": "scan"})
    request = Request(url, headers={"User-Agent": DEFAULT_UA, "Accept": "application/json"})
    try:
        opener.open(request, timeout=20).read()
    except (HTTPError, URLError) as error:
        raise QuarkNativeError(f"Quark account info failed: {error}") from error
    cookies = [
        f"{cookie.name}={cookie.value}"
        for cookie in jar
        if cookie.domain and "quark.cn" in cookie.domain
    ]
    if not cookies:
        raise QuarkNativeError("Quark login did not return cookies.")
    return "; ".join(cookies)


class QuarkClient:
    def __init__(self, cookie: str, user_agent: str = ""):
        self.cookie = cookie.strip()
        self.user_agent = (user_agent or DEFAULT_UA).strip()
        self.mobile_params = quark_cookie_mobile_params(cookie)

    @property
    def has_mobile_auth(self) -> bool:
        return all(self.mobile_params.get(key) for key in ("kps", "sign", "vcode"))

    def _headers(self, include_cookie: bool = True, *, playback: bool = False) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        if not playback:
            headers["Content-Type"] = "application/json"
        headers.update(PC_PLAY_HEADERS if playback else PC_API_HEADERS)
        if include_cookie and self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _refresh_cookie_from_response(self, response) -> None:
        values: list[str] = []
        try:
            values = list(response.headers.get_all("Set-Cookie") or [])
        except Exception:
            value = response.headers.get("Set-Cookie")
            values = [value] if value else []
        joined = ";".join(value for value in values if value)
        match = re.search(r"(?<!\w)__puus=([^;]+)", joined)
        if not match:
            return
        value = match.group(1).strip()
        if not value:
            return
        if re.search(r"(?<!\w)__puus=", self.cookie):
            self.cookie = re.sub(r"(?<!\w)__puus=[^;]*", f"__puus={value}", self.cookie)
        elif self.cookie:
            self.cookie = self.cookie.rstrip("; ") + f"; __puus={value}"
        else:
            self.cookie = f"__puus={value}"

    def _decode_response_body(self, response) -> str:
        raw = response.read()
        encoding = str(response.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw)
        return raw.decode("utf-8")

    def _decode_error_body(self, error: HTTPError) -> str:
        raw = error.read()
        encoding = str(error.headers.get("Content-Encoding") or "").lower()
        try:
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            elif encoding == "deflate":
                raw = zlib.decompress(raw)
        except Exception:
            pass
        return raw.decode("utf-8", errors="replace")

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
                self._refresh_cookie_from_response(response)
                payload = json.loads(self._decode_response_body(response) or "{}")
        except HTTPError as error:
            detail = self._decode_error_body(error)
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

    def list_files(self, pdir_fid: str = "0") -> dict[str, Any]:
        merged: list[dict[str, Any]] = []
        page = 1
        last: dict[str, Any] = {}
        while True:
            last = self._request(
                "GET",
                "file/sort",
                params={
                    "pdir_fid": pdir_fid or "0",
                    "_page": page,
                    "_size": 100,
                    "_sort": "file_type:asc,updated_at:desc",
                },
            )
            data = last.get("data") or {}
            items = data.get("list") or []
            if not items:
                break
            merged.extend(item for item in items if isinstance(item, dict))
            total = (last.get("metadata") or {}).get("_total") or data.get("total") or len(merged)
            if len(merged) >= int(total):
                break
            page += 1
        last.setdefault("data", {})["list"] = merged
        return last


    def create_folder(self, name: str, parent_fid: str = "0") -> dict[str, Any]:
        return self._request(
            "POST",
            "file",
            body={
                "pdir_fid": parent_fid or "0",
                "file_name": name,
                "dir_path": "",
                "dir_init_lock": "false",
            },
        )

    def delete_files(self, fids: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            "file/delete",
            params={"uc_param_str": ""},
            body={
                "action_type": 2,
                "filelist": [str(fid) for fid in fids if str(fid or "").strip()],
                "exclude_fids": [],
            },
        )

    def save_share_file(
        self,
        pwd_id: str,
        stoken: str,
        fid: str,
        fid_token: str,
        to_pdir_fid: str = "0",
        source_pdir_fid: str = "0",
    ) -> dict[str, Any]:
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
                "pdir_fid": source_pdir_fid or "0",
                "pdir_save_all": False,
                "exclude_fids": [],
                "scene": "link",
            },
            mobile=self.has_mobile_auth,
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

    def wait_task(self, task_id: str, timeout: int = 45) -> dict[str, Any]:
        start = time.time()
        retry_index = 0
        last: dict[str, Any] = {}
        while time.time() - start < timeout:
            last = self.query_task(task_id, retry_index)
            data = last.get("data") or {}
            status = data.get("status")
            if status == 2:
                return last
            if status == 3:
                raise QuarkNativeError(data.get("message") or "Quark task failed.")
            retry_index += 1
            time.sleep(0.7)
        raise QuarkNativeError(f"Quark task timeout: {task_id}")

    def video_play_urls(self, fid: str) -> tuple[dict[str, Any], dict[str, str]]:
        payload = self._request(
            "POST",
            "file/v2/play",
            base_url=PC_PLAY_BASE_URL,
            body={
                "fid": fid,
                "resolutions": "normal,low,high,super,2k,4k",
                "supports": "fmp4",
            },
        )
        headers = self._headers(playback=True)
        return payload, headers

    def download_url(self, fid: str) -> str:
        payload = self._request(
            "POST",
            "file/download",
            body={"fids": [fid]},
        )
        items = payload.get("data") or []
        if not isinstance(items, list) or not items:
            raise QuarkNativeError("Quark raw download URL is empty.")
        url = str((items[0] or {}).get("download_url") or "").strip()
        if not url:
            raise QuarkNativeError("Quark raw download URL is empty.")
        return normalize_quark_download_url(url)
