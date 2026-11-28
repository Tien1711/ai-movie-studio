@echo off
chcp 65001 >nul
setlocal

echo ================================================================
echo  AI Movie Studio - Mo 5 Chrome Worker DOC LAP chay ngam
echo  - KHONG tat, KHONG anh huong trinh duyet Chrome ca nhan!
echo  - Luon nap du 4 co chong dong bang (full toc do khi chay nen)
echo ================================================================
echo.

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
    echo [LOI] Khong tim thay chrome.exe!
    pause
    exit /b 1
)

set "BASE_DIR=%~dp0"
set "WORKER_DATA=%BASE_DIR%data\muse_chrome_workers"
if not exist "%WORKER_DATA%" mkdir "%WORKER_DATA%"

set "FLAGS=--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --disable-features=CalculateNativeWinOcclusion --no-first-run --no-default-browser-check --window-size=1100,800"

echo [1/2] Dang mo 5 Chrome Worker doc lap (Worker1 .. Worker5)...
start "" "%CHROME%" --user-data-dir="%WORKER_DATA%" %FLAGS% --profile-directory="Worker1" "https://muse.ai/"
ping -n 3 127.0.0.1 >nul
start "" "%CHROME%" --user-data-dir="%WORKER_DATA%" %FLAGS% --profile-directory="Worker2" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" --user-data-dir="%WORKER_DATA%" %FLAGS% --profile-directory="Worker3" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" --user-data-dir="%WORKER_DATA%" %FLAGS% --profile-directory="Worker4" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" --user-data-dir="%WORKER_DATA%" %FLAGS% --profile-directory="Worker5" "https://muse.ai/"

echo.
echo [OK] Da mo 5 Chrome Worker doc lap tai: %WORKER_DATA%
echo      (Lan dau tien: cai extension chrome-muse-profile + dang nhap muse.ai o moi Worker 1 lan duy nhat.)
endlocal
