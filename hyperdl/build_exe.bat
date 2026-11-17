@echo off
setlocal
cd /d "%~dp0"
echo === Compilando HyperDL.exe com PyInstaller ===
python -m venv .venv-build
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e . pyinstaller
pyinstaller --clean --noconfirm hyperdl.spec
echo.
if exist dist\HyperDL.exe (
    echo [OK] Executavel criado em: %~dp0dist\HyperDL.exe
) else (
    echo [ERRO] Falha ao gerar HyperDL.exe
    exit /b 1
)
pause
