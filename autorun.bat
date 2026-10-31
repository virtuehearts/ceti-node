@echo off
REM Uncensored Hub autorun (Windows): prefer WSL if available, else plain Python.
REM In WSL you get the full Linux path (Vulkan/CUDA via the Windows driver).
where wsl >nul 2>&1
if %ERRORLEVEL%==0 (
  echo Using WSL — running autorun.py inside WSL. Open http://localhost:8900 in your Windows browser.
  wsl python3 autorun.py %*
) else (
  python autorun.py %*
)
