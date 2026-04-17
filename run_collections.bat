@echo off
:: SBR Collections Automation — Monthly Runner
:: Scheduled to run on the 2nd of every month via Windows Task Scheduler.
:: Buildium posts $15 late fees on the 30th, so running on the 2nd captures
:: the previous month's delinquencies without missing February or short months.

cd /d "C:\Users\wrwor\OneDrive\Documents\Claude\Projects\HOA be gone"

echo ============================================================
echo  SBR Collections Run — %date%
echo  Running script against Buildium (dry_run setting in script)
echo ============================================================

python sbr_collections_automation.py

echo.
echo Script complete. Check sbrneighbors@gmail.com for the summary email.
pause
