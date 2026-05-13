"""
src/node/agent_proxy.py — node 端 HTTP loopback server, agent → node 入口.

 agent-via-node-proxy 实现 (Phase A MVP, sidecar 模式).

Phase A MVP: node HTTP server 接 agent 请求 → urllib forward 给本机 master HTTP.
            master 协议不动, 仅迁移调用方.
Phase A2 后续: 改 ws RPC wrap, 不再走本机 HTTP loopback.

设计:
- 仅 loopback (127.0.0.1) listen, 不公网
- agent 调用: GET/POST/PUT/DELETE 透明转发给 master
- node 端注入 Bearer secret (agent 不持 raw, spirit 一致)
- master 协议 unchanged

attack surface:
- 本机 loopback: 同 UID 进程都能调 (SO_PEERCRED 后续可加)
- 远端 daemon: 远端 loopback, 仅远端进程能调 (远端 ssh root + agent 同 UID)
"""
from __future__ import annotations
import http.server
import os
import threading
import urllib.error
import urllib.request

# Loopback master call: direct, bypass proxy env (Surge etc.)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_handler(master_url: str, master_secret: str):
    """构造 BaseHTTPRequestHandler 子类, master_url + secret 闭包绑定."""

    class AgentProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):     self._proxy("GET")
        def do_POST(self):    self._proxy("POST")
        def do_PUT(self):     self._proxy("PUT")
        def do_DELETE(self):  self._proxy("DELETE")
        def do_PATCH(self):   self._proxy("PATCH")

        def _proxy(self, method: str):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            body = self.rfile.read(length) if length > 0 else None

            url = master_url.rstrip("/") + self.path
            req_headers: dict[str, str] = {}
            for k, v in self.headers.items():
                if k.lower() in ("host", "content-length", "transfer-encoding", "connection"):
                    continue
                req_headers[k] = v
            req_headers["Authorization"] = f"Bearer {master_secret}"

            req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
            status = 502
            resp_headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
            resp_body = b'{"error":"proxy upstream error"}'

            try:
                with _NO_PROXY_OPENER.open(req, timeout=30.0) as resp:
                    status = resp.status
                    resp_headers = list(resp.headers.items())
                    resp_body = resp.read()
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = list(e.headers.items()) if e.headers else resp_headers
                try:
                    resp_body = e.read()
                except Exception:
                    pass
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status = 502
                resp_body = (
                    '{"error":"proxy upstream unreachable","detail":"'
                    + str(e).replace('"', "'")[:200]
                    + '"}'
                ).encode("utf-8")
                resp_headers = [("Content-Type", "application/json")]

            try:
                self.send_response(status)
                for k, v in resp_headers:
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            return

    return AgentProxyHandler


def start_proxy_server(host: str, port: int, master_url: str, master_secret: str) -> None:
    handler_cls = make_handler(master_url, master_secret)
    httpd = http.server.ThreadingHTTPServer((host, port), handler_cls)
    httpd.daemon_threads = True
    print(f"[agent-proxy] listening {host}:{port} → {master_url} (sidecar mode, Phase A)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def spawn_proxy_thread(host: str, port: int, master_url: str, master_secret: str) -> threading.Thread:
    t = threading.Thread(
        target=start_proxy_server,
        args=(host, port, master_url, master_secret),
        name="pre-agent-proxy",
        daemon=True,
    )
    t.start()
    return t


def derive_master_http_url(ws_master_url: str, env_override: str | None = None) -> str:
    if env_override:
        return env_override.rstrip("/")
    if ws_master_url.startswith("ws://"):
        u = "http://" + ws_master_url[len("ws://"):]
    elif ws_master_url.startswith("wss://"):
        u = "https://" + ws_master_url[len("wss://"):]
    else:
        u = ws_master_url
    if u.endswith("/node"):
        u = u[: -len("/node")]
    return u.rstrip("/")
