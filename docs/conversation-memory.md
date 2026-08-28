# Portable conversation memory

This layer borrows the useful workflow ideas from SillyTavern without making
SillyTavern, a character card, a lorebook, or an embedding service part of the
core contract.

## What is implemented

The host-neutral API lives in `memory_core.conversation`:

- `ConversationEvent` separates stable message identity from the digest of its
  current revision. Editing a source message invalidates derived checkpoints.
- `FormationPolicy` supports message-count and character-count triggers,
  blocking/background/manual modes, a recent-message protection window, and a
  bounded incremental batch.
- `MemoryCheckpoint` is a derived summary revision with exact source event
  digests and optional parent lineage.
- `CheckpointLedger` supports rollback and automatically falls back to the
  newest still-valid ancestor after a source edit or deletion.
- `ScopeAddress` represents global, user, agent/persona, and conversation
  scopes without using host-specific names.
- `PromptInjectionPolicy` controls position, depth, role, item count, and
  character budget. Rendered memory is labelled as fallible data rather than
  instructions.

The adapter in `memory_core.adapters.sillytavern` accepts official
SillyTavern-style JSONL, JSON arrays, and wrapped JSON objects:

- the `chat_metadata` header is preserved as canonical JSON;
- `name`, `is_user`, `is_system`, `send_date`, `mes`, `extra.type`, stable host
  message IDs, and swipe identity are normalized into portable events while
  the full raw row is hashed as evidence;
- inactive swipe text is kept in the evidence digest but is not treated as an
  active message; each swipe revision is separately hashed in event metadata;
- summaries stored at `message.extra.memory` are imported as derived
  checkpoints, not authoritative facts;
- Tavern Helper chat variables (`chat_metadata.variables`) and active message
  variables are flattened into schema-mapping candidates with no authority;
  likely secrets are omitted from that candidate stream;
- Data Bank scopes map as `global -> global`, `character -> agent`, and
  `chat -> conversation`; the portable layer additionally supports a user
  scope shared across agents and conversations.

## Deliberate differences from common chat-memory plugins

Raw messages remain evidence. Summaries reduce prompt cost but never replace
or upgrade the authority of the messages that produced them. Structured facts
and explicit forget/update operations remain a separate admission stage, so a
summary hallucination cannot silently become a durable fact.

Embedding retrieval is optional. Lexical, temporal, identity, and scope-aware
retrieval can operate without a local model or external embedding API.

## Replayable mutations, not model-written SQL

`memory_core.mutations` adds an independently designed mutation protocol:

- every `MemoryMutation` is an explicit `assert`, `dispute`, or `withdraw`
  operation against an entity/predicate pair;
- each operation names a versioned `MemoryFieldSchema` and binds the exact
  source event revisions that justified it;
- `base_revision` provides optimistic conflict detection instead of silent
  last-writer-wins behavior;
- `MutationLedger` materializes canonical state, supports idempotent retries
  within the active log segment, and reports post-conditions for every
  attempted commit;
- `CanonicalMemorySnapshot` round-trips the state and links compacted
  revisions without deleting the semantic audit trail.

This takes the useful operation-log/checkpoint idea from mature state systems
but keeps memory-specific semantics. It does not accept arbitrary SQL or a
model-authored table patch as the durable truth.

## Long-horizon continuity and selective forgetting

`memory_core.continuity` separates four retention classes:

- `core`: identity, names, durable user preferences, explicit boundaries, and
  other facts whose loss would make the relationship feel broken;
- `durable`: relationship state, ongoing commitments, and stable project or
  world state;
- `episodic`: source-bound events that may later be compressed into a
  checkpoint;
- `transient`: scene details and short-lived working state.

Capacity retirement is utility-based but has hard continuity invariants. By
default, valid core memory, durable memory, pinned memory, and the final
core/durable record for an identity anchor cannot be automatically evicted.
Transient state may retire directly. Episodic state returns
`checkpoint_compaction_required` with a source batch and cannot retire until a
replacement checkpoint has been formed and validated. If only protected
records remain, the planner rejects the incoming admission instead of silently
forgetting an old fact. Invalidated source revisions leave the active set
immediately but remain in append-only audit history.

At retrieval time, `select_continuity_context` reserves a small query-independent
budget for active core and durable facts before ordinary query retrieval. If
the budget cannot contain every core fact, it returns
`requires_compaction=True` and the omitted IDs. The host must form a reviewed,
source-bound compact representation or increase the budget; omission is never
reported as a complete continuity set.

Retention class is part of `MemoryFieldSchema` and survives canonical snapshot
round-trips. For example, `preferred_name` should normally be `core`, current
relationship state `durable`, an important scene `episodic`, and temporary UI
state `transient`. Explicit `withdraw` still overrides retention protection:
"long term" must not defeat a user's request to forget.

## Tavern Helper (酒馆助手) import boundary

`memory_core.adapters.tavern_helper.import_tavern_helper_export` accepts:

- current script and folder exports;
- legacy scripts using top-level `buttons` and legacy `{type, value}` wrappers;
- global/preset/character settings bundles containing `scripts` and
  `variables`;
- current character-card `data.extensions.tavern_helper` state and the two
  older `TavernHelper_*` character extension fields.

The importer never evaluates JavaScript and never follows a remote import. It
returns a quarantined script inventory, literal remote dependency URLs and
hashes, variable bundles, and scalar data candidates. Candidates always start
with `authority="none"`; importing them into durable memory requires an
explicit JSON-pointer-to-schema mapping and the normal evidence admission
step.

```python
from memory_core.adapters import import_tavern_helper_export

preview = import_tavern_helper_export(export_json, suggested_scope="character")
assert preview.code_executed is False
assert preview.remote_fetches == 0

for candidate in preview.candidates:
    # Application-owned policy: map candidate.json_pointer to a
    # MemoryFieldSchema, ask for review if ambiguous, then create a
    # source-bound MemoryMutation. Do not admit arbitrary paths wholesale.
    pass
```

The sample `数据库本体` export is only a remote-module loader with empty
`data`. A safe import therefore produces one quarantined script artifact and
zero memory candidates. The actual database behavior lives in the referenced
project; downloading or executing that code is a separate, explicit trust
decision.

The adapters are import-only at this stage. They do not modify SillyTavern chat
files, hide messages, install a browser extension, or write candidate values
to durable memory automatically. This keeps migration preview reversible while
the schema mapping is reviewed and tested.

## MCA integration

`mca chat --memory local` implements the first production host bridge:

- user and assistant turns are appended as immutable `ConversationEvent` rows in
  private HMAC-chained ledgers; log identity, sequence, previous HMAC, and payload
  are authenticated under a local key;
- explicit `/remember` writes a current-workspace semantic card, while
  `/remember --scope user` writes a stable local-user card available across workspaces;
  both are bound to the event's source reference and digest;
- `/correct` creates a superseding revision and `/forget` appends a tombstone plus
  the user's forget-event evidence;
- retrieval searches only the stable local-user and current-workspace scopes,
  using the existing evidence-temporal policy and a 5,000-character, four-item
  prompt budget; an optional OpenAI-compatible semantic provider can rerank the
  already hard-filtered candidates and falls back to lexical retrieval;
- simple preference-like statements may become pending candidates, but only
  `/remember @ID` can admit one to durable memory.

The bridge rejects obvious credentials, leaves memory off by default, and labels
retrieval as fallible historical data that cannot grant tool authority. `/clear`
only clears the model's current context; it deliberately does not erase durable
memory. Use `/forget` for an auditable tombstone, or `mca memory purge --yes` to
remove the complete local store. `mca memory verify` checks both HMAC chains and
their evidence links into SQLite. Full backups are verified but plaintext sensitive.

## Further integration boundary

A host integration only needs to implement four actions:

1. convert host messages to `ConversationEvent`;
2. persist raw event revisions and the checkpoint graph;
3. run a summary/candidate producer when `plan_formation` says a batch is due;
4. place `PromptInjection` according to the host's prompt API.

For SillyTavern, those actions can sit behind its extension event API. MCA now
implements them in its terminal chat host. A web service can expose the same
operations over JSON without either host dependency.

## References and licensing boundary

The design was checked against SillyTavern's official Summarize, Chat
Vectorization, and Data Bank documentation, plus the public feature surfaces of
Memory Books, Horae, Tavern Helper, and shujuku. No extension source code is
copied. This repository's implementation is an independent MIT-licensed Python
design. Third-party packages keep their own licenses and must not be copied
into this codebase without a separate licensing analysis.
