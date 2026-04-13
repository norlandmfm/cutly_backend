@echo off
setlocal EnableDelayedExpansion

echo ============================================
echo           SAFE GIT PUSH PRO v2
echo ============================================

echo.

:: ----------------------------
:: Check Git
:: ----------------------------
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Git is not installed.
    pause
    exit /b
)

:: ----------------------------
:: Auto git init if needed
:: ----------------------------
git rev-parse --git-dir >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] No git repo found. Running git init...
    git init
)

:: ----------------------------
:: Detect branch
:: ----------------------------
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set CURRENT_BRANCH=%%b

:: :: Si on est sur un nouveau repo sans commit, on définit 'main' par défaut
:: if "%CURRENT_BRANCH%"=="" (
::     set CURRENT_BRANCH=main
::     echo [INFO] New repository detected, defaulting to branch: !CURRENT_BRANCH!
:: )
set "CURRENT_BRANCH="

for /f %%b in ('git branch --show-current 2^>nul') do (
    set "CURRENT_BRANCH=%%b"
)

if "!CURRENT_BRANCH!"=="" (
    set "CURRENT_BRANCH=main"
    echo [INFO] New repository detected, defaulting to branch: !CURRENT_BRANCH!
)

:: ----------------------------
:: Detect or Set remote
:: ----------------------------
set "REMOTE_NAME="
for /f "delims=" %%r in ('git remote') do (
    set REMOTE_NAME=%%r
    goto :remoteFound
)

:remoteFound
if "%REMOTE_NAME%"=="" (
    echo [!] No remote detected.
    set /p REPO_URL="Enter Git Remote URL (ex: https://github.com/user/repo.git): "
    if "!REPO_URL!"=="" (
        echo [ERROR] Remote URL is required.
        pause
        exit /b
    )
    git remote add origin !REPO_URL!
    set REMOTE_NAME=origin
    echo [OK] Remote 'origin' added.
) else (
    for /f "delims=" %%u in ('git remote get-url !REMOTE_NAME!') do set CURRENT_URL=%%u
    echo Current Remote: !REMOTE_NAME! (!CURRENT_URL!)
    set /p CHANGE_REMOTE="Keep this remote? (y/n, default=y): "
    if /I "!CHANGE_REMOTE!"=="n" (
        set /p REPO_URL="Enter NEW Remote URL: "
        git remote set-url !REMOTE_NAME! !REPO_URL!
        echo [OK] Remote updated.
    )
)

echo.
echo ========================================================
echo [INFO] ACTUELLEMENT SUR LA BRANCHE : !CURRENT_BRANCH!
echo ========================================================
echo.


:: ----------------------------------------------- 
:: :: ----------------------------
:: :: Commit
:: :: ----------------------------
:: set /p COMMIT_MSG=Enter commit message: 
:: if "%COMMIT_MSG%"=="" (
::     echo Commit message required.
::     pause
::    exit /b
:: )
::
:: git add .
:: git commit -m "%COMMIT_MSG%"

:: ----------------------------
:: Commit
:: ----------------------------
echo.
echo 📝 Opening Notepad for your commit message...
echo Paste your multi-line text, SAVE the file, and CLOSE Notepad to continue.

:: Define a temporary file path
set "TEMP_MSG=%TEMP%\git_commit_msg.txt"

:: Create an empty file
type nul > "%TEMP_MSG%"

:: Open Notepad and pause the script until Notepad is closed
notepad "%TEMP_MSG%"

:: Check if the file is empty (in case you closed without saving)
for %%I in ("%TEMP_MSG%") do set FILESIZE=%%~zI
if %FILESIZE% EQU 0 (
    echo ❌ Commit message required. You saved an empty file.
    del "%TEMP_MSG%"
    pause
    exit /b
)

git add .
:: Use -F to tell Git to read the commit message from our temp file
git commit -F "%TEMP_MSG%"


:: Clean up the temporary file
del "%TEMP_MSG%"


:: ----------------------------------------------- 
echo.
set /p CONFIRM_PUSH=Proceed with normal push? (y/n): 
if /I not "%CONFIRM_PUSH%"=="y" (
    echo Push cancelled.
    pause
    exit /b
)

:: git push "!REMOTE_NAME!" "!CURRENT_BRANCH!"
:: Pour push sur le bonne branche automatiquement
git push -u "!REMOTE_NAME!" "!CURRENT_BRANCH!"

if !errorlevel! neq 0 (
    echo.
    echo ============================================
    echo Push failed - likely large file detected.
    echo ============================================
    echo.

    :: Install git-filter-repo if needed
    git filter-repo --help >nul 2>&1
    if !errorlevel! neq 0 (
        echo Installing git-filter-repo...
        pip install git-filter-repo
    )

    echo.
    echo Analyzing repository for large files...
    git filter-repo --analyze >nul 2>&1

    :: ----------------------------
    :: Automatically remove files >100MB and restore remote
    :: ----------------------------
    echo Removing files larger than 100MB to fix push...
    
    :: Sauvegarde l'URL du remote actuel avant que filter-repo ne le supprime par sécurité
    for /f "delims=" %%u in ('git remote get-url !REMOTE_NAME!') do set REMOTE_URL=%%u
    
    :: Nettoie nativement et proprement tout ce qui depasse 100MB
    git filter-repo --strip-blobs-bigger-than 100M --force
    
    :: Restaure le remote supprimé par filter-repo
    git remote add !REMOTE_NAME! "!REMOTE_URL!"

    echo.
    echo Cleaning repo...
    git reflog expire --expire=now --all
    git gc --prune=now --aggressive

    echo.
    echo FORCE PUSH REQUIRED
    echo Branch : !CURRENT_BRANCH!
    echo Remote : !REMOTE_NAME!
    echo.

    set /p CONFIRM_FORCE=Type FORCE to confirm force push: 
    if "!CONFIRM_FORCE!"=="FORCE" (
        :: git push "!REMOTE_NAME!" "!CURRENT_BRANCH!" --force
        :: Pour push sur le bonne branche automatiquement
        git push -u "!REMOTE_NAME!" "!CURRENT_BRANCH!" --force
        echo Done.
    ) else (
        echo Force push cancelled.
    )
)

echo.
echo Operation complete.
pause