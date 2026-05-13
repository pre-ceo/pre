# install.sh 一站式 bootstrap (pre_rule 内置创建 + pre_ui clone + MCP 注册)

## 动机

开源初始化后, 新用户的体验是: 自己依次 `git clone pre`, `git clone pre_rule`, `git clone pre_ui`, 跑 `install.sh` 装 shim, 手动改 `~/.claude.json` 加 mcpServers, 还要看 README 拼 `PRE_MCP_SECRET` 等 env. 步骤多, 跨仓库, 容易漏一步.

改造目标: `git clone pre.git && cd pre && bash scripts/install.sh` 一步把除 master/node 启动外的全部初始化完成.

副带要点:
- `pre_rule` 不再是独立仓库, 由 install.sh 从 `pre/templates/pre_rule/` 模板创建. 用户可在创建后自己 `git init` 私仓 sync 多机.
- `pre_rule` 内部新拆出 **system 层** (install 强制更新, 不建议改) 与 **global 层** (首次创建, 用户可改), 让平台层 prompt 框架升级不踩用户策略.

## 方案

### 1. pre_rule 文件分层

| 文件 | 层 | install 行为 | 内容 |
|---|---|---|---|
| `system.md` | system | 强制更新 (内容不同 backup 旧的) | governor 的 prompt 固定外壳 — 输入字段顺序, response format (第一行 ALLOW/ASK/DENY, 第二行 reason), 拼接 `global.md` + project rules 的指令, 不可妥协的安全底线 (rm -rf / curl\|sh / reverse shell / DROP DB 等绝对危险) |
| `global.md` | global | 不存在则创建 (从 template), 存在则保留 | 用户可调整的策略集 — npm 供应链判定细节 / ssh sudo allowlist / workspace scope policy / 项目特定 ASK 规则 |
| `system_analyze.md` | system | 强制更新 | analyzer prompt 固定外壳 — STOP_REASON / NEXT_ACTION 字段格式, freerun no-user-interaction 底线, anti-loop detection |
| `global_analyze.md` | global | 首次创建 | priority ladder, finding levels 模板, 用户对特定 agent 类型的 next-step 偏好 |
| `spawn.rc` | global | 首次创建 | sys_claude / sys_gemini tmux 启动 rc, 用户自定义代理 / IP 校验 |
| `config.json` | global | 首次创建 | `mode` / `governor_provider` / `governor_timeout` 等本机偏好 |
| `.gitignore` | system | 强制更新 | runtime state ignore 模板 |
| `README.md` | system | 强制更新 | pre_rule 自身说明 |
| `LICENSE` | system | 强制更新 | MIT |

governor 拼 prompt 顺序: `system.md` (强制) → `global.md` (强制) → `{project}/pre/rules.md` (可选). 老布局 (没有 `system.md`, 只有 `global.md`) 时 `system.md` 内容缺失, 但保持向后兼容: governor 只要任一规则文件存在就工作.

### 2. install.sh 流程扩展

现有 install.sh 已做: 路径解析, `~/.pre/env` 写入, shim 装到 `~/.local/bin`, PATH 提示. 新增段落 (插在已有 path 解析后, shim 装入前):

- **A. pre_rule 内容初始化** — 调 `python3 scripts/install_pre_rule.py "$PRE_RULE_ROOT"`. 该脚本:
  - 不存在 → 创建目录, 复制 `pre/templates/pre_rule/*` 全部文件.
  - 存在 → 按表格 system/global 类别处理: system 类内容 hash 不同就 backup 旧 (`.bak.<ts>`) + 覆盖并打印 diff 摘要; global 类已存在直接跳过.
- **B. pre_ui sibling clone** — 探测 `<PRE_PARENT>/pre_ui` 目录:
  - 存在 → 跳过 (尊重用户当前状态).
  - 不存在 → 推断 url 后 clone:
    1. 命令行 `--pre-ui-url=<url>` 优先,
    2. 否则 `git -C "$PRE_ROOT" remote get-url origin` 拿到 pre 的 remote, 用正则把仓名换成 `pre_ui` 推断 (`pre.git` → `pre_ui.git`, 兼容 ssh `git@github.com:org/pre.git` 与 https `https://github.com/org/pre.git`),
    3. 推断成功 → `git clone "$PRE_UI_URL" "<PRE_PARENT>/pre_ui"`, 失败 (网络/无权限/repo 未创建) 不 fatal, 只警告.
    4. pre 没有 origin (本地未 push) → 跳过, 提示 `pre 仓库无 origin remote, 跳过 pre_ui clone. push pre 后重跑 install.sh, 或 --pre-ui-url=<url> 显式指定`.
  - `--no-pre-ui` flag 显式跳过.
- **C. ~/.claude.json MCP 注册** — 调 `python3 scripts/install_mcp_registration.py`. 该脚本:
  - 读 `~/.claude.json` (不存在则建空 `{}`).
  - 检查 `mcpServers.pre` 块. 不存在 → merge (command/args/env 用 `$PRE_ROOT` 实际值).
  - 存在但与模板差异显著 → backup `~/.claude.json.bak.<ts>` + 覆盖, 打印 diff 摘要.
  - 一致 → 跳过.
- **D. 报告 + 提示**:
  - 当前的 "✓ pre installed" 段保留, 新增 pre_rule / pre_ui / MCP 注册的状态行.
  - 末尾提示: `下一步: bash $PRE_ROOT/scripts/bus_ctl.sh start && python3 $PRE_ROOT/scripts/pre_init.py /your/project` (启动 master/node + 把项目接入 hook).

### 3. governor / analyzer 改动

- `src/governor.py:37` — 改读两个文件并拼接:
  ```python
  system_rules = _load_file(os.path.join(rules_dir, "system.md")) if rules_dir else ""
  global_rules = _load_file(os.path.join(rules_dir, "global.md")) if rules_dir else ""
  ```
  prompt parts 顺序: `[GOVERNANCE] + Input` → `RECENT CONVERSATION` → `SYSTEM RULES` (强制部分) → `GLOBAL RULES` (用户可改) → `AGENT-SPECIFIC RULES` → format requirement.
- `src/analyzer.py:43` — 同理加 `system_analyze.md` 读取 + 拼接. system 段在 global 段前.
- 两边都保持: 单文件存在时仍工作 (向后兼容).

### 4. README 改动

- 删除 "git clone pre_rule" 行, 改成: pre_rule 由 `install.sh` 创建.
- 删除手动改 `~/.claude.json` 段, 改成 install.sh 自动注册.
- Quick start 简化为:
  ```bash
  export PRE_DIR=$HOME/your-path/pre
  git clone https://github.com/pre-ceo/pre.git "$PRE_DIR"
  cd "$PRE_DIR"
  bash scripts/install.sh           # 一站式: pre_rule + pre_ui + MCP + shim + PATH
  pre bus start                     # 起 master + 本机 node
  pre init /path/to/your-project    # 给项目装 hook
  ```
- 添加 "pre_rule 文件分层" 章节: system vs global 边界, 怎么编辑 global, 升级 pre 时 system 怎么自动同步.

### 5. 不在本次范围

- bus_ctl.sh 启动 master/node — 仍保留为显式步骤 (启动是 runtime 操作, install 是文件层).
- token bootstrap — `initial_tokens.txt` 在 master 首次启动时由 master.db 生成, install.sh 不参与.
- 跨机器 sync — 用户自行 `cd $PRE_RULE_ROOT && git init && git remote add origin <private repo>`.

## 实施步骤

1. 创建 `pre/templates/pre_rule/` 模板目录, 9 个文件:
   - `system.md` (新): 从现有 `pre_rule/global.md` 抽出格式约束 + 绝对安全底线
   - `global.md`: 现有 `pre_rule/global.md` 剩余可调策略
   - `system_analyze.md` (新): 从 `pre_rule/global_analyze.md` 抽出格式约束 + freerun no-user 底线
   - `global_analyze.md`: 现有剩余 priority ladder / finding levels
   - `spawn.rc`: 复制现有
   - `config.json`: minimal `{"mode": "enforce", "governor_provider": "gemini", "governor_timeout": 60}`
   - `.gitignore`: 复制现有
   - `README.md`: 重写, 体现新的 system/global 分层
   - `LICENSE`: MIT
2. 写 `scripts/install_pre_rule.py` — 处理创建/补写/系统文件强制更新逻辑.
3. 写 `scripts/install_mcp_registration.py` — 处理 `~/.claude.json` merge.
4. 改 `scripts/install.sh` — 加上 pre_rule init + pre_ui clone + MCP 注册三段.
5. 改 `src/governor.py` 拼 `system.md` + `global.md`.
6. 改 `src/analyzer.py` 拼 `system_analyze.md` + `global_analyze.md`.
7. 改 `pre/README.md`: 删除 clone pre_rule 步骤, 加 install.sh 一站式说明.
8. 验证: 在 `/tmp/pre_install_test/` 模拟全新机器跑一遍 install.sh, 检查 pre_rule 全部创建 + MCP 注册成功. 再跑第二次 install.sh, 检查 system 文件不重复 backup, global 文件保留, MCP 注册幂等.

## 验证

- **全新机器路径**: `PRE_RULE_ROOT=/tmp/pr_test bash scripts/install.sh -y` 后, `/tmp/pr_test/` 含 9 文件, `~/.claude.json` 有 `mcpServers.pre` 段.
- **二次 install 幂等**: 改 `/tmp/pr_test/global.md` 加一行, 重跑 install.sh, 该行仍在; `system.md` 若被改, 提示 diff + 强制覆盖 (有 `.bak`).
- **governor 仍工作**: 跑非白名单 Bash 触发 governor, 验证 SYSTEM RULES + GLOBAL RULES 都在 prompt 里 (打 debug 日志一次确认).
- **老布局兼容**: 临时把 `pre_rule/system.md` 移走, 再跑 governor, 只读 `global.md` 仍工作.

## 相关文件

| 文件 | 角色 |
|---|---|
| `scripts/install.sh` | 一站式 bootstrap 入口, 加 pre_rule/pre_ui/MCP 三段 |
| `scripts/install_pre_rule.py` (新) | 复制 templates + system/global 分层处理 |
| `scripts/install_mcp_registration.py` (新) | `~/.claude.json` merge |
| `templates/pre_rule/*` (新, 9 文件) | pre_rule 初始模板 |
| `src/governor.py` | 拼 system.md + global.md |
| `src/analyzer.py` | 拼 system_analyze.md + global_analyze.md |
| `README.md` | 删除 clone pre_rule + 手动 MCP 段 |

## 防再次踩坑

- 升级 pre 时, system 类模板文件若变了, install.sh 必须自动覆盖用户 pre_rule 里对应文件 — 不然 prompt 框架升级会失效. 已加 hash 对比 + backup 机制.
- 模板里**不要**包含本机特化路径 / 真实 account / 真实 IP. 所有占位用 `your-github-org` / `<host>` / `127.0.0.1` / `example.com`.
- `~/.claude.json` 合并时只动 `mcpServers.pre` 段, 不动其他 mcpServers / claudeCode settings / theme 等.
- pre_ui clone 失败不要 fatal: 用户可能在内网 / 无网络 / 故意不要 GUI.
