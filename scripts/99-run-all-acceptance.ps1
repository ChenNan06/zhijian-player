# 一键跑完全部验收（串行 —— 每个用例都要独占真实屏幕与输入队列）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\99-run-all-acceptance.ps1
#
# 结果汇总写到 logs\99-acceptance-summary.json

$ErrorActionPreference = 'Continue'
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ROOT = Split-Path -Parent $PSScriptRoot
Set-Location $ROOT
$PY = 'C:\DevTools\Python312\python.exe'
if (-not (Test-Path $PY)) { $PY = 'python' }

$CASES = @(
    @{ id = '41'; name = '界面巡检';           script = 'scripts\41-ui-tour.py' },
    @{ id = '42'; name = '物理拖拽拉伸';       script = 'scripts\42-accept-resize.py' },
    @{ id = '43'; name = '长按右方向键倍速';   script = 'scripts\43-accept-longpress.py' },
    @{ id = '44'; name = '4K 连续 Seek 性能';  script = 'scripts\44-accept-seek4k.py' },
    @{ id = '45'; name = '内存泄漏与退出闭环'; script = 'scripts\45-accept-memory.py' },
    @{ id = '46'; name = '像素级截屏自审';     script = 'scripts\46-visual-selfcheck.py' },
    @{ id = '47'; name = '安装包端到端';       script = 'scripts\47-accept-installer.py' },
    @{ id = '48'; name = '交互跟手性';         script = 'scripts\48-accept-responsiveness.py' },
    @{ id = '49'; name = '全屏/换肤/抽屉/布局'; script = 'scripts\49-accept-ui-round3.py' }
)

if (-not (Test-Path "$ROOT\assets\testmedia\uhd4k_45m.mp4")) {
    Write-Host "缺少 4K 测试片, 先生成测试媒体..." -ForegroundColor Yellow
    & $PY 'scripts\30-gen-testmedia.py'
}

New-Item -ItemType Directory -Force -Path "$ROOT\logs" | Out-Null
$summary = @()
foreach ($c in $CASES) {
    Write-Host "`n================ [$($c.id)] $($c.name) ================" -ForegroundColor Cyan
    # 每个用例前把残留进程清干净（上一个用例可能留了窗口）
    Get-Process python,unins -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    $out = "$ROOT\logs\99-$($c.id)-console.log"
    $t0 = Get-Date
    & $PY $c.script *>&1 | Tee-Object -FilePath $out
    $rc = $LASTEXITCODE
    $dur = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $tail = if (Test-Path $out) { (Get-Content $out -Tail 3) -join ' | ' } else { '' }
    $summary += [pscustomobject]@{
        id = $c.id; name = $c.name; exit_code = $rc; seconds = $dur; tail = $tail
    }
    Write-Host "[$($c.id)] $($c.name) -> 退出码 $rc ($dur s)" -ForegroundColor $(if ($rc -eq 0) { 'Green' } else { 'Red' })
}

Write-Host "`n==================== 汇总 ====================" -ForegroundColor Cyan
$summary | Format-Table -AutoSize
$summary | ConvertTo-Json -Depth 4 | Set-Content "$ROOT\logs\99-acceptance-summary.json" -Encoding UTF8

$failed = @($summary | Where-Object { $_.exit_code -ne 0 })
if ($failed.Count -eq 0) {
    Write-Host "全部 $($summary.Count) 项验收通过。" -ForegroundColor Green
    exit 0
} else {
    Write-Host "$($failed.Count)/$($summary.Count) 项未通过：$($failed.name -join ', ')" -ForegroundColor Red
    exit 1
}
