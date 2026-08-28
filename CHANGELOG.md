# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-08-28

### Added

- Added opt-in long-term conversation memory for `mca chat --memory local`, including private immutable event logs, bounded user- and workspace-scoped recall, evidence-bearing `/remember`, auditable `/forget` and `/correct`, and heuristic candidates that require explicit approval before becoming durable memory.
- Added HMAC-chained event and candidate ledgers, cross-store evidence verification, stable local user scope, optional semantic retrieval for chat, and automatic migration of valid legacy self-hashed conversation logs.
- Added offline `mca memory list/forget/correct/candidates`, verified plaintext backup/restore, and an explicit `mca memory purge --yes` path for irreversible removal of the full local store and evidence.
- Added an eight-case production conversation-memory gate covering scope isolation, temporal controls, approval, credential refusal, tampering, backup/restore, and evidence binding; the aggregate memory release gate now runs nine deterministic suites.
- Added content-free transaction recovery audits with per-attempt duration, step and workspace progress, failure class/cause types, verification state, and cumulative resume summaries. Harbor metadata now separates successful model calls from attempted and failed model requests.
- Added explicit `--streaming` and `--reasoning-effort` model transport controls. Transaction manifests pin both values across resume, and OpenAI-compatible runs remain on Chat Completions.
- Added a fixed-model Harbor transport preflight that requires a long-context streamed tool call before paid execution, plus a mini-swe-agent streaming shim so both comparison arms use Chat Completions with the same reasoning effort.

### Fixed

- Avoided appending duplicate resume notices when a model request fails before producing a new checkpoint, keeping repeated connection recovery context bounded without weakening fresh-verification requirements.
- Avoided the proxy's stalled non-streaming route and its incompatible Responses API route in the pinned Harbor comparison.

### Changed

- Added a correctness-oriented Ruff gate to contributor and release CI, and made the release workflow rerun the full test suite plus both deterministic evaluation gates before building artifacts.
- Added an 80% repository coverage floor plus formatter and type-check gates for the new memory trust boundary; full-repository formatter and typing adoption remain staged to avoid unrelated churn.
- Updated the supported-security-version and native Windows contributor guidance for the current 0.6.x runtime.

### Security

- Kept recalled conversation memory advisory and unable to grant tool authority, rejected obvious credentials from durable admission, and prevented heuristic extraction from silently writing active memory.
- Bound raw conversation and candidate rows to a private local HMAC key, sequence, log identity, and previous record; `mca memory verify` now checks those chains and binds SQLite sources and candidate approvals back to authenticated events.
- Made backups fail closed on invalid stores, unsafe archive paths, duplicate or unmanifested entries, digest mismatches, and non-private state; backups remain plaintext sensitive artifacts rather than encryption.
- Kept private Harbor environments and raw benchmark artifacts out of the source worktree by default to reduce accidental publication of sensitive prompts, trajectories, and workspace data.

## [0.5.0] - 2026-08-26

### Added

- Added an opt-in, evidence-bound local memory path for transactional runs. `mca tx run --memory local` retrieves bounded same-project advisory context, and a successful conflict-checked commit forms authenticated workflow and verified-repair memories from the transaction receipt.
- Added the read-only `mca memory status/search/show/sources/verify/health` CLI, a host-neutral `memory_core` package, stable Git project identity, temporal lifecycle state, SQLite FTS retrieval, authenticated evidence and edges, capacity retirement, and an optional OpenAI-compatible embedding candidate route with private caching and lexical fallback.
- Added deterministic memory gates for core integrity, architecture comparison, receipt-to-memory formation, host portability, 120-session longitudinal retention, long-conversation reading after explicit ingestion, outcome-control experiments, and production-loop intervention wiring. `python -m evals.run_memory_suite` runs all eight without model calls and emits one source-bound report.
- Added replayable conversation mutations, canonical checkpoints, continuity budgets, and quarantined SillyTavern/Tavern Helper import adapters as experimental portability work; they are not connected to production `mca chat` extraction.
- Added a Harbor 0.22 installed-agent adapter and a machine-readable 25-task SWE-bench Verified pilot for fixed-model harness comparison against `mini-swe-agent==2.1.0`. The protocol pins the dataset content hash, `openai/gpt-5.6-sol`, and its provider endpoint; the launcher supports a paired one-task smoke and rejects model/task drift, and no benchmark score is claimed in this release.
- Added normalized per-run model token and call counts to private trajectories/checkpoints so public harness reports can compare usage as well as task reward.

### Changed

- Kept memory disabled by default and limited production writes to authenticated, committed transaction evidence. Existing `run`, `chat`, and `tx` behavior remains unchanged unless `--memory local` is selected.
- Separated the host-neutral memory contracts from MCA-specific transaction, project-identity, semantic-provider, and agent-context adapters.

### Fixed

- Fixed generated `mca tx resume` commands after `StepLimitExceeded` so the cumulative step ceiling increases beyond the saved checkpoint instead of immediately repeating the same failure.
- Preserved transaction-only memory retrieval audit metadata across open failures and resumed trajectories; cumulative model usage and elapsed duration already recorded before the checkpoint are retained as well.

### Security

- Memory retrieval authenticates cards and current temporal state after hard scope, authority, validity, and lifecycle filtering. Structured retrieval audits store hashes and decisions rather than memory content.
- Injected memory is advisory and cannot grant tool authority. External or derived evidence cannot launder higher authority, and post-commit memory indexing failure cannot make a successful source commit appear rolled back.
- Automatic free-conversation extraction, remote script execution, and learned outcome policies remain outside the production boundary. The outcome-aware controller and real-model studies are experiments, not release claims.

## [0.4.0] - 2026-07-31

### Added

- Added `mca tx run/resume/status/receipt/commit/abort`, which executes coding runs in an isolated Git worktree, persists a tool-access WAL and read/write sets, and exposes an explicit verified `prepare` followed by conflict-checked `commit`.
- Added HMAC-authenticated prepared-patch receipts binding baseline, patch, verification, trajectory, WAL, and access-set evidence, plus a no-key `mca tx demo` for successful commit and concurrent-conflict refusal.
- Added native Windows command execution through `cmd.exe`, process-tree cleanup through `taskkill`, cross-platform transaction locks, and Windows CI coverage for both no-key demos.

### Changed

- Split the framework-independent transaction state machine from the Agent tool adapter. `transaction.py` no longer imports LangGraph or Agent contracts, while `transaction_adapter.py` owns tool-call auditing.
- Generalized `BashExecutor` internally to the platform-aware `CommandExecutor`; the former name and the `bash` tool identifier remain compatibility aliases.

### Fixed

- Preserved Windows CRLF normalization during isolated Git inspection so a clean worktree is not rejected when the index stores normalized LF content.
- Preserved nested argument quotes through `cmd.exe /s /c`, and stored receipt keys with binary I/O so Windows verification commands and tamper evidence do not fail from text-mode reinterpretation.

### Security

- Transaction commit now fails closed when the source `HEAD` or whole-workspace snapshot changed after begin, when the prepared workspace changed after verification, when workspace changes cannot be represented by the prepared Git patch, or when the private prepared patch fails its recorded SHA-256 integrity check.
- Transaction state must remain outside the source repository. The initial transaction protocol intentionally rejects all concurrent source-workspace changes; recorded read/write sets are audit evidence and do not yet relax conflict granularity.
- Receipts provide local tamper evidence under private machine key material; they are not portable third-party attestations and do not prove test completeness or semantic correctness.
- Native Windows has no built-in strong isolation backend. `--sandbox auto` uses Docker when available; `--sandbox none` is an explicit unisolated opt-out. Structured file operations use containment and symlink checks but cannot provide POSIX descriptor-relative race resistance against concurrent reparse-point changes.

## [0.3.4] - 2026-07-22

### Added

- Added ordered named `--check NAME COMMAND` verification matrices with at most 16 serial checks and additive, bounded, best-effort-redacted per-check evidence.

### Changed

- Refused verification when any named check, or the backward-compatible single `--test-command`, leaves a fingerprinted workspace file persistently changed; generators must run before verification.

### Compatibility

- Preserved stable `--test-command` output and event fields while applying the same fail-closed persisted-mutation rule to the legacy single-check path.
- Added focused CLI, workflow-policy, hygiene, and end-to-end coverage for named matrices and strict mutation refusal.

### Security

- Documented that boundary fingerprint captures detect persisted changes but are not immutable snapshots, and that trajectory redaction is best effort and trajectories remain sensitive.
- Kept the TrustBench boundary unchanged: this release adds no TrustBench extraction, adapters, or benchmark dependency, and the existing v0.3.2 offline benchmark contract remains unchanged.
- Kept the established end-to-end recovery test outside that unchanged v0.3.2 benchmark boundary.

## [0.3.3] - 2026-07-22

### Added

- Added `mca sandbox probe`, a provider-free disposable capability check for workspace writes, backend-specific outside-write and Unix-socket boundaries, denial or unavailability of a usable outbound route, and denial of a controlled TCP connection, plus dedicated real-backend CI that runs automatically on pushes and pull requests.

### Changed

- Hardened Linux `bwrap` with fully unshared namespaces and a read-only host root. The workspace and executor runtime are the only writable host paths; private `/run`, `/tmp`, and home tmpfs mounts are also writable inside the sandbox, with private `/dev` and fresh `/proc` views.
- Limited macOS `sandbox-exec` writes to the workspace and its private runtime tree, removing shared `/tmp` and `/private/tmp` write exceptions.
- Changed Docker execution on POSIX to use the invoking numeric UID:GID, a private size-limited `/tmp`, and explicit private `HOME`/`TMPDIR`, Python-bytecode, and Git environment values.

### Security

- Added backend-specific boundary tests and documented that native process-group cleanup remains best effort: a double-fork can escape into a new session, and `sandbox-exec` does not provide PID-namespace, cgroup, or container-equivalent descendant containment.
- Made probe results require reserved, cause-specific evidence exits: native mutation accepts only `EPERM`, `EACCES`, or `EROFS`, while Docker verifies the root mount's `ST_RDONLY` flag instead of inferring read-only state from a failed write. Other positive exits, negative executor returns, timeouts, exceptions, missing sentinel preconditions, and unavailable or `/tmp`-aliased host temp bases fail closed.
- A passing capability probe demonstrates only its bounded checks; it is not a guarantee that an arbitrary repository, command, dependency, image, daemon, kernel, or host is safe.

### Compatibility

- Preserved the existing `auto` selection order and explicit backend names. Every Docker image used for coding or tests must provide `/bin/sh`; `mca sandbox probe` additionally requires `python3`. Normal coding runs may still use another pre-pulled custom image when it satisfies the `/bin/sh` requirement.
- POSIX Docker workspaces now produce files as the invoking host UID:GID rather than container root; images that require root must be adjusted or replaced.

## [0.3.2] - 2026-07-20

### Added

- Added an eleven-case deterministic verified-patch benchmark covering single- and multi-file repair, explanation-only work, recovery, premature and stale verification, failed and zero-test refusal, disabled shell access, checkpoint resume, and authenticated Undo.
- Added weekly Dependabot updates, dependency review, `pip-audit`, CodeQL analysis, immutable action pins, least-privilege workflow permissions, and retained sanitized evaluation artifacts.

### Changed

- Required `mca run` callers to select both a real model and an authoritative test command explicitly; deterministic no-key use now goes through `mca demo`.
- Allowed `mca chat` to start without a test command only for read-only `/ask`; `/code` remains unavailable until authoritative verification is configured.

### Fixed

- Rejected missing test commands and recognized zero-test results as verification failures instead of accepting an empty test run; `--allow-zero-tests` is an explicit opt-out.
- Bound submission evidence to the current workspace fingerprint and exact benchmark tool contracts, including real tracked-diff evidence.

### Security

- Hardened CI and release workflows with explicit timeouts, concurrency controls, artifact checks, SHA-pinned actions, dependency auditing, and static analysis.

## [0.3.1] - 2026-07-20

### Changed

- Decoupled shared runtime contracts, context management, and verification orchestration from the LangGraph agent implementation while preserving lightweight CLI startup.
- Improved secure workspace fingerprint performance without narrowing the verification scope, with a reproducible local cold/warm benchmark.

## [0.3.0] - 2026-07-16

### Added

- Persistent `mca chat` sessions with runtime-enforced read-only `/ask` and explicit `/code` authorization modes.
- Checkpoint and resume support for one-shot runs and chat sessions, resuming only from complete tool boundaries.
- Private, HMAC-authenticated Undo journals with workspace/path/content binding and conflict-aware restoration.
- Dedicated DeepSeek provider support with tool-call `reasoning_content` round trips and explicit thinking-mode control.
- `mca doctor` read-only diagnostics for Python, workspace, Git, private state/config paths, provider configuration, and command-isolation availability without printing key values.
- A no-key `mca demo` that repairs a deterministic calculator fixture in a new temporary workspace without dirtying the clone.
- Deterministic offline behavior evaluations for a single-file fix, a no-change explanation, and recovery from a failed edit.
- Cross-platform configuration/state paths, private run artifacts, and explicit Docker image selection.

### Changed

- Submission is gated on an authoritative successful test bound to the current workspace fingerprint; subsequent changes and resume invalidate stale verification.
- Sandbox auto-selection probes actual backend usability and fails closed instead of silently executing without isolation.
- Informational CLI commands load without importing the LLM/agent runtime, and the CLI reports its package version.
- Tool output, search, file edits, context, state files, reasoning content, and tool-call batches have explicit resource limits.
- Provider credentials and base URLs are selected independently so DeepSeek and OpenAI environments are not mixed accidentally.
- Trajectory and chat outputs default to private user state rather than the target repository.

### Fixed

- Shell/test subprocess groups and per-invocation Docker containers are cleaned up after timeout, Ctrl-C, SIGTERM, and exception paths.
- Chat coding authorization no longer implies that a workspace modification occurred or that verification is already required.
- Multi-tool responses preserve one observation per tool call, including blocked and exceptional calls.

### Security

- Hardened workspace containment against path traversal, symbolic-link replacement, non-regular files, unsafe ownership, and broad secret-file permissions.
- Reduced child-process environments and expanded best-effort secret-like value redaction while documenting that trajectories remain sensitive.
- Added HMAC-authenticated, tamper-evident Undo integrity checks, resume schema validation, state-size limits, dirty-worktree protection, and trusted executable checks.
- Documented that controls are defense in depth rather than an absolute sandbox, and that the full Agent runtime requires macOS, Linux, or WSL2 in `0.3.x`.
