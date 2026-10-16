# 构建「至简Player」绿色便携版（PyInstaller onedir）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\50-build-portable.ps1
#
# 产物：
#   build\dist\至简Player\             <- onedir 目录
#   output\至简Player-Portable-<版本>\ <- 交付目录（含 run.bat / README）
#   output\至简Player-Portable-<版本>.zip

param(
    [switch]$SkipSmoke,
    [switch]$Clean,
    [switch]$SkipAssoc
)

$ErrorActionPreference = 'Stop'
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT

$VERSION = (& 'C:\DevTools\Python312\python.exe' -c "import sys; sys.path.insert(0,'src'); import zhijian; print(zhijian.__version__)").Trim()
Write-Host "=== 至简Player 打包 v$VERSION ===" -ForegroundColor Cyan

$PY = 'C:\DevTools\Python312\python.exe'
if (-not (Test-Path $PY)) { $PY = 'python' }

# ---- 前置检查 -------------------------------------------------------------
if (-not (Test-Path "$ROOT\runtime\libmpv-2.dll")) {
    throw "缺少 runtime\libmpv-2.dll。先跑 scripts\01-setup.ps1 或手工放入。"
}
if (-not (Test-Path "$ROOT\assets\icon.ico")) {
    Write-Host "生成图标..." -ForegroundColor Yellow
    & $PY "$ROOT\scripts\05-make-icon.py" | Out-Null
}

if ($Clean) {
    Remove-Item -Recurse -Force "$ROOT\build\dist", "$ROOT\build\work" -ErrorAction SilentlyContinue
}

# ---- PyInstaller ----------------------------------------------------------
Write-Host "`n--- PyInstaller ---" -ForegroundColor Cyan
$log = "$ROOT\logs\50-pyinstaller.log"
New-Item -ItemType Directory -Force -Path "$ROOT\logs" | Out-Null
# ⚠️ $ErrorActionPreference='Stop' 下, 原生命令往 stderr 写一行（PyInstaller 的
#    "running as admin" 警告就是）会被 PowerShell 5.1 包成 ErrorRecord 并中断脚本。
#    这里临时放宽, 只看退出码。
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PY -m PyInstaller --noconfirm --clean `
    --distpath "$ROOT\build\dist" `
    --workpath "$ROOT\build\work" `
    "$ROOT\packaging\zhijian.spec" *>&1 | Tee-Object -FilePath $log
$pyiRc = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

if ($pyiRc -ne 0) { throw "PyInstaller 失败 (rc=$pyiRc), 见 $log" }

# ⚠️ 判据：日志里必须真的出现 PySide6 的 hook（否则是"打成功了但什么都没打进"）
$hookLines = Select-String -Path $log -Pattern 'hook-PySide6' -SimpleMatch
if (-not $hookLines) {
    throw "PyInstaller 日志里没有 hook-PySide6 —— PySide6 根本没被打进去！检查 pathex / --paths。"
}
Write-Host "PySide6 hook 命中 $($hookLines.Count) 条" -ForegroundColor Green

$distDir = "$ROOT\build\dist\至简Player"
if (-not (Test-Path "$distDir\至简Player.exe")) { throw "没有产出 至简Player.exe" }
$exeSize = (Get-Item "$distDir\至简Player.exe").Length
$totalMB = [math]::Round(((Get-ChildItem $distDir -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "onedir 体积 $totalMB MB（exe 本身 $([math]::Round($exeSize/1MB,2)) MB）" -ForegroundColor Green

# ---- 补齐便携版必需文件 ----------------------------------------------------
Copy-Item "$ROOT\runtime\libmpv-2.dll" "$distDir\_internal\" -Force -ErrorAction SilentlyContinue
New-Item -ItemType File -Path "$distDir\portable.txt" -Force | Out-Null

# ---- 冒烟测试：三条判据缺一不可 --------------------------------------------
if (-not $SkipSmoke) {
    Write-Host "`n--- 冒烟测试（进程存活 + 启动横幅 + 主窗口标题）---" -ForegroundColor Cyan
    $exe = "$distDir\至简Player.exe"
    Remove-Item "$env:APPDATA\ZhiJian\ZhiJianPlayer\zhijian.log" -ErrorAction SilentlyContinue
    $p = Start-Process -FilePath $exe -ArgumentList '--control','zhjsmoke','--no-single-instance' -PassThru
    Start-Sleep -Seconds 12
    $alive = -not $p.HasExited
    # 日志位置：便携模式（有 portable.txt）写在程序目录, 否则写到 %APPDATA%
    $cand = @("$distDir\zhijian.log", "$env:APPDATA\ZhiJian\ZhiJianPlayer\zhijian.log")
    $banner = $false
    $usedLog = ''
    foreach ($lp in $cand) {
        if ((Test-Path $lp) -and (Select-String -Path $lp -Pattern '启动' -SimpleMatch -Quiet)) {
            $banner = $true; $usedLog = $lp; break
        }
    }
    Write-Host "  日志: $usedLog"
    $win = $false
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        $wins = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
        $win = ($wins -and $wins.MainWindowTitle -like '*至简Player*')
    } catch {}
    Write-Host "  进程存活=$alive  启动横幅=$banner  主窗口=$win"
    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
    if (-not ($alive -and $banner -and $win)) {
        throw "冒烟测试不通过：存活=$alive 横幅=$banner 窗口=$win"
    }
    Write-Host "  冒烟测试通过" -ForegroundColor Green
}

# ---- 收集到 output\ --------------------------------------------------------
$outName = "至简Player-Portable-$VERSION"
$outDir = "$ROOT\output\$outName"
# ⚠️ 冒烟测试是**在产物目录里**跑起来的，会留下 config.json 与 zhijian.log。
#    不清理的话它们会被打进 zip / 安装包 —— 用户拿到手就带着一份"别人跑过的痕迹"
#    （日志里还有测试机的路径）。
#    ⚠️ 必须**重试 + 断言**：刚被 Stop-Process 杀掉的进程可能还占着日志句柄，
#       单次 Remove-Item 会静默失败（-ErrorAction SilentlyContinue）⇒
#       "以为清了、其实还在"。本项目真踩过：zip 里躺着一个 zhijian.log。
foreach ($junk in @("$distDir\config.json", "$distDir\zhijian.log",
                    "$distDir\logs", "$distDir\__pycache__")) {
    for ($i = 0; $i -lt 6 -and (Test-Path -LiteralPath $junk); $i++) {
        Remove-Item -Recurse -Force $junk -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $junk) { Start-Sleep -Milliseconds 400 }
    }
    if (Test-Path -LiteralPath $junk) { throw "清理产物失败（文件被占用？）: $junk" }
}
Remove-Item -Recurse -Force $outDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$ROOT\output" | Out-Null
Copy-Item $distDir $outDir -Recurse -Force

# 便携版专用启动器（ASCII 正文, 中文只在文件名里）
$bat = @"
@echo off
rem ==========================================================================
rem  ZhiJian Player - portable launcher
rem  Just double-click. No install, no admin, no registry writes.
rem ==========================================================================
setlocal
set "HERE=%~dp0"
rem Prefer the dedicated GPU on hybrid-graphics laptops (same as Windows'
rem Settings > Display > Graphics > High performance). Needs a restart.
reg add "HKCU\Software\Microsoft\DirectX\UserGpuPreferences" /v "%HERE%ZhiJianPlayer.exe" /t REG_SZ /d "GpuPreference=2;" /f >nul 2>&1
start "" "%HERE%ZhiJianPlayer.exe" %*
exit /b 0
"@
# 用中文 exe 名（PowerShell 写文件不受 cmd 代码页影响）
$exePath = "$outDir\至简Player.exe"
$bat = $bat -replace 'ZhiJianPlayer\.exe', '至简Player.exe'
# .bat 正文必须纯 ASCII；中文 exe 名改用 %~dp0 + 通配找 exe 的方式规避
$batLines = @(
    '@echo off',
    'rem ==========================================================================',
    'rem  ZhiJian Player - portable launcher (green / no-install)',
    'rem  Just double-click. No admin, no registry writes except GPU preference.',
    'rem ==========================================================================',
    'setlocal',
    'set "HERE=%~dp0"',
    'for %%F in ("%HERE%*.exe") do if /I not "%%~nxF"=="unins.exe" set "APP=%%~fF"',
    'if not defined APP (echo [ERROR] player exe not found & pause & exit /b 2)',
    'rem Prefer the dedicated GPU on hybrid-graphics laptops (equal to Settings >',
    'rem System > Display > Graphics > High performance). Takes effect next start.',
    'reg add "HKCU\Software\Microsoft\DirectX\UserGpuPreferences" /v "%APP%" /t REG_SZ /d "GpuPreference=2;" /f >nul 2>&1',
    'start "" "%APP%" %*',
    'exit /b 0'
)
[System.IO.File]::WriteAllText("$outDir\启动 至简Player.bat", ($batLines -join "`r`n") + "`r`n",
    (New-Object System.Text.UTF8Encoding($false)))

# 便携版说明
$readme = @"
至简Player —— 三合一全能媒体查看器（绿色便携版 v$VERSION）
=========================================================

怎么启动
--------
双击「启动 至简Player.bat」, 或直接双击「至简Player.exe」。
不需要安装、不需要管理员权限、不写注册表（只写一条"用独显跑"的偏好, 见下）。

支持格式
--------
视频：MP4 / MKV / MOV / AVI / WMV / FLV / WebM / TS / M2TS / RMVB / 3GP / VOB …
图片：JPG / PNG / GIF / WebP / AVIF / BMP / TIFF / TGA / QOI …
音频：MP3 / FLAC / WAV / AAC / M4A / OGG / Opus / WMA / APE / AC3 / DTS …
字幕：SRT / ASS / SSA / VTT / SUB / IDX / SUP（自动挂载同目录同名文件）

必看的三件事
------------
1. 独显加速：本目录下的 .bat 会往
   HKCU\Software\Microsoft\DirectX\UserGpuPreferences 写一条 GpuPreference=2,
   等于在「设置 → 系统 → 显示 → 图形」里手工选「高性能」。
   混合显卡笔记本上这一步能让硬解走独显（零拷贝）, 否则可能落到核显。
   它**下次启动**才生效；不想要就删掉那条注册表项。
2. 配置存放：本目录下有 portable.txt → 配置写在程序目录里（真正的绿色版）。
   删掉 portable.txt 则改为写入 %APPDATA%\ZhiJian\ZhiJianPlayer\。
3. 退出：关闭窗口即完全退出, 不留后台进程、不留托盘图标。

快捷键
------
空格 播放/暂停    ←/→ 快退/快进 5 秒（**长按 → 平滑升到 2 倍速, 松开立即恢复**）
↑/↓ 音量          F 全屏        L 播放列表     V 字幕设置
,/. 逐帧          PageUp/PageDown 上/下一个   S 截图      Ctrl+, 设置
图片模式下：←/→ 切换图片, 滚轮无级缩放, R/E 旋转, H 水平镜像, 1 适应窗口, 2 原始大小

更多
----
完整文档、逆向对照表与验收报告见项目源码目录的 docs\。
"@
[System.IO.File]::WriteAllText("$outDir\使用说明.txt", $readme, (New-Object System.Text.UTF8Encoding($false)))
# UTF-8 with BOM 更利于记事本识别
$readmeBom = [System.IO.File]::ReadAllText("$outDir\使用说明.txt", [System.Text.Encoding]::UTF8)
[System.IO.File]::WriteAllText("$outDir\使用说明.txt", $readmeBom, (New-Object System.Text.UTF8Encoding($true)))

# ---- 打包 zip --------------------------------------------------------------
$zip = "$ROOT\output\$outName.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path $outDir -DestinationPath $zip -CompressionLevel Optimal
Write-Host "`n便携版 ZIP: $zip ($([math]::Round((Get-Item $zip).Length/1MB,1)) MB)" -ForegroundColor Green
Write-Host "便携版目录: $outDir" -ForegroundColor Green

# ---- 注册文件关联（内置逻辑：打包完就把本程序设成默认打开方式）------------------------
# 用户要求："每次打包完以后把至简播放器设置为默认媒体打开方式"。
# 调的是 src/zhijian/platform/win.py 的**唯一实现**（安装包 / 卸载器 / 命令行脚本同一份）。
# 失败**不阻塞打包**（注册表状态不该让产物构建失败），只醒目告警。
if (-not $SkipAssoc) {
    Write-Host "--- 注册文件关联（HKCU，内置逻辑）---" -ForegroundColor Cyan
    & $PY "$ROOT/scripts/52-set-default-app.py" --exe "$outDir/至简Player.exe"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "文件关联注册返回 $LASTEXITCODE（可手工重跑 scripts/52-set-default-app.py）"
    }
}
Write-Host "完成。" -ForegroundColor Cyan
