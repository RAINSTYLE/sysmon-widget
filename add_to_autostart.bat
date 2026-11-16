@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "EXE=%~dp0dist\SysMonWidget.exe"
if not exist "%EXE%" (
    echo Не найден dist\SysMonWidget.exe — сначала запустите build_exe.bat
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\SysMonWidget.lnk');" ^
  "$s.TargetPath = $env:EXE; $s.WorkingDirectory = Split-Path $env:EXE; $s.Save()"
if errorlevel 1 (echo Не удалось создать ярлык & pause & exit /b 1)
echo Готово! Виджет будет запускаться вместе с Windows.
pause
