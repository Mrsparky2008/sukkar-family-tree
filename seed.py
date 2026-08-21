#!/usr/bin/env python3
"""
Hand-editable seed data.

Edit the PEOPLE and UNIONS lists below, then run:

    python seed.py            # create the database and load this data
    python seed.py --reset    # wipe it first (destroys everything)
    python seed.py --check    # load nothing; just report on the current data

This is how the first vertical of the family gets in — typed out by hand from
what one person already knows, before the bot exists. After the bot is live,
new relatives arrive through the submission queue instead, and this file
becomes a record of the starting point.

--------------------------------------------------------------------------
HOW TO EDIT
--------------------------------------------------------------------------

Everyone gets a `key`: a short, lowercase, unique label you invent. Keys exist
only in this file, so that you can write `"father": "youssef"` instead of
guessing a database id. Use whatever reads clearly — `khalil_of_youssef` is a
fine key.

Per person:

    key         required, unique within this file
    given       required, the first name ONLY
    given_ar    optional, Arabic script
    sex         "M" or "F", or omit if unknown
    father      optional, another person's key
    mother      optional, another person's key
    family      optional, defaults to config.FAMILY_NAME. Set this for women
                who married in and kept their own family name.
    notes       optional free text — stories, context, who told you

There are no date fields, and none are coming. See constraint 2 in the spec.

Full names are NOT typed here. `Khalil` whose father is `Youssef` renders as
"Khalil Youssef" plus the family name, automatically. If you find yourself
typing a full name into `given`, that is the bug this design exists to prevent.

Branches are not typed here either: everyone is assigned to the branch of the
founding ancestor at the top of their patriline, automatically, on load.

--------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any

import config
import db


# ===========================================================================
# ###   EXAMPLE DATA — REPLACE ALL OF IT   ###############################
#
# These twelve people are invented. They exist so the repo runs, and so the
# three things most likely to be got wrong are visible in working form:
#
#   * two men named Khalil, told apart only by their fathers  (constraint 3)
#   * a cousin marriage joining two branches                  (constraint 1)
#   * women who married in, with their own family names
#
# Delete this block and type the real first vertical in its place. Keep
# config.FOUNDING_ANCESTORS in step with it: every `key` listed there must
# match a person here.
# ===========================================================================

PEOPLE: list[dict[str, Any]] = [
    # --- the generation above the founding brothers ------------------------
    {
        "key": "elias",
        "given": "Elias",
        "sex": "M",
        "notes": "Father of the founding brothers. Nothing known above him.",
    },
    # --- the founding brothers (each is a branch in config.py) -------------
    {"key": "youssef", "given": "Youssef", "sex": "M", "father": "elias"},
    {"key": "boutros", "given": "Boutros", "sex": "M", "father": "elias"},
    # --- married in --------------------------------------------------------
    {"key": "nada", "given": "Nada", "sex": "F", "family": "Karam"},
    {"key": "layla", "given": "Layla", "sex": "F", "family": "Rahme"},
    # --- second generation -------------------------------------------------
    # Renders as "Khalil Youssef" + the family name from config.
    {
        "key": "khalil_y",
        "given": "Khalil",
        "sex": "M",
        "father": "youssef",
        "mother": "nada",
    },
    {
        "key": "georges",
        "given": "Georges",
        "sex": "M",
        "father": "youssef",
        "mother": "nada",
    },
    # Renders as "Antoun Boutros" + the family name from config.
    {
        "key": "antoun",
        "given": "Antoun",
        "sex": "M",
        "father": "boutros",
        "mother": "layla",
    },
    # --- third generation --------------------------------------------------
    {"key": "therese", "given": "Therese", "sex": "F", "family": "Obeid"},
    {"key": "mariam", "given": "Mariam", "sex": "F", "father": "khalil_y", "mother": "therese"},
    # The second Khalil. Renders as "Khalil Antoun" + the family name — same
    # given name as khalil_y, disambiguated entirely by the father link.
    {"key": "khalil_a", "given": "Khalil", "sex": "M", "father": "antoun"},
    # --- fourth generation -------------------------------------------------
    # Child of the cousin marriage. Both branches are among his ancestors,
    # which is exactly the shape a tree structure cannot represent.
    {
        "key": "joseph",
        "given": "Joseph",
        "sex": "M",
        "father": "khalil_a",
        "mother": "mariam",
        "notes": "Descends from both founding brothers.",
    },
]

UNIONS: list[dict[str, Any]] = [
    {"a": "youssef", "b": "nada"},
    {"a": "boutros", "b": "layla"},
    {"a": "khalil_y", "b": "therese"},
    {
        "a": "khalil_a",
        "b": "mariam",
        "notes": "Cousin marriage — joins the two branches.",
    },
]

# ===========================================================================
# ###   END OF EXAMPLE DATA   ##############################################
# ===========================================================================


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def validate() -> list[str]:
    """Check the lists above before touching the database.

    Returns a list of problems. An empty list means the data is loadable —
    it does not mean the genealogy is correct, only that it is consistent.
    """
    problems: list[str] = []
    keys: set[str] = set()

    for index, person in enumerate(PEOPLE):
        where = f"PEOPLE[{index}]"
        key = person.get("key")
        if not key:
            problems.append(f"{where}: missing 'key'")
            continue
        where = f"PEOPLE[{index}] ({key})"
        if key in keys:
            problems.append(f"{where}: duplicate key")
        keys.add(key)

        if not person.get("given", "").strip():
            problems.append(f"{where}: missing 'given'")
        if " " in person.get("given", "").strip():
            problems.append(
                f"{where}: 'given' is the first name only — "
                f"got {person['given']!r}. Full names are computed."
            )
        if person.get("sex") not in (None, "M", "F"):
            problems.append(f"{where}: sex must be 'M', 'F', or omitted")

        for field in ("birth", "death", "born", "died", "dob", "year"):
            if field in person:
                problems.append(
                    f"{where}: '{field}' is not a field. This project stores "
                    f"no dates at all — see constraint 2."
                )

        unknown = set(person) - {
            "key", "given", "given_ar", "sex", "father", "mother", "family", "notes"
        }
        if unknown:
            problems.append(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")

    for index, person in enumerate(PEOPLE):
        key = person.get("key", f"PEOPLE[{index}]")
        for role in ("father", "mother"):
            ref = person.get(role)
            if ref is not None and ref not in keys:
                problems.append(f"{key}: {role} {ref!r} is not a key in PEOPLE")
            if ref is not None and ref == key:
                problems.append(f"{key}: is listed as their own {role}")

    for index, union in enumerate(UNIONS):
        where = f"UNIONS[{index}]"
        for side in ("a", "b"):
            ref = union.get(side)
            if ref is None:
                problems.append(f"{where}: missing '{side}'")
            elif ref not in keys:
                problems.append(f"{where}: {side}={ref!r} is not a key in PEOPLE")
        if union.get("a") is not None and union.get("a") == union.get("b"):
            problems.append(f"{where}: a person cannot be in a union with themselves")

    configured = {entry["key"] for entry in config.FOUNDING_ANCESTORS}
    for key in sorted(configured - keys):
        problems.append(
            f"config.FOUNDING_ANCESTORS has branch {key!r}, but PEOPLE has no "
            f"person with that key"
        )

    return problems


def load(conn: sqlite3.Connection) -> dict[str, int]:
    """Insert the data above. Returns a mapping of seed key to person id.

    Runs in one transaction: either the whole vertical lands or none of it does.
    """
    with db.transaction(conn):
        db.sync_branches(conn)

        # Pass 1: everyone, without parent links. Parents may appear anywhere
        # in the list, including below their children.
        ids: dict[str, int] = {}
        for person in PEOPLE:
            ids[person["key"]] = db.create_person(
                conn,
                person["given"],
                given_name_ar=person.get("given_ar"),
                family_name=person.get("family"),
                sex=person.get("sex"),
                notes=person.get("notes"),
            )

        # Pass 2: parent links, now that every id exists.
        for person in PEOPLE:
            links = {
                column: ids[person[role]]
                for role, column in (("father", "father_id"), ("mother", "mother_id"))
                if person.get(role)
            }
            if links:
                db.update_person(conn, ids[person["key"]], **links)

        # Pass 3: unions.
        for union in UNIONS:
            db.create_union(conn, ids[union["a"]], ids[union["b"]], union.get("notes"))

        # Pass 4: point each branch at its founder, then derive everyone's branch.
        for entry in config.FOUNDING_ANCESTORS:
            if entry["key"] in ids:
                db.set_branch_founder(conn, entry["key"], ids[entry["key"]])
        db.assign_branches(conn)

    return ids


def reset(conn: sqlite3.Connection) -> None:
    """Delete all data. The schema survives; the relatives do not."""
    with db.transaction(conn):
        for table in ("submissions", "contributors", "unions", "people", "branches"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN"
            " ('submissions','contributors','unions','people','branches')"
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report(conn: sqlite3.Connection) -> bool:
    """Print what is in the database. Returns False if anything needs a human."""
    branches = db.get_branches(conn)
    people = db.get_people(conn)
    unions = db.get_unions(conn)

    print(f"\n{len(people)} people, {len(unions)} unions, {len(branches)} branches\n")

    for branch in branches:
        members = [p for p in people if p["branch_id"] == branch["id"]]
        print(f"  {branch['display_name']}  ({len(members)})")
        for person in members:
            print(f"      {db.row_display_name(person)}")

    unplaced = [p for p in people if p["branch_id"] is None]
    if unplaced:
        print(f"\n  No branch  ({len(unplaced)})")
        for person in unplaced:
            print(f"      {db.row_display_name(person)}")

    issues = db.check_integrity(conn)
    clean = True

    if issues["foreign_key_violations"]:
        clean = False
        print(f"\nBROKEN REFERENCES: {len(issues['foreign_key_violations'])}")

    if issues["ancestry_cycles"]:
        clean = False
        print("\nANCESTRY CYCLES — these people are their own ancestor:")
        for person_id in issues["ancestry_cycles"]:
            person = db.get_person(conn, person_id)
            print(f"      #{person_id} {db.row_display_name(person)}")

    if issues["name_collisions"]:
        # Not an error. Constraint 3 says surface it and wait to see whether it
        # actually matters before building anything more elaborate.
        print("\nNAME COLLISIONS — these people compute to the same display name:")
        for name, rows in issues["name_collisions"]:
            ids = ", ".join(f"#{row['id']}" for row in rows)
            print(f"      {name}  ({ids})")

    if issues["branches_without_founder"]:
        clean = False
        print("\nBRANCHES WITH NO FOUNDER IN THE DATA:")
        for branch in issues["branches_without_founder"]:
            print(f"      {branch['key']} — {branch['display_name']}")

    print()
    return clean


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete all existing data before loading (destructive)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report on existing data without writing anything",
    )
    parser.add_argument("--db", help="database file (default: config.DATABASE_PATH)")
    args = parser.parse_args(argv)

    problems = validate()
    if problems:
        print(f"seed data has {len(problems)} problem(s):\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(file=sys.stderr)
        return 1

    conn = db.connect(args.db)
    db.init_db(conn)

    if args.check:
        return 0 if report(conn) else 1

    existing = db.count_people(conn)
    if existing and not args.reset:
        print(
            f"database already holds {existing} people at {args.db or config.DATABASE_PATH}.\n"
            f"Re-running would duplicate them.\n\n"
            f"  seed.py --check   to see what is there\n"
            f"  seed.py --reset   to wipe it and reload (destroys everything)",
            file=sys.stderr,
        )
        return 1

    if args.reset and existing:
        reset(conn)
        print(f"cleared {existing} existing people")

    load(conn)
    print(f"loaded {len(PEOPLE)} people and {len(UNIONS)} unions")
    return 0 if report(conn) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Output was piped into something that stopped reading, e.g. `| head`.
        # Not an error; just leave quietly without a traceback.
        sys.stdout = None
        raise SystemExit(0)
