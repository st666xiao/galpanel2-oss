# Windows PowerShell 5.1+; also supports: .\install.ps1 -Portable -Directory D:\Apps\GalPanel2
[CmdletBinding()]
param(
    [switch]$Portable,
    [string]$Directory
)

$oldErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Stop'
$work = $null
$oldProgress = $ProgressPreference
$oldTls = [Net.ServicePointManager]::SecurityProtocol
try {
    if ($env:OS -ne 'Windows_NT' -or ![Environment]::Is64BitOperatingSystem -or
        [Environment]::OSVersion.Version.Major -lt 10) {
        throw '需要 Windows 10/11 64 位系统。'
    }
    if ($Directory -and !$Portable) { throw '-Directory 仅用于 -Portable。' }
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = $oldTls -bor [Net.SecurityProtocolType]::Tls12
    $headers = @{ 'User-Agent' = 'GalPanel2-Installer'; 'Accept' = 'application/vnd.github+json' }
    Write-Host '正在查询 GalPanel2 最新正式发行版…'
    $release = Invoke-RestMethod 'https://api.github.com/repos/st666xiao/galpanel2-oss/releases/latest' -Headers $headers
    if ($release.tag_name -notmatch '^v([0-9][0-9A-Za-z.+-]*)$') { throw '发行版本号格式不正确。' }
    $version = $Matches[1]
    $name = if ($Portable) { "GalPanel2-$version-win64.zip" } else { "GalPanel2-Setup-$version.exe" }
    $asset = @($release.assets | Where-Object name -eq $name)
    $sums = @($release.assets | Where-Object name -eq 'SHA256SUMS.txt')
    if ($asset.Count -ne 1 -or $sums.Count -ne 1) { throw '该发行版缺少安装文件或 SHA256SUMS.txt，请到 Releases 检查。' }
    $work = Join-Path ([IO.Path]::GetTempPath()) ('GalPanel2-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $work | Out-Null
    $download = Join-Path $work $name
    Invoke-WebRequest $asset[0].browser_download_url -Headers $headers -UseBasicParsing -OutFile $download
    $sumFile = Join-Path $work 'SHA256SUMS.txt'
    Invoke-WebRequest $sums[0].browser_download_url -Headers $headers -UseBasicParsing -OutFile $sumFile
    $pattern = '^([a-fA-F0-9]{64})\s+\*?' + [regex]::Escape($name) + '$'
    $hashes = @(Get-Content -LiteralPath $sumFile | ForEach-Object {
        if ($_ -match $pattern) { $Matches[1] }
    })
    if ($hashes.Count -ne 1) { throw '校验文件中没有唯一匹配的 SHA256，已停止。' }
    if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $hashes[0]) {
        throw 'SHA256 校验失败，已停止，未运行安装程序或解压文件。'
    }
    Write-Host "SHA256 校验通过：$name"
    if ($Portable) {
        if (!$Directory) { $Directory = Join-Path $env:LOCALAPPDATA 'Programs\GalPanel2-Portable' }
        $target = [IO.Path]::GetFullPath($Directory)
        if (Test-Path -LiteralPath $target) {
            if (!(Test-Path -LiteralPath $target -PathType Container) -or
                @(Get-ChildItem -LiteralPath $target -Force).Count -gt 0) {
                throw '便携版目标必须是不存在或为空的目录，避免覆盖已有数据。'
            }
        }
        $expanded = Join-Path $work 'expanded'
        Expand-Archive -LiteralPath $download -DestinationPath $expanded
        $source = Join-Path $expanded 'GalPanel2'
        foreach ($exe in @('GalPanel2.exe', 'galpanel2-agent.exe', 'galpanel2-mcp.exe')) {
            if (!(Test-Path -LiteralPath (Join-Path $source $exe) -PathType Leaf)) { throw "压缩包缺少 $exe。" }
        }
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $target -Recurse
    } else {
        $target = Join-Path $env:LOCALAPPDATA 'Programs\GalPanel2'
        $process = Start-Process -FilePath $download -ArgumentList @(
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/CURRENTUSER', '/NORESTART', '/SP-',
            ('/DIR="' + $target + '"')
        ) -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) { throw "安装程序退出码：$($process.ExitCode)。" }
        if ($process.ExitCode -eq 3010) { Write-Warning '安装完成，需要稍后手动重启 Windows。' }
    }
    foreach ($exe in @('GalPanel2.exe', 'galpanel2-agent.exe', 'galpanel2-mcp.exe')) {
        if (!(Test-Path -LiteralPath (Join-Path $target $exe) -PathType Leaf)) { throw "安装结果缺少 $exe。" }
    }
    Write-Host "安装完成：$target"
    Write-Host "启动程序：$(Join-Path $target 'GalPanel2.exe')"
    Write-Host "Agent：$(Join-Path $target 'galpanel2-agent.exe')"
    Write-Host "MCP：$(Join-Path $target 'galpanel2-mcp.exe')"
} catch {
    throw "GalPanel2 安装失败：$($_.Exception.Message)"
} finally {
    if ($work -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force }
    $ErrorActionPreference = $oldErrorAction
    $ProgressPreference = $oldProgress
    [Net.ServicePointManager]::SecurityProtocol = $oldTls
}
