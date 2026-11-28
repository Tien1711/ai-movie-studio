@echo off
chcp 65001 >nul
setlocal

set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"
cd /d "%BASE_DIR%"

set "NODE_EXE=C:\Program Files\nodejs\node.exe"
if not exist "%NODE_EXE%" set "NODE_EXE=node"

set "PYTHON_EXE=C:\Users\Admin\AppData\Local\Programs\Python\Python310\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ================================================================
echo  AI Movie Studio - Khoi dong Gateway + Web Studio (Man hinh chinh)
echo ================================================================
echo.

echo [1/3] Kiem tra & khoi dong Muse Browser Gateway (cong 18787)...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 18787 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "AI Movie Studio - Muse Browser Gateway" /min /D "%BASE_DIR%" cmd /k ""%NODE_EXE%" --env-file=.env scripts\muse_browser_gateway.mjs"
    ping -n 3 127.0.0.1 >nul
) else (
    echo       Gateway 18787 dang chay san.
)

echo [2/3] Kiem tra & khoi dong AI Movie Studio Web Server (cong 8000)...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    start "AI Movie Studio - Web Server" /min /D "%BASE_DIR%" cmd /k ""%PYTHON_EXE%" app.py"
) else (
    echo       Studio 8000 dang chay san.
)

echo [3/3] Dang cho AI Movie Studio san sang tren cong 8000...
powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { $c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
    echo [LOI] Studio chua len cong 8000 sau 30 giay. Hay xem cua so "AI Movie Studio - Web Server" duoi Taskbar!
    pause
    exit /b 1
)

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
set "CHROME_FLAGS=--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --disable-features=CalculateNativeWinOcclusion"

echo [OK] Studio da san sang! Dang mo http://127.0.0.1:8000 ...
if exist "%CHROME%" (
    start "" "%CHROME%" %CHROME_FLAGS% --profile-directory="Default" "http://127.0.0.1:8000"
) else (
    start "" "http://127.0.0.1:8000"
)
endlocal
