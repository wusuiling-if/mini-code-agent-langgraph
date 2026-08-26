# Transactional Agent Runtime

[![tests](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml/badge.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml)
[![Python 3.10–3.13](https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/LICENSE)

> Keep agent changes out of your source checkout until they are verified and safe to apply.

[中文详细指南](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/README.zh-CN.md) · [Security policy](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/SECURITY.md) · [Contributing](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CONTRIBUTING.md) · [Changelog](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CHANGELOG.md)

Coding agents normally edit the same checkout a developer is using. A failed run, stale test result, concurrent edit, or interrupted process can therefore leave the source in an ambiguous state. Transactional Agent Runtime gives an agent an isolated Git worktree and treats its patch as a prepare/commit transaction.

The source checkout is not updated until the runtime has verified one exact workspace state, authenticated the prepared evidence, and rechecked the source for conflicts at commit time. LangGraph is an included loop adapter, not the transaction core.

## See the transaction

No API key or existing repository is required:

```bash
python -m pip install mini-code-agent-langgraph
mca tx demo
```

The demo runs two deterministic transactions. The first proves that a verified patch does not touch the source before commit. The second injects a concurrent source edit and proves that commit is refused without overwriting it. Output is line-oriented and machine-readable on Linux, macOS, and Windows.

## Core guarantees

- **Clean prepare:** agent tools and checks run in an isolated Git worktree; the source remains unchanged before commit.
- **Verification binding:** passing checks are bound to the exact prepared workspace fingerprint. A later change invalidates them.
- **Tamper evidence:** a local HMAC-authenticated receipt binds the baseline, patch, verification, trajectory, access log, and prepared fingerprint.
- **Conflict refusal:** commit rejects a changed source `HEAD`, any changed source-workspace fingerprint, a changed prepared workspace, or a mismatched patch.

## Deliberate limits

- Transactions require a clean Git worktree, and private runtime state must live outside the source repository.
- Conflict detection is intentionally whole-workspace: even an unrelated concurrent source edit rejects commit rather than attempting a merge.
- Receipts are local tamper evidence, not portable signatures, proof of test completeness, or proof that a patch is correct.
- Native Windows has no built-in isolation backend. `--sandbox auto` requires Docker; `--sandbox none` is an explicit unisolated opt-out.
- The short pre-apply check is not a filesystem-wide lock against every possible external race.

## Run a real transaction

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

See [the transaction protocol](docs/transaction-protocol.md) for lifecycle, receipt fields, recovery, and failure behavior. Agent-loop, provider, doctor, and sandbox operations are secondary integrations documented in [runtime operations](docs/runtime-operations.md) and [sandboxing](docs/sandboxing.md).

## Run and chat

The package still includes one-shot `mca run`, persistent `mca chat`, DeepSeek and OpenAI-compatible providers, trace inspection, and conflict-aware undo. These are integrations around the transaction core rather than the primary product identity.

See [runtime operations](docs/runtime-operations.md) for provider setup, verification matrices, chat modes, trajectory handling, and the legacy deterministic demo.

## Opt-in memory foundation

The project includes an evidence-bound local memory foundation:
immutable cards, append-only temporal status, authenticated
evidence/edges, SQLite FTS retrieval, and a read-only `mca memory` CLI. It is not
connected to `run`, `chat`, or `tx` by default, so existing behavior and required
dependencies remain unchanged. Transaction runs can explicitly select `--memory local`;
the original evidence-temporal retriever then injects only same-workspace advisory context.
After a successful commit, the runtime records both the authenticated verification workflow
and a receipt-bound verified repair patch, without storing verification command plaintext.
The outcome controller remains research-only after regressing in natural transfer tests. See [evidence-bound local memory](docs/memory.md)
for the trust model, commands, and current boundary.

The host-neutral `memory_core` package has no dependency on the MCA transaction or
agent runtime. MCA-specific receipt, stable Git identity, and context-injection code lives
behind adapters. Context rendering has a 16K hard budget, structured retrieval audits omit
memory content, and per-project repair capacity retires old records without deleting audit
history. Private resumable trajectories still contain the bounded injected context. Retrieval
narrows candidates to the requested scopes plus global records before authenticating cards and
their latest state, so unrelated project history no longer participates in ranking.

The portable conversation layer also includes replayable semantic mutations, canonical
checkpoints, post-condition commit reports, a loss-aware SillyTavern chat adapter, and a
non-executing Tavern Helper import preview. JavaScript and remote module loaders stay
quarantined; declarative variables become untrusted schema-mapping candidates rather than
durable facts. Core identity and preference memories are protected from capacity eviction,
while episodic/transient noise remains compressible or retireable. New sessions reserve a
bounded continuity context and report core overflow instead of silently omitting it. See
[portable conversation memory](docs/conversation-memory.md).

An optional OpenAI-compatible embedding backend can point to either a hosted API or a
local/private model server, so local model deployment is not required. It is off by default,
caches derived vectors privately, sees only hard-filtered candidates, and falls back to the
original lexical/graph retriever when unavailable.

In a no-embedding 120-session dialogue diagnostic, retrieval after explicit authenticated
session ingestion reached 10/10. DeepSeek reading scored 30% from the recent window and 90%
from both full history and evidence-temporal memory, while the memory path averaged 342 context
characters. This does not test automatic conversation extraction, which production `mca chat`
does not yet implement.

The deterministic cross-domain ablation compares no memory, pure top-k recall,
a traditional three-layer baseline, and the evidence-temporal hybrid:

```bash
.venv/bin/python -m evals.run_memory_comparison
```

The v0.5.0 release gate runs all eight deterministic memory suites without a
model call and emits one source-bound JSON report:

```bash
.venv/bin/python -m evals.run_memory_suite --json \
  --output /tmp/memory-v0.5.0.json
```

Outcome-aware control, automatic free-conversation extraction, and chat-format
imports remain experiments. See [project scope](docs/project-scope.md) for the
production and evaluation boundary.

## Public fixed-model harness comparison

The v0.5.0 candidate includes a Harbor 0.22 adapter and a pinned 25-task pilot from
the public SWE-bench Verified dataset. It compares MCA with `mini-swe-agent==2.1.0`
under the pinned `openai/gpt-5.6-sol` model and provider endpoint. The launcher is
dry-run by default and supports a paired one-task `--smoke`; no score is claimed
until both paid arms have run and their resolved task/image locks match. See
[the Harbor protocol](benchmarks/harbor/README.md).

## Enforced controls and limits

- New runs and chats reject dirty Git worktrees by default; arbitrary shell access is disabled by default.
- Structured file operations are confined to the resolved workspace, and `/ask` has a runtime read-only allowlist.
- User-configured authoritative verification—legacy `--test-command` or named `--check` entries—must pass against the current workspace fingerprint before submission. A recognized zero-test result is rejected by default, and resume invalidates earlier verification.
- Undo uses a private, HMAC-authenticated journal and rejects post-edit conflicts unless explicitly forced.
- `--allow-zero-tests` explicitly weakens verification by allowing a recognized zero-test result to satisfy the gate. `--sandbox none`, `--allow-shell`, `--allow-dirty`, `--yes`, and force/legacy Undo options also deliberately weaken protections; `--sandbox auto` fails closed if no usable backend is found.
- Native Windows supports transactions and structured tools. Local commands use `cmd.exe`; process isolation requires Docker.

Read [sandboxing](docs/sandboxing.md) for backend boundaries and probes, and the complete [security policy](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/SECURITY.md) for the threat model.

## Offline verified-patch benchmark

From a source checkout, reproduce the deterministic v0.3.2 baseline with:

```bash
.venv/bin/python -m evals.run_evals --json
```

The eleven cases cover `single-file-fix`, `multi-file-fix`, `explain-only`, `failed-fix-recovery`, `premature-submission`, `stale-verification`, `failed-test-refusal`, `zero-test-refusal`, `shell-disabled`, `checkpoint-resume`, and `authenticated-undo`. The v0.3.2 baseline is **11/11 passing**: nine verified submissions, two expected policy refusals, zero unexpected submissions, and zero unrelated changes.

This is offline runtime-policy conformance evidence produced with scripted local decisions. It does **not** measure model quality, autonomous repair ability, provider behavior, real-world task success, or SWE-bench performance.

## Project structure

```text
src/mini_code_agent/agent.py         LangGraph agent loop
src/mini_code_agent/chat.py          Persistent chat session
src/mini_code_agent/executor.py      Tools, approvals, and sandboxing
src/mini_code_agent/verification.py  Workspace-fingerprint verification gate
src/mini_code_agent/trajectory.py    Trajectory, trace, and undo support
src/mini_code_agent/transaction.py   Framework-independent transaction state machine
src/mini_code_agent/transaction_adapter.py Agent tool-call and access-set adapter
src/mini_code_agent/transaction_cli.py Transaction command orchestration and demo
src/mini_code_agent/receipt.py       Authenticated prepared-patch receipts
src/mini_code_agent/memory_models.py Immutable memory values and authority policy
src/mini_code_agent/memory_store.py  Authenticated SQLite/FTS memory foundation
src/mini_code_agent/locking.py       POSIX/Windows transaction file locks
src/mini_code_agent/security.py      Path and secret protections
src/mini_code_agent/cli.py           CLI and state/configuration handling
tests/                               Deterministic test suite
evals/                               Offline evaluation baseline
benchmarks/harbor/                   Fixed-model SWE-bench harness comparison
```

## Develop and validate a checkout

Cloning the repository and using an editable install are contributor workflows; follow [CONTRIBUTING.md](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CONTRIBUTING.md) for the source setup. In that activated development environment, run:

```bash
pytest -q
python -m pip check
python -m evals.run_evals --json
python -m evals.run_memory_suite --json
mca doctor --sandbox none
mca demo
```

`mca doctor --sandbox none` is a read-only configuration smoke test and intentionally reports an isolation warning. Native Windows runs both demos in CI; use Docker for isolated command execution or WSL2 when POSIX tooling is required by the target repository. For release expectations, see [CHANGELOG.md](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CHANGELOG.md).
