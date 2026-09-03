"""Offline runtime preflight used by ``id-detector doctor``."""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import importlib.metadata
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """Bounded, repeated process-tree cleanup after a timeout on every platform."""

    try:
        root = psutil.Process(process.pid)
        # Keep the root alive while descendants are enumerated repeatedly so a child created
        # during cleanup is seen on a later pass. Stage 1 will replace this with the shared
        # Windows Job Object launcher used by long-running pipeline commands.
        for _ in range(3):
            descendants = root.children(recursive=True)
            if not descendants:
                break
            for child in descendants:
                try:
                    child.terminate()
                except psutil.Error:
                    continue
            _, alive = psutil.wait_procs(descendants, timeout=0.25)
            for item in alive:
                try:
                    item.kill()
                except psutil.Error:
                    continue
            psutil.wait_procs(alive, timeout=0.25)
        root.terminate()
        try:
            root.wait(timeout=0.5)
        except psutil.TimeoutExpired:
            root.kill()
            root.wait(timeout=0.5)
    except psutil.Error:
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()


def _drain_after_timeout(process: subprocess.Popen[str]) -> None:
    """Drain pipes without allowing inherited descendant handles to block indefinitely."""

    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None and process.stderr is not process.stdout:
            process.stderr.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.kill()


def _run_version(command: list[str], timeout: int = 10) -> tuple[bool, str]:
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        _drain_after_timeout(process)
        return False, f"timed out after {timeout}s"
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "no output")
    return process.returncode == 0, first_line


def _write_test_tone(path: Path) -> None:
    """Generate a 12 s, 16 kHz, mono s16le WAV with changing harmonic tones."""

    sample_rate = 16_000
    samples = array("h")
    phase = 0.0
    for index in range(sample_rate * 12):
        section = index // (sample_rate // 4)
        frequency = 310 + (section * 137) % 2800
        phase += 2 * math.pi * frequency / sample_rate
        value = int(11_000 * math.sin(phase) + 4_000 * math.sin(phase * 1.503))
        samples.append(max(-32_768, min(32_767, value)))
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def _check_shazam() -> Check:
    try:
        importlib.import_module("shazamio")
        core = importlib.import_module("shazamio_core")
        with tempfile.TemporaryDirectory(prefix="id-detector-doctor-") as directory:
            path = Path(directory) / "synthetic.wav"
            _write_test_tone(path)
            recognizer = core.Recognizer(segment_duration_seconds=12)

            async def generate_signature() -> object:
                return await recognizer.recognize_path(str(path))

            signature = asyncio.run(generate_signature())
            encoded = getattr(signature, "signature", None)
            if not encoded:
                return Check("Shazam signature", "FAIL", "generated signature was empty")
        versions = (
            f"shazamio {importlib.metadata.version('shazamio')}, "
            f"shazamio-core {importlib.metadata.version('shazamio-core')}; 12 s WAV signed offline"
        )
        return Check("Shazam signature", "PASS", versions)
    except Exception as exc:  # preflight must report import/native failures instead of crashing
        return Check("Shazam signature", "FAIL", f"{type(exc).__name__}: {exc}")


def _check_vc_runtime() -> Check:
    if sys.platform != "win32":
        return Check("Visual C++ runtime", "WARN", "best-effort check applies only to Windows")
    missing: list[str] = []
    for library in ("vcruntime140.dll", "vcruntime140_1.dll"):
        try:
            ctypes.WinDLL(library)  # type: ignore[attr-defined]
        except OSError:
            missing.append(library)
    if missing:
        return Check("Visual C++ runtime", "WARN", f"not loadable: {', '.join(missing)}")
    return Check("Visual C++ runtime", "PASS", "vcruntime140 and vcruntime140_1 loadable")


def collect_checks() -> list[Check]:
    checks: list[Check] = []
    uv = shutil.which("uv")
    ok, detail = _run_version([uv, "--version"]) if uv else (False, "not found on PATH")
    checks.append(Check("uv", "PASS" if ok else "FAIL", detail))

    python_ok = sys.version_info[:2] == (3, 12)
    checks.append(Check("Python", "PASS" if python_ok else "FAIL", sys.version.split()[0]))

    for executable in ("ffmpeg", "ffprobe"):
        resolved = shutil.which(executable)
        ok, detail = (
            _run_version([resolved, "-version"]) if resolved else (False, "not found on PATH")
        )
        checks.append(Check(executable, "PASS" if ok else "FAIL", detail))

    ok, detail = _run_version([sys.executable, "-m", "yt_dlp", "--version"])
    checks.append(Check("yt-dlp module", "PASS" if ok else "FAIL", detail))

    node = shutil.which("node")
    ok, detail = _run_version([node, "--version"]) if node else (False, "not found on PATH")
    checks.append(Check("Node", "PASS" if ok else "FAIL", detail))
    checks.append(_check_shazam())
    checks.append(_check_vc_runtime())

    free = shutil.disk_usage(Path.cwd()).free
    gib = free // (1024**3)
    status = "PASS" if free >= 5 * 1024**3 else "WARN" if free >= 1024**3 else "FAIL"
    checks.append(Check("Free disk", status, f"{gib} GiB available at {Path.cwd().anchor}"))
    return checks


def run_doctor() -> int:
    checks = collect_checks()
    name_width = max(len(check.name) for check in checks)
    status_width = 6
    print(f"{'CHECK':<{name_width}}  {'STATUS':<{status_width}}  DETAIL")
    print(f"{'-' * name_width}  {'-' * status_width}  {'-' * 50}")
    for check in checks:
        print(f"{check.name:<{name_width}}  {check.status:<{status_width}}  {check.detail}")
    return 1 if any(check.status == "FAIL" for check in checks) else 0
