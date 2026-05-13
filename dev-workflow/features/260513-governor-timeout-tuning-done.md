# Governor timeout tuning for slow LLM providers

## 问题征兆

PreToolUse hook 对非白名单 Bash 命令把决策转给 governor (LLM subprocess) 判 ALLOW/ASK. 当 governor 子进程在默认 30s 内未返回, hook 决策回退 `ask`, agent UI 显示:

```
Hook PreToolUse:Bash requires confirmation for this command:
governor timeout after 30s
```

每次非白名单 Bash 都重复出现, agent 实际不能自动决策, 沦为每次手动 confirm. 表象像 hook 完全停止工作.

## 根因

- `src/governor.py` 跑 LLM CLI subprocess (claude -p / gemini -p 等) 评估工具调用
- 默认 timeout 取自 `pre_rule/config.json` 的 `governor_timeout` 字段, 缺省 30s
- 部分 LLM provider CLI 实际响应时间 > 30s, 原因任一:
  - 后端 LLM 当前响应慢 (provider 服务质量)
  - 走 HTTP/HTTPS 代理增加延迟
  - CLI cold start 慢 (history / config 加载)
  - 系统 prompt + 项目 rules.md 拼接后 prompt 较长, LLM 处理耗时
- 30s 超时后 subprocess raise `TimeoutExpired`, hook 返 `ask` + reason "governor timeout after 30s"

不是代码 bug, 是 timeout 配置跟 LLM 实际响应分布 mismatch.

## 决策链定位

PreToolUse hook 走多级决策 (`src/rules.py` + `src/hook.py`):

1. 黑名单 (rm -rf 等危险模式) → ask
2. 供应链 / 内联模式 → 强制 governor 不缓存
3. 白名单 (git/ls/echo 等安全前缀) → allow, **不调 governor**
4. 缓存 → 复用上次 governor verdict
5. governor (LLM subprocess) → 读 rules.md 决策

仅走到第 5 步才有 timeout 风险. 白名单 / 黑名单 / 命中缓存的命令不受影响.

## 方案

### Quick fix (本次采用)

加大 `pre_rule/config.json` 的 `governor_timeout`, 给 LLM provider 充足响应时间:

```json
{
  "governor_timeout": 60
}
```

`src/config.py` 的 `load_config()` 读该值, 传给 `governor.query_governor(timeout=...)`. 改完下次 hook 触发自动读新值 — 无需重启 hook (每次 hook invocation 都重新 load_config).

UX trade-off: 危险 Bash 命令最坏情况等 60s 才 LLM 决策完, 但 hook 不再 always timeout.

### 备选方案 (按情况选)

- **切换 `governor_provider`** — 不同 LLM CLI, 但各 provider 后端响应分布相近, 不保证显著快
- **把工具型 / 短期 agent 的 mode 改成 `observe`** (在 `{cwd}/pre/agent_config.json` 里) — 不调 governor, 只记录 + 危险命令 ask. 适合 audit / analysis-only 类 agent, 工具调用本身安全, governor 评估冗余
- **长期**: subprocess CLI → 直接 HTTP API call (省 CLI cold start), 或缩短 prompt (减 LLM 处理时间)

## 验证

修改后跑非白名单 Bash 穿过 hook:

- 之前: 30s timeout → ask
- 之后: LLM 在 ~10s 量级返 ALLOW → allow

实际响应时间随 LLM provider 当前负载浮动, 60s 给足 buffer.

## 相关文件

| 文件 | 角色 |
|---|---|
| `pre_rule/config.json` | user 可调的 `governor_timeout` 值 |
| `src/config.py` | `load_config()` 读 timeout |
| `src/governor.py` | subprocess 调 LLM CLI, 接 timeout 参数 |
| `src/hook.py` | PreToolUse 决策链入口 |
| `src/rules.py` | 黑/白名单 (短路 governor) |
| `{cwd}/pre/agent_config.json` | per-agent mode (observe 跳过 governor) |

## 防再次踩坑

- 加新 LLM provider 时, benchmark 一遍真实场景响应时间, 跟 `governor_timeout` 对齐
- 系统 prompt + rules.md 加长前评估对 LLM 响应时间影响
- 监控 governor timeout 率 (`pre_rule/logs/pre_hook_*.jsonl` 含 decision/reason), 持续 timeout 应升级 timeout 或切方案
