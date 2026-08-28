# Contributing

Thanks for helping improve mini-code-agent-langgraph. The project is intentionally scoped as a compact, security-first coding-agent reference runtime. Small, reviewable changes with deterministic tests are preferred over broad feature expansion.

Before proposing a TUI, web UI, MCP platform, multi-agent system, new public extension API, or a large runtime refactor, open a feature request so scope and security consequences can be discussed first.

## Development setup

Python 3.10 or newer is required.

```bash
git clone https://github.com/wusuiling-if/mini-code-agent-langgraph.git
cd mini-code-agent-langgraph
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

On Windows PowerShell, create and activate the environment with:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Native Windows supports the informational CLI, structured file tools, transactions,
and both no-key demos. Local commands use `cmd.exe`; strong command isolation requires
Docker, while `--sandbox none` is an explicit unisolated opt-out. WSL2 remains the
recommended environment for repositories and checks that require POSIX behavior.

## Local validation

Run the complete deterministic checks from macOS, Linux, or WSL2 before opening a pull request:

```bash
pytest -q
ruff check src tests evals benchmarks
ruff format --check src/mini_code_agent/conversation_ledger.py src/mini_code_agent/conversation_memory.py src/mini_code_agent/memory_backup.py src/mini_code_agent/cli.py evals/run_conversation_memory.py evals/run_memory_suite.py tests/test_conversation_memory_runtime.py tests/test_memory_release_suite.py
mypy src/mini_code_agent/conversation_ledger.py src/mini_code_agent/conversation_memory.py src/mini_code_agent/memory_backup.py
pytest -q --cov=src/mini_code_agent --cov=src/memory_core --cov-fail-under=80
python -m pip check
python -m evals.run_evals --json
python -m evals.run_memory_suite --json
mca doctor --sandbox none
mca demo
mca tx demo
```

`mca doctor --sandbox none` intentionally reports an isolation warning; it is a
read-only configuration smoke test. Both demos use temporary, credential-free
workspaces and do not call a model provider.

On native Windows, additionally exercise the platform-specific transaction and
command paths:

```powershell
mca --version
mca --help
mca doctor --sandbox none
python -m pytest -q tests/test_windows_compat.py tests/test_transaction.py
mca demo
mca tx demo
```

For a focused change, run the smallest relevant test first, then the full suite. Tests should use `tmp_path`, the scripted/mock model, and disposable credentials. Unit and default CI tests must not require network access, a real API key, a running Docker daemon, or modification of `examples/`.

## Change workflow

1. Start from a current branch and keep unrelated local changes out of the patch.
2. Add or update a failing test that demonstrates the behavior or regression.
3. Implement the smallest change that satisfies the test while preserving fail-closed defaults.
4. Run focused tests, the full `pytest -q` suite, and the offline evaluation.
5. Update README or other user documentation for changed commands, flags, platform behavior, state formats, or security boundaries.
6. Add a changelog entry for user-visible behavior, compatibility, or security changes.
7. Inspect `git diff --check` and the complete diff before submitting.

Do not commit API keys, `.env` files, private trajectories, Undo journals, fixture data copied from private repositories, or generated state. Use unmistakably fake placeholders in tests and documentation.

## Coding expectations

- Follow the existing typed Python and standard-library-first style.
- Prefer explicit limits, structured results, and deterministic error messages at trust boundaries.
- Keep informational CLI paths free of heavy LLM/runtime imports.
- Avoid shell parsing when an argv-based subprocess call is sufficient.
- Preserve current CLI behavior unless a documented compatibility change is intentional.
- Do not silently turn a failed security check into an unisolated fallback.
- Treat trajectories as sensitive even after best-effort redaction, and keep reversible source content in the private authenticated journal.

Ruff's correctness-oriented lint rules and the 80% repository coverage floor are
mandatory in CI. Ruff formatting and mypy are additionally mandatory for the v0.6
conversation-ledger, conversation-memory, and backup trust boundary listed above.
The older runtime has not yet completed a formatter or type-check migration, so avoid
unrelated bulk formatting and expand those gates deliberately when touching legacy code.

## Security review

Read [SECURITY.md](SECURITY.md) before changing a security boundary. Pull requests touching any of the following need focused positive and negative tests:

- workspace containment, path normalization, symlinks, file type, ownership, permissions, or atomic writes;
- shell/test execution, trusted executable discovery, subprocess environment, timeout, signal handling, process-tree cleanup, or Docker cleanup;
- sandbox probing, platform selection, or fail-closed fallback behavior;
- `/ask` and `/code` authorization, approvals, tool availability, or tool-call batching;
- workspace fingerprints, verification invalidation, submit gating, or authoritative verification execution;
- checkpoint/resume validation, trajectory size limits, redaction, state paths, HMAC keys, or conflict-aware Undo;
- provider selection, request payloads, reasoning round trips, and API-key handling.

For these changes, explain in the pull request:

- which protected asset and attacker-controlled input are involved;
- whether a failure is fail-open or fail-closed;
- which high-risk flags affect the result;
- which platforms and sandbox backends were exercised;
- how logs, fixtures, and test output were checked for secret exposure.

Report suspected vulnerabilities privately through [GitHub Security Advisories](https://github.com/wusuiling-if/mini-code-agent-langgraph/security/advisories/new), not through a public issue.

## Pull requests

Keep pull requests focused and describe:

- the problem and user-visible outcome;
- implementation scope and intentionally deferred work;
- exact validation commands and results;
- platform and sandbox coverage;
- threat-model impact, or why there is none;
- documentation and changelog updates;
- confirmation that no real secret or private artifact is included.

By contributing, you agree that your contribution is licensed under the repository's [MIT License](LICENSE).
