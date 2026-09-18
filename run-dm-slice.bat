@echo off
REM dm.pdf pages 183-302, end to end, in one double-click.
REM
REM This is the third book, and it is the actual test of the goal: capture,
REM translate, render and grade with nobody watching and nothing to fix in
REM between. The first two books each needed code changes partway through. If
REM this one finishes on its own, that is the evidence; if it does not, what it
REM stops on is the next thing to fix.
REM
REM   1. capture   - ten pages per process, retries a chunk that crashed
REM   2. translate - DeepSeek, capped by budget_usd in the config
REM   3. render    - ten pages per process, retries a chunk that crashed
REM   4. grade     - decides whether the result can be delivered, and says why
REM
REM Safe to run again: every stage checkpoints on disk, so a re-run resumes
REM rather than re-capturing, re-paying or re-rendering.
REM
REM Roughly 3-6 hours in total. You can leave it alone. Everything it writes
REM goes to D:, and a full log lands in tmp\dm-slice.log.

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
set "LOG=tmp\dm-slice.log"
set "CAP=output/pdf/dm-slice-r1"
if not exist "%TMP%" mkdir "%TMP%"

echo.
echo === dm.pdf pages 183-302: capture, translate, render, grade ===
echo Started: %DATE% %TIME%
echo Log: %LOG%
echo.

echo [1/4] Capturing 120 pages...
uv run python scripts/capture_val_full.py ^
  --source pdf/dm.pdf ^
  --from-page 183 --to-page 302 ^
  --chunk-size 10 --auto ^
  --capture-root tmp/dm-capture-scratch ^
  --output %CAP% >> "%LOG%" 2>&1
if errorlevel 1 (
  echo     [!] Capture did not finish. Re-run this file to resume.
  powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 20"
  pause
  exit /b 1
)
echo     done.

echo [2/4] Reconciling what the capture layer delivered...
uv run pdftrans capture-coverage ^
  --source pdf/dm.pdf ^
  --capture-map %CAP%/capture-map.jsonl ^
  --output %CAP%/capture-coverage.json >> "%LOG%" 2>&1
echo     done.

echo [3/4] Translating with DeepSeek, then rendering 120 pages...
uv run python scripts/translate_val_usable_sample.py ^
  --capture %CAP% ^
  --source pdf/dm.pdf ^
  --config configs/dm-slice.deepseek.toml ^
  --capture-root tmp/dm-capture-scratch ^
  --sample-name dm-slice ^
  --style-note-file configs/dm-slice.style.txt ^
  --style-version dm-slice-v1 ^
  --execute-paid >> "%LOG%" 2>&1

REM A non-zero exit here is expected when any unit needs review: the translate
REM stage ends with a single-process render it may not survive, and stops before
REM it when the quality gate holds a unit back. The translations are in the run
REM store either way, so the chunked renderer below picks them up. If translation
REM itself failed there is nothing to pick up, pages stay missing, and stage 4
REM says so - no stage can quietly deliver a book with holes in it.
echo     translate exit code: %ERRORLEVEL%

uv run python scripts/render_val_full.py --auto ^
  --capture %CAP% ^
  --source pdf/dm.pdf ^
  --config configs/dm-slice.deepseek.toml ^
  --output-prefix dm-slice >> "%LOG%" 2>&1
echo     render exit code: %ERRORLEVEL%

echo [4/4] Grading the result...
uv run pdftrans acceptance-report ^
  --source pdf/dm.pdf ^
  --render-dir %CAP% ^
  --run-id dm-slice-r1 ^
  --output-prefix dm-slice >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%

echo.
echo Finished: %DATE% %TIME%
echo.
echo === acceptance verdict ===
powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 40"
echo.
if %RC%==0 (
  echo Deliverable. Tell Claude it finished for the full report.
) else (
  echo Not deliverable, or a stage failed. Send Claude %LOG%
)
echo.
pause
