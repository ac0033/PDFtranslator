@echo off
REM Translate, render and grade optimization.pdf pages 150-269.
REM
REM Run this after run-capture-opt-slice.bat has finished.
REM
REM Just double-click it. Three stages, nothing to watch, nothing to run after:
REM   1. translate  - DeepSeek, capped by budget_usd in the config
REM   2. render     - one process per 10 pages, retries a chunk that crashed
REM   3. acceptance - decides whether the result can be delivered, and says why
REM
REM Safe to run again: translated units and rendered pages are both checkpoints,
REM so a re-run resumes rather than re-paying or re-rendering.
REM
REM Everything it writes goes to D:, and a full log lands in
REM tmp\deliver-opt-slice.log.

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
set "LOG=tmp\deliver-opt-slice.log"
if not exist "%TMP%" mkdir "%TMP%"

echo.
echo === optimization.pdf pages 150-269: translate, render, grade ===
echo Started: %DATE% %TIME%
echo Log: %LOG%
echo.
echo Roughly 2-4 hours in total. You can leave it alone.
echo.

echo [1/3] Translating with DeepSeek...
uv run python scripts/translate_val_usable_sample.py ^
  --capture output/pdf/opt-slice-r1 ^
  --source pdf/optimization.pdf ^
  --config configs/opt-slice.deepseek.toml ^
  --capture-root tmp/opt-capture-scratch ^
  --sample-name opt-slice ^
  --style-note-file configs/opt-slice.style.txt ^
  --style-version opt-slice-v1 ^
  --execute-paid >> "%LOG%" 2>&1

REM A non-zero exit here is usually the single-process render at the end of the
REM translate stage running out of memory. The translations are already in the
REM run store, so stage 2 picks them up; if translation itself failed, stage 2
REM cannot find results, pages stay missing, and stage 3 says so.
echo     exit code: %ERRORLEVEL%

echo [2/3] Rendering 120 pages, ten per process...
uv run python scripts/render_val_full.py --auto ^
  --capture output/pdf/opt-slice-r1 ^
  --source pdf/optimization.pdf ^
  --config configs/opt-slice.deepseek.toml ^
  --output-prefix opt-slice >> "%LOG%" 2>&1
echo     exit code: %ERRORLEVEL%

echo [3/3] Grading the result...
uv run pdftrans acceptance-report ^
  --source pdf/optimization.pdf ^
  --render-dir output/pdf/opt-slice-r1 ^
  --run-id opt-slice-r1 ^
  --output-prefix opt-slice >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

echo.
echo Finished: %DATE% %TIME%
echo.
echo === acceptance verdict ===
powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 40"
echo.
if %RC%==0 (
  echo Gradeable and not blocked. Tell Claude it finished for the full report.
) else (
  echo Not deliverable, or a stage failed. Send Claude %LOG%
)
echo.
pause
