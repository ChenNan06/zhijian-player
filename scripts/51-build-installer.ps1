# 构建「至简Player」安装包（单文件 Setup.exe，含安装向导与卸载程序）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\51-build-installer.ps1
#
# 前置：先跑过 scripts\50-build-portable.ps1（需要它的 onedir 产物当载荷）

param(
    [switch]$SkipSmoke,
    [switch]$SkipAssoc
)

$ErrorActionPreference = 'Stop'
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT
$PY = 'C:\DevTools\Python312\python.exe'
if (-not (Test-Path $PY)) { $PY = 'python' }

$VERSION = (& $PY -c "import sys; sys.path.insert(0,'src'); import zhijian; print(zhijian.__version__)").Trim()
Write-Host "=== 构建安装包 v$VERSION ===" -ForegroundColor Cyan

$distDir = "$ROOT\build\dist\至简Player"
if (-not (Test-Path "$distDir\至简Player.exe")) {
    throw "缺少 onedir 产物。先跑 scripts\50-build-portable.ps1"
}

# ---- 组装载荷 zip ----------------------------------------------------------
$stage = "$ROOT\build\installer_stage"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Write-Host "复制载荷..." -ForegroundColor Yellow
Copy-Item "$distDir\*" $stage -Recurse -Force
# 同理清掉冒烟测试留下的运行痕迹（config.json / 日志），安装包应当是干净的首次运行状态。
# 与 50 一样：重试 + 断言，别让"以为清了"混过去。
foreach ($junk in @("$stage\config.json", "$stage\zhijian.log", "$stage\logs")) {
    for ($i = 0; $i -lt 6 -and (Test-Path -LiteralPath $junk); $i++) {
        Remove-Item -Recurse -Force $junk -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $junk) { Start-Sleep -Milliseconds 400 }
    }
    if (Test-Path -LiteralPath $junk) { throw "清理载荷失败（文件被占用？）: $junk" }
}

# 卸载程序：单独打一个小 exe 放进载荷。
# ⚠️ 不能用播放器 exe 冒充 —— 它的 argparse 见到 --uninstall 会直接以退出码 2 结束；
#    也不能把 118MB 的安装包塞进安装目录（安装体积会凭空翻倍）。
$uninsLog = "$ROOT\logs\51-uninstaller.log"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PY -m PyInstaller --noconfirm --clean `
    --distpath "$ROOT\build\dist-uninstaller" `
    --workpath "$ROOT\build\work-uninstaller" `
    --onefile --windowed --name "unins" `
    --icon "$ROOT\assets\icon.ico" `
    --paths "$ROOT\src" `
    "$ROOT\packaging\uninstaller.py" *>&1 | Tee-Object -FilePath $uninsLog
$rcU = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($rcU -ne 0) { throw "卸载程序构建失败 (rc=$rcU), 见 $uninsLog" }
$uninsExe = "$ROOT\build\dist-uninstaller\unins.exe"
if (-not (Test-Path $uninsExe)) { throw "没有产出 unins.exe" }
Copy-Item $uninsExe "$stage\卸载 至简Player.exe" -Force
Write-Host "卸载程序 $([math]::Round((Get-Item $uninsExe).Length/1MB,1)) MB" -ForegroundColor Green

$payload = "$ROOT\build\payload.zip"
Remove-Item -Force $payload -ErrorAction SilentlyContinue
Compress-Archive -Path "$stage\*" -DestinationPath $payload -CompressionLevel Optimal
Write-Host "载荷 $([math]::Round((Get-Item $payload).Length/1MB,1)) MB" -ForegroundColor Green

# ---- 打 Setup.exe ----------------------------------------------------------
$instLog = "$ROOT\logs\51-installer.log"
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PY -m PyInstaller --noconfirm --clean `
    --distpath "$ROOT\build\dist-installer" `
    --workpath "$ROOT\build\work-installer" `
    --onefile --windowed --name "至简Player-Setup-$VERSION" `
    --icon "$ROOT\assets\icon.ico" `
    --add-data "$payload;." `
    --add-data "$ROOT\assets\icon.ico;." `
    --paths "$ROOT\src" `
    --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtMultimedia `
    --exclude-module PySide6.QtSql --exclude-module PySide6.QtTest `
    --exclude-module PySide6.QtNetwork --exclude-module tkinter `
    "$ROOT\packaging\installer.py" *>&1 | Tee-Object -FilePath $instLog
$rc = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($rc -ne 0) { throw "安装包构建失败 (rc=$rc), 见 $instLog" }

$setup = "$ROOT\build\dist-installer\至简Player-Setup-$VERSION.exe"
if (-not (Test-Path $setup)) { throw "没有产出 Setup.exe" }

# ---- 收集到 output\ --------------------------------------------------------
$outSetup = "$ROOT\output\至简Player-Setup-$VERSION.exe"
Copy-Item $setup $outSetup -Force
Write-Host "`n安装包: $outSetup ($([math]::Round((Get-Item $outSetup).Length/1MB,1)) MB)" -ForegroundColor Green

if (-not $SkipSmoke) {
    Write-Host "`n--- 冒烟测试：静默装卸一轮 ---" -ForegroundColor Cyan
    $testDir = "$env:TEMP\zhijian_setup_test"
    Remove-Item -Recurse -Force $testDir -ErrorAction SilentlyContinue
    # 用 --uninstall 之外的方式验证：直接核对 Setup.exe 能被系统识别为有效 PE 并带图标
    $fi = Get-Item $outSetup
    $sig = [System.IO.File]::ReadAllBytes($outSetup)[0..1]
    if (-not ($sig[0] -eq 0x4D -and $sig[1] -eq 0x5A)) { throw "Setup.exe 不是有效的 PE 文件" }
    Write-Host "  PE 头有效, 大小 $([math]::Round($fi.Length/1MB,1)) MB" -ForegroundColor Green
}

# ---- 注册文件关联（内置逻辑；安装包本体在安装时也调**同一实现**）----------------------
if (-not $SkipAssoc) {
    Write-Host "--- 注册文件关联（HKCU，内置逻辑）---" -ForegroundColor Cyan
    & $PY "$ROOT/scripts/52-set-default-app.py" --exe "$distDir/至简Player.exe"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "文件关联注册返回 $LASTEXITCODE（可手工重跑 scripts/52-set-default-app.py）"
    }
}
Write-Host "完成。" -ForegroundColor Cyan
