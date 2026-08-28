# Runtime Operations

The package retains the `mca` CLI and the `mini-code-agent-langgraph` PyPI name. Transaction commands are the primary product surface; the bundled LangGraph loop, model providers, chat, and non-transactional run remain integrations for supplying agent work.

## Configure a provider

Create a private environment-file template, populate it with a provider key, and inspect prerequisites without exposing secret values:

```bash
mca init
mca doctor --cwd /path/to/repo --sandbox auto --provider auto
```

For an OpenAI-compatible gateway that requires incremental responses, pin the
transport explicitly:

```bash
mca tx run "Fix the failing tests" \
  --cwd /path/to/repo \
  --model gpt-compatible \
  --provider openai \
  --base-url https://gateway.example/v1 \
  --streaming \
  --reasoning-effort low \
  --check tests "pytest -q"
```

This route uses Chat Completions, not the Responses API. The flags are opt-in so
existing provider behavior does not change silently.

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
  --memory local \
  --check tests "pytest -q"
```

`mca run` requires an explicit model and authoritative verification. `mca chat` starts in read-only `/ask` mode; enter `/code` only after starting it with `--test-command` or `--check`. `--yes` skips confirmations but never grants code mode by itself.

`--memory local` is a separate opt-in and does not grant code authority. It enables
same-workspace recall and these local controls:

```text
/remember TEXT          Store one explicit source-bound memory
/correct MEMORY_ID TEXT Supersede an active memory with a new revision
/forget ID_OR_QUERY     Tombstone exactly one matched memory
/memory [QUERY]         List active same-workspace memories
/memory candidates      List heuristic candidates awaiting approval
/remember @ID           Approve one pending candidate
/memory dismiss ID      Dismiss one pending candidate
```

Raw chat events and candidate decisions live under the private state directory.
Obvious credentials are refused for durable storage. Heuristics never admit a card
directly, and recalled content is bounded, provenance-bearing advisory data rather
than instructions or tool permission.

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
