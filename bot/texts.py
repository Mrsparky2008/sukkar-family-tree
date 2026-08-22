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
    f"Welcome.\n\n"
    f"This is the {FAMILY} family tree of {VILLAGE}. Relatives add the names "
    f"they know, the family checks them, and the tree grows.\n\n"
    f"Two questions and you're in."
)

ASK_SELF_GIVEN = "What's your first name?"

#: The family name is asked, never assumed. There are several spellings of the
#: same family — they are the same people, written down by different clerks in
#: different countries — and the one on a person's passport is theirs to keep.
ASK_SELF_FAMILY = "And how do you spell your family name?"
FAMILY_OTHER = "Something else"
ASK_FAMILY_OTHER = "How do you spell it?"

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
    "Thanks — you're in. One of the family will double-check the details, "
    "but there's nothing to wait for."
)

IDENTITY_ALREADY_LINKED = "You're already in as {name}."


# --- the guided tour -------------------------------------------------------
#
# A new contributor shouldn't be dropped in front of a menu. The bot leads:
# parents, brothers and sisters, their own family, then a generation up on
# each side — grandparents, uncles and aunties and who each of them married.
# Every step can be skipped, the menu is always one tap away, and nothing is
# ever asked twice.

TOUR_LETS_GO = "Let's do it"
TOUR_SKIP = "Skip for now"
TOUR_MENU = "I'll choose myself"
TOUR_NONE_SIBLINGS = "No brothers or sisters"
TOUR_NONE_FAMILY = "Not married"
TOUR_GO_ON = "Great — type their names."

TOUR_OWN_PARENTS = "Let's build your corner of the tree, starting with your parents."

TOUR_OWN_SIBLINGS = (
    "Do you have brothers and sisters? Type them all in one go, like:\n\n"
    "My brothers Tony and Joe, and my sister Mary. Tony married Rima."
)

TOUR_OWN_FAMILY = (
    "What about your own family — are you married? Kids?\n\n"
    "My wife is Laila, our kids are Sami, Rita and Joe."
)


def tour_grandparents(parent: str) -> str:
    return (
        f"Now a generation up: {parent}'s parents — your grandparents. "
        f"Do you know their names?"
    )


def tour_parent_siblings(parent: str, sex: str | None) -> str:
    his = "His" if sex == "M" else "Her" if sex == "F" else "Their"
    return (
        f"Did {parent} have brothers and sisters? They're your uncles and "
        f"aunties — and who each of them married matters just as much:\n\n"
        f"{his} brother Tony married Rima, {his.lower()} sister Mary "
        f"married Joseph Karam."
    )


TOUR_DONE = (
    "That's a brilliant start — {count} people so far. Keep going in any "
    "direction you like: cousins, their kids, in-laws, the lot. Every name "
    "you remember is one the tree keeps forever."
)


# --- the menu --------------------------------------------------------------

MENU_PROMPT = "What would you like to do?"

#: The cursor. Every flow adds relatives *for* somebody, and that somebody
#: starts as the contributor but moves as they work up the family.
SUBJECT_HEADING = "Adding relatives for: {name}"
#: When the bot knows how this person relates to the contributor, it says so.
#: "Adding relatives for: Toufic" about your own brother reads as if the bot
#: has no idea who he is — and a bot that sounds unsure invites doubt about
#: everything already entered.
SUBJECT_HEADING_RELATED = "Adding relatives for: {name} — {relation}"
SUBJECT_YOU = "you"

#: What a relative is called, by kind and sex. Lives here with the rest of
#: the wording so every phrase the bot uses for kinship comes from one table.
KIN_WORDS: dict[str, dict[str | None, str]] = {
    "parent": {"M": "father", "F": "mother", None: "parent"},
    "sibling": {"M": "brother", "F": "sister", None: "brother or sister"},
    "partner": {"M": "husband", "F": "wife", None: "husband or wife"},
    "child": {"M": "son", "F": "daughter", None: "child"},
    "grandparent": {"M": "grandfather", "F": "grandmother", None: "grandparent"},
    "parent_sibling": {"M": "uncle", "F": "aunt", None: "uncle or aunt"},
}


def kin_word(kind: str, sex: str | None) -> str:
    words = KIN_WORDS[kind]
    return words.get(sex) or words[None]


def relation_phrase(kin: str, owner: str | None) -> str:
    """"your brother" — or "Hanna's brother" when the cursor sits deeper in."""
    return f"your {kin}" if not owner else f"{owner}'s {kin}"


def parents_already_down(subject: str | None, parents: str) -> str:
    whose = "Your" if not subject else f"{subject}'s"
    return f"{whose} parents are already down: {parents}."

MENU_SWITCH = "Somebody else in the family"

SWITCH_PICK = (
    "Who are we adding relatives for?\n\n"
    "Pick anyone you know about — their parents, their brothers and sisters, "
    "whoever you can remember."
)

SWITCH_NOBODY = (
    "There's nobody else I can point you at yet. Add a few relatives first "
    "and they'll show up here."
)

SWITCHED = "Right — {name}."

MENU_ADD_PARENTS = "Add my parents"
MENU_ADD_SIBLING = "Add a sibling"
MENU_ADD_SPOUSE = "Add a spouse"
MENU_ADD_CHILD = "Add a child"
MENU_FIX = "Fix something I submitted"
MENU_VIEW = "View the tree"


# --- following the cursor --------------------------------------------------
#
# Every prompt below has to work whether the contributor is talking about
# themselves or about their great-grandfather. "your father" is wrong the
# moment the cursor moves, and a bot that gets that wrong reads as broken.


def possessive(subject: str | None) -> str:
    """"your" when we are talking about the contributor, "Youssef's" when not."""
    return "your" if not subject else f"{subject}'s"


def menu_labels(subject: str | None) -> dict[str, str]:
    if not subject:
        return {
            "parents": MENU_ADD_PARENTS,
            "sibling": MENU_ADD_SIBLING,
            "spouse": MENU_ADD_SPOUSE,
            "child": MENU_ADD_CHILD,
        }
    return {
        "parents": f"Add {subject}'s parents",
        "sibling": f"Add a brother or sister of {subject}",
        "spouse": f"Add {subject}'s husband or wife",
        "child": f"Add a child of {subject}",
    }


def ask_father(subject: str | None) -> str:
    return f"What's {possessive(subject)} father's first name?"


def ask_mother(subject: str | None) -> str:
    return f"And {possessive(subject)} mother's first name?"


def ask_mother_family(subject: str | None) -> str:
    who = "your mother" if not subject else f"{subject}'s mother"
    return f"What was {who}'s family name before she married?"


def ask_person_sex(name: str, owner: str | None, kind: str) -> str:
    """"Is Nawal your brother or sister?" — asked only when a dictated list
    never said. A name with no sex draws the tree wrong quietly, which is
    worse than one more question."""
    pair = f"{KIN_WORDS[kind]['M']} or {KIN_WORDS[kind]['F']}"
    return f"Is {name} {possessive(owner)} {pair}?"


SEX_NOT_UNDERSTOOD = "Just tap one of the buttons, or Skip if you're not sure."


def ask_same_person(name: str, match: str) -> str:
    """The bot noticed a likely match and the person who would know is right
    here. Ask now, while they are looking at it — their answer rides along
    with the submission as evidence for whoever reviews it."""
    return (
        f"Quick check — I might already know about {name}.\n\n"
        f"Is this the same person as {match}?"
    )


SAME_PERSON = "Same person"
DIFFERENT_PERSON = "No, different person"
NOT_SURE = "Not sure"

#: How a match reads inside the question.
MATCH_PENDING_SUFFIX = " (added recently, waiting for review)"


def ask_sibling_sex(subject: str | None) -> str:
    if not subject:
        return ASK_SIBLING_SEX
    return f"Was this a brother or a sister of {subject}?"


def ask_spouse_sex(subject: str | None) -> str:
    if not subject:
        return ASK_SPOUSE_SEX
    return f"Was this {subject}'s husband or wife?"


def ask_child_sex(subject: str | None) -> str:
    if not subject:
        return ASK_CHILD_SEX
    return f"Was this {subject}'s son or daughter?"


# --- shared prompts --------------------------------------------------------

#: Kept for the edit screen. Names are no longer confirmed one at a time —
#: it doubled every interaction, and everything is editable before it sends.
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
    "Saved. One of the family will look it over before it shows on the tree."
)

#: After a save, offer the obvious next step rather than dropping back to a
#: menu. This is what walks a contributor up the generations instead of
#: leaving them at their own parents.
CLIMB_PARENTS = "Do you know {name}'s parents?"

#: For a sibling or a child, their parents are already on the chart — asking
#: would be absurd and answering would duplicate them. The useful next
#: question is their own household.
CLIMB_FAMILY = "Does {name} have {spouse} or children?"
CLIMB_SIBLINGS = "Did {name} have brothers or sisters?"
CLIMB_YES = "Yes, let's do that"
CLIMB_NO = "No, that's all I know"

DEAD_END = (
    "That's genuinely useful — most people can't get that far back.\n\n"
    "If you ever sit down with someone older who remembers more, come back "
    "and we'll keep going."
)

SOURCE_BUTTON = "Someone else told me this"
SOURCE_ASK = (
    "Who told you? A name is enough — it goes on the record so we know where "
    "this came from."
)

CANCELLED = "Dropped that one. Nothing was saved."

CONFIRM_SUBMISSION = "Here's what I'll send:"

# --- the basket ------------------------------------------------------------

ADDED = "Got it — {summary}."

# --- a whole family in one message ----------------------------------------

DICTATED = (
    "That's {count} people — thank you, that's the fastest way to do this.\n\n"
    "Here's what I understood. Tap anything that's wrong."
)

DICTATED_ONE = "Got it. Here's what I understood — tap it if it's wrong."

#: Only ever list what was actually guessed. Warning about a guess the bot
#: did not make teaches people to ignore the warnings.
DICTATED_UNSURE = "\n\nI guessed at: {reasons}. Worth a look."
DICTATED_UNSURE_ONE = "\n\nOne thing I guessed at: {reasons}. Worth a look."

DICTATED_SUBJECT = "These are {name}'s relatives, so that's who I've put them under."

DICTATED_ABOUT_OTHERS = "Some of these belong to {names} rather than to you."

DICTATED_UNKNOWN_PEOPLE = (
    "\n\nI couldn't place {names} — nobody by that name is in the tree or in "
    "your list yet. Add them first and send this again."
)

DICTATED_REMARK = "Noted about {name}: {remark}."

DICTATED_SUBJECT_UNKNOWN = (
    "You've told me about {name}'s family, but I don't know who {name} is yet.\n\n"
    "Add {name} first — then tell me this again and it'll slot straight in."
)

SKETCH_HEADING = "What you've built so far:"

SKETCH_EMPTY = (
    "Nothing on the sketch yet — everything you'd added has already been "
    "sent for review. Add somebody and it starts again."
)

MENU_TYPE_HINT = (
    "Use a button above — or just type the names out and I'll sort them:\n\n"
    "    my father's parents are Toufic and Cilene\n"
    "    his sisters are Dibeh, Sonia and Rima"
)

DICTATED_NOTHING = (
    "I couldn't pick any names out of that, sorry.\n\n"
    "Try it like this:\n"
    "    his parents are Toufic and Cilene\n"
    "    his sisters are Dibeh, Sonia and Saide"
)

REVIEW_SEND = "Review and send ({count})"

REVIEW_HEADING = (
    "Here's everything you've given me. Check the spelling — tap any line to "
    "change it."
)

REVIEW_EMPTY = "You haven't added anyone yet."

SEND_ALL = "Send all {count}"

EDIT_ASK = "What should {name} be?"

EDITED = "Changed to {name}."

REMOVE = "Remove this one"

REMOVED = "Removed."

ADD_MORE = "Add someone else"

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
    "Passed on. Corrections get the same once-over as anything else, so "
    "nothing changes on the tree until someone's had a look."
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

RELAY_SUGGEST = (
    "You've just added {name}. If {he_she} uses Telegram, send them this — "
    "they'll be recognised straight away, because you've already put them in:"
    "\n\nhttps://t.me/{bot}"
)

SHARE = (
    f"Pass this on to any {FAMILY} relative:\n\n"
    f"https://t.me/{config.TELEGRAM_BOT_USERNAME}\n\n"
    f"The more people who add the names they know, the fewer gaps there are."
)

HELP = (
    "Add relatives you know, one at a time. Everything you send is checked by "
    "one of the family before it appears on the tree.\n\n"
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
