@echo off
REM Capture optimization.pdf pages 150-269 for the end-to-end generality test.
REM
REM Just double-click this. It drives all 12 chunks itself, gives each one its
REM own process, retries a chunk that crashes, and stops when it is done.
REM Nothing to watch and nothing to run after it.
REM
REM Safe to run again: a finished chunk is skipped, so if this is interrupted
REM - closed window, reboot, out of memory - re-running picks up where it left
REM off rather than starting over.
REM
REM Everything it writes goes to D:, and a full log lands in
REM tmp\capture-opt-slice.log.

cd /d D:\4_Projects\PDFtranslator
if errorlevel 1 (
  echo [!] Cannot enter D:\4_Projects\PDFtranslator
  pause
  exit /b 1
)

set "TMP=D:\4_Projects\PDFtranslator\tmp\systmp"
set "TEMP=D:\4_Projects\PDFtranslator\tmp\systmp"
set "TMPDIR=D:\4_Projects\PDFtranslator\tmp\systmp"
set "PYTHONIOENCODING=utf-8"
if not exist "%TMP%" mkdir "%TMP%"

echo.
echo === Capturing optimization.pdf pages 150-269 ===
echo Started: %DATE% %TIME%
echo Log: tmp\capture-opt-slice.log
echo.
echo This takes roughly 1-2 hours. You can leave it alone.
echo.

uv run python scripts/capture_val_full.py ^
  --source pdf/optimization.pdf ^
  --from-page 150 --to-page 269 ^
  --chunk-size 10 --auto ^
  --capture-root tmp/opt-capture-scratch ^
  --output output/pdf/opt-slice-r1 > tmp\capture-opt-slice.log 2>&1

set RC=%ERRORLEVEL%
echo.
echo Finished: %DATE% %TIME%   exit code: %RC%
echo.
echo === last lines of the log ===
powershell -NoProfile -Command "Get-Content 'tmp\capture-opt-slice.log' -Tail 15"
echo.
if %RC%==0 (
  echo DONE. Tell Claude it finished and it will take over from here.
) else (
  echo Did not finish. Re-running this file resumes from the last good chunk.
  echo If it keeps failing, send Claude tmp\capture-opt-slice.log
)
echo.
pause
