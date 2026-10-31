# MoneyPrinter Studio - Flow helper installer for Windows (no admin rights, prebuilt packages only).
#   irm https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.ps1 | iex
$ErrorActionPreference = 'Stop'
$RAW = 'https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge'
$D = Join-Path $HOME '.flow-bridge'; $B = Join-Path $HOME '.local\bin'
New-Item -ItemType Directory -Force -Path $D, $B | Out-Null
function Fail($m) { Write-Host ''; Write-Host "X  $m" -ForegroundColor Red; Write-Host '   Send a screenshot of this window to your Studio admin.'; throw 'install failed' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$env:UV_NO_BUILD = '1'; $env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:Path = "$B;$env:USERPROFILE\.cargo\bin;$env:Path"

Write-Host '1/4  Installing uv (free Python manager, no admin needed)...'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  try { powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" } catch { Fail 'Could not install uv. Check your internet connection and try again.' }
  $env:Path = "$B;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv did not install. Check your internet connection and try again.' }
}

Write-Host '2/4  Installing gflow-cli (drives Google Flow in your own Chrome)...'
try { Invoke-WebRequest -UseBasicParsing "$RAW/constraints-win.txt" -OutFile "$D\constraints-win.txt" } catch { Fail 'Could not download the version list. Check your internet connection.' }
& uv tool install --python 3.12 --upgrade --no-build --constraints "$D\constraints-win.txt" gflow-cli==0.92.1
if ($LASTEXITCODE -ne 0) { Fail 'Could not install gflow-cli.' }

Write-Host '3/4  Installing the browser engine (one-time, about 150 MB)...'
$pw = Join-Path (& uv tool dir) 'gflow-cli\Scripts\playwright.exe'
$ok = $false
if (Test-Path $pw) { & $pw install chromium; $ok = ($LASTEXITCODE -eq 0) }
if (-not $ok) { & uv tool run --from gflow-cli playwright install chromium; $ok = ($LASTEXITCODE -eq 0) }
if (-not $ok) { Write-Host '!  Browser engine download failed; gflow will use your installed Google Chrome instead.' -ForegroundColor Yellow }

Write-Host '4/4  Installing flow-bridge...'
try { Invoke-WebRequest -UseBasicParsing "$RAW/flow_bridge.py" -OutFile "$D\flow_bridge.py" } catch { Fail 'Could not download flow-bridge.' }
Set-Content -Encoding ASCII -Path "$B\flow-bridge.cmd" -Value "@echo off`r`nuv run --no-project --python 3.12 `"%USERPROFILE%\.flow-bridge\flow_bridge.py`" %*"
# user PATH (no admin)
$up = [Environment]::GetEnvironmentVariable('Path', 'User'); if (-not $up) { $up = '' }
if ($up -notlike "*$B*") { [Environment]::SetEnvironmentVariable('Path', "$B;$up", 'User') }
# Desktop shortcut 'Start Flow Helper'
try {
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Start Flow Helper.lnk'))
  $lnk.TargetPath = "$env:WINDIR\System32\cmd.exe"
  $lnk.Arguments = "/k `"$B\flow-bridge.cmd`" --connect https://moneyprinter-studio.onrender.com --folder `"%USERPROFILE%\Videos\FlowBridge`""
  $lnk.WorkingDirectory = $HOME; $lnk.Save()
} catch { Write-Host '!  Could not create the Desktop shortcut (not needed).' }
Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
