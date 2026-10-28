@echo off
rem REST API on http://localhost:8000  (interactive docs: http://localhost:8000/docs)
rem --reload restarts the server automatically when you edit Python files.
cd /d "%~dp0..\backend"
.venv\Scripts\python -m uvicorn app.main:app --port 8000 --reload
