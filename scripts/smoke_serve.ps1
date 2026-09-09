$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$workRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("idea-smoke-" + [guid]::NewGuid().ToString("N"))
$stdoutPath = Join-Path $workRoot "serve.stdout.log"
$stderrPath = Join-Path $workRoot "serve.stderr.log"
$process = $null
New-Item -ItemType Directory -Path $workRoot | Out-Null

try {
    $process = Start-Process -FilePath "uv" `
        -ArgumentList @("run", "idea", "serve", "--no-open", "--port", "8791", "--work-root", $workRoot) `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru

    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $health = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            throw "idea serve exited early with code $($process.ExitCode)"
        }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8791/healthz" -TimeoutSec 1
            if ($health.ok -eq $true) { break }
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if ($null -eq $health -or $health.ok -ne $true) {
        throw "GET /healthz did not become ready within 20 seconds"
    }
    $homeResponse = Invoke-WebRequest -Uri "http://127.0.0.1:8791/" -TimeoutSec 5 -UseBasicParsing
    if ($homeResponse.StatusCode -ne 200 -or $homeResponse.Content -notmatch "Drop a mix") {
        throw "GET / did not return the IDea home page"
    }
    Write-Output "smoke serve passed: /healthz=200 ok=true; /=200 contains 'Drop a mix'"
}
finally {
    if ($null -ne $process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
    }
}
