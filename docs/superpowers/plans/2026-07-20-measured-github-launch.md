# Measured GitHub Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the reviewed `0.3.1` release candidate into a CI-green, visually understandable, measurable GitHub launch without adding runtime features or manufacturing engagement.

**Architecture:** Keep runtime behavior untouched. Correct the failing diagnostics test at its isolation boundary, add a reproducible no-provider terminal recording and concise first-screen copy, then ship through the existing reviewed PR and OIDC release workflow. GitHub repository changes and release publication are separate gated operations so a missing PyPI publisher can never create a predictably broken tag.

**Tech Stack:** Python 3.10–3.13, pytest, Markdown, VHS/ffmpeg, GitHub Actions, GitHub CLI/API, Python packaging (`build`, `twine`), PyPI OIDC Trusted Publishing.

## Global Constraints

- Production runtime behavior and runtime dependencies must not change.
- The demo must execute the real `mca demo` command with the deterministic mock model, no provider call, and no API key.
- The demo may show a randomized `/tmp/mca-demo-*` path; it must not show a user home path, repository secrets, or provider values.
- The README must keep the no-key quickstart immediately after the demo and remain suitable as the PyPI long description.
- The first screen must name exactly these differentiator classes: verification-bound submission, resumable/redacted trajectories, and HMAC-authenticated conflict-aware Undo.
- HMAC must be described as authentication or tamper evidence, never as a digital signature or proof of semantic correctness.
- Do not enable GitHub Discussions, add a website/TUI/logo/comparison table, invent usage metrics, or add another runtime feature.
- Do not publish to Hacker News, V2EX, Reddit, or any other third-party account; the maintainer rewrites and publishes external posts personally.
- Do not solicit Stars, votes, comments, or synthetic engagement.
- A red required PR check blocks merge.
- A missing or mismatched PyPI pending publisher or GitHub `pypi` environment blocks creation of `v0.3.1`.
- Published tags and package versions are immutable and must never be moved or reused.

---

### Task 1: Isolate the diagnostics env-file regression

**Files:**
- Modify: `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: `run_diagnostics(cwd, sandbox, provider, env_file=...) -> list[DiagnosticCheck]`.
- Produces: a path-specific test double that rejects only reads of the selected env file and permits `importlib.metadata` to read installed package metadata.

- [ ] **Step 1: Reproduce the current cross-version failure before editing**

Run:

```bash
.venv/bin/python -m pytest tests/test_diagnostics.py::test_env_file_is_inspected_without_opening_or_exposing_contents -vv
```

Expected on the affected local Python: `FAIL` before the env check because `importlib.metadata.version("mini-code-agent-langgraph")` reaches the globally replaced `io.open` and raises `AssertionError: diagnostics must not open the env file`.

- [ ] **Step 2: Replace the global open failure with a path-specific guard**

Add `Callable` to the imports and replace the two unconditional monkeypatches inside the failing test with this exact guard:

```python
from collections.abc import Callable


def guard_open(original_open: Callable[..., object]) -> Callable[..., object]:
    def guarded_open(file: object, *args: object, **kwargs: object) -> object:
        try:
            opened_path = Path(file).resolve()  # type: ignore[arg-type]
        except (OSError, TypeError):
            opened_path = None
        if opened_path == env_file.resolve():
            raise AssertionError("diagnostics must not open the env file")
        return original_open(file, *args, **kwargs)

    return guarded_open

monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
monkeypatch.setattr(io, "open", guard_open(io.open))
```

Keep the existing secret non-disclosure assertion and add this assertion after `run_diagnostics(...)`:

```python
assert check_named(checks, "package").status == "pass"
```

Do not edit `src/mini_code_agent/diagnostics.py`; its env-file check already uses metadata-only operations.

- [ ] **Step 3: Verify the focused regression is green**

Run:

```bash
.venv/bin/python -m pytest tests/test_diagnostics.py::test_env_file_is_inspected_without_opening_or_exposing_contents -vv
.venv/bin/python -m pytest tests/test_diagnostics.py -q
```

Expected: the focused test passes; the complete diagnostics test module passes with no env-file contents in output.

- [ ] **Step 4: Commit the isolated CI correction**

```bash
git add tests/test_diagnostics.py
git commit -m "test: isolate diagnostics env file guard"
```

Expected: one test-only commit; `git show --stat --oneline HEAD` lists only `tests/test_diagnostics.py`.

---

### Task 2: Add the reproducible terminal demo and README first screen

**Files:**
- Create: `docs/demo.tape`
- Create: `docs/assets/demo.gif`
- Modify: `README.md`

**Interfaces:**
- Consumes: installed `.venv/bin/mca`, real command `mca demo`, deterministic mock model, and a temporary workspace rooted under `/tmp`.
- Produces: `docs/assets/demo.gif`, reproducible from `docs/demo.tape`, plus an English landing page that embeds the asset from `main`.

- [ ] **Step 1: Install the recording tool already verified missing on this macOS host**

Run:

```bash
brew install vhs
vhs --version
ffmpeg -version
```

Expected: Homebrew installs VHS and its `ttyd`/`ffmpeg` dependencies; all three commands exit zero.

- [ ] **Step 2: Add the exact reproducible tape**

Create `docs/demo.tape` with:

```text
Output docs/assets/demo.gif

Require mca

Set Shell "bash"
Set FontSize 20
Set Width 1100
Set Height 520
Set Padding 24
Set Theme "Catppuccin Frappe"
Set TypingSpeed 35 ms

Sleep 500 ms
Type "mca demo"
Sleep 300 ms
Enter
Sleep 8 s
```

This records the actual public command. Do not replace it with prerecorded or manually typed output.

- [ ] **Step 3: Render the GIF with a safe, stable temporary-path prefix**

Run:

```bash
mkdir -p docs/assets
TMPDIR=/tmp PATH="$PWD/.venv/bin:$PATH" vhs docs/demo.tape
file docs/assets/demo.gif
```

Expected: `docs/assets/demo.gif` is a non-empty animated GIF; its terminal output includes `exit_status: Submitted` and `tests: passed`, with generated paths beginning `/tmp/mca-demo-`.

- [ ] **Step 4: Replace the README first screen with badges, demo, and compact differentiators**

Keep the existing title, tagline, language/policy links, and introductory paragraph. Insert this badge row under the title:

```markdown
[![tests](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml/badge.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/actions/workflows/tests.yml)
[![Python 3.10–3.13](https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/wusuiling-if/mini-code-agent-langgraph/blob/main/LICENSE)
```

Immediately after the introductory paragraph, insert:

```markdown
![`mca demo` fixes a calculator bug, verifies the tests, and submits the patch](https://raw.githubusercontent.com/wusuiling-if/mini-code-agent-langgraph/main/docs/assets/demo.gif)

- **Verification-bound submission:** the selected test command must pass against the current workspace fingerprint before the agent can submit.
- **Inspectable recovery:** redacted trajectories persist each run and can resume safely after an interruption.
- **Conflict-aware Undo:** a private HMAC-authenticated journal rejects post-edit conflicts by default.
```

Leave `## Try it without an API key` directly after these bullets and keep its existing commands unchanged.

- [ ] **Step 5: Inspect the final frame and scan every new launch asset**

Run:

```bash
rm -f /tmp/mca-demo-final.png
ffmpeg -sseof -0.1 -i docs/assets/demo.gif -frames:v 1 /tmp/mca-demo-final.png
rg -n -I '/Users/|/home/|sk-[[:alnum:]_-]{12,}|API_KEY=[^[:space:]]+' README.md docs/demo.tape docs/assets/demo.gif
test -s docs/assets/demo.gif
```

Expected: `/tmp/mca-demo-final.png` visibly shows `Submitted` and `passed`; the scan returns no match; the GIF is non-empty. Use the image viewer to inspect `/tmp/mca-demo-final.png` before committing.

- [ ] **Step 6: Validate README targets and commit the demo**

Run:

```bash
rg -n 'actions/workflows/tests.yml|Python-3.10|License-MIT|raw.githubusercontent.com/.*/docs/assets/demo.gif|Verification-bound submission|Inspectable recovery|Conflict-aware Undo|## Try it without an API key' README.md
python3 - <<'PY'
from pathlib import Path

readme = Path("README.md").read_text(encoding="utf-8")
url = "https://raw.githubusercontent.com/wusuiling-if/mini-code-agent-langgraph/main/docs/assets/demo.gif"
assert url in readme
assert Path("docs/assets/demo.gif").is_file()
PY
git diff --check
```

Expected: every first-screen element is found, the embedded `main` URL maps to
the committed local asset, and diff check exits zero. The HTTP URL is checked
again immediately after merge in Task 5.

Commit:

```bash
git add README.md docs/demo.tape docs/assets/demo.gif
git commit -m "docs: add reproducible terminal demo"
```

---

### Task 3: Add the maintainer-owned launch kit

**Files:**
- Create: `docs/launch/0.3.1.md`

**Interfaces:**
- Consumes: the repository URL, `mca demo`, the three verified differentiators, and official community posting rules.
- Produces: bilingual general-purpose launch drafts, a technical article outline, channel sequencing, and a seven-day observation checklist; it never publishes externally.

- [ ] **Step 1: Create the launch kit with factual copy**

Create `docs/launch/0.3.1.md` with these sections and exact claims:

```markdown
# mini-code-agent-langgraph 0.3.1 launch kit

These are factual working drafts, not text to mass-post unchanged. The maintainer
must verify every claim, rewrite in their own voice, and publish personally.

## English draft

I built `mini-code-agent-langgraph`, a compact coding-agent CLI for developers
who want to inspect the loop instead of hiding it behind a large UI. A run cannot
reach `Submitted` until the selected test command passes against the current
workspace fingerprint. Runs persist redacted, resumable trajectories, and Undo
uses a private HMAC-authenticated journal with conflict checks. `mca demo` fixes
a deterministic calculator bug locally with the mock model, so the first run
needs no API key. Version 0.3.1 is available here:
https://github.com/wusuiling-if/mini-code-agent-langgraph

I would especially value technical feedback on the verification boundary,
trajectory format, and Undo threat model.

## 中文草稿

我做了一个可检查的轻量编程 Agent CLI：`mini-code-agent-langgraph`。它不是
把过程藏在大型界面里，而是把 Agent Loop、工具调用和轨迹保留下来。只有当
用户指定的测试命令在当前工作区指纹上通过后，任务才能进入 `Submitted`；
运行轨迹会脱敏并支持中断恢复；Undo 使用私有的 HMAC 认证日志，并默认拒绝
文件被后续修改后的冲突回滚。第一次体验直接运行 `mca demo`，使用本地 mock
模型修复一个确定性的计算器错误，不需要 API Key。0.3.1 在这里：
https://github.com/wusuiling-if/mini-code-agent-langgraph

我更希望收到关于验证边界、轨迹格式和 Undo 威胁模型的具体技术反馈。

## Technical article outline

1. Why tool execution is not task completion: define the postcondition for a coding agent.
2. Verification-bound submission: bind a passing command to the current workspace fingerprint.
3. Crash recovery without an opaque replay: persist redacted trajectories and invalidate stale verification on resume.
4. Undo as a security boundary: authenticate a private journal with HMAC and reject post-edit conflicts.
5. Threat model and deliberate limits: sandboxing is defense in depth, shell access is off by default, and HMAC is not a digital signature.
6. Reproduce it without a provider: run `mca demo`, inspect the trace, then review the patch and passing tests.
7. Open questions: trajectory portability, framework coupling, and where the minimal agent boundary should end.

## Channel order and anti-spam rules

1. GitHub Release and repository README: publish the canonical facts first. Do not fabricate adoption metrics or ask for Stars.
2. Hacker News, only after the repository is runnable: a personal Show HN title must start with `Show HN`, link to the runnable repository, and explain why it was built. Do not paste generated or AI-edited copy, solicit votes/comments, ask friends to engage, delete-and-repost, or frame a routine patch release as the whole reason to submit.
3. V2EX `分享创造`, only as a maintainer-authored Chinese post: disclose that it is your project, share concrete implementation details, and invite technical criticism. V2EX explicitly says not to send AI-generated content, so do not paste or lightly edit this draft; write the post yourself from verified project facts. Do not duplicate it across nodes or use zero-information replies to bump it.
4. One relevant Reddit community, only after reading that community's current rules: disclose self-promotion, prefer a technical text post over a bare link, and participate in the discussion. Do not repeat the same post across communities, mass-tag users, send unsolicited messages, or solicit votes.
5. A personal blog or social account may link to the longer technical article after it exists. Adapt the framing to that audience instead of copying an identical launch message everywhere.

Official rule references:

- Hacker News: https://news.ycombinator.com/showhn.html and https://news.ycombinator.com/newsguidelines.html
- V2EX: https://www.v2ex.com/about
- Reddit: https://support.reddithelp.com/hc/en-us/articles/360043504051-Spam

## Observation window

Record GitHub traffic once after the Release is live and once roughly seven days later:

- Reach: repository views, unique visitors, and referrers.
- Conversion: Stars and forks interpreted alongside meaningful views, not as a fixed quota.
- Activation: clones, Issues, and concrete technical feedback.

Do not add runtime features during the quiet observation window merely to move
a metric. Treat automated clones and raw views as traffic, not as users.
```

- [ ] **Step 2: Validate claims and community-rule links**

Run:

```bash
rg -n 'Submitted|workspace fingerprint|redacted|resumable|HMAC-authenticated|not a digital signature|Show HN|AI-generated|seven days' docs/launch/0.3.1.md
rg -n '求星|求 Star|please star|upvote this|HMAC is a digital signature|HMAC proves semantic correctness' docs/launch/0.3.1.md
git diff --check
```

Expected: the factual terms and safeguards are present; the solicitation scan returns no match; diff check exits zero.

- [ ] **Step 3: Commit the launch kit**

```bash
git add docs/launch/0.3.1.md
git commit -m "docs: add 0.3.1 launch kit"
```

---

### Task 4: Run release-candidate verification and independent review

**Files:**
- Verify: `tests/test_diagnostics.py`
- Verify: `README.md`
- Verify: `docs/demo.tape`
- Verify: `docs/assets/demo.gif`
- Verify: `docs/launch/0.3.1.md`
- Verify: all existing runtime, packaging, and workflow files in the branch.

**Interfaces:**
- Consumes: completed Tasks 1–3 and the branch-local `.venv`.
- Produces: fresh local evidence that the exact branch commit is fit to push; it does not alter runtime code.

- [ ] **Step 1: Run the complete tests, offline evaluations, and dependency check**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m evals.run_evals --json
.venv/bin/python -m pip check
```

Expected: at least 114 tests pass; all three evaluation fixtures are successful and verified; pip reports no broken requirements.

- [ ] **Step 2: Build and validate exactly one sdist and one wheel**

```bash
rm -rf build dist
.venv/bin/python -m build --sdist --wheel
.venv/bin/python -m twine check dist/*
.venv/bin/python -c "from pathlib import Path; assert len(list(Path('dist').glob('*.tar.gz'))) == 1; assert len(list(Path('dist').glob('*.whl'))) == 1"
```

Expected: one `0.3.1` source distribution, one `0.3.1` wheel, and `twine check` reports `PASSED` for both.

- [ ] **Step 3: Smoke-test the built wheel in a clean environment**

```bash
rm -rf /tmp/mca-release-smoke
python3 -m venv /tmp/mca-release-smoke
/tmp/mca-release-smoke/bin/python -m pip install dist/*.whl
/tmp/mca-release-smoke/bin/mca --version
/tmp/mca-release-smoke/bin/mca --help
/tmp/mca-release-smoke/bin/mca doctor --sandbox none
TMPDIR=/tmp /tmp/mca-release-smoke/bin/mca demo
/tmp/mca-release-smoke/bin/python -m pip check
```

Expected: version is `0.3.1`; help and doctor render; demo exits zero with `Submitted` and `passed`; pip reports no broken requirements.

- [ ] **Step 4: Run repository hygiene and secret-like scans**

```bash
git diff --check origin/main...HEAD
git status --short
.venv/bin/python scripts/check_added_content.py
git log --format='%H %s' origin/main..HEAD
```

Expected: diff check exits zero; only intentional ignored build artifacts may
exist; all tracked additions, including `docs/superpowers/**`, are scanned;
every reported candidate matches an exact approved synthetic value, file, and
line context; macOS and Linux user-home paths are rejected; history is linear
and contains only scoped commits.

- [ ] **Step 5: Obtain two-stage independent review**

Dispatch one reviewer against `docs/superpowers/specs/2026-07-20-measured-github-launch-design.md` for spec compliance, then a separate reviewer for code quality, documentation accuracy, security language, demo disclosure, and release-gate correctness.

Expected: no Critical or Important findings. Fix any valid finding in a focused commit and rerun the affected verification plus Steps 1 and 4.

---

### Task 5: Push, pass GitHub Actions, merge, and update repository metadata

**Files:**
- Publish: all commits on `codex/publish-v0-3-runtime`.
- Modify remotely after merge: repository description and topics only.

**Interfaces:**
- Consumes: a clean locally verified branch and Draft PR `#1`.
- Produces: a green merged `main`, an English repository description, and discoverability topics; Discussions remain disabled.

- [ ] **Step 1: Push the complete branch**

```bash
git push origin codex/publish-v0-3-runtime
```

Expected: origin advances to the exact reviewed local HEAD without force-push.

- [ ] **Step 2: Watch every PR check to completion**

```bash
gh pr checks 1 --watch --interval 15
```

Expected: Python 3.10, 3.11, 3.12, and 3.13 pytest; Linux/macOS/Windows CLI smoke; package; and offline eval all pass. Any failure returns execution to root-cause diagnosis; it does not get waived.

- [ ] **Step 3: Mark the PR ready and merge without rewriting history**

```bash
gh pr ready 1
gh pr merge 1 --merge --delete-branch=false
```

Expected: PR `#1` is merged into `main`; the published feature branch is retained until release completion.

- [ ] **Step 4: Update the public repository description and topics**

Run:

```bash
gh repo edit wusuiling-if/mini-code-agent-langgraph --description "Inspectable LangGraph coding agent with verified patches, resumable trajectories, sandboxed tools, and HMAC-authenticated Undo."
gh api --method PUT repos/wusuiling-if/mini-code-agent-langgraph/topics --input - <<'JSON'
{
  "names": [
    "agent-security",
    "ai-coding",
    "cli",
    "coding-agent",
    "developer-tools",
    "langgraph",
    "llm-agent",
    "python",
    "sandbox",
    "tool-use",
    "trajectory"
  ]
}
JSON
```

Expected: the description matches exactly; the topic list contains the eight existing relevant topics plus `agent-security`, `cli`, and `developer-tools`.

- [ ] **Step 5: Verify the merged landing page and embedded demo**

```bash
curl -fsSI https://raw.githubusercontent.com/wusuiling-if/mini-code-agent-langgraph/main/docs/assets/demo.gif
gh api repos/wusuiling-if/mini-code-agent-langgraph --jq '{description,topics,has_discussions,default_branch}'
gh pr view 1 --json state,mergedAt,mergeCommit,statusCheckRollup
```

Expected: the raw GIF returns HTTP success; `has_discussions` remains `false`; default branch is `main`; PR state is `MERGED`; every rolled-up check succeeded.

---

### Task 6: Enforce the release gate, publish 0.3.1 only if ready, and capture baseline

**Files:**
- No source-file changes.
- Create remotely only when both publishing prerequisites are confirmed: annotated tag `v0.3.1`, PyPI project/version, and GitHub Release `v0.3.1`.

**Interfaces:**
- Consumes: merged, green `main`; exact values from `RELEASING.md`; authenticated read access to GitHub and, if available, the maintainer's PyPI publisher settings.
- Produces: either a successful immutable `0.3.1` release or a precise stop-before-tag report naming the missing prerequisite.

- [ ] **Step 1: Verify the GitHub `pypi` environment and deployment policy**

Run:

```bash
gh api repos/wusuiling-if/mini-code-agent-langgraph/environments/pypi
gh api repos/wusuiling-if/mini-code-agent-langgraph/environments/pypi/deployment-branch-policies
```

Expected: environment name is exactly `pypi`; its deployment policy allows release tags matching `v*`. If the environment is absent or mismatched, stop before tagging and report the exact setting to create or correct.

- [ ] **Step 2: Confirm the PyPI pending Trusted Publisher identity**

In the authenticated PyPI publishing settings, verify these exact values from `RELEASING.md`: project `mini-code-agent-langgraph`, owner `wusuiling-if`, repository `mini-code-agent-langgraph`, workflow `release.yml`, environment `pypi`.

Expected: all five values match. If authenticated settings cannot be inspected or any value is absent/mismatched, stop before tagging and report this one-time manual prerequisite; never substitute a PyPI token.

- [ ] **Step 3: Re-verify the exact merged release commit**

```bash
git fetch origin main
git worktree add --detach /tmp/mca-release-main origin/main
cd /tmp/mca-release-main
python3 -m venv .release-venv
.release-venv/bin/python -m pip install build twine '.[dev]'
.release-venv/bin/python -m pytest -q
.release-venv/bin/python -m evals.run_evals --json
.release-venv/bin/python -m pip check
.release-venv/bin/python -m build --sdist --wheel
.release-venv/bin/python -m twine check dist/*
.release-venv/bin/python - <<'PY'
import sys
import tomllib
from pathlib import Path

root = Path.cwd().resolve()
sys.path.insert(0, str(root / "src"))
from mini_code_agent import __version__ as source_version

project_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
assert source_version == project_version == "0.3.1"
assert f"v{project_version}" == "v0.3.1"
PY
```

Expected: the merged commit passes all tests/evals/package checks and both source version declarations equal `0.3.1`.

- [ ] **Step 4: Create and push the immutable tag only after Steps 1–3 pass**

```bash
git tag -a v0.3.1 -m "mini-code-agent-langgraph 0.3.1"
git push origin v0.3.1
```

Expected: the tag points at the verified merged `main` commit and starts the `release.yml` workflow.

- [ ] **Step 5: Watch OIDC publication and verify PyPI before creating the GitHub Release**

```bash
gh run list --workflow release.yml --limit 1
gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status
curl -fsS https://pypi.org/pypi/mini-code-agent-langgraph/0.3.1/json
```

Expected: build and protected `pypi` jobs succeed; PyPI returns metadata for exactly `0.3.1`. A failed publish blocks the GitHub Release until diagnosed and never causes the tag/version to be moved or reused.

- [ ] **Step 6: Create and verify the matching GitHub Release**

```bash
gh release create v0.3.1 --verify-tag --generate-notes --title "mini-code-agent-langgraph 0.3.1"
gh release view v0.3.1 --json tagName,name,isDraft,isPrerelease,url
```

Expected: one non-draft, non-prerelease GitHub Release exists for exact tag `v0.3.1`.

- [ ] **Step 7: Capture the near-launch funnel baseline without a Star quota**

```bash
gh api repos/wusuiling-if/mini-code-agent-langgraph --jq '{captured_at: now,stargazers_count,forks_count,subscribers_count,open_issues_count}'
gh api repos/wusuiling-if/mini-code-agent-langgraph/traffic/views
gh api repos/wusuiling-if/mini-code-agent-langgraph/traffic/clones
gh api repos/wusuiling-if/mini-code-agent-langgraph/traffic/popular/referrers
```

Expected: record reach (views/referrers), conversion signals (Stars/forks), and activation signals (clones/Issues) once near launch. Repeat after roughly seven quiet days before choosing another feature investment.
