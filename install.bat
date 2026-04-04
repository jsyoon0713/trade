@echo off
chcp 65001 >nul
echo ================================================
echo   KRX 자동매매 봇 - 초기 설치
echo ================================================
echo.

:: Python 설치 확인
python --version >nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo.
    echo Python 3.11 이상을 설치하세요:
    echo   https://www.python.org/downloads/
    echo.
    echo 설치 시 반드시 "Add Python to PATH" 체크!
    pause
    exit /b 1
)

python --version
echo.

:: 가상환경 생성
if not exist ".venv" (
    echo [1/3] 가상환경 생성 중...
    python -m venv .venv
    if errorlevel 1 (
        echo [오류] 가상환경 생성 실패
        pause
        exit /b 1
    )
    echo       완료
) else (
    echo [1/3] 가상환경이 이미 존재합니다 - 건너뜀
)

:: pip 업그레이드
echo [2/3] pip 업그레이드 중...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
echo       완료

:: 패키지 설치
echo [3/3] 패키지 설치 중 (시간이 걸릴 수 있습니다)...
.venv\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
    echo [오류] 패키지 설치 실패
    pause
    exit /b 1
)
echo       완료

:: .env 파일 확인
echo.
if not exist ".env" (
    echo [주의] .env 파일이 없습니다. .env.example을 복사하여 설정하세요.
    copy .env.example .env >nul
    echo       .env 파일을 생성했습니다 - API 키를 입력해 주세요.
    notepad .env
) else (
    echo .env 파일 확인됨
)

echo.
echo ================================================
echo   설치 완료! start.bat 으로 실행하세요.
echo ================================================
pause
