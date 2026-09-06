import json
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


def validate_qb_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("qBittorrent 地址必须是有效的 HTTP/HTTPS URL，账号密码请单独填写")
    try:
        parsed.port
    except ValueError:
        raise ValueError("qBittorrent 端口无效")
    if parsed.query or parsed.fragment:
        raise ValueError("qBittorrent 地址不能包含查询参数或片段")
    return url


class QBittorrentClient:
    def __init__(self, base_url: str, username: str = "", password: str = "", timeout: float = 10.0, opener=None):
        self.base_url = validate_qb_url(base_url)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.origin = "{}://{}".format(urlparse(self.base_url).scheme, urlparse(self.base_url).netloc)
        self.opener = opener or build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar())).open
        self.version = ""

    def _request(self, endpoint: str, data: Optional[Dict[str, Any]] = None, expect_json: bool = True):
        body = urlencode(data).encode("utf-8") if data is not None else None
        request = Request(
            self.base_url + "/api/v2" + endpoint,
            data=body,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.origin,
                "Referer": self.base_url + "/",
                "User-Agent": "Meizang/0.6",
            },
        )
        response = self.opener(request, timeout=self.timeout)
        try:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if expect_json else payload
        finally:
            response.close()

    def login(self) -> str:
        response = self._request(
            "/auth/login", {"username": self.username, "password": self.password}, expect_json=False,
        ).strip()
        if response != "Ok.":
            raise ValueError("qBittorrent 登录失败，请检查地址、账号和密码")
        self.version = self._request("/app/version", expect_json=False).strip()
        return self.version

    def torrents(self) -> List[Dict[str, Any]]:
        result = self._request("/torrents/info")
        if not isinstance(result, list) or any(not isinstance(item, dict) or not item.get('hash') or 'state' not in item for item in result):
            raise ValueError('qB 任务列表无效，本次不进行整理或清理')
        return result

    def files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        result = self._request("/torrents/files?{}".format(urlencode({"hash": torrent_hash})))
        if not isinstance(result, list) or any(not isinstance(item, dict) or not item.get('name') for item in result):
            raise ValueError('qB 文件列表无效')
        return result

    def pause(self, torrent_hash: str) -> None:
        self._torrent_action("stop", "pause", torrent_hash)

    def resume(self, torrent_hash: str) -> None:
        self._torrent_action("start", "resume", torrent_hash)

    def _torrent_action(self, current: str, legacy: str, torrent_hash: str) -> None:
        preferred = current if self.version.lstrip("v").split(".")[0].isdigit() and int(self.version.lstrip("v").split(".")[0]) >= 5 else legacy
        fallback = legacy if preferred == current else current
        try:
            self._request("/torrents/{}".format(preferred), {"hashes": torrent_hash}, expect_json=False)
        except HTTPError as error:
            if error.code not in (404, 405):
                raise
            self._request("/torrents/{}".format(fallback), {"hashes": torrent_hash}, expect_json=False)
