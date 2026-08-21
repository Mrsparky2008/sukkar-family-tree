# Step 2 — Telegram capture

Not built yet.

The bot's only write path is `db.add_submission()`. It must never import
`create_person`, `update_person`, or `create_union` — see constraint 4 in the
spec and the privileged-writes banner in `db.py`.
