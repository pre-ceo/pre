# FE UI first-launch deploy chain fix

新机器跑 `bash scripts/install.sh && pre bus start` 后, 用户期望浏览器开 fe ui 立即看 transcript stream. 实际撞 5 道墙, 本轮把这条 chain 打通.

## 问题征兆 (按用户体验顺序)

1. `pre install` 后 `~/.pre/env` 只有路径, **没有任何 PRE_*_SECRET** — 用户不知道 fe ui 要用什么 token.
2. fe ui 打开后无 token 输入位置/拿不到, 调 master 全 401.
3. 用户在 settings 手动粘贴 token → 调 transcript SSE → **403 `default_deny_no_match`** (read_pane_capability check fail).
4. capability fix 后 → **404 `transcript_not_found`** (marker `transcript_path.txt` 不存在).
5. agent 跑了一次工具, marker 出现, fe ui 短暂工作 → 重新坏 → 调研发现 hook 在 `cfg.mode=observe` 下根本不写 marker, mode=enforce 才写.
6. (顺带) 用户在 pre repo 自己跑 `pre init` 把 pre 当 agent → 生成 `pre/next.md` → working tree dirty → `pre update` 拒绝 pull.

## 根因链 (从末端到根)

| 层 | 现象 | 根因 |
|---|---|---|
| `transcript_path.txt` 不存在 | hook 没持久化 transcript path | `src/hook.py:38` 只在 `cfg.mode == "enforce"` 写 marker; 默认 `observe` 跑了不写 |
| `default_deny_no_match` | sse-ticket 颁发被 ACL 拒 | `pre_rule/hook/read_pane_capability.json` 不存在; fallback `default: deny + 空 allow` → 全拒 |
| 即使存在 也对不上 | template 里的 caller hash 静态写死 | hash 是 `sha256("Bearer <raw>")[:12]`, 每机器 bootstrap 的 raw 不同, 静态模板永远 mismatch |
| `PRE_GUI_SECRET` 缺 | 老 bootstrap 时代码只 issue 4 token | start_master.py `_bootstrap_tokens` 是 first-start-only (db 空才跑), 后来加的 gui/hook 永不补 |
| 即使代码改了 也不补 env | bootstrap 完后只 write_secrets_to_env(issued), 已 issued 但 env 缺的不动 | env write 跟 issue 同 run 绑死, 升级路径 (db 已有 token / env 还缺) 不覆盖 |
| Magic link 看不到 | fe ui 没机制接收一次性 URL token | fe ui 入口不解析 URL fragment `#token=<raw>` |
| `pre update` 拒 dirty | `.gitignore` 不全, init 生成的 `pre/next.md` 入 working tree | 历史 .gitignore 只排除 `.pre/` `.claude/` 等, 没排除 init 在 cwd `pre/` 子目录里生成的 per-project state |

## 方案 (顺序对应 5 commits)

### 1. `8e40e01` add `pre repair` + `pre update` subcommands

`pre init` 遇到现有 hook / agent_config 是 skipped/conflict, 无 idempotent 修复路径. 加两条命令:
- `pre repair [cwd]` — 强制重写 cwd 的 `agent_config.json` (preserve 用户字段, 强更 driver 字段) + `.claude/settings.json` 的 PreToolUse + Stop hook (保留其他 hook). 不动 rules.md / next.md / pointer.
- `pre update` — `git pull --ff-only` pre + pre_ui (refuse dirty/non-ff), `uv sync`, `pre bus restart`.

### 2. `94f1fc1` auto-issue gui-default + bootstrap magic link

改 `start_master.py:_bootstrap_tokens` 从 first-start-only 改 idempotent (缺哪个 default label 补哪个), 新颁发 token 自动 append `~/.pre/env` (if-not-set). gui-default 新颁发时 stderr 输出一次性 magic link:

```
http://127.0.0.1:5174/index.html#token=<raw>&next=/
```

token 走 URL fragment (#后), 浏览器只读不发 server, 无 server log 残留. `pre_token.py rotate --label gui-default` 同样输出 link, 提供失去 token 的 fail-safe.

跨仓 dispatch 给 pre_ui agent 在 fe ui 入口加 ~30 行 JS — 读 fragment, `A.setToken(raw)` 落 localStorage, `history.replaceState` 清掉 hash, 跳 `next`.

### 3. `7968e4b` two upgrade-path fixes

- **`.gitignore`** 加 6 行排除 init/runtime 在 cwd `pre/` 下生成的 per-project state (`next.md`, `.done`, `.next_action`, `transcript_path.txt`, `findings/`, `reports/`). 历史 tracked 的 `pre/agent_config.json` + `pre/rules.md` 不动.
- **`start_master.py:_sync_env_from_initial_tokens`** — bootstrap 后无条件跑, 扫 `initial_tokens.txt` 全部 label, 缺的 `PRE_<KIND>_SECRET` 补到 env. 解决 "db 已 bootstrap 过但 env 从未 sync" 的升级路径; magic link 也会在 synced gui 时显示.

### 4. `234025e` auto-sync read_pane_capability.json

`pre_rule/hook/read_pane_capability.json` 是 master 在 ticket 颁发 / read_pane 时查的 ACL, fallback `default: deny + 空 allow`. install_pre_rule.py 不 provision (无 hook/ template 子目录), 即使 provision 模板里的 hash 也对不上 fresh 机器 bootstrap 的 raw.

加 `_sync_capability_from_initial_tokens` — master 启动算 6 个 default label 的 `sha256("Bearer <raw>")[:12]`, 同步到 capability.json allow (文件不存在就建). idempotent + additive, 不删用户加的条目.

### 5. (本轮) `pre repair` 默认 promote `pre_rule/config.json` mode → enforce

老 install (template 当时是 `observe`) 装的 config.json 是 observe, install_pre_rule.py 把 config.json 当 global (已存在保留), 升级不覆盖 → hook 跑了不写 marker → fe ui transcript_not_found.

`pre_repair.py` 加 `_repair_pre_rule_mode`: 默认跑时检查 `pre_rule/config.json` mode, != enforce 就改 enforce (backup 老的). 加 `--no-rule-mode` flag opt-out.

不动 template (已是 enforce, 新 install 没问题). 不动其他 user 自定义 config 字段.

## 新机器 / 老机器升级操作流

**新机器** (fresh install):
```
git clone <pre> && bash pre/scripts/install.sh
pre bus start
  → bootstrap 颁发 6 default token
  → env sync 自动 append PRE_*_SECRET
  → capability sync 写 read_pane_capability.json
  → stderr 显示 magic link
浏览器开 magic link → fe ui 自动激活 → transcript stream 直接可用 (mode=enforce by template)
```

**老机器升级**:
```
pre update                # 拉新代码 + bus restart
  → bootstrap idempotent skip (db 已有 6 token)
  → env sync 补缺的 PRE_<KIND>_SECRET (含 PRE_GUI_SECRET)
  → capability sync 补缺的 default caller hash (含 gui-default)
  → magic link 显示 (因 synced 含 PRE_GUI_SECRET)
浏览器开 magic link → fe ui 激活
cd <agent-cwd> && pre repair    # 把 pre_rule mode 改 enforce
agent 跑一次工具 → hook 触发 → transcript_path.txt 写 → fe ui transcript 可见
```

## 关键 invariants

- token raw 永远不进 git tracked 文件 (本仓 .gitignore + 全局 CLAUDE.md redact 双层防御)
- magic link 走 URL fragment, server log / proxy 看不到
- env / capability sync 都是 if-key-not-set / additive — 用户手编不被覆盖
- mode `enforce` ↔ `observe` 是 user-facing 设计选择, `pre repair` 显式 promote (不是 silent override)

## 已知 limitations / followup

- fe ui 浏览器 localStorage 还是手动 token 输入流程的 superset — magic link 只在 fresh 激活时帮一次, localStorage 清后需要 `pre_token.py rotate --label gui-default` 重生 link
- `pre_rule/config.json` 是 global 而非 cwd-specific, `pre repair` 改 mode 实际影响所有 agent (不只是当前 cwd 这一个) — 跟 repair 的 "局部修复" semantics 略 mismatch, 但用户 explicit OK
- agent_config.json + rules.md 历史 tracked 在 pre repo 自身的 `pre/` 子目录, 没移到 .gitignore (向后兼容). 长期可考虑 `git rm --cached` + ignore, 留 followup
- magic link UI 流是 pre_ui v1, 没 chord 短码 / QR 码等替代 UX, 桌面浏览器单机使用没必要扩展
