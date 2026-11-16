@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\SysMonWidget.lnk')"
echo Виджет убран из автозагрузки.
pause
