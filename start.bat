@echo off
rem
rem Open the GS1 Digital Link operator shell.  Double-click me.
rem
rem Opens a desktop window bound to 127.0.0.1 - there is no shareable URL and no browser tab.
rem "start.bat --browser" serves the same pages in a browser instead, for a machine with no
rem webview available.  The console window this opens is where errors appear; closing it
rem closes the shell.
rem
rem Run install.bat first, once.

setlocal
rem uv finds the project from here.  The data is elsewhere: see GS1_DATA_DIR below.
cd /d "%~dp0"

rem Must match install.bat; tests/test_packaging.py checks that it does.
set "PYTHON_VERSION=3.11"

set "UV="
for /f "delims=" %%I in ('where uv 2^>nul') do if not defined UV set "UV=%%I"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"

if not defined UV (
    echo This machine has not been set up yet - double-click install.bat first.
    echo.
    pause
    exit /b 1
)

rem The data folder the installer recorded (or one IT already pinned in GS1_DATA_DIR).  Without
rem it the shell would fall back to this folder, which on a release install holds no data.
rem "call" keeps cmd from stripping the quotes around the uv path inside for /f.
set "FOUND_DATA_DIR="
for /f "usebackq delims=" %%I in (`call "%UV%" run --frozen --extra ui --python %PYTHON_VERSION% python -m scripts.data_folder`) do set "FOUND_DATA_DIR=%%I"
if not defined FOUND_DATA_DIR (
    echo Double-click install.bat first - it sets up the data folder.
    echo.
    pause
    exit /b 1
)
set "GS1_DATA_DIR=%FOUND_DATA_DIR%"
echo Data folder: %GS1_DATA_DIR%

rem --frozen: use uv.lock exactly as committed and never update it.  Starting the app is not
rem the moment to resolve new versions of anything that talks to a live site.
"%UV%" run --frozen --extra ui --python %PYTHON_VERSION% python -m ui %*
if errorlevel 1 (
    echo.
    echo -- The operator shell exited with an error. ---------------------------------
    echo If it never opened a window, run install.bat again first.
    echo Otherwise send the lines above to whoever maintains this tool.
    echo.
    pause
    exit /b 1
)
