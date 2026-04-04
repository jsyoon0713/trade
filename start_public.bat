@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title KRX 자동매매 봇 (Tailscale 외부 접속)

:: 가상환경 확인
if not exist ".venv\Scripts\python.exe" (
    echo [오류] install.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:: 포트 읽기
set WEB_PORT=5001
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="WEB_PORT" set WEB_PORT=%%b
)
set WEB_PORT=%WEB_PORT: =%

:: Tailscale 연결 확인
echo Tailscale 상태 확인 중...
set TAILSCALE_IP=

:: Tailscale 100.x.x.x 대역 IP 탐색
for /f "tokens=2 delims=:" %%a in ('ipconfig 2^>nul ^| findstr /C:"100."') do (
    set CANDIDATE=%%a
    set CANDIDATE=!CANDIDATE: =!
    echo !CANDIDATE! | findstr /R "^100\." >nul 2>&1
    if not errorlevel 1 (
        if not defined TAILSCALE_IP set TAILSCALE_IP=!CANDIDATE!
    )
)

echo.
if defined TAILSCALE_IP (
    echo [Tailscale 연결됨]
    echo   Tailscale IP: %TAILSCALE_IP%
    echo.
    echo   외부 접속 주소: http://%TAILSCALE_IP%:%WEB_PORT%
    echo   ^(같은 Tailscale 계정이 연결된 기기에서 접속 가능^)
) else (
    echo [Tailscale 미연결]
    echo.
    echo   Tailscale 이 실행되지 않았거나 로그인되지 않았습니다.
    echo.
    echo   설치/설정 방법:
    echo     1. https://tailscale.com/download 에서 Windows용 다운로드
    echo     2. 설치 후 로그인 ^(Google/GitHub 계정 사용 가능^)
    echo     3. 접속할 다른 기기에도 Tailscale 설치 + 같은 계정 로그인
    echo     4. 이 스크립트 다시 실행
    echo.
    choice /C YN /M "Tailscale 없이 로컬만 실행하시겠습니까?"
    if errorlevel 2 (
        pause
        exit /b 0
    )
)

:: WEB_PASSWORD 경고
findstr /C:"WEB_PASSWORD=" .env >nul 2>&1
if errorlevel 1 (
    echo.
    echo [경고] .env 에 WEB_PASSWORD 미설정 — 누구나 접속 가능합니다!
    echo        반드시 비밀번호를 설정하세요.
    echo.
)

echo.
echo ════════════════════════════════════════
echo   서버 시작 중... (포트 %WEB_PORT%)
echo ════════════════════════════════════════
echo.

:: 브라우저 자동 열기
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:%WEB_PORT%"

set WEB_PORT=%WEB_PORT%
.venv\Scripts\python.exe web.py

echo.
echo 서버가 종료되었습니다.
pause
