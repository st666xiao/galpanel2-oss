# Offline release-asset fixtures; run on Windows after building Setup and zip.
param([Parameter(Mandatory)][string]$Version)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$fixture = Join-Path $env:RUNNER_TEMP ('gp2-install-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $fixture | Out-Null
$assetFixtures = @{}
foreach ($name in @("GalPanel2-Setup-$Version.exe", "GalPanel2-$Version-win64.zip", 'SHA256SUMS.txt')) {
    $assetFixtures[$name] = Join-Path $fixture $name
    Copy-Item (Join-Path $root $name) $assetFixtures[$name]
}
$releaseFixture = @{ tag_name = "v$Version"; assets = @(
    foreach ($name in $assetFixtures.Keys) { @{ name = $name; browser_download_url = $name } }
) }
function Invoke-RestMethod {
    param($Uri, $Headers)
    return $releaseFixture
}
function Invoke-WebRequest {
    param($Uri, $Headers, [switch]$UseBasicParsing, $OutFile)
    Copy-Item -LiteralPath $assetFixtures[$Uri] -Destination $OutFile
}
$installer = Join-Path $root 'install.ps1'
$portable = Join-Path $fixture 'portable with spaces'
& $installer -Portable -Directory $portable
if (!(Test-Path "$portable\_internal")) { throw 'Portable runtime missing' }
# Existing files must survive a rejected portable overwrite.
$sentinel = Join-Path $portable 'user-library.galdb'
Set-Content $sentinel 'do not overwrite'
try {
    & $installer -Portable -Directory $portable
    throw 'Expected occupied-directory rejection'
} catch {
    if ($_.Exception.Message -notlike '*便携版目标*') { throw }
}
if ((Get-Content $sentinel) -ne 'do not overwrite') { throw 'Portable data changed' }
# A mismatched checksum must fail before Setup is launched / a destination exists.
$sums = Get-Content $assetFixtures['SHA256SUMS.txt'] -Raw
Set-Content $assetFixtures['SHA256SUMS.txt'] ($sums -replace '[a-fA-F0-9]{64}', ('0' * 64)) -Encoding ascii
try {
    & $installer
    throw 'Expected checksum rejection'
} catch {
    if ($_.Exception.Message -notlike '*SHA256 校验失败*') { throw }
}
Set-Content $assetFixtures['SHA256SUMS.txt'] $sums -Encoding ascii
# Place library / covers / backup in the fallback data directory, outside {app}.
$data = Join-Path $env:APPDATA ('GalPanel2\deploy-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory "$data\library.galdb.covers" -Force | Out-Null
Set-Content "$data\library.galdb" 'library sentinel'
Set-Content "$data\library.galdb.covers\cover.png" 'cover sentinel'
Set-Content "$data\library.agent-test.bak" 'backup sentinel'
Set-Content "$data\settings.json" 'settings sentinel'
$before = @(Get-ChildItem $data -Recurse -File | Get-FileHash | Select-Object Path, Hash)
& $installer
Invoke-Expression (Get-Content -LiteralPath $installer -Raw) # same execution route as irm | iex
$installed = Join-Path $env:LOCALAPPDATA 'Programs\GalPanel2'
foreach ($entry in @('galpanel2-agent', 'galpanel2-mcp')) {
    & "$installed\$entry.exe" --help
    if ($LASTEXITCODE -ne 0) { throw "Installed $entry failed" }
}
$p = Start-Process "$installed\GalPanel2.exe" -ArgumentList '--screen','title','--screenshot',('"' + "$fixture\installed-title.png" + '"') -Wait -PassThru
if ($p.ExitCode -ne 0 -or !(Test-Path "$fixture\installed-title.png")) { throw 'Installed GUI smoke failed' }
if (!(Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\GalPanel2.lnk")) { throw 'Start menu shortcut missing' }
$p = Start-Process "$installed\unins000.exe" -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru
if ($p.ExitCode -ne 0) { throw 'Uninstall failed' }
if (Test-Path "$installed\GalPanel2.exe") { throw 'Program survived uninstall' }
$after = @(Get-ChildItem $data -Recurse -File | Get-FileHash | Select-Object Path, Hash)
if (Compare-Object $before $after -Property Path, Hash) { throw 'User data changed during install/upgrade/uninstall' }
Remove-Item $data, $fixture -Recurse -Force
Write-Host 'PASS: portable, overwrite rejection, checksum rejection, install, upgrade, GUI/CLI, shortcut, uninstall and data preservation'
