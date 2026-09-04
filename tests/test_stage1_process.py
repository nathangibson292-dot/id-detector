from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import psutil
import pytest

from id_detector.process import ProcessTimeout, run_process


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
