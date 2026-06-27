@echo off
schtasks /Delete /TN "Aperio" /F
taskkill /IM aperio.exe /F 2>nul
echo Aperio daemon removed.
