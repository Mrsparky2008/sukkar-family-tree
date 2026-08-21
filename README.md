# Family Tree

A crowdsourced family graph. Relatives submit names and relationships through a
Telegram bot, branch admins review and approve, and the result renders as a
public read-only web page.

Built white-label: everything family-specific lives in `config.py`, so another
family can fork this repo, edit that one file, and deploy.

> **Status: ready for a pilot.**
> Foundation, Telegram capture, and a command-line review queue are built and
> tested. The Flask admin interface and the public Cytoscape view are not.

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

To run the bot as well:

```bash
pip install -r requirements.txt
cp .env.example .env      # then put your @BotFather token in it
python -m bot
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

submissions.py  The payload contract: what a submission looks like, and how
                to describe one. Shared by the bot and the review queue.
review.py       The review queue on the command line. Enough to run a pilot
                before the Flask interface exists.

bot/            Step 2 — Telegram capture. See bot/README.md.
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

## What Step 2 gives you

- The whole bot: identification on first contact, then the six-option menu.
- One question per message, every typed answer read back for confirmation, and
  "no" re-asks rather than starting over.
- A first name with a space in it is refused with an explanation, because
  "Khalil Youssef Sukkar" typed into a first-name box is how the computed-name
  rule silently produces nonsense.
- Corrections go into the queue as suggestions. Nothing edits live data, and
  the original submission is left exactly as it was sent.
- Duplicate flagging on every submission, for the admin to judge.
- 47 more tests, driving real conversations against a temporary database.
  A test fails the build if anything under `bot/` calls a privileged write.

Details in `bot/README.md`.

## The family name is asked, never assumed

Arabic transliteration is not standardised, so the same family reached four
countries spelled four ways — and that spelling is now on people's passports.
Signup asks which one, as buttons, so it costs a tap:

```
And how do you spell your family name?
   [ Sukkar ] [ Sukar ] [ Succar ] [ Soukkar ] [ Something else ]
```

Each person keeps their own spelling. Every spelling folds onto one canonical
form for matching and for branch grouping, so a Succar in Sydney and a
Soukkar in Beirut corroborate each other rather than fragmenting into
separate families. **A different spelling never means a different branch.**

The configured list is a starting point, not a limit. A relative who signs up
with a spelling nobody listed is answering "how do you spell *our* family
name", so that answer is learned as a variant and folds from then on — the
`family_variants` table. A family name collected anywhere else, like a
mother's maiden name, is a different family and is never learned.

This matters because the same man's descendants can be spelled one way on an
Australian passport and another way on the Lebanese records.

### Where a spelling split off

```bash
python review.py --spellings
```

```
Sukar  —  4 people
    splits from Succar at Kalim Semaan Sukar — 3 descendant(s) carry it
      (father Semaan Succar spells it Succar)

All of the above are one family. Spelling differences are
transliteration, not descent — matching folds them together.
```

Usually one clerk, at one border, on one day — and everyone below that person
inherits it.

### Whose spelling wins

Three relatives can record the same man three ways: one guesses, one copies a
headstone, one reads a passport. They are not equally authoritative.

A person's own answer to "how do you spell your family name" beats anyone
else's guess, and a guess never overwrites an answer:

```
after a brother's guess    : Succar
after his own answer       : Sukkar
after a cousin's later guess: Sukkar     <- unchanged
```

Every spelling anyone claimed is kept and shown in the review queue, because
the same man really is spelled differently on Lebanese and Australian paper
and somebody searching either way should find him.

### When both are true, show both

Where a name is genuinely written two ways, the tree shows both rather than
hiding that there was a choice:

```
Steven Semaan Sukar / Sukkar
```

The name rule underneath is untouched — this appends to what
`display_name()` produced. Constraint 3 still holds: one place a name is
built.

None of this affects identity. Spelling is folded away before matching, so
what actually identifies a person is their father, their mother, their
siblings and their children. Two records with the same given name and the
same parents are not two people, whatever the surname says — and each extra
relative that agrees raises the confidence floor, so father *and* mother
matching scores higher than either alone.

## Nothing is confirmed one name at a time

An earlier version read every answer back for a yes/no. It doubled the taps
and made a conversation feel like a form. Answers now go straight through,
and everything is checked once at the end, on a screen the contributor can
actually read and edit:

```
Here's everything you've given me. Check the spelling — tap any line to change it.

1. Kalim as father, Wadiha as mother of Steven Succar
2. Semaan as father of Kalim
   [ 1. Kalim ] [ 2. Semaan ] [ Send all 2 ] [ Add someone else ]
```

Tapping a line retypes or removes it. Nothing reaches the queue until they
send, and removing a parent removes anything that hung off them, so no
grandfather is left anchored to nothing.

## A whole family in one message

The relatives who hold four generations in their heads do not want twenty
questions. They type the lot:

```
Kalim's parents are Toufic and Cilene
Kalim's sisters: Dibeh, Sonia and Saide, married to Jamil Tarabay
```

That reads into six people with the right relationships — including the
husband hanging off the sister he married, not off Kalim — and comes back as
an editable list. See `bot/README.md`.

## Adding relatives for anyone, not just yourself

A contributor can only enter about six people if every question is about
*them*. So the bot keeps a cursor — whoever it is currently adding relatives
for — and moves it. An uncle is your father's brother, so pointing the cursor
at your father turns the same five questions into uncles and aunts. Every
prompt follows the cursor: *"What's Youssef's father's first name?"*

After each save the bot offers the next step rather than dropping back to a
menu, which is what walks someone up the generations:

```
Saved.
Do you know Youssef's parents?          [Yes, let's do that] [No, that's all I know]
```

It climbs until they run out, then goes sideways. A contributor can point the
cursor at somebody still sitting in the queue, so naming a grandfather never
waits on an admin approving the father first.

## Corroboration

Half the men in a branch answer to the same given name, so matching on
spelling alone is weak. What identifies someone is who they are attached to.

`db.corroborate()` scores a claim against both people already in the tree and
other pending claims — because two brothers submitting the same third brother
collide in the queue, before either is approved. It reports the evidence:

```
#3  Khaleel as brother of Georges Youssef Sukkar
    from Georges Youssef Sukkar   [pending]
    told to them by his mother Nada
    ? Khalil Youssef Sukkar (in the tree, 0.885) — same father, same mother
```

That is a misspelling caught by shared parents, not by the spelling.

It stays a hint. Nothing is auto-merged and nothing is auto-rejected — but
approving something that scores 0.9 or higher against an existing person is
refused until the reviewer says which they meant, because one tired tap
otherwise creates a second Youssef and moves his son onto the copy.

## Who says so, and how would they know?

Every claim anyone makes is kept forever, with who made it. Nothing is ever
overwritten: approving a submission writes a person, but the submission that
produced it stays exactly as it was sent. Two relatives who disagree both stay
on the record.

```bash
python review.py --who 12
```

```
#12  Hanna Najib Sukkar
  parents:  Najib Sukkar
  children: Jason Hanna Sukkar

  Where this came from, closest teller first:
      #2  Hanna (Johnny) as father of Jason
          told by Jason Hanna Sukkar — a parent, child or spouse,
          who heard it from his mother Therese  [merged]
      #1  Hanna (John) as brother of Wadiha
          told by Steven Kalim Sukkar — a niece, nephew, aunt or uncle  [merged]

  If these disagree, Jason Hanna Sukkar is the closest to them
  — but that is a hint, not a ruling.
```

Closeness is counted through the graph — parents, children and marriages —
so a son sorts above a cousin without anyone configuring that. It is an
ordering, never a decision: a human picks, and the losing claim stays
readable.

Because claims are append-only, that choice can be made or remade at any
point in the future from information captured today. Which is the whole
reason the queue never edits anything in place.

## Reviewing

```bash
python review.py                       # the queue, with evidence
python review.py --show 4              # one submission in full
python review.py --approve 4           # accept it, create the people
python review.py --merge 4 --into 12   # it is person 12 — link, don't duplicate
python review.py --reject 4 --note "not a relative"
python review.py --tree                # the family as it stands
```

Merging is not discarding: the relationship the submission claimed still gets
applied, it just attaches to the existing person. And merging only ever fills
gaps — it never overwrites something an admin already recorded.

---

## Deployment notes

Secrets come from the environment, never the repo. Copy `.env.example` to
`.env` and fill it in; `.env` is gitignored.

The whole database is one SQLite file, so a backup is `cp family.db
family.db.bak`. Point `FAMILY_TREE_DB` somewhere outside the checkout in
production.
