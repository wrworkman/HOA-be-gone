# SBR Collections Automation — Project State
*Last updated: April 11, 2026*

---

## What This Project Does
Automates monthly collections notices for Signal Butte Ranch HOA.
Every month the script scans all 442 active homeowner accounts in Buildium,
finds who is delinquent, posts the correct fine, and sends the correct letter.
Crystal used to do this by hand. The script does it in ~5 minutes.

---

## Files in This Folder

| File | Purpose |
|------|---------|
| `sbr_collections_automation.py` | **Main script — this is the one to run** |
| `sbr_collections_automation_backup.py` | Backup of the version before the April 11 fix |
| `buildium_lookup.py` | Helper script used during setup to find GL account IDs and test the API connection |
| `PROJECT_STATE.md` | This file |

---

## Buildium Credentials (in sbr_collections_automation.py CONFIG block)

| Key | Value |
|-----|-------|
| Client ID | `d33da506-8f83-4e5f-a808-04c3cb0842a6` |
| Client Secret | `jhDyXiXoG1NAWVfxiy1pklL2nobQkleTbexdjAwbt78=` |
| Association ID | `103158` (Signal Butte Ranch) |

Gmail app password for summary email is still a placeholder — needs to be filled in before go-live:
`"email_password": "YOUR_GMAIL_APP_PASSWORD"`

---

## GL Account IDs (confirmed April 10, 2026)

| ID | Name | Used for |
|----|------|---------|
| 51537 | Income - Collections Demand Notices | 60/90/120-day, Advanced, Pre-Legal |
| 51538 | Income - Collections Certified Notices | 150-day pre-lien, 180-day lien, certified stages |
| 51539 | Income - Collections Notices (misc) | Older charges posted before current system |
| 67944 | Income - Collections Lien Filing | 180-day lien filing |
| 8 | Income - Late Fees | Auto-applied $15/month by Buildium |
| 4 | Income - Homeowner Assessments | $62/month base assessment |

---

## Stage Progression (what the script sends at each step)

| Prior memo in Buildium | Next action | Fine | Certified? | All addresses? |
|------------------------|-------------|------|-----------|---------------|
| None (first offense) | 60-Day Collection Notice | $40 | No | No (owner only) |
| 60-Day | 90-Day Collection Notice | $40 | No | No |
| 90-Day | 120-Day Collection Notice | $40 | No | Yes |
| 120-Day | 150-Day Pre-Lien Notice | $40 | **Yes** | Yes |
| 150-Day Pre-Lien | 180-Day Lien Filing | $250 | **Yes** | Yes |
| 180-Day Lien | Advanced Delinquency Notice | $40 | No | Yes |
| Advanced Delinquency | Advanced Delinquency Notice (repeating) | $40 | No | Yes |
| Advanced (18+ months or $10k+) | Pre-Legal 60-Day Notice | $40 | **Yes** | Yes |
| Pre-Legal 60-Day | Pre-Legal Final Notice (30-Day) | $40 | **Yes** | Yes |
| Pre-Legal Final | **BOARD ALERT — turn over to attorney** | $0 | — | — |

---

## Last Dry Run Results (April 11, 2026)

- **29 accounts** processed automatically
- **8 accounts** flagged for manual review (legacy "Charge" memos — see below)
- **1 board alert** (see below)
- **2 accounts** needing certified mail queue approval in Page Per Page
- **0 accounts** skipped

To see which specific account IDs are flagged/board alerts, run the script — they now print to the log at the end.

### The 8 Manual Review Accounts (count may drop after April 11 fix)
These accounts have ONLY old "Charge" memos in their collections history — no readable stage text — so the script genuinely cannot determine their current stage. Note: violation fine charges are intentionally excluded from this check (different amounts, different escalation process). Only $40/$250 assessment-collection charges with unreadable memos trigger this flag. Crystal needs to open each account in Buildium, review the charge history, and manually post the correct next notice with a proper memo. After that, the script handles them automatically every month.

### The 1 Board Alert Account
This account already received the Pre-Legal Final Notice (30-day). The script will NOT automatically handle it — it just flags it. The board needs to decide: turn it over to the collections attorney, or give the homeowner one more chance. This is intentional — attorney referrals require a human decision.

---

## Delinquency Entry Rules (important)

- **1 late fee ($15)** = account is 30 days late → script skips it, too early for collections
- **2+ late fees** = account is 60+ days late → collections process begins (60-Day Notice)
- **Already in collections** (has prior stage memos) → process continues regardless of late fee count

This matches Buildium's behavior: it posts the $15 fee automatically each month an account is unpaid. The script should always run AFTER Buildium posts monthly late fees, not before.

## What Still Needs to Be Done Before Go-Live

1. **Fill in Gmail app password** in CONFIG: `"email_password": "YOUR_GMAIL_APP_PASSWORD"`
2. **Confirm what day Buildium posts late fees each month** — schedule the script to run the next day
3. **Manually resolve remaining flagged accounts** in Buildium — Crystal reviews each, posts the correct next notice by hand
4. **Verify 3-5 accounts manually** — spot-check in Buildium that stage calls match reality
5. **Set `dry_run: False`** in CONFIG when ready
6. **Decide on board alert account** — attorney referral or hold?

---

## How to Run

```powershell
cd "C:\Users\wrwor\OneDrive\Documents\Claude\Projects\HOA be gone"
python sbr_collections_automation.py
```

Script takes about 5 minutes to scan all 442 accounts (one API call per account).

---

## How to Revert if Something Goes Wrong

```powershell
copy /Y sbr_collections_automation_backup.py sbr_collections_automation.py
```

---

## Bigger Goal — Multi-HOA Template

The end goal is a reusable system that can be deployed for any HOA community, not just SBR.
This means:
- The script becomes the **engine** (no HOA-specific values hardcoded)
- Each HOA gets a **config file** (Association ID, GL accounts, fine amounts, assessment amount, email, stage rules)
- The system runs **in the cloud** (GitHub Actions — free, no computer needed, cron-scheduled)
- Deploying a new HOA = add a config file + set credentials as GitHub Secrets

### What needs to change for template readiness
1. Extract CONFIG block into a separate `config.json` or `.env` file per HOA
2. Make stage fine amounts and stage sequence configurable (some HOAs may not file liens, different amounts)
3. Move to GitHub Actions for hosting — each HOA = own repo or own branch + config
4. Script reads config from file at runtime, not hardcoded

**Plan:** Run SBR test first → confirm everything correct → refactor to template → set up GitHub Actions for SBR as the first deployment → use SBR as the proven template for future HOAs.

### Credential Security — Order of Operations for New HOA Deployments

**For SBR running locally from OneDrive:** hardcoding credentials in CONFIG is acceptable — single machine, no shared access.

**For any new HOA deployment (especially GitHub/cloud):**

1. **Never put passwords or API keys directly in the script.** If the script is ever pushed to GitHub, credentials in the code are exposed — even in a private repo.
2. Create a `.env` file in the project folder with all secrets:
   ```
   BUILDIUM_CLIENT_ID=xxxxx
   BUILDIUM_CLIENT_SECRET=xxxxx
   GMAIL_APP_PASSWORD=xxxxx
   ```
3. Add `.env` to `.gitignore` before the first commit — this prevents it from ever being tracked.
4. Update the script to load credentials from environment variables using `python-dotenv` (3-line change, already planned for template refactor).
5. For GitHub Actions deployments: store credentials as **GitHub Secrets** (repo Settings → Secrets and variables → Actions). The workflow file references them as `${{ secrets.GMAIL_APP_PASSWORD }}` — never in the code.
6. Each HOA gets its own set of secrets. No credentials are ever shared between HOA repos.

---

## Bug Fixed April 11, 2026
**Problem:** Accounts 22398 and 22518 (and others at Advanced stage) were routing to 60-Day Collection Notice instead of Advanced Delinquency Notice.

**Root cause:** GL account 51539 was missing from the collections GL set. Charges posted under that GL were invisible to the script, making those accounts appear to have no collections history.

**Fix applied:** Added 51539 to COLLECTIONS_GL_IDS. Also changed charge detection to scan memo text first (regardless of GL account) as the primary detection method, with GL-based detection as fallback. Tightened number-based regex matching to use word boundaries.
