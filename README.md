# Family Tree

A crowdsourced family graph. Relatives submit names and relationships through a
Telegram bot, branch admins review and approve, and the result renders as a
public read-only web page.

Built white-label: everything family-specific lives in `config.py`, so another
family can fork this repo, edit that one file, and deploy.

> **Status: Step 1 of 4 (Foundation) — complete.**
> Steps 2–4 (Telegram bot, admin queue, public view) are not built yet.

---

## The four rules

These shape every file here. They are worth reading before changing anything.

**1. It is a graph, not a tree.**
Cousin intermarriage going back to the 1800s means a person can reach the same
ancestor by several different paths. People are nodes; parentage and unions are
edges. Nothing nested is ever stored. Tree-shaped *views* get rendered from the
graph — the graph itself stays flat.

**2. There are no dates. Anywhere.**
No birth, no death, no marriage year, no approximations. Names and
relationships only. This removes every privacy question about living relatives,
removes date validation entirely, and turns a five-minute submission into a
thirty-second one — which is the difference between relatives contributing and
not. `created_at` and `reviewed_at` describe *rows*, not relatives. There is a
test that fails if a date-shaped column appears anywhere.

**3. Display names are computed, never typed.**
`db.display_name()` is the single implementation, used by the bot, the admin
interface, and the public view alike. Given name, then father's given name,
then family name. Because the string is regenerated on every read, correcting a
father link corrects every name derived from it — it cannot drift.

**4. Nothing writes directly to the people table.**
Every submission, *including every correction*, lands in `submissions` and waits
for an admin. No exceptions.

---

## Getting started

Python 3.11 or newer. Step 1 needs no third-party packages at all.

```bash
python seed.py            # create the database and load the seed data
python seed.py --check    # report on what is in it
python -m unittest discover -s tests -t .
```

`seed.py` prints every person grouped by branch, then flags anything that needs
a human: broken references, ancestry cycles, and display-name collisions.

The database is written to `data/family.db` and is gitignored — live family
data is never committed. Override the location with `FAMILY_TREE_DB`.

---

## Making it your family

Two files, in this order.

**1. `config.py`** — family name, village, colours, and one entry per founding
ancestor. Every branch needs a `key`, which is how the seed data refers to it.

**2. `seed.py`** — replace the example people with the real first vertical. The
editing rules are at the top of that file; the short version:

- `given` is the **first name only**. Full names are computed, never typed.
- Refer to parents by their `key`, not by an id.
- There are no date fields.
- Branches are assigned automatically by walking each person's patriline, so
  you never type one in.

Then `python seed.py --reset` to reload.

A test asserts that the family name and village appear in no Python file
outside `config.py`. If you hardcode one, the suite tells you.

---

## Layout

```
config.py       Everything family-specific. The only file a fork edits.
schema.sql      Five tables. Runnable repeatedly; every statement is IF NOT EXISTS.
db.py           All SQLite access, the display-name rule, fuzzy matching,
                branch assignment, integrity checks.
seed.py         Hand-editable starting data, plus validate / load / report.
tests/          python -m unittest discover -s tests -t .

bot/            Step 2 — Telegram capture.       Not built.
admin/          Step 3 — Flask review queue.     Not built.
web/            Step 4 — Public Cytoscape view.  Not built.
```

### Schema

| table | holds |
|---|---|
| `people` | The nodes. Given name only; no dates; father and mother links. |
| `unions` | Marriage edges. Undirected — a cousin marriage is one more edge, not a broken branch. |
| `submissions` | The queue. Everything from the bot lands here first. |
| `contributors` | Telegram identity. The user ID *is* the login. |
| `branches` | One per founding brother, materialised from `config.py`. |

### Branch assignment

Nobody types a branch. `db.assign_branches()` derives it, in order of
confidence:

1. **Patriline** — walk father links up to a founding ancestor.
2. **Marriage** — anyone still unassigned takes a partner's branch, so a woman
   who married in shows up when the view is filtered to her husband's branch.

Anyone reachable by neither — an ancestor above the split into branches, for
instance — is left unassigned rather than guessed at.

---

## What Step 1 gives you

- `db.display_name()` and `row_display_name()` — constraint 3, in one place.
- People, union, sibling, ancestor, and children queries that terminate on a
  graph with multiple paths to the same ancestor.
- `db.add_submission()` / `list_submissions()` / `resolve_submission()` — the
  queue, with branch scoping already wired through the submitter's branch.
- `db.find_probable_matches()` — fuzzy duplicate detection on given name plus
  father's given name. It only ever produces a hint for the admin queue.
  Nothing is auto-merged and nothing is auto-rejected.
- `db.check_integrity()` — foreign key violations, ancestry cycles, name
  collisions, branches with no founder, people with no branch.
- 72 tests over all of it, including the `display_name` doctests.

---

## Deployment notes

Secrets come from the environment, never the repo. Copy `.env.example` to
`.env` and fill it in; `.env` is gitignored.

The whole database is one SQLite file, so a backup is `cp family.db
family.db.bak`. Point `FAMILY_TREE_DB` somewhere outside the checkout in
production.
