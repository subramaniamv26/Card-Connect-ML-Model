@echo off
rem One-time setup: Python venv + packages, frontend packages, database schema.
rem Run from anywhere:  dev\setup.cmd
setlocal
cd /d "%~dp0.."

if not exist backend\.env (
  echo backend\.env is missing. Copy backend\.env.example to backend\.env and fill it in first.
  exit /b 1
)

echo == Python virtual environment
if not exist backend\.venv ( py -3.12 -m venv backend\.venv || exit /b 1 )
backend\.venv\Scripts\python -m pip install -q -r backend\requirements-dev.txt || exit /b 1

echo == Frontend packages
pushd frontend
if not exist .env.local copy .env.example .env.local >nul
call npm ci --no-audit --no-fund || exit /b 1
popd

echo == Database schema (applies pending migrations)
pushd backend
.venv\Scripts\python -m app.migrate || exit /b 1
popd
echo Setup complete. Next: create a user with dev\create-user.cmd, then start the three processes.
