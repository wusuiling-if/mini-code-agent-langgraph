# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
