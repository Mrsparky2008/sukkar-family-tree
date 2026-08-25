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
    f"A few quick questions and you're in."
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

ASK_SELF_SEX = "And are you a man or a woman? It's how the tree draws you."

#: The house question. Asked of everyone at sign-in, because a house passes
#: father to son and everyone added afterwards inherits it — one tap here
#: saves the question being asked of a dozen relatives later.
ASK_SELF_HOUSE = "And which house are you from?"

ASK_SELF_HOUSE_WHY = (
    "The family divides into houses that pass from father to son. Knowing "
    "yours keeps you with the right relatives — and stops you being mixed "
    "up with someone who shares your name in another house."
)

HOUSE_OTHER = "Another house"
HOUSE_UNKNOWN = "I'm not sure"
ASK_HOUSE_OTHER = "Which house? Type the name as your family says it."

#: Shown on a person's own record and in the review desk. Never inside a
#: name — see the note on the `house` field in submissions.py.
def house_line(name: str) -> str:
    return f"House: {name}"

FATHER_NOT_FAMILY = (
    "That's the family name — I need your father's FIRST name. "
    "What was he called?"
)
SELF_MAN = "Man"
SELF_WOMAN = "Woman"

IDENTITY_GUESS = (
    "I think you might already be on the tree. Is one of these you?"
)

IDENTITY_NONE_OF_THESE = "None of these are me"

#: Appended to the guess when the tree is published: answer by looking,
#: not from memory. A wrong "none of these" makes a duplicate person.
IDENTITY_GUESS_LOOK = (
    "\n\nNot sure? Tap the tree button under a name to look at them — "
    "their parents and family are right there."
)


def check_on_tree(person_id: int) -> str:
    return f"🌳 Look at #{person_id} on the tree"

IDENTITY_CONFIRMED = "Good to meet you, {name}."

IDENTITY_QUEUED = (
    "Thanks — you're in. One of the family will double-check the details, "
    "but there's nothing to wait for."
)

IDENTITY_ALREADY_LINKED = "You're already in as {name}."

#: Somebody who reached the menu without introducing themselves. Everything
#: here is added *for* a person, so there is nothing useful they can do yet.
SIGN_IN_FIRST = (
    "Before we start — I need to know who you are, so what you add lands "
    "in the right place. It's a few quick questions."
)


# --- the guided tour -------------------------------------------------------
#
# A new contributor shouldn't be dropped in front of a menu. The bot leads:
# parents, brothers and sisters, their own family, then a generation up on
# each side — grandparents, uncles and aunties and who each of them married.
# Every step can be skipped, the menu is always one tap away, and nothing is
# ever asked twice.

TOUR_LETS_GO = "Yes — let's add them"
TOUR_SKIP = "Skip for now"
TOUR_MENU = "I'll choose myself"
TOUR_NONE_SIBLINGS = "No brothers or sisters"
TOUR_NOT_MARRIED = "Not married"
TOUR_NO_CHILDREN = "No children"
TOUR_NONE = "No"

TOUR_OWN_PARENTS = "Let's start building your family — your parents first."

TOUR_OWN_SIBLINGS = (
    "Do you have brothers and sisters? I'll take them one at a time."
)

TOUR_OWN_SPOUSE = "Are you married?"

TOUR_OWN_CHILDREN = "Any children?"


def tour_grandparents(parent: str) -> str:
    return (
        f"Now a generation up: {parent}'s parents — your grandparents. "
        f"Do you know their names?"
    )


def tour_parent_siblings(parent: str, sex: str | None) -> str:
    return (
        f"Did {parent} have brothers and sisters? They're your uncles and "
        f"aunties — one at a time, and who each of them married counts too."
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
MENU_VIEW = "See my corner of the tree"
#: A plain link button at the end of every menu: the whole tree, one tap,
#: straight into the browser. Only shown once the tree is published.
SEE_TREE = "🌳 See the whole tree"


# --- following the cursor --------------------------------------------------
#
# Every prompt below has to work whether the contributor is talking about
# themselves or about their great-grandfather. "your father" is wrong the
# moment the cursor moves, and a bot that gets that wrong reads as broken.


def possessive(subject: str | None) -> str:
    """"your" when we are talking about the contributor, "Youssef's" when not."""
    return "your" if not subject else f"{subject}'s"


def tagged(label: str | None, person_id: int | None) -> str | None:
    """"Toufic (#29)" — the name for the human, the number for the record.

    Names are for humans; the number is the permanent reference. Shown
    together wherever the bot talks about somebody already on the tree, so
    the family learns the numbers by seeing them, never by being taught."""
    if not label or not person_id or "(#" in label:
        return label
    return f"{label} (#{person_id})"


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


def ask_person_sex_guessed(name: str, owner: str | None, kind: str, sex: str) -> str:
    """When everyone on the tree with this name is one sex, lead with the
    guess so the common case is a single confirming tap — and the guess is
    always shown, never silently applied."""
    word = KIN_WORDS[kind][sex]
    return f"{name} — {possessive(owner)} {word}, I'm guessing?"


GUESS_YES = "Yes — {word}"
GUESS_NO = "No — {word}"


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


def ask_meant_yourself(name: str) -> str:
    """They typed their own name in the third person. Usually themselves —
    but half the family shares a handful of names, so ask, don't assume."""
    return f"Quick check — by {name}, did you mean yourself?"


MEANT_MYSELF = "Yes, that's me"
MEANT_SOMEONE_ELSE = "No, a different {name}"

SELF_MISREAD = (
    "Righto — I've taken those out so they don't land on you. Tell me about "
    "that {name} again with their father's name, so I know which {name} "
    "you mean: \"{name} son of Tony married...\""
)

#: How a match reads inside the question. "Waiting for review" meant nothing
#: to first-timers — they don't know there's a review. Say who and why.
MATCH_PENDING_SUFFIX = " (someone in the family added them recently)"


#: The same-father question startles people who don't know why it's asked —
#: it can read as the bot doubting their family. Saying the reason out loud
#: ("I ask everyone") turns it from an accusation into a formality.
_SAME_FATHER_WHY = (
    "I ask everyone — some brothers and sisters share just the one parent, "
    "and the tree has to draw the line to the right father."
)


def ask_same_father(subject: str | None) -> str:
    whose = "you" if not subject else subject
    return f"Same father as {whose}?\n\n({_SAME_FATHER_WHY})"


YES_WORD = "Yes"
NO_WORD = "No"

ASK_SIBLING_FATHER = "What's {his_her} father's first name?"


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

# --- the check before anything sticks --------------------------------------

CONFIRM_CHECK = "Just checking:"
CONFIRM_CORRECT = "Correct"
CONFIRM_CHANGE = "Change it"

# --- what next -------------------------------------------------------------
#
# After each person, the obvious next moves — concrete, named, one tap.

NEXT_PROMPT = "What next?"

# --- counted capture --------------------------------------------------------
#
# "How many brothers? How many sisters?" — then exactly that many name
# questions, sexes already known, no add-another taps, one read-back at the
# end. The counts are the shortcut AND the map of what to ask next.

COUNT_MORE = "More"
COUNT_ASK_NUMBER = "Type the number."
COUNT_NOT_A_NUMBER = "Just a number — how many?"

_ORDINALS = [
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth",
]


def ordinal(n: int) -> str:
    return _ORDINALS[n - 1] if 1 <= n <= len(_ORDINALS) else f"{n}th"


def how_many(subject: str | None, kin_plural: str) -> str:
    whose = "do you" if not subject else f"did {subject}"
    return f"How many {kin_plural} {whose} have?"


def counted_name(subject: str | None, position: int, kin: str) -> str:
    return f"{possessive(subject).capitalize()} {ordinal(position)} {kin}'s first name?"


def ask_same_father_all(subject: str | None, count: int) -> str:
    whose = "you" if not subject else subject
    lead = "Same father as" if count == 1 else "All the same father as"
    return f"{lead} {whose}?\n\n({_SAME_FATHER_WHY})"


COUNTED_NONE_NOTED = "Righto — noted."
NEXT_ANOTHER_SIBLING = "Add another brother or sister"
NEXT_ANOTHER_CHILD = "Add another child"
NEXT_SPOUSE_OF = "Add {name}'s husband or wife"
NEXT_CHILDREN_OF = "Add {name}'s children"
NEXT_CHILDREN_MINE = "Add your children"
NEXT_CHILDREN_COUPLE = "Add {a} and {b}'s children"
NEXT_PARENTS_OF = "Add {name}'s parents"
NEXT_SIBLINGS_OF = "Add {name}'s brothers and sisters"

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

SKETCH_HEADING = "Your corner of the tree so far:"

SKETCH_EMPTY = (
    "Nothing to draw for you yet — add a few relatives and this becomes "
    "your corner of the tree."
)

MENU_TYPE_HINT = (
    "Use a button above — or just type the names out and I'll sort them:\n\n"
    "    my father's parents are Toufic and Seleneh\n"
    "    his sisters are Dibeh, Sonia and Rima"
)

DICTATED_NOTHING = (
    "I couldn't pick any names out of that, sorry.\n\n"
    "Try it like this:\n"
    "    his parents are Toufic and Seleneh\n"
    "    his sisters are Dibeh, Sonia and Saide"
)

REVIEW_SEND = "Review and send ({count})"

REVIEW_HEADING = (
    "Here's everything you've given me. Check the spelling — tap any line to "
    "change it."
)

REVIEW_EMPTY = "You haven't added anyone yet."

SEND_ALL = "Send all {count}"

EDIT_ASK = (
    "Fixing {name}.\n\n"
    "Type the correct first name and I'll swap it in — or remove the "
    "whole line."
)

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


def ask_father_family(subject: str | None) -> str:
    who = "your father" if not subject else f"{subject}'s father"
    return f"And how is {who}'s family name spelled?"


#: The father was assumed to carry the family's own name. Usually he does —
#: but when somebody belongs to this family through their mother, he does
#: not, and the assumption then flows down every one of his descendants.
FATHER_FAMILY_OTHER = "Another family"
ASK_FATHER_FAMILY_OTHER = "Which family? Type the name as they spell it."


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

FIX_PICK = "Which one needs fixing?"

FIX_ASK_NOTE = (
    "What should it say instead?\n\n"
    "Write it however you like — a person reads these."
)

#: Corrections about anyone on the tree, not just what this contributor
#: sent. The numbers are what make it safe: half the family shares a
#: handful of names, and #12 can only ever mean one man.
FIX_TREE = "Something on the tree"

FIX_TREE_ASK = (
    "Tell me what's wrong, in your own words — a person reads these, and "
    "nothing changes until they've checked it.\n\n"
    "Use the numbers beside the names when you can, so two people with "
    "the same name never get mixed up:\n"
    "    remove the marriage between #12 and #15\n"
    "    #46 is spelled Marie, not Mary"
)


def fix_tree_about(people: str) -> str:
    return f"That's about: {people}."


def fix_id_unknown(number: str) -> str:
    return (
        f"There's nobody numbered #{number} on the tree — worth checking "
        f"it against the chart. Send the correction again with the right "
        f"number."
    )


def ask_person_id(name: str, options: str) -> str:
    """More than one person answers to this name; only a number settles it."""
    return (
        f"There's more than one {name} on the tree, so the name alone "
        f"could mean either of them:\n\n{options}\n\n"
        f"Can you send the correction again with the person's number in "
        f"it? You'll find it beside their name in the family tree."
    )

FIX_SAVED = (
    "Passed on. Corrections get the same once-over as anything else, so "
    "nothing changes on the tree until someone's had a look."
)


# --- the review desk --------------------------------------------------------
#
# Approving from a phone: /review walks an admin through the queue one item
# at a time, with the evidence and the safe next moves as buttons.

REVIEW_NOT_ADMIN = "The review desk is for the family's admins."


def review_heading(item_id: int, remaining: int) -> str:
    waiting = "1 waiting" if remaining == 1 else f"{remaining} waiting"
    return f"Reviewing #{item_id} — {waiting}."


def review_match_line(who: str, why: str) -> str:
    return f"Looks like {who} — {why}."


REVIEW_APPROVE = "✅ Approve — onto the tree"
REVIEW_MERGE = "Same person — merge"
REVIEW_FORCE = "Different person — add anyway"
REVIEW_REJECT = "❌ Reject"
REVIEW_SKIP = "Skip for now"
REVIEW_STOP = "Stop reviewing"


def review_done(count: int) -> str:
    if not count:
        return "Review closed — nothing changed."
    handled = "1 item" if count == 1 else f"{count} items"
    return (
        f"Review closed — {handled} handled. "
        f"The family link refreshes itself."
    )


REVIEW_QUEUE_CLEAR = "Nothing waiting — the queue is clear."


def review_check_line(who: str, verdict: str | None) -> str:
    word = {
        "stands": "stands by their version",
        "concedes": "says the new claim is right",
        "unsure": "isn't sure",
        None: "was asked, hasn't answered yet",
    }[verdict]
    return f"Family check: {who} — {word}."


# --- green and yellow -------------------------------------------------------
#
# First-hand, uncontested claims from an admitted contributor go straight
# onto the tree, permanent numbers and all. A claim that overlaps or
# disagrees with the record sends a question to the person behind the
# standing entry — the family sorts most of it out before any admin has to.

AUTO_APPROVED_ONE = "Straight onto the tree: {name}."

AUTO_APPROVED_MANY = "Straight onto the tree:\n{names}"

QUEUED_FOR_CHECK = (
    "Some of what you added touches people already on the tree, so it gets "
    "a quick once-over before it lands — nothing for you to do."
)


def peer_check_question(asker: str, claim: str, standing: str) -> str:
    return (
        f"Quick family check.\n\n"
        f"{asker} just added: {claim}.\n\n"
        f"The tree already shows {standing}, which came from you.\n\n"
        f"How confident are you in yours? If it needs fixing, the menu's "
        f"\"Fix something I submitted\" opens your entry to change."
    )


PEER_STANDS = "What I said stands"
PEER_CONCEDES = "They're right"
PEER_UNSURE = "Not sure"

PEER_THANKS = {
    "stands": "Noted — your version stands on the record.",
    "concedes": "Noted — that goes with the claim for the reviewer.",
    "unsure": "Noted, honestly the most useful answer there is.",
}

FIX_THIS = "Something's wrong — fix it"
FIX_BACK = "Back to the list"

FIX_STATUS = {
    "pending": "waiting for review",
    "approved": "approved",
    "merged": "merged with someone already in the tree",
    "rejected": "not accepted",
}


# --- view the tree ---------------------------------------------------------

VIEW_TREE = "The whole family, all the corners joined up:\n{url}"

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
