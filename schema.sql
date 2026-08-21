-- ---------------------------------------------------------------------------
-- Family tree schema
--
-- Two structural rules drive every decision here:
--
--   1. This is a GRAPH, not a tree. Cousin intermarriage means a person can
--      reach the same ancestor by several paths. People are nodes; parentage
--      and unions are edges. Nothing nested is ever stored.
--
--   2. There are NO DATES about people. No birth, no death, no marriage year,
--      not even an approximate one. Only `created_at` / `reviewed_at` audit
--      stamps, which describe rows, not relatives. Do not add date columns.
--
-- Safe to run repeatedly: every statement is IF NOT EXISTS.
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------------------
-- branches — one per founding brother
--
-- Defined in config.FOUNDING_ANCESTORS and materialised here at seed time.
-- `founding_ancestor_id` is nullable only because branches and people
-- reference each other; the seed inserts branches first, then backfills.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS branches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    key                 TEXT    NOT NULL UNIQUE,
    display_name        TEXT    NOT NULL,
    founding_ancestor_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    admin_telegram_id   INTEGER,
    colour              TEXT,
    sort_order          INTEGER NOT NULL DEFAULT 0
);


-- ---------------------------------------------------------------------------
-- people — the nodes
--
-- `given_name` is the first name only. The full display name is NEVER stored:
-- it is computed from the father link by db.display_name(), so it cannot drift
-- out of sync when a father is later corrected. See constraint 3.
--
-- `family_name` defaults from config.FAMILY_NAME but is per-person, so a woman
-- who married in keeps her own.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS people (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    given_name             TEXT    NOT NULL,
    given_name_ar          TEXT,
    family_name            TEXT    NOT NULL,
    sex                    TEXT    CHECK (sex IN ('M', 'F')),
    father_id              INTEGER REFERENCES people(id) ON DELETE SET NULL,
    mother_id              INTEGER REFERENCES people(id) ON DELETE SET NULL,
    branch_id              INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    notes                  TEXT,
    created_by_telegram_id INTEGER,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),

    CHECK (trim(given_name) <> ''),
    CHECK (trim(family_name) <> ''),
    -- A person cannot be their own parent. Deeper ancestry cycles are not
    -- expressible as a CHECK; db.find_ancestry_cycles() catches those.
    CHECK (father_id IS NULL OR father_id <> id),
    CHECK (mother_id IS NULL OR mother_id <> id)
);

CREATE INDEX IF NOT EXISTS idx_people_father ON people(father_id);
CREATE INDEX IF NOT EXISTS idx_people_mother ON people(mother_id);
CREATE INDEX IF NOT EXISTS idx_people_branch ON people(branch_id);
CREATE INDEX IF NOT EXISTS idx_people_given  ON people(given_name);


-- ---------------------------------------------------------------------------
-- unions — the marriage edges
--
-- A separate table, rather than a spouse column, is what makes constraint 1
-- work: a cousin marriage is just one more edge joining two branches, not a
-- contradiction in a tree structure.
--
-- Unions are undirected. The unique index normalises the pair so that
-- (a, b) and (b, a) cannot both be stored.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS unions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_a_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    partner_b_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    notes        TEXT,

    CHECK (partner_a_id <> partner_b_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_unions_pair ON unions(
    MIN(partner_a_id, partner_b_id),
    MAX(partner_a_id, partner_b_id)
);

CREATE INDEX IF NOT EXISTS idx_unions_a ON unions(partner_a_id);
CREATE INDEX IF NOT EXISTS idx_unions_b ON unions(partner_b_id);


-- ---------------------------------------------------------------------------
-- submissions — the queue
--
-- Constraint 4: nothing from the bot ever writes to `people` or `unions`.
-- Every addition AND every correction lands here first and waits for an admin.
--
-- `payload_json` holds the raw submitted data verbatim, so a rejected or
-- mis-merged submission can always be re-read as it was sent.
-- `matched_person_id` is set by the fuzzy matcher as a hint only. It never
-- causes an automatic merge.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id  INTEGER NOT NULL,
    payload_json      TEXT    NOT NULL,
    matched_person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    status            TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'approved', 'merged', 'rejected')),
    reviewed_by       INTEGER,
    reviewed_at       TEXT,
    review_note       TEXT,
    -- Resulting person, once approved or merged. Lets the admin queue show
    -- what a decision actually produced, and lets a contributor's "fix
    -- something I submitted" flow find their own earlier work.
    resulting_person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status, id);
CREATE INDEX IF NOT EXISTS idx_submissions_user   ON submissions(telegram_user_id);


-- ---------------------------------------------------------------------------
-- contributors — Telegram identity
--
-- The Telegram user ID is the identity. No login, no password, no email.
-- `linked_person_id` is who this contributor is within the family, which is
-- how the bot can say "add my parents" without asking who they are again.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS contributors (
    telegram_user_id INTEGER PRIMARY KEY,
    display_label    TEXT,
    linked_person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    branch_id        INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    is_admin         INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    admin_scope      TEXT    CHECK (admin_scope IS NULL OR admin_scope IN ('branch', 'super')),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contributors_branch ON contributors(branch_id);
