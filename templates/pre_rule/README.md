# pre_rule — User-Editable Rules + Local Runtime State for [pre](https://github.com/your-github-org/pre)

[中文](#中文) · [English](#english) · [License: MIT](LICENSE)

> This directory was created by `pre/scripts/install.sh`. It is **not** a
> separate upstream repository — it is generated from `pre/templates/pre_rule/`.
> You can `git init` it yourself if you want to sync rule changes across
> machines via a private remote; pre does not push anything for you.

## English

`pre_rule/` is a sibling of the `pre` code repo. It holds two things:

1. **Rules** — text prompts fed into the governor (PreToolUse) and analyzer
   (Stop). Layered into a *system* layer (managed by install.sh) and a
   *global* layer (yours to edit).
2. **Runtime state** — logs, per-agent caches, cron schedules, local
   secrets. None of it is version-controlled (see `.gitignore`).

### File layout

| File | Layer | install.sh behavior | What goes here |
|---|---|---|---|
| `system.md` | system | overwritten every install | Governor output contract + absolute safety floor. **Do not edit** — your changes will be reverted. |
| `global.md` | global | created once, never replaced | Operator-level governor policy you actually tune (npm supply-chain detail, ssh sudo allowlist, workspace scope, …). |
| `system_analyze.md` | system | overwritten every install | Analyzer output contract + hard rules (anti-loop, no-user in autonomous). |
| `global_analyze.md` | global | created once | Analyzer style guide (priority ladder, finding levels). |
| `spawn.rc` | global | created once | Bash sourced before each `sys_claude`/`sys_gemini`/`sys_codex` tmux session — proxy env, egress checks. |
| `config.json` | global | created once | `mode` / `governor_provider` / `governor_timeout`. |
| `.gitignore` | system | overwritten every install | Runtime state ignore patterns. |
| `README.md` / `LICENSE` | system | overwritten every install | Project metadata. |

### Files that exist at runtime (git-ignored)

| Path | Purpose |
|---|---|
| `agents/<host>-<project>/` | Per-agent runtime cache + project-level `agent_config.json` overrides. |
| `logs/` | PreToolUse decision log (jsonl). |
| `runtime/` | Master/node registry, persistence. |
| `freerun/`, `cron/`, `hook/` | Mode-specific runtime state. |
| `notify_config.json` | Webhook secrets / group ids — **chmod 600**. |
| `.env_sync_secret` | Per-node HMAC sync secret. |
| `master.db` | Master SQLite (token table, etc). |

### How pre finds these rules

`pre/src/common/paths.py` resolves the rule root in this order:

1. `$PRE_RULE_ROOT` (written into `~/.pre/env` by `install.sh`).
2. `<pre repo>/../pre_rule` (sibling fallback).

The governor concatenates the prompt as: `system.md` → `global.md` →
`<project>/pre/rules.md`. The analyzer does the same with the
`*_analyze.md` pair plus `<project>/pre/analyze_rules.md`.

### Editing

- Edit `global.md` / `global_analyze.md` freely — re-running `install.sh`
  will not touch them.
- Do **not** edit `system.md` / `system_analyze.md`. If you have a strong
  reason, open a PR upstream against `pre/templates/pre_rule/`.
- Want to sync rule changes across machines? `git init` this directory
  yourself, push to a private remote, `git pull` on the other host.

### Recovering from a bad edit

`install.sh` does not restore deleted global files automatically (they are
yours, after all). If you delete one and want the template back, the
fastest path is:

```bash
rm global.md                  # or whichever you broke
bash <pre>/scripts/install.sh # re-creates missing global files
```

---

<a id="中文"></a>
## 中文

`pre_rule/` 是 `pre` 代码仓的 sibling 目录, 由 `install.sh` 从
`pre/templates/pre_rule/` 创建. 装两类东西:

1. **规则** — governor (PreToolUse) 与 analyzer (Stop) 的 prompt 文本.
   分 *system* 层 (install 管, 不动用户) 和 *global* 层 (用户改).
2. **运行时状态** — 日志 / agent 缓存 / cron 调度 / 本机 secret.
   全部 git-ignore.

### 文件分层

| 文件 | 层 | install.sh 行为 | 内容 |
|---|---|---|---|
| `system.md` | system | 每次 install 强制覆盖 | governor 输出合约 + 绝对安全底线. **不要改**, 改了会被回滚 |
| `global.md` | global | 首次创建, 之后保留 | 你实际要调整的策略 (npm 供应链细则 / ssh sudo allowlist / workspace scope) |
| `system_analyze.md` | system | 强制覆盖 | analyzer 输出合约 + 硬规则 (anti-loop / autonomous 无人) |
| `global_analyze.md` | global | 首次创建 | analyzer 风格 (priority ladder / finding levels) |
| `spawn.rc` | global | 首次创建 | sys_* tmux session 启动前 source 的 bash (代理 / 出口校验) |
| `config.json` | global | 首次创建 | `mode` / `governor_provider` / `governor_timeout` |
| `.gitignore` | system | 强制覆盖 | runtime state 忽略列表 |
| `README.md` / `LICENSE` | system | 强制覆盖 | 元数据 |

### 运行时文件 (git-ignore)

| 路径 | 用途 |
|---|---|
| `agents/<host>-<project>/` | 每个 agent 的运行时缓存 + 项目级 `agent_config.json` |
| `logs/` | PreToolUse 决策日志 (jsonl) |
| `runtime/` | master / node 注册表 |
| `freerun/`, `cron/`, `hook/` | mode-specific runtime |
| `notify_config.json` | webhook 凭证 — **chmod 600** |
| `.env_sync_secret` | 节点同步 HMAC secret |
| `master.db` | master SQLite |

### pre 怎么定位规则

`pre/src/common/paths.py` 顺序:

1. `$PRE_RULE_ROOT` (install.sh 写入 `~/.pre/env`)
2. `<pre repo>/../pre_rule` (sibling fallback)

Governor 拼接顺序: `system.md` → `global.md` → `<project>/pre/rules.md`.
Analyzer 同理用 `*_analyze.md` 对加 `<project>/pre/analyze_rules.md`.

### 编辑

- 随便改 `global.md` / `global_analyze.md`, install.sh 不会动它们.
- 不要改 `system.md` / `system_analyze.md`. 真有意见去
  `pre/templates/pre_rule/` 改了开 PR.
- 多机 sync 自己 `git init` 这个目录, 推私仓, 别的机器 `git pull`.

### 恢复模板

`install.sh` 不会还原被你删的 global 文件 (毕竟是你的). 想拿回模板:

```bash
rm global.md                  # 或其它被改坏的
bash <pre>/scripts/install.sh # 缺失的 global 文件会重建
```
