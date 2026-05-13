"""
Master 持久化层 — sqlite

仅 stdlib (sqlite3). 表设计简单, 后续可加索引/迁移。
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    host TEXT,
    capabilities TEXT,
    last_seen REAL,
    online INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    node_id TEXT,
    driver_type TEXT,
    role TEXT,
    state TEXT,
    capabilities TEXT,
    metadata TEXT,
    last_update REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    ts REAL,
    from_agent TEXT,
    to_agent TEXT,
    from_role TEXT,
    to_role TEXT,
    kind TEXT,
    payload TEXT,
    parent_id TEXT,
    priority INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msg_from ON messages(from_agent);
CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_agent);
-- mini_task 小任务追踪 (一个 user prompt → stop 一个 cycle)
CREATE TABLE IF NOT EXISTS mini_tasks (
    mini_task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    request TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    reply TEXT,
    started_ts REAL NOT NULL,
    ended_ts REAL NOT NULL,
    duration_sec REAL,
    tool_count INTEGER DEFAULT 0,
    parent_dispatch_id TEXT,
    source TEXT DEFAULT 'transcript_parser',
    received_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mini_tasks_agent ON mini_tasks(agent_id, ended_ts DESC);
CREATE INDEX IF NOT EXISTS idx_mini_tasks_dispatch ON mini_tasks(parent_dispatch_id);
--  Phase A (): multi-node LLM usage telemetry
-- D3=B 持久化混合分层. 12-24 月趋势 + 重启续跑. v1 additive non-breaking schema.
-- [Phase B advisory blueprint per user IC-2=C lock,
--  write path commented per dispatcher 18:35 ruling, ]
-- Option B 回滚: schema 保留 (HC-G4 痕迹), write 路径在 server.py report_usage handler 注释.
-- IC-2=C 双轨实施: registry.usage_by_node + messages.kind=usage_event audit (不入此表).
-- 等真长期趋势分析需求出现 (≥6 月数据需求) 单独 dispatch 重启 SQLite write.
CREATE TABLE IF NOT EXISTS usage_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    recv_ts REAL NOT NULL,
    node_id TEXT NOT NULL,
    cli_type TEXT NOT NULL,
    agent_id TEXT,
    session_id TEXT,
    token_input INTEGER,
    token_output INTEGER,
    token_total INTEGER,
    quota_used INTEGER,
    quota_limit INTEGER,
    quota_used_pct REAL,
    quota_reset_at TEXT,
    billing_period TEXT,
    project_name TEXT,
    cwd_sanitized TEXT,
    redact_hits TEXT,
    schema_extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON usage_telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_node ON usage_telemetry(node_id, ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_cli ON usage_telemetry(cli_type, ts);
-- (user 直接需求): usage_prober snapshot upsert latest per provider.
-- 跟 usage_telemetry (append-only 时序) 不冲突: snapshot 是 latest valid state, telemetry 是历史.
-- 仅当 parser status ∈ {ok, limit_reached} 时 upsert (validation 在 caller 层 _validate_parsed).
CREATE TABLE IF NOT EXISTS usage_snapshot (
    provider TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    models_json TEXT,
    used_pct REAL,
    reset_at TEXT,
    fetch_ts REAL NOT NULL,
    source TEXT,
    raw_excerpt TEXT
);
-- (user ): account 升主键 + node 降 label + db 唯一 SoT.
-- (provider, account) PK 保证同 account 跨 node 采集只留最新一行.
-- collected_by_node 是 label 仅记录"出处", 不参与分组. UPSERT 用 ON CONFLICT WHERE
-- excluded.fetch_ts > existing.fetch_ts 防过期数据 (race / 时钟偏) 覆盖新数据.
-- 仅 status ∈ {ok, limit_reached} 入库; 其他 status (unknown / probe_inconclusive / skipped)
-- 永不污染此表 (HC-G11 vacuous truth).
CREATE TABLE IF NOT EXISTS usage_snapshot_v2 (
    provider TEXT NOT NULL,
    account TEXT NOT NULL,
    status TEXT NOT NULL,
    used_pct REAL,
    reset_at TEXT,
    fetch_ts REAL NOT NULL,
    collected_by_node TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    raw_excerpt TEXT,
    PRIMARY KEY (provider, account)
);
CREATE INDEX IF NOT EXISTS idx_usage_snap_v2_node ON usage_snapshot_v2(collected_by_node);
CREATE INDEX IF NOT EXISTS idx_usage_snap_v2_ts ON usage_snapshot_v2(fetch_ts DESC);
-- Phase A v2 (): per-node × per-cli last successful usage snapshot.
-- IC-2=C 合理细化扩展, current state SOT (sys_beep 拉点 GET /api/v1/usage/last_success).
-- 跟 messages.kind=usage_event audit (历史) + usage_telemetry (Phase B advisory 时序) 三表 cohabitation.
-- HC-DRLI-1 双 ts: ts_last_success (仅 success 更新) + ts_last_attempt (每次更新).
-- HC-DRLI-2 DB SOT: 这是权威源, registry.usage_by_node 内存退化派生 (master 启动 reload 自愈).
CREATE TABLE IF NOT EXISTS last_success_per_node (
    node_id TEXT NOT NULL,
    cli_type TEXT NOT NULL,
    ts_last_success REAL NOT NULL,        -- HC-DRLI-1: 仅 success 时更新 (collector ts)
    ts_last_attempt REAL NOT NULL,        -- HC-DRLI-1: 每次 attempt (success 或 fail) 都更新
    status_last_attempt TEXT NOT NULL,    -- HC-DRLI-1: enum [success, fail]
    recv_ts REAL NOT NULL,                -- master server clock when ws frame received
    quota_used INTEGER,
    quota_limit INTEGER,
    quota_used_pct REAL,
    quota_reset_at TEXT,
    billing_period TEXT,
    model TEXT,
    agent_id TEXT,
    session_id TEXT,
    raw_excerpt TEXT,                     -- redacted, ≤2KB
    PRIMARY KEY (node_id, cli_type)
);
CREATE INDEX IF NOT EXISTS idx_last_success_node ON last_success_per_node(node_id);
CREATE INDEX IF NOT EXISTS idx_last_success_recv_ts ON last_success_per_node(recv_ts DESC);
-- Phase A (): multi-node sync outbound 协议 manifest 表.
-- per file_path × per target_node, INSERT OR REPLACE 单行 SOT current state.
-- 跟 sync_audit jsonl (append-only history) 双轨, 跟 last_success_per_node 同模式.
-- HMAC: row_hmac 用 per-node secret HMAC () 防 master.db 篡改后伪造 manifest.
CREATE TABLE IF NOT EXISTS sync_manifest (
    file_path TEXT NOT NULL,
    target_node TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    source_mtime REAL NOT NULL,
    last_synced_ts REAL NOT NULL,
    last_sync_status TEXT NOT NULL,
    row_hmac TEXT,
    PRIMARY KEY (file_path, target_node)
);
CREATE INDEX IF NOT EXISTS idx_sync_manifest_node ON sync_manifest(target_node);
CREATE INDEX IF NOT EXISTS idx_sync_manifest_ts ON sync_manifest(last_synced_ts DESC);
-- Phase E (NS-M14 agent-security): dispatcher P0 治理债务关卡 audit 表.
-- chain_hash 链式哈希防 audit 篡改 (跟 git commit 链同 spirit).
CREATE TABLE IF NOT EXISTS governance_debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL,                   -- finding 文件名或 msg_id
    priority TEXT NOT NULL,                     -- enum [P0, P1, HIGH, INFO]
    title TEXT NOT NULL,
    detail TEXT,
    deadline_ts REAL,                           -- 14d D-lock 类 deadline (NULL = 无 deadline)
    created_ts REAL NOT NULL,
    resolved_ts REAL,                           -- NULL = unresolved
    status TEXT NOT NULL,                       -- enum [unresolved, resolved, dismissed]
    row_hmac TEXT NOT NULL,                     -- HMAC sha256(finding_id|priority|created_ts) per master secret
    chain_hash TEXT NOT NULL                    -- sha256(prev_chain_hash + row_hmac), genesis = sha256("genesis")
);
CREATE INDEX IF NOT EXISTS idx_gov_debts_status ON governance_debts(status);
CREATE INDEX IF NOT EXISTS idx_gov_debts_priority ON governance_debts(priority);
CREATE INDEX IF NOT EXISTS idx_gov_debts_deadline ON governance_debts(deadline_ts);

-- 多 token RBAC: 不同接入者(node/operator/cli/mcp) 各自一份 token,
-- master 仅持 sha256 hash, raw 不入库. 详见 dev-workflow/features/260510-multi-token-rbac-create.md
CREATE TABLE IF NOT EXISTS bus_tokens (
    token_hash    TEXT PRIMARY KEY,
    label         TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL,             -- node|operator|cli|mcp
    scopes        TEXT NOT NULL,             -- JSON array
    agent_id      TEXT,                      -- mcp 专属: 锁定调用方 from_agent
    created_ts    REAL NOT NULL,
    expires_ts    REAL,                      -- NULL = 永久
    last_used_ts  REAL,
    revoked_ts    REAL,                      -- 软删保留审计
    metadata      TEXT                       -- 自由 JSON
);
CREATE INDEX IF NOT EXISTS idx_bus_tokens_label   ON bus_tokens(label);
CREATE INDEX IF NOT EXISTS idx_bus_tokens_revoked ON bus_tokens(revoked_ts);
CREATE INDEX IF NOT EXISTS idx_bus_tokens_role    ON bus_tokens(role);
"""


class MasterDB:
    """简单 DAO. 不做线程安全 (asyncio 单 loop 用), 不做迁移"""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def upsert_node(self, node_id: str, host: str, capabilities: list,
                    last_seen: float, online: bool = True):
        self.conn.execute(
            "INSERT OR REPLACE INTO nodes(node_id, host, capabilities, last_seen, online) "
            "VALUES (?, ?, ?, ?, ?)",
            (node_id, host, json.dumps(capabilities), last_seen, 1 if online else 0),
        )
        self.conn.commit()

    def mark_node_offline(self, node_id: str):
        self.conn.execute(
            "UPDATE nodes SET online=0 WHERE node_id=?", (node_id,)
        )
        self.conn.commit()

    def list_nodes(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT node_id, host, capabilities, last_seen, online FROM nodes"
        )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "node_id": r[0],
                "host": r[1],
                "capabilities": json.loads(r[2] or "[]"),
                "last_seen": r[3],
                "online": bool(r[4]),
            })
        return rows

    def upsert_agent(self, agent_id: str, node_id: str, driver_type: str,
                     role: str, state: str, capabilities: list,
                     metadata: dict, ts: float):
        self.conn.execute(
            "INSERT OR REPLACE INTO agents(agent_id, node_id, driver_type, role, state, "
            "capabilities, metadata, last_update) VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, node_id, driver_type, role, state,
             json.dumps(capabilities), json.dumps(metadata), ts),
        )
        self.conn.commit()

    def list_agents(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT agent_id, node_id, driver_type, role, state, capabilities, "
            "metadata, last_update FROM agents"
        )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "agent_id": r[0], "node_id": r[1], "driver_type": r[2],
                "role": r[3], "state": r[4],
                "capabilities": json.loads(r[5] or "[]"),
                "metadata": json.loads(r[6] or "{}"),
                "last_update": r[7],
            })
        return rows

    def insert_message(self, msg: dict):
        self.conn.execute(
            "INSERT OR IGNORE INTO messages(id, ts, from_agent, to_agent, from_role, "
            "to_role, kind, payload, parent_id, priority) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                msg["id"], msg["ts"],
                msg["from_agent"], msg.get("to_agent"),
                msg.get("from_role", ""), msg.get("to_role"),
                msg["kind"], json.dumps(msg.get("payload", {})),
                msg.get("parent_id"), msg.get("priority", 0),
            ),
        )
        self.conn.commit()

    def upsert_usage_snapshot(self, provider: str, status: str,
                                  models: dict, used_pct: float | None,
                                  reset_at: str | None, fetch_ts: float,
                                  source: str = "tmux_pane_parse",
                                  raw_excerpt: str = "") -> bool:
        """(user ): upsert latest valid snapshot per provider.
        caller 必须先 validate (status ∈ {ok, limit_reached}), 此层仅 upsert.
        """
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO usage_snapshot("
                "provider, status, models_json, used_pct, reset_at, fetch_ts, source, raw_excerpt"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (
                    provider, status,
                    json.dumps(models or {}, ensure_ascii=False),
                    used_pct, reset_at, fetch_ts, source,
                    (raw_excerpt or "")[:1024],
                ),
            )
            self.conn.commit()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------------- usage_snapshot_v2 (account-keyed) ----------------

    def upsert_usage_snapshot_v2(self, *, provider: str, account: str,
                                  status: str, used_pct: float | None,
                                  reset_at: str | None, fetch_ts: float,
                                  collected_by_node: str,
                                  parsed: dict, raw_excerpt: str = "") -> tuple[bool, str]:
        """SoT upsert (user ).

        UPSERT (provider, account); WHERE excluded.fetch_ts > existing.fetch_ts 防过期覆盖.
        caller 必须先 validate (status ∈ {ok, limit_reached}).
        返 (ok, action): action ∈ {'inserted', 'updated', 'skipped_older', 'error'}.
        """
        try:
            cur = self.conn.execute(
                "SELECT fetch_ts FROM usage_snapshot_v2 WHERE provider=? AND account=?",
                (provider, account),
            )
            row = cur.fetchone()
            if row is not None and float(row[0]) >= float(fetch_ts):
                return True, "skipped_older"
            self.conn.execute(
                "INSERT INTO usage_snapshot_v2("
                "provider, account, status, used_pct, reset_at, fetch_ts, "
                "collected_by_node, parsed_json, raw_excerpt) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider, account) DO UPDATE SET "
                "  status = excluded.status, used_pct = excluded.used_pct, "
                "  reset_at = excluded.reset_at, fetch_ts = excluded.fetch_ts, "
                "  collected_by_node = excluded.collected_by_node, "
                "  parsed_json = excluded.parsed_json, raw_excerpt = excluded.raw_excerpt "
                "WHERE excluded.fetch_ts > usage_snapshot_v2.fetch_ts",
                (
                    provider, account, status, used_pct, reset_at, fetch_ts,
                    collected_by_node,
                    json.dumps(parsed or {}, ensure_ascii=False),
                    (raw_excerpt or "")[:1024],
                ),
            )
            self.conn.commit()
            return True, ("inserted" if row is None else "updated")
        except Exception as e:  # noqa: BLE001
            return False, f"error:{type(e).__name__}:{str(e)[:120]}"

    def query_usage_snapshot_v2(self) -> list[dict]:
        """返 [{provider, account, status, used_pct, reset_at, fetch_ts,
                collected_by_node, parsed, raw_excerpt}, ...] (按 provider, account 排).
        """
        cur = self.conn.execute(
            "SELECT provider, account, status, used_pct, reset_at, fetch_ts, "
            "collected_by_node, parsed_json, raw_excerpt "
            "FROM usage_snapshot_v2 ORDER BY provider, account"
        )
        out: list[dict] = []
        for row in cur.fetchall():
            try:
                parsed = json.loads(row[7]) if row[7] else {}
            except (ValueError, TypeError):
                parsed = {}
            out.append({
                "provider": row[0],
                "account": row[1],
                "status": row[2],
                "used_pct": row[3],
                "reset_at": row[4],
                "fetch_ts": row[5],
                "collected_by_node": row[6],
                "parsed": parsed,
                "raw_excerpt": row[8] or "",
            })
        return out

    def query_usage_snapshot(self,
                                providers: list[str] | None = None) -> dict:
        """返 {provider: {status, models, used_pct, reset_at, fetch_ts, source, raw_excerpt}}.
        providers=None 返全部.
        """
        if providers:
            placeholders = ",".join("?" * len(providers))
            cur = self.conn.execute(
                f"SELECT provider, status, models_json, used_pct, reset_at, "
                f"fetch_ts, source, raw_excerpt FROM usage_snapshot "
                f"WHERE provider IN ({placeholders})",
                tuple(providers),
            )
        else:
            cur = self.conn.execute(
                "SELECT provider, status, models_json, used_pct, reset_at, "
                "fetch_ts, source, raw_excerpt FROM usage_snapshot")
        out: dict = {}
        for row in cur.fetchall():
            try:
                models = json.loads(row[2]) if row[2] else {}
            except (ValueError, TypeError):
                models = {}
            out[row[0]] = {
                "status": row[1],
                "models": models,
                "used_pct": row[3],
                "reset_at": row[4],
                "fetch_ts": row[5],
                "source": row[6],
                "raw_excerpt": row[7] or "",
            }
        return out

    def insert_usage_telemetry(self, row: dict) -> int:
        """ Phase A ( D3=B).
        row 必含: schema_version, kind, ts, recv_ts, node_id, cli_type. 其他 optional.
        返 lastrowid (int).
        校验由 caller (server.py _validate_telemetry_payload) 已做, 此层仅 INSERT.
        """
        cur = self.conn.execute(
            "INSERT INTO usage_telemetry("
            "schema_version, kind, ts, recv_ts, node_id, cli_type, agent_id, session_id, "
            "token_input, token_output, token_total, "
            "quota_used, quota_limit, quota_used_pct, quota_reset_at, billing_period, "
            "project_name, cwd_sanitized, redact_hits, schema_extra"
            ") VALUES (?,?,?,?,?,?,?,?, ?,?,?, ?,?,?,?,?, ?,?,?,?)",
            (
                row["schema_version"], row["kind"], row["ts"], row["recv_ts"],
                row["node_id"], row["cli_type"],
                row.get("agent_id"), row.get("session_id"),
                row.get("token_input"), row.get("token_output"), row.get("token_total"),
                row.get("quota_used"), row.get("quota_limit"),
                row.get("quota_used_pct"), row.get("quota_reset_at"),
                row.get("billing_period"),
                row.get("project_name"), row.get("cwd_sanitized"),
                json.dumps(row.get("redact_hits") or {}),
                json.dumps(row.get("schema_extra") or {}),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def upsert_last_success(self, node_id: str, cli_type: str,
                              status_last_attempt: str,
                              ts_last_attempt: float,
                              recv_ts: float,
                              **fields) -> bool:
        """Phase A v2 (): UPSERT last_success_per_node 单行.

        HC-DRLI-1 双 ts 语义:
          - status_last_attempt='success': ts_last_success 跟 ts_last_attempt 同步更新 + 字段全 update
          - status_last_attempt='fail': 仅 ts_last_attempt 更新, ts_last_success + 字段保留上次 success

        HC-DRLI-2 DB SOT: 这层是权威源, 写失败 → caller 必 reject + finding HIGH (M11 outbox).
        fail-safe 返 True/False (失败不 raise, caller 决 reject 路径).
        """
        if status_last_attempt not in ("success", "fail"):
            return False
        try:
            if status_last_attempt == "success":
                # success 路径: 全字段 UPSERT, ts_last_success 更新到本次 ts_last_attempt
                self.conn.execute(
                    "INSERT INTO last_success_per_node("
                    "node_id, cli_type, ts_last_success, ts_last_attempt, "
                    "status_last_attempt, recv_ts, "
                    "quota_used, quota_limit, quota_used_pct, quota_reset_at, "
                    "billing_period, model, agent_id, session_id, raw_excerpt"
                    ") VALUES (?,?,?,?, ?,?, ?,?,?,?, ?,?,?,?,?) "
                    "ON CONFLICT(node_id, cli_type) DO UPDATE SET "
                    "ts_last_success=excluded.ts_last_success, "
                    "ts_last_attempt=excluded.ts_last_attempt, "
                    "status_last_attempt=excluded.status_last_attempt, "
                    "recv_ts=excluded.recv_ts, "
                    "quota_used=excluded.quota_used, "
                    "quota_limit=excluded.quota_limit, "
                    "quota_used_pct=excluded.quota_used_pct, "
                    "quota_reset_at=excluded.quota_reset_at, "
                    "billing_period=excluded.billing_period, "
                    "model=excluded.model, "
                    "agent_id=excluded.agent_id, "
                    "session_id=excluded.session_id, "
                    "raw_excerpt=excluded.raw_excerpt",
                    (
                        node_id, cli_type, ts_last_attempt, ts_last_attempt,
                        "success", recv_ts,
                        fields.get("quota_used"), fields.get("quota_limit"),
                        fields.get("quota_used_pct"), fields.get("quota_reset_at"),
                        fields.get("billing_period"), fields.get("model"),
                        fields.get("agent_id"), fields.get("session_id"),
                        fields.get("raw_excerpt"),
                    ),
                )
            else:
                # fail 路径: HC-DRLI-1 严守 — ts_last_success + 业务字段保留上次 success
                # 仅更新 ts_last_attempt + status_last_attempt + recv_ts
                # 若没上次 success 行, INSERT 时 ts_last_success=ts_last_attempt 占位 (但 status=fail)
                cur = self.conn.execute(
                    "SELECT ts_last_success FROM last_success_per_node "
                    "WHERE node_id=? AND cli_type=?",
                    (node_id, cli_type)
                )
                existing = cur.fetchone()
                if existing:
                    # 上次有行 (success 或 fail), 仅更新 attempt 相关
                    self.conn.execute(
                        "UPDATE last_success_per_node SET "
                        "ts_last_attempt=?, status_last_attempt='fail', recv_ts=? "
                        "WHERE node_id=? AND cli_type=?",
                        (ts_last_attempt, recv_ts, node_id, cli_type),
                    )
                else:
                    # 首次 attempt 即 fail, INSERT 占位 (业务字段全 NULL)
                    self.conn.execute(
                        "INSERT INTO last_success_per_node("
                        "node_id, cli_type, ts_last_success, ts_last_attempt, "
                        "status_last_attempt, recv_ts"
                        ") VALUES (?,?,?,?,?,?)",
                        (node_id, cli_type, ts_last_attempt, ts_last_attempt,
                         "fail", recv_ts),
                    )
            self.conn.commit()
            return True
        except Exception:  # noqa: BLE001 — fail-safe, caller decide reject
            return False

    def query_last_success(self, node_id: Optional[str] = None,
                             cli_type: Optional[str] = None) -> list[dict]:
        """Phase A v2 (): GET /api/v1/usage/last_success endpoint 用.
        no filter = 返全表. node_id / cli_type 可单独 filter.
        """
        sql = ("SELECT node_id, cli_type, ts_last_success, ts_last_attempt, "
               "status_last_attempt, recv_ts, "
               "quota_used, quota_limit, quota_used_pct, quota_reset_at, "
               "billing_period, model, agent_id, session_id, raw_excerpt "
               "FROM last_success_per_node")
        clauses = []
        params = []
        if node_id:
            clauses.append("node_id=?"); params.append(node_id)
        if cli_type:
            clauses.append("cli_type=?"); params.append(cli_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recv_ts DESC"
        cur = self.conn.execute(sql, tuple(params))
        cols = ["node_id", "cli_type", "ts_last_success", "ts_last_attempt",
                "status_last_attempt", "recv_ts",
                "quota_used", "quota_limit", "quota_used_pct", "quota_reset_at",
                "billing_period", "model", "agent_id", "session_id", "raw_excerpt"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def upsert_sync_manifest(self, file_path: str, target_node: str,
                                sha256: str, file_size: int,
                                source_mtime: float, last_synced_ts: float,
                                last_sync_status: str,
                                row_hmac: Optional[str] = None) -> bool:
        """ Phase A sync manifest INSERT OR REPLACE."""
        try:
            self.conn.execute(
                "INSERT INTO sync_manifest("
                "file_path, target_node, sha256, file_size, source_mtime, "
                "last_synced_ts, last_sync_status, row_hmac"
                ") VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_path, target_node) DO UPDATE SET "
                "sha256=excluded.sha256, file_size=excluded.file_size, "
                "source_mtime=excluded.source_mtime, "
                "last_synced_ts=excluded.last_synced_ts, "
                "last_sync_status=excluded.last_sync_status, "
                "row_hmac=excluded.row_hmac",
                (file_path, target_node, sha256, file_size, source_mtime,
                 last_synced_ts, last_sync_status, row_hmac),
            )
            self.conn.commit()
            return True
        except Exception:  # noqa: BLE001 — fail-safe
            return False

    def query_sync_manifest(self, target_node: Optional[str] = None,
                              file_path: Optional[str] = None) -> list[dict]:
        """ Phase A sync manifest 查询."""
        sql = ("SELECT file_path, target_node, sha256, file_size, source_mtime, "
               "last_synced_ts, last_sync_status, row_hmac FROM sync_manifest")
        clauses = []; params = []
        if target_node:
            clauses.append("target_node=?"); params.append(target_node)
        if file_path:
            clauses.append("file_path=?"); params.append(file_path)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_synced_ts DESC"
        cur = self.conn.execute(sql, tuple(params))
        cols = ["file_path", "target_node", "sha256", "file_size",
                "source_mtime", "last_synced_ts", "last_sync_status", "row_hmac"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def query_dispatch_events(self, dispatch_id: str) -> list[dict]:
        """拉某 dispatch_id 的所有相关 message, 按 ts 升序.
        匹配 payload TEXT 列里 '"dispatch_id":"<id>"' 子串."""
        # 防止 dispatch_id 被注入扩展模式
        if not dispatch_id or len(dispatch_id) > 64 or "%" in dispatch_id or "_" in dispatch_id.replace("-", ""):
            # 允许字母数字 + 连字符 (例 )
            import re as _re
            if not _re.match(r"^[A-Za-z0-9\-]{1,64}$", dispatch_id):
                return []
        # JSON serialize 可能含空格 "dispatch_id": "" 或不含空格, 用宽松 pattern
        pattern = f'%"dispatch_id"%"{dispatch_id}"%'
        cur = self.conn.execute(
            "SELECT id, ts, from_agent, to_agent, from_role, to_role, kind, "
            "payload, parent_id, priority FROM messages "
            "WHERE payload LIKE ? ORDER BY ts ASC",
            (pattern,),
        )
        out = []
        for r in cur.fetchall():
            payload = json.loads(r[7] or "{}")
            out.append({
                "id": r[0], "ts": r[1], "from_agent": r[2], "to_agent": r[3],
                "from_role": r[4], "to_role": r[5], "kind": r[6],
                "payload": payload, "parent_id": r[8], "priority": r[9],
            })
        return out

    def list_dispatches(self, since: float = 0, limit: int = 50,
                        status_filter: str | None = None) -> list[dict]:
        """扫 messages 抽 distinct dispatch_id, 给每个聚合一个概要.
        sqlite 没原生 JSON_EXTRACT (老版本没启用), 手动 LIKE + 解析."""
        # 找所有 payload 含 dispatch_id 的 message (LIKE 通配)
        cur = self.conn.execute(
            "SELECT ts, from_agent, to_agent, from_role, kind, payload "
            "FROM messages WHERE payload LIKE '%dispatch_id%' AND ts >= ? "
            "ORDER BY ts DESC",
            (since,),
        )
        # 按 dispatch_id 聚合
        agg: dict[str, dict] = {}  # dispatch_id -> dict
        for r in cur.fetchall():
            ts, frm, to, frm_role, kind, payload_str = r
            try:
                payload = json.loads(payload_str or "{}")
            except json.JSONDecodeError:
                continue
            did = payload.get("dispatch_id")
            if not did or not isinstance(did, str):
                continue
            if did not in agg:
                agg[did] = {
                    "dispatch_id": did,
                    "started_ts": ts, "last_ts": ts,
                    "ceo": None, "dispatcher": None,
                    "managers": set(),
                    # executor 单值 → executors set (多 executor 并行支持)
                    "executors": set(),
                    "first_executor": None,  # 内部, 兼容老字段 dispatch.executor
                    "reports_from": set(),   # 内部, 哪些 agent 发了 report (按 from_agent)
                    "msg_count": 0, "kinds": set(),
                    "task_title_sample": None,
                    "brief": None,           # dispatcher 派 task_request 时调 LLM 生成的概览
                    "brief_ts": None,
                    "brief_by": None,
                    "approve_seen": None,
                    "report_seen": False,
                }
            d = agg[did]
            d["msg_count"] += 1
            d["kinds"].add(kind)
            d["started_ts"] = min(d["started_ts"], ts)
            d["last_ts"] = max(d["last_ts"], ts)
            # dispatch_brief — event-driven LLM 概览
            if kind == "dispatch_brief":
                brief_text = payload.get("brief") or payload.get("text") or ""
                if brief_text and (not d["brief"] or ts > (d["brief_ts"] or 0)):
                    d["brief"] = brief_text[:200]
                    d["brief_ts"] = ts
                    d["brief_by"] = payload.get("generated_by") or frm or ""
                continue  # brief 不当作业务 message 计入 status 推断
            # 角色推断
            # 跟 executor 同根因 — frm_role == "CEO" 永远不匹配
            # (agent-ceo 发 task_request frm_role 实际是 "worker"), 老逻辑 task_title_sample
            # / ceo / dispatcher 一直推断不出. 改为不依赖 frm_role.
            if kind == "task_request" and not d["ceo"]:
                d["ceo"] = frm
                d["dispatcher"] = to
                # task_title 字段: 兼容多种命名 (task_title / task_summary / task / text)
                t = (payload.get("task_title") or payload.get("task_summary")
                     or payload.get("task") or payload.get("text") or "")
                if t and not d["task_title_sample"]:
                    d["task_title_sample"] = t[:80]
            if kind == "evaluate_request":
                if not d["dispatcher"] and frm:
                    d["dispatcher"] = frm
                if to:
                    d["managers"].add(to)
            # executors set (多 executor 并行支持).
            # 任何 kind=command 的 to_agent 都算 executor (不依赖 frm_role —
            # 实测 agent-ceo 发 command 的 frm_role 是 "worker" 不是 "CEO",
            # 老 frm_role==CEO 过滤一直推断不出 executor, 一直是 None).
            # 跟 list_dispatches_indexed 保持一致.
            if kind == "command" and to:
                d["executors"].add(to)
                if not d["first_executor"]:
                    d["first_executor"] = to
            if kind == "task_verdict":
                d["approve_seen"] = bool(payload.get("approve"))
            if kind == "report":
                d["report_seen"] = True
                # 记录哪个 agent 发的 report (用于 multi-executor 完成度)
                if frm:
                    d["reports_from"].add(frm)
        # 推断 status
        out = []
        import time as _time
        now = _time.time()
        # 缩短 abandoned 阈值, 不同 status 不同窗口
        # in_progress_evaluation 通常 5-20min 应完成; executing 6h 是常规上限; 旧的 24h 太长
        STALE_BY_STATUS = {
            "in_progress_evaluation": 3600,        # 1h
            "approved_pending_executor": 21600,    # 6h
            "executing": 21600,                    # 6h
            "unknown": 1800,                       # 30min
        }
        for did, d in agg.items():
            kinds = d["kinds"]
            # multi-executor status 推断
            # - 全 executors 都发了 report → done
            # - 部分 executors 发了 report → partial_done (新)
            # - 一个都没发 但 command 已派 → executing
            # - 其他状态保留
            exec_n = len(d["executors"])
            # reports_from 跟 executors 取交集 (只算 executor 发的 report)
            rep_done = len(d["reports_from"] & d["executors"]) if exec_n > 0 else 0
            if exec_n > 0 and rep_done >= exec_n:
                status = "done"
            elif exec_n > 0 and rep_done > 0:
                status = "partial_done"
            elif "report" in kinds and exec_n == 0:
                # 兼容老 dispatch (single executor 推断不到 from_agent ↔ executor)
                status = "done"
            elif "command" in kinds:
                status = "executing"
            elif "task_verdict" in kinds:
                status = "rejected" if d["approve_seen"] is False else "approved_pending_executor"
            elif "task_request" in kinds:
                status = "in_progress_evaluation"
            else:
                status = "unknown"
            stale_threshold = STALE_BY_STATUS.get(status, 86400)
            if (now - d["last_ts"]) > stale_threshold and status not in ("done", "rejected"):
                status = "abandoned"
            d["status"] = status
            d["managers"] = sorted(list(d["managers"]))
            # executors set 输出 + executor 兼容字段 (取 first)
            d["executors"] = sorted(list(d["executors"]))
            d["executor"] = d.pop("first_executor")
            d["reports_from"] = sorted(list(d["reports_from"]))  # 给 GUI 看完成度
            d.pop("kinds")
            d.pop("approve_seen")
            d.pop("report_seen")
            out.append(d)
        # 状态过滤
        if status_filter:
            out = [d for d in out if d["status"] == status_filter]
        # 按 last_ts 倒序
        out.sort(key=lambda x: x["last_ts"], reverse=True)
        return out[:limit]

    def find_recent_dispatch_for_agent(self, agent_id: str) -> tuple[str | None, str | None]:
        """找该 agent 最近相关的 dispatch_id + 它在该 dispatch 的角色.
        返回 (dispatch_id, role) 或 (None, None) 表示无.
        role: 'ceo' | 'dispatcher' | 'manager' | 'executor' | 'participant'
        只看最近一条 含 dispatch_id 的 message."""
        # 严格 pattern '"dispatch_id"' 含引号 (JSON 字段), 避免 chat text 里引用 "dispatch_id" 字串误匹配
        cur = self.conn.execute(
            "SELECT from_agent, to_agent, from_role, kind, payload, ts "
            "FROM messages "
            "WHERE (from_agent=? OR to_agent=?) AND payload LIKE '%\"dispatch_id\"%' "
            "ORDER BY ts DESC LIMIT 5",
            (agent_id, agent_id),
        )
        rows = cur.fetchall()
        if not rows:
            return None, None
        # 找最近一条真有 payload.dispatch_id 字段的 (有时即使 LIKE 匹配, 解析后该字段还是 None — 例如 chat text 提到 "dispatch_id" 关键词)
        for row in rows:
            frm, to, frm_role, kind, payload_str, _ts = row
            try:
                payload = json.loads(payload_str or "{}")
            except json.JSONDecodeError:
                continue
            did = payload.get("dispatch_id")
            if did:
                break
        else:
            return None, None
        # 推断 role
        role = "participant"
        if kind == "task_request" and frm == agent_id:
            role = "ceo"
        elif kind == "task_verdict" and frm == agent_id:
            role = "dispatcher"
        elif kind == "evaluate_request":
            role = "dispatcher" if frm == agent_id else "manager"
        elif kind == "verdict_reply" and frm == agent_id:
            role = "manager"
        elif kind == "command":
            role = "ceo" if frm == agent_id else "executor"
        elif kind == "report" and frm == agent_id:
            role = "executor"
        return did, role

    def list_dispatches_indexed(self, state_filter: str | None = None,
                                 since: float = 0, limit: int = 100) -> list[dict]:
        """agent-ceo 7 状态对齐版本 dispatch 索引.
        state filter: 'all' / 'active' (含 waiting_*/executing) / 'done' (含 done/rejected) / 单状态名.
        """
        cur = self.conn.execute(
            "SELECT id, ts, from_agent, to_agent, from_role, kind, payload "
            "FROM messages WHERE payload LIKE '%\"dispatch_id\"%' AND ts >= ? "
            "ORDER BY ts ASC",  # asc 让聚合时容易识别 first/last event
            (since,),
        )
        agg: dict[str, dict] = {}
        for r in cur.fetchall():
            mid, ts, frm, to, frm_role, kind, payload_str = r
            try:
                payload = json.loads(payload_str or "{}")
            except json.JSONDecodeError:
                continue
            did = payload.get("dispatch_id")
            if not did or not isinstance(did, str):
                continue
            if did not in agg:
                agg[did] = {
                    "dispatch_id": did,
                    "started_ts": ts,
                    "last_event_ts": ts,
                    # executor → executors set (multi-executor 支持), executor 字段保留兼容
                    "executors": set(),
                    "first_executor": None,
                    "executor": None,  # 兼容老 API, 输出时 = first_executor
                    "reports_from": set(),  # 哪个 agent 发了 report
                    "brief": None,
                    "parent_dispatch": None,
                    "events": [],  # 内部聚合用, 推断 state, 最后删
                }
            d = agg[did]
            d["last_event_ts"] = max(d["last_event_ts"], ts)
            d["events"].append({"kind": kind, "frm": frm, "to": to, "frm_role": frm_role,
                                 "payload": payload, "ts": ts})
            # 抽 brief (优先 task_request 的 task/text)
            if not d["brief"] and kind in ("task_request", "command"):
                t = payload.get("task") or payload.get("text") or payload.get("task_title") or ""
                if t:
                    d["brief"] = t[:60]
            # 抽 executors set (任何 command 的 to_agent 都是 executor)
            if kind == "command" and to:
                d["executors"].add(to)
                if not d["first_executor"]:
                    d["first_executor"] = to
                    d["executor"] = to  # 兼容字段
            # 记录 report from agent (用于 multi-executor 完成度)
            if kind == "report" and frm:
                d["reports_from"].add(frm)
            # 抽 parent_dispatch
            if not d["parent_dispatch"]:
                pd = payload.get("depends_on") or payload.get("parent_dispatch_id")
                if pd and isinstance(pd, str):
                    d["parent_dispatch"] = pd

        # 状态推断 + filter
        out = []
        for did, d in agg.items():
            events = d.pop("events")
            kinds = [e["kind"] for e in events]
            has_request = "task_request" in kinds
            has_verdict = "task_verdict" in kinds
            has_command = "command" in kinds
            has_report = "report" in kinds
            # 取最近 task_verdict 看 approve / conditional
            last_verdict_e = next((e for e in reversed(events) if e["kind"] == "task_verdict"), None)
            verdict_approve = None
            verdict_is_conditional = False
            if last_verdict_e:
                vp = last_verdict_e["payload"]
                verdict_approve = vp.get("approve")
                verdict_is_conditional = (
                    vp.get("verdict_kind") == "conditional"
                    or "must_have" in vp  # 含 must_have 字段视为待决策
                )

            # multi-executor state 推断
            exec_n = len(d["executors"])
            rep_done = len(d["reports_from"] & d["executors"]) if exec_n > 0 else 0
            # 推断 state (优先级从高到低)
            if exec_n > 0 and rep_done >= exec_n:
                state = "done"
            elif exec_n > 0 and rep_done > 0:
                state = "partial_done"
            elif has_report and exec_n == 0:
                # 兼容老 dispatch (无 executors set 推断, 直接看 report 字段)
                state = "done"
            elif has_command:
                state = "executing"
            elif has_verdict:
                if verdict_approve is False:
                    state = "rejected"
                elif verdict_is_conditional:
                    state = "waiting_user_decision"
                else:
                    state = "verdict_in"
            elif has_request:
                state = "waiting_verdict"
            else:
                state = "unknown"

            # awaiting_dependency 优先级覆盖 (依赖未 done 时强制 awaiting)
            if d["parent_dispatch"]:
                parent = agg.get(d["parent_dispatch"])
                if parent:
                    # 简单看 parent 是否有 report (递归判定 done 太复杂)
                    parent_kinds = [e["kind"] for e in parent.get("events", [])]
                    if "report" not in parent_kinds:
                        state = "awaiting_dependency"

            d["state"] = state
            # 输出 executors set + reports_from list (GUI 显完成度)
            d["executors"] = sorted(list(d["executors"]))
            d["reports_from"] = sorted(list(d["reports_from"]))
            d.pop("first_executor", None)  # 内部字段不输出, executor 兼容字段已设
            out.append(d)

        # filter
        if state_filter and state_filter != "all":
            if state_filter == "active":
                # partial_done 也算 active (还在等剩余 executor)
                active_set = {"waiting_verdict", "verdict_in", "waiting_user_decision",
                              "executing", "awaiting_dependency", "partial_done"}
                out = [d for d in out if d["state"] in active_set]
            elif state_filter == "done":
                out = [d for d in out if d["state"] in ("done", "rejected")]
            else:
                out = [d for d in out if d["state"] == state_filter]

        out.sort(key=lambda x: x["last_event_ts"], reverse=True)
        return out[:limit]

    def query_messages(self, agent_id: str | None = None,
                       since: float = 0, limit: int = 100,
                       kind: str | None = None) -> list[dict]:
        clauses = ["ts >= ?"]
        params: list = [since]
        if agent_id:
            clauses.append("(from_agent=? OR to_agent=?)")
            params.extend([agent_id, agent_id])
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        params.append(limit)
        sql = ("SELECT id, ts, from_agent, to_agent, from_role, to_role, kind, "
               "payload, parent_id, priority FROM messages WHERE " +
               " AND ".join(clauses) + " ORDER BY ts DESC LIMIT ?")
        cur = self.conn.execute(sql, tuple(params))
        out = []
        for r in cur.fetchall():
            # fail-safe: 旧脏数据 payload 含 raw control char (未 escape \n)
            # 用 strict=False 兼容; 仍失败 fallback {_payload_decode_error: <msg>}
            raw = r[7] or "{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                try:
                    payload = json.loads(raw, strict=False)
                except json.JSONDecodeError as e:
                    payload = {"_payload_decode_error": str(e), "_raw_excerpt": raw[:120]}
            out.append({
                "id": r[0], "ts": r[1], "from_agent": r[2], "to_agent": r[3],
                "from_role": r[4], "to_role": r[5], "kind": r[6],
                "payload": payload,
                "parent_id": r[8], "priority": r[9],
            })
        return out

    # ---- mini_task 小任务追踪 ----

    def insert_mini_task(self, payload: dict) -> dict:
        """UPSERT mini_task. payload 来自 transcript_parser.build_mini_task_payload.
        返回 {ok, dedup} — dedup=True 表示已存在 (按 mini_task_id), 不重写.
        如果新数据 ended_ts 比旧的更晚, 更新 (允许同 prompt 多次提交时取最新 reply).
        """
        mid = payload.get("mini_task_id") or ""
        if not mid:
            return {"ok": False, "error": "missing mini_task_id"}
        existing = self.conn.execute(
            "SELECT ended_ts FROM mini_tasks WHERE mini_task_id=?", (mid,)
        ).fetchone()
        actions_json = json.dumps(payload.get("actions") or [], ensure_ascii=False)
        if existing:
            old_ended = existing[0] or 0
            new_ended = float(payload.get("ended_ts") or 0)
            if new_ended <= old_ended:
                return {"ok": True, "dedup": True}
            # 更新 (新 cycle 数据更完整)
            self.conn.execute(
                "UPDATE mini_tasks SET request=?, actions_json=?, reply=?, "
                "ended_ts=?, duration_sec=?, tool_count=?, parent_dispatch_id=?, "
                "source=?, received_ts=? WHERE mini_task_id=?",
                (
                    payload.get("request") or "",
                    actions_json,
                    payload.get("reply") or "",
                    new_ended,
                    float(payload.get("duration_sec") or 0),
                    int(payload.get("tool_count") or 0),
                    payload.get("parent_dispatch_id"),
                    payload.get("_source") or "transcript_parser",
                    time.time(),
                    mid,
                ),
            )
            self.conn.commit()
            return {"ok": True, "dedup": False, "updated": True}
        # 新插入
        self.conn.execute(
            "INSERT INTO mini_tasks (mini_task_id, agent_id, request, actions_json, "
            "reply, started_ts, ended_ts, duration_sec, tool_count, "
            "parent_dispatch_id, source, received_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mid,
                payload.get("agent_id") or "",
                payload.get("request") or "",
                actions_json,
                payload.get("reply") or "",
                float(payload.get("started_ts") or 0),
                float(payload.get("ended_ts") or 0),
                float(payload.get("duration_sec") or 0),
                int(payload.get("tool_count") or 0),
                payload.get("parent_dispatch_id"),
                payload.get("_source") or "transcript_parser",
                time.time(),
            ),
        )
        self.conn.commit()
        return {"ok": True, "dedup": False, "inserted": True}

    def query_mini_tasks(self, agent_id: str | None = None,
                          since: float = 0, limit: int = 50,
                          parent_dispatch_id: str | None = None,
                          include_actions: bool = False) -> list[dict]:
        """列表查询 mini_task. 默认不返 actions_json (节省 payload), include_actions=True 返完整.
        排序: ended_ts DESC.
        """
        clauses = ["ended_ts >= ?"]
        params: list = [since]
        if agent_id:
            clauses.append("agent_id=?")
            params.append(agent_id)
        if parent_dispatch_id:
            clauses.append("parent_dispatch_id=?")
            params.append(parent_dispatch_id)
        params.append(limit)
        cols = ("mini_task_id, agent_id, request, "
                + ("actions_json, " if include_actions else "")
                + "reply, started_ts, ended_ts, duration_sec, tool_count, "
                "parent_dispatch_id, source, received_ts")
        sql = (f"SELECT {cols} FROM mini_tasks WHERE "
               + " AND ".join(clauses)
               + " ORDER BY ended_ts DESC LIMIT ?")
        cur = self.conn.execute(sql, tuple(params))
        out = []
        for r in cur.fetchall():
            i = 0
            d = {
                "mini_task_id": r[i], "agent_id": r[i+1],
                "request": r[i+2],
            }
            i += 3
            if include_actions:
                try:
                    d["actions"] = json.loads(r[i] or "[]")
                except json.JSONDecodeError:
                    d["actions"] = []
                i += 1
            d.update({
                "reply": r[i],
                "started_ts": r[i+1], "ended_ts": r[i+2],
                "duration_sec": r[i+3], "tool_count": r[i+4],
                "parent_dispatch_id": r[i+5],
                "source": r[i+6], "received_ts": r[i+7],
            })
            out.append(d)
        return out

    def get_mini_task(self, mini_task_id: str) -> dict | None:
        """单条详情, 含 actions 解析后的 list."""
        cur = self.conn.execute(
            "SELECT mini_task_id, agent_id, request, actions_json, reply, "
            "started_ts, ended_ts, duration_sec, tool_count, parent_dispatch_id, "
            "source, received_ts FROM mini_tasks WHERE mini_task_id=?",
            (mini_task_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        try:
            actions = json.loads(r[3] or "[]")
        except json.JSONDecodeError:
            actions = []
        return {
            "mini_task_id": r[0], "agent_id": r[1], "request": r[2],
            "actions": actions, "reply": r[4],
            "started_ts": r[5], "ended_ts": r[6],
            "duration_sec": r[7], "tool_count": r[8],
            "parent_dispatch_id": r[9], "source": r[10],
            "received_ts": r[11],
        }

    # ===== bus_tokens (multi-token RBAC) =====

    def insert_bus_token(self, token_hash: str, label: str, role: str,
                         scopes: list, agent_id: Optional[str] = None,
                         expires_ts: Optional[float] = None,
                         metadata: Optional[dict] = None) -> bool:
        """写一行 token 记录. raw token 不入库, 调用方传 sha256 hash.
        返 True 成功, False 重名 (label 已存在)."""
        try:
            self.conn.execute(
                "INSERT INTO bus_tokens (token_hash, label, role, scopes, "
                "agent_id, created_ts, expires_ts, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token_hash, label, role, json.dumps(scopes),
                 agent_id, time.time(), expires_ts,
                 json.dumps(metadata or {})),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_bus_token_by_hash(self, token_hash: str) -> Optional[dict]:
        """按 hash 查 token. 含 revoked / expired 检查由调用方做."""
        cur = self.conn.execute(
            "SELECT token_hash, label, role, scopes, agent_id, created_ts, "
            "expires_ts, last_used_ts, revoked_ts, metadata "
            "FROM bus_tokens WHERE token_hash=?",
            (token_hash,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "token_hash": r[0], "label": r[1], "role": r[2],
            "scopes": json.loads(r[3] or "[]"),
            "agent_id": r[4],
            "created_ts": r[5], "expires_ts": r[6],
            "last_used_ts": r[7], "revoked_ts": r[8],
            "metadata": json.loads(r[9] or "{}"),
        }

    def list_bus_tokens(self, include_revoked: bool = False) -> list[dict]:
        """列出所有 token (脱敏: 不返 token_hash, 仅 metadata)."""
        sql = ("SELECT label, role, scopes, agent_id, created_ts, "
               "expires_ts, last_used_ts, revoked_ts FROM bus_tokens")
        if not include_revoked:
            sql += " WHERE revoked_ts IS NULL"
        sql += " ORDER BY created_ts DESC"
        rows = []
        for r in self.conn.execute(sql).fetchall():
            rows.append({
                "label": r[0], "role": r[1],
                "scopes": json.loads(r[2] or "[]"),
                "agent_id": r[3],
                "created_ts": r[4], "expires_ts": r[5],
                "last_used_ts": r[6], "revoked_ts": r[7],
            })
        return rows

    def revoke_bus_token(self, label: str) -> bool:
        """软删: 设 revoked_ts. 返 True 命中, False 未找到."""
        cur = self.conn.execute(
            "UPDATE bus_tokens SET revoked_ts=? WHERE label=? AND revoked_ts IS NULL",
            (time.time(), label),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def update_bus_token_agent_id(self, label: str, agent_id: Optional[str]) -> bool:
        """改 agent_id binding. 给 mcp-default 类升级到 node-prefix 用.
        sqlite IS NOT 已覆盖 NULL vs 非NULL 比较.
        返 True 实际改了行, False 没找到 / 已撤 / 值没变."""
        cur = self.conn.execute(
            "UPDATE bus_tokens SET agent_id=? "
            "WHERE label=? AND revoked_ts IS NULL AND agent_id IS NOT ?",
            (agent_id, label, agent_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def touch_bus_token(self, token_hash: str):
        """更新 last_used_ts. 容错: 失败不抛."""
        try:
            self.conn.execute(
                "UPDATE bus_tokens SET last_used_ts=? WHERE token_hash=?",
                (time.time(), token_hash),
            )
            self.conn.commit()
        except sqlite3.Error:
            pass

    def count_active_bus_tokens(self) -> int:
        """活跃 token 数 (未撤 + 未过期). bootstrap 判断用."""
        now = time.time()
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM bus_tokens "
            "WHERE revoked_ts IS NULL AND (expires_ts IS NULL OR expires_ts > ?)",
            (now,),
        )
        return cur.fetchone()[0]
