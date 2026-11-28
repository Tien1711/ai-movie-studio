@echo off
chcp 65001 >nul
setlocal

echo ================================================================
echo  AI Movie Studio - Mo 5 Chrome Profile cu chay ngam (An Taskbar)
echo  - KHONG tat trinh duyet Chrome ca nhan dang mo!
echo  - Mo 5 profile: Profile 26 (1230), 27 (1231), 13 (a), 29 (1232), 30 (1233)
echo  - An 5 icon khoi thanh Taskbar & day ra sau cua so chinh
echo ================================================================
echo.

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
    echo [LOI] Khong tim thay chrome.exe!
    pause
    exit /b 1
)

set "FLAGS=--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --disable-features=CalculateNativeWinOcclusion --window-size=1100,800"

echo [1/3] Dang mo 5 profile Muse da dang nhap san (khong dong Chrome ca nhan)...
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 26" "https://muse.ai/"
ping -n 3 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 27" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 13" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 29" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 30" "https://muse.ai/"
ping -n 5 127.0.0.1 >nul

echo [2/3] Dang an 5 cua so Muse khoi thanh Taskbar va day ra lop nen sau cung...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\hide_muse_taskbar.ps1"

echo [3/3] Xong! 5 Chrome Muse dang chay ngam gon gang, khong hien tren Taskbar.
endlocal
