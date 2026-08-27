# Fixed-model Harbor comparison

This integration compares two coding-agent harnesses while holding the model and
SWE-bench task subset fixed:

- baseline: Harbor's `mini-swe-agent==2.1.0` integration;
- candidate: `MiniCodeAgentHarborAdapter` running MCA 0.5.0;
- dataset: the 25-task pilot in `pilot-25.json`, drawn from the public 500-task
  `swe-bench/swe-bench-verified` Harbor dataset;
- attempts: one per task, with Harbor retries disabled.

The dataset reference is pinned to its Harbor content SHA-256 rather than the mutable
`latest` tag. The protocol is machine-readable in `protocol.json`. A run is publishable only
after the exact model, both Harbor-resolved job configs/task locks, image digests,
agent versions, and raw result directories have been retained. The checked-in
protocol intentionally contains no score yet.

The deterministic three-task smoke is recorded in
[`docs/benchmarks/harbor-three-task-smoke-2026-08-26.zh-CN.md`](../../docs/benchmarks/harbor-three-task-smoke-2026-08-26.zh-CN.md).
It found one candidate transaction that exhausted all three connection-resume
attempts, so the 25-task pilot is currently blocked rather than silently expanding a
transport failure into a larger paid run.

## Why this benchmark

SWE-bench Verified supplies real repository issues, containerized workspaces, and
hidden issue-specific verification. It is useful for a same-model harness A/B.
Because the benchmark is old and known to have contamination concerns, the result
must not be presented as a current frontier-capability claim.

## MCA arm boundary

Each task runs as an MCA Git transaction. Memory is disabled. The pilot explicitly
enables MCA's shell so both harnesses can inspect and test the repository, but that
permission exists only inside Harbor's disposable Docker environment. `git diff
--check` is the required agent-visible integrity check, and the prepared patch is
committed only after that check. Harbor then runs its hidden verifier outside the
agent phase. MCA uses `--sandbox none` *inside* the task because Harbor's container is
the enclosing sandbox; this does not run task code directly on the benchmark host.

The adapter records aggregate model usage from MCA's private transaction trajectory.
The trajectory itself remains in the Harbor job directory for audit and may contain
the private task prompt, so do not publish it without reviewing/redacting it.

## Install and dry-run

Harbor 0.22 requires Python 3.12+. Docker execution is best run on an x86-64 host or
cloud sandbox because SWE-bench images are not a reliable local ARM/macOS workload.
The launcher accepts either bare task names or `swe-bench/...` references and emits
the fully-qualified filter required by Harbor 0.22. It also locates `harbor` beside
the active Python executable, so calling `.harbor-venv/bin/python` directly works
without separately activating the virtual environment, and makes the repository's
candidate adapter importable by the Harbor subprocess.
The candidate installer first reuses the task image's pinned `uv==0.7.13`; its
network installer is only a fallback, which avoids an unnecessary mutable download.
The shared agent environment also sets `BASH_ENV=/root/.local/bin/env`. Harbor's
upstream mini-swe-agent installer checks for `uv` before it sources that file; loading
the task image's pinned tool environment at shell startup prevents the baseline from
falling back to an unpinned network installer. The same environment is applied to both
arms and does not replace the task image's existing `PATH`.
Fetching the exact candidate package may retry up to three times to tolerate transport
failures; each attempt uses the same immutable package specification.
The candidate permits up to three explicit transaction resumes after an interrupted open
checkpoint, with 10/20/30-second backoff. Request retries remain disabled; the resumed
transaction retains cumulative steps and token usage and must run fresh verification
before it can commit.
Each transaction attempt now appends a structured, content-free recovery record and
the shell log marks the attempt number, fixed backoff, exit status, and duration. Model
usage distinguishes successful calls from all attempted and failed requests, so a
transport failure is no longer hidden from usage totals. Repeated failures before a
new model response do not duplicate the resume notice. These diagnostics do not change
the pinned three-resume budget or enable request retries.
The protocol also pins Docker execution to `linux/amd64` and lowers `uv` download
concurrency for both agent setup and verifier parsing. These transport settings are
recorded in each Harbor lock and do not change the selected packages or test logic.

```bash
python3.12 -m venv .harbor-venv
. .harbor-venv/bin/activate
python -m pip install -r benchmarks/harbor/requirements.txt

python -m benchmarks.harbor.launch \
  --model openai/gpt-5.6-sol
```

The protocol pins `openai/gpt-5.6-sol` at
`https://api.dstopology.com/v1`; a different model is rejected so the two arms cannot
silently drift. The launcher prints both commands without spending money. Keep the
key outside the repository and export it before execution. The launcher maps the
credential and pinned endpoint into both Harbor agent conventions without printing
the secret:

```bash
export OPENAI_API_KEY='...'
```

Run one paid task through both arms first:

```bash
python -m benchmarks.harbor.launch \
  --model openai/gpt-5.6-sol \
  --smoke \
  --n-concurrent 1 \
  --package-spec IMMUTABLE_WHEEL_OR_VCS_SPEC \
  --execute
```

After that smoke has no adapter, image, provider, or verifier error, launch both
25-task arms explicitly:

```bash
python -m benchmarks.harbor.launch \
  --model openai/gpt-5.6-sol \
  --n-concurrent 4 \
  --package-spec IMMUTABLE_WHEEL_OR_VCS_SPEC \
  --execute
```

Before MCA 0.5.0 is on PyPI, pass an immutable wheel URL or VCS commit through
`--package-spec`. Paid execution requires this argument explicitly, including after
release, so the candidate artifact is always visible in the command record. Do not use
a mutable branch reference in a report.

## Reporting checklist

Report paired task outcomes and resolved rate, but separate infrastructure errors
from genuine reward-zero attempts. Include input/output/cache tokens, model calls,
wall time, and the exact command/protocol. Verify that both arms resolved the same 25
task revisions before comparing scores. Complete the deterministic three-task smoke
without agent transport errors before the pilot; expand to all 500 tasks only after
the pilot has no adapter or verifier errors.
