# Governor — Global Policy (yours to edit)

> This file is the **global layer**. `install.sh` creates it once from the
> template and never overwrites it again — your edits stick. The system
> layer (`system.md`) handles the output contract and the absolute safety
> floor. Put operator- and machine-level policy here. Put project-specific
> rules in `<project>/pre/rules.md`.

## Default Policy

ALLOW everything unless it matches a category below.

## Danger Categories → ASK (escalate to human)

### Destructive Operations

- `rm -rf`, `rm -f` with broad targets
- `git reset --hard`, `git push --force`, `git clean -f`
- `DROP TABLE`, `DROP DATABASE`, `TRUNCATE`
- `kill -9`, `killall`
- `mkfs`, `dd if=` (block-device variants are DENY via the system floor)

### Credential / Secret Exposure

- Reading `.env`, `.credentials`, private keys, `*_SECRET`, `*_TOKEN` files
- Reading `/etc/passwd`, `/etc/shadow`, SSH keys
- Commands that output secrets to stdout or pipe to external services

### npm Supply Chain Security

For `npm install`, `npm add`, `npx`, `yarn add`, `pnpm add` commands —
perform a supply chain assessment first:

1. **Version** — is a specific version pinned? `^` or `~` means version drift
   risk.
2. **Popularity** — well-known package from a major org (express, lodash,
   react, typescript = HIGH; unknown single-maintainer = LOW).
3. **Typosquat** — does the package name look like a misspelling of a popular
   one (`expresss`, `lod-ash`, `reacr`)?
4. **Scope** — is it scoped to a known org (`@angular/`, `@babel/`,
   `@types/`)? Scoped is safer.

Decision based on verdict:

- **SAFE** (high popularity + no typosquat + version pinned, or a well-known
  tool) → **ALLOW**
- **CAUTION** (unlocked version on popular package, or unknown package) →
  **ASK**
- **DANGER** (typosquat suspect, unknown package from unknown author) →
  **ASK**

Exceptions that are always ALLOW:

- `npx tsc`, `npx tsx`, `npx eslint`, `npx prettier` (standard dev tools, no
  install).
- `npm run <script>`, `npm test`, `npm start` (runs existing scripts, no
  install).
- `npm ls`, `npm list`, `npm outdated`, `npm audit` (read-only queries).

Reason format: `[npm audit] <package> — popularity: HIGH/MED/LOW, typosquat:
NO/POSSIBLE, scope: YES/NO — verdict: SAFE/CAUTION/DANGER. <brief>`

### Network Risk

- `npm publish`, `pip upload`, `docker push` (publishing)
- HTTP POST/PUT with tokens or credentials in the command

> `curl|sh` and reverse shells are handled by the absolute floor in
> `system.md` (DENY, not ASK). Keep them off the list here.

### SSH Remote Commands

SSH 远程命令 (`ssh host "cmd"`, `ssh user@host cmd`) 按**远端命令的实际语义**
判定, 不因为 sudo 出现在远端命令里就一律 ASK.

**ALLOW (read / query, even with sudo):**

- `sudo pm2 list/show/logs`, `sudo systemctl status/show`
- `sudo journalctl …` (查日志, 不带 `-k`/`-f` 删除选项)
- `sudo cat <log/conf>`, `sudo tail`, `sudo head`, `sudo grep`, `sudo less`
- `sudo ls`, `sudo find`, `sudo du`, `sudo df`, `sudo ps`
- `sudo netstat`, `sudo ss`, `sudo ip a`, `sudo iptables -L`
- `pm2 status/show/logs`, `docker ps`, `docker logs`, `docker inspect`

**ASK (mutating / restart / destructive):**

- `sudo systemctl restart/start/stop/reload/disable/enable`
- `sudo pm2 restart/stop/delete`
- `sudo apt install/remove/upgrade`, `sudo yum install/remove`
- `sudo rm`, `sudo mv`, `sudo cp` (修改文件)
- `sudo chmod`, `sudo chown`
- `sudo iptables -A/-D/-F/-I` (修改防火墙)
- `sudo reboot`, `sudo shutdown`
- 任何远端写文件 / 改配置 / 重启服务 / 部署的命令

判定原则: **看远端命令做什么, 不看是否有 sudo**. 读取查询类即使带 sudo 也是
只读, 直接 ALLOW.

### Workspace Scope Policy

All projects live under a shared workspace parent directory (the parent of
CWD's parent). For example, if CWD is `/workspace/cursor/project-a`, the
workspace root is `/workspace/cursor/`.

**READ across projects: ALLOW**

- Any agent may freely read files from sibling projects under the same
  workspace root.
- Example: agent in `cursor/project-a` reading `cursor/project-b/src/config.ts`
  → ALLOW.

**WRITE is project-isolated: only own CWD**

- An agent may ONLY write/edit files within its own CWD (or its
  subdirectories).
- Writing to a sibling project → **ASK**. Cross-project writes should be
  reviewed.
- Example: agent in `cursor/project-a` writing to
  `cursor/project-b/package.json` → ASK.

**Outside workspace: ASK**

- Reading or writing files completely outside the workspace root → **ASK**.
- Exception: `~/.claude/` (config files) → ALLOW for read and write.
- Exception: `~/` (home directory dotfiles) → ALLOW for read only.

### Irreversible Production Operations

- Database migrations on production
- Deployment commands (`deploy`, `kubectl apply` to prod)
- Package version bumps + publish in one step
