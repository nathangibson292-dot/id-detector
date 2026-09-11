param([string]$BrowserPath = "", [int]$Port = 8792)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$gateRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("idea-local-gate-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $gateRoot | Out-Null
$process = $null
$browser = $null
$listener = $null
$helper = Join-Path $gateRoot "gate.py"
# Python preserves an explicitly empty environment value on Windows; PowerShell 5.1 removes it.
@'
import os
import subprocess
import sys
from pathlib import Path

os.environ["AUDD_API_TOKEN"] = ""
os.environ["IDEA_TEST_MODE"] = "1"
repo, root, port, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
sys.path.insert(0, str(repo))
work = root / "work"
if mode == "serve":
    # The server analyses nothing here (--no-analyse), so the Shazam kill-switch stays on.
    # The prepare phase below must NOT set it: since 1b-iii it is a real refusal (plan
    # §2.3.5), and the golden Free run reaches no network anyway - _analyse is handed an
    # injected FakeShazamHTTP transport, so no request can leave this process.
    os.environ["IDEA_ENGINE_SHAZAM"] = "off"
    raise SystemExit(subprocess.call([str(repo / "idea.cmd"), "--no-open", "--no-analyse",
                                     "--port", port, "--work-root", str(work)], cwd=repo))
from scripts.make_golden import run_local_free
from id_detector.cli import _load_cached
from id_detector.io import native_path
page = run_local_free(work)
media = page.parents[3]
assert _load_cached(work, media.name) is not None
page_url = "/" + page.parent.joinpath("index.html").relative_to(Path(native_path(work))).as_posix()
probe = work / "gate" / "probe" / "present" / "index.html"
probe.parent.mkdir(parents=True)
probe.write_text('''<!doctype html><title>Local audio gate</title><pre id="log">waiting</pre>
<iframe id="mix" src="''' + page_url + '''"></iframe><script>
const frame = document.getElementById('mix');
frame.onload = () => {
  const audio = frame.contentDocument.querySelector('audio');
  if (!audio) { document.getElementById('log').textContent = 'no audio element'; return; }
  audio.addEventListener('error', () => { document.getElementById('log').textContent = 'audio-error'; });
  audio.addEventListener('seeked', () => {
    if (audio.currentTime >= 1) {
      console.log('audio-ok'); document.getElementById('log').textContent = 'audio-ok';
      new Image().src = 'http://127.0.0.1:''' + str(int(port) + 1) + '''/audio-ok';
    }
  });
  const seek = () => { audio.currentTime = 2; };
  if (audio.readyState >= 1) seek(); else audio.addEventListener('loadedmetadata', seek, {once:true});
};
</script>''', encoding="utf-8")
print("prepared offline cached mix " + media.name)
'@ | Set-Content -Encoding UTF8 $helper

try {
    if (-not $BrowserPath) {
        $candidates = @(
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
        )
        $BrowserPath = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (-not $BrowserPath -or -not (Test-Path -LiteralPath $BrowserPath)) {
        throw "No Chromium browser found. Re-run with -BrowserPath pointing to Edge or Chrome."
    }
    & uv run python $helper $repoRoot $gateRoot $Port prepare
    if ($LASTEXITCODE -ne 0) { throw "Offline fixture preparation failed" }
    $arguments = 'run python "{0}" "{1}" "{2}" {3} serve' -f $helper, $repoRoot, $gateRoot, $Port
    $process = Start-Process -FilePath "uv" -ArgumentList $arguments -WorkingDirectory $repoRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $gateRoot "serve.log") `
        -RedirectStandardError (Join-Path $gateRoot "serve.err")
    $base = "http://127.0.0.1:$Port"
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) { throw "idea.cmd exited early" }
        try {
            $health = Invoke-RestMethod "$base/healthz" -TimeoutSec 1
            if ($health.ok) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $ready) { throw "idea.cmd did not become healthy" }
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, ($Port + 1))
    $listener.Start()
    $browserArgs = '--headless --disable-gpu --no-first-run --remote-debugging-port=0 --user-data-dir="{0}" "{1}/gate/probe/present/index.html"' -f (Join-Path $gateRoot "browser"), $base
    $browser = Start-Process -FilePath $BrowserPath -ArgumentList $browserArgs -WindowStyle Hidden `
        -PassThru -RedirectStandardOutput (Join-Path $gateRoot "browser.log") `
        -RedirectStandardError (Join-Path $gateRoot "browser.err")
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $audioOk = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not $listener.Pending()) { Start-Sleep -Milliseconds 100; continue }
        $client = $listener.AcceptTcpClient()
        try {
            $stream = $client.GetStream()
            $stream.ReadTimeout = 2000
            $reader = New-Object System.IO.StreamReader($stream)
            $request = $reader.ReadLine()
            $body = [System.Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Length: 0`r`nConnection: close`r`n`r`n")
            $stream.Write($body, 0, $body.Length)
            if ($request -eq "GET /audio-ok HTTP/1.1") { $audioOk = $true; break }
        } finally { $client.Close() }
    }
    if (-not $audioOk) { throw "Probe did not log audio-ok after metadata and seek; inspect $gateRoot" }
    Write-Output "local gate passed: idea.cmd; cached mix open; audio metadata + seek; probe logged audio-ok"
    Write-Output "Evidence: $gateRoot"
} finally {
    if ($null -ne $listener) { $listener.Stop() }
    foreach ($child in @($browser, $process)) {
        if ($null -ne $child -and -not $child.HasExited) {
            # Edge can exit one subprocess between HasExited and taskkill's tree walk. Swallow
            # only that race: if the process really is gone, cleanup succeeded; any other
            # taskkill failure leaves it running and must still fail the gate.
            try { & taskkill.exe /PID $child.Id /T /F 2>$null | Out-Null }
            catch {
                $child.Refresh()
                if (-not $child.HasExited) { throw }
            }
        }
    }
}
