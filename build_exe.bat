@echo off
chcp 65001 >nul
echo ================================================
echo   KRX 자동매매 봇 - 단독 실행파일(.exe) 빌드
echo   Python이 없는 PC에서도 실행 가능한 exe 생성
echo ================================================
echo.

:: 가상환경 확인
if not exist ".venv\Scripts\python.exe" (
    echo [오류] 가상환경이 없습니다. install.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:: PyInstaller 설치
echo [1/3] PyInstaller 설치 확인...
.venv\Scripts\pip.exe install pyinstaller --quiet
echo       완료

:: 빌드
echo [2/3] 실행파일 빌드 중 (수 분 소요)...
.venv\Scripts\pyinstaller.exe autotrader.spec --noconfirm
if errorlevel 1 (
    echo [오류] 빌드 실패
    pause
    exit /b 1
)
echo       완료

:: 배포 폴더 구성
echo [3/3] 배포 파일 정리...
if not exist "dist\KRX자동매매" mkdir "dist\KRX자동매매"

:: 필수 파일 복사
copy ".env.example" "dist\KRX자동매매\.env.example" >nul
xcopy "config" "dist\KRX자동매매\config" /E /I /Y >nul
if exist "data" xcopy "data" "dist\KRX자동매매\data" /E /I /Y >nul
if exist "logs" xcopy "logs" "dist\KRX자동매매\logs" /E /I /Y >nul

:: 실행 안내 파일 생성
(
echo KRX 자동매매 봇 실행 방법
echo ============================
echo.
echo 1. .env.example 을 복사하여 .env 로 이름 변경
echo 2. .env 파일에 LS증권 API 키 입력
echo 3. KRX자동매매.exe 실행
echo 4. 브라우저에서 http://localhost:5001 접속
echo.
echo 문의: settings.yaml 에서 설정 변경 가능
) > "dist\KRX자동매매\실행방법.txt"

echo.
echo ================================================
echo   빌드 완료!
echo   배포 폴더: dist\KRX자동매매\
echo   이 폴더 전체를 다른 PC로 복사하세요.
echo ================================================
pause
