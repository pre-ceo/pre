"""
pre master/remote_node_manager.py — master 主动 connect 远端 node.

架构:
- master 启动时读 pre_rule/remote_nodes.json
- 对每个 enabled remote_node 跑一个 manager task:
  1. ssh exec 启远端 node daemon (一次性, 远端 nohup -d)
  2. ssh -L 维护正向隧道 (本地 19510+ → 远端 9500)
  3. master ws.connect ws://127.0.0.1:tunnel_port/node (master is client)
  4. handshake 后等 node 主动发 register_node, master 处理 (复用 dispatch_node_message)
  5. 监控 disconnect, 重 connect (隧道断/node crash 等情况)

安全模型:
- master 不公网 (仅 127.0.0.1)
- 远端 node 仅 127.0.0.1 ws listen, 0 outgoing
- ssh 鉴权 + Bearer 双层

跟 handle_node_ws 的差异:
- master 这边发 mask=True (client side), 收 unmasked
- node 主动发 register_node, master 处理 (跟 server 端一样的 RPC handler)
"""
from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ws_lib import (
    ws_client_handshake, encode_frame, read_frame, send_text, send_close,
    OPCODE_TEXT, OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG,
)
from master.registry import NodeInfo

# token: lazy resolve from ~/.pre/env via token_resolver (PR3)
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context


# ---------- 路径 ----------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RULE_ROOT = (PROJECT_ROOT.parent / "pre_rule").resolve()
REMOTE_NODES_CONFIG = RULE_ROOT / "remote_nodes.json"


# ---------- 配置加载 ----------

def load_remote_nodes() -> list[dict]:
    """读 pre_rule/remote_nodes.json. 不存在或解析失败返 []."""
    if not REMOTE_NODES_CONFIG.exists():
        return []
    try:
        with open(REMOTE_NODES_CONFIG, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[remote-mgr] load {REMOTE_NODES_CONFIG} failed: {e}", flush=True)
        return []
    nodes = doc.get("remote_nodes") or []
    return [n for n in nodes if n.get("enabled", True)]


# ---------- ssh helpers ----------

async def _ssh_exec_start_daemon(rn: dict) -> bool:
    """ssh 一次性启远端 node daemon. nohup + start_new_session 让 ssh 退出不杀 daemon.
    远端命令: cd <path> && nohup uv run python scripts/start_node.py --transport ws-server --listen-host 127.0.0.1 --listen-port <port> > /tmp/pre_node_<id>.log 2>&1 &"""
    ssh_alias = rn["ssh_alias"]
    pre_path = rn.get("remote_pre_path", "/root/workspace/pre")
    listen_port = rn.get("remote_listen_port", 9500)
    node_id = rn["node_id"]
    caps = ",".join(rn.get("capabilities", ["cli-claude-code-local"]))
    secret = _resolve_token("node")  # 远端 node ws connect 回 master 用 node role

    log_file = f"/tmp/pre_node_{node_id}.log"
    pid_file = f"/tmp/pre_node_{node_id}.pid"

    # 远端命令: 检查是否已跑, 没跑则启动. 优先 uv, fallback 到 python3 (远端可能无 uv)
    # PATH 加 ~/.local/bin (ssh 非交互式不读 .bashrc, uv 默认装到这)
    # source pre_rule/.env_sync_secret 让 daemon 拿到 PRE_SYNC_HMAC_SECRET
    py_cmd = "$(command -v uv >/dev/null && echo 'uv run python' || echo 'python3')"
    rule_root = os.path.dirname(pre_path) + "/pre_rule"  # /root/workspace/pre_rule
    sync_secret_file = f"{rule_root}/.env_sync_secret"
    remote_cmd = (
        f"export PATH=\"$HOME/.local/bin:$PATH\" && "
        f"cd {pre_path} && "
        # source HMAC secret 文件 (如存在), 让 daemon 进程拿到 PRE_SYNC_HMAC_SECRET
        f"[ -f {sync_secret_file} ] && set -a && . {sync_secret_file} && set +a; "
        f"if [ -f {pid_file} ] && kill -0 $(cat {pid_file}) 2>/dev/null; then "
        f"  echo 'already running pid='$(cat {pid_file}); "
        f"else "
        f"  nohup {py_cmd} scripts/start_node.py "
        f"    --node-id {node_id} "
        f"    --transport ws-server "
        f"    --listen-host 127.0.0.1 "
        f"    --listen-port {listen_port} "
        f"    --secret {secret} "
        f"    --capabilities {caps} "
        f"    > {log_file} 2>&1 & "
        f"  echo $! > {pid_file}; "
        f"  echo 'started pid='$(cat {pid_file}); "
        f"fi"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
            ssh_alias, remote_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        out_s = out.decode("utf-8", errors="replace").strip()
        err_s = err.decode("utf-8", errors="replace").strip()
        print(f"[remote-mgr] {node_id} ssh exec daemon: {out_s} {('| stderr=' + err_s) if err_s else ''}",
              flush=True)
        return proc.returncode == 0
    except (asyncio.TimeoutError, OSError) as e:
        print(f"[remote-mgr] {node_id} ssh exec daemon failed: {e}", flush=True)
        return False


async def _spawn_ssh_tunnel(rn: dict) -> Optional[asyncio.subprocess.Process]:
    """spawn ssh -L (forward) + -R (reverse) tunnel subprocess. 返 Popen.
    加 -R 反向, 让远端 daemon 能 POST 给 master HTTP 19500
    (e.g. usage_probe_once 远端跑时上传数据). 远端 127.0.0.1:19500 → 本机 master 19500."""
    ssh_alias = rn["ssh_alias"]
    listen_port = rn.get("remote_listen_port", 9500)
    local_port = rn.get("local_tunnel_port", 19510)
    master_port = rn.get("master_port_for_remote", 19500)
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ssh",
            "-N",  # no remote command
            "-T",  # no tty
            "-o", "ServerAliveInterval=20",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{listen_port}",
            "-R", f"127.0.0.1:{master_port}:127.0.0.1:{master_port}",
            ssh_alias,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        return proc
    except OSError as e:
        print(f"[remote-mgr] {rn['node_id']} ssh tunnel spawn failed: {e}", flush=True)
        return None


# ---------- ws connection 处理 ----------

async def _handle_remote_node_session(rn: dict, registry, db, dispatch_fn):
    """完整的 connect → handshake → register → message loop. 返回时 connection 已断."""
    node_id = rn["node_id"]
    local_port = rn.get("local_tunnel_port", 19510)
    secret = _resolve_token("node")  # ws Upgrade /node 校 role=node

    # 等 ssh tunnel 起来 (autossh / ssh -L 启动后需要 1-2s)
    await asyncio.sleep(2.0)

    # ws connect
    try:
        reader, writer = await ws_client_handshake(
            "127.0.0.1", local_port, "/node",
            extra_headers={"Authorization": f"Bearer {secret}"},
            timeout=10.0,
        )
    except (ConnectionError, asyncio.TimeoutError, OSError) as e:
        print(f"[remote-mgr] {node_id} ws handshake failed: {e}", flush=True)
        return

    print(f"[remote-mgr] {node_id} ws connected via 127.0.0.1:{local_port}", flush=True)

    try:
        # node 主动发 register_node (跟 client mode 同样语义), master 端处理
        # 第一帧必须是 register_node (远端 node server_mode 在 _serve_one 里发)
        opcode, payload = await asyncio.wait_for(
            read_frame(reader, expect_masked=False),  # node 是 server, 发 unmasked
            timeout=15.0,
        )
        if opcode != OPCODE_TEXT:
            print(f"[remote-mgr] {node_id} unexpected opcode {opcode}", flush=True)
            writer.close()
            return

        msg = json.loads(payload.decode("utf-8"))
        if msg.get("method") != "register_node":
            print(f"[remote-mgr] {node_id} expected register_node first, got {msg.get('method')!r}",
                  flush=True)
            writer.close()
            return

        params = msg.get("params", {})
        if params.get("secret") != secret:
            print(f"[remote-mgr] {node_id} bad secret in register_node", flush=True)
            # 回 error (master 这边作为 ws client, 发 mask=True)
            await send_text(writer, json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32000, "message": "bad secret"},
            }), masked=True)
            await send_close(writer, masked=True)
            return

        # 用 node_id from rn (config) 作为权威, 不信任 node 自报
        info = NodeInfo(
            node_id=node_id,
            host=params.get("host", rn["ssh_alias"]),
            capabilities=params.get("capabilities", []),
            ws_writer=writer,
        )
        # 标记 ws_writer 是 client-side (发送时需要 mask)
        # 现有 forward_send_to_agent 用 send_text(ws_writer, json) 默认 masked=False (server side)
        # master-connect mode 下 master 是 client, 必须 masked=True
        # 解决: writer 上加属性 _send_masked
        writer._pre_send_masked = True  # type: ignore
        registry.add_node(info)
        print(f"[remote-mgr] {node_id} registered (master-connect)", flush=True)

        # ack (master client side, mask=True)
        await send_text(writer, json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"ok": True, "ts": time.time()},
        }), masked=True)

        # message loop
        while True:
            opcode, payload = await read_frame(reader, expect_masked=False)
            if opcode == OPCODE_CLOSE:
                break
            if opcode == OPCODE_PING:
                writer.write(encode_frame(payload, OPCODE_PONG, masked=True))
                await writer.drain()
                continue
            if opcode != OPCODE_TEXT:
                continue
            try:
                m = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            await dispatch_fn(m, node_id, registry, db, writer)

    except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, OSError) as e:
        print(f"[remote-mgr] {node_id} session ended: {e}", flush=True)
    except Exception as e:
        import traceback
        print(f"[remote-mgr] {node_id} unexpected error: {e}\n{traceback.format_exc()[:1000]}",
              flush=True)
    finally:
        registry.remove_node(node_id)
        try:
            writer.close()
        except Exception:
            pass


# ---------- per-node manager loop ----------

async def manage_remote_node(rn: dict, registry, db, dispatch_fn,
                             stop_event: Optional[asyncio.Event] = None):
    """单个 remote_node 的全 lifecycle: ssh exec → tunnel → ws session → 重连.
    永久跑直到 stop_event 触发."""
    node_id = rn["node_id"]
    backoff = 5.0
    BACKOFF_MAX = 60.0
    tunnel_proc = None

    try:
        while not (stop_event and stop_event.is_set()):
            try:
                # 1. 启远端 daemon (idempotent: 远端有 pid file 检查)
                ok = await _ssh_exec_start_daemon(rn)
                if not ok:
                    print(f"[remote-mgr] {node_id} daemon spawn failed, retry in {backoff}s",
                          flush=True)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue

                # 2. 启 ssh tunnel (如果之前的死了)
                if tunnel_proc is None or tunnel_proc.returncode is not None:
                    tunnel_proc = await _spawn_ssh_tunnel(rn)
                    if tunnel_proc is None:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, BACKOFF_MAX)
                        continue

                # 3. ws session
                await _handle_remote_node_session(rn, registry, db, dispatch_fn)
                # session 结束 (disconnect)
                backoff = 5.0  # reset

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[remote-mgr] {node_id} loop error: {e}", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
    finally:
        if tunnel_proc and tunnel_proc.returncode is None:
            try:
                tunnel_proc.terminate()
                await asyncio.wait_for(tunnel_proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    tunnel_proc.kill()
                except ProcessLookupError:
                    pass


async def start_remote_node_managers(registry, db, dispatch_fn):
    """master 启动时调一次, 对 remote_nodes.json 中所有 enabled 节点 spawn manager task."""
    nodes = load_remote_nodes()
    if not nodes:
        print(f"[remote-mgr] no remote nodes configured ({REMOTE_NODES_CONFIG})", flush=True)
        return []

    print(f"[remote-mgr] launching {len(nodes)} remote node manager(s): "
          f"{[n['node_id'] for n in nodes]}", flush=True)

    tasks = []
    for rn in nodes:
        t = asyncio.create_task(manage_remote_node(rn, registry, db, dispatch_fn))
        tasks.append(t)
    return tasks
