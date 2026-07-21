# Offline Verified Patch benchmark

This directory contains `verified-patch-v0.3.2`, an eleven-case deterministic
benchmark for the production `MiniCodeAgent` loop and executor. It uses scripted
local responses, standard-library-only fixtures, and disposable workspace copies.
It requires no provider credentials and makes no network requests.

The cases are:

- `single-file-fix`: repair one file, verify, inspect a real tracked diff, and submit;
- `multi-file-fix`: make exactly two required implementation edits and submit;
- `explain-only`: verify and explain existing behavior without changing files;
- `failed-fix-recovery`: observe a failed edit, correct it, and verify again;
- `premature-submission`: refuse an unverified submit, then verify and recover;
- `stale-verification`: invalidate evidence after a later edit, then retest;
- `failed-test-refusal`: refuse submission after an authoritative test failure;
- `zero-test-refusal`: reject zero discovered tests and refuse submission;
- `shell-disabled`: refuse arbitrary shell, then verify with the structured tool;
- `checkpoint-resume`: resume at a safe boundary and require a fresh passing test;
- `authenticated-undo`: submit a verified edit and authenticate exact restoration.

The two terminal refusal cases are passing policy outcomes when the expected
failure is observed and no submission is accepted. The Undo case has outcome
`reverted`; other successful completion cases have outcome `submitted`.

Run the complete suite from the repository root:

```bash
.venv/bin/python -m evals.run_evals --json
```

Run one case, select several cases, or save the sanitized report:

```bash
.venv/bin/python -m evals.run_evals --case failed-test-refusal --json
.venv/bin/python -m evals.run_evals --case single-file-fix --case multi-file-fix --json
.venv/bin/python -m evals.run_evals --output /tmp/mca-eval-report.json --json
```

Every selection retains report schema `2` and suite name
`verified-patch-v0.3.2`. Stable evidence includes case names, scripted plans,
expected outcomes, policy refusal codes, exact expected and unrelated changes,
structured `returncode`/`tests_run` records, ordered tool-result and private
hashed-argument contracts, steps, tool calls, and pass/fail results. Expected
argument signatures come from an explicit private specification oracle that is
independent of the scripted model responses. The single-file scenario creates
a committed disposable Git baseline and requires recognized tracked-diff
markers. `duration_ms`, Python major/minor, and the platform system name are
informational and may vary by machine. The process exits nonzero if any selected
case fails its full contract.

Reports contain normalized evidence only. They do not contain raw agent state,
workspace contents, secret values, Undo records, authentication keys, or
persistence and internal state paths. Tool argument values and their private
validation signatures are also omitted. Lifecycle files used by resume and
Undo exist only inside the scenario's disposable temporary directory.

## Scope boundary

This benchmark is offline runtime-policy evidence. Scripted plans measure
production-loop verification, refusal, recovery, resume, Undo, and workspace
discipline. They do **not** measure model quality, autonomous repair ability,
SWE-bench performance, provider behavior, or general software-engineering task
success.
