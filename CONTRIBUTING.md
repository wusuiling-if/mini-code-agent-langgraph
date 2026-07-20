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

Native Windows is tested only for informational CLI/configuration paths. Use WSL2/Linux for `run`, `chat`, `demo`, structured file tools, and isolated command execution in `0.3.x`.

## Local validation

Run the complete deterministic checks from macOS, Linux, or WSL2 before opening a pull request:

```bash
pytest -q
python -m pip check
python -m evals.run_evals --json
mca doctor --sandbox none
mca demo  # macOS, Linux, or WSL2
```

`mca doctor --sandbox none` intentionally reports an isolation warning; it is a read-only configuration smoke test. `mca demo` uses a temporary, credential-free workspace and does not call a model provider; skip it on native Windows and run it from WSL2 instead.

Native Windows validation is limited to the informational surface:

```powershell
mca --version
mca --help
mca doctor --sandbox none
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

There is currently no mandatory formatter or type checker configuration. Avoid unrelated formatting churn and make the patch easy to audit.

## Security review

Read [SECURITY.md](SECURITY.md) before changing a security boundary. Pull requests touching any of the following need focused positive and negative tests:

- workspace containment, path normalization, symlinks, file type, ownership, permissions, or atomic writes;
- shell/test execution, trusted executable discovery, subprocess environment, timeout, signal handling, process-tree cleanup, or Docker cleanup;
- sandbox probing, platform selection, or fail-closed fallback behavior;
- `/ask` and `/code` authorization, approvals, tool availability, or tool-call batching;
- workspace fingerprints, verification invalidation, submit gating, or authoritative test execution;
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
