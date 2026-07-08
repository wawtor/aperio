@echo off
REM Install Aperio daemon to start at Windows logon.
REM
REM aperio.exe must be built first:
REM   cd daemon
REM   cargo build --release
REM   copy target\release\aperio.exe ..\aperio.exe

set "EXE=%~dp0aperio.exe"

if not exist "%EXE%" (
    echo ERROR: aperio.exe not found.
    echo.
    echo Build it first:
    echo   cd src
    echo   cargo build --release
    echo   copy target\release\aperio.exe ..\aperio.exe
    echo.
    pause
    exit /b 1
)

schtasks /Create /TN "Aperio" /TR "\"%EXE%\"" /SC ONLOGON /RL LIMITED /F

REM Create the `aperio` command shim and put it on the user PATH
if not exist "%~dp0bin" mkdir "%~dp0bin"
echo @echo off> "%~dp0bin\aperio.cmd"
echo start "" pythonw "%%~dp0..\aperio.py" %%*>> "%~dp0bin\aperio.cmd"
powershell -NoProfile -Command ^
  "$b = '%~dp0bin'; $p = [Environment]::GetEnvironmentVariable('Path','User');" ^
  "if ((';' + $p + ';') -notlike ('*;' + $b + ';*')) {" ^
  "  [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';' + $b), 'User') }"

echo.
echo Aperio daemon installed ^(runs at logon^).
echo   Setup GUI  : aperio   ^(from any new terminal^)
echo   Start now  : schtasks /Run /TN Aperio
echo   Stop now   : taskkill /IM aperio.exe /F
echo   Log file   : %~dp0aperio.log
echo   Uninstall  : uninstall.bat
