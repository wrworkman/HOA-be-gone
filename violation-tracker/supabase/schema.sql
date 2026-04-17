-- ═══════════════════════════════════════════════════════════════
-- SBR Violation Tracker — Supabase Schema
-- Run this entire script in the Supabase SQL Editor
-- (Dashboard → SQL Editor → New Query → paste → Run)
-- ═══════════════════════════════════════════════════════════════

-- 1. Violation reference counter (for generating VIO-YYYY-NNNN)
CREATE SEQUENCE IF NOT EXISTS violation_seq START 1;

-- 2. Main violations table
CREATE TABLE IF NOT EXISTS violations (
  id                  BIGSERIAL PRIMARY KEY,
  violation_ref       TEXT UNIQUE,                -- VIO-2026-0001 (auto-assigned below)
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  address             TEXT NOT NULL,
  lat                 DOUBLE PRECISION,
  lng                 DOUBLE PRECISION,
  buildium_acct_id    INTEGER,                    -- filled by cascade engine
  category_id         TEXT NOT NULL,
  violation_id        TEXT NOT NULL,
  violation_label     TEXT NOT NULL,
  stage               INTEGER DEFAULT 1,          -- 1–7, filled by cascade
  status              TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open','pending_resolution','resolved')),
  fine_amount         NUMERIC(8,2) DEFAULT 0,     -- filled by cascade
  photo_url           TEXT,                       -- Supabase Storage URL
  resolution_photo    TEXT,                       -- homeowner upload URL
  ai_verdict          TEXT
                          CHECK (ai_verdict IN ('resolved','pending','not_resolved', NULL)),
  ai_notes            TEXT,
  officer             TEXT NOT NULL DEFAULT 'Crystal',
  deadline_date       DATE,
  resolved_at         TIMESTAMPTZ,
  lob_letter_id       TEXT,
  twilio_sms_id       TEXT,
  cascade_processed   BOOLEAN NOT NULL DEFAULT FALSE,
  board_approved      BOOLEAN,                    -- NULL=N/A, FALSE=pending, TRUE=sent
  notes               TEXT
);

-- 3. Auto-generate VIO-YYYY-NNNN on insert
CREATE OR REPLACE FUNCTION assign_violation_ref()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.violation_ref IS NULL THEN
    NEW.violation_ref := 'VIO-' || to_char(NOW(), 'YYYY') || '-' ||
                         LPAD(nextval('violation_seq')::TEXT, 4, '0');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_violation_ref
BEFORE INSERT ON violations
FOR EACH ROW EXECUTE FUNCTION assign_violation_ref();

-- 4. Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_violations_status     ON violations(status);
CREATE INDEX IF NOT EXISTS idx_violations_address    ON violations(address);
CREATE INDEX IF NOT EXISTS idx_violations_cascade    ON violations(cascade_processed);
CREATE INDEX IF NOT EXISTS idx_violations_created    ON violations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_violations_cat_viol   ON violations(category_id, violation_id);

-- 5. Row Level Security — PWA (anon key) can INSERT and SELECT only
ALTER TABLE violations ENABLE ROW LEVEL SECURITY;

-- Allow the PWA (anonymous/authenticated) to INSERT new violations
CREATE POLICY "pwa_insert" ON violations
  FOR INSERT WITH CHECK (true);

-- Allow the PWA to SELECT violations (for address badge and recent list)
CREATE POLICY "pwa_select" ON violations
  FOR SELECT USING (true);

-- Allow the cascade engine (service_role key) to UPDATE
CREATE POLICY "cascade_update" ON violations
  FOR UPDATE USING (true);

-- 6. Storage bucket for violation photos
-- Run this in the Storage section OR via SQL:
INSERT INTO storage.buckets (id, name, public)
VALUES ('violation-photos', 'violation-photos', TRUE)
ON CONFLICT (id) DO NOTHING;

-- Allow public uploads (the PWA uses the anon key)
CREATE POLICY "allow_uploads" ON storage.objects
  FOR INSERT WITH CHECK (bucket_id = 'violation-photos');

-- Allow public reads (photo URLs embedded in emails/letters need to be public)
CREATE POLICY "allow_reads" ON storage.objects
  FOR SELECT USING (bucket_id = 'violation-photos');

-- 7. Helpful view: open violations with stage info
CREATE OR REPLACE VIEW v_open_violations AS
SELECT
  v.violation_ref,
  v.created_at::DATE          AS date_logged,
  v.address,
  v.violation_label,
  v.stage,
  v.status,
  v.fine_amount,
  v.deadline_date,
  v.deadline_date - CURRENT_DATE AS days_remaining,
  v.cascade_processed,
  v.board_approved,
  v.photo_url,
  v.officer
FROM violations v
WHERE v.status IN ('open', 'pending_resolution')
ORDER BY v.deadline_date ASC;

-- 8. SMS Opt-In table (consent records from the web form)
CREATE TABLE IF NOT EXISTS sms_optins (
  id           BIGSERIAL PRIMARY KEY,
  opted_in_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  name         TEXT NOT NULL,
  address      TEXT NOT NULL,
  phone        TEXT NOT NULL,           -- E.164 format: +14805551234
  consented    BOOLEAN NOT NULL DEFAULT TRUE,
  consent_text TEXT,                    -- Description of how consent was collected
  opted_out_at TIMESTAMPTZ,            -- Set when homeowner replies STOP
  source       TEXT DEFAULT 'web_form' -- web_form, manual, buildium_import
);

ALTER TABLE sms_optins ENABLE ROW LEVEL SECURITY;

-- Anyone can submit the opt-in form (anon key)
CREATE POLICY "optin_insert" ON sms_optins FOR INSERT WITH CHECK (true);

-- Only service role (cascade engine) can read
CREATE POLICY "optin_select" ON sms_optins FOR SELECT USING (true);

-- 9. Verify
SELECT 'Schema created successfully.' AS result;
