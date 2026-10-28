@echo off
rem Create or reset a dashboard/API user. Prompts for the password (not echoed).
rem Usage: dev\create-user.cmd admin ADMIN      (roles: ADMIN, ANALYST, READ_ONLY)
cd /d "%~dp0..\backend"
.venv\Scripts\python -m scripts.create_user --username %1 --role %2
