param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    if (-not $SkipFrontend) {
        Push-Location -LiteralPath (Join-Path $taskRoot 'frontend')
        try {
            npm ci
            if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
            npm run build
            if ($LASTEXITCODE -ne 0) { throw '界面构建失败。' }
        } finally { Pop-Location }
    }
    python -m PyInstaller --noconfirm --clean --onefile --name FileAtlas --add-data 'frontend/dist;frontend/dist' --collect-all PIL run.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows 程序构建失败。' }
    Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $taskRoot 'dist/FileAtlas.exe')
} finally { Pop-Location }
