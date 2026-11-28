@echo off
chcp 65001 >nul
setlocal

set "BASE_DIR=%~dp0"
cd /d "%BASE_DIR%"

echo ================================================================
echo  AI Movie Studio - Khoi dong Gateway + Web Studio (Man hinh chinh)
echo ================================================================
echo.

echo [1/3] Kiem tra & khoi dong Muse Browser Gateway (cong 18787)...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 18787 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "AI Movie Studio - Muse Browser Gateway" /min cmd /k "cd /d ""%BASE_DIR%"" && node --env-file=.env scripts\muse_browser_gateway.mjs"
    ping -n 3 127.0.0.1 >nul
) else (
    echo       Gateway 18787 dang chay san.
)

echo [2/3] Kiem tra & khoi dong AI Movie Studio Web Server (cong 8000)...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "AI Movie Studio - Web Server" /min cmd /k "cd /d ""%BASE_DIR%"" && set PYTHONUTF8=1 && python app.py"
    ping -n 4 127.0.0.1 >nul
) else (
    echo       Studio 8000 dang chay san.
)

echo [3/3] Dang mo giao dien AI Movie Studio (http://127.0.0.1:8000)...
start "" "http://127.0.0.1:8000"

echo.
echo [OK] Da san sang! Ban co the bam nut "Mo Chrome chay nen" truc tiep tren web.
endlocal
