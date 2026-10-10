@echo off
setlocal

rem mini-ide one-click build script (self-contained: auto venv + deps + exe)
rem Requires Python 3.11+ on PATH.
rem Output: mini-ide.exe + mini-ide-cli.exe + mini-ide-runtime\ in project root.

cd /d "%~dp0\.."
set "BUILD_DIR=%CD%"
set "VENV_DIR=%BUILD_DIR%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "PACKAGE_ONLY=0"
if /i "%~1"=="--package-only" set "PACKAGE_ONLY=1"
if not "%~1"=="" if not "%PACKAGE_ONLY%"=="1" goto :bad_args
if not "%~2"=="" goto :bad_args

rem ---------- 1. Locate system Python ----------
set "SYS_PY="
where python >nul 2>nul && set "SYS_PY=python"
if "%SYS_PY%"=="" where py >nul 2>nul && set "SYS_PY=py -3"
if "%SYS_PY%"=="" goto :no_python

rem ---------- 2. Create venv if missing ----------
if exist "%VENV_PY%" goto :venv_ready
echo [build] Creating venv at %VENV_DIR% ...
%SYS_PY% -m venv "%VENV_DIR%"
if errorlevel 1 goto :fail_venv
:venv_ready

rem ---------- 3. Install deps if missing ----------
"%VENV_PY%" -c "import PySide6, psutil, watchdog, sqlparse, pygments, PyInstaller" >nul 2>nul
if not errorlevel 1 goto :deps_ready
echo [build] Installing dependencies (first run, may take minutes) ...
set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIP_DEFAULT_TIMEOUT=120"
"%VENV_PY%" -m pip install --quiet --upgrade pip
"%VENV_PY%" -m pip install --quiet PySide6 psutil watchdog sqlparse Pygments pyinstaller Pillow
if errorlevel 1 goto :fail_deps
:deps_ready

rem ---------- 4. Generate icon if missing ----------
if exist "src\resources\icon.ico" goto :icon_ready
echo [build] Generating icon ...
"%VENV_PY%" scripts\make_icon.py
if errorlevel 1 goto :fail_icon
:icon_ready

rem ---------- 5. Clean old artifacts ----------
echo [build] Cleaning old artifacts ...
if exist pyinstaller-work rmdir /s /q pyinstaller-work
if exist dist rmdir /s /q dist
if exist mini-ide.spec del /q mini-ide.spec
if exist mini-ide-cli.spec del /q mini-ide-cli.spec

rem Prepare candidate metadata without consuming a version on failure.
"%VENV_PY%" scripts\release_version.py prepare --root "%BUILD_DIR%" --output "pyinstaller-work\release-version"
if errorlevel 1 goto :fail_version

rem ---------- 6. Build ----------
echo [build] Running PyInstaller ...
rem Keep PyInstaller's DLL discovery away from project JDK/Node/etc. PATH entries.
rem Otherwise unrelated runtime DLLs can be frozen into mini-ide.exe.
set "ORIG_PATH=%PATH%"
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0\;%VENV_DIR%\Scripts"
"%VENV_PY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --name mini-ide ^
    --windowed ^
    --contents-directory mini-ide-runtime ^
    --icon src\resources\icon.ico ^
    --version-file "pyinstaller-work\release-version\mini-ide.exe.version.txt" ^
    --add-data "pyinstaller-work\release-version\version.json;." ^
    --add-data "src;src" ^
    --hidden-import psutil ^
    --hidden-import watchdog ^
    --hidden-import sqlparse ^
    --hidden-import pygments ^
    --distpath dist ^
    --workpath pyinstaller-work ^
    --specpath . ^
    main.py
set "BUILD_RC=%ERRORLEVEL%"
if not "%BUILD_RC%"=="0" goto :restore_path_and_fail_build

echo [build] Running PyInstaller for console CLI ...
"%VENV_PY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --name mini-ide-cli ^
    --console ^
    --contents-directory mini-ide-runtime ^
    --icon src\resources\icon.ico ^
    --version-file "pyinstaller-work\release-version\mini-ide-cli.exe.version.txt" ^
    --add-data "pyinstaller-work\release-version\version.json;." ^
    --hidden-import psutil ^
    --distpath dist ^
    --workpath pyinstaller-work ^
    --specpath . ^
    cli_main.py
set "BUILD_RC=%ERRORLEVEL%"
if not "%BUILD_RC%"=="0" goto :restore_path_and_fail_build
set "PATH=%ORIG_PATH%"

if not exist "dist\mini-ide\mini-ide.exe" goto :fail_build
if not exist "dist\mini-ide-cli\mini-ide-cli.exe" goto :fail_build
findstr /i /c:"\\jdk" "pyinstaller-work\mini-ide\Analysis-00.toc" >nul 2>nul
if not errorlevel 1 goto :fail_bad_dll_source
findstr /i /c:"\\jdk" "pyinstaller-work\mini-ide-cli\Analysis-00.toc" >nul 2>nul
if not errorlevel 1 goto :fail_bad_dll_source

rem Assemble one complete portable directory, including matching GUI/CLI metadata.
mkdir "dist\release"
copy /y "dist\mini-ide\mini-ide.exe" "dist\release\mini-ide.exe" >nul
if errorlevel 1 goto :fail_build
copy /y "dist\mini-ide-cli\mini-ide-cli.exe" "dist\release\mini-ide-cli.exe" >nul
if errorlevel 1 goto :fail_build
xcopy /e /i /y "dist\mini-ide\mini-ide-runtime" "dist\release\mini-ide-runtime" >nul
if errorlevel 1 goto :fail_build
xcopy /e /i /y "dist\mini-ide-cli\mini-ide-runtime" "dist\release\mini-ide-runtime" >nul
if errorlevel 1 goto :fail_build
if "%PACKAGE_ONLY%"=="1" goto :commit_version

echo [build] Publishing mini-ide.exe and mini-ide-cli.exe ...
copy /y "dist\mini-ide\mini-ide.exe" "..\mini-ide.exe" >nul
if errorlevel 1 goto :fail_publish
copy /y "dist\mini-ide-cli\mini-ide-cli.exe" "..\mini-ide-cli.exe" >nul
if errorlevel 1 goto :fail_publish
if exist "..\mini-ide-runtime" rmdir /s /q "..\mini-ide-runtime"
xcopy /e /i /y "dist\mini-ide\mini-ide-runtime" "..\mini-ide-runtime" >nul
if errorlevel 1 goto :fail_publish
xcopy /e /i /y "dist\mini-ide-cli\mini-ide-runtime" "..\mini-ide-runtime" >nul
if errorlevel 1 goto :fail_publish

:commit_version
if "%PACKAGE_ONLY%"=="1" (
    "%VENV_PY%" scripts\release_version.py commit --root "%BUILD_DIR%" --output "pyinstaller-work\release-version" --release "dist\release"
) else (
    "%VENV_PY%" scripts\release_version.py commit --root "%BUILD_DIR%" --output "pyinstaller-work\release-version" --release ".."
)
if errorlevel 1 goto :fail_version
echo.
if "%PACKAGE_ONLY%"=="1" (
    echo [build] Done. Output: _build\dist\release\ ^(running IDE unchanged^)
    endlocal
    exit /b 0
)
echo [build] Done. Output: mini-ide.exe + mini-ide-cli.exe + mini-ide-runtime\ (project root)
endlocal
exit /b 0

:bad_args
echo [build] Usage: build.bat [--package-only]
exit /b 2
:fail_version
echo [build] Version metadata failed. The build is not marked successful.
exit /b 1
:no_python
echo [build] ERROR: python not found. Install Python 3.11+ first.
exit /b 1
:fail_venv
echo [build] venv creation failed
exit /b 1
:fail_deps
echo [build] dep install failed
exit /b 1
:fail_icon
echo [build] icon generation failed
exit /b 1
:fail_bad_dll_source
echo [build] PyInstaller collected DLLs from a JDK path. Check PATH isolation.
exit /b 1
:fail_publish
echo [build] failed to publish mini-ide executables. Close running mini-ide first.
exit /b 1
:restore_path_and_fail_build
set "PATH=%ORIG_PATH%"
goto :fail_build
:fail_build
echo [build] PyInstaller failed
exit /b 1
