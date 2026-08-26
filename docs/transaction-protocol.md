# Transaction Protocol

`mca tx` treats an agent run as a prepare/commit transaction over a clean Git repository.

## Lifecycle

1. `tx run` snapshots the source `HEAD` and whole-workspace fingerprint, creates a detached worktree under private application state, and runs the agent there.
2. Tool calls append read/write evidence to a write-ahead access log.
3. The transaction reaches `prepared` only when configured checks pass and their workspace fingerprint exactly matches the isolated workspace.
4. The runtime records a binary Git patch and an HMAC-authenticated receipt.
5. `tx commit` authenticates durable state, rechecks source and prepared fingerprints, validates the patch, and only then applies it to the source.
6. When the run explicitly selected `--memory local`, a successful commit deterministically records the authenticated verification workflow as informative procedural memory.

The source checkout remains unchanged through prepare.

## Receipt

The receipt binds the baseline, a local workspace-identity hash, patch hash, verification evidence and fingerprint, command fingerprints, memory mode, trajectory digest, access-log digest, and observed read/write sets. Verification command plaintext is not persisted. `mca tx receipt TRANSACTION_ID` renders non-source evidence for inspection.

This is tamper evidence under a private machine key. It is not a portable signature, remote attestation, proof that verification is complete, or proof that the patch is correct.

Memory indexing is post-commit and auxiliary. An indexing failure is reported without changing or misreporting the already committed transaction state.

## Conflict behavior

Commit fails closed if the source `HEAD` or any fingerprinted source path changed after begin, if the isolated workspace changed after prepare, or if the stored patch no longer matches its authenticated receipt. Conflict detection is deliberately whole-workspace; observed access sets are audit evidence and do not enable automatic merging of unrelated concurrent edits.

The check/apply interval is short but is not a filesystem-wide lock against every external writer.

## Recovery

After a complete tool checkpoint, resume with the same model and verification configuration:

```bash
mca tx resume TRANSACTION_ID --model deepseek --check tests "pytest -q"
mca tx abort TRANSACTION_ID
```

`--max-steps` is a cumulative ceiling across the original run and every resume.
When a transaction reaches that ceiling, the generated `next:` command raises it
above the saved checkpoint; manually written resume commands must do the same.
Resume always invalidates earlier verification and requires the configured checks
to pass again. Transaction-only memory retrieval audit metadata is carried forward
without re-retrieving or duplicating advisory context.

Transaction metadata, checkpoints, patches, receipts, access logs, and isolated worktrees live under private application state outside the source repository.
