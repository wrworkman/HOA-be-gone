# SBR Violation Tracker — System Blueprint

## Architecture Overview

```
Crystal's Phone (PWA)
        ↓ submits violation
   Supabase Database + Photo Storage (free tier)
        ↓ new row triggers
   GitHub Actions — Cascade Engine (Python)
        ↓ simultaneously
   ┌────────────────────────────────────────────┐
   │  Buildium      Twilio SMS   Gmail   Lob    │
   │  (note+fine)   (homeowner)  (email) (mail) │
   └────────────────────────────────────────────┘
        ↓ resolution link in every notice
   Homeowner Resolution Portal (GitHub Pages)
        ↓ uploads correction photo
   Claude Vision API — validates fix
        ↓ result
   Auto-close OR Pending (Crystal weekly review)
```

## Component 1 — Crystal's PWA (Field Tool)

File: `pwa/index.html` (single file, hosted on GitHub Pages)

### Flow:
1. Crystal opens URL on phone → bookmarks it
2. Taps "New Violation"
3. Camera opens → photo taken
4. GPS fires automatically → lat/lng captured
5. Reverse geocode → address pre-filled (OpenStreetMap Nominatim, free)
6. Claude Vision analyzes photo → pre-selects likely violation from dropdown
7. Crystal confirms/adjusts category and specific violation
8. Optional notes field
9. Submit → photo + data POST to Supabase
10. Confirmation screen with violation ID

### Smart features:
- Trash can blackout warning (if today is Mon/Thu or day before/after, shows banner)
- If address has open violations, shows badge: "⚠️ 2 active violations at this address"
- GPS coordinates + timestamp locked at moment of photo — tamper-proof record
- Works offline: queues submissions if no signal, syncs when back online

---

## Component 2 — Cascade Engine (Python / GitHub Actions)

File: `backend/cascade.py`

Runs: Every 30 minutes via GitHub Actions cron (or triggered by Supabase webhook)

### For each new unprocessed violation:

1. **Buildium lookup** — find owner account by address
2. **History check** — query Supabase for prior violations at this address
   with this violation type → determine stage (1st, 2nd, 3rd... observation)
3. **20-day rule** — if last notice for this violation was < 20 days ago, skip
4. **Fine calculation** — from fine-schedule.json
5. **Buildium actions:**
   - POST note with photo URL, GPS coords, timestamp, stage, officer name
   - POST fine to ledger (stage 3+)
   - Update violation status
6. **Twilio SMS** — brief notice to homeowner mobile on file
7. **Gmail email** — full notice with violation photo, stage info, resolution link
8. **Lob letter** — physical mail (stage 2+, certified at stage 6)
9. **Mark processed** in Supabase

### Board approval gate (stages 6-7):
- Does NOT auto-send
- Sends Crystal an alert email with approve/deny link
- Only sends when Crystal approves

---

## Component 3 — Homeowner Resolution Portal

File: `resolution-portal/index.html` (GitHub Pages)

URL format: `https://sbrhoa.github.io/violations/resolve?id=VIO-2026-0042`

### Flow:
1. Homeowner taps link from SMS/email
2. Page loads: shows violation photo, type, date, stage, fine amount
3. "Upload Your Fix" button → mobile camera or file picker
4. Submit correction photo
5. Claude Vision compares violation photo vs resolution photo:
   - **Resolved**: "✅ Looks great! We've marked this resolved. No further action needed."
   - **Uncertain**: "⏳ Thanks for submitting. Crystal will review within 2 business days."
   - **Not resolved**: "❗ It looks like [specific issue] may still need attention. Please re-submit when complete."
6. Status set to Resolved or Pending accordingly

### Always shows:
- Where homeowner is in the violation stage process
- What the next fine would be if unresolved
- Inspection schedule notice ("Our next neighborhood inspection is approximately [date range]")

---

## Component 4 — Reporting

### Weekly (Monday morning email to Crystal):
- Pending resolutions awaiting manual review (before/after photos)
- Violations approaching deadline (< 5 days remaining)
- New violations since last week

### Monthly (board report — Word + PDF):
- Total violations issued by category (bar chart)
- Active open violations with photos
- Resolved violations with before/after photos
- Fine revenue collected
- Repeat offender summary
- Month-over-month trend

### Monthly resolved report (separate):
- Every violation closed this month
- Violation photo + resolution photo side by side
- Closed date and days to resolve

---

## Component 5 — Top Yards Program

File: `resolution-portal/top-yards.html`

- Homeowners submit their own yard photo via simple form
- Monthly voting (one vote per Buildium account)
- Winner announced in monthly email blast
- Hall of fame page showing past winners
- Optional: winner gets a small credit or recognition in newsletter

---

## Data Model (Supabase)

### violations table
| field              | type      | notes                                    |
|--------------------|-----------|------------------------------------------|
| id                 | uuid      | primary key                              |
| violation_ref      | text      | VIO-YYYY-NNNN (human readable)           |
| created_at         | timestamp | auto                                     |
| address            | text      | from reverse geocode                     |
| lat                | float     | GPS latitude                             |
| lng                | float     | GPS longitude                            |
| buildium_acct_id   | int       | looked up by cascade engine              |
| category_id        | text      | from violation-categories.json           |
| violation_id       | text      | specific violation within category       |
| violation_label    | text      | human readable                           |
| stage              | int       | 1-7                                      |
| status             | text      | open / pending_resolution / resolved     |
| fine_amount        | float     | 0 at stage 1-2                           |
| photo_url          | text      | Supabase storage URL                     |
| resolution_photo   | text      | homeowner uploaded photo URL             |
| ai_verdict         | text      | resolved / pending / not_resolved        |
| ai_notes           | text      | Claude's explanation                     |
| officer            | text      | Crystal (or future multi-officer)        |
| deadline_date      | date      | created_at + 20 days                     |
| resolved_at        | timestamp | when closed                              |
| lob_letter_id      | text      | for tracking                             |
| twilio_sms_id      | text      | for tracking                             |
| cascade_processed  | bool      | false until cascade runs                 |
| board_approved     | bool      | null = N/A, false = pending, true = sent |
| notes              | text      | Crystal's optional field notes           |

---

## Fine Schedule Summary

| Stage | Label                    | Fine  | Letter    | Certified |
|-------|--------------------------|-------|-----------|-----------|
| 1     | 1st Observation          | $0    | Email only| No        |
| 2     | 2nd Observation          | $0    | Yes       | No        |
| 3     | 3rd Observation          | $50   | Yes       | No        |
| 4     | 4th Observation          | $100  | Yes       | No        |
| 5     | 5th Observation          | $150  | Yes       | No        |
| 6     | Right to Cure            | $55   | Yes       | Yes ✉️    |
| 7     | HOA Cures at Owner Exp.  | $200+ | Yes       | Yes ✉️    |

*Stages 6-7 require board approval before sending*
*Minimum 20 days between all notices*
*No trash can violations day before/after Monday (black) or Thursday (blue) pickup*

---

## Tech Stack

| Component            | Tool                  | Cost         |
|----------------------|-----------------------|--------------|
| PWA hosting          | GitHub Pages          | Free         |
| Photo + data storage | Supabase              | Free tier    |
| Cascade engine       | GitHub Actions        | Free         |
| SMS                  | Twilio                | ~$0.008/text |
| Email                | Gmail SMTP            | Free         |
| Physical mail        | Lob.com               | ~$1 / $6     |
| AI photo analysis    | Claude Vision API     | ~$0.01/photo |
| Geocoding            | OpenStreetMap Nominatim| Free        |

Estimated cost per violation: **~$1.10 first notice (email+SMS only), ~$2.10 with letter**

---

## Build Order

1. violation-categories.json + fine-schedule.json ✅ done
2. Supabase setup (schema + storage bucket)
3. PWA — camera, GPS, address fill, dropdown, submit
4. Cascade engine — Buildium lookup, stage logic, notifications
5. Resolution portal — homeowner upload + Claude Vision check
6. Weekly Crystal digest
7. Monthly board reports
8. Top Yards program
