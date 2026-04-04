@echo off
chcp 65001 >nul
title KRX 자동매매 봇 (외부 접속)

:: 가상환경 확인
if not exist ".venv\Scripts\python.exe" (
    echo [오류] install.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:: .env 확인
if not exist ".env" (
    echo [오류] .env 파일이 없습니다.
    pause
    exit /b 1
)

:: WEB_PASSWORD 설정 여부 경고
findstr /C:"WEB_PASSWORD=" .env | findstr /V "your_secure_password_here" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [경고] .env 파일에 WEB_PASSWORD 가 설정되어 있지 않습니다!
    echo        외부에서 누구나 접속 가능합니다. 설정을 권장합니다.
    echo.
    choice /C YN /M "그래도 계속 실행하시겠습니까?"
    if errorlevel 2 exit /b 0
)

:: ngrok 실행 방법 선택
echo.
echo ════════════════════════════════════════
echo   외부 접속 방법을 선택하세요
echo ════════════════════════════════════════
echo   [1] ngrok (무료, 매번 URL 바뀜)
echo   [2] Cloudflare Tunnel (무료, URL 고정 가능)
echo   [3] 없음 - 로컬 네트워크만 사용
echo.
choice /C 123 /M "선택"

if errorlevel 3 goto local_only
if errorlevel 2 goto cloudflare
if errorlevel 1 goto ngrok

:ngrok
echo.
:: ngrok 설치 확인
where ngrok >nul 2>&1
if errorlevel 1 (
    echo [ngrok 설치 필요]
    echo 1. https://ngrok.com/download 에서 다운로드
    echo 2. ngrok.exe 를 이 폴더에 복사
    echo 3. https://ngrok.com 에서 무료 회원가입 후 authtoken 복사
    echo 4. 명령창에서: ngrok config add-authtoken 발급받은토큰
    pause
    exit /b 1
)
echo [ngrok] 서버 시작 후 터널 연결 중...
start "KRX봇-서버" .venv\Scripts\python.exe web.py
timeout /t 3 /nobreak >nul
start "KRX봇-ngrok" ngrok http 5001 --log=stdout
echo.
echo ngrok 창에서 'Forwarding' 줄의 https://xxxx.ngrok-free.app URL로 접속하세요.
goto end

:cloudflare
echo.
:: cloudflared 확인
where cloudflared >nul 2>&1
if errorlevel 1 (
    echo [Cloudflare Tunnel 설치 필요]
    echo 1. https://github.com/cloudflare/cloudflared/releases 에서
    echo    cloudflared-windows-amd64.exe 다운로드
    echo 2. cloudflared.exe 로 이름 변경 후 이 폴더에 복사
    pause
    exit /b 1
)
echo [Cloudflare] 서버 시작 후 터널 연결 중...
start "KRX봇-서버" .venv\Scripts\python.exe web.py
timeout /t 3 /nobreak >nul
start "KRX봇-tunnel" cloudflared tunnel --url http://localhost:5001
echo.
echo Cloudflare 창에서 'trycloudflare.com' URL로 접속하세요.
goto end

:local_only
echo.
echo [로컬 네트워크 전용]
echo 같은 공유기에 연결된 기기에서만 접속 가능합니다.
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
    set LOCAL_IP=%%a
    goto got_ip
)
:got_ip
set LOCAL_IP=%LOCAL_IP: =%
echo 접속 주소: http://%LOCAL_IP%:5001
echo.
.venv\Scripts\python.exe web.py
goto end

:end
echo.
pause
