@echo off
chcp 65001 >nul
setlocal

echo ================================================================
echo  AI Movie Studio - Mo 5 Chrome Profile cu da dang nhap san
echo  - KHONG tat trinh duyet Chrome ca nhan dang mo!
echo  - Mo 5 profile: Profile 26 (1230), 27 (1231), 13 (a), 29 (1232), 30 (1233)
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

echo [1/2] Dang mo 5 profile Muse da dang nhap san (khong dong Chrome ca nhan)...
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 26" "https://muse.ai/"
ping -n 3 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 27" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 13" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 29" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 30" "https://muse.ai/"
ping -n 3 127.0.0.1 >nul

echo [2/2] Dang dua cua so Chrome ca nhan (Studio) len tren cung...
start "" "%CHROME%" --profile-directory="Default" "http://127.0.0.1:8000"

echo.
echo [OK] Da mo xong 5 profile Muse cu ma khong tat Chrome ca nhan!
endlocal
