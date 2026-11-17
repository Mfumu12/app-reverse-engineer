@echo off
setlocal
cd /d "%~dp0"
echo === Compilando GenniusDM.exe (Desktop + WebView2) com PyInstaller ===
python -m venv .venv-build
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e . pywebview pyinstaller
pyinstaller --clean --noconfirm hyperdl.spec
echo.
if exist dist\GenniusDM.exe (
    echo [OK] Executavel desktop criado em: %~dp0dist\GenniusDM.exe
) else (
    echo [ERRO] Falha ao gerar GenniusDM.exe
    exit /b 1
)
pause
