@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Сборка SysMonWidget.exe ===
if not exist ".venv\Scripts\python.exe" (
    echo Сначала запустите install.bat
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole ^
    --name SysMonWidget widget.py || (echo Ошибка сборки & pause & exit /b 1)
echo.
echo Готово! Файл: %~dp0dist\SysMonWidget.exe
echo Его можно скопировать в любую папку и запускать двойным кликом.
pause
