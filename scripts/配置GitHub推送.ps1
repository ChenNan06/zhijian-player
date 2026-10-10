# 至简 Player —— GitHub 推送凭据一次性配置
#
# 为什么需要这一步：
#   你希望"任何 AI 都可以随时帮我上传"。GitHub 默认不记住凭据，
#   每次推送都要重新输 token —— AI 没法输。所以把凭据存到本机用户目录
#   （不是仓库里），之后**任何工具**都能直接 git push。
#
# ⚠️ token 只写进 %USERPROFILE%\.git-credentials，
#    不会进仓库、不会进提交记录。用完可随时在 GitHub 上吊销。

$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = '至简 Player - GitHub 凭据配置'

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' 至简 Player - GitHub 推送凭据一次性配置' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host '你需要准备：'
Write-Host '  1) 仓库地址，形如  https://github.com/你的用户名/仓库名.git'
Write-Host '  2) 一个 Personal Access Token（Fine-grained，勾 repo 读写）'
Write-Host '     申请地址： https://github.com/settings/tokens'
Write-Host ''

$repo = Read-Host '请粘贴仓库地址'
if ([string]::IsNullOrWhiteSpace($repo)) { Write-Host '仓库地址为空，退出。' -ForegroundColor Red; Read-Host '按回车关闭'; exit 1 }

$user = Read-Host '请输入你的 GitHub 用户名'
if ([string]::IsNullOrWhiteSpace($user)) { Write-Host '用户名为空，退出。' -ForegroundColor Red; Read-Host '按回车关闭'; exit 1 }

$sec = Read-Host '请粘贴 Personal Access Token' -AsSecureString
$token = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ([string]::IsNullOrWhiteSpace($token)) { Write-Host 'token 为空，退出。' -ForegroundColor Red; Read-Host '按回车关闭'; exit 1 }

Write-Host ''
Write-Host '[1/3] 配置凭据存储 ...' -ForegroundColor Yellow
git config --global credential.helper store

Write-Host '[2/3] 写入凭据文件 ...' -ForegroundColor Yellow
$credPath = Join-Path $env:USERPROFILE '.git-credentials'
$line = "https://${user}:${token}@github.com"
if (Test-Path $credPath) {
    $kept = Get-Content $credPath -ErrorAction SilentlyContinue | Where-Object { $_ -notmatch 'github\.com' }
    $kept = @($kept) + $line
    Set-Content -Path $credPath -Value $kept -Encoding ASCII
} else {
    Set-Content -Path $credPath -Value $line -Encoding ASCII
}
Write-Host "      已写入 $credPath" -ForegroundColor Green

Write-Host '[3/3] 配置 remote 并测试 ...' -ForegroundColor Yellow
git remote remove origin 2>$null
git remote add origin $repo
git fetch origin 2>&1 | ForEach-Object { Write-Host "      $_" }

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' 完成。之后任何 AI / 工具都可以直接执行：' -ForegroundColor Green
Write-Host ''
Write-Host '     git push -u origin main' -ForegroundColor White
Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Read-Host '按回车关闭'
