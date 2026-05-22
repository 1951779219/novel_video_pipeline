@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
title Novel Video Pipeline Updater

set "UPDATE_SRC=%~dp0"
if "%UPDATE_SRC:~-1%"=="\" set "UPDATE_SRC=%UPDATE_SRC:~0,-1%"

if not "%~1"=="" (
    set "TARGET_DIR=%~1"
    goto :normalize_target
)

echo ============================================================
echo   Novel Video Pipeline - Update Tool
echo ============================================================
echo.
echo Drag or type the existing app directory, then press Enter.
echo Example: F:\Manao\novel_video_pipeline
echo.

:ask_target
set "TARGET_DIR="
set /p "TARGET_DIR=Target app directory: "

:normalize_target
set "TARGET_DIR=%TARGET_DIR:"=%"
if "%TARGET_DIR:~-1%"=="\" set "TARGET_DIR=%TARGET_DIR:~0,-1%"

if not defined TARGET_DIR (
    echo [ERROR] Target directory is empty.
    goto :ask_target
)

if not exist "%TARGET_DIR%" (
    echo [ERROR] Target directory does not exist: %TARGET_DIR%
    goto :ask_target
)

if not exist "%TARGET_DIR%\app\gui.py" (
    echo [ERROR] Target directory does not look like the app root.
    echo         app\gui.py was not found in: %TARGET_DIR%
    goto :ask_target
)

if not exist "%UPDATE_SRC%\app" (
    echo [ERROR] Update package is missing app directory: %UPDATE_SRC%\app
    pause
    exit /b 1
)

echo.
echo Update source : %UPDATE_SRC%
echo Target app    : %TARGET_DIR%
echo.
echo Waiting for the app to exit...
ping -n 4 127.0.0.1 >nul

echo.
echo [1/4] Backing up settings...
if exist "%TARGET_DIR%\data\settings.json" (
    if not exist "%TARGET_DIR%\data" mkdir "%TARGET_DIR%\data" >nul 2>&1
    copy /y "%TARGET_DIR%\data\settings.json" "%TARGET_DIR%\data\settings.json.bak" >nul
    echo   settings.json backed up.
) else (
    echo   no settings.json found, skip.
)

echo.
echo [2/4] Updating app directory...
robocopy "%UPDATE_SRC%\app" "%TARGET_DIR%\app" /E /IS /IT /XD __pycache__ /NP /NFL /NDL /NJH /NJS >nul
if errorlevel 8 (
    echo [ERROR] Failed to update app directory.
    pause
    exit /b 1
)
echo   app directory updated.

echo.
echo [3/4] Updating root files...
for %%f in (
    README.md
    requirements.txt
    update.bat
    启动.bat
    桌面GUI.bat
) do (
    if exist "%UPDATE_SRC%\%%f" (
        copy /y "%UPDATE_SRC%\%%f" "%TARGET_DIR%\%%f" >nul
        echo   %%f
    )
)

echo.
echo [4/4] Cleaning Python cache...
if exist "%TARGET_DIR%\app" (
    for /d /r "%TARGET_DIR%\app" %%d in (__pycache__) do (
        if exist "%%d" rd /s /q "%%d" >nul 2>&1
    )
)
echo   cache cleaned.

echo.
echo ============================================================
echo   Update complete.
echo   You can close this window and start the app normally.
echo ============================================================
echo.
pause
exit /b 0
