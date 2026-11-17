@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [HyperDL] A criar ambiente virtual Python...
    python -m venv .venv
)
echo [HyperDL] A instalar/atualizar dependencias...
".venv\Scripts\python.exe" -m pip install --quiet -e .
echo [HyperDL] A iniciar servidor em http://127.0.0.1:8787 ...
start "" "http://127.0.0.1:8787"
".venv\Scripts\python.exe" -m hyperdl serve --host 127.0.0.1 --port 8787
pause
