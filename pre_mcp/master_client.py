"""master_client — pre_mcp 调 master HTTP API 的 facade.

stdlib only (urllib + json). 不直接 import master 内部模块.
Bearer + X-PRE-Node-Id headers 让 master 端二次校 from_agent prefix 可用.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional


# 强制 direct (绕开用户环境里的 HTTP_PROXY / Surge / Clash). master 是 loopback,
# 不应经任何系统代理. urllib 默认会读 env, 用显式 empty ProxyHandler 屏蔽.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class MasterClient:
    """pre_mcp → master HTTP facade. 全调用都加 Bearer + X-PRE-Node-Id."""

    def __init__(self, master_url: Optional[str] = None,
                 secret: Optional[str] = None,
                 node_id: Optional[str] = None,
                 timeout_sec: float = 5.0):
        self.master_url = master_url or os.environ.get(
            "PRE_MASTER_URL", "http://127.0.0.1:19500"
        )
        # PR3: 优先 PRE_MCP_SECRET (mcp-bound token); fallback PRE_SECRET (legacy, 过渡期)
        # pre_mcp 不能引 src/common (CLAUDE.md 隔离), 自己读 env key.
        self.secret = (secret
                       or os.environ.get("PRE_MCP_SECRET")
                       or os.environ.get("PRE_SECRET", "pre"))
        self.node_id = node_id or os.environ.get("PRE_NODE_ID", "local")
        self.timeout = timeout_sec

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.secret}",
            "X-PRE-Node-Id": self.node_id,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str,
                  body: Optional[dict] = None) -> tuple[bool, dict, int]:
        """通用 HTTP request. 返 (ok, parsed_json_or_error, status_code)."""
        url = self.master_url.rstrip("/") + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        try:
            req = urllib.request.Request(
                url, data=data, headers=self._headers(), method=method
            )
            with _NO_PROXY_OPENER.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return True, json.loads(raw), resp.status
                except json.JSONDecodeError:
                    return True, {"raw": raw}, resp.status
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
            except (json.JSONDecodeError, ValueError):
                err_body = {"error": "http_error"}
            return False, {**err_body, "_status": e.code}, e.code
        except (urllib.error.URLError, OSError) as e:
            return False, {"error": "connection_failed", "detail": str(e)[:200]}, 0

    def send_message(self, to_agent: str, kind: str, payload: dict,
                      from_agent: Optional[str] = None,
                      parent_id: Optional[str] = None) -> tuple[bool, dict]:
        body = {
            "kind": kind,
            "payload": payload,
        }
        if from_agent:
            body["from_agent"] = from_agent
        if parent_id:
            body["parent_id"] = parent_id
        ok, resp, _ = self._request(
            "POST", f"/api/v1/agents/{to_agent}/send", body
        )
        return ok, resp

    def fetch_inbox(self, agent_id: str, since: float = 0,
                     limit: int = 50, kind: Optional[str] = None) -> tuple[bool, dict]:
        path = f"/api/v1/agents/{agent_id}/messages?since={since}&limit={limit}"
        if kind:
            path += f"&kind={kind}"
        ok, resp, _ = self._request("GET", path)
        return ok, resp

    def read_pane(self, agent_id: str, lines: int = 100,
                   grep: Optional[str] = None) -> tuple[bool, dict]:
        path = f"/api/v1/agents/{agent_id}/pane?lines={lines}"
        if grep:
            import urllib.parse
            path += f"&grep={urllib.parse.quote(grep)}"
        path += "&raw=0"  # 强制 raw=false ( 配合, sanitized only)
        ok, resp, _ = self._request("GET", path)
        return ok, resp

    def cycle_state(self, agent_id: str) -> tuple[bool, dict]:
        ok, resp, _ = self._request(
            "GET", f"/api/v1/agents/{agent_id}/cycle_state"
        )
        return ok, resp

    def audit_mcp_tool_call(self, caller: str, tool: str,
                              args_redacted: dict, result_status: str,
                              latency_ms: int) -> bool:
        """audit kind=mcp_tool_call 留 master.db SOT .
         严白 schema additionalProperties:false. payload 字段固定.
        """
        body = {
            "kind": "mcp_tool_call",
            "payload": {
                "tool": tool,
                "caller_agent_id": caller,
                "args_redacted": args_redacted,
                "result_status": result_status,
                "latency_ms": latency_ms,
                "ts": time.time(),
            },
            "from_agent": caller,
        }
        ok, _, _ = self._request(
            "POST", "/api/v1/agents/audit.mcp/send", body
        )
        return ok
