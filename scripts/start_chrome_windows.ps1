$ErrorActionPreference = "Stop"
$profileDir = Join-Path $PSScriptRoot "..\.browser-profile"
$chromeCandidates = @(
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
  "$env:PROGRAMFILES\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "$env:PROGRAMFILES\Microsoft\Edge\Application\msedge.exe"
)
$browser = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { throw "未找到 Chrome 或 Edge" }
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
Start-Process -FilePath $browser -ArgumentList @(
  "--remote-debugging-port=9222",
  "--user-data-dir=$profileDir",
  # BOSS 的安全脚本会读 navigator.webdriver 和 AutomationControlled 特征。
  "--disable-blink-features=AutomationControlled",
  "--no-default-browser-check",
  "--no-first-run",
  "https://www.zhipin.com/web/user/"
)
Write-Host "已启动独立浏览器。请登录 BOSS 后运行 job-radar collect。"
