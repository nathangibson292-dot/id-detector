from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_built_wheel_contains_and_loads_runtime_resources(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    built = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(wheel_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, built.stdout + built.stderr
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "id_detector/resources/jobs.sql" in names
    assert "id_detector/resources/provider_configs/shazam-v3.json" in names

    program = """
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import id_detector
from id_detector.jobs import AsyncJobStore
from id_detector.recognise import load_provider_config
assert '.whl' in id_detector.__file__
config, name = load_provider_config(Path.cwd())
assert config.measured and name == 'shazam-v3.json'
async def smoke():
    async with AsyncJobStore(Path(sys.argv[2])):
        pass
asyncio.run(smoke())
print(name)
"""
    installed = subprocess.run(
        [sys.executable, "-c", program, str(wheel), str(tmp_path / "installed.sqlite")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    assert installed.stdout.strip() == "shazam-v3.json"
