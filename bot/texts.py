"""
Every string the bot says.

In one file so the tone stays consistent, so it can be translated in one pass,
and so no family-specific word ends up hardcoded in a handler — the names all
come from config.

Tone: this is a relative talking to a relative. Short sentences, no jargon, no
"submission has been recorded in the queue". Most people reading these are on a
phone and have thirty seconds.
"""

from __future__ import annotations

import config

FAMILY = config.FAMILY_NAME
VILLAGE = config.VILLAGE


# --- first contact ---------------------------------------------------------

WELCOME = (
    f"Ahlan wa sahlan.\n\n"
    f"This is the {FAMILY} family tree of {VILLAGE}. Relatives add the names "
    f"they know, someone from your branch checks them, and the tree grows.\n\n"
    f"Two questions and you're in."
)

ASK_SELF_GIVEN = "What's your first name?"

ASK_SELF_FATHER = "And your father's first name?"

ASK_SELF_FATHER_WHY = (
    "Your father's name is how we tell apart the many people who share yours."
)

IDENTITY_GUESS = (
    "I think you might already be on the tree. Is one of these you?"
)

IDENTITY_NONE_OF_THESE = "None of these are me"

IDENTITY_CONFIRMED = "Good to meet you, {name}."

IDENTITY_QUEUED = (
    "Thank you. Someone from your branch will confirm you — usually within a "
    "day or two.\n\nYou don't have to wait. You can start adding relatives now."
)

IDENTITY_ALREADY_LINKED = "You're already in as {name}."


# --- the menu --------------------------------------------------------------

MENU_PROMPT = "What would you like to do?"

MENU_ADD_PARENTS = "Add my parents"
MENU_ADD_SIBLING = "Add a sibling"
MENU_ADD_SPOUSE = "Add a spouse"
MENU_ADD_CHILD = "Add a child"
MENU_FIX = "Fix something I submitted"
MENU_VIEW = "View the tree"


# --- shared prompts --------------------------------------------------------

CONFIRM_NAME = "{name} — have I spelled that right?"

YES = "Yes"
NO_RETYPE = "No, let me type it again"
SKIP = "I don't know"
CANCEL = "Cancel"
BACK_TO_MENU = "Back to the menu"

RETYPE = "No problem. Type it again."

FIRST_NAME_ONLY = (
    "Just the first name, please — I work out the rest from the family links.\n\n"
    "So {first} rather than {whole}."
)

NAME_TOO_LONG = "That's longer than a first name. Try again?"

NAME_EMPTY = "I didn't catch a name there. Try again?"

SAVED = (
    "Saved. It's with your branch's reviewer now — it won't show on the tree "
    "until they've had a look."
)

CANCELLED = "Dropped that one. Nothing was saved."

CONFIRM_SUBMISSION = "Here's what I'll send:"

SEND_IT = "Send it"
START_OVER = "Start over"


# --- add parents -----------------------------------------------------------

ASK_FATHER_GIVEN = "What's your father's first name?"
ASK_MOTHER_GIVEN = "And your mother's first name?"
ASK_MOTHER_FAMILY = "What was your mother's family name before she married?"


# --- add sibling -----------------------------------------------------------

ASK_SIBLING_SEX = "Is this a brother or a sister?"
SIBLING_BROTHER = "Brother"
SIBLING_SISTER = "Sister"
ASK_SIBLING_GIVEN = "What's {his_her} first name?"


# --- add spouse ------------------------------------------------------------

ASK_SPOUSE_SEX = "Is this your husband or your wife?"
SPOUSE_HUSBAND = "Husband"
SPOUSE_WIFE = "Wife"
ASK_SPOUSE_GIVEN = "What's {his_her} first name?"
ASK_SPOUSE_FAMILY = "What was {his_her} family name before marriage?"


# --- add child -------------------------------------------------------------

ASK_CHILD_SEX = "A son or a daughter?"
CHILD_SON = "Son"
CHILD_DAUGHTER = "Daughter"
ASK_CHILD_GIVEN = "What's {his_her} first name?"


# --- fix something ---------------------------------------------------------

FIX_NOTHING_YET = (
    "You haven't sent anything yet, so there's nothing to fix.\n\n"
    "Add someone first and it'll show up here."
)

FIX_PICK = "Which one needs fixing?"

FIX_ASK_NOTE = (
    "What should it say instead?\n\n"
    "Write it however you like — a person reads these."
)

FIX_SAVED = (
    "Passed on to your branch's reviewer. Corrections go through the same "
    "check as anything else, so nothing changes on the tree until they say so."
)

FIX_STATUS = {
    "pending": "waiting for review",
    "approved": "approved",
    "merged": "merged with someone already in the tree",
    "rejected": "not accepted",
}


# --- view the tree ---------------------------------------------------------

VIEW_TREE = "Here it is:\n{url}"

VIEW_TREE_UNPUBLISHED = (
    "The tree isn't published yet — it goes up once there are enough names on "
    "it to be worth looking at. Keep adding and you'll be part of the first "
    "version."
)


# --- housekeeping ----------------------------------------------------------

SHARE = (
    f"Pass this on to any {FAMILY} relative:\n\n"
    f"https://t.me/{config.TELEGRAM_BOT_USERNAME}\n\n"
    f"The more people who add the names they know, the fewer gaps there are."
)

HELP = (
    "Add relatives you know, one at a time. Everything you send is checked by "
    "someone from your branch before it appears on the tree.\n\n"
    "/start — the menu\n"
    "/share — the link to send to relatives\n"
    "/cancel — stop what we're doing\n\n"
    "There are no dates anywhere in this — just names and who belongs to whom."
)

NOT_UNDERSTOOD = "Use the buttons above, or /start to begin again."

ERROR = (
    "Something went wrong on my end — nothing was lost, and nothing was saved. "
    "Try /start."
)


def his_her(sex: str | None) -> str:
    return {"M": "his", "F": "her"}.get(sex or "", "their")


def he_she(sex: str | None) -> str:
    return {"M": "he", "F": "she"}.get(sex or "", "they")
