"""
Tests for the review interface.

Driven through Flask's test client against a temporary database, with the
password and admin identities set for the duration of each test.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db  # noqa: E402
import seed  # noqa: E402
import submissions as S  # noqa: E402
from admin.app import create_app  # noqa: E402

PASSWORD = "correct-horse"
SUPER = 900
BRANCH_ADMIN = 901
NOBODY = 902


class AdminTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db = Path(self._tmp.name) / "admin.db"

        self._old = (config.ADMIN_PASSWORD, config.SUPER_ADMIN_TELEGRAM_IDS,
                     config.SECRET_KEY, config.DATABASE_PATH)
        config.ADMIN_PASSWORD = PASSWORD
        config.SUPER_ADMIN_TELEGRAM_IDS = [SUPER]
        config.SECRET_KEY = "test-secret"
        config.DATABASE_PATH = self._db

        conn = db.connect(self._db)
        db.init_db(conn)
        self.ids = seed.load(conn)
        # A branch admin over the line of Youssef.
        self.youssef_branch = db.get_branch_by_key(conn, "youssef")["id"]
        self.boutros_branch = db.get_branch_by_key(conn, "boutros")["id"]
        conn.execute(
            "UPDATE branches SET admin_telegram_id = ? WHERE id = ?",
            (BRANCH_ADMIN, self.youssef_branch),
        )
        conn.commit()
        conn.close()

        app = create_app(self._db)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        (config.ADMIN_PASSWORD, config.SUPER_ADMIN_TELEGRAM_IDS,
         config.SECRET_KEY, config.DATABASE_PATH) = self._old
        self._tmp.cleanup()

    # ---- helpers ----------------------------------------------------------

    def conn(self):
        return db.connect(self._db)

    def login(self, telegram_id=SUPER, password=PASSWORD):
        return self.client.post(
            "/login",
            data={"password": password, "telegram_id": str(telegram_id)},
            follow_redirects=True,
        )

    def csrf(self):
        with self.client.session_transaction() as session:
            return session["_csrf"]

    def submit(self, user, branch_id=None, given="Rita", about_key="khalil_y"):
        conn = self.conn()
        if branch_id is not None:
            db.upsert_contributor(conn, user, branch_id=branch_id)
        payload = S.build(
            S.ADD_CHILD,
            submitted_by=S.submitter(user),
            about=S.subject(person_id=self.ids[about_key], label="Khalil"),
            people=[S.person(S.CHILD, given, sex="F")],
        )
        sid = db.add_submission(conn, user, payload)
        conn.commit()
        conn.close()
        return sid


class AccessTests(AdminTestCase):
    def test_the_queue_is_closed_without_a_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_the_wrong_password_is_refused(self):
        response = self.login(password="wrong")
        self.assertIn(b"Wrong password", response.data)

    def test_a_non_admin_id_is_refused_even_with_the_password(self):
        response = self.login(telegram_id=NOBODY)
        self.assertIn(b"isn&#39;t an admin", response.data)

    def test_a_super_admin_gets_in(self):
        response = self.login(SUPER)
        self.assertIn(b"super admin", response.data)

    def test_a_branch_admin_gets_in(self):
        response = self.login(BRANCH_ADMIN)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"super admin", response.data)

    def test_no_password_configured_means_closed_not_open(self):
        config.ADMIN_PASSWORD = ""
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 503)

    def test_actions_need_the_csrf_token(self):
        self.login(SUPER)
        sid = self.submit(111)
        response = self.client.post(f"/submission/{sid}/approve", data={})
        self.assertEqual(response.status_code, 400)


class QueueTests(AdminTestCase):
    def test_a_pending_submission_appears_with_its_evidence(self):
        self.login(SUPER)
        sid = self.submit(111, given="Georges")  # collides with the seed
        response = self.client.get("/")
        self.assertIn(f"#{sid}".encode(), response.data)
        self.assertIn(b"might already be", response.data)
        self.assertIn(b"Georges Youssef Sukkar", response.data)

    def test_a_branch_admin_sees_only_their_branch(self):
        mine = self.submit(111, branch_id=self.youssef_branch, given="Zaha")
        other = self.submit(222, branch_id=self.boutros_branch, given="Fadwa")
        self.login(BRANCH_ADMIN)
        response = self.client.get("/")
        self.assertIn(b"Zaha", response.data)
        self.assertNotIn(b"Fadwa", response.data)

    def test_a_super_admin_sees_everything(self):
        self.submit(111, branch_id=self.youssef_branch, given="Zaha")
        self.submit(222, branch_id=self.boutros_branch, given="Fadwa")
        self.login(SUPER)
        response = self.client.get("/")
        self.assertIn(b"Zaha", response.data)
        self.assertIn(b"Fadwa", response.data)


class DecisionTests(AdminTestCase):
    def test_approve_creates_the_person(self):
        self.login(SUPER)
        sid = self.submit(111, given="Zaha")
        conn = self.conn()
        before = db.count_people(conn)
        conn.close()

        self.client.post(
            f"/submission/{sid}/approve", data={"_csrf": self.csrf()},
            follow_redirects=True,
        )
        conn = self.conn()
        self.assertEqual(db.count_people(conn), before + 1)
        row = db.get_submission(conn, sid)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["reviewed_by"], SUPER)
        conn.close()

    def test_a_probable_duplicate_is_blocked_with_the_reason(self):
        self.login(SUPER)
        sid = self.submit(111, given="Georges")
        response = self.client.post(
            f"/submission/{sid}/approve", data={"_csrf": self.csrf()},
            follow_redirects=True,
        )
        self.assertIn(b"looks like", response.data)
        conn = self.conn()
        self.assertEqual(db.get_submission(conn, sid)["status"], "pending")
        conn.close()

    def test_approve_anyway_overrides_the_guard(self):
        self.login(SUPER)
        sid = self.submit(111, given="Georges")
        self.client.post(
            f"/submission/{sid}/approve",
            data={"_csrf": self.csrf(), "anyway": "yes"},
            follow_redirects=True,
        )
        conn = self.conn()
        self.assertEqual(db.get_submission(conn, sid)["status"], "approved")
        conn.close()

    def test_merge_links_without_creating(self):
        self.login(SUPER)
        sid = self.submit(111, given="Georges")
        conn = self.conn()
        before = db.count_people(conn)
        conn.close()

        self.client.post(
            f"/submission/{sid}/merge",
            data={"_csrf": self.csrf(), "into": str(self.ids["georges"])},
            follow_redirects=True,
        )
        conn = self.conn()
        self.assertEqual(db.count_people(conn), before)
        row = db.get_submission(conn, sid)
        self.assertEqual(row["status"], "merged")
        self.assertEqual(row["resulting_person_id"], self.ids["georges"])
        conn.close()

    def test_reject_requires_a_reason(self):
        self.login(SUPER)
        sid = self.submit(111, given="Zaha")
        response = self.client.post(
            f"/submission/{sid}/reject", data={"_csrf": self.csrf(), "note": ""},
            follow_redirects=True,
        )
        self.assertIn(b"Say why", response.data)
        conn = self.conn()
        self.assertEqual(db.get_submission(conn, sid)["status"], "pending")
        conn.close()

    def test_reject_with_a_reason_lands(self):
        self.login(SUPER)
        sid = self.submit(111, given="Zaha")
        self.client.post(
            f"/submission/{sid}/reject",
            data={"_csrf": self.csrf(), "note": "not one of ours"},
            follow_redirects=True,
        )
        conn = self.conn()
        row = db.get_submission(conn, sid)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["review_note"], "not one of ours")
        conn.close()

    def test_edit_then_approve_fixes_the_person_not_the_submission(self):
        self.login(SUPER)
        sid = self.submit(111, given="Ritta")
        conn = self.conn()
        original = db.get_submission(conn, sid)["payload_json"]
        conn.close()

        self.client.post(
            f"/submission/{sid}/approve",
            data={"_csrf": self.csrf(), "edit-0-given_name": "Rita"},
            follow_redirects=True,
        )
        conn = self.conn()
        row = db.get_submission(conn, sid)
        person = db.get_person(conn, row["resulting_person_id"])
        self.assertEqual(person["given_name"], "Rita")
        # The append-only rule: the stored submission is byte-for-byte as sent.
        self.assertEqual(row["payload_json"], original)
        self.assertEqual(row["review_note"], "approved with edits")
        conn.close()


class PersonPageTests(AdminTestCase):
    def test_the_person_page_shows_relatives_and_provenance(self):
        self.login(SUPER)
        sid = self.submit(111, given="Zaha")
        self.client.post(
            f"/submission/{sid}/approve", data={"_csrf": self.csrf()},
            follow_redirects=True,
        )
        conn = self.conn()
        person_id = db.get_submission(conn, sid)["resulting_person_id"]
        conn.close()

        response = self.client.get(f"/person/{person_id}")
        self.assertIn(b"Zaha", response.data)
        self.assertIn(b"Khalil Youssef Sukkar", response.data)  # her father
        self.assertIn(b"Where this came from", response.data)

    def test_find_answers_which_saide(self):
        self.login(SUPER)
        response = self.client.get("/find?q=Khalil")
        self.assertIn(b"Khalil Youssef Sukkar", response.data)
        self.assertIn(b"Khalil Antoun Sukkar", response.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
