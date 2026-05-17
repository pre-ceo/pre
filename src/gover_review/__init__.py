"""gover_review — 周期审查 governor ask 历史并产出规则改进 proposal.

模块布局:
  extract.py  抽取窗内 ask 条目 + 打包 Layer A 上下文 (jsonl 邻居 + transcript)
  reviewer.py 调 codex -p 输出 proposal JSON (target_layer ∈ {B, C})
  state.py    cycle 状态机 + pending 锁
  reporter.py INFO finding 写入 + 用户回答 watcher + 改进报告生成
"""
