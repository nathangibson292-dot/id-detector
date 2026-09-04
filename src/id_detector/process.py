"""Bounded subprocess execution with whole-process-tree cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class ProcessResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessError(RuntimeError):
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        super().__init__(f"command exited {result.returncode}: {detail[-2000:]}")


class ProcessTimeout(TimeoutError):
    pass


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _kill_psutil_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    descendants: list[psutil.Process] = []
    for _ in range(3):
        try:
            descendants = root.children(recursive=True)
        except psutil.Error:
            break
        for child in reversed(descendants):
            with contextlib.suppress(psutil.Error):
                child.terminate()
        _, alive = psutil.wait_procs(descendants, timeout=0.25)
        for child in alive:
            with contextlib.suppress(psutil.Error):
                child.kill()
        psutil.wait_procs(alive, timeout=0.25)
    try:
        root.terminate()
        root.wait(timeout=0.5)
    except psutil.TimeoutExpired:
        try:
            root.kill()
            root.wait(timeout=0.5)
        except psutil.Error:
            pass
    except psutil.Error:
        pass


async def _run_posix(
    args: Sequence[str], cwd: Path | None, env: Mapping[str, str] | None, timeout: float
) -> ProcessResult:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        await asyncio.to_thread(_kill_psutil_tree, process.pid)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 2)
        if isinstance(sys.exception(), asyncio.CancelledError):
            raise
        raise ProcessTimeout(f"command timed out after {timeout:g}s") from None
    return ProcessResult(tuple(args), process.returncode or 0, _decode(stdout), _decode(stderr))


def _read_win32_pipe(handle: object) -> bytes:
    import pywintypes
    import win32file

    chunks: list[bytes] = []
    while True:
        try:
            _, data = win32file.ReadFile(handle, 64 * 1024)
        except pywintypes.error as exc:
            if exc.winerror in {109, 232}:  # broken/no-data pipe after all writers close
                break
            raise
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


async def _wait_win32_handle(handle: object, timeout: float) -> bool:
    """Poll a process handle without pinning an executor thread for the full timeout."""

    import win32event

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = win32event.WaitForSingleObject(handle, 0)
        if result == win32event.WAIT_OBJECT_0:
            return True
        if result != win32event.WAIT_TIMEOUT:
            raise OSError(f"unexpected Win32 wait result: {result}")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))


async def _run_windows(
    args: Sequence[str], cwd: Path | None, env: Mapping[str, str] | None, timeout: float
) -> ProcessResult:
    # CreateProcess is intentionally used directly so the child is suspended until it has been
    # assigned to the kill-on-close Job Object. No descendant can escape between spawn and assign.
    import win32api
    import win32con
    import win32job
    import win32pipe
    import win32process
    import win32security

    security = win32security.SECURITY_ATTRIBUTES()
    security.bInheritHandle = True
    stdout_read, stdout_write = win32pipe.CreatePipe(security, 0)
    stderr_read, stderr_write = win32pipe.CreatePipe(security, 0)
    win32api.SetHandleInformation(stdout_read, win32con.HANDLE_FLAG_INHERIT, 0)
    win32api.SetHandleInformation(stderr_read, win32con.HANDLE_FLAG_INHERIT, 0)

    startup = win32process.STARTUPINFO()
    startup.dwFlags |= win32process.STARTF_USESTDHANDLES
    startup.hStdOutput = stdout_write
    startup.hStdError = stderr_write
    startup.hStdInput = win32api.GetStdHandle(win32api.STD_INPUT_HANDLE)

    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)

    process_handle = thread_handle = None
    read_tasks: list[asyncio.Task[bytes]] = []
    try:
        creation_flags = win32process.CREATE_SUSPENDED | win32process.CREATE_NO_WINDOW
        process_handle, thread_handle, process_id, _ = win32process.CreateProcess(
            None,
            subprocess.list2cmdline([str(item) for item in args]),
            None,
            None,
            True,
            creation_flags,
            dict(env) if env else None,
            str(cwd) if cwd else None,
            startup,
        )
        win32job.AssignProcessToJobObject(job, process_handle)
        win32process.ResumeThread(thread_handle)
        thread_handle.Close()
        thread_handle = None
        stdout_write.Close()
        stderr_write.Close()

        read_tasks = [
            asyncio.create_task(asyncio.to_thread(_read_win32_pipe, stdout_read)),
            asyncio.create_task(asyncio.to_thread(_read_win32_pipe, stderr_read)),
        ]
        if not await _wait_win32_handle(process_handle, timeout):
            job.Close()
            job = None
            await _wait_win32_handle(process_handle, 5)
            raise ProcessTimeout(f"command timed out after {timeout:g}s")
        stdout, stderr = await asyncio.wait_for(asyncio.gather(*read_tasks), 5)
        read_tasks.clear()
        return ProcessResult(
            tuple(str(item) for item in args),
            win32process.GetExitCodeProcess(process_handle),
            _decode(stdout),
            _decode(stderr),
        )
    except asyncio.CancelledError:
        if job is not None:
            job.Close()
            job = None
        if process_handle is not None:
            await _wait_win32_handle(process_handle, 5)
        if read_tasks:
            await asyncio.wait_for(asyncio.gather(*read_tasks, return_exceptions=True), 5)
            read_tasks.clear()
        raise
    finally:
        for task in read_tasks:
            task.cancel()
        for handle in (stdout_write, stderr_write, stdout_read, stderr_read, thread_handle):
            with contextlib.suppress(AttributeError, OSError):
                handle.Close()
        if process_handle is not None:
            process_handle.Close()
        if job is not None:
            job.Close()


async def run_process(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 600,
    check: bool = True,
) -> ProcessResult:
    """Run an argument vector without a shell and enforce a bounded whole-tree lifetime."""

    if not args:
        raise ValueError("process argument vector cannot be empty")
    string_args = [os.fspath(item) for item in args]
    result = (
        await _run_windows(string_args, cwd, env, timeout)
        if sys.platform == "win32"
        else await _run_posix(string_args, cwd, env, timeout)
    )
    if check and result.returncode:
        raise ProcessError(result)
    return result
