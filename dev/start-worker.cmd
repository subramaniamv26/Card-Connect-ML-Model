@echo off
rem Background worker: daily training at 02:00 Asia/Colombo, "Train now" requests, CSV imports, exports.
rem Keep it running in its own terminal. Restart it after editing Python code.
cd /d "%~dp0..\backend"
.venv\Scripts\python -m app.worker
