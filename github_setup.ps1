# SBR Collections — GitHub Setup Script
# Run this once from PowerShell in the project folder

Set-Location "C:\Users\wrwor\OneDrive\Documents\Claude\Projects\HOA be gone"

# 1. Install python-dotenv (needed for .env credential loading)
pip install python-dotenv

# 2. Initialize git repo
git init
git branch -M main

# 3. Stage only safe files (credentials stay out via .gitignore)
git add sbr_collections_automation.py
git add PROJECT_STATE.md
git add .gitignore
git add buildium_lookup.py

# 4. First commit
git commit -m "Initial commit — SBR Collections Automation v2.0"

# ── STOP HERE ──────────────────────────────────────────────────────
# Before running the lines below, go to github.com and:
#   1. Click the + icon → New repository
#   2. Name it: sbr-collections
#   3. Set to Private
#   4. DO NOT check "Add a README" or any other options
#   5. Click Create repository
#   6. Copy the URL it gives you (looks like https://github.com/yourname/sbr-collections.git)
#   7. Paste it in the line below, then run these last two lines:

# git remote add origin https://github.com/YOUR_USERNAME/sbr-collections.git
# git push -u origin main
