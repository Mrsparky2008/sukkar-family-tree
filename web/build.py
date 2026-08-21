#!/usr/bin/env python3
"""
Build the public view: one self-contained HTML file.

    python web/build.py                 # writes web/tree.html
    python web/build.py --out /tmp/x.html --db /path/to/family.db

No CDN, no build step, no dependencies. The graph is drawn on a canvas by a
couple of hundred lines of plain JavaScript, so the file works offline, works
in five years, and can be emailed to a relative who will open it on a phone.

The data is embedded as JSON, which means the file is a snapshot: rebuild it
after a round of approvals.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import submissions as subs  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent / "template.html"


def collect(conn) -> dict:
    """Everything the page needs, already resolved so the browser does no work."""
    people = db.get_people(conn)
    by_id = {row["id"]: row for row in people}

    payload_people = []
    for row in people:
        person_id = row["id"]
        claims = []
        for claim in db.provenance(conn, person_id):
            claims.append(
                {
                    "what": subs.describe(claim["claim"]),
                    "who": claim["told_by"],
                    "how": claim["closeness"],
                    "heard": claim["heard_from"],
                }
            )

        spellings = [c["spelling"] for c in db.spelling_claims(conn, person_id)]

        payload_people.append(
            {
                "id": person_id,
                "given": row["given_name"],
                "family": row["family_name"],
                "aka": row["also_known_as"],
                "arabic": row["given_name_ar"],
                "display": db.display_name_with_also_known_as(row),
                "sex": row["sex"],
                "branch": row["branch_id"],
                "father": row["father_id"],
                "mother": row["mother_id"],
                "parents": [p["id"] for p in db.get_parents(conn, person_id)],
                "partners": [p["id"] for p in db.get_partners(conn, person_id)],
                "children": [c["id"] for c in db.get_children(conn, person_id)],
                "siblings": [s["id"] for s in db.get_siblings(conn, person_id)],
                "notes": row["notes"],
                "spellings": spellings,
                "claims": claims,
                # Searching should find someone by any name they answer to.
                "search": " ".join(
                    part.lower()
                    for part in (
                        row["given_name"],
                        row["family_name"],
                        row["also_known_as"],
                        row["given_name_ar"],
                        db.row_display_name(row),
                    )
                    if part
                ),
            }
        )

    unions = [
        [row["partner_a_id"], row["partner_b_id"]]
        for row in db.get_unions(conn)
        if row["partner_a_id"] in by_id and row["partner_b_id"] in by_id
    ]

    branches = [
        {"id": row["id"], "name": row["display_name"], "colour": row["colour"]}
        for row in db.get_branches(conn)
    ]

    return {"people": payload_people, "unions": unions, "branches": branches}


def build(conn, out: Path) -> Path:
    html = TEMPLATE.read_text(encoding="utf-8")

    heading = f"The {config.FAMILY_NAME} family"
    replacements = {
        "__TITLE__": f"{config.FAMILY_NAME} Family Graph",
        "__HEADING__": heading,
        "__PLACE__": f"of {config.VILLAGE}",
        "__DATA__": json.dumps(collect(conn), ensure_ascii=False, separators=(",", ":")),
    }
    for token, value in config.COLOURS.items():
        replacements[f"__{token}__"] = value

    for token, value in replacements.items():
        html = html.replace(token, value)

    leftover = [line for line in html.splitlines() if "__" in line and "--" in line]
    if leftover:
        print(f"warning: {len(leftover)} unreplaced token(s)", file=sys.stderr)

    out.write_text(html, encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--db")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "tree.html"))
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    db.init_db(conn)
    try:
        out = build(conn, Path(args.out))
    finally:
        conn.close()

    size = out.stat().st_size
    print(f"wrote {out} ({size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
