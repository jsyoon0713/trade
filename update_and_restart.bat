@echo off
chcp 65001 >nul
title KRX 봇 업데이트 및 재시작

echo.
echo ════════════════════════════════════════
echo   KRX 자동매매 봇 — 업데이트 및 재시작
echo ════════════════════════════════════════
echo.

:: ── 실행 중인 봇 프로세스 확인 ────────────────────────────────────────────
echo [1] 실행 중인 봇 종료 중...
tasklist /FI "IMAGENAME eq python.exe" 2>nul | find "python.exe" >nul
if not errorlevel 1 (
    echo     python.exe 프로세스 발견 - 종료 시도...
    taskkill /F /IM python.exe >nul 2>&1
    timeout /t 2 /nobreak >nul
    echo     종료 완료
) else (
    echo     실행 중인 python 프로세스 없음
)

:: ── GitHub 업데이트 ───────────────────────────────────────────────────────
echo.
echo [2] GitHub 최신 코드 적용 중...
git fetch origin >nul 2>&1
if errorlevel 1 (
    echo     [오류] Git fetch 실패. 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

for /f %%a in ('git rev-parse HEAD') do set LOCAL_HASH=%%a
for /f %%a in ('git rev-parse origin/master 2^>nul || git rev-parse origin/main 2^>nul') do set REMOTE_HASH=%%a

if "%LOCAL_HASH%"=="%REMOTE_HASH%" (
    echo     이미 최신 버전입니다.
) else (
    git pull origin master >nul 2>&1 || git pull origin main >nul 2>&1
    echo     업데이트 완료!

    :: requirements 변경 여부 확인
    git diff --name-only HEAD@{1} HEAD 2>nul | findstr /i "requirements" >nul 2>&1
    if not errorlevel 1 (
        echo     패키지 업데이트 중...
        .venv\Scripts\pip.exe install -r requirements.txt -q
        echo     패키지 업데이트 완료!
    )
)

:: ── 봇 재시작 ─────────────────────────────────────────────────────────────
echo.
echo [3] 봇 재시작...
timeout /t 2 /nobreak >nul
start "" cmd /c "start.bat"

echo.
echo 업데이트 및 재시작 완료!
timeout /t 3 /nobreak >nul
exit
