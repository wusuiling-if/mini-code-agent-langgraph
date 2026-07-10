# mini-code-agent-langgraph

一个用 LangGraph 写的极简 AI 编程 Agent。这个项目不是代码补全工具，而是一个可观察、可测试、可撤销的 coding agent 原型，用来展示：

- Agent Loop：模型循环决策
- Tool Use：模型调用结构化工具读文件、改代码、跑测试
- Context / Trace：完整保存运行轨迹，方便复盘失败
- Safety：默认禁用任意 shell，限制文件访问范围，支持沙箱
- Undo：基于 trajectory 撤销结构化编辑

## 架构

```text
用户任务
  ↓
CLI
  ↓
LangGraph StateGraph
  ↓
LLM 生成 tool call
  ↓
Tool Runtime 执行工具
  ↓
Observation 回传给 LLM
  ↓
循环直到 submit
```

核心路径：

```text
src/mini_code_agent/agent.py       Agent Loop
src/mini_code_agent/model.py       工具声明 + 模型接入
src/mini_code_agent/executor.py    Tool Runtime / 安全策略 / 沙箱
src/mini_code_agent/trajectory.py  Trace / Diff / Undo
src/mini_code_agent/security.py    路径边界 + 密钥脱敏
```

## 快速开始

先跑 mock 模型，不需要 API key：

```bash
cd mini-code-agent-langgraph
PYTHONPATH=src python3 -m mini_code_agent run "修复失败测试" \
  --cwd examples/calculator_bug \
  --model mock \
  --yes \
  --allow-dirty
```

查看轨迹：

```bash
PYTHONPATH=src python3 -m mini_code_agent trace runs/latest.traj.json --diff
```

撤销结构化编辑：

```bash
PYTHONPATH=src python3 -m mini_code_agent undo runs/latest.traj.json --dry-run
```

## 使用真实模型

OpenAI-compatible 模型：

```bash
export OPENAI_API_KEY=...
PYTHONPATH=src python3 -m mini_code_agent run "修复失败测试" \
  --cwd /path/to/repo \
  --model gpt-4.1-mini \
  --test-command "python3 -m pytest" \
  --yes
```

DeepSeek：

```bash
export DEEPSEEK_API_KEY=...
PYTHONPATH=src python3 -m mini_code_agent run "修复失败测试" \
  --cwd /path/to/repo \
  --model deepseek \
  --test-command "python3 -m pytest" \
  --yes
```

也可以生成本地环境变量文件：

```bash
PYTHONPATH=src python3 -m mini_code_agent init
PYTHONPATH=src python3 -m mini_code_agent run "检查项目结构" \
  --cwd /path/to/repo \
  --model deepseek \
  --env-file .env.local
```

## 工具系统

模型可以调用这些工具：

| 工具 | 作用 |
| --- | --- |
| `list_files` | 列出工作区文件 |
| `search_files` | 搜索文本文件 |
| `read_file` | 读取工作区内文件 |
| `write_file` | 写入工作区内文件 |
| `apply_patch` | 精确文本替换，并返回 diff |
| `replace_lines` | 按行号替换，并返回 diff |
| `run_tests` | 运行用户配置的测试命令 |
| `git_diff` | 查看 git diff |
| `submit` | 结束任务 |
| `bash` | 高风险逃生口，默认禁用 |

默认情况下，模型不能随便执行 shell。`bash` 只有在传入 `--allow-shell` 后才可用。

## 安全设计

这个项目的默认策略是“少给能力，而不是猜所有危险命令”：

- 文件工具只能访问 `--cwd` 内部
- 任意 bash 默认禁用
- 自定义测试命令默认禁用，需要 `--allow-shell`
- 可用时用 `sandbox-exec`、`bwrap` 或 Docker 包住 shell/test 命令
- 常见 API key/token 会在 observation 和 trajectory 中脱敏
- dirty git worktree 默认拒绝运行，避免覆盖用户已有改动
- 每次运行记录 `workspace_changes`
- 结构化编辑会保存 before/after，用于 undo

注意：这不是完美安全边界。不要在敏感仓库里公开 trajectory，因为它可能包含代码片段和编辑前后的文件内容。

## Trajectory

Trajectory 是 Agent 的完整黑匣子，记录：

- 用户任务
- sandbox 状态
- 每轮模型输出
- 每个 tool call
- 每个 observation
- 文件 diff
- workspace 变化
- 最终提交摘要

这让你可以分析 Agent 为什么成功、为什么失败、哪一步跑偏。

## 和成熟 AI 编程工具的差异

这个项目不是为了替代 Codex 或 Claude Code。它更像一个“AI 编程工具解剖台”：

| 维度 | mini-code-agent-langgraph | Codex / Claude Code |
| --- | --- | --- |
| 目标 | 学习和实验 Agent 机制 | 真实生产力工具 |
| 上下文工程 | 很轻量 | 更成熟 |
| 工具系统 | 结构化但少量 | 更丰富 |
| 安全 | 默认最小权限 + 简单沙箱 | 更完整的权限和隔离 |
| 可观察性 | trajectory JSON + trace CLI | 产品化 UI |
| 可撤销 | 支持结构化编辑 undo | 更完整的编辑体验 |

这个项目重点展示 coding agent 的底层机制：Agent Loop、Tool Use、Trace、Sandbox、Undo、Evaluation。

## 测试

```bash
PYTHONPATH=src pytest -q
```

当前覆盖：

- 工具路径边界
- 密钥脱敏
- bash 默认禁用
- 危险命令拦截
- 测试命令限制
- apply_patch / replace_lines / write_file diff
- mock agent 修复失败测试
- trace 命令
- undo 恢复
- 非交互 CLI 错误提示

## 项目状态

这是一个研究型原型，适合继续扩展：

- Trace Viewer Web UI
- 更强 context packing
- benchmark/evaluation runner
- Docker 多语言沙箱
- MCP 工具接入
- 多 agent / subagent 实验

