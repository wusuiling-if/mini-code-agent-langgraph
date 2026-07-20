# Offline behavior baseline

This directory contains a minimal deterministic baseline for the production
`MiniCodeAgent` loop.  It uses scripted local models and disposable copies of
small fixtures, so it needs no API key and makes no network requests.

The three cases cover:

- `single-file-fix`: reproduce, edit one file, verify, and submit;
- `explain-only`: inspect and explain without changing the workspace;
- `failed-fix-recovery`: reject a failed correction, recover, verify, and submit.

Run the complete suite from the repository root:

```bash
.venv/bin/python evals/run_evals.py
```

Run one case or save the JSON report:

```bash
.venv/bin/python evals/run_evals.py --case failed-fix-recovery
.venv/bin/python evals/run_evals.py --output /tmp/mca-eval-report.json
```

Each case reports `success`, `verified`, `steps`, `tool_calls`, `duration_ms`,
and `unrelated_changes`, plus verification/recovery diagnostics.  The process
returns nonzero when any selected case fails.  Durations naturally vary by
machine; the fixtures, tool plans, expected changes, and pass/fail result are
otherwise deterministic.

This is a runtime-policy regression baseline, not a model-quality benchmark.
A later online eval can reuse the cases while replacing `ScriptedEvalModel`
with a real provider and adding token, cost, and semantic-quality metrics.
