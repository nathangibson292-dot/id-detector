param(
    [int]$Port = 8791,
    [string]$OutputDir = "docs/screenshots/3a-ii"
)

# Non-blocking accessibility/appearance evidence for cycle 3a-ii (PLAN-v2 §5, 3a-ii gate).
#
# Captures BOTH page types that matter at BOTH widths -- the home form and a *populated* result page
# at 1280 px and at 390 px (well inside the 720 px breakpoint) -- and runs the vendored
# axe-core 4.10.2 over each.  The result page is seeded from the committed presentation fixtures into
# a scratch work root, so this never touches the real `work/` tree and never makes a provider call.
# Every missing prerequisite (no Edge, no fixture) is reported and exits 0: this is evidence, not a
# gate.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$output = Join-Path $root $OutputDir
$env:IDEA_TEST_MODE = "1"
$env:AUDD_API_TOKEN = ""
New-Item -ItemType Directory -Force -Path $output | Out-Null
$edgeCandidates = @(
    "$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
)
$edge = $edgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $edge) {
    Write-Host "screenshot/axe evidence skipped: Microsoft Edge was not found"
    exit 0
}

# --- seed a scratch work root with one real, populated result bundle -----------------------------
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("idea-shots-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$seeder = @'
import sys
from pathlib import Path

sys.path.insert(0, ".")
from tests.test_projection import _publish

work = Path(sys.argv[1])
_, _, item, _ = _publish(work, "garage", collapse=True)
# The server serves by on-disk path, which is not necessarily the source record's keys.
print(f"{item.media_dir.parent.name}/{item.media_dir.name}")
'@
$seedScript = Join-Path $work "seed.py"
Set-Content -LiteralPath $seedScript -Value $seeder -Encoding UTF8
$env:PYTHONIOENCODING = "utf-8"
$keys = (& uv run python $seedScript $work 2>$null | Select-Object -Last 1)
if (-not $keys) {
    Write-Host "screenshot/axe evidence skipped: could not seed a fixture result page"
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
}

$server = Start-Process -FilePath "uv" -ArgumentList @(
    "run", "idea", "serve", "--no-open", "--port", $Port, "--work-root", $work
) -WorkingDirectory $root -WindowStyle Hidden -PassThru
try {
    $base = "http://127.0.0.1:$Port"
    $ready = $false
    foreach ($attempt in 1..40) {
        try {
            $health = Invoke-RestMethod -Uri "$base/healthz" -TimeoutSec 1
            if ($health.ok) { $ready = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "local server did not become ready" }

    $axe = Get-Content -Raw (Join-Path $root `
        "src/id_detector/webapp/vendor/axe-core-4.10.2.min.js")
    $pages = @(
        @{ name = "home";   url = "$base/" },
        @{ name = "result"; url = "$base/$keys/present/index.html" }
    )
    foreach ($page in $pages) {
        # Desktop: the window size is the layout viewport, so shoot the page directly.
        & $edge --headless --disable-gpu --hide-scrollbars --window-size=1280,900 `
            "--screenshot=$(Join-Path $output ("{0}-1280.png" -f $page.name))" $page.url | Out-Null

        # Phone: this Edge build does NOT honour --window-size as the LAYOUT viewport -- a
        # "--window-size=390,844" capture lays the page out at desktop width and simply crops the
        # image to 390 px, so the <= 720 px media query never fires and the shot is not mobile
        # evidence at all.  An iframe gives its content a real 390 px layout viewport, so the
        # breakpoint fires and what is photographed is the phone layout.
        $frame = Join-Path ([System.IO.Path]::GetTempPath()) `
            ("idea-390-" + [guid]::NewGuid().ToString("N") + ".html")
        Set-Content -LiteralPath $frame -Encoding UTF8 -Value (
            '<!doctype html><body style="margin:0;background:#0a0a0f">' +
            '<iframe src="' + $page.url + '" style="width:390px;height:2200px;border:0" ' +
            'scrolling="no"></iframe></body>')
        try {
            & $edge --headless --disable-gpu --hide-scrollbars --window-size=400,2200 `
                --virtual-time-budget=12000 `
                "--screenshot=$(Join-Path $output ("{0}-390.png" -f $page.name))" `
                ([uri]$frame).AbsoluteUri | Out-Null
        } finally {
            Remove-Item -LiteralPath $frame -Force
        }

        $html = (Invoke-WebRequest -UseBasicParsing -Uri $page.url).Content
        $label = $page.name
        $runner = @"
<script>$axe</script><script>
axe.run().then(function(result){
  document.body.innerHTML='<pre id="axe-report">'+
    JSON.stringify({url:'$label',violations:result.violations},null,2)+'</pre>';
});
</script></body>
"@
        # The instrumented page replaces its own body with axe's violation list, and a screenshot of
        # that is the report.  `--dump-dom` is deliberately NOT used: current Edge/Chromium builds
        # accept the flag and emit nothing, which is how an axe "report" can appear to be produced
        # while no file is ever written.  The instrumented page itself stays in TEMP and is deleted:
        # it inlines 553 KB of axe-core, which the repo-wide identifier audit would (correctly) flag
        # if it were committed as evidence.
        $instrumented = $html.Replace("</body>", $runner)
        $temporary = Join-Path ([System.IO.Path]::GetTempPath()) `
            ("idea-axe-" + [guid]::NewGuid().ToString("N") + ".html")
        Set-Content -LiteralPath $temporary -Value $instrumented -Encoding UTF8
        try {
            & $edge --headless --disable-gpu --hide-scrollbars --window-size=1280,2000 `
                --virtual-time-budget=15000 `
                "--screenshot=$(Join-Path $output "axe-$label.png")" `
                ([uri]$temporary).AbsoluteUri | Out-Null
        } finally {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    Write-Host ("wrote 4 screenshots (home/result at 1280 and 390) and an axe violation " +
        "report screenshot for each page, in $output")
} finally {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
