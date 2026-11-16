@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка зависимостей виджета ===

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe""
if not defined PY (
    echo Python не найден. Установите его командой:
    echo     winget install -e --id Python.Python.3.12
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Создаю окружение .venv ...
    %PY% -m venv .venv || (echo Ошибка создания окружения & pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || (echo Ошибка установки & pause & exit /b 1)
echo.
echo Готово! Теперь можно запускать run.bat
pause
