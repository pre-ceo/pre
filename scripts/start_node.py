#!/usr/bin/env python3
"""
启动 pre Node — 加载 driver, 注册 agent 到 Master.

用法:
  # 本机 client mode (默认, 主动连 master)
  uv run python scripts/start_node.py --node-id local --capabilities cli-claude-code-local

  # 远端 server mode ( master-connect, 被动等 master ws connect)
  uv run python scripts/start_node.py --node-id remote-node \\
      --transport ws-server \\
      --listen-host 127.0.0.1 \\
      --listen-port 9500 \\
      --capabilities cli-claude-code-local

  注: cli_claude_code_remote driver 在 删除. 远端控制走 master-connect (远端
  跑独立 node 加载 cli-claude-code-local driver, 通过 ssh tunnel 跟 master 通信).
"""
import argparse
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from node.client import NodeClient
from node.driver_manager import DriverManager
from node.agent_proxy import spawn_proxy_thread, derive_master_http_url
from common.paths import PRE_RULE_ROOT

# token: lazy resolve from ~/.pre/env via token_resolver (PR3)
try:
    from src.common.token_resolver import resolve as _resolve_token  # hook context
except ImportError:
    from common.token_resolver import resolve as _resolve_token  # master context


async def run_node(args):
    # node_ctx 包含给 driver 用的所有配置
    # 删 remote_host/remote_pre_path/remote_rule_root/remote_node_id
    # (cli_claude_code_remote driver 已删, master-connect 替代)
    node_ctx = {
        "node_id": args.node_id,
    }

    caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    dm = DriverManager(node_ctx)
    await dm.load(caps)

    # transport 选项 — client (默认主动连 master) / server (被动等 master connect)
    client = NodeClient(
        node_id=args.node_id,
        master_url=args.master,
        secret=args.secret,
        capabilities=caps,
        server_mode=(args.transport == "ws-server"),
        listen_host=args.listen_host,
        listen_port=args.listen_port,
    )

    # ── stale 追踪: register_and_mark_stale 跨 closure 共享 ──
    # 跨 disconnect/reconnect 保留, 但 node 重启会丢; master 跨重启也不持久化 registry,
    # 所以单 session 内有效, 重启全清. 用户接受此简化.
    last_registered_specs: dict[str, dict] = {}

    async def register_and_mark_stale(agents: list[dict]) -> dict:
        """register 当前 agents + diff 出消失的 re-register 标 status=stale.

        让 master/GUI 把 "driver 已不再 yield 的老 agent" 标 stale,
        跟 ok / failed 区分开 — 三态分别对应:
          ok    = driver 当前 yield 且配置全 (cwd/pre/hook/tmux 都 ok)
          failed= driver 当前 yield 但缺一项 (no-pointer / cwd-missing / hook 缺等)
          stale = driver 早期 yield 过, 现在不再 yield (cwd 删了 / pointer 删了)
        """
        import time as _t
        new_ids = {a["agent_id"] for a in agents}
        stale_ids = set(last_registered_specs) - new_ids

        reg_success = 0
        reg_err = 0
        for a in agents:
            try:
                await client.call("register_agent", a, timeout=10.0)
                reg_success += 1
            except Exception as e:
                reg_err += 1
                print(f"[node] register_agent {a['agent_id']} failed: {e}", flush=True)

        stale_success = 0
        for sid in stale_ids:
            old_spec = last_registered_specs.get(sid) or {}
            stale_meta = dict(old_spec.get("metadata", {}) or {})
            stale_meta["status"] = "stale"
            stale_meta["failure_reason"] = "removed-from-driver-discover"
            stale_meta["failure_hint"] = (
                "agent disappeared from driver discover — cwd removed, "
                "pre_rule pointer deleted, or driver type changed"
            )
            stale_meta["_pruned_ts"] = _t.time()
            stale_spec = dict(old_spec)
            stale_spec["metadata"] = stale_meta
            stale_spec["state"] = "stale"
            try:
                await client.call("register_agent", stale_spec, timeout=5.0)
                stale_success += 1
            except Exception as e:
                print(f"[node] mark stale {sid} failed: {e}", flush=True)

        last_registered_specs.clear()
        for a in agents:
            last_registered_specs[a["agent_id"]] = a

        return {"registered": reg_success, "stale": stale_success, "errors": reg_err}

    async def inbound_handler(method: str, params: dict):
        """处理 master 推过来的消息"""
        if method == "ping":
            return {"pong": True, "ts": params.get("ts")}

        # : master 内嵌 cron 跨 node RPC, master 推 exec_cmd 让 node 跑 subprocess
        # detached, fire-and-forget. cmd 自己负责 POST master 报状态.
        if method == "exec_cmd":
            import subprocess as _sp
            cmd = params.get("cmd") or []
            cwd = params.get("cwd") or None
            env_extra = params.get("env") or {}
            schedule_id = params.get("schedule_id") or "<adhoc>"
            if not isinstance(cmd, list) or not cmd:
                return {"ok": False, "error": "invalid cmd"}
            env = os.environ.copy()
            try:
                env.update({str(k): str(v) for k, v in env_extra.items()})
            except Exception:
                pass
            try:
                proc = _sp.Popen(
                    cmd, cwd=cwd, env=env,
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    start_new_session=True,
                )
                print(f"[node] exec_cmd schedule={schedule_id} cmd={cmd[:3]} pid={proc.pid}", flush=True)
                return {"ok": True, "pid": proc.pid, "node_id": args.node_id,
                        "schedule_id": schedule_id}
            except (OSError, ValueError) as e:
                print(f"[node] exec_cmd failed: {e}", flush=True)
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

        # phase 1: master 单向 broadcast 规则文件 (HMAC + sha256 验证)
        # 落地 chmod 444 (advisory chattr +i 需 root, phase 1 仅 chmod). 拒反向 push.
        if method == "sync_rules":
            import hmac as _hmac
            import hashlib as _hashlib
            target_relpath = params.get("target_relpath") or ""
            content = params.get("content") or ""
            expected_sha256 = params.get("sha256") or ""
            expected_hmac = params.get("hmac") or ""
            secret = os.environ.get("PRE_SYNC_HMAC_SECRET", "")
            if not secret or len(secret) < 32:
                return {"ok": False, "error": "PRE_SYNC_HMAC_SECRET missing or <32 bytes"}
            # 路径限制: 必须以 freerun/ 开头, 不含 ../ 防路径注入
            if (".." in target_relpath or target_relpath.startswith("/")
                    or not target_relpath.startswith("freerun/")):
                return {"ok": False, "error": "invalid target_relpath"}
            # 验证 sha256
            actual_sha = _hashlib.sha256(content.encode("utf-8")).hexdigest()
            if actual_sha != expected_sha256:
                return {"ok": False, "error": f"sha256 mismatch (expect {expected_sha256[:16]}, got {actual_sha[:16]})"}
            # 验证 HMAC
            actual_hmac = _hmac.new(secret.encode("utf-8"),
                                     content.encode("utf-8"),
                                     _hashlib.sha256).hexdigest()
            if not _hmac.compare_digest(actual_hmac, expected_hmac):
                return {"ok": False, "error": "hmac verify failed"}
            # 落地 chmod 444
            from pathlib import Path as _Path
            rule_root = _Path(PRE_RULE_ROOT)
            target = rule_root / target_relpath
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                os.chmod(str(target), 0o444)
                print(f"[node] sync_rules ok: {target_relpath} ({len(content)} bytes, sha256={actual_sha[:12]})", flush=True)
                return {"ok": True, "node_id": args.node_id, "target_relpath": target_relpath,
                        "bytes": len(content), "sha256": actual_sha}
            except OSError as e:
                return {"ok": False, "error": f"write failed: {type(e).__name__}: {str(e)[:200]}"}

        if method == "command_agent":
            agent_id = params.get("agent_id", "")
            driver_type = params.get("driver_type", "")
            op = params.get("op", "")
            op_args = params.get("args", {})
            if op == "send":
                ok = await dm.send(agent_id, driver_type, op_args)
                return {"ok": ok}
            elif op == "get_state":
                state = await dm.get_state(agent_id, driver_type)
                return {"state": state}
            elif op == "decide":
                key = op_args.get("key", "")
                ok = await dm.decide(agent_id, driver_type, key)
                return {"ok": ok}
            return {"error": "unknown op"}

        if method == "discover_agents":
            agents = await dm.discover_all_agents()
            stats = await register_and_mark_stale(agents)
            return {"agents": agents, **stats}

        return {"warning": f"unknown method: {method}"}

    client.on_inbound = inbound_handler

    # 后台任务: 每次连接 (含重连) 都重新 register agents
    # 修老 bug — master 重启后 node 不自动 re-register
    async def register_loop():
        last_set = False
        while True:
            try:
                if not client._connected.is_set():
                    if last_set:
                        print(f"[node] disconnected, will re-register on reconnect", flush=True)
                    last_set = False
                    # 等连接 set
                    await client._connected.wait()

                if not last_set and client._connected.is_set():
                    print(f"[node] connected, discovering agents", flush=True)
                    agents = await dm.discover_all_agents()
                    print(f"[node] discovered {len(agents)} agents", flush=True)
                    stats = await register_and_mark_stale(agents)
                    print(f"[node] registered {stats['registered']}/{len(agents)} agents "
                          f"(+{stats['stale']} stale, {stats['errors']} errors)", flush=True)
                    last_set = True

                # A3: 检查 network probe pending re-register
                # _bg fill cache 后加 cwd 进 pending, 这里取出 → re-register 那些 agent
                # (让 metadata.network 字段从 None 变实际值)
                if last_set and client._connected.is_set():
                    try:
                        from drivers.cli_claude_code_local.driver import take_pending_reregister
                        pending_cwds = take_pending_reregister()
                        if pending_cwds:
                            agents = await dm.discover_all_agents()
                            re_count = 0
                            for a in agents:
                                if (a.get("metadata") or {}).get("cwd") in pending_cwds:
                                    try:
                                        await client.call("register_agent", a, timeout=10.0)
                                        re_count += 1
                                    except Exception as e:
                                        print(f"[node] re-register network {a['agent_id']} failed: {e}", flush=True)
                            if re_count:
                                print(f"[node] network re-registered {re_count} agents (cache fill)", flush=True)
                    except Exception as e:
                        print(f"[node] take_pending_reregister error: {e}", flush=True)

                # poll 直到 disconnect
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                import traceback
                print(f"[node] register_loop error: {e}\n{traceback.format_exc()}", flush=True)
                await asyncio.sleep(5.0)

    asyncio.create_task(register_loop())

    # 后台任务: 周期性 detect_pending + detect_activity 上报 master (10s)
    async def pending_reporter():
        try:
            await client._connected.wait()
            while True:
                try:
                    pending = await dm.list_all_pending()
                    await client.notify("report_pending", {
                        "node_id": args.node_id,
                        "pending": pending,
                    })
                except Exception as e:
                    print(f"[node] report_pending error: {e}", flush=True)
                await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            pass

    async def activity_reporter():
        try:
            await client._connected.wait()
            while True:
                try:
                    activity = await dm.list_all_activity()
                    await client.notify("report_activity", {
                        "node_id": args.node_id,
                        "activity": activity,
                    })
                except Exception as e:
                    print(f"[node] report_activity error: {e}", flush=True)
                await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            pass

    asyncio.create_task(pending_reporter())
    asyncio.create_task(activity_reporter())

    # stuck detector — 30s 周期检测有 pending 输入但未 busy 的 agent
    # fix: 跟踪文本变化, 必须连续 ≥3 次同文本 (90s 不变) 才算 stuck
    # fix: session attached 时完全跳过 (用户在打字, 任何 90s 不变都不应 Enter)
    stuck_state = {}

    async def stuck_detector_loop():
        try:
            await client._connected.wait()
            import sys, os, subprocess
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
            from tmux_helper import (
                is_input_pending, send_key,
                get_outstanding_inject, clear_outstanding_inject,
            )
            STUCK_THRESHOLD = 3
            while True:
                try:
                    # 一次性获取 attached sessions 集合
                    attached = set()
                    try:
                        r = subprocess.run(
                            ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}"],
                            capture_output=True, text=True, timeout=3,
                        )
                        for line in (r.stdout or "").splitlines():
                            if ":1" in line:
                                attached.add(line.split(":")[0])
                    except (subprocess.TimeoutExpired, OSError):
                        pass

                    for t, drv in dm.drivers.items():
                        # codex driver 也纳入 (codex 输入框同样会卡)
                        if t not in ("cli-claude-code-local", "cli-codex-local"):
                            continue
                        try:
                            specs = await drv.discover_agents()
                        except Exception:
                            continue
                        for spec in specs:
                            ts = spec.metadata.get("tmux_session", "")
                            if not ts:
                                continue
                            # attached session 完全跳过 — 用户在打字, 任何 stuck 判定都可能误触发
                            if ts in attached:
                                stuck_state.pop(spec.agent_id, None)
                                continue
                            stuck, pending_text = is_input_pending(ts)
                            aid = spec.agent_id
                            if not stuck:
                                stuck_state.pop(aid, None)
                                continue
                            # 文本变化跟踪
                            prev = stuck_state.get(aid)
                            if prev and prev["text"] == pending_text:
                                stuck_state[aid] = {"text": pending_text, "count": prev["count"] + 1}
                            else:
                                stuck_state[aid] = {"text": pending_text, "count": 1}
                            cnt = stuck_state[aid]["count"]
                            if cnt < STUCK_THRESHOLD:
                                continue  # 还在打字, 不动
                            # provenance 检查 — 仅对 driver 已注入的 inbox 文本 auto-Enter,
                            # ghost-text / 用户 paste / 未知来源一律不动 (保守 0 风险).
                            inject = get_outstanding_inject(ts)
                            if not inject:
                                print(f"[node] stuck_detector: {aid} pending_text 无 inject 登记, 跳过 auto-Enter (ghost-text/paste? text={pending_text[:40]!r})", flush=True)
                                stuck_state.pop(aid, None)
                                continue
                            # 严格全等: pending_text 截 200 字, 跟 inject_text[:200] 必须字字相同
                            inject_text_truncated = inject["text"][:200]
                            if pending_text != inject_text_truncated:
                                print(f"[node] stuck_detector: {aid} pending_text 跟 inject 不匹配, 跳过 auto-Enter (pending={pending_text[:40]!r} inject={inject_text_truncated[:40]!r})", flush=True)
                                stuck_state.pop(aid, None)
                                continue
                            # 匹配 → 真卡住的 inbox 注入, 安全 retry Enter
                            send_key(ts, "Enter")
                            clear_outstanding_inject(ts)
                            print(f"[node] stuck_detector: {aid} text unchanged for {cnt}×30s, auto Enter sent (provenance OK, text={pending_text[:40]!r})", flush=True)
                            stuck_state.pop(aid, None)
                            try:
                                await client.notify("agent_event", {
                                    "agent_id": aid,
                                    "event": "auto_unstuck",
                                    "pending_text": pending_text[:200],
                                    "stuck_intervals": cnt,
                                })
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[node] stuck_detector error: {e}", flush=True)
                await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            pass

    asyncio.create_task(stuck_detector_loop())

    # Phase A MVP: spawn agent-proxy HTTP server (loopback only)
    # agent → node :http_port → node forwards → master HTTP (sidecar 模式).
    # Phase A2 后续重构: 改 ws RPC wrap (master_proxy_request method), 不再走本机 HTTP loopback.
    if args.http_port and args.http_port > 0:
        master_http_url = derive_master_http_url(
            args.master,
            env_override=os.environ.get("PRE_PROXY_MASTER_URL"),
        )
        try:
            spawn_proxy_thread(
                host=args.http_host,
                port=args.http_port,
                master_url=master_http_url,
                master_secret=args.secret,
            )
        except OSError as e:
            print(f"[node] agent-proxy spawn failed (port {args.http_port} busy?): {e}", flush=True)

    if args.transport == "ws-server":
        print(f"[node] starting node_id={args.node_id} (ws-server on {args.listen_host}:{args.listen_port}), drivers={list(dm.drivers.keys())}", flush=True)
        try:
            await client.run_server()
        finally:
            await dm.shutdown()
    else:
        print(f"[node] starting node_id={args.node_id} → {args.master}, drivers={list(dm.drivers.keys())}", flush=True)
        try:
            await client.run()
        finally:
            await dm.shutdown()


def main():
    # Phase D cond3: umask 077 process-level set (daemon side)
    # 防新建 file 默认权限超 600. 跟 start_master.py 同 spirit, 治理债务 1 行 fix.
    os.umask(0o077)

    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default=socket.gethostname().split(".")[0])
    p.add_argument("--master", default="ws://127.0.0.1:19500/node")
    p.add_argument("--secret", default=None,
                   help="ws bearer; 默认从 ~/.pre/env PRE_NODE_SECRET 取 (PR3)")
    p.add_argument("--capabilities", default="",
                   help="逗号分隔: cli-claude-code-local, cli-codex-local "
                        "(cli-claude-code-remote 已废弃)")
    # transport 选项
    p.add_argument("--transport", default="ws-client",
                   choices=["ws-client", "ws-server"],
                   help="ws-client (默认, 主动连 master) / ws-server (被动等 master connect)")
    p.add_argument("--listen-host", default="127.0.0.1",
                   help="ws-server 模式监听 host (默认 127.0.0.1, 不公网)")
    p.add_argument("--listen-port", type=int, default=9500,
                   help="ws-server 模式监听端口")
    # Phase A: agent-proxy HTTP server (loopback only)
    p.add_argument("--http-host", default=os.environ.get("NODE_HTTP_HOST", "127.0.0.1"),
                   help="agent-proxy listen host (默认 127.0.0.1, loopback only)")
    p.add_argument("--http-port", type=int,
                   default=int(os.environ.get("NODE_HTTP_PORT", "19501")),
                   help="agent-proxy listen port (本机默认 19501, 远端可设 9501; 设 0 禁用)")
    args = p.parse_args()
    if args.secret is None:
        args.secret = _resolve_token("node")  # PRE_NODE_SECRET from ~/.pre/env

    try:
        asyncio.run(run_node(args))
    except KeyboardInterrupt:
        print("\n[node] stopped.")


if __name__ == "__main__":
    main()
