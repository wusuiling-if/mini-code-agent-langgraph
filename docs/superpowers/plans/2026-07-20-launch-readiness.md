# Launch Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a verified `0.3.1` release candidate with an English-first landing page, complete Chinese documentation, and a tokenless PyPI release workflow.

**Architecture:** Documentation and release automation remain separate from the agent runtime. Package version metadata is updated in its two existing sources, while tag-triggered automation builds once and hands the validated distributions to PyPI Trusted Publishing.

**Tech Stack:** Markdown, Python packaging (`build`, `twine`), GitHub Actions, PyPI OIDC Trusted Publishing.

## Global Constraints

- Do not modify runtime behavior in this release-readiness change.
- Do not add runtime dependencies or repository secrets.
- Keep the full Chinese operational guide available.
- Describe HMAC undo as authenticated or tamper-evident, not digitally signed.
- Do not create a tag until the PyPI Trusted Publisher is configured.

---

### Task 1: Documentation and repository hygiene

**Files:**
- Modify: `.gitignore`
- Create: `README.zh-CN.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: existing CLI commands and documented safety boundaries.
- Produces: the package landing page consumed by GitHub and PyPI.

- [ ] **Step 1: Preserve the Chinese guide**

Copy the tracked contents of `README.md` to `README.zh-CN.md` and add an English
language link directly under its title.

- [ ] **Step 2: Write the English landing page**

Cover the no-key demo, run/chat examples, enforced controls, platform limits,
project structure, validation commands, and links to the detailed policies.
Every shown command must exist in `mca --help`.

- [ ] **Step 3: Ignore generated Finder metadata**

Add this exact repository-wide pattern:

```gitignore
.DS_Store
```

- [ ] **Step 4: Validate documentation references**

Run:

```bash
rg -n "README.zh-CN.md|mca demo|mca doctor|mca trace|mca undo" README.md README.zh-CN.md
git diff --check
```

Expected: both language links and all five command surfaces are present; diff
check exits zero.

### Task 2: Version and release automation

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/mini_code_agent/__init__.py`
- Modify: `CHANGELOG.md`
- Create: `RELEASING.md`
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: package version `0.3.1` and Git tag `v0.3.1`.
- Produces: validated sdist/wheel artifacts and an OIDC PyPI publish job.

- [ ] **Step 1: Add a failing version consistency check**

Run before editing:

```bash
.venv/bin/python -c "from mini_code_agent import __version__; assert __version__ == '0.3.1'"
```

Expected: assertion failure because the current version is `0.3.0`.

- [ ] **Step 2: Update release metadata**

Set both version declarations to `0.3.1` and move the two current Unreleased
items into `## [0.3.1] - 2026-07-20`, leaving an empty Unreleased heading.

- [ ] **Step 3: Add Trusted Publishing workflow**

On tags matching `v*`, build with `python -m build`, validate with
`python -m twine check dist/*`, upload the `dist/` artifact, and publish through
`pypa/gh-action-pypi-publish` in the protected `pypi` environment with
`id-token: write`.

- [ ] **Step 4: Document release prerequisites**

`RELEASING.md` must specify the PyPI project name, GitHub owner/repository,
workflow filename, environment name, verification commands, tag commands, and
rollback rule: never reuse or move a published tag/version.

- [ ] **Step 5: Verify version consistency**

Run:

```bash
.venv/bin/python -c "from mini_code_agent import __version__; assert __version__ == '0.3.1'"
rg -n 'version = "0.3.1"|\[0.3.1\]' pyproject.toml CHANGELOG.md
```

Expected: both commands exit zero.

### Task 3: Release-candidate verification and publication

**Files:**
- Verify all files changed by Tasks 1 and 2.

**Interfaces:**
- Consumes: completed documentation and release automation.
- Produces: a pushed branch and Draft PR; it does not create a release tag.

- [ ] **Step 1: Run deterministic validation**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m evals.run_evals --json
.venv/bin/python -m pip check
git diff --check
```

Expected: all tests pass, all three eval cases are successful and verified,
pip reports no broken requirements, and diff check exits zero.

- [ ] **Step 2: Build and inspect distributions**

```bash
.venv/bin/python -m build --sdist --wheel
.venv/bin/python -m twine check dist/*
```

Expected: one `0.3.1` sdist and one `0.3.1` wheel; Twine reports `PASSED` for
both.

- [ ] **Step 3: Review and publish branch**

Stage only the intended files, commit with `chore: prepare public 0.3.1 release`,
push `codex/publish-v0-3-runtime`, and open a Draft PR against `main` describing
the 12 existing runtime commits plus release-readiness changes and exact checks.
