@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: AXELR ONE-CLICK RECORD v1.1 — run from repo root
:: No fake API to trigger Game Bar (there isn't one). Manual Win+Alt+R during countdown.

echo.
echo ██████╗ ███████╗██╗     ██╗      █████╗ ███████╗
echo ██╔══██╗██╔════╝██║     ██║     ██╔══██╗██╔════╝
echo ██████╔╝█████╗  ██║     ██║     ███████║███████╗
echo ██╔══██╗██╔══╝  ██║     ██║     ██╔══██║╚════██║
echo ██║  ██║███████╗███████╗███████╗██║  ██║███████║
echo ╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝
echo.
echo           Live Production Demo Recorder
echo.

:: Step 1: Validate dependencies
echo [1/5] Validating system dependencies...

where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ Node.js not found. Install from https://nodejs.org/  (Windows installer)
    pause & exit /b 1
)
for /f "delims=" %%i in ('node --version') do set node_ver=%%i
echo   ✓ Node.js %node_ver%

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ Python not found. Install from https://www.python.org/downloads/  (check "Add to PATH")
    pause & exit /b 1
)
for /f "delims=" %%i in ('python --version') do set py_ver=%%i
echo   ✓ %py_ver%

where npm >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ❌ npm not found. Reinstall Node.js.
    pause & exit /b 1
)

:: Step 2: Ensure ws + dotenv + ts-node exist
echo.
echo [2/5] Checking Node.js dependencies…
if not exist "node_modules\ws" (
    echo   ⚠ ws not found, installing ws dotenv ts-node typescript…
    call npm install ws dotenv --no-save
    call npm install -D ts-node typescript --no-save
    if %ERRORLEVEL% neq 0 (
        echo ❌ npm install failed. Fix network access, then run:
        echo     npm install ws dotenv --no-save
        pause & exit /b 1
    )
    echo   ✓ Dependencies installed
) else (
    echo   ✓ Dependencies present
)

:: Step 3: Instructions
echo.
echo [3/5] Recording instructions
echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║  HOW TO RECORD                                              ║
echo ╠══════════════════════════════════════════════════════════════╣
echo ║  1. Open hackathon-demo.html in Chrome/Edge BEFORE start   ║
echo ║  2. Wait for header to show CONNECTED (green)              ║
echo ║  3. During the countdown below, PRESS Win+Alt+R            ║
echo ║     (Xbox Game Bar). Windows has no scripting API for it.  ║
echo ║  4. Click START RECORDING in the dashboard                 ║
echo ║  5. Demo auto-stops recording + saves to Downloads         ║
echo ╚══════════════════════════════════════════════════════════════╝
echo.
echo Press any key to start the 8-second countdown…
pause >nul

:: Step 4: Countdown
echo.
echo [4/5] Countdown…
for /l %%i in (8,-1,1) do (
    if %%i equ 1 (
        cls
        echo.
        echo        ╔════════════════════════════════════════════╗
        echo        ║       PRESS Win+Alt+R NOW TO RECORD!       ║
        echo        ╚════════════════════════════════════════════╝
        echo.
        echo        Then click START RECORDING in the dashboard.
        echo.
    ) else (
        echo   Countdown: %%i seconds remaining…
    )
    timeout /t 1 /nobreak >nul
)

:: Step 5: Launch
echo.
echo [5/5] Starting demo-master.ts …
echo.
call npx ts-node demo-master.ts

:: Post-production guide
echo.
echo ───────────────────────────────────────────────────────────────
echo               DEMO COMPLETE — POST-PRODUCTION
echo ───────────────────────────────────────────────────────────────
echo.
echo To merge recording with AI voice-over:
echo   1. Install ffmpeg from https://ffmpeg.org/download.html
echo   2. Put raw video (video-raw.mp4) and voice (voice-raw.mp3) here
echo   3. Run:
echo.
echo      ffmpeg -i video-raw.mp4 -i voice-raw.mp3 -c:v copy -c:a aac -b:a 192k -shortest merged.mp4
echo.
echo Face-cam overlay (replace 177 with the length of your recording in seconds):
echo.
echo      ffmpeg -i merged.mp4 -i face-cam.mp4 -filter_complex "[1:v]scale=320:320:force_original_aspect_ratio=increase,crop=320:320,format=yuva420p,geq=lum='p(X,Y)':a='if(lte((X-160)^2+(Y-160)^2,160^2),255,0)'[cam];[0:v][cam]overlay=W-340:H-340:shortest=1[v]" -map "[v]" -map 0:a -c:v libx264 -crf 20 -c:a copy video-with-cam.mp4
echo.
echo Final polish (adjust fade-out start to your video length):
echo.
echo      ffmpeg -i video-with-cam.mp4 -vf "fade=t=in:st=0:d=0.5,fade=t=out:st=177:d=2" -af "loudnorm=I=-16:TP=-1.5:LRA=11" -c:v libx264 -preset slow -crf 20 -c:a aac -b:a 192k axelr-final.mp4
echo.
pause