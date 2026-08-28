# Project scope

`mini-code-agent-langgraph` is the system project for a compact, inspectable coding-agent runtime. Its durable scope is:

- agent-loop and context behavior;
- sandbox and permission enforcement;
- evidence-bound local memory;
- crash-safe checkpoint and transaction recovery;
- trace, verification, authenticated receipts, and conflict refusal;
- deterministic eval engineering and fixed-model public benchmark comparisons.

The project does not implement or host SFT, DPO, reinforcement learning, reward-model training, or training-data pipelines. Public benchmark integrations measure the runtime and agent harness; they do not turn this repository into a model-training project.

## Production memory boundary

Production memory is opt-in. Transactional coding runs may retrieve bounded, same-project advisory context and form new memories only after an authenticated transaction receipt reaches a successful commit. Interactive chat may use `mca chat --memory local`; it writes private raw event evidence, admits only explicit `/remember` approvals, and represents `/correct` and `/forget` as auditable temporal changes. The read-only `mca memory` commands inspect and verify active and historical cards.

Heuristic chat extraction may only create pending candidates and cannot silently admit durable memory. Model-driven automatic admission, SillyTavern/Tavern Helper imports, and the outcome-aware controller remain experimental and cannot be used as evidence for a production quality claim.

## Evaluation boundary

Deterministic repository suites are release gates. Paid or nondeterministic model experiments are versioned research artifacts and never run in required CI. A public benchmark report must pin the dataset, model, agent versions, prompts and tool schema, resource limits, attempts, and environment digests; fixed-model comparisons change the harness, not the model.
