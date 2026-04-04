@echo off
chcp 65001 >nul
title KRX 자동매매 봇

:: 가상환경 확인
if not exist ".venv\Scripts\python.exe" (
    echo [오류] 가상환경이 없습니다. install.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:: .env 확인
if not exist ".env" (
    echo [오류] .env 파일이 없습니다. API 키를 설정하세요.
    pause
    exit /b 1
)

echo ================================================
echo   KRX 자동매매 봇 시작
echo   대시보드: http://localhost:5001
echo   종료: 이 창을 닫거나 Ctrl+C
echo ================================================
echo.

:: 브라우저 자동 열기 (3초 후)
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5001"

:: 서버 실행
.venv\Scripts\python.exe web.py

echo.
echo 서버가 종료되었습니다.
pause
