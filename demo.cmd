@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\local-demo.ps1" %*
set "PATCHPROOF_DEMO_EXIT=%ERRORLEVEL%"
if not "%PATCHPROOF_DEMO_EXIT%"=="0" (
  echo.
  echo PatchProof local demo failed. Review the message above.
  pause
)
exit /b %PATCHPROOF_DEMO_EXIT%
