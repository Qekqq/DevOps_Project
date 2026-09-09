@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Project Python was not found: .venv\Scripts\python.exe
    exit /b 1
)
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m scripts.deploy_release --resume %*
set "stack_exit_code=%errorlevel%"
if "%stack_exit_code%"=="0" if exist "%~dp0.local-history\actions-runner\.runner" call "%~dp0runner.cmd"
popd
exit /b %stack_exit_code%
