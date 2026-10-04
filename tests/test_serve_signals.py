"""pvdx-serve shutdown: SIGTERM outside Docker (kill, launchd, systemd) stops the workers like Ctrl-C does."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

import pvdx_ground

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signals")

# Run the same pvdx_ground the tests import (a checkout or worktree), not whatever the venv has installed.
PACKAGE_ROOT = Path(pvdx_ground.__file__).resolve().parents[1]
# pvdx-serve with a stand-in ingest worker that only waits for stop: exercises the --no-api loop offline.
FAKE_INGEST = (
    "import sys\n"
    "import pvdx_ground.serve.__main__ as serve\n"
    "serve.run_ingest = lambda settings, poll, stop: stop.wait()\n"
    "sys.exit(serve.main(sys.argv[1:]))\n"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(condition: Callable[[], bool], proc: subprocess.Popen, output: list[str], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if proc.poll() is not None:
            pytest.fail(f"pvdx-serve exited early with {proc.returncode}:\n{''.join(output)}")
        if time.monotonic() > deadline:
            pytest.fail(f"pvdx-serve not ready after {timeout:.0f}s:\n{''.join(output)}")
        time.sleep(0.05)


@contextlib.contextmanager
def serve_process(
    tmp_path: Path, command: list[str], *args: str, **env_extra: str
) -> Iterator[tuple[subprocess.Popen, list[str]]]:
    # A minimal environment and a cwd without .env: nothing from the developer's shell or the repo .env leaks in.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")}
    env.update(PYTHONPATH=str(PACKAGE_ROOT), PYTHONDONTWRITEBYTECODE="1", STATE_DB=str(tmp_path / "state.db"),
               API_HOST="127.0.0.1", **env_extra)
    proc = subprocess.Popen(
        [sys.executable, *command, "--env", str(tmp_path / "missing.env"), *args],
        cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output: list[str] = []

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output.append(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        yield proc, output
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        reader.join(timeout=5)  # the pipe is at EOF once the process is gone


def api_up(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
    except httpx.HTTPError:
        return False
    return True


def stop_with_sigterm(proc: subprocess.Popen) -> tuple[int, float]:
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail("pvdx-serve did not exit within 20s of SIGTERM")
    return code, time.monotonic() - started


def test_sigterm_stops_the_api_and_runs_worker_shutdown(tmp_path):
    port = free_port()
    args = ("--no-ingest", "--no-decode", "--port", str(port))
    with serve_process(tmp_path, ["-m", "pvdx_ground.serve"], *args) as (proc, output):
        wait_for(lambda: api_up(port), proc, output)
        code, _ = stop_with_sigterm(proc)
    log = "".join(output)
    assert code == 0, log
    assert "shutting down workers" in log


def test_sigterm_in_no_api_mode_stops_workers_promptly(tmp_path):
    with serve_process(tmp_path, ["-c", FAKE_INGEST], "--no-api", "--no-decode", NORAD_CAT_ID="62394") as (proc, output):
        wait_for(lambda: any("ingest worker started" in line for line in output), proc, output)
        code, elapsed = stop_with_sigterm(proc)
    log = "".join(output)
    assert code == 0, log
    assert "shutting down workers" in log and "all workers stopped" not in log
    assert elapsed < 10, log  # the wait loop reacted and the worker saw stop (no 15 s join timeout)
