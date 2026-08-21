"""Run the review interface:  python -m admin

Binds to localhost only. On the server you reach it over an SSH tunnel:

    ssh -L 8080:localhost:8080 admin@<server>

then open http://localhost:8080 — no public port, no certificate to manage.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.app import create_app  # noqa: E402

create_app().run(host="127.0.0.1", port=int(os.environ.get("ADMIN_PORT", "8080")))
