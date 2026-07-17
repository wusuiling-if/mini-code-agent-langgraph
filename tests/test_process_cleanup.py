from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

import mini_code_agent.executor as executor_module
from mini_code_agent.executor import BashExecutor, _DockerRunMetadata


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("wait failed")])
def test_run_argv_cleans_up_on_any_wait_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    executor = BashExecutor(tmp_path, sandbox_mode="none")
    cleaned: list[object] = []

    class FailingProcess:
        pid = 12345
        returncode = None

        def wait(self, timeout=None):
            raise failure

    process = FailingProcess()
    monkeypatch.setattr(executor_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(executor, "_terminate_process_group", cleaned.append)

    with pytest.raises(type(failure), match=str(failure) or None):
        executor._run_argv(["/bin/sh", "-c", ":"], sandbox=False)

    assert cleaned == [process]


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_timeout_kills_background_descendant(tmp_path: Path) -> None:
    executor = BashExecutor(tmp_path, sandbox_mode="none")
    pidfile = tmp_path / "child.pid"
    command = (
        "sleep 30 & "
        f"echo $! > {shlex.quote(str(pidfile))}; "
        "wait"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        executor._run_argv(
            ["/bin/sh", "-c", command],
            sandbox=False,
            timeout_seconds=1,
        )

    child_pid = int(pidfile.read_text(encoding="ascii").strip())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail(f"background child {child_pid} survived command timeout")


def test_docker_cleanup_prefers_cid_and_removes_cidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = BashExecutor(tmp_path, sandbox_mode="none")
    cidfile = tmp_path / "container.cid"
    container_id = "a" * 64
    cidfile.write_text(container_id, encoding="ascii")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(executor_module.subprocess, "run", fake_run)
    executor._cleanup_docker_run(
        _DockerRunMetadata(
            executable="/usr/local/bin/docker",
            name="mca-test-container",
            cidfile=cidfile,
        )
    )

    assert calls == [["/usr/local/bin/docker", "rm", "-f", container_id]]
    assert not cidfile.exists()


def test_docker_runs_receive_unique_names_and_cidfiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = BashExecutor(tmp_path, sandbox_mode="docker")
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: "/usr/local/bin/docker" if name == "docker" else "",
    )

    first = executor._sandboxed_argv(["python", "--version"])
    second = executor._sandboxed_argv(["python", "--version"])
    first_name = first[first.index("--name") + 1]
    second_name = second[second.index("--name") + 1]
    first_cidfile = first[first.index("--cidfile") + 1]
    second_cidfile = second[second.index("--cidfile") + 1]

    assert first_name != second_name
    assert first_cidfile != second_cidfile
    assert first_name.startswith("mca-")
    assert first_cidfile.endswith(".cid")
