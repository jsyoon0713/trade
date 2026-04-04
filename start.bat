@echo off
chcp 65001 >nul
title KRX 자동매매 봇

:: 가상환경 확인
if not exist ".venv\Scripts\python.exe" (
    echo [오류] 가상환경이 없습니다. install.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:: .env 확인 및 포트 읽기
if not exist ".env" (
    echo [오류] .env 파일이 없습니다. API 키를 설정하세요.
    pause
    exit /b 1
)

:: .env 에서 WEB_PORT 읽기 (없으면 기본값 5001)
set WEB_PORT=5001
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="WEB_PORT" set WEB_PORT=%%b
)
:: 공백 제거
set WEB_PORT=%WEB_PORT: =%

:: 포트 사용 중 확인
netstat -ano | findstr ":%WEB_PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo [경고] 포트 %WEB_PORT% 가 이미 사용 중입니다.
    echo.
    echo 해결 방법:
    echo   1. .env 파일에서 WEB_PORT 를 다른 번호로 변경
    echo      예: WEB_PORT=5002
    echo.
    echo   2. 사용 중인 프로세스 확인:
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%WEB_PORT% " ^| findstr "LISTENING"') do (
        echo      PID %%p ^(작업관리자에서 확인 가능^)
    )
    echo.

    :: 5002~5010 에서 빈 포트 자동 탐색
    set FOUND_PORT=
    for %%p in (5002 5003 5004 5005 5006 5007 5008 5009 5010) do (
        if not defined FOUND_PORT (
            netstat -ano | findstr ":%%p " | findstr "LISTENING" >nul 2>&1
            if errorlevel 1 set FOUND_PORT=%%p
        )
    )

    if defined FOUND_PORT (
        echo   사용 가능한 포트: %FOUND_PORT%
        choice /C YN /M "%FOUND_PORT% 포트로 대신 실행할까요?"
        if errorlevel 2 (
            echo .env 파일을 수정하고 다시 실행하세요.
            pause
            exit /b 1
        )
        set WEB_PORT=%FOUND_PORT%
    ) else (
        echo .env 파일에서 WEB_PORT 를 변경 후 다시 실행하세요.
        pause
        exit /b 1
    )
)

:: Tailscale IP 표시
set TAILSCALE_IP=
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"100."') do (
    if not defined TAILSCALE_IP (
        set TAILSCALE_IP=%%a
        set TAILSCALE_IP=!TAILSCALE_IP: =!
    )
)

:: 로컬 IP 표시
setlocal enabledelayedexpansion
set LOCAL_IP=
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do (
    if not defined LOCAL_IP (
        set LOCAL_IP=%%a
        set LOCAL_IP=!LOCAL_IP: =!
    )
)

echo.
echo ════════════════════════════════════════
echo   KRX 자동매매 봇 시작  (포트 %WEB_PORT%)
echo ════════════════════════════════════════
echo   로컬:       http://localhost:%WEB_PORT%
if defined LOCAL_IP (
    echo   로컬네트워크: http://%LOCAL_IP%:%WEB_PORT%
)
if defined TAILSCALE_IP (
    echo   Tailscale:  http://%TAILSCALE_IP%:%WEB_PORT%
) else (
    echo   Tailscale:  (Tailscale 미연결 - 연결 후 위 주소로 외부 접속 가능)
)
echo   종료: 이 창을 닫거나 Ctrl+C
echo ════════════════════════════════════════
echo.

:: 브라우저 자동 열기 (3초 후)
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:%WEB_PORT%"

:: WEB_PORT 환경변수 설정 후 서버 실행
set WEB_PORT=%WEB_PORT%
.venv\Scripts\python.exe web.py

echo.
echo 서버가 종료되었습니다.
pause
