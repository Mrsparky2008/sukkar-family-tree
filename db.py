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
    _add_missing_columns(conn)
    sync_family_variants(conn)
    conn.commit()


#: Columns added after a database may already exist. CREATE TABLE IF NOT
#: EXISTS will not add them, so they go on here.
_LATER_COLUMNS = {
    "people": {
        "family_name_self_reported": "INTEGER NOT NULL DEFAULT 0",
        "also_known_as": "TEXT",
        "from_submission_id": "INTEGER",
        "branch_declared": "INTEGER NOT NULL DEFAULT 0",
    },
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _LATER_COLUMNS.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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


def display_name_with_also_known_as(row: sqlite3.Row) -> str:
    """The computed name, with the other name they answer to.

        Youssef Najib FAMILYNAME (Joe)

    Half the family was anglicised at a border, and the English form is what
    the new country uses. Nobody looking for their uncle types the name on his
    birth certificate.
    """
    base = row_display_name(row)
    also_known_as = _optional(row, "also_known_as")
    return f"{base} ({also_known_as})" if also_known_as else base


def display_name_with_spellings(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """The display name, plus any other spelling recorded for this person.

        Steven Semaan SPELLING-A / SPELLING-B

    When the family name is genuinely written two ways — one country's paper
    against another's — showing both beats picking one and hiding the fact
    that there was a choice. Still one name rule underneath: this appends to
    what `display_name()` produced, it never builds a name itself.
    """
    base = display_name_with_also_known_as(row)
    others = [
        claim["spelling"]
        for claim in spelling_claims(conn, row["id"])
        if claim["spelling"] != row["family_name"]
    ]
    if not others:
        return base
    return f"{base} / {' / '.join(dict.fromkeys(others))}"


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
    also_known_as: str | None = None,
    sex: str | None = None,
    father_id: int | None = None,
    mother_id: int | None = None,
    branch_id: int | None = None,
    notes: str | None = None,
    created_by_telegram_id: int | None = None,
    from_submission_id: int | None = None,
) -> int:
    """Insert a person and return their id. Privileged — see the note above."""
    cursor = conn.execute(
        """
        INSERT INTO people (given_name, given_name_ar, also_known_as, family_name, sex,
                            father_id, mother_id, branch_id, notes,
                            created_by_telegram_id, from_submission_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            given_name.strip(),
            given_name_ar,
            also_known_as,
            (family_name or config.FAMILY_NAME).strip(),
            sex,
            father_id,
            mother_id,
            branch_id,
            notes,
            created_by_telegram_id,
            from_submission_id,
        ),
    )
    return int(cursor.lastrowid)


def update_person(conn: sqlite3.Connection, person_id: int, **fields: Any) -> None:
    """Update named columns on a person. Privileged — see the note above."""
    allowed = {
        "given_name",
        "given_name_ar",
        "also_known_as",
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


def set_family_name(
    conn: sqlite3.Connection,
    person_id: int,
    family_name: str,
    self_reported: bool,
) -> bool:
    """Record how this person's family name is spelled. Returns True if changed.

    Precedence is the whole point. Three relatives can record the same man
    three different ways — one guesses, one copies a headstone, one reads a
    passport — and they are not equally authoritative. A person's own answer
    to "how do you spell your family name" beats anyone else's guess, and a
    guess never overwrites an answer.
    """
    row = get_person(conn, person_id)
    if row is None:
        return False

    already_self = bool(row["family_name_self_reported"])
    if already_self and not self_reported:
        return False  # a guess does not overwrite what the person said
    if row["family_name"] == family_name and already_self == self_reported:
        return False

    conn.execute(
        "UPDATE people SET family_name = ?, family_name_self_reported = ?"
        " WHERE id = ?",
        (family_name.strip(), int(self_reported), person_id),
    )
    return True


def spelling_claims(
    conn: sqlite3.Connection, person_id: int
) -> list[dict[str, Any]]:
    """Every spelling anyone has claimed for this person, and who claimed it.

    Kept because the same man is genuinely spelled differently on Lebanese and
    Australian paper, and somebody searching either way should find him.
    """
    claims: list[dict[str, Any]] = []
    row = get_person(conn, person_id)
    if row is not None and row["family_name"]:
        claims.append(
            {
                "spelling": row["family_name"],
                "who": "themselves" if row["family_name_self_reported"] else "recorded",
                "self_reported": bool(row["family_name_self_reported"]),
            }
        )

    for submission in conn.execute(
        "SELECT * FROM submissions WHERE resulting_person_id = ? ORDER BY id",
        (person_id,),
    ):
        payload = submission_payload(submission)
        self_reported = payload.get("kind") == "identify"
        for entry in payload.get("people") or []:
            spelling = entry.get("family_name")
            if not spelling:
                continue
            # One payload can name several people. Only the entry that is
            # actually this person says anything about how *their* name is
            # spelled — a mother's maiden name is not a variant of her
            # husband's surname.
            if row is not None and entry.get("given_name", "").casefold() != (
                row["given_name"] or ""
            ).casefold():
                continue
            if any(c["spelling"] == spelling for c in claims):
                continue
            who = payload.get("submitted_by") or {}
            claims.append(
                {
                    "spelling": spelling,
                    "who": "themselves"
                    if self_reported
                    else (who.get("label") or f"telegram {submission['telegram_user_id']}"),
                    "self_reported": self_reported,
                }
            )
    return claims


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


def people_from_submission(
    conn: sqlite3.Connection, submission_id: int
) -> list[sqlite3.Row]:
    """Everyone a single submission created."""
    return conn.execute(
        PERSON_SELECT + " WHERE p.from_submission_id = ? ORDER BY p.id",
        (submission_id,),
    ).fetchall()


def get_unions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM unions ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# Peer checks — "you said this, they said that"
# ---------------------------------------------------------------------------


def add_peer_check(
    conn: sqlite3.Connection,
    submission_id: int,
    telegram_user_id: int,
    question: str,
) -> int:
    cursor = conn.execute(
        """INSERT INTO peer_checks (submission_id, telegram_user_id, question)
           VALUES (?, ?, ?)""",
        (submission_id, telegram_user_id, question),
    )
    return int(cursor.lastrowid)


def get_peer_check(conn: sqlite3.Connection, check_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM peer_checks WHERE id = ?", (check_id,)
    ).fetchone()


def answer_peer_check(
    conn: sqlite3.Connection, check_id: int, verdict: str
) -> None:
    """Record the answer. Append-only in spirit: a verdict is written once
    and the question text stays exactly as it was asked."""
    conn.execute(
        """UPDATE peer_checks
              SET verdict = ?, answered_at = datetime('now')
            WHERE id = ? AND verdict IS NULL""",
        (verdict, check_id),
    )


def peer_checks_for(
    conn: sqlite3.Connection, submission_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM peer_checks WHERE submission_id = ? ORDER BY id",
        (submission_id,),
    ).fetchall()


# ===========================================================================
# Branches
# ===========================================================================


def sync_branches(conn: sqlite3.Connection) -> None:
    """Materialise the configured groupings into the `branches` table.

    Two lists feed it, because there are two ways to belong to a grouping and
    the family knows one of them long before the other:

      * `config.HOUSES` — declared. Somebody says which house they are from
        and their descendants inherit it.
      * `config.FOUNDING_ANCESTORS` — computed. Descent from a named man in
        the tree settles it outright.

    They are the same grouping either way, so both land in one table and keys
    must not collide. Keyed on `key`, so editing a display name, colour, or
    branch admin in config and re-running updates the row instead of creating
    a second one.
    """
    configured = list(config.HOUSES) + list(config.FOUNDING_ANCESTORS)
    for position, entry in enumerate(configured):
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


def declare_house(
    conn: sqlite3.Connection, person_id: int, branch_key: str
) -> bool:
    """Record that this person belongs to this house, on somebody's word.

    A declaration is a fixed point: `assign_branches` will never overwrite
    it, and everyone below on the father chain inherits it. Returns False
    for a house nobody has configured, so an unrecognised answer is kept as
    a claim on the record rather than guessed into the wrong grouping.
    """
    branch = get_branch_by_key(conn, branch_key)
    if branch is None:
        return False
    conn.execute(
        "UPDATE people SET branch_id = ?, branch_declared = 1 WHERE id = ?",
        (branch["id"], person_id),
    )
    return True


def assign_branches(conn: sqlite3.Connection) -> int:
    """Give every person a branch, and return how many were changed.

    Three passes, in order of confidence:

      1. Declaration. Somebody who would know said which house this person
         belongs to. Nothing derived may overwrite it.
      2. Patriline. Walk father links upward; the first declared ancestor or
         founding ancestor reached decides it. This covers everyone born into
         the family, including those whose house nobody stated directly.
      3. Marriage. Anyone still unassigned takes the branch of a partner who
         has one. This covers people who married in, so they appear when the
         public view is filtered to their spouse's grouping — assigned, never
         declared, because it is not theirs by birth.

    Anyone reachable by none of the three is left NULL rather than guessed at.
    """
    declared = {
        row["id"]: row["branch_id"]
        for row in conn.execute(
            "SELECT id, branch_id FROM people"
            " WHERE branch_declared = 1 AND branch_id IS NOT NULL"
        )
    }
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
            if current in declared:
                found = declared[current]
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


def sync_family_variants(conn: sqlite3.Connection) -> None:
    """Put the configured spellings in the table. Safe to re-run."""
    for spelling in config.FAMILY_NAME_VARIANTS:
        conn.execute(
            "INSERT OR IGNORE INTO family_variants (spelling, canonical, source)"
            " VALUES (?, ?, 'config')",
            (spelling, config.FAMILY_NAME),
        )


def record_family_variant(
    conn: sqlite3.Connection, spelling: str | None, source: str = "self-reported"
) -> None:
    """Learn a spelling of the family name we had not seen before.

    Only ever called for a name given in answer to "how do you spell your
    family name" during signup. A family name collected anywhere else — a
    mother's maiden name, a wife's — is a different family and must not land
    here.
    """
    if not spelling or not spelling.strip():
        return
    conn.execute(
        "INSERT OR IGNORE INTO family_variants (spelling, canonical, source)"
        " VALUES (?, ?, ?)",
        (spelling.strip(), config.FAMILY_NAME, source),
    )


def known_family_variants(conn: sqlite3.Connection | None = None) -> set[str]:
    """Every spelling that counts as this family, folded to lowercase."""
    variants = {v.casefold() for v in config.FAMILY_NAME_VARIANTS}
    variants.add(config.FAMILY_NAME.casefold())
    if conn is not None:
        try:
            variants.update(
                row["spelling"].casefold()
                for row in conn.execute("SELECT spelling FROM family_variants")
            )
        except sqlite3.OperationalError:
            # Table not created yet — config alone is a fine answer.
            pass
    return variants


def canonical_family_name(
    family_name: str | None, conn: sqlite3.Connection | None = None
) -> str:
    """Fold a spelling variant onto the family's canonical form.

    Every known spelling answers to one canonical name for matching and for
    branch grouping, while `people.family_name` keeps whatever is actually on
    that person's documents. A name that is not a known variant comes back as
    given — someone who married in keeps their own family name.

    Pass `conn` to include spellings learned from relatives signing up.
    """
    if not family_name:
        return config.FAMILY_NAME
    cleaned = family_name.strip()
    if cleaned.casefold() in known_family_variants(conn):
        return config.FAMILY_NAME
    return cleaned


def same_family(
    a: str | None, b: str | None, conn: sqlite3.Connection | None = None
) -> bool:
    """Whether two family names are the same family, spelling aside."""
    return (
        canonical_family_name(a, conn).casefold()
        == canonical_family_name(b, conn).casefold()
    )


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
        "SELECT id, sex, father_id, mother_id FROM people WHERE id = ?",
        (person_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "id": row["id"],
        "sex": row["sex"],
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


def _parent_name(conn: sqlite3.Connection, person_id: int | None) -> str | None:
    if not person_id:
        return None
    row = conn.execute(
        "SELECT given_name FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    return row["given_name"] if row else None


def _relational_objections(
    conn: sqlite3.Connection,
    candidate: sqlite3.Row,
    role: str,
    subject_id: int,
    subject: dict[str, Any],
) -> list[str]:
    """Why this candidate is probably NOT the person being described.

    Corroboration on its own is half an answer. Two men named Joseph, one
    the son of Metanios and one the son of Lichaa, agree on everything the
    scorer looks at and disagree on the only thing that matters. Saying
    "same name" and stopping there invites exactly the wrong tap, so the
    disagreement gets found and said out loud in the same breath.

    A missing fact is never an objection — most of this tree is missing
    facts. Only two recorded facts that cannot both be true count.
    """
    objections: list[str] = []

    def clash(recorded_id: int | None, expected_id: int | None, word: str) -> None:
        if recorded_id and expected_id and recorded_id != expected_id:
            name = _parent_name(conn, recorded_id)
            objections.append(
                f"their {word} is recorded as {name}" if name
                else f"a different {word} is recorded"
            )

    if role == "child":
        # The subject is the parent being claimed. Which slot depends on who
        # they are; with their sex unrecorded, either slot already filled by
        # somebody else is the disagreement.
        if subject.get("sex") == "M":
            clash(candidate["father_id"], subject_id, "father")
        elif subject.get("sex") == "F":
            clash(candidate["mother_id"], subject_id, "mother")
        else:
            clash(candidate["father_id"], subject_id, "father")
            clash(candidate["mother_id"], subject_id, "mother")
    elif role == "sibling":
        clash(candidate["father_id"], subject.get("father_id"), "father")
        clash(candidate["mother_id"], subject.get("mother_id"), "mother")
    elif role == "father":
        clash(subject.get("father_id"), candidate["id"], "father")
    elif role == "mother":
        clash(subject.get("mother_id"), candidate["id"], "mother")

    return objections


def name_sex_hint(conn: sqlite3.Connection, given_name: str) -> str | None:
    """What this family calls people with this name — "M", "F", or None.

    Learned from the family's own records, never from an outside name list:
    Hanna is a man here whatever the West thinks. A single disagreement
    anywhere kills the hint — a guess shown to the wrong person costs more
    than a question ever does."""
    target = given_name.strip().casefold()
    if not target:
        return None
    seen: set[str] = set()

    for row in conn.execute(
        "SELECT sex FROM people WHERE sex IS NOT NULL"
        " AND lower(given_name) = ?", (target,)
    ):
        seen.add(row["sex"])

    for row in conn.execute("SELECT * FROM submissions WHERE status = 'pending'"):
        payload = submission_payload(row)
        for entry in payload.get("people") or []:
            if (
                entry.get("sex")
                and (entry.get("given_name") or "").casefold() == target
            ):
                seen.add(entry["sex"])

    return seen.pop() if len(seen) == 1 else None


def corroborate(
    conn: sqlite3.Connection,
    given_name: str,
    role: str | None = None,
    family_name: str | None = None,
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

        objections: list[str] = []
        if subject and role:
            reasons.extend(
                _relational_reasons(conn, row, role, subject_person_id, subject)
            )
            objections = _relational_objections(
                conn, row, role, subject_person_id, subject
            )

        if reasons:
            # A shared relative outweighs a shaky spelling: "Khaleel, brother
            # of the same man" is the same person as "Khalil". And each extra
            # relative that agrees raises the floor — two people with the same
            # father, the same mother and the same given name are not two
            # people, whatever the surname says.
            floor = 0.5 + 0.1 * min(len(reasons) - 1, 3)
            score = floor + (1 - floor) * score

        if family_name and row["family_name"]:
            if not same_family(family_name, row["family_name"], conn):
                # Genuinely different families — a Karam who married in is not
                # a candidate. Spelling variants of our own name are NOT this
                # case; same_family() folds those together first.
                score *= 0.6

        if score >= cutoff:
            results.append(
                {
                    "kind": "person",
                    "id": row["id"],
                    "person_id": row["id"],
                    "label": row_display_name(row),
                    "score": round(score, 3),
                    "reasons": reasons,
                    "objections": objections,
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

            # Two contributors rarely hang the same person off the same
            # subject — a brother describes "my sibling Nawal", and later
            # Nawal describes herself. What they DO both know is the
            # father, so an agreeing father is what links the claims
            # before any admin has approved either.
            if father_given_name and entry.get("father_given_name"):
                if (
                    name_similarity(father_given_name, entry["father_given_name"])
                    >= 0.85
                ):
                    reasons.append(
                        f"father also given as {entry['father_given_name']}"
                    )

            if reasons:
                floor = 0.5 + 0.1 * min(len(reasons) - 1, 3)
                score = floor + (1 - floor) * score

            if score >= cutoff:
                results.append(
                    {
                        "kind": "submission",
                        "id": row["id"],
                        "person_id": None,
                        "label": submission_person_label(entry),
                        "score": round(score, 3),
                        "reasons": reasons,
                        "objections": [],
                        "submitted_by": row["telegram_user_id"],
                    }
                )

    def rank(match: dict[str, Any]) -> tuple[int, float]:
        """Something that agrees beats a name; a name beats a name that clashes.

        Scores tie constantly here — two people called Joseph both score 1.0
        on the name — and whoever the tie handed first place to was the one
        the review desk offered. So the evidence decides the order, and the
        score only settles ties within the same kind of evidence.
        """
        if match["reasons"]:
            standing = 2
        elif match.get("objections"):
            standing = 0
        else:
            standing = 1
        return (standing, match["score"])

    results.sort(key=rank, reverse=True)
    return results


# ===========================================================================
# Provenance
# ===========================================================================
#
# Every claim anyone has made is kept forever, with who made it. Nothing is
# ever overwritten — an approval writes a person, but the submission that
# produced it stays exactly as it was sent.
#
# That is what makes the question "who says so, and how would they know?"
# answerable later from data captured today. Closeness is the usual answer to
# the second half: a son knows his own father better than a cousin does. This
# module works that out and reports it. It never decides.
# ===========================================================================


def relationship_distance(
    conn: sqlite3.Connection, a: int, b: int, limit: int = 8
) -> int | None:
    """Steps between two people through parents, children and marriages.

    0 is the same person, 1 a parent, child or spouse, 2 a sibling or
    grandparent, and so on. None means further apart than `limit`, or not
    connected at all.
    """
    if a == b:
        return 0

    seen = {a}
    frontier = [a]
    for step in range(1, limit + 1):
        following: list[int] = []
        for person_id in frontier:
            neighbours: set[int] = set()
            row = conn.execute(
                "SELECT father_id, mother_id FROM people WHERE id = ?", (person_id,)
            ).fetchone()
            if row is not None:
                neighbours.update(
                    parent for parent in (row["father_id"], row["mother_id"]) if parent
                )
            neighbours.update(
                child["id"] for child in get_children(conn, person_id)
            )
            neighbours.update(
                partner["id"] for partner in get_partners(conn, person_id)
            )

            for neighbour in neighbours:
                if neighbour == b:
                    return step
                if neighbour not in seen:
                    seen.add(neighbour)
                    following.append(neighbour)
        if not following:
            return None
        frontier = following
    return None


#: How a distance reads in a sentence, for the review screen.
_CLOSENESS = {
    0: "themselves",
    1: "a parent, child or spouse",
    2: "a sibling, grandparent or grandchild",
    3: "a niece, nephew, aunt or uncle",
    4: "a first cousin",
}


def closeness(distance: int | None) -> str:
    if distance is None:
        return "not connected in the tree yet"
    return _CLOSENESS.get(distance, f"{distance} steps away")


def standing_name_author(
    conn: sqlite3.Connection, person_id: int
) -> tuple[int | None, int | None]:
    """Who put the name that is on record there, and who they are.

    Returns (telegram_user_id, their person id) — either may be None. The
    first submission that produced this person is the one that named them;
    later claims about the same person did not.
    """
    origin = conn.execute(
        "SELECT from_submission_id FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    row = None
    if origin and origin["from_submission_id"]:
        row = get_submission(conn, int(origin["from_submission_id"]))
    if row is None:
        row = conn.execute(
            "SELECT * FROM submissions WHERE resulting_person_id = ?"
            " ORDER BY id LIMIT 1",
            (person_id,),
        ).fetchone()
    if row is None:
        return (None, None)
    teller = (submission_payload(row).get("submitted_by") or {})
    who = teller.get("person_id")
    if not who:
        # Older payloads did not carry it, and a contributor who signed in
        # after sending still has a link. Falling back keeps them weighable
        # instead of counting as nobody, which would let anyone overrule them.
        contributor = get_contributor(conn, int(row["telegram_user_id"]))
        who = contributor["linked_person_id"] if contributor else None
    return (row["telegram_user_id"], who)


def inherited_spelling(
    conn: sqlite3.Connection, person_id: int, old_spelling: str
) -> list[sqlite3.Row]:
    """Descendants down the father chain still carrying the old spelling.

    A family name is stored on each person, not derived from the father, and
    deliberately so: this family really does spell it several ways, and one
    man's passport does not overrule his brother's. But when a spelling was
    wrong rather than different, everyone who inherited the mistake is still
    wearing it.

    So this finds them and stops there. Anyone who answered for their own
    name is left out — their own word is not a mistake to be swept up — and
    so is anyone already spelling it some third way, because that is a
    separate claim, not this one.
    """
    if not old_spelling:
        return []
    found: list[sqlite3.Row] = []
    frontier = [person_id]
    seen = {person_id}
    while frontier:
        following: list[int] = []
        for parent in frontier:
            for child in conn.execute(
                PERSON_SELECT + " WHERE p.father_id = ?", (parent,)
            ):
                if child["id"] in seen:
                    continue
                seen.add(child["id"])
                following.append(child["id"])
                if child["family_name_self_reported"]:
                    # Their own word, and their children follow them rather
                    # than their grandfather — so the sweep stops here, not
                    # just skips them.
                    following.pop()
                    continue
                if (child["family_name"] or "") == old_spelling:
                    found.append(child)
        frontier = following
    return found


def correction_weight(
    conn: sqlite3.Connection, person_id: int, corrector_person_id: int | None
) -> dict[str, Any]:
    """Whether a correction outranks the word already on record.

    Nobody here has a credential. What they have is a position: a daughter
    knows her own mother's maiden name, and a second cousin is repeating
    something he heard. So closeness on the tree stands in for authority —
    the same measure the provenance screen already reports, used to decide
    rather than only to display.

    Ties go to the correction. Somebody equally close who has gone to the
    trouble of saying it is wrong is the better of two equal claims, and a
    correction that loses a tie can never be made at all.

    Distance is measured through parents, children and marriages, so a wife's
    own family reaches her by marriage even when her maiden line is not
    recorded. `None` means unconnected, and never outranks anybody.
    """
    author_user, author_person = standing_name_author(conn, person_id)
    mine = (
        relationship_distance(conn, corrector_person_id, person_id)
        if corrector_person_id
        else None
    )
    theirs = (
        relationship_distance(conn, author_person, person_id)
        if author_person
        else None
    )

    if mine is None:
        outranks = False
    elif theirs is None:
        outranks = True
    else:
        outranks = mine <= theirs

    return {
        "mine": mine,
        "theirs": theirs,
        "author_user_id": author_user,
        "author_person_id": author_person,
        "outranks": outranks,
        "how_close": closeness(mine),
        "theirs_how_close": closeness(theirs),
    }


def _teller_label(
    conn: sqlite3.Connection, teller: dict[str, Any], teller_id: int | None, row
) -> str:
    if teller_id is not None:
        person = get_person(conn, teller_id)
        if person is not None:
            return row_display_name(person)
    return teller.get("label") or f"telegram {row['telegram_user_id']}"


def provenance(conn: sqlite3.Connection, person_id: int) -> list[dict[str, Any]]:
    """Everything anyone has said about this person, closest teller first.

    Ordering by closeness is not a ruling. It is the order a human would want
    to read them in, because the person nearest to the subject usually knows
    best — and when they do not, seeing both claims side by side is the only
    way anyone finds out.
    """
    claims: list[dict[str, Any]] = []

    # Two ways a submission speaks about somebody. `resulting_person_id`
    # records what a decision produced — but only one person per decision,
    # and "add my parents" produces two. The mother of every such pair had
    # no provenance at all: nothing about who said she existed, on a tree
    # whose whole point is that the record remembers who told it what.
    # `from_submission_id` on the person closes that.
    origin = conn.execute(
        "SELECT from_submission_id FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    origin_id = origin["from_submission_id"] if origin else None

    for row in conn.execute(
        "SELECT * FROM submissions"
        " WHERE resulting_person_id = ?1 OR (?2 IS NOT NULL AND id = ?2)"
        " ORDER BY id",
        (person_id, origin_id),
    ):
        payload = submission_payload(row)
        teller = payload.get("submitted_by") or {}
        teller_id = teller.get("person_id")
        if teller_id is None:
            # They had not been confirmed in the tree when they sent this, but
            # they may have been since. Saying "not connected" about somebody
            # plainly on the chart reads as a bug.
            contributor = get_contributor(conn, row["telegram_user_id"])
            if contributor is not None:
                teller_id = contributor["linked_person_id"]
        distance = (
            relationship_distance(conn, teller_id, person_id)
            if teller_id is not None
            else None
        )
        claims.append(
            {
                "submission_id": row["id"],
                "status": row["status"],
                "when": row["created_at"],
                "claim": payload,
                "told_by": _teller_label(conn, teller, teller_id, row),
                "told_by_person_id": teller_id,
                "heard_from": payload.get("source"),
                "distance": distance,
                "closeness": closeness(distance),
            }
        )

    claims.sort(
        key=lambda claim: (
            claim["distance"] if claim["distance"] is not None else 99,
            claim["submission_id"],
        )
    )
    return claims


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

    # A house is declared, not descended from anyone in the tree, so having
    # no founding ancestor is its normal state. Only a grouping that claims
    # to be defined by descent is incomplete without one.
    by_descent = {entry["key"] for entry in config.FOUNDING_ANCESTORS}
    orphan_branches = [
        row
        for row in conn.execute(
            "SELECT * FROM branches WHERE founding_ancestor_id IS NULL"
        )
        if row["key"] in by_descent
    ]

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
