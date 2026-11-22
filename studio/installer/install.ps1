param([string]$Bot, [string]$Hash, [string]$Installer, [switch]$DockerRequired, [switch]$Quiet)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function Docker {
    & docker.exe @args
    if ($LASTEXITCODE -ne 0) { throw "Docker 操作失败 ($($args[0]))。请检查 Docker Desktop 和当前应用的配置。" }
}
try {
    if (![Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { throw '需要 Windows x64。' }
    if ($DockerRequired) {
        if (!(Get-Command docker.exe -ErrorAction SilentlyContinue) -or
            (Docker info --format '{{.OSType}}/{{.Architecture}}') -notmatch '^linux/(x86_64|amd64)$') {
            throw '请先安装并启动 Docker Desktop，选择 Linux x64 容器，然后重跑本安装包。'
        }
        Docker compose version
    }
    $archive = Join-Path $PSScriptRoot 'payload.tgz'
    if ((Get-FileHash -LiteralPath $archive).Hash -ne $Hash) { throw '安装包完整性校验失败。' }
    $base = Join-Path $env:LOCALAPPDATA "AgentStrata\Apps\$Bot"
    [IO.Directory]::CreateDirectory($base) | Out-Null
    $lock = [IO.File]::Open((Join-Path $base 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $version = Join-Path $base $Hash
    $existing = Test-Path -LiteralPath $version
    $stage = Join-Path $base ([guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    $systemTar = Join-Path ([Environment]::SystemDirectory) 'tar.exe'
    if (![IO.File]::Exists($systemTar)) { throw '当前 Windows 缺少系统 tar.exe。' }
    & $systemTar -xzf $archive -C $stage
    if ($LASTEXITCODE -ne 0) { throw '无法解开应用资源。需要支持 tar.exe 的 Windows 10 或更新版本。' }
    if (!$existing) {
        [IO.Directory]::Move($stage, $version)
    } else {
        foreach ($file in (Get-ChildItem -LiteralPath $stage -File -Recurse -Force)) {
            $installed = Join-Path $version $file.FullName.Substring($stage.Length + 1)
            if (!(Test-Path -LiteralPath $installed -PathType Leaf) -or
                (Get-FileHash -LiteralPath $installed).Hash -ne (Get-FileHash -LiteralPath $file.FullName).Hash) {
                throw '已安装版本文件已改变。请先备份并移走该版本目录，再重跑安装包。'
            }
        }
    }
    Set-Location -LiteralPath $version
    $config = Get-Content -LiteralPath 'install.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.docker) {
        Docker image load -i images.tar
        foreach ($image in $config.images.PSObject.Properties) {
            if ((Docker image inspect --format '{{.Id}}' $image.Name) -ne $image.Value) { throw '镜像摘要不匹配。' }
        }
        if ($config.setup) { Docker compose -p "as-$Bot" -f compose.yaml run --rm --no-deps gateway configure }
        Docker compose -p "as-$Bot" -f compose.yaml up -d --pull never --no-build --wait
        if ($config.entry) {
            $address = Docker compose -p "as-$Bot" -f compose.yaml port $config.entry.service $config.entry.port
            $target = "http://$address/"
        } else {
            Write-Host '应用服务已就绪。请在自己的渠道中核对真实收发。'
        }
    } else {
        $target = Join-Path $version 'site\index.html'
    }
    if ($config.clients.'windows-x64') { $target = Join-Path $version $config.clients.'windows-x64' }
    if ($target) {
        $folder = Join-Path ([Environment]::GetFolderPath('Programs')) "AgentStrata\$Bot"
        [IO.Directory]::CreateDirectory($folder) | Out-Null
        $link = '打开应用.lnk'
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $folder $link))
        $shortcut.Description = $config.name
        if (Test-Path -LiteralPath (Join-Path $version 'open-client.ps1')) {
            $shortcut.TargetPath = Join-Path $PSHOME "powershell.exe"
            $shortcut.WorkingDirectory = $version
            $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $version 'open-client.ps1') + '"'
        } else {
            $shortcut.TargetPath = $target
            $shortcut.WorkingDirectory = Split-Path -LiteralPath $target
        }
        $shortcut.Save()
        if (!$Quiet) { Start-Process -FilePath (Join-Path $folder $link) }
    }
    & (Join-Path $PSScriptRoot "install-receipt.ps1") -Bot $Bot -Installer $Installer -ServicesReady:$DockerRequired
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (!$Quiet -and [Environment]::UserInteractive) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, '应用安装')
    }
    exit 1
} finally {
    if ($lock) { $lock.Dispose() }
    if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
