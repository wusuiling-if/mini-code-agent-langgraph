# 记忆评测记录

本目录保存经过脱敏、带源码指纹和整份记录 SHA-256 的版本化评测快照。

记录不包含API Key、认证密钥、原始记忆正文、模型完整响应或本机私有状态路径。在线模型报告
只保留模型、调用次数、各方案准确率和失败用例名。

生成当前快照：

```bash
.venv/bin/python -m evals.save_memory_report \
  --regression-summary "429 passed, 2 skipped" \
  --recorded-at 2026-08-18T00:00:00+08:00 \
  --intervention-report evals/results/2026-08-18-memory-natural-transfer-deepseek-v3.json \
  --conversation-report evals/results/2026-08-18-memory-long-conversation-deepseek-v1.json \
  --output evals/results/2026-08-18-memory-evaluation-v13.json
```

校验记录本身及其绑定的评测源码：

```bash
.venv/bin/python -m evals.save_memory_report \
  --verify evals/results/2026-08-18-memory-evaluation-v13.json
```

v13 绑定 120-session 长期对话读取、真实模型三路对照、泛化 cue 拒答修复和 429 项回归；
v12 绑定作用域优先候选缩减、批量状态解析、纵向延迟复测和 427 项回归；
v11 绑定可选 OpenAI-compatible embedding backend、私有持久缓存、崩溃恢复、失败回退、
portability 评测和 426 项回归；v10 绑定宿主无关核心、MCA 适配器、预算/容量/秘密策略、稳定项目身份、
检索审计、portability 评测和 417 项回归；v9 绑定原检索器生产 opt-in 读写闭环、真实补丁经验形成和 406 项回归；v8 绑定无手写记忆和
预灌反馈的自然经验迁移报告；v7 绑定复杂三文件 DeepSeek 9 次介入报告；
v6 绑定简单任务的真实模型三路报告；
v5 新增确定性生产 Agent 闭环介入 A/B；
v4 绑定结果感知控制、反记忆、带证据反馈和影子策略隔离。v3 绑定并发安全准入、幂等证据追加
和版本替代修复；v2 是修复
前的确定性形成记录；带
DeepSeek 单次实测摘要的 v1 文件也作为历史记录保留。旧记录的源码指纹不再对应当前实现。

只要评测逻辑、记忆模型、存储或检索源码发生变化，旧记录的源码指纹校验就会失败。此时应重新
运行全部评测并生成新日期或新版本的记录，不应直接覆盖历史结论。
