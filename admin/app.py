"""
The review interface. Step 3 of the spec.

Flask, password protected, and deliberately boring: server-rendered HTML,
no JavaScript framework, no build step. It reuses the exact same approve /
merge / reject logic as the command line (`review.py`), so there is one
implementation of the rules and two ways to press the buttons.

Who sees what
    Branch admins see only their own branch's queue — they are the people who
    actually know those relatives. Super admins (config.SUPER_ADMINS) see
    everything, which is where cross-branch merges happen.

How you get in
    One shared password (ADMIN_PASSWORD) plus your Telegram ID. The password
    gates the door; the ID decides your scope and goes on the audit trail.
    An ID that is not an admin of anything is refused even with the password.

Nothing here writes to the family data except through review.approve /
merge / reject — the same privileged path an admin on the command line uses.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)

import config
import db
import review
import submissions


def create_app(database_path=None) -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = database_path  # None means config.DATABASE_PATH
    app.secret_key = config.SECRET_KEY or secrets.token_hex(32)
    if not config.SECRET_KEY:
        # Sessions die on restart; harmless for a pilot, worth saying once.
        app.logger.warning("SECRET_KEY not set — logins will not survive a restart")

    # ---- one connection per request ---------------------------------------

    def conn() -> sqlite3.Connection:
        if "conn" not in g:
            g.conn = db.connect(app.config["DATABASE"])
            db.init_db(g.conn)
        return g.conn

    @app.teardown_appcontext
    def close(_exc):
        connection = g.pop("conn", None)
        if connection is not None:
            connection.close()

    # ---- who is allowed in ------------------------------------------------

    def scope() -> list[int] | None:
        """Branch ids this admin may review. None means all of them."""
        return db.admin_branch_ids(conn(), session["admin_id"])

    def is_admin(telegram_id: int) -> bool:
        if db.is_super_admin(telegram_id, conn()):
            return True
        branches = db.admin_branch_ids(conn(), telegram_id)
        return branches is None or bool(branches)

    def logged_in(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "admin_id" not in session:
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapped

    def check_csrf():
        if request.form.get("_csrf") != session.get("_csrf"):
            abort(400)

    @app.template_filter("describe")
    def describe_filter(payload):
        return submissions.describe(payload)

    @app.context_processor
    def globals_for_templates():
        return {
            "family": config.FAMILY_NAME,
            "village": config.VILLAGE,
            "csrf": session.get("_csrf", ""),
        }

    # ---- login ------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not config.ADMIN_PASSWORD:
            return (
                "ADMIN_PASSWORD is not set. Put it in .env and restart — "
                "the review interface stays closed until it has a password.",
                503,
            )
        error = None
        if request.method == "POST":
            password = request.form.get("password", "")
            raw_id = request.form.get("telegram_id", "").strip()
            if not hmac.compare_digest(password, config.ADMIN_PASSWORD):
                error = "Wrong password."
            elif not raw_id.isdigit():
                error = "Your Telegram ID is a number — message @userinfobot to get it."
            elif not is_admin(int(raw_id)):
                error = (
                    "That ID isn't an admin of any branch. If it should be, "
                    "a super admin needs to add it first."
                )
            else:
                session.clear()
                session["admin_id"] = int(raw_id)
                session["_csrf"] = secrets.token_hex(16)
                return redirect(url_for("queue"))
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- the queue --------------------------------------------------------

    def scoped_submissions(status):
        branches = scope()
        if branches is None:
            return db.list_submissions(conn(), status=status)
        rows = []
        for branch_id in branches:
            rows.extend(db.list_submissions(conn(), status=status, branch_id=branch_id))
        rows.sort(key=lambda row: row["id"])
        return rows

    def view_of(row) -> dict:
        """Everything the queue template needs about one submission."""
        payload = db.submission_payload(row)
        matches = review.evidence(conn(), payload, row["id"])[:3]
        for match in matches:
            if match["person_id"] is not None:
                person = db.get_person(conn(), match["person_id"])
                match["parents"] = ", ".join(
                    db.row_display_name(p)
                    for p in db.get_parents(conn(), match["person_id"])
                )
        teller = payload.get("submitted_by") or {}
        return {
            "id": row["id"],
            "status": row["status"],
            "when": row["created_at"],
            "summary": submissions.describe(payload),
            "details": submissions.detail_lines(payload)[1:],
            "people": payload.get("people") or [],
            "teller": teller.get("label") or f"telegram {row['telegram_user_id']}",
            "source": payload.get("source"),
            "note": payload.get("note") if payload.get("kind") == submissions.CORRECTION else None,
            "kind": payload.get("kind"),
            "matches": matches,
            "review_note": row["review_note"],
        }

    @app.route("/")
    @logged_in
    def queue():
        status = request.args.get("status", "pending")
        if status not in ("pending", "approved", "merged", "rejected", "all"):
            status = "pending"
        rows = scoped_submissions(None if status == "all" else status)
        return render_template(
            "queue.html",
            items=[view_of(row) for row in rows],
            status=status,
            admin_id=session["admin_id"],
            super_admin=db.is_super_admin(session["admin_id"], conn()),
        )

    # ---- the four decisions ------------------------------------------------

    def act(submission_id: int, action):
        """Run one decision, translating Blocked into a message, not a crash."""
        try:
            action()
            conn().commit()
        except review.Blocked as problem:
            conn().rollback()
            flash(str(problem), "problem")
        return redirect(url_for("queue"))

    @app.route("/submission/<int:submission_id>/approve", methods=["POST"])
    @logged_in
    def approve(submission_id):
        check_csrf()
        force = request.form.get("anyway") == "yes"
        edits = {}
        for key, value in request.form.items():
            # edit-<index>-<field>, from the inline edit fields.
            if key.startswith("edit-"):
                _, index, field = key.split("-", 2)
                edits.setdefault(int(index), {})[field] = value
        return act(
            submission_id,
            lambda: review.approve(
                conn(), submission_id, reviewed_by=session["admin_id"],
                force=force, edits=edits or None,
            ),
        )

    @app.route("/submission/<int:submission_id>/merge", methods=["POST"])
    @logged_in
    def merge(submission_id):
        check_csrf()
        into = request.form.get("into", "")
        if not into.isdigit():
            flash("Merge needs the number of the person they already are.", "problem")
            return redirect(url_for("queue"))
        return act(
            submission_id,
            lambda: review.merge(
                conn(), submission_id, int(into), reviewed_by=session["admin_id"]
            ),
        )

    @app.route("/submission/<int:submission_id>/reject", methods=["POST"])
    @logged_in
    def reject(submission_id):
        check_csrf()
        note = request.form.get("note", "").strip()
        if not note:
            flash(
                "Say why, in a few words — the person who sent it can see the reason.",
                "problem",
            )
            return redirect(url_for("queue"))
        return act(
            submission_id,
            lambda: review.reject(
                conn(), submission_id, reviewed_by=session["admin_id"], note=note
            ),
        )

    # ---- people ------------------------------------------------------------

    @app.route("/person/<int:person_id>")
    @logged_in
    def person(person_id):
        row = db.get_person(conn(), person_id)
        if row is None:
            abort(404)
        relatives = {
            "Parents": db.get_parents(conn(), person_id),
            "Married": db.get_partners(conn(), person_id),
            "Brothers and sisters": db.get_siblings(conn(), person_id),
            "Children": db.get_children(conn(), person_id),
        }
        return render_template(
            "person.html",
            person=row,
            display=db.display_name_with_spellings(conn(), row),
            relatives={
                title: [(r["id"], db.row_display_name(r)) for r in rows]
                for title, rows in relatives.items()
            },
            claims=db.provenance(conn(), person_id),
        )

    @app.route("/find")
    @logged_in
    def find():
        needle = request.args.get("q", "").strip()
        hits = []
        if needle:
            target = needle.casefold()
            for row in db.get_people(conn()):
                haystack = " ".join(
                    part.casefold()
                    for part in (row["given_name"], row["family_name"],
                                 row["also_known_as"], row["given_name_ar"],
                                 db.row_display_name(row))
                    if part
                )
                if target in haystack or target == str(row["id"]):
                    parents = ", ".join(
                        db.row_display_name(p)
                        for p in db.get_parents(conn(), row["id"])
                    )
                    hits.append((row["id"], db.display_name_with_also_known_as(row), parents))
        return render_template("find.html", q=needle, hits=hits)

    return app
