# Releasing

Releases are built by GitHub Actions and published to PyPI with OpenID Connect
(OIDC). The repository must not contain a PyPI token, username, or password.

## One-time setup for the first release

Before creating any release tag:

1. In PyPI's publishing settings, create a pending Trusted Publisher with these
   exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `mini-code-agent-langgraph` |
   | GitHub owner | `wusuiling-if` |
   | GitHub repository | `mini-code-agent-langgraph` |
   | Workflow filename | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher creates the project on the first successful publish. It
   does not reserve the project name, so complete the release promptly after
   configuring it.
2. In the GitHub repository, create an environment named `pypi`. Configure its
   deployment branch and tag policy so only tags matching `v*` may deploy.
   Add a required reviewer when the repository plan and team setup support it.
3. Confirm that `.github/workflows/release.yml` still names the `pypi`
   environment and requests only `id-token: write` in its publish job.

Do not create a tag until both the pending PyPI publisher and the protected
GitHub environment exist.

## Prepare and verify a release

Release only a reviewed commit that has been merged to `main`. From a clean,
up-to-date checkout of `main`, confirm that `pyproject.toml` and
`mini_code_agent.__version__` contain the intended version, then run:

```bash
python -m pytest -q
python -m evals.run_evals --json
python -m pip check
python -m build --sdist --wheel
python -m twine check dist/*
```

Derive the release version from `pyproject.toml`, then verify the exact
source/package/tag relationship locally:

```bash
VERSION="$(awk -F '"' '/^version = / { print $2; exit }' pyproject.toml)"
test -n "$VERSION"
export VERSION
python - <<'PY'
import os
import re
import sys
from pathlib import Path

root = Path.cwd().resolve()
sys.path.insert(0, str(root / "src"))
from mini_code_agent import __version__ as source_version

version = os.environ["VERSION"]
assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
assert source_version == version, (source_version, version)
assert f"v{version}"[1:] == version
print(f"release version: {version}; tag: v{version}")
PY
```

The release workflow repeats the version assertion, rejects any tag that is not
exactly `v` followed by the package version, and verifies that the tagged commit
is reachable from `origin/main` before building any package artifacts.

## Tag and publish

After every required check and review succeeds on merged `main`, create and
push one annotated tag:

```bash
git switch main
git pull --ff-only
git status --short
VERSION="$(awk -F '"' '/^version = / { print $2; exit }' pyproject.toml)"
test -n "$VERSION"
git tag -a "v${VERSION}" -m "mini-code-agent-langgraph ${VERSION}"
git push origin "v${VERSION}"
```

Watch the `release.yml` workflow. Its build job validates one source archive and
one wheel, then the protected `pypi` job publishes those exact artifacts through
Trusted Publishing. Confirm the release on PyPI before continuing.

After the workflow succeeds, create the matching GitHub Release:

```bash
VERSION="$(awk -F '"' '/^version = / { print $2; exit }' pyproject.toml)"
test -n "$VERSION"
gh release create "v${VERSION}" --verify-tag --generate-notes \
  --title "mini-code-agent-langgraph ${VERSION}"
```

## Fix-forward rule

Published package versions and release tags are immutable. Never reuse, move,
delete and recreate, or force-push an already published version or tag. If a
release is wrong, fix the problem on `main`, increment the version, review the
change, and publish a new tag.
