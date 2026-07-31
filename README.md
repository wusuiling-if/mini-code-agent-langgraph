# mini-code-agent-langgraph

[![tests](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml/badge.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml)
[![Python 3.10–3.13](https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/LICENSE)

> A compact transactional runtime for coding agents: isolate, verify, attest, then commit.

[中文详细指南](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/README.zh-CN.md) · [Security policy](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/SECURITY.md) · [Contributing](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CONTRIBUTING.md) · [Changelog](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CHANGELOG.md)

`mini-code-agent-langgraph` is a small, auditable runtime centered on one mechanism: an agent works in an isolated Git transaction, produces verification-bound evidence, and cannot update the source worktree until an explicit conflict-checked commit. LangGraph is the included loop adapter, not the transaction core.

![`mca demo` fixes a calculator bug, verifies the tests, and submits the patch](https://raw.githubusercontent.com/wusuiling-if/mini-code-agent-langgraph/main/docs/assets/demo.gif)

- **Verification-bound submission:** user-configured verification—legacy `--test-command` or named `--check` entries—must pass against the current workspace fingerprint before the agent can submit.
- **Transactional execution:** `mca tx` treats an agent run as a prepare/commit protocol: the source stays untouched until verification evidence and conflict checks agree.
- **Inspectable recovery:** redacted trajectories persist each run and can resume safely after an interruption.
- **Conflict-aware Undo:** a private HMAC-authenticated journal rejects post-edit conflicts by default.

## Install and try it without an API key

```bash
python -m pip install mini-code-agent-langgraph
mca demo
```

`mca demo` fixes a deterministic calculator fixture in a temporary workspace. It does not modify the clone or contact a model provider. Before using a real repository, inspect prerequisites without reading secret values:

```bash
mca doctor --cwd /path/to/repo --sandbox auto --provider auto
mca sandbox probe --sandbox auto
```

`doctor` performs static prerequisite checks. `run` and coding-enabled `chat` sessions perform the authoritative backend startup check; a coding-enabled chat is one started with `--test-command` or `--check`. An `/ask`-only chat started without either skips that check because it cannot run tests, shell commands, or coding tools. Doctor checks whether a provider key is present in the current process environment without printing its value, and inspects private env-file metadata without opening the file.

`mca sandbox probe` goes further: without a provider key or target repository, it creates disposable data and checks a workspace write plus backend-specific outside-write, Unix-socket, and network boundaries. Native backends must read an exact host sentinel and return the probe's reserved evidence code only when mutation is blocked by `EPERM`, `EACCES`, or `EROFS`; Docker must not see that sentinel and must separately report the `ST_RDONLY` mount flag for `/`. Other positive exits, launch failures, exceptions, and timeouts are failures rather than denial evidence. `bwrap` and Docker must hide the controlled and known host Unix sockets, while `sandbox-exec` may expose a path only if connection remains denied. The network check first attempts a no-packet UDP `connect` to a TEST-NET address and, only when the process cannot obtain or use an outbound route, requires denial of a controlled loopback TCP connection. It prints one `[PASS]` or `[FAIL]` result per check and rejects `--sandbox none`, which cannot demonstrate isolation. The probe itself is bounded and useful for validating a local setup, but a passing result is evidence only for these checks, not proof that arbitrary untrusted code is safe.

## Run and chat

Create a private environment-file template with `mca init`, populate it with a provider key, then use a DeepSeek or OpenAI-compatible provider for a real task:

```bash
mca init
mca run "Fix the failing tests" --cwd /path/to/repo --model deepseek --provider deepseek --test-command "python3 -m pytest -q"
mca chat --cwd /path/to/repo --model deepseek --provider deepseek --test-command "python3 -m pytest -q"
```

`mca run` is a one-shot coding run and requires both an explicit `--model` and authoritative verification configured with `--test-command` or `--check`; it rejects `--model mock`, so use `mca demo` for the deterministic no-key flow. `mca chat` is a persistent REPL that starts in read-only `/ask` mode. A chat started without either verification form remains `/ask`-only and blocks `/code`; supply one, then enter `/code`, to explicitly allow coding tools. `--yes` skips confirmations but never grants `/code` mode by itself.

For a named verification matrix, configure the checks in the order they must run:

```bash
mca run "Fix the issue" \
  --model deepseek \
  --check tests "pytest -q" \
  --check lint "ruff check ." \
  --check types "pyright"
```

Named checks run serially and must all begin and end with one unchanged workspace fingerprint. A check that leaves a fingerprinted file changed invalidates the entire matrix with WorkspaceChangedDuringVerification; run generators before the matrix. Ignored cache paths retain the existing fingerprint policy.

`--test-command` remains the backward-compatible single-check form. Configure at most 16 checks. Worst-case matrix time is approximately the number of checks multiplied by the per-command timeout.

Stable `--test-command` output and event fields remain compatible, but the single legacy command now also fails closed if it leaves a fingerprinted file changed. Use ignored cache paths only through the existing trusted runtime artifact policy.

This evidence shows that the configured commands passed under the runtime policy for one workspace state. It does not prove test completeness, code correctness, model quality, or overall system safety.

Fingerprint capture occurs at check boundaries. It detects persisted changes but cannot prove that a command did not modify and restore a file entirely between captures; this feature does not claim immutable-snapshot execution.

Matrix configuration commands are not directly serialized into structured evidence and output is bounded. Redaction is best effort for known patterns, environment values, and values configured through the existing redaction controls; arbitrary command output can echo command text or values that cannot be classified perfectly. Treat trajectory files as sensitive and do not publish them without review.

Runs and chats save a trajectory. Inspect it or preview a conflict-aware undo before changing files:

```bash
mca trace /path/to/run.traj.json --diff
mca undo /path/to/run.traj.json --dry-run
```

## Transactional runs

Use a transaction when the agent must not edit the source worktree before its patch is verified and explicitly committed:

```bash
mca tx run "Fix the failing tests" \
  --cwd /path/to/clean/git/repo \
  --model deepseek \
  --check tests "pytest -q"

mca tx status TRANSACTION_ID
mca tx receipt TRANSACTION_ID
mca tx commit TRANSACTION_ID
```

`tx run` snapshots a clean Git root, creates a detached worktree under the private state directory, and runs the normal sandboxed agent there. Tool calls are persisted as a write-ahead access log with read/write sets. A run reaches `prepared` only when the agent submits and its passing verification fingerprint exactly matches the isolated workspace. Until `tx commit`, the source worktree is unchanged.

`tx commit` fails closed unless the source `HEAD` and entire source-workspace fingerprint still match the begin snapshot and the isolated workspace still matches the prepared fingerprint. It checks the binary Git patch before applying it; ignored or otherwise non-patchable workspace changes cannot be prepared. This first version deliberately uses whole-workspace conflict detection. The recorded read/write sets are audit evidence, not yet a promise that unrelated concurrent edits can be merged.

Every prepared transaction receives an HMAC-authenticated receipt binding the baseline, patch hash, verification evidence and fingerprint, trajectory digest, WAL digest, and access sets. `tx commit` verifies that receipt against durable state; `mca tx receipt` renders its non-source evidence. This is local tamper evidence under the private machine key, not a portable signature or proof that another machine should trust.

If the process stops after a complete tool checkpoint, resume with the same model and verification configuration:

```bash
mca tx resume TRANSACTION_ID --model deepseek --check tests "pytest -q"
mca tx abort TRANSACTION_ID
```

Transaction metadata, checkpoints, patches, and worktrees live under the private application state directory. Keep that directory outside the source repository. `commit` protects against observed pre-commit conflicts; it is not a filesystem-wide lock against another process racing the short check/apply interval.

Run the two-path demonstration without an API key:

```bash
mca tx demo
```

It proves that the successful source remains unchanged before commit, then injects a concurrent user edit into a second repository and shows that commit is refused without overwriting it.

## Enforced controls and limits

- New runs and chats reject dirty Git worktrees by default; arbitrary shell access is disabled by default.
- Structured file operations are confined to the resolved workspace, and `/ask` has a runtime read-only allowlist.
- User-configured authoritative verification—legacy `--test-command` or named `--check` entries—must pass against the current workspace fingerprint before submission. A recognized zero-test result is rejected by default, and resume invalidates earlier verification.
- Undo uses a private, HMAC-authenticated journal and rejects post-edit conflicts unless explicitly forced.
- `--allow-zero-tests` explicitly weakens verification by allowing a recognized zero-test result to satisfy the gate. `--sandbox none`, `--allow-shell`, `--allow-dirty`, `--yes`, and force/legacy Undo options also deliberately weaken protections; `--sandbox auto` fails closed if no usable backend is found.
- Native Windows supports the Agent runtime, structured tools, `mca demo`, and transactions. Local commands use `cmd.exe`; `--sandbox auto` requires Docker because Windows has no built-in native isolation backend. `--sandbox none` is an explicit unisolated opt-out. macOS uses `sandbox-exec`; Linux uses `bwrap` or Docker when available.

Backend boundaries differ:

| Backend | Enforced boundary | Important limit |
| --- | --- | --- |
| Linux `bwrap` | Unshares namespaces and keeps the host root read-only. The workspace and executor runtime tree are the only writable host paths; private writable tmpfs mounts provide `/run`, `/tmp`, and home, alongside private `/dev` and fresh `/proc` views. | Relies on the host kernel and installed Bubblewrap; its PID namespace strengthens descendant containment but is not a guarantee against a compromised host. |
| macOS `sandbox-exec` | Denies network and default writes, hides the real home except for a workspace below it, and limits writes to the workspace plus an executor-owned private runtime tree used for `HOME` and `TMPDIR`; shared `/tmp` and `/private/tmp` are not writable. | It is an OS policy profile, not a PID namespace, cgroup, or container boundary, and Apple may deprecate or restrict it. |
| Docker | Uses no network, a read-only/capability-free container, resource limits, a single writable workspace bind, and a private size-limited `/tmp`; on POSIX it maps the invoking numeric UID:GID and explicitly sets private `HOME`/`TMPDIR`, Python-bytecode, and Git environment values. | Relies on a trusted daemon, image, host kernel, and configuration. Every coding/test image must contain `/bin/sh`; `mca sandbox probe` additionally requires `python3`. |

Native process-group cleanup after timeout, interruption, or exceptions is best effort: a double-forked process can create a new session and escape that process group. Bubblewrap's PID namespace and Docker's container boundary provide stronger descendant containment, but no backend provides an absolute OS/process-containment guarantee.

These controls are defense in depth, not a guarantee that an untrusted repository, command, dependency, image, host, or provider is safe. Do not run it in a workspace containing production credentials. Read the complete [security policy](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/SECURITY.md) before use.

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
src/mini_code_agent/locking.py       POSIX/Windows transaction file locks
src/mini_code_agent/security.py      Path and secret protections
src/mini_code_agent/cli.py           CLI and state/configuration handling
tests/                               Deterministic test suite
evals/                               Offline evaluation baseline
```

## Develop and validate a checkout

Cloning the repository and using an editable install are contributor workflows; follow [CONTRIBUTING.md](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CONTRIBUTING.md) for the source setup. In that activated development environment, run:

```bash
pytest -q
python -m pip check
python -m evals.run_evals --json
mca doctor --sandbox none
mca demo
```

`mca doctor --sandbox none` is a read-only configuration smoke test and intentionally reports an isolation warning. Native Windows runs both demos in CI; use Docker for isolated command execution or WSL2 when POSIX tooling is required by the target repository. For release expectations, see [CHANGELOG.md](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/CHANGELOG.md).
