# dev-workflow/findings/

gover_review agent (周期审查 governor ask 历史) 写改进报告的归档目录.

每个 cycle 完成时 agent 在这里写一个文件: `YYMMDD-cycle-N.md`, 含:

- 用户决断 summary (accept / reject / modify / skip / no_answer 计数)
- 每个 proposal 详情 + 待 apply patch 草案
- 落地 checklist (accept/modify 项, 标 target_layer C → src/rules.py 或 B → rules.md)

用户根据 checklist 手动 apply patch — agent 不自动改 rules.py / rules.md.

设计文档: `../features/260517-gover-review-done.md`.
