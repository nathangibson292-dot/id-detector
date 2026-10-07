from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from id_detector.process import ProcessTimeout, run_process

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows inherited-pipe regression test")
def test_child_starts_while_another_thread_blocks_on_parent_stdin() -> None:
    """Model the supervised worker: its watcher owns a pending read on the parent pipe."""

    harness = r"""
import asyncio
import os
import sys
import threading
import time

from id_detector.process import run_process

reading = threading.Event()

def watch_parent_pipe():
    reading.set()
    sys.stdin.buffer.read(4096)

threading.Thread(target=watch_parent_pipe, daemon=True).start()
assert reading.wait(1)
time.sleep(0.25)
result = asyncio.run(run_process([sys.executable, "-c", "print('ok')"], timeout=30))
print(result.stdout.strip(), flush=True)
os._exit(0)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), value] if (value := env.get("PYTHONPATH")) else [str(ROOT / "src")]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", harness],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("run_process child timed out while the worker-like stdin read was pending")
        stdout = process.stdout.read() if process.stdout is not None else ""
        stderr = process.stderr.read() if process.stderr is not None else ""
        assert returncode == 0, stderr
        assert stdout.strip() == "ok"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object acceptance test")
def test_job_object_kills_ytdlp_spawned_ffmpeg_child(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "ffmpeg-child.pid"
    # This models yt-dlp's process shape: the managed downloader launches an ffmpeg descendant
    # that inherits its handles and outlives the immediate operation unless the whole Job dies.
    downloader = (
        "import subprocess,sys,time; from pathlib import Path; "
        "ffmpeg=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(ffmpeg.pid),encoding='utf-8'); time.sleep(60)"
    )
    with pytest.raises(ProcessTimeout):
        asyncio.run(run_process([sys.executable, "-c", downloader, str(child_pid_path)], timeout=1))
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object acceptance test")
def test_cancelling_job_object_returns_promptly_and_kills_descendant(tmp_path: Path) -> None:
    async def scenario() -> tuple[float, int]:
        child_pid_path = tmp_path / "cancelled-child.pid"
        parent = (
            "import subprocess,sys,time; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
            "Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8'); time.sleep(600)"
        )
        task = asyncio.create_task(
            run_process([sys.executable, "-c", parent, str(child_pid_path)], timeout=7200)
        )
        deadline = time.monotonic() + 10
        while not child_pid_path.is_file() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + 3
        while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert not psutil.pid_exists(child_pid)
        return elapsed, child_pid

    elapsed, _ = asyncio.run(scenario())
    assert elapsed < 3
