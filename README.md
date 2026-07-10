# mini-code-agent-langgraph

A tiny coding agent built with LangGraph. It is intentionally small:

- CLI: human-facing command line entry point
- tools: model-facing capabilities for bash, file reads, patches, diffs, and tests
- trajectory: full run record for debugging agent behavior

## Quick Start

Run the mock model first. It does not call any external API.

```bash
cd mini-code-agent-langgraph
PYTHONPATH=src python3 -m mini_code_agent run "demo task" --model mock --yes
```

For unattended runs, pass `--yes`. Without it, confirmation mode requires an interactive terminal.

Run with an OpenAI-compatible model:

```bash
export OPENAI_API_KEY=...
PYTHONPATH=src python3 -m mini_code_agent run "fix the failing tests" --cwd /path/to/repo --model gpt-4.1-mini
```

Run with DeepSeek:

```bash
export DEEPSEEK_API_KEY=...
PYTHONPATH=src python3 -m mini_code_agent run "inspect this repo" --cwd /path/to/repo --model deepseek
```

Or keep secrets in a local env file that is never committed:

```bash
printf 'DEEPSEEK_API_KEY=...\n' > .env.local
PYTHONPATH=src python3 -m mini_code_agent run "inspect this repo" \
  --cwd /path/to/repo \
  --model deepseek \
  --env-file .env.local
```

DeepSeek aliases:

- `deepseek` -> `deepseek-v4-flash`
- `deepseek-flash` -> `deepseek-v4-flash`
- `deepseek-pro` -> `deepseek-v4-pro`

Run against an OpenAI-compatible gateway:

```bash
PYTHONPATH=src python3 -m mini_code_agent run "inspect the project" \
  --cwd /path/to/repo \
  --model your-model-name \
  --base-url http://localhost:8000/v1
```

By default, commands require confirmation. Use `--yes` only in a safe test repo.

Useful commands:

```bash
PYTHONPATH=src python3 -m mini_code_agent init
PYTHONPATH=src python3 -m mini_code_agent trace runs/latest.traj.json --diff
PYTHONPATH=src python3 -m mini_code_agent undo runs/latest.traj.json --dry-run
```

## Safety

This project includes first-pass safety:

- file tools are confined to `--cwd`
- arbitrary bash is disabled by default
- shell/test commands are sandboxed with `sandbox-exec` or `bwrap` when available
- obvious destructive shell commands are blocked when shell is enabled
- tool observations and trajectories redact common API key patterns
- API keys should come from environment variables or `--env-file`, not command-line args
- dirty git worktrees are refused by default; pass `--allow-dirty` to override
- every run records `workspace_changes`
- `--test-command` configures the default command used by `run_tests`
- `--allow-shell` is required before the model can use arbitrary bash
- `--sandbox auto` uses `sandbox-exec`, `bwrap`, or Docker when available

This is still not a perfect security boundary across all platforms. Keep using disposable repositories for `--yes`, and only pass `--allow-shell` when you understand the command risk.

## Structured Tools

The model can call:

- `bash(command)`: shell command in `--cwd`
- `list_files(path, max_files)`: list workspace files
- `search_files(pattern, path, max_results)`: search text files
- `read_file(path, start_line, end_line, max_chars)`: safe file read inside `--cwd`
- `write_file(path, content)`: safe file write inside `--cwd`
- `apply_patch(path, old, new, replace_all)`: exact text replacement inside `--cwd`
- `replace_lines(path, start_line, end_line, new_text)`: line-range replacement inside `--cwd`
- `git_diff(path)`: show current diff
- `run_tests(command)`: run test command in `--cwd`
- `submit(summary)`: finish the task

`bash` is kept as an escape hatch and legacy final-submission path. It is disabled unless you pass `--allow-shell`.

By default, `run_tests` only runs the configured `--test-command`. Custom test commands require `--allow-shell`.

## What To Read First

- `src/mini_code_agent/cli.py`: user-facing CLI
- `src/mini_code_agent/agent.py`: LangGraph agent loop
- `src/mini_code_agent/executor.py`: tool implementation and first-pass safety policy
- `runs/*.traj.json`: saved trajectory files
