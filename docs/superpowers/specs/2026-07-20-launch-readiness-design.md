# Launch Readiness Design

## Goal

Make the current security-first runtime understandable and installable from its
public repository before adding another major capability. The launch surface
must communicate one narrow promise: this project produces coding-agent runs
that are inspectable, verification-gated, resumable, and reversible.

## Scope

This release-readiness slice includes:

- an English-first landing README with a complete Chinese companion;
- repository hygiene for generated macOS metadata;
- a patch release that accurately records the already-completed runtime
  decoupling and fingerprint performance work;
- a tag-driven build and PyPI Trusted Publishing workflow;
- maintainer instructions that prevent a tag from being cut before the PyPI
  publisher and GitHub release environment are configured.

It does not include RuntimePolicy, trajectory replay, an HTML trace viewer,
IDE integrations, MCP, multi-agent execution, or new model providers. Those
belong to separate reviewed changes after the public baseline is available.

## Positioning

The primary English message is:

> A compact, security-first LangGraph coding-agent runtime with
> verification-gated patches, crash recovery, and authenticated undo.

The project is a reference runtime and experiment platform, not a replacement
for broad production assistants. Copy must emphasize enforced runtime controls
and must not call HMAC authentication a public digital signature.

## Documentation Structure

`README.md` becomes a concise English landing page. It must let a developer:

1. understand the differentiator above the fold;
2. run the no-key demo in one copyable sequence;
3. see the enforced safety/reliability controls and their limits;
4. discover run, chat, trace, undo, doctor, benchmark, and evaluation commands;
5. reach the full Chinese guide, security policy, contribution guide, and
   changelog.

The current detailed Chinese README moves intact to `README.zh-CN.md`, with a
language link back to English. Package metadata continues to use `README.md`.

## Release Model

The package version becomes `0.3.1`. The existing Unreleased entries become a
dated `0.3.1` section because they describe changes already present at HEAD.
The package and runtime version must remain identical.

Tags matching `v*` trigger a release workflow that:

1. checks out the tagged source;
2. builds sdist and wheel;
3. validates both with Twine;
4. uploads distributions as a GitHub Actions artifact;
5. publishes to PyPI through OIDC Trusted Publishing.

The workflow uses a protected `pypi` GitHub environment. The maintainer guide
requires configuring the PyPI publisher before creating the first tag. No API
token is stored in the repository.

## Failure and Safety Behavior

- The release job fails before publishing if build or Twine validation fails.
- PyPI publishing is impossible without the configured GitHub environment and
  PyPI Trusted Publisher relationship.
- Generated `.DS_Store` files remain untracked and are never staged.
- No tag or production release is created by this pull request; the PR prepares
  and verifies the release path first.

## Acceptance Criteria

- `README.md` provides a complete English quick start and links to Chinese.
- `README.zh-CN.md` preserves all current operational and security guidance.
- `pyproject.toml` and `mini_code_agent.__version__` both report `0.3.1`.
- `CHANGELOG.md` contains a dated `0.3.1` section.
- `RELEASING.md` documents Trusted Publishing setup and exact release commands.
- `.github/workflows/release.yml` builds, checks, and publishes tagged artifacts
  without repository secrets.
- `.DS_Store` is ignored everywhere.
- The full test suite, offline evaluations, package build, and metadata checks
  pass before the branch is pushed.
