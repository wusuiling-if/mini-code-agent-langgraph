# Transactional Agent Runtime

[English](README.md)

> **Agent 补丁验证并确认可提交之前，不污染源工作区。**

Coding Agent 通常直接修改开发者正在使用的 checkout。失败的运行、过期的测试结果、并发修改或进程中断，都可能让源代码停在含义不明的中间状态。Transactional Agent Runtime 为 Agent 创建隔离 Git worktree，并把补丁处理为 prepare/commit 事务。

Runtime 只有在验证结果绑定到当前工作区、prepare 证据通过认证、且 commit 时源工作区仍无冲突后，才会更新源 checkout。LangGraph 是随附的 Agent Loop 适配器，不是事务内核。

[![tests](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml/badge.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml)
[![Python 3.10–3.13 tested](https://img.shields.io/badge/Python_tested-3.10%E2%80%933.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[安全策略](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [更新记录](CHANGELOG.md)

## 先看事务（无需 API Key）

```bash
python -m pip install mini-code-agent-langgraph
mca tx demo
```

Demo 会运行两个确定性事务：第一个证明验证后的补丁在 commit 前不会修改源工作区；第二个注入并发修改，并证明 commit 会拒绝覆盖。Linux、macOS 和 Windows 输出相同的行式机器可读结果。

## 核心保证

- **prepare 不污染源仓库：** Agent 工具和验证都在隔离 worktree 运行。
- **验证绑定：** 通过的 checks 绑定到精确的 prepare 工作区指纹；之后的任何变化都会使其失效。
- **篡改可检测：** 本机 HMAC receipt 绑定 baseline、补丁、验证、trajectory、访问日志和工作区指纹。
- **冲突即拒绝：** 源 `HEAD` 或工作区改变、prepare 工作区改变、补丁不匹配时，commit 均 fail closed。

## 明确限制

- 事务要求干净的 Git worktree，私有运行状态必须在源仓库之外。
- 当前按整个工作区检测冲突；即使无关的并发修改也会拒绝，而不会自动合并。
- Receipt 是本机篡改证据，不是可移植签名，也不证明测试完整或补丁正确。
- 原生 Windows 没有内置隔离后端；`--sandbox auto` 需要 Docker，`--sandbox none` 是显式关闭隔离。
- commit 前的短暂检查不是覆盖所有外部写入者的文件系统级锁。

## 在真实仓库运行

```bash
mca tx run "Fix the failing tests" \
  --cwd /path/to/clean/git/repo \
  --model deepseek \
  --memory local \
  --check tests "pytest -q"

mca tx status TRANSACTION_ID
mca tx receipt TRANSACTION_ID
mca tx commit TRANSACTION_ID
```

事务生命周期、receipt 字段、恢复和失败行为见 [事务协议](docs/transaction-protocol.md)。Provider、doctor、Agent Loop 和 sandbox 是次级集成，操作细节见 [Runtime Operations](docs/runtime-operations.md) 与 [Sandboxing](docs/sandboxing.md)。

## 显式启用的记忆基础

项目现已包含一套证据约束本地记忆基础：不可变记忆卡片、
追加式时态状态、经过认证的证据与关系、SQLite FTS 检索，以及只读的
`mca memory` 命令。默认情况下，`run`、`chat` 和 `tx` 都不会读取或写入记忆，
因此现有行为和必需依赖保持不变。事务可显式设置 `--memory local`；成功 commit 后，
Runtime 会确定性保存经过认证的验证流程和 receipt 绑定的真实补丁经验，但不会持久化验证
命令正文。下一次同 workspace 的显式事务会直接通过原证据时态检索器读取并注入有限上下文；
结果感知控制器因真实模型迁移测试退化而只保留为实验组件。

宿主无关的 `memory_core` 已与 MCA 适配器分离：普通 CI receipt 和自定义 Agent 可以实现相同
协议；MCA 只负责 transaction、Git 项目身份和 advisory 注入。上下文有 16K 硬预算，结构化
检索审计不复制正文，项目移动后身份稳定，旧 repair 会按容量追加标记为 stale。私有 trajectory
为支持 resume 仍保留实际注入的有限上下文。检索先在数据库层缩到当前作用域和 global，随后
验签卡片与最新状态；其他项目的历史不再参与当前项目排序。

泛用对话层还包含可重放的语义变更流、规范化 Checkpoint、提交后验收报告、尽量无损的
SillyTavern 聊天适配器，以及不执行代码的酒馆助手导入预览。JavaScript 与远程模块加载器
始终隔离；脚本数据、角色变量和聊天变量只会形成待做 Schema 映射的无权限候选，不会自动
变成长期事实。核心身份与偏好不会因容量自动淘汰，情节/瞬时噪声仍可压缩或退休；新会话会
为连续性事实保留有限预算，核心事实装不下时显式报告，而不是悄悄漏掉。详见
[泛用对话记忆](docs/conversation-memory.md)。

可选 embedding backend 支持任意 OpenAI-compatible endpoint：可以用远程 API，也可以指向本机
或内网模型服务，因此不是必须本地部署。默认关闭；远程模式会发送硬过滤后的候选正文，本地模式
隐私更好但需要自行运行模型。向量使用私有 SQLite 派生缓存，后端失败时自动退回原检索器。

不使用 embedding 的 120-session 长期对话诊断中，显式认证写入后的离线检索为 10/10；
DeepSeek 读取最近窗口为 30%，完整历史和证据时态记忆均为 90%，而记忆路径平均只注入 342
字符。该结果不包含自由对话自动抽取；生产 `mca chat` 目前仍没有这条写入闭环。

存储与信任模型、只读命令和当前能力边界见[本地记忆说明](docs/memory.md)，
前沿项目与论文调研、分阶段方案见[时态经验记忆设计](docs/superpowers/specs/2026-08-17-evidence-bound-temporal-memory-design.md)。
通用检索策略、四路消融基线、离线结果和可选真实模型评测见
[记忆架构评测说明](docs/memory-evaluation.md)。
历史在线模型记录属于实验材料，不作为发布门禁；确定性发布报告应从当前源码重新生成。

无需 API Key 的四路对照：

```bash
.venv/bin/python -m evals.run_memory_comparison
.venv/bin/python -m evals.run_memory_longitudinal
.venv/bin/python -m evals.run_memory_control
.venv/bin/python -m evals.run_memory_intervention
```

v0.5.0 的发布门禁用一个命令运行全部八个确定性记忆套件，不调用模型，并输出与评测源码绑定的
统一 JSON 报告：

```bash
.venv/bin/python -m evals.run_memory_suite --json \
  --output /tmp/memory-v0.5.0.json
```

结果感知控制、自由对话自动抽取和聊天格式导入仍属于实验范围。生产承诺与明确不做的事项见
[项目范围](docs/project-scope.md)。

## 公共 Benchmark：固定模型 Harness 对照

v0.5.0 候选版已经接入 Harbor 0.22，并固定了一组来自公共 SWE-bench Verified 的 25 题
pilot。实验只改变 Agent Harness：候选组使用 MCA，对照组使用
`mini-swe-agent==2.1.0`，两组固定为同一个 `openai/gpt-5.6-sol` 模型和 provider
endpoint。Launcher 默认只打印命令，不会产生模型费用，并提供双臂单题 `--smoke`；在两组
真实运行完成且 Harbor 解析出的 task/image lock 一致前，项目不声明任何分数。运行协议、隔离边界和报告清单见
[Harbor 固定模型对照](benchmarks/harbor/README.md)。

## 安全与可靠性边界

| 控制 | Runtime 强制行为 | 边界 |
| --- | --- | --- |
| 只读聊天 | `/ask` 只允许列目录、搜索、读文件和查看 diff；新增工具不会自动获得权限 | `/code` 是用户显式授予的编码能力，不代表模型输出一定正确 |
| 验证门 | 修改后只有当前工作区指纹对应的权威验证通过，才允许 `submit`；识别到 0 个测试时默认拒绝 | 验证命令和测试覆盖率由用户负责配置；`--allow-zero-tests` 会显式削弱该门禁 |
| 事务执行 | `mca tx` 在隔离 Git worktree 中运行，将验证后的 `prepare` 与冲突检查后的 `commit` 分开 | 第一版按整个工作区检测冲突；读写集合是审计证据，尚不用于自动合并无关并发修改 |
| 崩溃恢复 | run/chat 从完整工具边界恢复，并使恢复前的验证结果失效 | 被强制终止的外部命令可能已经产生部分副作用 |
| HMAC 认证撤销 | 私有 Undo journal 以 HMAC 绑定轨迹、工作区、路径和内容 hash，并在覆盖前检查冲突 | HMAC 校验可检测本机 journal 是否被篡改，不证明修改在语义上安全 |
| Fail-closed 隔离 | `auto` 实际探测后端；没有可用后端时拒绝执行命令，除非用户显式选择 `none` | macOS 使用 `sandbox-exec`，Linux 使用 `bwrap` 或 Docker；原生 Windows 的 `auto` 只使用 Docker，没有本机强隔离后端 |
| 进程清理 | 超时、Ctrl-C、SIGTERM 和异常后尝试回收命令进程组及本次 Docker 容器 | 原生进程组清理是 best effort；double-fork 进程可创建新 session，`sandbox-exec` 不提供 PID namespace、cgroup 或容器等价的完整后代进程收容 |

这些机制是纵深防御，不是绝对安全沙箱。不要把不受信任的仓库与生产凭证放在同一工作区，也不要未经检查运行仓库自带的构建或测试命令；完整威胁模型见 [SECURITY.md](SECURITY.md)。

项目重点展示并约束一个最小 coding-agent 闭环：

```text
观察工作区 → 选择工具 → 执行动作 → 获取反馈 → 验证修改 → 提交或继续
```

- 结构化文件工具和默认关闭的任意 shell
- LangGraph Agent Loop
- 可恢复的 run/chat checkpoint、trajectory、diff 与可冲突检测的 undo
- `/ask` 只读聊天和 `/code` 编码授权模式
- macOS `sandbox-exec`、Linux `bwrap`，以及跨平台 Docker 后端
- OpenAI 与 DeepSeek 独立 provider 配置

## 它是什么界面

当前是两种终端界面：`mca run` 是一次性 CLI，`mca chat` 是行式交互 REPL。它不是 curses/全屏 TUI，也没有 Web UI；因此通过 SSH、普通 Terminal 和 CI 都能使用，部署时不需要浏览器或前端服务。

## 部署前提

在本机长期使用至少需要：

- Python 3.10+ 和项目虚拟环境
- 真实 `run` / `chat` 需要 DeepSeek 或 OpenAI API Key；确定性的 `mca demo` 不需要 Key
- 明确可用的隔离后端：macOS `sandbox-exec`、Linux `bwrap` 或 Docker；如果显式使用 `--sandbox none`，只应指向无凭证、可丢弃的可信目录
- 原生 Windows 可运行 `run`、`chat`、demo、事务和结构化文件工具；本地命令使用 `cmd.exe`，隔离执行需 Docker，或显式接受 `--sandbox none`
- 用户预先配置的权威验证：单个 `--test-command`，或按顺序列出的命名 `--check`
- 可写的用户状态目录；默认目录和权限见“运行状态与轨迹”

`git` 不是非 Git 项目的硬依赖，但 Git 仓库中的 dirty 检查和 diff 需要系统可信路径里的 `git`。

## 安装

需要 Python 3.10 或更高版本；CI 当前覆盖 3.10–3.13。推荐使用独立虚拟环境。普通用户安装发布包：

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install mini-code-agent-langgraph
mca demo
```

Windows PowerShell：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install mini-code-agent-langgraph
mca --version
mca --help
mca doctor --sandbox none
mca demo
mca tx demo
```

原生 Windows 已纳入 runtime CI。目标仓库的验证命令应使用 Windows 可执行语法；依赖 Bash、GNU 工具链或 POSIX 路径语义的项目仍建议在 WSL2 中运行。

### 源码开发

克隆仓库、执行 `python -m pip install -e ".[dev]"` 和运行测试属于贡献者工作流，请按 [CONTRIBUTING.md](CONTRIBUTING.md) 配置源码开发环境；不要把 editable install 当作普通用户的首选安装路径。

## 无密钥本地验证

`mca demo` 使用 Mock 模型在新建的系统临时目录测试完整一次性 Agent Loop，并保留目录供检查：

```bash
mca demo
```

Demo 不需要 API Key，也不会修改仓库中的 `examples/calculator_bug`。它内部对确定性 fixture 显式使用无隔离执行，因此只适合作为本地演示；真实 `run` / `chat` 仍保持 fail-closed 沙箱默认值。命令会按平台序列化，在 POSIX 使用 `/bin/sh`，在原生 Windows 使用 `cmd.exe`。

## 命名验证矩阵

按需要执行的顺序配置命名检查：

```bash
mca run "Fix the issue" \
  --model deepseek \
  --check tests "pytest -q" \
  --check lint "ruff check ." \
  --check types "pyright"
```

命名检查按串行执行，并且都必须在同一个未改变的工作区指纹状态下开始和结束。任何检查如果令受指纹覆盖的文件仍有改动，都会以 `WorkspaceChangedDuringVerification` 使整个矩阵失效；请在矩阵前运行生成器。被忽略的缓存路径继续遵循现有指纹策略。

`--test-command` 仍是向后兼容的单检查形式。最多配置 16 个检查。矩阵最坏情况下耗时约为检查数乘以每个命令的超时时间。

稳定的 `--test-command` 输出和事件字段保持兼容，但现在该单一旧命令如果让受指纹覆盖的文件保持改动，也会 fail closed。只能依照现有受信任 runtime artifact 策略使用被忽略的缓存路径。

这些证据表明：配置的命令在一个工作区状态下按照 runtime 策略通过。它不证明测试完整性、代码正确性、模型质量或整体系统安全性。

指纹在检查边界捕获。它能检测持久性改动，但无法证明命令没有在两次捕获之间完整地修改并恢复文件；此功能不声称提供不可变快照执行。

矩阵配置命令不会直接序列化到结构化证据中，且输出有大小边界。对已知模式、环境变量值以及通过现有脱敏控制配置的值，脱敏均为尽力而为；任意命令输出可能回显命令文本或无法被完美分类的值。轨迹文件应视为敏感数据，未经审查不得发布。

## 事务运行

当你不希望 Agent 在验证和人工提交前直接改动源工作区时，使用事务模式：

```bash
mca tx run "Fix the failing tests" \
  --cwd /path/to/clean/git/repo \
  --model deepseek \
  --check tests "pytest -q"

mca tx status TRANSACTION_ID
mca tx receipt TRANSACTION_ID
mca tx commit TRANSACTION_ID
```

`tx run` 会对干净的 Git 根目录建立快照，在私有状态目录下创建 detached worktree，并在其中运行原有的沙箱 Agent。每次工具调用都会持续写入访问 WAL，同时记录 read/write set。只有 Agent 已提交、通过的验证指纹又与隔离工作区精确一致时，事务才进入 `prepared`；在执行 `tx commit` 前，源工作区不会被修改。

`tx commit` 会再次确认源仓库 `HEAD`、整个源工作区指纹和 prepared 工作区指纹均未改变，并先执行 Git patch 检查。被 `.gitignore` 忽略或其他无法由补丁表达的变化不能进入 `prepared`。第一版有意采用整个工作区级别的严格冲突检测；记录的 read/write set 目前用于审计，不会自动合并无关的并发修改。

每个 prepared transaction 都会获得一份 HMAC 认证的 receipt，将基线、patch hash、验证证据与指纹、trajectory 摘要、WAL 摘要和访问集合绑定起来。`tx commit` 会强制把 receipt 与持久状态交叉校验，`mca tx receipt` 可展示其中不含源码的证据。它依赖本机私有密钥提供本地防篡改能力，不是可移植签名，也不意味着另一台机器应当直接信任。

进程在完整工具 checkpoint 后中断时，可以用相同模型与验证配置恢复；也可以直接放弃隔离工作区：

```bash
mca tx resume TRANSACTION_ID --model deepseek --check tests "pytest -q"
mca tx abort TRANSACTION_ID
```

事务 manifest、checkpoint、patch 和 worktree 都位于私有应用状态目录，该目录必须在源仓库之外。`commit` 能拒绝提交前观察到的冲突，但它不是覆盖整个文件系统的锁，无法阻止其他进程恰好在短暂的 check/apply 间隔中竞态写入。

无需 API Key 即可运行成功提交和冲突拒绝两个场景：

```bash
mca tx demo
```

Demo 会证明成功场景在 commit 前没有修改源仓库；随后在第二个仓库注入并发用户编辑，展示 commit 拒绝且不会覆盖该编辑。

## 配置密钥

默认在用户配置目录创建私有 `0600` env 文件：

```bash
mca init
```

使用真实 provider 前，请先在生成的文件中填入 provider key。之后 `mca run` 和 `mca chat` 会自动加载这个默认文件；只有使用其他位置时才需要传 `--env-file`。

如果 OpenAI 兼容中转在非流式请求上容易断连，可显式固定传输方式：

```bash
mca tx run "修复失败测试" \
  --cwd /path/to/repo \
  --model gpt-compatible \
  --provider openai \
  --base-url https://gateway.example/v1 \
  --streaming \
  --reasoning-effort low \
  --check tests "pytest -q"
```

这条路径固定使用 Chat Completions，不切换到 Responses API。两个选项默认不启用，避免静默改变其他 provider 的行为。事务 manifest 会记录它们；resume 若改掉任一值会拒绝继续，自动打印的 `next:` 命令则会完整继承。

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
mca chat --model deepseek \
  --deepseek-thinking \
  --env-file ~/.config/mca.env \
  --test-command "python3 -m pytest"
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
  --env-file ~/.config/mca.env \
  --test-command "python3 -m pytest"
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

会话默认进入 `/ask`：允许列目录、搜索、读文件和查看 diff，但运行 shell、测试或写文件会被 runtime 强制阻止，而不只是依赖提示词。如果启动时不传 `--test-command` 或 `--check`，该会话只能使用 `/ask`，`/code` 会被阻止；所有编码会话都应显式配置权威验证：使用旧版 `--test-command`，或使用命名的 `--check`。

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
  --env-file ~/.config/mca.env \
  --test-command "python3 -m pytest"
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

0.1/0.2 中未经过 HMAC 认证的旧版 Undo 数据默认拒绝写文件。确实检查并信任旧文件后，才可显式使用 `--allow-legacy-unsafe`。

## 工具与安全边界

| 工具 | 作用 |
| --- | --- |
| `list_files` | 列出工作区文件 |
| `search_files` | 搜索工作区文本 |
| `read_file` | 读取文件或行范围 |
| `write_file` | 写入文件 |
| `apply_patch` | 精确文本替换 |
| `replace_lines` | 按行替换 |
| `run_tests` | 运行用户配置的验证矩阵 |
| `git_diff` | 查看变更 |
| `submit` | 结束任务 |
| `bash` | 任意 shell 逃生口，默认禁用 |

默认策略：

- `--cwd` 必须是目录，文件工具限制在其真实路径范围内
- 新建 run/chat 时 dirty Git 工作区默认拒绝启动；`--allow-dirty` 会关闭这项保护，resume 前也必须自行检查并暂存额外改动
- shell 默认关闭，验证命令和子进程使用收敛后的环境
- shell/test 子进程在超时、Ctrl-C、SIGTERM 和异常后回收整个进程组；Docker 运行使用唯一 name/cidfile 并在退出时强制清理
- `mca run` 即使没有检测到文件变化，也必须至少通过一次用户配置的权威验证才能提交
- `mca run` 要求显式传入 `--model` 和 `--test-command` 或命名 `--check`，并拒绝 `--model mock`；无 Key 的确定性流程请使用 `mca demo`
- 工作区指纹覆盖内容、文件类型、权限位、symlink target、依赖目录及 Git 本地配置/hooks；缓存目录和易变 Git 数据库除外
- 模型不能覆盖旧版 `--test-command` 或命名的 `--check`；失败的权威验证、改变工作区指纹的后续操作以及 resume 会使旧验证失效
- 权威命令被识别为 0 个测试时默认不能通过验证；`--allow-zero-tests` 会允许它通过，是需要用户明确接受的验证弱化
- `/ask` 使用只读工具允许列表；未来新增工具不会被默认放行
- 工具输出、搜索、结构化编辑、tool-call 数量、持久对话和 `reasoning_content` 都有资源上限；状态文件读写共享 256 MiB 硬上限，可用 `--context-chars` 调整上下文预算
- 启动时实际探测沙箱能力；`auto` 会按顺序尝试本机后端，某个后端存在但不可运行时继续尝试下一个，全部失败才拒绝启动
- `--max-steps`、命令超时、请求超时必须大于零

沙箱可用性依赖操作系统：

- macOS：优先尝试系统 `sandbox-exec`。它拒绝网络和默认写入，隐藏真实 home（其中的目标工作区除外），只允许写工作区和 executor 所有的私有 runtime tree；`HOME`、`TMPDIR` 指向该私有目录，共享 `/tmp` 与 `/private/tmp` 不可写。它是系统策略 profile，不是 PID namespace、cgroup 或容器边界，而且系统可能弃用或限制它
- Linux：优先尝试 `bwrap`。它 unshare namespaces 并保持宿主根目录只读；workspace 与 executor runtime tree 是仅有的可写宿主路径，沙箱内的私有 `/run`、`/tmp` 和 home tmpfs 也可写，并使用私有 `/dev` 与全新 `/proc`。其 PID namespace 对后代进程的收容强于普通进程组，但仍依赖宿主内核与 Bubblewrap
- macOS / Linux：安装并启动 Docker，并预先拉取沙箱镜像后可选择 `--sandbox docker`；容器无网络、根文件系统只读、丢弃 capabilities、设置资源上限，只有工作区 bind mount 可写，`/tmp` 是私有且有大小限制的 tmpfs。在 POSIX 宿主上以调用者的数字 UID:GID 运行，并显式设置私有 `HOME`/`TMPDIR`、Python bytecode 与 Git 环境。默认镜像是 `python:3.11-slim`，可用 `--docker-image` 或 `MCA_DOCKER_IMAGE` 指向带目标项目依赖的预构建镜像，运行时不会隐式拉镜像
- 原生 Windows：本地命令由 `cmd.exe` 执行，超时清理使用 `taskkill /T /F`；`auto` 仅尝试 Docker。没有 Docker 时必须显式选择 `--sandbox none`，这不提供隔离
- 没有可用后端时，只有显式 `--sandbox none` 才允许不隔离执行

可用 `mca sandbox probe --sandbox auto` 检查上述有限边界，也可显式指定 `sandbox-exec`、`bwrap` 或 `docker`。所有 Docker coding/test 镜像都必须提供 `/bin/sh`，probe 还额外需要 `python3`；普通编码运行仍可使用满足 `/bin/sh` 要求的其他预拉取自定义镜像。原生进程清理都是 best effort：POSIX 的 double-fork 或 Windows 中脱离进程树的后代仍可能逃逸；Bubblewrap 的 PID namespace 和 Docker 容器边界提供更强收容，但所有后端都不是完整 OS/process containment 保证。

沙箱、路径检查和脱敏都不是运行不可信仓库的绝对安全边界。不要让 Agent 在包含生产凭证、SSH 私钥或不应被模型读取的数据目录中运行。

## 项目结构

```text
src/mini_code_agent/contracts.py   Runtime 协议与工具结果契约
src/mini_code_agent/context.py     上下文压缩与工具调用审计
src/mini_code_agent/agent.py       LangGraph Agent Loop
src/mini_code_agent/chat.py        持续聊天会话
src/mini_code_agent/model.py       工具声明与 provider adapter
src/mini_code_agent/executor.py    Tool Runtime、审批和沙箱
src/mini_code_agent/verification.py 工作区指纹绑定的验证门
src/mini_code_agent/trajectory.py  Trace、Diff 和 Undo
src/mini_code_agent/transaction.py 框架无关的事务状态机
src/mini_code_agent/transaction_adapter.py Agent 工具调用与读写集适配层
src/mini_code_agent/transaction_cli.py 事务命令编排与 Demo
src/mini_code_agent/receipt.py     HMAC 认证的 prepared patch receipt
src/memory_core/                   宿主无关的记忆协议、形成、预算与生命周期策略
src/mini_code_agent/memory_adapters/ MCA receipt、Git 身份与 Agent 上下文适配器
src/mini_code_agent/memory_models.py 不可变记忆模型与权限策略
src/mini_code_agent/memory_store.py  HMAC 认证的 SQLite/FTS 记忆存储
src/mini_code_agent/memory_admission.py Runtime 所有的证据解析与记忆准入
src/mini_code_agent/locking.py     POSIX/Windows 事务文件锁
src/mini_code_agent/security.py    路径与密钥安全
src/mini_code_agent/cli.py         CLI、状态目录与授权模式
```

## 测试与离线 benchmark

```bash
pytest -q
python -m pip check
python -m evals.run_evals --json
python -m evals.run_memory_suite --json
```

从源码 checkout 的仓库根目录精确复现 v0.3.2 verified-patch 基线：

```bash
.venv/bin/python -m evals.run_evals --json
```

这 11 个 case 是：`single-file-fix`、`multi-file-fix`、`explain-only`、`failed-fix-recovery`、`premature-submission`、`stale-verification`、`failed-test-refusal`、`zero-test-refusal`、`shell-disabled`、`checkpoint-resume` 和 `authenticated-undo`。v0.3.2 基线为 **11/11 通过**：9 个已验证的提交、2 个符合预期的策略拒绝、0 个意外提交、0 个无关改动。

这是使用本地脚本化决策生成的离线 runtime-policy 一致性证据。它**不衡量**模型质量、自主修复能力、provider 行为、真实项目任务成功率或 SWE-bench 成绩。

可用以下命令测量安全工作区指纹的 cold/warm capture：

```bash
python benchmarks/benchmark_fingerprint.py --root . --runs 5
```

该 benchmark 扫描完整 verification scope，并报告当前机器和文件系统上的本地性能证据；结果不是可移植的 CI 阈值。

这是一个安全优先的 reference runtime，而不是 Codex 或 Claude Code 的替代品。发布与贡献约定分别见 [CHANGELOG.md](CHANGELOG.md) 和 [CONTRIBUTING.md](CONTRIBUTING.md)。
