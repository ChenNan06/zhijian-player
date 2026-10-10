# 一键准备运行环境：下载 libmpv 解码内核 + 校验 Python 依赖 + 生成图标
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\01-setup.ps1

param(
    [string]$MpvUrl = 'https://github.com/zhongfly/mpv-winbuild/releases/download/2026-10-05-c152964208/mpv-dev-x86_64-20261005-git-c152964208.7z',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT
$PY = 'C:\DevTools\Python312\python.exe'
if (-not (Test-Path $PY)) { $PY = 'python' }

Write-Host "=== 至简Player 环境准备 ===" -ForegroundColor Cyan

# ---- 1) Python 依赖 --------------------------------------------------------
Write-Host "`n[1/4] 检查 Python 依赖..." -ForegroundColor Cyan
& $PY -c @"
import importlib, sys
need = {'PySide6': '6.9', 'PIL': None, 'pillow_avif': None, 'pillow_heif': None}
miss = []
for m in need:
    try:
        importlib.import_module(m)
    except Exception:
        miss.append(m)
print('MISSING=' + ','.join(miss))
sys.exit(1 if miss else 0)
"@
if ($LASTEXITCODE -ne 0) {
    Write-Host "  有缺包, 正在安装（走代理 127.0.0.1:7897, 不可用则直连）..." -ForegroundColor Yellow
    $proxy = ''
    if (Test-NetConnection -ComputerName 127.0.0.1 -Port 7897 -InformationLevel Quiet -WarningAction SilentlyContinue) {
        $proxy = '--proxy http://127.0.0.1:7897'
        Write-Host "  代理可用, 走代理"
    } else {
        Write-Host "  代理不可用, 直连"
    }
    $parts = @()
    if ($proxy) { $parts += $proxy.Split(' ') }
    $parts += @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', "$ROOT\requirements.txt")
    & $PY @parts
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
} else {
    Write-Host "  依赖齐全" -ForegroundColor Green
}

# ---- 2) libmpv -------------------------------------------------------------
Write-Host "`n[2/4] 准备 libmpv 解码内核..." -ForegroundColor Cyan
$dll = "$ROOT\runtime\libmpv-2.dll"
if ((Test-Path $dll) -and (-not $Force)) {
    $mb = [math]::Round((Get-Item $dll).Length / 1MB, 1)
    Write-Host "  已存在（$mb MB），跳过。要重下加 -Force" -ForegroundColor Green
} else {
    New-Item -ItemType Directory -Force -Path "$ROOT\runtime", "$ROOT\downloads\mpv" | Out-Null
    $arch = "$ROOT\downloads\mpv\mpv-dev-x86_64.7z"
    if (-not (Test-Path $arch) -or $Force) {
        Write-Host "  下载中（约 31 MB）..."
        $pxy = @()
        if (Test-NetConnection -ComputerName 127.0.0.1 -Port 7897 -InformationLevel Quiet -WarningAction SilentlyContinue) {
            $pxy = @('-x', 'http://127.0.0.1:7897')
        }
        & curl.exe -L --fail --retry 3 @pxy -o $arch $MpvUrl
        if ($LASTEXITCODE -ne 0) { throw "下载失败。可手工下载后放到 $arch" }
    }
    Write-Host "  解压中..."
    $bz = 'E:\Bandizip\bz.exe'
    if (Test-Path $bz) {
        & $bz x -y "-o:$ROOT\downloads\mpv\extract" $arch | Out-Null
    } else {
        # 退回到 .NET 解压（只支持 zip；7z 必须有 Bandizip 或 7-Zip）
        $sz = 'C:\Program Files\7-Zip\7z.exe'
        if (Test-Path $sz) {
            & $sz x -y "-o$ROOT\downloads\mpv\extract" $arch | Out-Null
        } else {
            throw "找不到解压工具（E:\Bandizip\bz.exe 或 7-Zip）。请手工解压 $arch 到 downloads\mpv\extract"
        }
    }
    $src = "$ROOT\downloads\mpv\extract\libmpv-2.dll"
    if (-not (Test-Path $src)) { throw "解压后没有 libmpv-2.dll, 请检查压缩包结构" }
    Copy-Item $src $dll -Force
    Write-Host "  已安装到 runtime\libmpv-2.dll（$([math]::Round((Get-Item $dll).Length/1MB,1)) MB）" -ForegroundColor Green
}

# ---- 3) 图标 ---------------------------------------------------------------
Write-Host "`n[3/4] 生成应用图标..." -ForegroundColor Cyan
& $PY "$ROOT\scripts\05-make-icon.py" | Out-Null
Write-Host "  assets\icon.ico 就绪" -ForegroundColor Green

# ---- 4) 自检 ---------------------------------------------------------------
Write-Host "`n[4/4] 引擎自检..." -ForegroundColor Cyan
$env:PYTHONPATH = "$ROOT\src"
& $PY -m zhijian --probe
if ($LASTEXITCODE -ne 0) { throw "自检失败" }

Write-Host "`n环境就绪。运行 run.bat 即可启动。" -ForegroundColor Cyan
