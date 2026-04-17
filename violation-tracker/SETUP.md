# SBR Violation Tracker — Setup Guide

This guide walks through every step to go live. Order matters.

---

## Step 1 — Create a Supabase Project (free)

1. Go to [supabase.com](https://supabase.com) → **Start for Free**
2. Create a new organization: `SBR HOA`
3. Create a new project: `sbr-violations` | Region: `US West` | Password: (save it!)
4. Wait ~2 minutes for project to spin up

**Get your credentials:**
- Dashboard → **Project Settings** → **API**
- Copy `Project URL` → this is your `SUPABASE_URL`
- Copy `anon public` key → this is your `SUPABASE_ANON_KEY` (used in the PWA)
- Copy `service_role secret` key → this is your `SUPABASE_SERVICE_KEY` (used in GitHub secrets ONLY — never in client code)

**Run the schema:**
- Dashboard → **SQL Editor** → **New Query**
- Open `supabase/schema.sql`, paste the entire contents, click **Run**
- You should see: `Schema created successfully.`

---

## Step 2 — Set Up Twilio (for SMS)

1. Go to [twilio.com](https://twilio.com) → Create a free account
2. Upgrade to a paid account (SMS requires a paid plan — costs ~$0.008/text)
3. Buy a phone number (≈$1/month) with SMS capability
   - Console → **Phone Numbers** → **Buy a Number** → choose Arizona area code (480)

**Save these values:**
- Account SID (starts with `AC...`)
- Auth Token
- Your Twilio phone number (e.g., `+14809990000`)

---

## Step 3 — Get a Gmail App Password

The cascade engine sends email using your Gmail via SMTP.

1. Go to [myaccount.google.com](https://myaccount.google.com)
2. Security → **2-Step Verification** (must be enabled)
3. Security → **App passwords**
4. Create new app password: name it `SBR Violations`
5. Copy the 16-character password (format: `xxxx xxxx xxxx xxxx`)

This is your `GMAIL_APP_PASSWORD`.

---

## Step 4 — Create a GitHub Repository

1. Go to [github.com](https://github.com) → **New repository**
2. Name: `sbr-violations` | Private | Initialize with README
3. Enable **GitHub Pages**: Settings → Pages → Source: **GitHub Actions**

**Push the violation-tracker folder:**
In GitHub Desktop:
- Add Existing Repository → select the `violation-tracker` folder
- Commit all files → Push to `sbr-violations`

---

## Step 5 — Add GitHub Secrets

This is how the cascade engine gets credentials without exposing them.

GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add each of these:

| Secret Name              | Value |
|--------------------------|-------|
| `SUPABASE_URL`           | Your Supabase Project URL |
| `SUPABASE_SERVICE_KEY`   | Your Supabase service_role key |
| `BUILDIUM_CLIENT_ID`     | From Buildium API settings |
| `BUILDIUM_CLIENT_SECRET` | From Buildium API settings |
| `TWILIO_ACCOUNT_SID`     | From Twilio console |
| `TWILIO_AUTH_TOKEN`      | From Twilio console |
| `TWILIO_FROM_NUMBER`     | Your Twilio number, e.g. `+18005551234` |
| `GMAIL_FROM`             | Your Gmail address |
| `GMAIL_APP_PASSWORD`     | Your 16-char app password |
| `LOB_API_KEY`            | From Lob dashboard (live key) |
| `CRYSTAL_EMAIL`          | Crystal's email address |
| `CRYSTAL_PHONE`          | Crystal's cell phone (for alerts) |
| `RESOLUTION_PORTAL_URL`  | `https://wrworkman.github.io/HOA-be-gone/violation-tracker/resolution-portal` |

---

## Step 6 — Update the PWA with Your Supabase Credentials

Open `pwa/index.html` and find the CONFIG block near the top of the `<script>` section:

```javascript
const CONFIG = {
  SUPABASE_URL:      'https://YOUR_PROJECT_ID.supabase.co',   // ← replace
  SUPABASE_ANON_KEY: 'YOUR_SUPABASE_ANON_KEY',                // ← replace
  ...
};
```

Replace both values with your Supabase Project URL and `anon public` key.

Do the same in `resolution-portal/index.html`.

Commit and push.

---

## Step 7 — Install the PWA on Crystal's Phone

1. Open Safari on Crystal's iPhone
2. Navigate to: `https://YOUR_GITHUB_USERNAME.github.io/sbr-violations/pwa/`
3. Tap the **Share** button (box with arrow)
4. Tap **Add to Home Screen**
5. Name it `SBR Violations` → **Add**

The app icon will appear on her home screen and work like a native app.

**Android:** Open Chrome → tap the three-dot menu → **Add to Home Screen**

---

## Step 8 — Test End-to-End

1. Open the PWA on Crystal's phone
2. Tap **New Violation**
3. Take a photo of something (your hand, a wall — just testing)
4. Confirm the GPS fires and address appears
5. Select a category and violation
6. Submit

**Check Supabase:**
- Dashboard → **Table Editor** → `violations`
- You should see a new row with `cascade_processed = false`

**Trigger the cascade manually:**
- GitHub repo → **Actions** → **Cascade Engine** → **Run workflow**
- Watch the logs — should show "Processing VIO-2026-0001"

**Check results:**
- Buildium: check the owner's account notes and ledger
- Crystal's phone: should receive an SMS
- Owner's email: should receive a violation notice
- Supabase: `cascade_processed` should now be `true`

---

## GitHub Actions Schedule

| Workflow         | Schedule                  | What it does |
|------------------|---------------------------|--------------|
| `cascade.yml`    | Every 30 minutes          | Processes new violations |
| `weekly_digest.yml` | Mondays at 7 AM MST    | Sends Crystal her weekly summary |

---

## Estimated Monthly Costs

| Service   | What it covers        | Cost estimate |
|-----------|-----------------------|---------------|
| Supabase  | Database + storage    | Free (under 500MB) |
| GitHub    | Hosting + Actions     | Free |
| Twilio    | SMS per violation     | ~$0.008/text |
| Gmail     | Email per violation   | Free |
| Lob       | Letter per violation  | ~$1.00 (first class) / ~$6.00 (certified) |
| Total     | Per violation (email+SMS only) | ~$0.01 |
| Total     | Per violation (with letter)    | ~$1.01–$6.01 |

---

## Folder Structure

```
violation-tracker/
├── pwa/
│   ├── index.html          ← Crystal's field tool (bookmark this on her phone)
│   ├── manifest.json       ← PWA manifest (home screen install)
│   └── sw.js               ← Service worker (offline support)
├── resolution-portal/
│   └── index.html          ← Homeowner resolution page (linked in every notice)
├── backend/
│   ├── cascade.py          ← Main engine (runs every 30 min via GitHub Actions)
│   └── weekly_digest.py    ← Monday morning email to Crystal
├── .github/workflows/
│   ├── cascade.yml         ← Cron schedule for cascade
│   └── weekly_digest.yml   ← Cron schedule for digest
├── supabase/
│   └── schema.sql          ← Run this once to set up the database
└── docs/
    ├── SYSTEM-BLUEPRINT.md
    ├── violation-categories.json
    └── fine-schedule.json
```

---

## Questions / Troubleshooting

**Cascade isn't running:**
Check GitHub Actions → the workflow should have a green checkmark. If it's failing, check the logs for the error message.

**SMS not sending:**
Verify your Twilio account is fully activated (not just trial). Trial accounts can only text verified numbers.

**Photos not uploading:**
Check Supabase Storage → `violation-photos` bucket must exist and have the `allow_uploads` policy. Re-run the schema SQL if needed.

**Address not filling in:**
GPS requires HTTPS. Make sure you're accessing the PWA via `https://` (GitHub Pages always uses HTTPS).
