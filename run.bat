@echo off
cd /d "%~dp0"
set NO_PROXY=localhost,127.0.0.1,e-services.cr.gov.hk,*.cr.gov.hk
set no_proxy=localhost,127.0.0.1,e-services.cr.gov.hk,*.cr.gov.hk
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe main.py %*
) else (
    python main.py %*
)
