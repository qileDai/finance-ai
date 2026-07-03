# 香港公司工商注册智能体 - 安装脚本 (Windows PowerShell)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== 安装依赖 ===" -ForegroundColor Cyan

# 绕过代理 SSL 问题
$env:NO_PROXY = "*"
$env:no_proxy = "*"

python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt -i https://pypi.org/simple

Write-Host "=== 安装 Playwright 浏览器 ===" -ForegroundColor Cyan
.\.venv\Scripts\playwright install chromium

Write-Host ""
Write-Host "=== 安装完成 ===" -ForegroundColor Green
Write-Host "运行: .\.venv\Scripts\python main.py --step register"
Write-Host "完整流程: .\.venv\Scripts\python main.py --full"
