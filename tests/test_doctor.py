from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil

from id_detector.doctor import _run_version


def test_timed_out_command_cleans_descendants_without_unbounded_pipe_wait(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    program = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "stdout=sys.stdout,stderr=sys.stderr); "
        "Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8'); time.sleep(30)"
    )
    child_pid: int | None = None
    started = time.monotonic()
    try:
        ok, detail = _run_version([sys.executable, "-c", program, str(child_pid_path)], timeout=1)
        elapsed = time.monotonic() - started
        assert not ok
        assert detail == "timed out after 1s"
        assert elapsed < 6
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(child_pid)
    finally:
        if child_pid is not None and psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()
