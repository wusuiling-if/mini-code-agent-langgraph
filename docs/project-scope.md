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

Production memory is opt-in for transactional coding runs. It may retrieve bounded, same-project advisory context and may form new memories only after an authenticated transaction receipt reaches a successful commit. The read-only `mca memory` commands inspect and verify that state.

Automatic free-conversation extraction, generic chat ingestion, SillyTavern/Tavern Helper imports, and the outcome-aware controller remain experimental. They must not silently write production memory or be used as evidence for a production quality claim.

## Evaluation boundary

Deterministic repository suites are release gates. Paid or nondeterministic model experiments are versioned research artifacts and never run in required CI. A public benchmark report must pin the dataset, model, agent versions, prompts and tool schema, resource limits, attempts, and environment digests; fixed-model comparisons change the harness, not the model.
