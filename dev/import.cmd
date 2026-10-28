@echo off
rem Import one CSV file.  Usage: dev\import.cmd <dataset> <path\to\file.csv>
rem Datasets, in load order: customers cards merchants transactions offer_targeting offer_outcomes offers eligibility_rules
cd /d "%~dp0..\backend"
.venv\Scripts\python -m scripts.import_csv --dataset %1 --file "%~f2" --user cli
