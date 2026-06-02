@echo off
REM GitHub Setup Script for ops-automation-engine (Windows)
REM This script automates the GitHub repository setup

setlocal enabledelayedexpansion

echo ================================
echo GitHub Setup for ops-automation-engine
echo ================================
echo.

REM Check if git is available
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Git is not installed. Please install Git first.
    exit /b 1
)

REM Get GitHub username
set /p GITHUB_USERNAME="Enter your GitHub username: "
set /p REPO_NAME="Enter repository name (default: ops-automation-engine): "

if "!REPO_NAME!"=="" (
    set REPO_NAME=ops-automation-engine
)

set REPO_URL=https://github.com/!GITHUB_USERNAME!/!REPO_NAME!.git

echo.
echo Repository URL: !REPO_URL!
echo.

REM Check if remote already exists
git remote get-url origin >nul 2>&1
if %errorlevel% equ 0 (
    echo Note: Remote 'origin' already configured as:
    git remote get-url origin
    set /p UPDATE_REMOTE="Do you want to update it? (y/n): "
    if "!UPDATE_REMOTE!"=="y" (
        git remote remove origin
        git remote add origin !REPO_URL!
        echo Remote updated
    )
) else (
    git remote add origin !REPO_URL!
    echo Remote added
)

REM Push to main
echo.
echo Pushing code to GitHub main branch...
git push -u origin main
echo Code pushed to main branch

echo.
echo ================================
echo Next: Add Secrets to GitHub
echo ================================
echo.
echo 1. Go to: https://github.com/!GITHUB_USERNAME!/!REPO_NAME!/settings/secrets/actions
echo.
echo 2. Create secret: DOCKER_USERNAME
echo    Value: Your Docker Hub username
echo.
echo 3. Create secret: DOCKER_PASSWORD
echo    Value: Your Docker Hub Personal Access Token
echo    (Get it at: https://hub.docker.com/settings/security)
echo.
echo 4. After adding secrets, watch the build at:
echo    https://github.com/!GITHUB_USERNAME!/!REPO_NAME!/actions
echo.
