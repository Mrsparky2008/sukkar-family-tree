"""
Family-specific configuration — the ONLY file another family needs to edit.

The seven Bsharri families are meant to be able to fork this repo, edit this
one file, and deploy. That only holds if the rule is enforced strictly:

    If a family name, village name, colour, or ancestor name appears anywhere
    outside this file, that is a bug.

Everything here is a plain module-level constant. Secrets are read from the
environment so they are never committed; see .env.example.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Read a .env file into the environment, if one is there.

    Deliberately not python-dotenv: this is a dozen lines, it has no
    dependency to keep working in five years, and real environment variables
    always win so a deployment can override the file without editing it.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


_load_env_file(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

#: Default surname applied to new people. Individuals may override it — women
#: who married into the family keep their own family name in the `people` row.
FAMILY_NAME = "Sukkar"

#: Arabic script rendering of the family name. Optional; used in the bot
#: greeting and the public page heading when present.
FAMILY_NAME_AR = "سكر"

#: Village of origin, for page headings and the bot's introduction.
VILLAGE = "Bsharri"
VILLAGE_AR = "بشري"

#: Shown at the top of the public page and in the bot's /start message.
TAGLINE = f"The {FAMILY_NAME} family of {VILLAGE}, Lebanon"


# ---------------------------------------------------------------------------
# Branches — the founding brothers
# ---------------------------------------------------------------------------
#
# One entry per founding ancestor. Each becomes a row in the `branches` table,
# and every descendant is assigned to a branch by walking their patriline up to
# one of these people (see db.assign_branches).
#
# `key`                 stable identifier, referenced by seed.py. Never reuse or
#                       renumber these — they are how seed data names a branch.
# `given_name`          the founder's given name, used to locate them at seed time.
# `display_name`        shown in the branch filter on the public page.
# `admin_telegram_id`   the relative who reviews this branch's submission queue.
#                       Leave None until that person has messaged the bot; a
#                       branch with no admin falls through to the super admins.
# `colour`              node colour for this branch in the public graph view.
#
# ---------------------------------------------------------------------------
# REPLACE THE ENTRIES BELOW WITH THE SEVEN REAL BROTHERS.
# They are placeholders that match the worked example in seed.py so the repo
# runs out of the box. Real names go here and in seed.py together.
# ---------------------------------------------------------------------------

FOUNDING_ANCESTORS = [
    {
        "key": "youssef",
        "given_name": "Youssef",
        "display_name": "Line of Youssef",
        "admin_telegram_id": None,
        "colour": "#3d6b8f",
    },
    {
        "key": "boutros",
        "given_name": "Boutros",
        "display_name": "Line of Boutros",
        "admin_telegram_id": None,
        "colour": "#9c6644",
    },
    # {"key": "...", "given_name": "...", "display_name": "Line of ...",
    #  "admin_telegram_id": None, "colour": "#6b8f3d"},
]

#: Fallback palette, used if a branch above omits `colour` or more branches
#: exist than colours defined.
BRANCH_PALETTE = [
    "#3d6b8f",
    "#9c6644",
    "#6b8f3d",
    "#8f3d6b",
    "#3d8f8a",
    "#8f7a3d",
    "#5c4f8f",
]


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

COLOURS = {
    "background": "#faf8f4",
    "surface": "#ffffff",
    "text": "#2b2118",
    "text_muted": "#6f665c",
    "border": "#e0d8cc",
    "accent": "#b07d3f",
    "node_male": "#3d6b8f",
    "node_female": "#a35a6b",
    "node_unknown": "#8a8178",
    "edge_parent": "#bdb4a7",
    "edge_union": "#b07d3f",
    "highlight": "#b07d3f",
    "ok": "#3f7d4f",
    "warn": "#b0783f",
    "error": "#a3453d",
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

#: Single SQLite file. Override with the FAMILY_TREE_DB environment variable
#: when deploying, so the database lives outside the checkout.
DATABASE_PATH = Path(
    os.environ.get("FAMILY_TREE_DB", BASE_DIR / "data" / "family.db")
)


# ---------------------------------------------------------------------------
# Secrets — from the environment, never committed
# ---------------------------------------------------------------------------


def _env_id_list(name: str) -> list[int]:
    """Parse a comma-separated list of Telegram user IDs from the environment."""
    raw = os.environ.get(name, "")
    return [int(part) for part in (p.strip() for p in raw.split(",")) if part]


#: Step 2. From @BotFather.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

#: The bot's @username, used in the "share this with a relative" message that
#: gets forwarded around WhatsApp. Without the @.
TELEGRAM_BOT_USERNAME = "Sukar_bot"

#: Telegram users with cross-branch review powers. These are trusted from
#: config rather than the database so a locked-out super admin can always be
#: restored by editing the environment.
SUPER_ADMIN_TELEGRAM_IDS = _env_id_list("SUPER_ADMIN_TELEGRAM_IDS")

#: Step 3. Password for the Flask admin interface.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

#: Step 3. Flask session signing key.
SECRET_KEY = os.environ.get("SECRET_KEY", "")


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------

#: Similarity threshold (0-1) above which a submission is flagged as a probable
#: duplicate of an existing person. Flagging only — nothing is ever auto-merged
#: or auto-rejected; an admin decides. Lower it to catch more, at the cost of
#: more false positives in the queue.
FUZZY_MATCH_THRESHOLD = 0.82

#: Submissions shown per page in the admin queue.
QUEUE_PAGE_SIZE = 25

#: Where the public read-only view is published. The bot's "View the tree"
#: option sends this link. Leave empty until step 4 is deployed; the bot then
#: says the tree is not published yet instead of sending a dead link.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")

#: How many of a contributor's own past submissions the "Fix something I
#: submitted" flow offers. Telegram inline keyboards get unusable past about
#: this many rows.
FIXABLE_SUBMISSION_LIMIT = 10
