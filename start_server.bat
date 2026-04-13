@echo off
setlocal EnableDelayedExpansion

echo ===================================================
echo     DEMARRAGE DU BACKEND CUTLY (WINDOWS)
echo ===================================================
echo.

set /p USER_PORT="Sur quel port lancer le serveur ? (Entree = 8000) : "
if "!USER_PORT!"=="" set USER_PORT=8000

echo.
echo [*] Etape 1 : Liberation du port !USER_PORT!...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":!USER_PORT! " ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
echo [OK] Voie libre !
echo.

:: ================ FFMPEG
echo [*] Etape 2 : Verification de ffmpeg...
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] ffmpeg non trouve. Installation via winget...
    winget install --id Gyan.FFmpeg -e --silent
    echo [OK] ffmpeg installe. Relance le script si ffmpeg n'est pas encore dans le PATH.
) else (
    echo [OK] ffmpeg detecte.
)
echo.

:: ================ VENV
echo [*] Etape 3 : Verification du venv...
if not exist "venv\" (
    echo [!] Venv absent. Creation en cours...
    python -m venv venv
    echo [*] Installation des requirements...
    venv\Scripts\python.exe -m pip install --upgrade pip -q
    venv\Scripts\python.exe -m pip install -r requirements.txt
    echo [OK] Venv cree et configure.
) else (
    echo [OK] Environnement virtuel detecte.
)
echo.

:: ================ LANCEMENT
echo [*] Lancement du serveur Python...
echo ===================================================
venv\Scripts\python.exe -m uvicorn api_server:app --host 0.0.0.0 --port !USER_PORT! --reload

echo.
echo [!] Le serveur s'est arrete.
pause
