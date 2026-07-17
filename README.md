# mini-code-agent-langgraph

> **A compact, security-first LangGraph coding agent with verified patches, crash recovery, and signed undo.**

一个面向学习、审计与扩展的安全优先 LangGraph 编程 Agent：既能执行一次性编码任务，也能在持续会话中聊天、读代码、修改文件和运行验证。它是单进程的**行式 CLI / REPL**，不是全屏 TUI；“compact”描述部署与理解成本，不代表它拥有极小的第三方依赖栈。

[![tests](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml/badge.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml)
[![Python 3.10–3.13 tested](https://img.shields.io/badge/Python_tested-3.10%E2%80%933.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[安全策略](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [更新记录](CHANGELOG.md)

## 30 秒体验（无需 API Key）

```bash
git clone https://github.com/wusuiling-if/mini-code-agent-langgraph.git
cd mini-code-agent-langgraph
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
mca demo
```

`mca demo` 在系统临时目录创建并修复一个确定性的 calculator fixture；它不会修改当前 clone，也不会连接模型服务。准备在真实仓库运行前，可先执行只读诊断：

```bash
mca doctor --cwd /path/to/repo --sandbox auto --provider auto
```

这条无 Key 诊断会把 provider 记为 warning 而不是失败。`mca demo` 当前支持 macOS、Linux 与 WSL2；原生 Windows 请使用 WSL2/Linux 环境运行完整 Agent。

`doctor` 只静态检查 sandbox 可执行文件是否在 PATH；真正的 backend/daemon/image 可用性仍由 `run` 和 `chat` 启动时的 probe 决定。

## 安全与可靠性边界

| 控制 | Runtime 强制行为 | 边界 |
| --- | --- | --- |
| 只读聊天 | `/ask` 只允许列目录、搜索、读文件和查看 diff；新增工具不会自动获得权限 | `/code` 是用户显式授予的编码能力，不代表模型输出一定正确 |
| 验证门 | 修改后只有当前工作区指纹对应的权威测试通过，才允许 `submit` | 测试命令和测试覆盖率由用户负责配置 |
| 崩溃恢复 | run/chat 从完整工具边界恢复，并使恢复前的验证结果失效 | 被强制终止的外部命令可能已经产生部分副作用 |
| 签名撤销 | 私有 Undo journal 以 HMAC 绑定轨迹、工作区、路径和内容 hash，并在覆盖前检查冲突 | 签名证明本机 journal 完整性，不证明修改在语义上安全 |
| Fail-closed 隔离 | `auto` 实际探测后端；没有可用后端时拒绝执行命令，除非用户显式选择 `none` | macOS 使用 `sandbox-exec`，Linux 使用 `bwrap` 或 Docker；原生 Windows 的完整 Agent runtime 尚不支持 |
| 进程清理 | 超时、Ctrl-C、SIGTERM 和异常后回收命令进程组及本次 Docker 容器 | 宿主内核、Docker daemon 或依赖链失陷不在保证范围内 |

这些机制是纵深防御，不是绝对安全沙箱。不要把不受信任的仓库与生产凭证放在同一工作区，也不要未经检查运行仓库自带的构建或测试命令；完整威胁模型见 [SECURITY.md](SECURITY.md)。

项目重点展示并约束一个最小 coding-agent 闭环：

```text
观察工作区 → 选择工具 → 执行动作 → 获取反馈 → 验证修改 → 提交或继续
```

- 结构化文件工具和默认关闭的任意 shell
- LangGraph Agent Loop
- 可恢复的 run/chat checkpoint、trajectory、diff 与可冲突检测的 undo
- `/ask` 只读聊天和 `/code` 编码授权模式
- macOS `sandbox-exec`、Linux `bwrap`，以及 macOS/Linux/WSL2 的 Docker 后端
- OpenAI 与 DeepSeek 独立 provider 配置

## 它是什么界面

当前是两种终端界面：`mca run` 是一次性 CLI，`mca chat` 是行式交互 REPL。它不是 curses/全屏 TUI，也没有 Web UI；因此通过 SSH、普通 Terminal 和 CI 都能使用，部署时不需要浏览器或前端服务。

## 部署前提

在本机长期使用至少需要：

- Python 3.10+ 和项目虚拟环境
- DeepSeek 或 OpenAI API Key；只跑 `--model mock` 不需要 Key
- 明确可用的隔离后端：macOS `sandbox-exec`、Linux `bwrap` 或 Docker；如果显式使用 `--sandbox none`，只应指向无凭证、可丢弃的可信目录
- 原生 Windows 仅验证 `help`、`version`、`doctor` 和配置路径；`run`、`chat`、`demo` 及结构化文件工具请在 WSL2/Linux 中运行
- 一个由用户预先配置的权威测试命令，例如 `python3 -m pytest -q`
- 可写的用户状态目录；默认目录和权限见“运行状态与轨迹”

`git` 不是非 Git 项目的硬依赖，但 Git 仓库中的 dirty 检查和 diff 需要系统可信路径里的 `git`。

## 安装

需要 Python 3.10 或更高版本；CI 当前覆盖 3.10–3.13。推荐使用独立虚拟环境。

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
pytest -q
```

Windows PowerShell：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
mca --version
mca --help
mca doctor --sandbox none
```

该原生 Windows 安装只承诺信息诊断命令；完整 Agent runtime 请从 WSL2/Linux 环境运行。

## 无密钥本地验证

`mca demo` 使用 Mock 模型在新建的系统临时目录测试完整一次性 Agent Loop，并保留目录供检查：

```bash
mca demo
```

Demo 不需要 API Key，也不会修改仓库中的 `examples/calculator_bug`。它内部对确定性 fixture 显式使用无隔离执行，因此只适合作为本地演示；真实 `run` / `chat` 仍保持 fail-closed 沙箱默认值。Demo 依赖 POSIX 命令执行，支持 macOS、Linux 和 WSL2，不支持原生 Windows。

## 配置密钥

默认在用户配置目录创建私有 `0600` env 文件：

```bash
mca init
```

之后 `mca run` 和 `mca chat` 会自动加载这个默认文件；只有使用其他位置时才需要传 `--env-file`。

默认路径：

| 平台 | 配置文件 |
| --- | --- |
| macOS | `~/Library/Application Support/mini-code-agent/env` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/mini-code-agent/env` |
| Windows | `%APPDATA%\mini-code-agent\env` |

也可指定项目外的路径：

```bash
mca init --path ~/.config/mca.env
```

在 POSIX 系统上，`--env-file` 必须是普通非符号链接文件，且 group/other 不能拥有任何权限；否则 CLI 会在读取前拒绝运行。修复命令：

```bash
chmod 600 ~/.config/mca.env
```

Windows 的 POSIX mode 位不能完整表达 ACL，必要时还应通过文件属性或 ACL 限制其他账户访问。

## Provider 与模型

### DeepSeek

```dotenv
DEEPSEEK_API_KEY=...
# DEEPSEEK_BASE_URL=https://api.deepseek.com
```

```bash
mca run "检查并修复失败测试" \
  --cwd /path/to/repo \
  --model deepseek \
  --env-file ~/.config/mca.env \
  --test-command "python3 -m pytest"
```

别名：

- `deepseek` / `deepseek-flash` → `deepseek-v4-flash`
- `deepseek-pro` → `deepseek-v4-pro`

DeepSeek 使用专用 `ChatDeepSeek` adapter，而不是把第三方 API 当成 OpenAI。适配器会保留并在工具调用后回传 `reasoning_content`。为获得稳定、成本可预测的工具循环，thinking 默认显式关闭；需要时传入：

```bash
mca chat --model deepseek --deepseek-thinking --env-file ~/.config/mca.env
```

### OpenAI / OpenAI-compatible

```dotenv
OPENAI_API_KEY=...
# OPENAI_BASE_URL=https://api.openai.com/v1
```

```bash
mca chat \
  --model gpt-4.1-mini \
  --provider openai \
  --env-file ~/.config/mca.env
```

本地兼容服务即使不验证密钥，也必须显式设置一个占位值，避免配置错误被推迟到请求阶段：

```dotenv
MCA_API_KEY=not-needed
MCA_BASE_URL=http://127.0.0.1:8000/v1
```

`--provider auto` 根据 DeepSeek 别名或 `deepseek-*` 模型名选择 DeepSeek，其余选择 OpenAI。使用网关且模型名无法代表 provider 时，请显式传 `--provider deepseek` 或 `--provider openai`。

环境变量不会跨 provider 误用。优先级如下：

| Provider | API Key | Base URL |
| --- | --- | --- |
| DeepSeek | `DEEPSEEK_API_KEY` → `MCA_API_KEY` | `--base-url` → `DEEPSEEK_BASE_URL` → `DEEPSEEK_API_BASE` → `MCA_BASE_URL` → 官方地址 |
| OpenAI | `OPENAI_API_KEY` → `MCA_API_KEY` | `--base-url` → `OPENAI_BASE_URL` → `MCA_BASE_URL` → SDK 默认地址 |

密钥缺失会在 Agent 启动前报错。LLM 请求默认超时 60 秒并重试 2 次，可用 `--request-timeout` 和 `--max-retries` 调整；工具命令超时则由独立的 `--timeout` 控制。

## 聊天 + 编码

```bash
mca chat \
  --cwd /path/to/repo \
  --model deepseek \
  --env-file ~/.config/mca.env \
  --test-command "python3 -m pytest"
```

会话默认进入 `/ask`：允许列目录、搜索、读文件和查看 diff，但运行 shell、测试或写文件会被 runtime 强制阻止，而不只是依赖提示词。

```text
/ask              切换到只读聊天
/ask 解释这段代码  切换并立即提问
/code             显式允许编码工具，仍遵守逐次确认
/code 修复测试     切换并立即执行任务
/clear            清除对话上下文
/exit             保存并退出
```

`--yes` 会跳过 `/code` 模式中的写入和命令确认；它不会自动把会话从 `/ask` 切换到 `/code`。

恢复上一次聊天：

```bash
mca chat --resume /path/to/session.chat.json \
  --model deepseek \
  --env-file ~/.config/mca.env
```

恢复后默认重新进入 `/ask`，不会静默继承 `/code` 授权。

## 运行状态与轨迹

不传 `--output` 时，trajectory 不再写入目标仓库，而是写入用户私有状态目录的 `runs/` 子目录。目录在 POSIX 系统上设置为 `0700`；文件名包含 UTC 微秒时间和随机后缀，例如：

```text
20260714T031245.123456Z-a1b2c3d4.traj.json
```

| 平台 | 状态根目录 |
| --- | --- |
| macOS | `~/Library/Application Support/mini-code-agent/state` |
| Linux | `${XDG_STATE_HOME:-~/.local/state}/mini-code-agent` |
| Windows | `%LOCALAPPDATA%\mini-code-agent` |

可用 `MCA_STATE_DIR` 和 `MCA_CONFIG_DIR` 覆盖默认位置。新建任务的显式 `--output` 和默认输出都使用独占创建；已有文件会报错。只有显式 `--resume` 会原子更新原 checkpoint，或通过新的 `--output` 另存。

CLI 在结束时打印真实 trajectory 路径：

```bash
mca trace /path/to/run.traj.json --diff
mca undo /path/to/run.traj.json --dry-run
mca undo /path/to/run.traj.json
```

未完成的一次性任务可从最近一个完整工具边界继续：

```bash
mca run --resume /path/to/run.traj.json \
  --model deepseek \
  --env-file ~/.config/mca.env \
  --test-command "python3 -m pytest -q" \
  --max-steps 80
```

`--max-steps` 是累计模型调用上限；如果原任务已经达到旧上限，恢复时应调高。恢复不会信任 trajectory 中旧的“测试通过”状态，而会要求对当前工作区重新运行权威测试。进程若恰好在 shell 命令执行中被强制杀死，命令可能已有部分副作用；恢复会从上一个完整工具边界继续并使旧验证失效。

Undo 原始恢复内容保存在状态根目录的私有 `undo/` 中，使用 `0600` 文件和本机密钥的 HMAC-SHA256 绑定 trajectory、工作区、路径及前后 hash。Trajectory 仍可能包含源代码、提示、读取结果和命令输出；脱敏只是纵深防御，任何 trajectory 都应按敏感文件处理，不能直接公开分享。撤销前会检查文件是否在 Agent 修改后再次变化；发生冲突时默认拒绝覆盖，只有明确接受风险时才使用 `--force`。

0.1/0.2 的 unsigned undo 数据默认拒绝写文件。确实检查并信任旧文件后，才可显式使用 `--allow-legacy-unsafe`。

## 工具与安全边界

| 工具 | 作用 |
| --- | --- |
| `list_files` | 列出工作区文件 |
| `search_files` | 搜索工作区文本 |
| `read_file` | 读取文件或行范围 |
| `write_file` | 写入文件 |
| `apply_patch` | 精确文本替换 |
| `replace_lines` | 按行替换 |
| `run_tests` | 运行用户配置的验证命令 |
| `git_diff` | 查看变更 |
| `submit` | 结束任务 |
| `bash` | 任意 shell 逃生口，默认禁用 |

默认策略：

- `--cwd` 必须是目录，文件工具限制在其真实路径范围内
- 新建 run/chat 时 dirty Git 工作区默认拒绝启动；`--allow-dirty` 会关闭这项保护，resume 前也必须自行检查并暂存额外改动
- shell 默认关闭，测试命令和子进程使用收敛后的环境
- shell/test 子进程在超时、Ctrl-C、SIGTERM 和异常后回收整个进程组；Docker 运行使用唯一 name/cidfile 并在退出时强制清理
- `mca run` 即使没有检测到文件变化，也必须至少通过一次用户配置的权威测试才能提交
- 工作区指纹覆盖内容、文件类型、权限位、symlink target、依赖目录及 Git 本地配置/hooks；缓存目录和易变 Git 数据库除外
- 模型不能覆盖 `--test-command`；失败的权威测试、改变工作区指纹的后续操作以及 resume 会使旧验证失效
- `/ask` 使用只读工具允许列表；未来新增工具不会被默认放行
- 工具输出、搜索、结构化编辑、tool-call 数量、持久对话和 `reasoning_content` 都有资源上限；状态文件读写共享 256 MiB 硬上限，可用 `--context-chars` 调整上下文预算
- 启动时实际探测沙箱能力；`auto` 会按顺序尝试本机后端，某个后端存在但不可运行时继续尝试下一个，全部失败才拒绝启动
- `--max-steps`、命令超时、请求超时必须大于零

沙箱可用性依赖操作系统：

- macOS：优先尝试系统 `sandbox-exec`（系统可能弃用或限制它）
- Linux：优先尝试 `bwrap`
- macOS / Linux：安装并启动 Docker，并预先拉取沙箱镜像后可选择 `--sandbox docker`；默认镜像是 `python:3.11-slim`，可用 `--docker-image` 或 `MCA_DOCKER_IMAGE` 指向带目标项目依赖的预构建镜像，运行时不会隐式拉镜像
- 原生 Windows：`0.3.x` 不支持完整的 Agent runtime；请在 WSL2/Linux 中使用上述隔离后端
- 没有可用后端时，只有显式 `--sandbox none` 才允许不隔离执行

沙箱、路径检查和脱敏都不是运行不可信仓库的绝对安全边界。不要让 Agent 在包含生产凭证、SSH 私钥或不应被模型读取的数据目录中运行。

## 项目结构

```text
src/mini_code_agent/agent.py       LangGraph Agent Loop
src/mini_code_agent/chat.py        持续聊天会话
src/mini_code_agent/model.py       工具声明与 provider adapter
src/mini_code_agent/executor.py    Tool Runtime、审批和沙箱
src/mini_code_agent/trajectory.py  Trace、Diff 和 Undo
src/mini_code_agent/security.py    路径与密钥安全
src/mini_code_agent/cli.py         CLI、状态目录与授权模式
```

## 测试

```bash
pytest -q
python -m pip check
python -m evals.run_evals --json
```

`evals/run_evals.py` 是无需 API Key 的确定性行为基线，覆盖单文件修复、无需修改的解释任务，以及失败修改后的恢复。它输出 JSON 指标，包括成功率、验证率、步骤、工具调用数、耗时和无关改动数；任一 case 失败时返回非零退出码。

这是一个安全优先的 reference runtime，而不是 Codex 或 Claude Code 的替代品。发布与贡献约定分别见 [CHANGELOG.md](CHANGELOG.md) 和 [CONTRIBUTING.md](CONTRIBUTING.md)。
