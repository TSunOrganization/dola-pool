# Keep running even if an individual command fails.
$ErrorActionPreference = "Continue"
# Real filesystem path of the project (kept exactly as-is; it contains non-ASCII directory names).
Set-Location 'F:\ai\视频逆向\dola-pool'
# Log file paths (stdout / stderr), placed next to the project.
$out = Join-Path (Get-Location) 'server.log'
$err = Join-Path (Get-Location) 'server.err.log'
# Stop any previously running Python 3.12 server process so the port is freed up.
Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*Python312*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
# Remove stale log files.
if (Test-Path $out) { Remove-Item $out -Force -ErrorAction SilentlyContinue }
if (Test-Path $err) { Remove-Item $err -Force -ErrorAction SilentlyContinue }
# Launch the uvicorn server in the background (hidden window), redirecting stdout/stderr to the log files.
Start-Process -FilePath 'python.exe' -ArgumentList '-m','uvicorn','server:app','--host','127.0.0.1','--port','8000' -WorkingDirectory 'F:\ai\视频逆向\dola-pool' -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
Start-Sleep -Seconds 10
# Print the tail of both logs for a quick health check.
Write-Output "=== OUT ==="
Get-Content $out -Tail 25 -ErrorAction SilentlyContinue
Write-Output "=== ERR ==="
Get-Content $err -Tail 25 -ErrorAction SilentlyContinue
