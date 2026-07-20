# Runtime Decoupling and Fingerprint Performance Design

Date: 2026-07-17

## Goal

Improve runtime performance and reduce internal coupling without weakening the
workspace-verification boundary or changing the public CLI, trajectory schema,
tool behavior, or supported Python versions.

## Measured baseline

On the current repository and machine:

- the fingerprint covers 4,679 workspace entries;
- a full rehash took 2.519 seconds at the median of three fresh-cache runs;
- a cached scan took 0.440 seconds at the median of four warm runs;
- `mca --help` and `mca --version` started in about 0.21 seconds.

The main performance target is workspace fingerprinting, not CLI parser startup.

## Compatibility constraints

- Preserve the current CLI commands, flags, exit behavior, trajectory schema,
  checkpoint compatibility, and public package entry point.
- Preserve fingerprint coverage for regular files, directories, file modes,
  symlink targets, dependency directories, and local Git configuration/hooks.
- Preserve fail-closed behavior when fingerprinting or verification fails.
- Preserve the current `agent.py` and `executor.py` imports through compatibility
  re-exports where existing tests or callers use them.
- Keep Python 3.10 through 3.13 support.
- Add no runtime dependency.

## Architecture

The existing orchestration files mix reusable runtime policy with mode-specific
control flow. `chat.py` imports multiple internal functions from `agent.py`, and
both modes depend on the concrete `BashExecutor`. The refactor introduces three
one-purpose modules:

- `contracts.py` owns `ToolExecutor`, `Redactor`, and snapshot protocols plus the
  shared `ToolResult`, `ExecutedToolCall`, and `ToolBatchOutcome` value objects.
- `context.py` owns tool-call auditing, tool-call limits, and message compaction.
- `verification.py` owns workspace fingerprint adaptation, `VerificationGate`,
  tool-effect classification, and batch execution policy.

`agent.py` and `chat.py` remain mode-specific orchestrators. They depend on the
protocols and shared services rather than on each other or on `BashExecutor`.
`executor.py` remains the concrete file, command, sandbox, and process runtime.

The executor protocol includes `execute_tool`, `workspace_fingerprint`, and
`sandbox_status`, because both orchestrators use all three capabilities. The
snapshot protocol includes its deterministic digest and `diff` operation in
addition to the files mapping.

Compatibility imports in `agent.py` and `executor.py` keep the current import
surface working while new code imports from the focused modules directly.

## Fingerprint data flow

`WorkspaceFingerprinter` owns the metadata/content cache and captures a
`WorkspaceSnapshot`:

1. Resolve ignored artifacts once relative to the trusted workspace root.
2. Traverse one directory at a time in deterministic name order.
3. On POSIX, open child directories relative to their already trusted parent
   descriptor with `O_DIRECTORY` and `O_NOFOLLOW` where available.
4. Read entry metadata without following symlinks.
5. Reuse a cached content hash when the complete metadata signature is unchanged.
6. Otherwise open the regular file relative to its parent descriptor with
   `O_NOFOLLOW`, verify the opened descriptor is regular, and hash its bytes.
7. Read symlink targets without following them and include type/mode information
   for every supported entry.
8. Capture the selected local Git controls using the same safe mechanism.
9. Build the same deterministic `files` mapping and compute its digest once.

Platforms without the required descriptor operations use the existing safe path
implementation. Native Windows remains limited to the informational commands
already documented for the 0.3 series.

The verification layer consumes only the snapshot digest. Snapshot mappings stay
available for before/after diffs and compatibility.

## Security and error handling

- Directory and file descriptors are always closed with `try/finally` or context
  managers, including hashing and traversal failures.
- Symlinks are recorded but never traversed.
- An unreadable or concurrently removed entry is represented with the existing
  deterministic unreadable marker; an unexpected scanner failure propagates to
  the verification layer and blocks submission.
- Cache signatures retain mode, device, inode, size, nanosecond mtime, and
  nanosecond ctime, so metadata or content replacement invalidates the cache.
- No ignore rule is added for `.venv`, `node_modules`, or other dependency trees.
- Command execution, sandbox probing, approvals, and process cleanup do not move
  into the fingerprint component.

## Performance acceptance criteria

On the same repository and machine, using the checked-in benchmark command:

- full rehash median improves by at least 40 percent from 2.519 seconds;
- cached scan median improves by at least 25 percent from 0.440 seconds;
- CLI help/version median does not regress by more than 10 percent from 0.21
  seconds.

These values are recorded benchmark evidence, not CI pass/fail thresholds. CI
tests deterministic behavior and cache use instead of wall-clock timing.

## Testing

Development follows red-green-refactor:

- architecture test: importing `mini_code_agent.chat` does not import
  `mini_code_agent.agent`;
- compatibility tests: legacy imports resolve to the extracted definitions;
- protocol tests: a minimal fake executor can drive verification batch policy;
- parity tests: legacy and optimized snapshots agree for files, directories,
  modes, symlinks, dependency directories, and ignored artifacts;
- cache tests: unchanged files reuse hashes and changing one file rehashes that
  file while preserving other cache entries;
- failure tests: scanner errors keep submission fail-closed;
- existing hardening, process-cleanup, CLI, agent, and eval tests remain green;
- the offline evaluation, wheel build, and secret scan run before completion.

## Out of scope

- changing dependency-directory coverage;
- changing the trajectory or Undo journal format;
- replacing JSON checkpoints with an event store;
- adding a plugin system, TUI, MCP, or multi-agent runtime;
- changing provider behavior or model defaults.
