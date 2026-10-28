@echo off
rem Unit tests (no database needed).
cd /d "%~dp0..\backend"
.venv\Scripts\python -m pytest -q tests --ignore=tests/integration
