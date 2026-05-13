"""
pre HTTP API Server
纯 stdlib 实现 (http.server), 绑定 127.0.0.1

Endpoints:
  GET /agents — 列出所有 agent
  GET /agents/{id}/logs — 最近日志
  GET /agents/{id}/logs?n=50 — 指定数量
  GET /agents/{id}/status — 当前状态 (模式、最后活动)
  POST /agents/{id}/analyze — 手动触发分析
  PUT /agents/{id}/mode — 切换模式 {"mode": "supervised"|"autonomous"|"freerun"}
"""
import json
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from .config import load_config, PROJECT_ROOT
from .analyzer import analyze_stop, load_agent_config, save_agent_config
from .governor import ensure_agent_dir


class APIHandler(BaseHTTPRequestHandler):
    """pre API request handler"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path == "/agents":
            return self._list_agents()

        parts = path.split("/")
        # /agents/{id}/logs or /agents/{id}/status
        if len(parts) == 4 and parts[1] == "agents":
            agent_id = parts[2]
            action = parts[3]
            if action == "logs":
                n = int(params.get("n", ["20"])[0])
                return self._agent_logs(agent_id, n)
            elif action == "status":
                return self._agent_status(agent_id)

        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        parts = path.split("/")

        # POST /agents/{id}/analyze
        if len(parts) == 4 and parts[1] == "agents" and parts[3] == "analyze":
            return self._agent_analyze(parts[2])

        self._json_response(404, {"error": "not found"})

    def do_PUT(self):
        path = urlparse(self.path).path.rstrip("/")
        parts = path.split("/")

        # PUT /agents/{id}/mode
        if len(parts) == 4 and parts[1] == "agents" and parts[3] == "mode":
            return self._agent_set_mode(parts[2])

        self._json_response(404, {"error": "not found"})

    # --- Handlers ---

    def _list_agents(self):
        cfg = load_config()
        agents_dir = cfg.pre_base_dir
        if not os.path.isdir(agents_dir):
            return self._json_response(200, {"agents": []})

        agents = []
        for name in sorted(os.listdir(agents_dir)):
            agent_path = os.path.join(agents_dir, name)
            if not os.path.isdir(agent_path):
                continue
            agent_cwd = "/" + name.replace("-", "/")
            config = load_agent_config(agent_path, agent_cwd)
            last_activity = self._get_last_activity(cfg.log_dir, name)
            pre_dir = os.path.join(agent_cwd, "pre")
            agents.append({
                "id": name,
                "mode": config.get("mode", "supervised"),
                "last_activity": last_activity,
                "has_rules": os.path.isfile(os.path.join(pre_dir, "rules.md")),
                "has_analyze_rules": os.path.isfile(os.path.join(pre_dir, "analyze_rules.md")),
            })

        self._json_response(200, {"agents": agents})

    def _agent_logs(self, agent_id: str, n: int):
        cfg = load_config()
        agent_path = os.path.join(cfg.pre_base_dir, agent_id)
        if not os.path.isdir(agent_path):
            return self._json_response(404, {"error": f"agent '{agent_id}' not found"})

        # 从 agent_id 反推 cwd: 把 - 替换回 /
        cwd = "/" + agent_id.replace("-", "/")
        logs = self._read_logs(cfg.log_dir, cwd, n)
        # 为每条日志附加 summary (便于前端展示)
        for log in logs:
            log["summary"] = _summarize_log(log)
        self._json_response(200, {"agent": agent_id, "count": len(logs), "logs": logs})

    def _agent_status(self, agent_id: str):
        cfg = load_config()
        agent_path = os.path.join(cfg.pre_base_dir, agent_id)
        if not os.path.isdir(agent_path):
            return self._json_response(404, {"error": f"agent '{agent_id}' not found"})

        cwd = "/" + agent_id.replace("-", "/")
        config = load_agent_config(agent_path, cwd)
        last_activity = self._get_last_activity(cfg.log_dir, agent_id)

        # 规则文件从项目 {cwd}/pre/ 读取
        pre_dir = os.path.join(cwd, "pre")
        rules_path = os.path.join(pre_dir, "rules.md")
        analyze_rules_path = os.path.join(pre_dir, "analyze_rules.md")

        # 读取 stop hook 实时状态
        stop_status = None
        stop_status_path = os.path.join(agent_path, "stop_status.json")
        if os.path.isfile(stop_status_path):
            try:
                with open(stop_status_path) as f:
                    stop_status = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        status = {
            "agent": agent_id,
            "cwd": cwd,
            "mode": config.get("mode", "supervised"),
            "last_activity": last_activity,
            "has_rules": os.path.isfile(rules_path),
            "has_analyze_rules": os.path.isfile(analyze_rules_path),
            "cache_file": os.path.isfile(os.path.join(agent_path, "decision_cache.json")),
            "stop_hook": stop_status,
        }
        self._json_response(200, status)

    def _agent_analyze(self, agent_id: str):
        cfg = load_config()
        agent_path = os.path.join(cfg.pre_base_dir, agent_id)
        if not os.path.isdir(agent_path):
            return self._json_response(404, {"error": f"agent '{agent_id}' not found"})

        cwd = "/" + agent_id.replace("-", "/")
        analysis = analyze_stop(
            agent_pre_dir=agent_path,
            rules_dir=cfg.rules_dir,
            cwd=cwd,
            log_dir=cfg.log_dir,
            last_n=20,
            timeout=cfg.governor_timeout,
            provider=cfg.governor_provider,
        )
        self._json_response(200, {"agent": agent_id, "analysis": analysis})

    def _agent_set_mode(self, agent_id: str):
        cfg = load_config()
        agent_path = os.path.join(cfg.pre_base_dir, agent_id)

        # 读取请求 body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return self._json_response(400, {"error": "missing body"})

        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid JSON"})

        new_mode = data.get("mode", "")
        if new_mode not in ("supervised", "autonomous", "freerun"):
            return self._json_response(400, {"error": "mode must be 'supervised', 'autonomous', or 'freerun'"})

        # 确保目录存在
        os.makedirs(agent_path, exist_ok=True)
        cwd = "/" + agent_id.replace("-", "/")
        config = load_agent_config(agent_path, cwd)
        old_mode = config.get("mode", "supervised")
        config["mode"] = new_mode
        save_agent_config(agent_path, config, cwd)

        self._json_response(200, {
            "agent": agent_id,
            "old_mode": old_mode,
            "new_mode": new_mode,
        })

    # --- Helpers ---

    def _read_logs(self, log_dir: str, cwd: str, n: int) -> list:
        """读取指定 cwd 的最近 N 条日志"""
        if not os.path.isdir(log_dir):
            return []

        files = sorted(
            [f for f in os.listdir(log_dir) if f.startswith("pre_hook_") and f.endswith(".jsonl")],
        )

        entries = []
        for fname in files[-3:]:
            fpath = os.path.join(log_dir, fname)
            try:
                with open(fpath) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                            if e.get("cwd", "") == cwd:
                                entries.append(e)
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

        return entries[-n:]

    def _get_last_activity(self, log_dir: str, agent_id: str) -> str:
        """获取 agent 最后活动时间"""
        cwd = "/" + agent_id.replace("-", "/")
        logs = self._read_logs(log_dir, cwd, 1)
        if logs:
            return logs[-1].get("ts", "")
        return ""

    def _json_response(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())

    def log_message(self, format, *args):
        """覆盖默认日志, 简化输出"""
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {args[0]}")


def _summarize_log(entry: dict) -> str:
    """
    根据日志条目生成一行人类可读的摘要, 含工具名 + 关键参数 + 决策
    支持 Claude Code 和 Gemini CLI 工具名
    """
    event = entry.get("event", "")
    tool = entry.get("tool", "?")
    decision = entry.get("decision", "?")
    source = entry.get("source", "")
    reason = entry.get("reason", "")
    inp = entry.get("input", {}) or {}

    # Stop event 特殊处理
    if event == "stop":
        mode = entry.get("mode", "")
        stop_reason = entry.get("stop_reason", "") or entry.get("analysis", {}).get("stop_reason", "")
        return f"STOP [{mode}] {stop_reason}".strip()

    # 提取工具关键参数
    detail = ""
    if tool in ("Bash", "run_shell_command"):
        detail = inp.get("command", "")[:100]
    elif tool in ("Read", "read_file"):
        detail = inp.get("file_path") or inp.get("absolute_path", "")
    elif tool in ("Write", "Edit", "write_file", "replace"):
        fp = inp.get("file_path") or inp.get("absolute_path", "")
        detail = f"write {fp}"
    elif tool in ("Grep", "grep_search"):
        pattern = inp.get("pattern", "")
        path = inp.get("path") or inp.get("dir_path", "")
        detail = f'"{pattern}" in {path}' if path else f'"{pattern}"'
    elif tool in ("Glob", "glob"):
        pattern = inp.get("pattern", "")
        path = inp.get("path") or inp.get("dir_path", "")
        detail = f"{pattern} in {path}" if path else pattern
    elif tool == "list_directory":
        detail = inp.get("dir_path") or inp.get("path", "")
    elif tool == "Agent":
        detail = inp.get("description", "")
    elif tool == "WebSearch":
        detail = inp.get("query", "")
    elif tool == "WebFetch":
        detail = inp.get("url", "")
    else:
        # fallback: 第一个有意义的字段
        for key in ("command", "query", "url", "description", "prompt",
                    "file_path", "absolute_path", "path", "dir_path", "pattern", "text"):
            if inp.get(key):
                detail = f"{key}={str(inp[key])[:80]}"
                break

    # 组合: [DECISION] (source) TOOL: detail | reason
    parts = [f"[{decision.upper()}]"]
    if source:
        parts.append(f"({source})")
    parts.append(f"{tool}:")
    if detail:
        parts.append(str(detail)[:120])
    if reason:
        parts.append(f"— {reason[:100]}")
    return " ".join(parts)


def run_server(host: str = "127.0.0.1", port: int = 19400):
    """启动 API server"""
    # SO_REUSEADDR: 防止重启时 Address already in use
    HTTPServer.allow_reuse_address = True
    server = HTTPServer((host, port), APIHandler)
    print(f"pre API server running at http://{host}:{port}")
    print(f"Endpoints:")
    print(f"  GET  /agents")
    print(f"  GET  /agents/{{id}}/logs?n=20")
    print(f"  GET  /agents/{{id}}/status")
    print(f"  POST /agents/{{id}}/analyze")
    print(f"  PUT  /agents/{{id}}/mode  body: {{\"mode\": \"supervised\"|\"autonomous\"}}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()
