@echo off
setlocal
cd /d "%~dp0"

set "APP_EXE=%~dp0.venv\Scripts\yysls-news.exe"
if not exist "%APP_EXE%" (
    echo Project virtual environment or launcher was not found.
    echo Follow the first-time setup steps in README.md.
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0.env" (
    echo The .env configuration file was not found.
    echo Copy .env.example to .env and complete the initial setup.
    echo.
    pause
    exit /b 1
)

echo Starting the news monitor and QQBot service...
echo Admin page: http://127.0.0.1:43100/
echo Press Ctrl+C to stop all services.
echo.
"%APP_EXE%" all
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Service exited with error code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
