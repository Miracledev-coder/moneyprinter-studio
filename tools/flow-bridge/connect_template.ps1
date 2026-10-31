# MoneyPrinter Studio: Connect Google Flow (Windows). Personalised: the pairing link inside works once, 30 min.
$STUDIO = '__STUDIO__'; $TOKEN = '__TOKEN__'
$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$B = Join-Path $HOME '.local\bin'; $D = Join-Path $HOME '.flow-bridge'; $env:Path = "$B;$env:Path"
Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
function Msg($t, $buttons = 'OK') { try { [System.Windows.MessageBox]::Show($t, 'Flow Helper', $buttons) } catch { Write-Host $t; 'OK' } }
function Stage($s) { try { Invoke-RestMethod -Method Post -Uri "$STUDIO/api/flow/progress" -ContentType 'application/json' -Body (@{token = $TOKEN; stage = $s } | ConvertTo-Json) -TimeoutSec 10 | Out-Null } catch {} }
function Oops($t) { Msg $t | Out-Null; Write-Host "X  $t" -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }
Write-Host 'MoneyPrinter Studio - connecting your Google Flow'; Write-Host ''
Stage 'started'
if (-not (Get-Command flow-bridge -ErrorAction SilentlyContinue) -or -not (Get-Command gflow -ErrorAction SilentlyContinue)) {
  Write-Host 'Installing the helper (a few minutes, only the first time)...'; Stage 'installing'
  try { Invoke-RestMethod 'https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.ps1' | Invoke-Expression }
  catch { Oops 'The install did not finish. Check your internet and run this again. If it fails twice, send a screenshot of this window to your Studio admin.' }
  $env:Path = "$B;$env:Path"; $ErrorActionPreference = 'Continue'
}
& gflow auth status *> $null
if ($LASTEXITCODE -ne 0) {
  Stage 'signin'
  Msg 'A Chrome window will open. Sign in to the Google account you use for Flow, then close the window when Flow shows your projects.' | Out-Null
  & flow-bridge --signin
  if ($LASTEXITCODE -ne 0) { Oops 'Google Flow sign-in did not finish. Run this again to retry.' }
}
$ACC = ''
try { $j = (& gflow auth list --json 2>$null) -join ' '; if ($j -match '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}') { $ACC = $Matches[0] } } catch {}
$F = Join-Path $HOME 'Videos\FlowBridge'; New-Item -ItemType Directory -Force -Path $F, $D | Out-Null
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'Flow Helper.lnk'
$auto = $false
if (-not (Test-Path $startup) -and -not (Test-Path "$D\no-autostart")) {
  if ((Msg 'Start the Flow helper automatically when you sign in to Windows?' 'YesNo') -eq 'Yes') { $auto = $true } else { New-Item -ItemType File -Force "$D\no-autostart" | Out-Null }
}
$pair = @(); $used = Join-Path $D "used-$TOKEN"
if ($TOKEN -and -not (Test-Path $used)) { New-Item -ItemType File -Force $used | Out-Null; $pair = @('--pair', $TOKEN) }
if ($pair.Count -gt 0) {
  Stage 'pairing'
  $extra = @(); if ($ACC) { $extra = @('--account', $ACC) }
  & flow-bridge --connect $STUDIO @pair @extra --pair-only
  if ($LASTEXITCODE -ne 0) { Oops 'Pairing did not work (this file may be older than 30 minutes or already used). Get a fresh one from Studio > Account.' }
}
if ($auto) {
  try {
    $ws = New-Object -ComObject WScript.Shell; $l = $ws.CreateShortcut($startup)
    $l.TargetPath = "$env:WINDIR\System32\cmd.exe"; $l.Arguments = "/c `"$B\flow-bridge.cmd`" --connect $STUDIO --folder `"$F`""
    $l.WindowStyle = 7; $l.WorkingDirectory = $HOME; $l.Save()
  } catch { Write-Host '!  Could not add the startup item.' }
}
Write-Host ''; Write-Host 'Connected. Keep this window open (you can minimise it) while Studio makes videos.' -ForegroundColor Green
& flow-bridge --connect $STUDIO --folder $F
Read-Host 'Helper stopped. Press Enter to close'
