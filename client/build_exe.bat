@echo off
REM updater.exe 빌드 스크립트
REM 실행 전: pip install pyinstaller requests

echo [빌드] updater.exe 빌드 시작...

pyinstaller ^
    --onefile ^
    --name updater ^
    --icon NONE ^
    --console ^
    updater.py

if %ERRORLEVEL% == 0 (
    echo [빌드] 완료! dist\updater.exe
    echo [배포] dist\updater.exe 를 각 PC의 C:\auto\ 에 복사하세요
) else (
    echo [빌드] 실패!
)
pause
