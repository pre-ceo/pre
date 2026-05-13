# pre 项目 agent 规则 — 自维护授权

此文件是 pre 平台自己的 agent (`local.cli-claude-code-local.pre`) 在 cwd
`$PRE_ROOT` 下的项目级规则, 由 governor 加载. 目的: 授权
pre agent 对自己负责的总线组件 (master / node / cron / ui 静态服务) 进行
**生命周期自维护** (start / stop / restart / status / logs), 无需 ASK.

## 自维护允许 (ALLOW)

下列操作属于平台 agent 的份内职责, **直接允许**:

### 1. 通过统一脚本 (推荐路径)

- `bash scripts/bus_ctl.sh start|stop|restart|status|logs|attach [master|node|cron|ui|all]`
- `bash $PRE_ROOT/scripts/bus_ctl.sh ...` (绝对路径同等)

### 2. 单组件直拉 (脚本坏了 / 调试时用)

- `python3 scripts/start_master.py [args]`
- `python3 scripts/start_node.py [args]`
- `python3 scripts/api_server.py [args]`
- `uv run python scripts/start_master.py [args]` (同上)

### 3. tmux 会话管理 (仅 pre-* 前缀, 不放任意 tmux kill)

- `tmux kill-session -t pre-master`
- `tmux kill-session -t pre-node`
- `tmux kill-session -t pre-cron`
- `tmux kill-session -t preui-static`
- `tmux new-session -d -s pre-master ...` / 同类 pre-* / preui-* session 创建
- `tmux capture-pane -t pre-*`, `tmux has-session -t pre-*` (现有白名单已覆盖)

### 4. 进程级清理 (bus_ctl.sh 卡死 / 进程僵尸时)

- `pkill -f "scripts/start_master\.py"`
- `pkill -f "scripts/start_node\.py"`
- `pkill -f "scripts/api_server\.py"`

仅限完整匹配脚本路径; 不允许 `pkill python3` / `pkill -f master` 这种宽匹配
(会误杀同名进程).

### 5. 诊断

- `lsof -nP -iTCP -sTCP:LISTEN | grep 19500` (master 端口探测)
- `lsof -i :19500` / `lsof -i :19501` (master / cron 端口)
- `netstat -an | grep 19500` / `ss -an | grep 19500`
- `curl -sS http://127.0.0.1:19500/healthz` (公开端点, 不要 token)

## 仍要 ASK (拒绝下放)

- `rm -rf ~/.pre/data/` — master 数据库不可单方面清, 用户决策
- `rm` 任何 `~/.pre/env` / `~/.pre/data/initial_tokens.txt` — token 单点
- `git push --force` / `git reset --hard` / `git clean -f` — git 破坏性默认黑名单
- `pkill -9 -f python` / `killall python` — 宽匹配
- 修改 `~/.pre/env` 内容 / 写 token 到任何文件

## 边界 (跨 repo)

- `pre_ui` / `pre_rule` 仓库的服务 (`fe_ctl.sh` 之类) 不是 pre agent 的份内事,
  通过 bus 派单给对应 agent — 不在此文授权范围.
- master 的代码 (`src/master/server.py`) 改动算正常项目内改动, 由其他规则
  覆盖, 不在自维护范畴.

## 备注

此处 "自维护" 仅授权 **拉起 / 杀掉 / 看日志** 这种生命周期动作, 不含 master
配置变更 (端口 / token / role / capability 配置), 后者需 ASK 用户.
