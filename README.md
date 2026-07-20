# mini-code-agent-langgraph

> A compact, security-first LangGraph coding agent with verified patches, crash recovery, and HMAC-authenticated undo.

[中文详细指南](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/README.zh-CN.md) · [Security policy](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/SECURITY.md) · [Contributing](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CONTRIBUTING.md) · [Changelog](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CHANGELOG.md)

`mini-code-agent-langgraph` is a single-process, line-oriented CLI and REPL for studying, auditing, and extending a constrained coding-agent loop. It is not a full-screen TUI or web application.

## Try it without an API key

```bash
git clone https://github.com/wusuiling-if/mini-code-agent-langgraph.git
cd mini-code-agent-langgraph
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
mca demo
```

`mca demo` fixes a deterministic calculator fixture in a temporary workspace. It does not modify the clone or contact a model provider. Before using a real repository, inspect prerequisites without reading secret values:

```bash
mca doctor --cwd /path/to/repo --sandbox auto --provider auto
```

`doctor` performs static prerequisite checks; `run` and `chat` perform the authoritative sandbox usability probe at startup. Doctor checks whether a provider key is present in the current process environment without printing its value, and inspects private env-file metadata without opening the file.

## Run and chat

Create a private environment-file template with `mca init`, populate it with a provider key, then use a DeepSeek or OpenAI-compatible provider for a real task:

```bash
mca init
mca run "Fix the failing tests" --cwd /path/to/repo --model deepseek --provider deepseek --test-command "python3 -m pytest -q"
mca chat --cwd /path/to/repo --model deepseek --provider deepseek
```

`mca run` is a one-shot agent run. `mca chat` is a persistent REPL that starts in read-only `/ask` mode; enter `/code` to explicitly allow coding tools. `--yes` skips confirmations but never grants `/code` mode by itself. `--model mock` is available for local `run` dry runs and tests, not chat.

Runs and chats save a trajectory. Inspect it or preview a conflict-aware undo before changing files:

```bash
mca trace /path/to/run.traj.json --diff
mca undo /path/to/run.traj.json --dry-run
```

## Enforced controls and limits

- New runs and chats reject dirty Git worktrees by default; arbitrary shell access is disabled by default.
- Structured file operations are confined to the resolved workspace, and `/ask` has a runtime read-only allowlist.
- A user-selected authoritative test must pass against the current workspace fingerprint before submission. Resume invalidates earlier verification.
- Undo uses a private, HMAC-authenticated journal and rejects post-edit conflicts unless explicitly forced.
- `--sandbox auto` fails closed if no usable backend is found. `--sandbox none`, `--allow-shell`, `--allow-dirty`, `--yes`, and force/legacy Undo options deliberately weaken protections.
- Native Windows supports informational CLI and configuration paths only. Run the full agent, structured tools, and `mca demo` from macOS, Linux, or WSL2. macOS uses `sandbox-exec`; Linux uses `bwrap` or Docker when available.

These controls are defense in depth, not a guarantee that an untrusted repository, command, dependency, image, host, or provider is safe. Do not run it in a workspace containing production credentials. Read the complete [security policy](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/SECURITY.md) before use.

## Project structure

```text
src/mini_code_agent/agent.py         LangGraph agent loop
src/mini_code_agent/chat.py          Persistent chat session
src/mini_code_agent/executor.py      Tools, approvals, and sandboxing
src/mini_code_agent/verification.py  Workspace-fingerprint verification gate
src/mini_code_agent/trajectory.py    Trajectory, trace, and undo support
src/mini_code_agent/security.py      Path and secret protections
src/mini_code_agent/cli.py           CLI and state/configuration handling
tests/                               Deterministic test suite
evals/                               Offline evaluation baseline
```

## Validate a checkout

```bash
pytest -q
python -m pip check
python -m evals.run_evals --json
mca doctor --sandbox none
mca demo
```

`mca doctor --sandbox none` is a read-only configuration smoke test and intentionally reports an isolation warning. Skip `mca demo` on native Windows and run it from WSL2 instead. For contribution and release expectations, see [CONTRIBUTING.md](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CONTRIBUTING.md) and [CHANGELOG.md](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CHANGELOG.md).
