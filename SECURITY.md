# Security Policy

mini-code-agent-langgraph is a compact reference runtime for studying and extending coding-agent safety controls. Its controls reduce accidental damage and constrain model actions, but they do not make arbitrary repositories, commands, dependencies, containers, or model providers trustworthy.

中文摘要：本项目采用最小权限、验证门、私有状态、HMAC 认证撤销和 fail-closed 命令隔离作为纵深防御；它不是绝对安全沙箱。请勿在包含生产凭证或其他敏感资产的工作区中运行不受信任的代码。

## Supported versions

| Version | Security fixes |
| --- | --- |
| `0.3.x` | Supported |
| `0.2.x` and earlier | Not supported |

Security fixes are applied to the latest `0.3.x` release. Older trajectory and Undo formats may be readable for inspection, but unsafe legacy Undo data is not trusted for writes unless the user explicitly opts in.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through [GitHub Security Advisories](https://github.com/wusuiling-if/mini-code-agent-langgraph/security/advisories/new). Do not open a public issue or discussion before a fix or coordinated disclosure is available.

Include, when possible:

- the affected release or commit;
- operating system, Python version, selected sandbox, and Docker version/image if applicable;
- a minimal reproducer using disposable data and test credentials;
- the expected security boundary and the observed bypass;
- impact on workspace files, credentials, host processes, trajectory data, or Undo integrity;
- whether the issue requires `--sandbox none`, `--allow-shell`, `--allow-dirty`, `--yes`, `--force`, or `--allow-legacy-unsafe`.

Never include a real API key, access token, private repository content, or an unredacted trajectory in a report. Rotate any credential that may have been exposed before submitting the report.

## Threat model

### Inputs treated as untrusted

- model responses and tool arguments;
- repository text that may contain prompt injection;
- paths, patches, search expressions, and command output;
- interrupted or partially completed agent runs;
- trajectory files received from another machine or user.

### Assets the runtime attempts to protect

- files outside the resolved `--cwd` workspace;
- pre-existing user changes inside a Git worktree;
- API keys and other secret-like environment values;
- the integrity and confidentiality of private state and Undo source content;
- the host from accidental, lingering, or unconstrained command execution;
- the integrity of the verify-before-submit decision.

### Trust assumptions

- the local operating-system account, Python interpreter, installed dependencies, and selected container runtime are trusted;
- the user deliberately selects the workspace, provider, test command, sandbox, and high-risk flags;
- the configured test command is an authoritative check chosen by the user;
- source sent to the selected model provider is permitted to leave the machine under that provider's terms;
- Docker isolation relies on a trusted Docker daemon, image, host kernel, and configuration.

## Enforced controls

- Structured file tools resolve paths inside the workspace and reject symbolic-link traversal for protected operations.
- Dirty Git worktrees are rejected by default so existing work is not silently mixed with agent changes.
- Arbitrary shell access is disabled by default. `/ask` mode has a runtime read-only allowlist.
- `run` and coding-enabled `chat` sessions (those started with `--test-command`) probe the requested command-isolation backend and fail closed when none works. An `/ask`-only chat started without `--test-command` skips the probe because it cannot execute tests, shell commands, or coding tools. `--sandbox none` is an explicit opt-out.
- `mca sandbox probe` uses disposable data without contacting a model provider or target repository. Native backends must read an exact host sentinel and return a reserved evidence exit only when mutation is blocked by `EPERM`, `EACCES`, or `EROFS`; Docker must not see the host sentinel and must separately return that evidence exit only when `statvfs("/")` reports `ST_RDONLY`. Other positive exits, negative executor returns, timeouts, exceptions, missing preconditions, and an unavailable canonical `/var/tmp` base fail the probe. A candidate that resolves to or below the private `/tmp` mount is rejected. `bwrap` and Docker must hide the controlled and known host Unix sockets, while `sandbox-exec` may expose them only when connection is denied. Network isolation requires the process to be unable to obtain or use an outbound route to a TEST-NET address (tested by UDP `connect` without sending a packet) and denial of a controlled loopback TCP connection. It rejects `--sandbox none`; a passing probe is evidence for these checks only, not a general proof of safe execution.
- Linux `bwrap` unshares namespaces and keeps the host root read-only. The workspace and executor-owned runtime tree are the only writable host paths; private writable tmpfs mounts provide `/run`, `/tmp`, and home, alongside private `/dev` and fresh `/proc` views.
- macOS `sandbox-exec` denies network and default writes, hides the real home except for a workspace below it, and permits writes only to that workspace and the private runtime tree used for `HOME` and `TMPDIR`. Shared `/tmp` and `/private/tmp` are not writable.
- Docker runs without network or capabilities, with a read-only root, resource limits, one writable workspace bind, and a private size-limited `/tmp`. On POSIX it maps the invoking numeric UID:GID and explicitly sets private `HOME`/`TMPDIR`, Python-bytecode, and Git environment values.
- Native Windows is limited to informational CLI/configuration paths in `0.3.x`; run the full Agent runtime and structured file tools from WSL2/Linux.
- A successful test is bound to the current workspace fingerprint. Fingerprint-changing operations, a failed authoritative `run_tests`, and resume invalidate stale verification.
- Resume starts from a complete tool boundary and requires fresh verification before submission. The new-run dirty-worktree gate is not a substitute for reviewing or stashing extra changes before resume.
- Private Undo journals are stored with restrictive permissions and authenticated with HMAC over the trajectory/workspace/path/content relationship. Undo checks for post-edit conflicts before writing.
- Child commands have time and output bounds. Timeout, Ctrl-C, SIGTERM, and exception paths attempt to reap the process group and the Docker container created for that invocation.
- Provider environments are narrowed and secret-like values are redacted from observations and trajectories on a best-effort basis; trajectories remain sensitive and are never considered safe to publish by default.

## Non-goals and known limits

The project does **not** guarantee:

- safe execution of a deliberately malicious repository, build script, test suite, dependency, container image, kernel exploit, or compromised Docker daemon;
- prevention of all prompt injection or every unsafe semantic change proposed by a model;
- confidentiality from a model provider after content is intentionally sent to that provider;
- correctness or completeness of user-supplied tests;
- recovery of every external side effect when a process is killed between tool boundaries;
- complete descendant-process containment on native host backends: process-group cleanup is best effort, and a double-fork can create a new session. Bubblewrap's PID namespace and Docker's container boundary provide stronger containment, but `sandbox-exec` is not a PID namespace, cgroup, or container boundary;
- availability against denial-of-service, resource exhaustion outside configured limits, or provider outages;
- protection after the local account, Python environment, dependency chain, local HMAC key material, or host is compromised.

Flags such as `--sandbox none`, `--allow-shell`, `--allow-dirty`, `--yes`, `--force`, and `--allow-legacy-unsafe` deliberately weaken one or more controls. Use them only in disposable, credential-free workspaces after reviewing the consequence.

Docker isolation additionally assumes the selected image is trusted. Every image used for coding or tests must provide `/bin/sh`; `mca sandbox probe` additionally requires `python3`. The default `python:3.11-slim` image provides both. A normal coding run may use a different pre-pulled image only if it still provides `/bin/sh`.

## Handling secrets and artifacts

- Prefer the private config file created by `mca init`; on POSIX systems it is expected to be a regular, user-owned `0600` file.
- Do not place production credentials, SSH keys, signing keys, or unrelated sensitive data under `--cwd`.
- Treat every trajectory as sensitive because it may contain source, prompts, file reads, and command output. Review and redact it, diffs, crash logs, and diagnostic output before sharing; redaction is defense in depth, not proof that every secret format was removed.
- `mca doctor` checks secret-file metadata without reading that file's contents, and checks only in-process key presence without printing values; a key stored solely in the private env file is intentionally not verified by doctor. Its sandbox check is static PATH discovery, while `run` and coding-enabled `chat` perform the authoritative usability probe; `/ask`-only chat skips it.
- Use short-lived test credentials for reproductions and rotate anything copied into a terminal transcript, issue, or chat.

## Security-sensitive changes

Changes to workspace path resolution, symlink handling, command execution, sandbox selection, subprocess environments, approval modes, workspace fingerprints, verification state, checkpoint parsing, trajectory serialization, secret redaction, or Undo authentication require focused security tests and explicit threat-model review. See [CONTRIBUTING.md](CONTRIBUTING.md) for the expected review checklist.
