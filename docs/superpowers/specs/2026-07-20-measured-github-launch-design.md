# Measured GitHub Launch Design

Date: 2026-07-20

## Goal

Turn the existing `0.3.1` release candidate into a small, measurable public
launch that makes the repository easier to discover, understand, and try. The
launch does not set a Star quota. It separates reach, repository conversion,
and real usage so the next investment is based on evidence rather than another
feature guess.

## Current state

- Draft PR #1 contains the reviewed runtime, documentation, packaging, and
  Trusted Publishing work.
- Package, offline evaluation, and cross-platform CLI checks pass.
- The Python 3.11–3.13 test matrix is red because one diagnostics test replaces
  every `builtins.open` and `io.open` call. Newer Python versions legitimately
  read installed package metadata through `io.open`, so the test fails before
  it reaches the env-file check. Python 3.10 happens not to expose the same
  over-broad test double.
- The public repository has no Release and no visual demo. Its description is
  Chinese-only, although PR #1 supplies an English-first README and a complete
  Chinese companion.
- Repository traffic is too small to distinguish a reach problem from a README
  conversion problem.

## Authorized boundary

The maintainer selected the GitHub-owned launch path:

- Codex may fix CI, improve tracked launch assets, update repository metadata,
  merge the reviewed PR, and create the GitHub/PyPI `0.3.1` release once all
  release prerequisites exist.
- Codex may prepare English and Chinese third-party launch copy.
- Codex must not publish to third-party communities or act as the maintainer on
  external accounts.
- No paid promotion, automated starring, vote solicitation, or cross-post spam
  is allowed.

## Launch funnel

The launch is evaluated as three separate stages:

1. **Reach:** repository views and referrers show whether anyone encountered the
   project.
2. **Conversion:** Stars relative to meaningful repository views show whether
   the first screen communicates a differentiated reason to care.
3. **Activation:** clones, Issues, and concrete technical feedback show whether
   visitors tried or seriously evaluated the project.

No fixed Star target is required. A roughly seven-day quiet observation window
after release is sufficient for the first diagnosis; runtime features should
not be added during that window merely to move the metric.

## Components

### 1. CI correction

Change only the failing diagnostics test. Its file-open guard must reject reads
of the selected env file while permitting unrelated standard-library metadata
reads. Production diagnostics behavior remains unchanged: `_env_file_check`
uses metadata operations and never opens the env file.

Acceptance criteria:

- the focused regression fails with the current over-broad guard and passes
  with the path-specific guard;
- Python 3.10, 3.11, 3.12, and 3.13 test jobs pass;
- package, offline evaluation, and all three CLI smoke jobs remain green.

### 2. Repository first screen

The English README remains concise and PyPI-safe. Improve only the area above
the first long section:

- restore tests, Python support, and MIT license badges;
- embed one real terminal demo directly after the introductory paragraph;
- state the three differentiators in compact language: verification-bound
  submission, resumable/redacted trajectories, and HMAC-authenticated
  conflict-aware Undo;
- retain the no-key quickstart immediately after the demo.

The demo is a short, source-controlled terminal recording of `mca demo`. It
must use the deterministic mock model, contact no provider, contain no key, and
show the resulting `Submitted` status and passing tests. Commit both the rendered
GIF under `docs/assets/` and a reproducible VHS source under `docs/demo.tape`.
Random temporary paths may appear, but no user home path, repository secret, or
provider value may appear.

Do not add a website, full-screen TUI mockup, product logo project, comparison
table against competitors, or fabricated usage statistics in this launch.

### 3. Repository metadata

Use an English description that describes observable behavior instead of broad
marketing language:

> Inspectable LangGraph coding agent with verified patches, resumable
> trajectories, sandboxed tools, and HMAC-authenticated Undo.

Keep the existing relevant topics and add `agent-security`, `developer-tools`,
and `cli` if the topic limit permits. Issues remain the feedback surface;
Discussions stay disabled until there is enough activity to justify another
channel.

### 4. Release path

Release actions are ordered and gated:

1. Fix CI on the existing feature branch and rerun the full local validation.
2. Add the README/demo/launch-copy changes and independently review them.
3. Push the branch, wait for every required PR check, mark the PR ready, and
   merge it to `main` without rewriting published history.
4. Confirm the PyPI pending publisher and protected GitHub `pypi` environment
   exactly match `RELEASING.md`.
5. Only then create and push `v0.3.1`. The existing workflow builds, validates,
   and publishes through OIDC.
6. After PyPI succeeds, create the matching GitHub Release with concise notes
   and the exact tag.

If the PyPI pending publisher is not yet configured, stop after merging and
report that one-time manual prerequisite. Do not create a tag that is expected
to fail and do not bypass Trusted Publishing with a repository token.

### 5. Launch copy and observation

Add `docs/launch/0.3.1.md` containing:

- a concise English launch post;
- a concise Chinese launch post;
- one longer technical-post outline centered on verification, trajectories,
  and Undo rather than generic AI claims;
- the already researched channel order and per-channel anti-spam constraints.

The maintainer rewrites and publishes third-party posts personally. After the
release, capture GitHub traffic, Stars, forks, clones, referrers, and actionable
Issues once near launch and again after roughly seven days. Interpret the
results by funnel stage; do not treat clone bots or raw view counts as users.

## Data flow

```text
CI green
  -> reviewable README + real no-key demo
  -> merge to main
  -> verify PyPI/GitHub publishing identities
  -> tag v0.3.1
  -> PyPI publish through OIDC
  -> GitHub Release
  -> maintainer-owned external posts
  -> observe reach / conversion / activation
```

## Safety and failure handling

- A red required check blocks merge.
- A missing or mismatched publisher/environment blocks tagging.
- A failed PyPI publish blocks GitHub Release completion until the failure is
  understood; published versions and tags are never reused or moved.
- The demo uses only the mock model and disposable calculator workspace.
- Secret-like values are scanned in new text and binary recording sources
  before commit. Rendered assets are inspected visually.
- HMAC is described as authentication/tamper evidence, never as a digital
  signature or proof of semantic correctness.
- Third-party posts are never sent automatically.

## Verification

Before merge:

- run the focused diagnostics regression;
- run all 114+ tests locally in the branch-local virtual environment;
- run all offline evaluations and `pip check`;
- build one sdist and one wheel and pass `twine check`;
- install the wheel in a clean environment and run version/help/doctor/demo;
- run `git diff --check` and a new-additions secret-like scan;
- inspect the GIF and confirm its README link resolves;
- obtain an independent review with no Critical or Important findings.

GitHub Actions must then confirm the Python 3.10–3.13 matrix, Linux/macOS/Windows
CLI smoke tests, package build, and offline evaluation on the pushed commit.

## Out of scope

- LangChain/LangGraph decoupling;
- new runtime tools, providers, TUI, website, plugin system, or multi-agent
  behavior;
- third-party account actions;
- paid acquisition, Star exchanges, or synthetic engagement;
- a hard Star target for the first observation window.

