@echo off
chcp 65001 >nul
cd /d "%~dp0"
set NO_PROXY=localhost,127.0.0.1,::1
set no_proxy=localhost,127.0.0.1,::1

set "PY_CMD=python"
python --version >nul 2>nul
if errorlevel 1 (
    py -3 --version >nul 2>nul
    if errorlevel 1 (
        echo Python not found. Trying to install Python 3 with winget...
        where winget >nul 2>nul
        if errorlevel 1 goto NEED_PYTHON
        winget install --id Python.Python.3.13 -e --silent --accept-package-agreements --accept-source-agreements
        python --version >nul 2>nul
        if errorlevel 1 (
            py -3 --version >nul 2>nul
            if errorlevel 1 goto NEED_PYTHON
            set "PY_CMD=py -3"
        )
    ) else (
        set "PY_CMD=py -3"
    )
)

echo Checking full dependencies...
%PY_CMD% -m app.dependency_manager --ensure
if errorlevel 1 (
    echo Dependency check failed. See data\dependency_report.json for details.
    pause
    exit /b 1
)

echo Starting WebUI: http://127.0.0.1:7860
%PY_CMD% -m app.webui
pause
exit /b 0

:NEED_PYTHON
echo Python is required but could not be installed automatically.
echo Please install Python 3.11+ manually, then run this file again.
pause
exit /b 1
