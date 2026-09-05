import http.client
import json
import os
import socket
import uuid
from typing import List


GATEWAY_SOCKET = "/var/run/trim_open_gateway_apiscope.socket"


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 3.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def shared_accessible_folders(app_name: str = "meizang") -> List[str]:
    """Return directories granted through fnOS shared-access authorization."""
    token = os.environ.get("TRIM_API_TOKEN", "").strip()
    socket_path = os.environ.get("TRIM_API_SOCKET", GATEWAY_SOCKET)
    if not token or not os.path.exists(socket_path):
        return []

    payload = json.dumps({
        "reqId": uuid.uuid4().hex,
        "req": "trim.file.getSharedAccessibleFolders",
        "appName": app_name,
        "data": {},
    }).encode("utf-8")
    connection = UnixHTTPConnection(socket_path)
    try:
        connection.request(
            "POST",
            "/api/v1/trimapp",
            body=payload,
            headers={
                "Authorization": "Bearer {}".format(token),
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        result = json.loads(response.read().decode("utf-8"))
        if response.status != 200 or result.get("code") != 0:
            raise RuntimeError(result.get("msg") or "飞牛目录授权查询失败")
        paths = result.get("data", {}).get("paths", [])
        return [str(path) for path in paths if isinstance(path, str) and path]
    finally:
        connection.close()
