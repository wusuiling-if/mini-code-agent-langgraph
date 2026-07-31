# Runtime Operations

The package retains the `mca` CLI and the `mini-code-agent-langgraph` PyPI name. Transaction commands are the primary product surface; the bundled LangGraph loop, model providers, chat, and non-transactional run remain integrations for supplying agent work.

## Configure a provider

Sign in from the terminal. The key prompt is hidden and the credential is stored in a private per-user file, so no config file needs to be opened manually:

```bash
mca login deepseek
# or: mca login openai
mca doctor --cwd /path/to/repo --sandbox auto --provider auto
```

Run `mca logout deepseek` (or `openai`) to remove a saved credential. Interactive agent startup also offers this login flow when the selected provider has no credential. Environment variables, `--env-file`, and the advanced `mca init` env-template workflow remain available for automation and custom endpoints.

## One-shot and chat integrations

```bash
mca run "Fix the failing tests" \
  --cwd /path/to/repo \
  --model deepseek \
  --provider deepseek \
  --check tests "pytest -q"

mca chat \
  --cwd /path/to/repo \
  --model deepseek \
  --provider deepseek \
  --check tests "pytest -q"
```

`mca run` requires an explicit model and authoritative verification. `mca chat` starts in read-only `/ask` mode; enter `/code` only after starting it with `--test-command` or `--check`. `--yes` skips confirmations but never grants code mode by itself.

Named checks execute serially in declaration order. They must all begin and end against one unchanged workspace fingerprint:

```bash
--check tests "pytest -q" --check lint "ruff check ." --check types "pyright"
```

The backward-compatible `--test-command` form configures one check. A recognized zero-test result is rejected by default. `--allow-zero-tests` explicitly weakens this gate.

Runs and chats save a redacted trajectory. Treat it as sensitive because arbitrary command output cannot always be classified perfectly:

```bash
mca trace /path/to/run.traj.json --diff
mca undo /path/to/run.traj.json --dry-run
```

Use [`mca tx run`](transaction-protocol.md) when an agent must not edit the source checkout before verification and explicit commit.
