## Summary

Describe the developer-visible outcome and why it is needed.

## Scope

- Included:
- Explicitly deferred:
- Compatibility or state-format impact:

## Validation

List exact commands and results.

- [ ] Focused tests
- [ ] `pytest -q`
- [ ] `python -m pip check`
- [ ] `python -m evals.run_evals --json`
- [ ] Relevant `mca doctor` / `mca demo` smoke checks
- [ ] `git diff --check`

Platforms and sandbox backends exercised:

## Security review

- Protected asset(s):
- Attacker- or model-controlled input(s):
- Fail-open or fail-closed behavior on error:
- Interaction with `--sandbox none`, `--allow-shell`, `--allow-dirty`, `--yes`, `--force`, or `--allow-legacy-unsafe`:
- Secret/redaction review performed:

- [ ] I reviewed [SECURITY.md](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/SECURITY.md) and documented any threat-model impact.
- [ ] Path, symlink, permission, command, sandbox, verification, resume, trajectory, Undo, provider, and process-cleanup changes have focused negative tests where applicable.
- [ ] The patch contains no real API key, `.env` value, private source, unredacted trajectory, Undo journal, or other sensitive artifact.

## Documentation

- [ ] README/help text is updated for user-visible behavior.
- [ ] `CHANGELOG.md` is updated when the change is user-visible, security-relevant, or incompatible.
- [ ] No documentation update is needed (explain why below).

Additional notes:
