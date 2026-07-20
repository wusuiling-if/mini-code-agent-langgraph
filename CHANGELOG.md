# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
