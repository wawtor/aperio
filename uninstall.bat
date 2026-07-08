@echo off
schtasks /Delete /TN "Aperio" /F
taskkill /IM aperio.exe /F 2>nul

REM Remove the `aperio` command shim and its PATH entry
powershell -NoProfile -Command ^
  "$b = '%~dp0bin'; $p = [Environment]::GetEnvironmentVariable('Path','User');" ^
  "$parts = $p -split ';' | Where-Object { $_ -and $_ -ine $b };" ^
  "[Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')"
if exist "%~dp0bin" rmdir /s /q "%~dp0bin"

echo Aperio daemon removed.
