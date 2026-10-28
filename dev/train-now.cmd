@echo off
rem Run one full training + recommendation run in this terminal (no worker needed). Takes ~2-3 minutes.
cd /d "%~dp0..\backend"
.venv\Scripts\python -m scripts.train_once
