"""
pre Node Client — 连接 Master 的 WebSocket 客户端 + 自动重连 + 心跳

asyncio 实现, 仅 stdlib.
跟 src/ws_lib.py 共享 frame encode/decode (但 client 必须 send masked, expect server unmasked)。
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
from typing import Optional, Callable, Awaitable
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ws_lib import (
    WS_GUID, encode_frame, read_frame, send_text, send_close,
    OPCODE_TEXT, OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG,
)
from common.paths import PRE_LOG_ROOT


HEARTBEAT_INTERVAL = 10.0   # 秒, 兼做 pending 检测频率
RECONNECT_INITIAL = 1.0
RECONNECT_MAX = 60.0  # Phase C: 30s → 60s cap (跟 NS-M15 ≥30min fail-closed 一致)
RECONNECT_STUCK_THRESHOLD_SEC = 1800.0  # 30min, ≥此值写 finding HIGH-daemon-ws-stuck


class NodeClient:
    """
    Node ↔ Master WS 长连接客户端.
    用法:
        client = NodeClient(node_id="local", master_url="ws://...", secret="...")
        client.on_inbound = my_handler # async (method, params) -> result | None
        await client.run() # 自动重连
    """

    def __init__(self, node_id: str, master_url: str, secret: str,
                 capabilities: list = None, host: str = "",
                 server_mode: bool = False,
                 listen_host: str = "127.0.0.1",
                 listen_port: int = 9500):
        """
        server_mode=True 时变 ws server (master 主动 connect 我).
        client_mode (默认): 主动连 master_url, 自动重连.
        server_mode: listen on (listen_host, listen_port), 等 master accept connection.
        """
        self.node_id = node_id
        self.master_url = master_url
        self.secret = secret
        self.capabilities = capabilities or []
        self.host = host or socket.gethostname()
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self.on_inbound: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]] = None
        self._stop = False
        self._connected = asyncio.Event()
        # 
        self.server_mode = server_mode
        self.listen_host = listen_host
        self.listen_port = listen_port
        # mask 方向: client 模式发 masked, server 模式发 unmasked
        self._send_masked = not server_mode

    # ---------- 内部: WS 握手 ----------
    async def _ws_handshake(self):
        u = urlparse(self.master_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 19500
        path = u.path or "/node"

        self.reader, self.writer = await asyncio.open_connection(host, port)

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {self.secret}\r\n"
            "\r\n"
        )
        self.writer.write(req.encode())
        await self.writer.drain()

        # 读响应 head
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await self.reader.read(4096)
            if not chunk:
                raise ConnectionError("WS handshake: connection closed by master")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
        if "101" not in head.split("\r\n")[0]:
            raise ConnectionError(f"WS handshake failed:\n{head}")

    # ---------- 发送 ----------
    async def _send_json(self, obj: dict):
        if not self.writer:
            raise ConnectionError("not connected")
        # client mode masked, server mode unmasked
        await send_text(self.writer, json.dumps(obj), masked=self._send_masked)

    async def call(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        """调用 master 的 method, 等待 response (匹配 id)"""
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send_json({
            "jsonrpc": "2.0", "id": rid, "method": method, "params": params
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict):
        """无 id 调用 (no response expected)"""
        await self._send_json({
            "jsonrpc": "2.0", "method": method, "params": params
        })

    # ---------- 接收 + 分发 ----------
    async def _recv_loop(self):
        """读 WS frame, 分发响应到 _pending future, 或 inbound 调 on_inbound"""
        while not self._stop:
            try:
                # client mode 收 unmasked, server mode 收 masked
                opcode, payload = await read_frame(
                    self.reader, expect_masked=not self._send_masked
                )
            except (asyncio.IncompleteReadError, ConnectionError):
                break

            if opcode == OPCODE_CLOSE:
                break
            if opcode == OPCODE_PING:
                # pong 方向跟 send 一致
                self.writer.write(encode_frame(payload, OPCODE_PONG, masked=self._send_masked))
                await self.writer.drain()
                continue
            if opcode != OPCODE_TEXT:
                continue

            try:
                m = json.loads(payload.decode("utf-8"))
            except Exception:
                continue

            # 响应: 有 id 且非 method
            if "id" in m and m.get("method") is None:
                fut = self._pending.get(m["id"])
                if fut and not fut.done():
                    if "error" in m:
                        fut.set_exception(RuntimeError(f"master error: {m['error']}"))
                    else:
                        fut.set_result(m.get("result", {}))
                continue

            # inbound notification 或 request
            method = m.get("method")
            params = m.get("params", {})
            req_id = m.get("id")

            if self.on_inbound:
                try:
                    result = await self.on_inbound(method, params)
                except Exception as e:
                    if req_id is not None:
                        await self._send_json({
                            "jsonrpc": "2.0", "id": req_id,
                            "error": {"code": -32000, "message": str(e)}
                        })
                    continue
                if req_id is not None:
                    await self._send_json({
                        "jsonrpc": "2.0", "id": req_id,
                        "result": result if result is not None else {"ok": True}
                    })

    # ---------- 心跳 ----------
    async def _heartbeat_loop(self):
        while not self._stop:
            try:
                await self.notify("node_heartbeat", {
                    "ts": time.time(), "agent_count": 0,
                })
            except Exception:
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ---------- 主循环 (一次连接) ----------
    async def _connect_once(self):
        await self._ws_handshake()

        # 必须先启动 recv loop 才能有 task 来 fire response future
        recv_task = asyncio.create_task(self._recv_loop())

        # 注册 (响应会被 recv loop fire)
        try:
            result = await self.call("register_node", {
                "node_id": self.node_id,
                "host": self.host,
                "capabilities": self.capabilities,
                "secret": self.secret,
            }, timeout=10.0)
        except Exception:
            recv_task.cancel()
            raise
        print(f"[node] registered to master: {result}", flush=True)
        self._connected.set()

        # 启动心跳
        hb_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await recv_task  # 阻塞直到对端断开
        finally:
            hb_task.cancel()
            for t in (hb_task,):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    # ---------- 自动重连 ( Phase C: 永不放弃 + ≥30min finding HIGH) ----------
    async def run(self):
        """主入口, 阻塞直到 stop().

         Phase C (Finding-C ≤30min): ws 重连永不放弃, ≥30min stuck →
        finding HIGH-daemon-ws-stuck-{node_id} alert critical ( vacuous truth 第 10 次).
        """
        import time as _time
        self._stop = False
        backoff = RECONNECT_INITIAL
        disconnect_start_ts = None  # 首次 disconnect ts, 用于 stuck 时长计算
        stuck_finding_written = False  # 防 finding HIGH 重复写
        while not self._stop:
            try:
                await self._connect_once()
                # 连接成功 reset stuck tracking
                disconnect_start_ts = None
                stuck_finding_written = False
                backoff = RECONNECT_INITIAL  # reset backoff
            except (ConnectionError, OSError) as e:
                print(f"[node] connection error: {e}", flush=True)
            except Exception as e:
                print(f"[node] unexpected error: {e}", flush=True)
            finally:
                self._connected.clear()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("reconnecting"))
                self._pending.clear()
                if self.writer:
                    try:
                        self.writer.close()
                    except Exception:
                        pass
                    self.writer = None
                self.reader = None

            if self._stop:
                break

            # 跟踪 disconnect 时长
            if disconnect_start_ts is None:
                disconnect_start_ts = _time.time()
            stuck_for = _time.time() - disconnect_start_ts
            if stuck_for >= RECONNECT_STUCK_THRESHOLD_SEC and not stuck_finding_written:
                self._write_stuck_finding(stuck_for)
                stuck_finding_written = True

            print(f"[node] reconnecting in {backoff}s (stuck for {stuck_for:.0f}s)...", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _write_stuck_finding(self, stuck_for: float):
        """ Phase C : ws 重连 ≥30min finding HIGH critical."""
        try:
            from pathlib import Path as _Path
            import os as _os
            from datetime import datetime as _dt, timezone as _tz
            findings_dir = _Path(PRE_LOG_ROOT) / "findings"
            findings_dir.mkdir(parents=True, exist_ok=True)
            try:
                _os.chmod(str(findings_dir), 0o700)
            except OSError:
                pass
            ts_str = _dt.now(tz=_tz.utc).strftime("%Y%m%dT%H%M%SZ")
            fpath = findings_dir / f"HIGH-daemon-ws-stuck-{self.node_id}-{ts_str}.md"
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(
                    f"# HIGH: daemon ws reconnect stuck ≥30min\n\n"
                    f"- ts: {ts_str}\n"
                    f"- node_id: {self.node_id}\n"
                    f"- master_url: {self.master_url}\n"
                    f"- stuck_for_seconds: {stuck_for:.0f}\n"
                    f"- ADR: + NS-M15\n\n"
                    f"## body\n\n"
                    f"daemon ws 重连永不放弃但 ≥30min 仍未连上, 触发 fail-closed alert critical.\n"
                    f"vacuous truth 第 10 次防御.\n\n"
                    f"建议:\n"
                    f"1. master 是否 down (bus_ctl.sh status master)\n"
                    f"2. ssh tunnel 是否断 (autossh -R 19500 状态)\n"
                    f"3. daemon 是否 secret 失效 (per-node secret rotation 跟 grace 期失效)\n\n"
                    f"<phase_c daemon_ws_stuck>\n"
                )
            try:
                _os.chmod(str(fpath), 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def stop(self):
        self._stop = True

    # ---------- server mode (master 主动 connect 我) ----------
    async def _serve_one(self, reader, writer):
        """accept 后处理 ws handshake (server side) + 跑 recv_loop. 单 connection."""
        from ws_lib import parse_http_request, build_handshake_response
        # 读 HTTP 请求
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
                if not chunk:
                    writer.close()
                    return
                buf += chunk
                if len(buf) > 65536:
                    writer.close()
                    return
            head, _, _ = buf.partition(b"\r\n\r\n")
            method, path, headers = parse_http_request(head)
            if headers.get("upgrade", "").lower() != "websocket":
                writer.close()
                return
            # bearer 鉴权 (master 端发 Authorization: Bearer)
            auth = headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:].strip() != self.secret:
                resp = b"HTTP/1.1 401 Unauthorized\r\n\r\n"
                writer.write(resp)
                await writer.drain()
                writer.close()
                return
            client_key = headers.get("sec-websocket-key", "")
            writer.write(build_handshake_response(client_key))
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, ValueError) as e:
            print(f"[node-server] handshake failed: {e}", flush=True)
            writer.close()
            return

        self.reader = reader
        self.writer = writer
        recv_task = asyncio.create_task(self._recv_loop())

        # server-mode 下 node 仍主动发 register_node (跟 client mode 一样的 RPC 顺序)
        try:
            result = await self.call("register_node", {
                "node_id": self.node_id,
                "host": self.host,
                "capabilities": self.capabilities,
                "secret": self.secret,  # 双层鉴权: ws bearer + RPC body
            }, timeout=10.0)
        except Exception as e:
            print(f"[node-server] register_node failed: {e}", flush=True)
            recv_task.cancel()
            try:
                writer.close()
            except Exception:
                pass
            return
        print(f"[node-server] master registered me: {result}", flush=True)
        self._connected.set()
        hb_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await recv_task
        finally:
            hb_task.cancel()
            self._connected.clear()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("master disconnected"))
            self._pending.clear()
            try:
                writer.close()
            except Exception:
                pass
            self.reader = None
            self.writer = None
            print(f"[node-server] master disconnected, waiting for new connect...", flush=True)

    async def run_server(self):
        """server mode 主入口: listen + 一次只处理一个 master connection."""
        self._stop = False
        # 跑一个 connection-at-a-time 模型 (pre 假设单 master 单 node)
        async def _handle(reader, writer):
            await self._serve_one(reader, writer)

        server = await asyncio.start_server(_handle, self.listen_host, self.listen_port)
        addr = server.sockets[0].getsockname() if server.sockets else (self.listen_host, self.listen_port)
        print(f"[node-server] listening {addr[0]}:{addr[1]} (server_mode, awaiting master connect)", flush=True)
        async with server:
            await server.serve_forever()
