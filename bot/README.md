# Step 2 — Telegram capture

Run it:

    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=...        # or put it in .env
    python -m bot

Long polling, so there is no webhook, no public hostname, and no certificate to
renew. It runs anywhere Python runs.

## Files

| file | holds |
|---|---|
| `texts.py` | Every string the bot says. One file, so the tone stays consistent and nothing family-specific gets hardcoded. |
| `flows.py` | The conversations, as data: a list of questions and a function turning answers into a payload. No Telegram, no database — testable on its own. |
| `handlers.py` | One generic ask → confirm → advance loop over those flows. |
| `store.py` | The only file that touches the database. |
| `main.py` | Wiring and entry point. |

## Why the flows are data

The spec asks for one question per message and for every typed answer to be
confirmed before use. Written out per flow, that is five near-identical state
machines and about twenty-five conversation states. Written as data, it is five
states and a list of questions per flow. Adding a question is one line.

## Constraint 4

`store.py` is the boundary. It wraps exactly what the bot may do: read people,
read its own submissions, write to `submissions`, and record who a Telegram
user is in `contributors`.

There is deliberately no wrapper for `create_person`, `update_person`, or
`create_union` — and a test fails the build if anything under `bot/` calls one.
If a flow seems to need one, it needs `store.queue()` instead.

Writing to `contributors` is not a violation: that table records who is holding
the phone, not who exists in the family.

## Duplicate flagging

Every queued submission is fuzzy-matched against existing people — on given
name plus the father's given name, scoped to the submitter's branch — and the
best match is stored in `submissions.matched_person_id`.

That is a hint for whoever reviews it. Nothing is auto-merged and nothing is
auto-rejected.

## Testing

`tests/harness.py` fakes Telegram's transport but drives the real handlers
through the real routing table from `build_conversation()`, so the callback
patterns are covered rather than reimplemented.

    python -m unittest discover -s tests -t .

## Not done, deliberately

Conversation state lives in memory. If the bot restarts mid-conversation, the
relative sends `/start` and picks up again. Persisting it would mean a pickle
file to manage and corrupt; the failure it prevents is one extra tap.
