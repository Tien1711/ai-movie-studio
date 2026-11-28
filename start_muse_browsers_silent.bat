@echo off
chcp 65001 >nul
setlocal

echo ================================================================
echo  AI Movie Studio - Mo 5 Chrome Profile chay ngam FULL TOC DO
echo  (Khong bi Windows Chrome dong bang khi thu nho / nam sau)
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

echo [1/3] Dang dong nhe nhang Chrome mac dinh de nap 4 co chong dong bang...
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -notlike '*--type=*' -and $_.CommandLine -notlike '*muse2api-profiles*' }; foreach ($p in $procs) { $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue; if ($proc) { $null = $proc.CloseMainWindow() } }; Start-Sleep -Seconds 3; $left = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -notlike '*muse2api-profiles*' }; if ($left) { $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }"

ping -n 3 127.0.0.1 >nul

echo [2/3] Dang mo 5 profile Muse o lop nen sau cung (1230, 1231, a, 1232, 1233)...
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 26" "https://muse.ai/"
ping -n 3 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 27" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 13" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 29" "https://muse.ai/"
ping -n 2 127.0.0.1 >nul
start "" "%CHROME%" %FLAGS% --profile-directory="Profile 30" "https://muse.ai/"
ping -n 4 127.0.0.1 >nul

echo [3/3] Dang dua cua so Chrome ca nhan (Studio http://127.0.0.1:8000) len tren cung...
start "" /max "%CHROME%" %FLAGS% --restore-last-session --profile-directory="Default" "http://127.0.0.1:8000"

echo.
echo [OK] Da mo xong 5 trinh duyet Muse chay ngam full toc do!
endlocal
