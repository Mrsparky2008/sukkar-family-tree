"""
Data access layer.

Everything that touches SQLite goes through this module. The bot, the admin
interface, and the public view all import from here and never open their own
connection or write their own SQL.

Two rules are enforced in this file, because they are the two that silently rot
if they are enforced by convention alone:

  * A person's full name is COMPUTED, never stored. `display_name()` below is
    the single implementation. Nothing anywhere else may concatenate names.

  * Writes to `people` and `unions` are privileged. They are reachable only
    from the seed script and from an admin approving a submission. The bot
    calls `add_submission()` and nothing else.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

import config

SCHEMA_PATH = config.BASE_DIR / "schema.sql"

#: Used when a caller does not pass an explicit threshold.
FUZZY_FALLBACK = config.FUZZY_MATCH_THRESHOLD


def submission_person_label(entry: dict[str, Any]) -> str:
    """A person inside a pending payload, as text.

    Local rather than imported from `submissions`, because that module reads
    payloads and this one stores them; importing upward would make the two
    circular.
    """
    name = entry.get("given_name", "?")
    if entry.get("family_name"):
        name = f"{name} {entry['family_name']}"
    return name


# ===========================================================================
# Connection
# ===========================================================================


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with foreign keys on and dict-like rows.

    Pass ``":memory:"`` for a throwaway database (used by the tests).
    """
    target = path if path is not None else config.DATABASE_PATH
    if target != ":memory:":
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = str(target)

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    # Off by default in SQLite, and everything in this schema depends on it.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create any missing tables and indexes. Safe to call on an existing file."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # executescript ends the implicit transaction; re-assert the pragma.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception.

    An approval writes a person, a union, and a submission update together;
    a half-applied approval would be worse than a failed one.
    """
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


# ===========================================================================
# THE display-name rule
# ===========================================================================


def display_name(
    given_name: str,
    family_name: str,
    father_given_name: str | None = None,
) -> str:
    """Build a person's full name. This is the only place it is ever built.

    The order is: given name, father's given name, family name.

        >>> display_name("Steven", "FAMILYNAME", "Khalil")
        'Steven Khalil FAMILYNAME'

    With no known father it degrades to given plus family name:

        >>> display_name("Mariam", "FAMILYNAME")
        'Mariam FAMILYNAME'

    Arabic naming convention names the eldest son after the grandfather, so a
    branch fills up with men sharing a given name. Inserting the father's name
    is how the family tells them apart: two men both called Khalil, one the son
    of Youssef and one the son of Antoun, come out as "Khalil Youssef ..." and
    "Khalil Antoun ..." with no extra bookkeeping.

    The result is never written to the database. It is regenerated on every
    read, so correcting a father link corrects every name derived from it.

    (FAMILYNAME above is a placeholder. The real default lives in config.py,
    which is the only file that may name a family.)
    """
    parts = (given_name, father_given_name, family_name)
    return " ".join(part.strip() for part in parts if part and part.strip())


#: Selects a person along with the one extra field `display_name` needs, so
#: rendering a list of people costs one query rather than one query per person.
PERSON_SELECT = """
    SELECT p.*,
           father.given_name AS father_given_name,
           mother.given_name AS mother_given_name,
           b.key             AS branch_key,
           b.display_name    AS branch_display_name,
           b.colour          AS branch_colour
      FROM people p
 LEFT JOIN people   father ON father.id = p.father_id
 LEFT JOIN people   mother ON mother.id = p.mother_id
 LEFT JOIN branches b      ON b.id      = p.branch_id
"""


def row_display_name(row: sqlite3.Row) -> str:
    """Adapter: apply `display_name` to a row selected via `PERSON_SELECT`."""
    return display_name(
        row["given_name"],
        row["family_name"],
        _optional(row, "father_given_name"),
    )


def _optional(row: sqlite3.Row, column: str) -> Any:
    """Read a column that may not be present in this particular query."""
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


# ===========================================================================
# People — reads
# ===========================================================================


def get_person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row | None:
    return conn.execute(
        PERSON_SELECT + " WHERE p.id = ?", (person_id,)
    ).fetchone()


def get_people(
    conn: sqlite3.Connection, branch_id: int | None = None
) -> list[sqlite3.Row]:
    if branch_id is None:
        return conn.execute(
            PERSON_SELECT + " ORDER BY p.family_name, p.given_name, p.id"
        ).fetchall()
    return conn.execute(
        PERSON_SELECT + " WHERE p.branch_id = ? ORDER BY p.given_name, p.id",
        (branch_id,),
    ).fetchall()


def get_parents(conn: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        PERSON_SELECT
        + """
        WHERE p.id IN (
            SELECT father_id FROM people WHERE id = ?1 AND father_id IS NOT NULL
            UNION
            SELECT mother_id FROM people WHERE id = ?1 AND mother_id IS NOT NULL
        )
        """,
        (person_id,),
    ).fetchall()


def get_children(conn: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        PERSON_SELECT
        + " WHERE p.father_id = ?1 OR p.mother_id = ?1"
        + " ORDER BY p.id",
        (person_id,),
    ).fetchall()


def get_partners(conn: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    """Everyone this person is in a union with, in either direction."""
    return conn.execute(
        PERSON_SELECT
        + """
        WHERE p.id IN (
            SELECT partner_b_id FROM unions WHERE partner_a_id = ?1
            UNION
            SELECT partner_a_id FROM unions WHERE partner_b_id = ?1
        )
        ORDER BY p.id
        """,
        (person_id,),
    ).fetchall()


def get_siblings(
    conn: sqlite3.Connection, person_id: int, full_only: bool = False
) -> list[sqlite3.Row]:
    """Anyone sharing at least one parent (or both, when `full_only`)."""
    person = conn.execute(
        "SELECT father_id, mother_id FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None:
        return []

    father_id, mother_id = person["father_id"], person["mother_id"]
    if father_id is None and mother_id is None:
        return []

    if full_only:
        if father_id is None or mother_id is None:
            return []
        clause = "p.father_id = ? AND p.mother_id = ?"
        params: tuple[Any, ...] = (father_id, mother_id, person_id)
    else:
        clauses, params_list = [], []
        if father_id is not None:
            clauses.append("p.father_id = ?")
            params_list.append(father_id)
        if mother_id is not None:
            clauses.append("p.mother_id = ?")
            params_list.append(mother_id)
        clause = " OR ".join(clauses)
        params = (*params_list, person_id)

    return conn.execute(
        PERSON_SELECT + f" WHERE ({clause}) AND p.id <> ? ORDER BY p.id", params
    ).fetchall()


def get_ancestors(conn: sqlite3.Connection, person_id: int) -> set[int]:
    """All ancestor ids, following both parents.

    Iterative with a visited set, because intermarriage means the same
    ancestor is reached by several paths and a naive walk would revisit them.
    """
    seen: set[int] = set()
    frontier = [person_id]
    while frontier:
        current = frontier.pop()
        row = conn.execute(
            "SELECT father_id, mother_id FROM people WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            continue
        for parent_id in (row["father_id"], row["mother_id"]):
            if parent_id is not None and parent_id not in seen:
                seen.add(parent_id)
                frontier.append(parent_id)
    return seen


def count_people(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]


# ===========================================================================
# People — privileged writes
# ===========================================================================
#
# Constraint 4: these are callable from the seed script and from an admin
# approving a submission. THE BOT MUST NEVER CALL THEM. If you find yourself
# importing one of these into bot/, the answer is add_submission().
# ===========================================================================


def create_person(
    conn: sqlite3.Connection,
    given_name: str,
    *,
    family_name: str | None = None,
    given_name_ar: str | None = None,
    sex: str | None = None,
    father_id: int | None = None,
    mother_id: int | None = None,
    branch_id: int | None = None,
    notes: str | None = None,
    created_by_telegram_id: int | None = None,
) -> int:
    """Insert a person and return their id. Privileged — see the note above."""
    cursor = conn.execute(
        """
        INSERT INTO people (given_name, given_name_ar, family_name, sex,
                            father_id, mother_id, branch_id, notes,
                            created_by_telegram_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            given_name.strip(),
            given_name_ar,
            (family_name or config.FAMILY_NAME).strip(),
            sex,
            father_id,
            mother_id,
            branch_id,
            notes,
            created_by_telegram_id,
        ),
    )
    return int(cursor.lastrowid)


def update_person(conn: sqlite3.Connection, person_id: int, **fields: Any) -> None:
    """Update named columns on a person. Privileged — see the note above."""
    allowed = {
        "given_name",
        "given_name_ar",
        "family_name",
        "sex",
        "father_id",
        "mother_id",
        "branch_id",
        "notes",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"not updatable: {', '.join(sorted(unknown))}")
    if not fields:
        return

    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE people SET {assignments} WHERE id = ?",
        (*fields.values(), person_id),
    )


def create_union(
    conn: sqlite3.Connection,
    partner_a_id: int,
    partner_b_id: int,
    notes: str | None = None,
) -> int:
    """Record a marriage. Returns the union id, existing or new.

    Unions are undirected, so recording the same couple in the opposite order
    is a no-op rather than a duplicate.
    """
    existing = conn.execute(
        """
        SELECT id FROM unions
         WHERE (partner_a_id = ?1 AND partner_b_id = ?2)
            OR (partner_a_id = ?2 AND partner_b_id = ?1)
        """,
        (partner_a_id, partner_b_id),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])

    cursor = conn.execute(
        "INSERT INTO unions (partner_a_id, partner_b_id, notes) VALUES (?, ?, ?)",
        (partner_a_id, partner_b_id, notes),
    )
    return int(cursor.lastrowid)


def get_unions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM unions ORDER BY id").fetchall()


# ===========================================================================
# Branches
# ===========================================================================


def sync_branches(conn: sqlite3.Connection) -> None:
    """Materialise config.FOUNDING_ANCESTORS into the `branches` table.

    Keyed on `key`, so editing a display name, colour, or branch admin in
    config and re-running updates the row instead of creating a second one.
    """
    for position, entry in enumerate(config.FOUNDING_ANCESTORS):
        colour = entry.get("colour") or config.BRANCH_PALETTE[
            position % len(config.BRANCH_PALETTE)
        ]
        conn.execute(
            """
            INSERT INTO branches (key, display_name, admin_telegram_id, colour, sort_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                display_name      = excluded.display_name,
                admin_telegram_id = excluded.admin_telegram_id,
                colour            = excluded.colour,
                sort_order        = excluded.sort_order
            """,
            (
                entry["key"],
                entry["display_name"],
                entry.get("admin_telegram_id"),
                colour,
                position,
            ),
        )


def get_branches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM branches ORDER BY sort_order, id"
    ).fetchall()


def get_branch_by_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM branches WHERE key = ?", (key,)).fetchone()


def set_branch_founder(
    conn: sqlite3.Connection, branch_key: str, person_id: int
) -> None:
    conn.execute(
        "UPDATE branches SET founding_ancestor_id = ? WHERE key = ?",
        (person_id, branch_key),
    )


def assign_branches(conn: sqlite3.Connection) -> int:
    """Give every person a branch, and return how many were changed.

    Two passes, in order of confidence:

      1. Patriline. Walk father links upward; the first founding ancestor
         reached decides the branch. This covers everyone born into the family.
      2. Marriage. Anyone still unassigned takes the branch of a partner who
         has one. This covers women who married in, so they appear when the
         public view is filtered to their husband's branch.

    Anyone reachable by neither is left NULL rather than guessed at.
    """
    founders = {
        row["founding_ancestor_id"]: row["id"]
        for row in conn.execute(
            "SELECT id, founding_ancestor_id FROM branches"
            " WHERE founding_ancestor_id IS NOT NULL"
        )
    }
    people = {
        row["id"]: (row["father_id"], row["branch_id"])
        for row in conn.execute("SELECT id, father_id, branch_id FROM people")
    }

    resolved: dict[int, int | None] = {}

    def patriline_branch(person_id: int) -> int | None:
        """Walk up the father chain to a founder, memoising as we go."""
        chain: list[int] = []
        current: int | None = person_id
        found: int | None = None

        while current is not None:
            if current in resolved:
                found = resolved[current]
                break
            if current in founders:
                found = founders[current]
                break
            if current in chain:  # cyclic data — stop rather than spin
                break
            chain.append(current)
            entry = people.get(current)
            current = entry[0] if entry else None

        for member in chain:
            resolved[member] = found
        return found

    updates: dict[int, int] = {}
    for person_id, (_father_id, current_branch) in people.items():
        branch_id = patriline_branch(person_id)
        if branch_id is not None and branch_id != current_branch:
            updates[person_id] = branch_id

    # Pass 2: fall back to a partner's branch.
    settled = {
        person_id: updates.get(person_id, people[person_id][1])
        for person_id in people
    }
    for union in conn.execute("SELECT partner_a_id, partner_b_id FROM unions"):
        a, b = union["partner_a_id"], union["partner_b_id"]
        for this, other in ((a, b), (b, a)):
            if settled.get(this) is None and settled.get(other) is not None:
                settled[this] = settled[other]
                updates[this] = settled[other]

    for person_id, branch_id in updates.items():
        conn.execute(
            "UPDATE people SET branch_id = ? WHERE id = ?", (branch_id, person_id)
        )
    return len(updates)


# ===========================================================================
# Submissions — the queue
# ===========================================================================
#
# This is the bot's only write path.
# ===========================================================================


def add_submission(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    payload: dict[str, Any],
    matched_person_id: int | None = None,
) -> int:
    """Queue a submission for review. Returns the submission id."""
    cursor = conn.execute(
        """
        INSERT INTO submissions (telegram_user_id, payload_json, matched_person_id)
        VALUES (?, ?, ?)
        """,
        (
            telegram_user_id,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            matched_person_id,
        ),
    )
    return int(cursor.lastrowid)


def get_submission(
    conn: sqlite3.Connection, submission_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM submissions WHERE id = ?", (submission_id,)
    ).fetchone()


def list_submissions(
    conn: sqlite3.Connection,
    status: str | None = "pending",
    branch_id: int | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Queue contents, oldest first.

    `branch_id` scopes to submissions from contributors in that branch, which
    is how a branch admin sees only the relatives they actually know.
    """
    clauses, params = [], []
    if status is not None:
        clauses.append("s.status = ?")
        params.append(status)
    if branch_id is not None:
        clauses.append("c.branch_id = ?")
        params.append(branch_id)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT s.*, c.branch_id AS submitter_branch_id
          FROM submissions s
     LEFT JOIN contributors c ON c.telegram_user_id = s.telegram_user_id
        {where}
      ORDER BY s.id
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_submissions_by_user(
    conn: sqlite3.Connection, telegram_user_id: int, limit: int = 10
) -> list[sqlite3.Row]:
    """A contributor's own submissions, newest first.

    Feeds the bot's "fix something I submitted" flow, which is why it returns
    every status: a relative may well want to correct something already
    approved, and that correction goes back through the queue like anything
    else.
    """
    return conn.execute(
        "SELECT * FROM submissions WHERE telegram_user_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (telegram_user_id, limit),
    ).fetchall()


def submission_payload(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["payload_json"])


def resolve_submission(
    conn: sqlite3.Connection,
    submission_id: int,
    status: str,
    reviewed_by: int,
    resulting_person_id: int | None = None,
    review_note: str | None = None,
) -> None:
    """Record an admin's decision. Never called automatically."""
    if status not in {"approved", "merged", "rejected"}:
        raise ValueError(f"not a decision: {status!r}")
    conn.execute(
        """
        UPDATE submissions
           SET status = ?, reviewed_by = ?, reviewed_at = datetime('now'),
               resulting_person_id = ?, review_note = ?
         WHERE id = ?
        """,
        (status, reviewed_by, resulting_person_id, review_note, submission_id),
    )


# ===========================================================================
# Contributors
# ===========================================================================


def get_contributor(
    conn: sqlite3.Connection, telegram_user_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contributors WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()


def upsert_contributor(
    conn: sqlite3.Connection,
    telegram_user_id: int,
    *,
    display_label: str | None = None,
    linked_person_id: int | None = None,
    branch_id: int | None = None,
    is_admin: bool | None = None,
    admin_scope: str | None = None,
) -> None:
    """Create or update a contributor, leaving omitted fields untouched."""
    conn.execute(
        "INSERT OR IGNORE INTO contributors (telegram_user_id) VALUES (?)",
        (telegram_user_id,),
    )
    fields: dict[str, Any] = {}
    if display_label is not None:
        fields["display_label"] = display_label
    if linked_person_id is not None:
        fields["linked_person_id"] = linked_person_id
    if branch_id is not None:
        fields["branch_id"] = branch_id
    if is_admin is not None:
        fields["is_admin"] = int(is_admin)
    if admin_scope is not None:
        fields["admin_scope"] = admin_scope
    if not fields:
        return

    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE contributors SET {assignments} WHERE telegram_user_id = ?",
        (*fields.values(), telegram_user_id),
    )


def is_super_admin(telegram_user_id: int, conn: sqlite3.Connection | None = None) -> bool:
    """Super admin via config, or via the contributors table.

    Config wins and is checked first, so a super admin who is accidentally
    removed from the database can still be restored from the environment.
    """
    if telegram_user_id in config.SUPER_ADMIN_TELEGRAM_IDS:
        return True
    if conn is None:
        return False
    row = get_contributor(conn, telegram_user_id)
    return bool(row and row["is_admin"] and row["admin_scope"] == "super")


def admin_branch_ids(
    conn: sqlite3.Connection, telegram_user_id: int
) -> list[int] | None:
    """Branches this admin may review. None means all of them."""
    if is_super_admin(telegram_user_id, conn):
        return None

    branches = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM branches WHERE admin_telegram_id = ?",
            (telegram_user_id,),
        )
    ]
    row = get_contributor(conn, telegram_user_id)
    if row and row["is_admin"] and row["branch_id"] is not None:
        if row["branch_id"] not in branches:
            branches.append(row["branch_id"])
    return branches


# ===========================================================================
# Fuzzy matching
# ===========================================================================


def canonical_family_name(family_name: str | None) -> str:
    """Fold a spelling variant onto the family's canonical form.

    Arabic transliteration is not standardised, so the same family reaches
    four countries spelled four ways. Those spellings are one family: matching
    has to see them as the same, while display must not. The variants live in
    config.FAMILY_NAME_VARIANTS. A name that is not one of them comes back as
    given — someone who married in keeps their own family name.
    """
    if not family_name:
        return config.FAMILY_NAME
    cleaned = family_name.strip()
    for variant in config.FAMILY_NAME_VARIANTS:
        if variant.casefold() == cleaned.casefold():
            return config.FAMILY_NAME
    return cleaned


def same_family(a: str | None, b: str | None) -> bool:
    """Whether two family names are the same family, spelling aside."""
    return canonical_family_name(a).casefold() == canonical_family_name(b).casefold()


def name_similarity(a: str, b: str) -> float:
    """Rough similarity of two given names, 0-1, case and accent tolerant."""
    return SequenceMatcher(None, a.strip().casefold(), b.strip().casefold()).ratio()


def find_probable_matches(
    conn: sqlite3.Connection,
    given_name: str,
    father_given_name: str | None = None,
    branch_id: int | None = None,
    threshold: float | None = None,
) -> list[tuple[sqlite3.Row, float]]:
    """Existing people who might already be this person, best score first.

    Matching is on given name plus the father's given name, within a branch
    where one is known — the same two facts the bot asks for on first contact.
    This only ever produces a hint for the admin queue. Nothing is merged or
    rejected on the strength of a score.
    """
    cutoff = config.FUZZY_MATCH_THRESHOLD if threshold is None else threshold

    sql = PERSON_SELECT
    params: tuple[Any, ...] = ()
    if branch_id is not None:
        sql += " WHERE p.branch_id = ?"
        params = (branch_id,)

    scored: list[tuple[sqlite3.Row, float]] = []
    for row in conn.execute(sql, params):
        score = name_similarity(given_name, row["given_name"])
        if father_given_name and row["father_given_name"]:
            # The father's name is the disambiguator, so weight it heavily.
            father_score = name_similarity(
                father_given_name, row["father_given_name"]
            )
            score = 0.5 * score + 0.5 * father_score
        elif father_given_name or row["father_given_name"]:
            # One side knows the father and the other does not. Not a
            # contradiction, but not corroboration either.
            score *= 0.85
        if score >= cutoff:
            scored.append((row, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


# ===========================================================================
# Corroboration
# ===========================================================================
#
# Name similarity alone is weak: half the men in a branch are called Khalil.
# What actually identifies someone is who they are attached to. Two people
# independently describing "Georges, brother of Khalil son of Youssef" have
# agreed on a relative, and that is far stronger evidence than a matching
# spelling.
#
# This produces evidence for an admin to look at. It never merges anything.
# ===========================================================================


def _relatives_of(conn: sqlite3.Connection, person_id: int) -> dict[str, Any]:
    """The facts about a person that a second submitter could corroborate."""
    row = conn.execute(
        "SELECT id, father_id, mother_id FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    if row is None:
        return {}
    return {
        "father_id": row["father_id"],
        "mother_id": row["mother_id"],
        "partner_ids": {p["id"] for p in get_partners(conn, person_id)},
        "child_ids": {c["id"] for c in get_children(conn, person_id)},
    }


def _relational_reasons(
    conn: sqlite3.Connection,
    candidate: sqlite3.Row,
    role: str,
    subject_id: int,
    subject: dict[str, Any],
) -> list[str]:
    """Why this candidate might already be the person being described."""
    reasons: list[str] = []

    if role == "sibling":
        if subject["father_id"] and candidate["father_id"] == subject["father_id"]:
            reasons.append("same father")
        if subject["mother_id"] and candidate["mother_id"] == subject["mother_id"]:
            reasons.append("same mother")
    elif role == "child":
        if candidate["father_id"] == subject_id or candidate["mother_id"] == subject_id:
            reasons.append("already recorded as their child")
    elif role == "father":
        if subject["father_id"] == candidate["id"]:
            reasons.append("already recorded as their father")
        elif candidate["id"] in subject["child_ids"]:
            reasons.append("already has this person as a child")
    elif role == "mother":
        if subject["mother_id"] == candidate["id"]:
            reasons.append("already recorded as their mother")
    elif role == "spouse":
        if candidate["id"] in subject["partner_ids"]:
            reasons.append("already recorded as their spouse")

    return reasons


def corroborate(
    conn: sqlite3.Connection,
    given_name: str,
    role: str | None = None,
    subject_person_id: int | None = None,
    subject_submission_id: int | None = None,
    father_given_name: str | None = None,
    branch_id: int | None = None,
    exclude_submission_id: int | None = None,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Everyone this claim might already be, with the evidence, best first.

    Searches both directions the same person can already exist:

      * `people` — someone an admin has already approved.
      * `submissions` — someone else's pending claim. This is where two
        brothers submitting the same third brother actually collide, and it
        happens before either is approved, so matching only against approved
        people would miss the common case entirely.
    """
    cutoff = FUZZY_FALLBACK if threshold is None else threshold
    subject = _relatives_of(conn, subject_person_id) if subject_person_id else {}
    results: list[dict[str, Any]] = []

    # --- against people already in the tree -------------------------------
    sql = PERSON_SELECT
    params: tuple[Any, ...] = ()
    if branch_id is not None:
        sql += " WHERE p.branch_id = ?"
        params = (branch_id,)

    for row in conn.execute(sql, params):
        if row["id"] == subject_person_id:
            # The person we are adding relatives for cannot be their own
            # sibling, parent, or child.
            continue
        score = name_similarity(given_name, row["given_name"])
        reasons: list[str] = []

        if father_given_name and row["father_given_name"]:
            if name_similarity(father_given_name, row["father_given_name"]) >= 0.85:
                reasons.append(f"father also given as {row['father_given_name']}")

        if subject and role:
            reasons.extend(
                _relational_reasons(conn, row, role, subject_person_id, subject)
            )

        if reasons:
            # A shared relative outweighs a shaky spelling: "Khaleel, brother
            # of the same man" is the same person as "Khalil".
            score = 0.5 + 0.5 * score

        if score >= cutoff:
            results.append(
                {
                    "kind": "person",
                    "id": row["id"],
                    "person_id": row["id"],
                    "label": row_display_name(row),
                    "score": round(score, 3),
                    "reasons": reasons,
                }
            )

    # --- against other pending claims --------------------------------------
    for row in conn.execute(
        "SELECT * FROM submissions WHERE status = 'pending' ORDER BY id"
    ):
        if exclude_submission_id is not None and row["id"] == exclude_submission_id:
            continue
        payload = submission_payload(row)
        about = payload.get("about") or {}

        for entry in payload.get("people") or []:
            score = name_similarity(given_name, entry.get("given_name", ""))
            reasons = []

            same_subject = (
                subject_person_id is not None
                and about.get("person_id") == subject_person_id
            ) or (
                subject_submission_id is not None
                and about.get("submission_id") == subject_submission_id
            )
            if same_subject and entry.get("role") == role:
                reasons.append(
                    f"someone else described the same {role} of the same person"
                )
                score = 0.5 + 0.5 * score

            if score >= cutoff:
                results.append(
                    {
                        "kind": "submission",
                        "id": row["id"],
                        "person_id": None,
                        "label": submission_person_label(entry),
                        "score": round(score, 3),
                        "reasons": reasons,
                        "submitted_by": row["telegram_user_id"],
                    }
                )

    results.sort(key=lambda match: match["score"], reverse=True)
    return results


# ===========================================================================
# Integrity
# ===========================================================================


def find_name_collisions(
    conn: sqlite3.Connection,
) -> list[tuple[str, list[sqlite3.Row]]]:
    """People who compute to the same display name.

    Constraint 3 says: surface these in the admin queue, and do not build a
    deeper disambiguation scheme until they actually occur.
    """
    buckets: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(PERSON_SELECT):
        buckets.setdefault(row_display_name(row).casefold(), []).append(row)
    return [
        (row_display_name(rows[0]), rows)
        for rows in buckets.values()
        if len(rows) > 1
    ]


def find_ancestry_cycles(conn: sqlite3.Connection) -> list[int]:
    """Ids of people who are their own ancestor.

    Hand-seeding a few hundred relatives from memory will eventually produce
    one of these, and every graph walk in the app would loop forever on it.
    """
    parents = {
        row["id"]: (row["father_id"], row["mother_id"])
        for row in conn.execute("SELECT id, father_id, mother_id FROM people")
    }

    UNVISITED, IN_PROGRESS, DONE = 0, 1, 2
    state = dict.fromkeys(parents, UNVISITED)
    bad: set[int] = set()

    for start in parents:
        if state[start] != UNVISITED:
            continue
        # Explicit stack: an iterative DFS, so deep patrilines cannot blow up
        # the Python recursion limit.
        stack: list[tuple[int, Iterator[int]]] = []
        state[start] = IN_PROGRESS
        stack.append((start, iter([p for p in parents[start] if p is not None])))

        while stack:
            node, remaining = stack[-1]
            advanced = False
            for parent_id in remaining:
                if parent_id not in state:
                    continue
                if state[parent_id] == IN_PROGRESS:
                    bad.add(parent_id)
                    continue
                if state[parent_id] == UNVISITED:
                    state[parent_id] = IN_PROGRESS
                    stack.append(
                        (
                            parent_id,
                            iter([p for p in parents[parent_id] if p is not None]),
                        )
                    )
                    advanced = True
                    break
            if not advanced:
                state[node] = DONE
                stack.pop()

    return sorted(bad)


def check_integrity(conn: sqlite3.Connection) -> dict[str, list[Any]]:
    """Everything worth a human's attention about the current data."""
    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()

    orphan_branches = conn.execute(
        "SELECT * FROM branches WHERE founding_ancestor_id IS NULL"
    ).fetchall()

    no_branch = conn.execute(
        PERSON_SELECT + " WHERE p.branch_id IS NULL ORDER BY p.id"
    ).fetchall()

    return {
        "foreign_key_violations": fk_violations,
        "ancestry_cycles": find_ancestry_cycles(conn),
        "name_collisions": find_name_collisions(conn),
        "branches_without_founder": orphan_branches,
        "people_without_branch": no_branch,
    }
