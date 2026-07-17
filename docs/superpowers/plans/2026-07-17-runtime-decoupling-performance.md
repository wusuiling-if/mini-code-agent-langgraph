# Runtime Decoupling and Fingerprint Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the chat-to-agent and runtime-to-concrete-executor coupling while making secure workspace fingerprinting materially faster.

**Architecture:** Extract shared contracts, context handling, and verification policy into focused modules, leaving `agent.py` and `chat.py` as mode-specific orchestrators. Replace per-file safe-path reopening during fingerprint capture with a descriptor-relative scanner that preserves the same coverage and deterministic mapping, with the existing implementation as a fallback.

**Tech Stack:** Python 3.10–3.13, dataclasses, typing protocols, POSIX descriptor-relative filesystem APIs, LangChain messages, LangGraph, pytest.

## Global Constraints

- Preserve the current CLI commands, flags, exit behavior, trajectory schema, checkpoint compatibility, and public package entry point.
- Preserve fingerprint coverage for regular files, directories, file modes, symlink targets, dependency directories, and local Git configuration/hooks.
- Preserve fail-closed behavior when fingerprinting or verification fails.
- Preserve the current `agent.py` and `executor.py` imports through compatibility re-exports where existing tests or callers use them.
- Keep Python 3.10 through 3.13 support.
- Add no runtime dependency.
- Full rehash median must improve by at least 40 percent from 2.519 seconds on the reference machine.
- Cached scan median must improve by at least 25 percent from 0.440 seconds on the reference machine.
- CLI help/version median must not regress by more than 10 percent from 0.21 seconds on the reference machine.

---

### Task 1: Extract shared runtime boundaries

**Files:**
- Create: `src/mini_code_agent/contracts.py`
- Create: `src/mini_code_agent/context.py`
- Create: `src/mini_code_agent/verification.py`
- Create: `tests/test_architecture.py`
- Modify: `src/mini_code_agent/agent.py`
- Modify: `src/mini_code_agent/chat.py`
- Modify: `src/mini_code_agent/executor.py`

**Interfaces:**
- Produces: `ToolExecutor`, `Redactor`, `SnapshotLike`, `ToolResult`, `ExecutedToolCall`, and `ToolBatchOutcome` from `contracts.py`.
- Produces: `audit_tool_args`, `audit_tool_calls`, `limit_model_tool_calls`, `compact_messages`, and `_message_size` from `context.py`.
- Produces: `VerificationGate`, `capture_workspace_fingerprint`, and `execute_tool_batch` from `verification.py`.
- Preserves: the same names imported from `mini_code_agent.agent` and `ToolResult` imported from `mini_code_agent.executor`.

- [ ] **Step 1: Write failing architecture tests**

Add tests with these exact behaviors:

```python
def test_chat_import_does_not_load_agent_module():
    command = [
        sys.executable,
        "-c",
        "import sys; import mini_code_agent.chat; "
        "print('mini_code_agent.agent' in sys.modules)",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "False"


def test_legacy_runtime_exports_are_preserved():
    from mini_code_agent.agent import VerificationGate as LegacyGate
    from mini_code_agent.agent import compact_messages as legacy_compact
    from mini_code_agent.executor import ToolResult as LegacyToolResult
    from mini_code_agent.context import compact_messages
    from mini_code_agent.contracts import ToolResult
    from mini_code_agent.verification import VerificationGate

    assert LegacyGate is VerificationGate
    assert legacy_compact is compact_messages
    assert LegacyToolResult is ToolResult
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_architecture.py -q
```

Expected: the import-isolation test fails because `chat.py` imports
`mini_code_agent.agent`; imports of the new modules fail until they are created.

- [ ] **Step 3: Create the shared contracts**

Move `ToolResult` without changing its fields or `to_observation` behavior, then
define structural protocols:

```python
class Redactor(Protocol):
    def redact_text(self, text: str) -> str: ...
    def redact_data(self, value: Any) -> Any: ...


class SnapshotLike(Protocol):
    files: dict[str, str]


@runtime_checkable
class ToolExecutor(Protocol):
    cwd: Path
    redactor: Redactor

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolResult: ...
    def workspace_fingerprint(
        self, *, ignore_paths: set[Path] | None = None
    ) -> SnapshotLike | str: ...
```

Move `ExecutedToolCall` and `ToolBatchOutcome` to the same module. Import
`DEFAULT_OUTPUT_LIMIT` and `truncate_text` there so `ToolResult.to_observation`
stays byte-for-byte equivalent in behavior.

- [ ] **Step 4: Extract context and verification policy**

Move the message/audit functions and their private helpers from `agent.py` to
`context.py` without changing algorithms. Move the verification gate,
fingerprint adapter, tool-effect constants, blocked-result helpers, and
`execute_tool_batch` to `verification.py`. Type the executor argument as
`ToolExecutor`, not `BashExecutor`.

Import these definitions into `agent.py` to preserve compatibility re-exports.
Change `chat.py` to import them from `context.py` and `verification.py`. Change
`executor.py` to import and re-export `ToolResult` from `contracts.py`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_architecture.py tests/test_agent_cli.py tests/test_hardening.py -q
```

Expected: all selected tests pass and the subprocess prints `False` for the
agent-module import check.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/mini_code_agent/contracts.py src/mini_code_agent/context.py \
  src/mini_code_agent/verification.py src/mini_code_agent/agent.py \
  src/mini_code_agent/chat.py src/mini_code_agent/executor.py \
  tests/test_architecture.py
git commit -m "refactor: decouple shared agent runtime policy"
```

### Task 2: Optimize secure fingerprint capture

**Files:**
- Modify: `src/mini_code_agent/workspace.py`
- Modify: `src/mini_code_agent/executor.py`
- Create: `tests/test_workspace_fingerprint.py`
- Create: `benchmarks/benchmark_fingerprint.py`

**Interfaces:**
- Consumes: `SnapshotLike` through the existing executor method.
- Produces: `WorkspaceFingerprinter.capture(ignore_paths=...) -> WorkspaceSnapshot`.
- Produces: `WorkspaceSnapshot.fingerprint: str` while retaining `root`, `files`,
  `capture`, and `diff`.

- [ ] **Step 1: Write failing scanner tests**

Add deterministic tests:

```python
def test_snapshot_fingerprint_matches_legacy_digest(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    snapshot = WorkspaceSnapshot.capture(tmp_path, cache={})
    payload = json.dumps(
        snapshot.files, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert snapshot.fingerprint == hashlib.sha256(payload.encode()).hexdigest()


def test_capture_does_not_resolve_every_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for index in range(40):
        path = tmp_path / "pkg" / f"file-{index}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text(str(index), encoding="utf-8")
    calls = 0
    original = SafeWorkspace.resolve

    def counted(self, path):
        nonlocal calls
        calls += 1
        return original(self, path)

    monkeypatch.setattr(SafeWorkspace, "resolve", counted)
    WorkspaceSnapshot.capture(tmp_path, cache={})
    assert calls <= 4
```

Also test that a warm cache returns the same fingerprint, changing one file
changes it, directory modes and symlink targets remain covered, ignored artifacts
remain excluded, and a forced scanner failure propagates instead of returning a
trusted fingerprint.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_workspace_fingerprint.py -q
```

Expected: `WorkspaceSnapshot.fingerprint` is missing and the path-resolution
count exceeds four.

- [ ] **Step 3: Implement `WorkspaceFingerprinter`**

Add an owned cache and descriptor-relative POSIX scanner. The core opening flags
must be composed as follows:

```python
def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags
```

Open the root once, scan deterministic entry names, open child directories and
regular files relative to the trusted parent descriptor, use `os.fstat` before
hashing, and close every descriptor in `finally`. Never traverse a symlink.

Use the existing metadata signature:

```python
signature = (
    metadata.st_mode,
    metadata.st_dev,
    metadata.st_ino,
    metadata.st_size,
    metadata.st_mtime_ns,
    metadata.st_ctime_ns,
)
```

When descriptor-relative operations are unavailable, delegate to the current
`SafeWorkspace.iter_entries` and `_hash_entry` implementation.

Give `WorkspaceSnapshot` a lazily cached `fingerprint` property using the exact
legacy JSON serialization. Keep `WorkspaceSnapshot.capture(...)` as a compatible
class method that constructs a temporary `WorkspaceFingerprinter` around the
supplied cache.

- [ ] **Step 4: Inject the fingerprinter into `BashExecutor`**

Construct one `WorkspaceFingerprinter` in `BashExecutor.__init__` and delegate
`workspace_fingerprint` to it. Remove the bare cache from the executor. Update
`capture_workspace_fingerprint` to use `snapshot.fingerprint` when present and
retain the `files` fallback for third-party executors.

- [ ] **Step 5: Add a repeatable benchmark command**

Create `benchmarks/benchmark_fingerprint.py` with CLI options `--root` and
`--runs`. It must report JSON containing `entries`, `cold_seconds`,
`cold_median_seconds`, `warm_seconds`, and `warm_median_seconds`. Cold samples use
a fresh fingerprinter cache; warm samples reuse one fingerprinter. The script
must not enforce timing thresholds.

- [ ] **Step 6: Run focused tests and benchmark**

Run:

```bash
.venv/bin/python -m pytest tests/test_workspace_fingerprint.py \
  tests/test_hardening.py tests/test_process_cleanup.py -q
.venv/bin/python benchmarks/benchmark_fingerprint.py --root . --runs 5
```

Expected: all tests pass; full-rehash median is at most 1.511 seconds and warm
median is at most 0.330 seconds on the reference machine.

- [ ] **Step 7: Commit Task 2**

```bash
git add src/mini_code_agent/workspace.py src/mini_code_agent/executor.py \
  tests/test_workspace_fingerprint.py benchmarks/benchmark_fingerprint.py
git commit -m "perf: accelerate secure workspace fingerprinting"
```

### Task 3: Document and verify the integrated change

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_cli_launch.py`

**Interfaces:**
- Consumes: all interfaces produced by Tasks 1 and 2.
- Produces: documented benchmark command and startup regression coverage.

- [ ] **Step 1: Add a startup import regression assertion**

Extend the existing lightweight CLI subprocess test so `mca --help` still does
not load `langgraph`, provider adapters, `mini_code_agent.agent`, or
`mini_code_agent.workspace`.

- [ ] **Step 2: Run the focused CLI test and verify its current state**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli_launch.py -q
```

Expected: pass if lazy-loading remained intact; if it fails, treat it as a
regression and correct imports before continuing.

- [ ] **Step 3: Document the new module boundaries and benchmark**

Update the project-structure section to list `contracts.py`, `context.py`, and
`verification.py`. Add the exact benchmark command:

```bash
python benchmarks/benchmark_fingerprint.py --root . --runs 5
```

State that it scans the full verification scope and reports local evidence rather
than a portable CI threshold. Add an Unreleased changelog entry for runtime
decoupling and secure fingerprint performance.

- [ ] **Step 4: Run complete verification**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m evals.run_evals --json
.venv/bin/python -m pip wheel --no-deps . -w "$(mktemp -d)"
git diff --check
```

Expected: all tests pass, all three offline eval cases succeed and verify with no
unrelated changes, wheel build exits zero, and `git diff --check` is silent.

- [ ] **Step 5: Commit Task 3**

```bash
git add README.md CHANGELOG.md tests/test_cli_launch.py
git commit -m "docs: record runtime performance architecture"
```
