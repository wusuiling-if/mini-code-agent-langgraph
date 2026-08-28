# Offline Verified Patch benchmark

## Memory release gate

The release-facing entry point runs all deterministic memory suites without
provider credentials or model calls:

```bash
.venv/bin/python -m evals.run_memory_suite --json \
  --output /tmp/memory-v0.6.0.json
```

The v0.6.0 report contains nine suites, including an eight-case production
conversation-memory gate for user/workspace scope, corrections and forgetting,
candidate approval, credential refusal, HMAC tampering, backup/restore, and
cross-store evidence. It also contains each suite's machine-readable result, an aggregate gate,
an explicit claims boundary, and a SHA-256 binding the runner sources. The
outcome-controller and scripted agent-intervention suites remain labeled as
experiments even though their deterministic wiring checks participate in the
release gate. Paid real-model runners below are never required CI gates.

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

## Deterministic memory formation

The receipt-to-memory lifecycle has a separate offline suite:

```bash
.venv/bin/python -m evals.run_memory_formation
.venv/bin/python -m evals.run_memory_formation --json
```

Its nine cases exercise default-off behavior, rejection before commit,
authenticated formation after commit, replay idempotency, scoped retrieval,
cross-commit evidence merging, verification-command change detection with
superseded history, and
rejection of legacy evidence that is not command-fingerprint-bound. It uses real
disposable Git transactions and the production admission/store/retrieval path,
but no model calls or free-text extraction.

## Outcome-aware memory control

The controller has a separate deterministic regression suite:

```bash
.venv/bin/python -m evals.run_memory_control
.venv/bin/python -m evals.run_memory_control --json
```

Its six cases compare static retrieval with the outcome-aware control layer,
exercise authenticated harmful/helpful feedback, explicit contraindications,
abstention, stuck-stage requery, and shadow-policy isolation. It makes no model
calls and does not claim learned-policy or open-world generalization.

The production-loop intervention A/B is separate:

```bash
.venv/bin/python -m evals.run_memory_intervention
.venv/bin/python -m evals.run_memory_intervention --json
```

It runs no-memory, static-retrieval, and controlled-memory conditions through
the production `MiniCodeAgent`, executor, verification gate, and a real fixture
test. A deterministic model stub reacts to advisory context, so this measures
wiring, harmful-intervention containment, and step/edit overhead—not real-model
quality.

Run the same intervention with a paid/non-deterministic tool-calling model:

```bash
# First smoke-test only the controlled condition.
.venv/bin/python -m evals.run_memory_intervention_model \
  --provider deepseek --model deepseek-flash \
  --condition controlled_memory

# Then run all three conditions and save a sanitized report.
.venv/bin/python -m evals.run_memory_intervention_model \
  --provider deepseek --model deepseek-flash \
  --output /tmp/memory-real-intervention.json
```

Credentials are read from the environment by the normal provider adapter.
Reports omit responses, memory values, tool outputs, local paths, and credentials.

For a harder three-file checkout repair, with balanced condition order and
optional repeats:

```bash
.venv/bin/python -m evals.run_memory_complex_intervention_model \
  --provider deepseek --model deepseek-flash --repeats 3 \
  --output /tmp/memory-complex-real-intervention.json
```

The fixture requires coordinated discount, shipping, expedited surcharge, tax,
and validation behavior across three implementation modules. Memory contains
repository rules rather than literal test output.

To remove hand-written memory and seeded outcomes entirely, run the natural
experience-transfer protocol:

```bash
.venv/bin/python -m evals.run_memory_natural_intervention_model \
  --provider deepseek --model deepseek-flash --repeats 3 \
  --output /tmp/memory-natural-transfer.json
```

A real no-memory training run must first pass verification. Its implementation
diff and authenticated outcome automatically form the only experience card.
The three conditions then repair an unseen fixture with different filenames,
function names, rates, thresholds, and amounts. Controller feedback is derived
only from real verification outcomes.

## Experimental conversation shadow extraction

Free-conversation extraction remains outside the production runtime.  The
experimental shadow evaluator proposes structured fact candidates from a
sample of the 120-session conversation fixture, validates exact user-supported
evidence quotes, and applies accepted lifecycle operations only to a separate
temporary memory store:

```bash
.venv/bin/python -m evals.run_memory_shadow_extraction \
  --provider deepseek --model deepseek-flash \
  --output /tmp/memory-shadow-extraction.json
```

The A/B compares the existing raw-session reader with structured shadow memory.
It reports candidate recall, filler-session false positives, rejection reasons,
singleton supersession, multi-valued coexistence, explicit forgetting, scope
isolation, reader accuracy, and store integrity.  The primary store is never
mutated by candidates.  This is an extraction experiment, not a production
automatic-write feature.

The portable SillyTavern-format path has a smaller adversarial real-model
exercise. It imports official-style JSONL fields and summary checkpoints, uses
message/character formation policy with a protected recent window, and checks
updates, explicit forgetting, hidden user authorship, assistant-only claims,
and a hallucinated summary without enabling embeddings:

```bash
.venv/bin/python -m evals.run_sillytavern_memory_eval \
  --provider deepseek --model deepseek-flash \
  --output /tmp/sillytavern-portable-memory.json
```

All extracted candidates remain in a disposable shadow store.

## Scope boundary

This benchmark is offline runtime-policy evidence. Scripted plans measure
production-loop verification, refusal, recovery, resume, Undo, and workspace
discipline. They do **not** measure model quality, autonomous repair ability,
SWE-bench performance, provider behavior, or general software-engineering task
success.
