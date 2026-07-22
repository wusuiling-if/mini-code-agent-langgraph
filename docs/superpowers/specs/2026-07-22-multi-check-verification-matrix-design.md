# Multi-Check Verification Matrix Design

## Goal

Add a named verification matrix without weakening the project's central trust
contract: every successful check must have observed the same fingerprinted
workspace state, and submission must remain bound to that exact state.

This is the first of two independent changes. This specification covers only
the runtime multi-check feature. A later specification and pull request will
define an agent-neutral TrustBench protocol and adapter package.

## Decisions and Alternatives

The selected design is an executor-owned strict transaction:

- the user configures one or more named checks;
- the model receives one argument-free `run_tests` tool;
- the executor preflights the complete matrix, obtains one approval, and runs
  checks serially using the existing sandbox and process lifecycle;
- a fingerprint captured immediately before execution becomes the matrix
  fingerprint;
- the workspace must still have that fingerprint immediately before and after
  every check;
- one aggregate result reaches the existing scalar `VerificationGate`.

This preserves the current agent/executor boundary and avoids persisting a
per-check ledger in LangGraph state.

Two alternatives were rejected:

1. A gate-owned per-check ledger would permit selective reruns, but it would
   spread check names and fingerprints through tool schemas, agent and chat
   state, checkpoints, resume migration, and trajectory compatibility.
2. Running checks against a copied or read-only workspace would provide a
   stronger execution boundary but would break common build systems that need
   writable caches or generated artifacts and would add significant copying,
   path, and cross-platform complexity.

Composing commands into one shell expression was also rejected because it
would lose structured names, independent timeouts, cleanup, diagnostics, and
zero-test evidence.

## Scope

Included:

- repeatable named checks for `mca run` and `mca chat`;
- strict same-fingerprint verification for matrices;
- structured, redacted per-check evidence;
- backward compatibility for the existing `--test-command` and single-command
  executor path;
- deterministic unit, integration, architecture, resume, and CLI coverage;
- user and security documentation.

Excluded:

- automatic check discovery;
- model-selected commands or check names;
- parallel check execution;
- selective reruns;
- per-check timeout or zero-test CLI overrides;
- new dependencies;
- changes to fingerprint coverage or ignore rules;
- changes to sandbox backend selection;
- renaming or exposing a second model tool;
- TrustBench extraction or third-party agent adapters;
- Windows runtime support.

## CLI Contract

The repeatable option consumes a name and one shell command:

```bash
mca run "Fix the issue" \
  --model deepseek \
  --check tests "pytest -q" \
  --check lint "ruff check ." \
  --check types "pyright"
```

`--test-command` remains supported:

```bash
mca run "Fix the issue" \
  --model deepseek \
  --test-command "pytest -q" \
  --check lint "ruff check ."
```

Normalization rules:

- `--test-command COMMAND` becomes the reserved check name `tests`;
- legacy `tests` runs first, followed by explicit checks in declaration order;
- explicit names must match `[a-z][a-z0-9_-]{0,31}`;
- names are lowercase; uppercase names are invalid rather than normalized;
- blank commands, duplicate names, an explicit `tests` combined with
  `--test-command`, and more than 16 total checks are startup errors;
- `mca run` requires at least one of `--test-command` or `--check`;
- `mca chat` remains read-only without either option and enables `/code` only
  when at least one check is configured;
- resume uses the checks supplied on the new invocation and never trusts
  commands recovered from a trajectory.

Check commands are trusted user configuration. The model cannot supply,
override, select, reorder, or skip them.

## Components and Boundaries

### `checks.py`

This new standard-library-only module owns configuration validation and the
strict transaction algorithm. It defines immutable values conceptually
equivalent to:

```python
@dataclass(frozen=True)
class VerificationCheck:
    name: str
    command: str


@dataclass(frozen=True)
class VerificationCheckEvidence:
    name: str
    returncode: int
    duration_ms: int
    tests_run: int | None = None
    exception_info: str = ""
    blocked: bool = False
    approved: bool = True
```

The runner receives injected callbacks for command execution and fingerprint
capture. It does not import `BashExecutor`, LangGraph, model classes, or CLI
state.

### `BashExecutor`

`BashExecutor` accepts an ordered sequence of `VerificationCheck` values in
addition to the existing optional `default_test_command`. The legacy argument
is normalized to `tests` when a matrix is constructed.

The executor retains responsibility for:

- dangerous-command preflight;
- user confirmation;
- selected sandbox use;
- environment narrowing and redaction;
- per-command timeout and process cleanup;
- zero-test recognition;
- output bounds;
- converting the matrix result into `ToolResult`.

Each check uses a fresh invocation of the existing command execution path. The
runner does not compose a shell command or reuse a persistent shell.

### Tool and verification state

The model-facing tool remains the argument-free `run_tests`. Its description
changes to say that it runs every user-configured verification check. Keeping
one tool avoids ambiguous tool selection and preserves existing scripted and
trajectory contracts.

`ToolResult` gains an additive `verification_checks` collection containing
redacted `VerificationCheckEvidence`. Existing fields remain present. The
`ToolExecutor` protocol, `VerificationGate`, workspace fingerprint API, and
checkpoint schema remain unchanged.

For the exact legacy configuration containing only `--test-command`, the
existing single-command code path and top-level result fields remain
compatible. A check configured through `--check`, or any configuration with
multiple checks, uses strict matrix semantics.

## Execution and Fingerprint Data Flow

1. Parse and validate all CLI checks before loading the model or starting a
   sandbox.
2. Preflight every command against the existing safety policy. If any command
   is blocked, execute none of them.
3. Present the complete ordered matrix for one confirmation. If confirmation
   is declined, execute none of it.
4. Capture fingerprint `F0` immediately before the first check.
5. Before each check, capture the workspace and require equality with `F0`.
   This detects concurrent external changes between checks.
6. Execute the check serially through the configured sandbox.
7. Capture the workspace again and require equality with `F0` regardless of
   the command return code.
8. Record bounded, redacted evidence. Continue after an ordinary nonzero check
   so the user receives the complete failure set.
9. Stop immediately after a fingerprint change, fingerprint capture error,
   timeout, interruption, sandbox lifecycle error, policy error, or approval
   error. These failures make continued execution unsafe or misleading.
10. Return aggregate success only when every configured check succeeded,
    recognized zero-test policy was satisfied, and every fingerprint comparison
    matched `F0`.
11. `execute_tool_batch` captures the normal post-tool fingerprint. The existing
    `VerificationGate.record_test()` binds aggregate success to it. Any later
    edit, shell mutation, resume, or fingerprint error invalidates the evidence
    exactly as it does today.

The existing `WorkspaceFingerprinter` exclusions remain authoritative. A check
may write ignored caches, but writing a fingerprinted generated file fails with
`WorkspaceChangedDuringVerification`. Users must run generators before the
verification matrix and then verify the resulting stable workspace.

## Result and Output Contract

For a matrix, `run_tests` prints a compact ordered summary such as:

```text
CHECK  STATUS  EXIT  DURATION
tests  PASS    0     820ms
lint   FAIL    1     140ms
types  PASS    0     510ms
```

Bounded, redacted output sections may follow for failed checks. Successful
output is not repeated unless needed for recognized test-count evidence.

Structured evidence contains no raw command strings or full command output.
The top-level command value uses a fixed marker for matrices rather than a
joined command. Trajectories may store the additive evidence because it is
redacted and bounded, but they never store the trusted matrix configuration.

Aggregate behavior:

- return code `0` means every check passed on `F0`;
- ordinary check failure returns nonzero with `VerificationCheckFailed`;
- mutation returns `WorkspaceChangedDuringVerification` and identifies the
  responsible check name without exposing its command;
- a fingerprint capture failure remains `WorkspaceFingerprintError`;
- a policy-blocked matrix is marked blocked and runs nothing;
- rejected confirmation has `approved=False` and runs nothing;
- recognized zero-test output fails the responsible check unless the existing
  global `--allow-zero-tests` weakening flag is present;
- model-supplied arguments are discarded exactly as for the current
  authoritative test command.

## Failure and Recovery Semantics

Ordinary check failures do not mint verification and block submission. The
agent may edit the workspace and call `run_tests` again, at which point a new
matrix transaction and new `F0` are created.

If a check mutates a fingerprinted path, the mutation is not automatically
reverted. The result names the check and blocks submission. The workspace is
authoritative; the agent or user must inspect and deliberately repair or accept
the change before rerunning the complete matrix.

Resume always clears passing evidence and requires a fresh complete matrix.
Changing the configured matrix on resume is allowed because the new CLI
configuration is authoritative; no prior result is reusable.

## Testing Strategy

Implementation follows red-green-refactor. Each behavior begins with a focused
failing test whose expected failure demonstrates the missing feature.

CLI coverage:

- check-only, legacy-only, and combined parsing;
- order preservation and `tests` normalization;
- invalid, duplicate, reserved, blank, and over-limit configurations;
- run requirement and chat `/code` availability;
- CLI import/startup architecture remains lightweight.

Executor and transaction coverage:

- deterministic serial order and one approval;
- complete preflight before any execution;
- all-pass aggregate and multiple ordinary failures;
- strict mutation detection before and after every check;
- concurrent external mutation detection;
- fingerprint capture failure, timeout, interruption, sandbox error, zero-test
  result, output bounds, and secret redaction;
- each command uses the selected sandbox and existing cleanup path;
- exact legacy-only behavior remains compatible.

Agent and lifecycle coverage:

- two passing checks permit submission;
- one failed check blocks submission;
- a later edit makes the aggregate stale;
- same-assistant-message check, edit, and submit remains blocked;
- resume discards prior matrix evidence;
- chat returns to `/ask` after a verified coding turn;
- old trajectory and fake `ToolExecutor` tests remain valid.

Regression coverage:

- the existing 11-case `verified-patch-v0.3.2` suite remains 11/11 without
  changing its exact 50-call oracle;
- one separate end-to-end matrix security test covers pass, mutation refusal,
  repair, rerun, and submit without changing the published v0.3.2 baseline;
- full pytest, package build/install, offline eval, sandbox integration, CLI
  smoke, dependency audit, and workflow-policy checks remain green.

## Documentation and Release

Update `README.md`, `README.zh-CN.md`, `SECURITY.md`, CLI help, and
`CHANGELOG.md` with:

- repeatable examples;
- the strict same-fingerprint guarantee;
- the legacy compatibility rule;
- the fact that fingerprinted build outputs make a matrix fail;
- the 16-check and serial-execution limits;
- worst-case runtime of approximately check count multiplied by per-command
  timeout;
- explicit language that the matrix proves runtime-policy consistency, not test
  completeness or code correctness.

## Acceptance Criteria

- `--check NAME COMMAND` works in run and chat with zero new dependencies.
- A matrix cannot begin until every command passes preflight and one approval is
  granted.
- Every successful check is proven to have observed the same fingerprint `F0`.
- Any fingerprinted mutation during a matrix blocks verification with a stable,
  test-covered error.
- All configured checks must pass before submission; later changes invalidate
  the aggregate.
- Raw matrix commands, unbounded outputs, secrets, and trusted matrix
  configuration do not enter structured trajectory evidence. The legacy-only
  event shape remains unchanged.
- `--test-command` by itself remains backward-compatible; existing scripted
  benchmark contracts remain unchanged.
- The complete local and remote verification matrix passes before the feature
  is described as complete.

## Follow-Up Boundary

The later TrustBench pull request may consume the additive per-check evidence
through an MCA adapter capability. Its benchmark core must not import
`mini_code_agent`, and this runtime feature must not depend on the benchmark
package. Until an independent second adapter exists, TrustBench will be
described as an agent-neutral protocol with an MCA reference adapter, not a
validated cross-agent benchmark or security certification.
