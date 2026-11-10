"""AnythingLLM 本机上传代理：密钥仅留在服务端，并提供上传页面。

仅监听回环地址；浏览器页面与 API 同源，避免 file:// 与跨域请求。
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
PORT = 8010
UPSTREAM_TIMEOUT = 120
_ALLOWED_ORIGIN_HOSTS = {"localhost", "127.0.0.1"}


class ProxyHandler(BaseHTTPRequestHandler):
    """转发三个受控 API，其他 AnythingLLM 端点不对外暴露。"""

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        try:
            return urlsplit(origin).hostname in _ALLOWED_ORIGIN_HOSTS
        except ValueError:
            return False

    def _check_origin(self) -> bool:
        if self._origin_allowed():
            return True
        self._send_json(403, {"error": "仅允许本机页面访问代理"})
        return False

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Content-Length 无效"})
            return None
        if length < 0:
            self._send_json(400, {"error": "Content-Length 不能为负数"})
            return None
        return self.rfile.read(length)

    def _forward(self, method: str, path: str, body: bytes | None = None) -> None:
        api_key = os.environ.get("ANYTHINGLLM_API_KEY", "").strip()
        if not api_key:
            self._send_json(503, {"error": "未配置 ANYTHINGLLM_API_KEY"})
            return
        base_url = os.environ.get("ANYTHINGLLM_BASE_URL", "http://localhost:3001").rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        content_type = self.headers.get("Content-Type")
        if body is not None and content_type:
            headers["Content-Type"] = content_type
        request = Request(base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=UPSTREAM_TIMEOUT) as response:
                self._send_bytes(
                    response.status,
                    response.read(),
                    response.headers.get("Content-Type", "application/json; charset=utf-8"),
                )
        except HTTPError as exc:
            payload = exc.read()
            content_type = exc.headers.get("Content-Type", "application/json; charset=utf-8")
            self._send_bytes(exc.code, payload, content_type)
        except TimeoutError:
            self._send_json(504, {"error": "AnythingLLM 请求超时"})
        except (URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            self._send_json(502, {"error": f"无法连接 AnythingLLM：{reason}"})

    def do_GET(self) -> None:
        """提供同源上传页与工作区列表。"""
        if not self._check_origin():
            return
        path = urlsplit(self.path).path
        if path in {"/", "/upload.html"}:
            try:
                body = Path(__file__).with_name("upload.html").read_bytes()
            except OSError as exc:
                self._send_json(500, {"error": f"上传页面读取失败：{exc}"})
                return
            self._send_bytes(200, body, "text/html; charset=utf-8")
        elif path == "/api/workspaces":
            self._forward("GET", "/api/v1/workspaces")
        else:
            self._send_json(404, {"error": "端点不存在"})

    def do_POST(self) -> None:
        """原样转发 multipart 上传与 JSON 向量化请求。"""
        if not self._check_origin():
            return
        parsed = urlsplit(self.path)
        if parsed.path not in {"/api/upload", "/api/embed"}:
            self._send_json(404, {"error": "端点不存在"})
            return
        body = self._read_body()
        if body is None:
            return
        if parsed.path == "/api/upload":
            self._forward("POST", "/api/v1/document/upload", body)
            return
        slug = parse_qs(parsed.query).get("slug", [""])[0].strip()
        if not slug:
            self._send_json(400, {"error": "缺少 slug 查询参数"})
            return
        path = f"/api/v1/workspace/{quote(slug, safe='')}/update-embeddings"
        self._forward("POST", path, body)

    def log_message(self, format: str, *args: object) -> None:
        """保留标准访问日志，但不记录请求体与密钥。"""
        super().log_message(format, *args)


def main() -> None:
    """启动仅供本机使用的上传代理。"""
    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    print(f"AnythingLLM 上传代理：http://{HOST}:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
