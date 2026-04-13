@echo off
cd /d "%~dp0"
uvicorn api_server:app --reload --port 8000
