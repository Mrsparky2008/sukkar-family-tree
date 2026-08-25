"""
The public chart's own JavaScript.

The rest of the suite covers Python. This file drives the real chart script
in a stubbed browser, because the gestures it implements cannot be checked
any other way — and their absence is invisible until somebody is standing in
a kitchen holding a phone. Skipped where node is not installed; nothing in
the project needs it to run or deploy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from web import build as chart  # noqa: E402

NODE = shutil.which("node")

#: Enough of a browser for the chart to load: every unknown property answers
#: with a no-op, so the script can draw, measure and listen to its heart's
#: content without any of it going anywhere.
STUB = """
const fs = require('fs');
const handlers = { canvas: {} };
const noop = () => {};
const ctx = new Proxy({}, {
  get: (t, k) => (k in t ? t[k] : (t[k] = noop)),
  set: (t, k, v) => ((t[k] = v), true),
});
function makeEl(name) {
  return new Proxy({
    style: {}, dataset: {}, children: [],
    classList: { add: noop, remove: noop, toggle: noop },
    addEventListener: (type, fn) => { (handlers[name] ||= {})[type] = fn; },
    setPointerCapture: noop, releasePointerCapture: noop,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
    getContext: () => ctx,
    textContent: name === 'data' ? fs.readFileSync(process.argv[3], 'utf8') : '',
    appendChild: noop, setAttribute: noop, removeAttribute: noop,
    focus: noop, querySelectorAll: () => [], closest: () => null,
  }, {
    get: (t, k) => (k in t ? t[k] : (t[k] = noop)),
    set: (t, k, v) => ((t[k] = v), true),
  });
}
const elements = {};
global.document = {
  getElementById: id => (elements[id] ||= makeEl(id === 'chart' ? 'canvas' : id)),
  createElement: () => makeEl('el'),
  addEventListener: noop,
  documentElement: makeEl('html'),
};
global.window = { addEventListener: noop, devicePixelRatio: 2,
                  matchMedia: () => ({ matches: false, addEventListener: noop }) };
global.location = { hash: '' };
global.requestAnimationFrame = noop;
global.getComputedStyle = () => ({ getPropertyValue: () => '#000000' });
global.HANDLERS = handlers;
eval(fs.readFileSync(process.argv[2], 'utf8') + '\\n' + CHECKS);
"""


@unittest.skipUnless(NODE, "node is not installed")
class GestureTests(unittest.TestCase):
    """Panning, pinching and tapping on a phone."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        work = Path(cls._tmp.name)

        conn = db.connect(":memory:")
        db.init_db(conn)
        father = db.create_person(conn, "Tanios", sex="M")
        mother = db.create_person(conn, "Zeina", sex="F")
        db.create_union(conn, father, mother)
        db.create_person(conn, "Sami", sex="M", father_id=father, mother_id=mother)
        conn.commit()

        page = chart.build(conn, work / "tree.html")
        conn.close()
        html = page.read_text(encoding="utf-8")

        script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
        data = re.search(r'id="data"[^>]*>(.*?)</script>', html, re.S).group(1)
        json.loads(data)  # the page must carry valid data at all

        cls.script = work / "chart.js"
        cls.script.write_text(script, encoding="utf-8")
        cls.data = work / "data.json"
        cls.data.write_text(data, encoding="utf-8")
        cls.work = work

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def drive(self, checks: str) -> str:
        harness = self.work / "harness.js"
        harness.write_text(
            f"const CHECKS = {json.dumps(checks)};\n{STUB}", encoding="utf-8"
        )
        done = subprocess.run(
            [NODE, str(harness), str(self.script), str(self.data)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_spreading_two_fingers_zooms_in(self):
        out = self.drive("""
            const g = HANDLERS.canvas, before = view.k;
            g.pointerdown({ pointerId: 1, clientX: 300, clientY: 300 });
            g.pointerdown({ pointerId: 2, clientX: 400, clientY: 300 });
            g.pointermove({ pointerId: 2, clientX: 500, clientY: 300 });
            console.log(view.k > before ? 'in' : 'no');
        """)
        self.assertEqual(out, "in", "a phone has no other way to zoom")

    def test_pinching_zooms_back_out(self):
        out = self.drive("""
            const g = HANDLERS.canvas;
            g.pointerdown({ pointerId: 1, clientX: 300, clientY: 300 });
            g.pointerdown({ pointerId: 2, clientX: 500, clientY: 300 });
            const wide = view.k;
            g.pointermove({ pointerId: 2, clientX: 350, clientY: 300 });
            console.log(view.k < wide ? 'out' : 'no');
        """)
        self.assertEqual(out, "out")

    def test_a_drag_does_not_count_as_a_tap(self):
        out = self.drive("""
            const g = HANDLERS.canvas;
            let picked = 0; pick = () => { picked++; };
            g.pointerdown({ pointerId: 1, clientX: 100, clientY: 100 });
            g.pointermove({ pointerId: 1, clientX: 300, clientY: 260 });
            g.pointerup({ pointerId: 1, clientX: 300, clientY: 260 });
            console.log(picked === 0 ? 'clean' : 'opened somebody');
        """)
        self.assertEqual(out, "clean")

    def test_a_tap_still_selects(self):
        out = self.drive("""
            const g = HANDLERS.canvas;
            let picked = 0; pick = () => { picked++; };
            g.pointerdown({ pointerId: 1, clientX: 100, clientY: 100 });
            g.pointerup({ pointerId: 1, clientX: 101, clientY: 100 });
            console.log(picked === 1 ? 'selected' : 'missed');
        """)
        self.assertEqual(out, "selected")

    def test_lifting_one_finger_does_not_jump_the_zoom(self):
        out = self.drive("""
            const g = HANDLERS.canvas;
            g.pointerdown({ pointerId: 1, clientX: 300, clientY: 300 });
            g.pointerdown({ pointerId: 2, clientX: 400, clientY: 300 });
            const held = view.k;
            g.pointerup({ pointerId: 2, clientX: 400, clientY: 300 });
            g.pointermove({ pointerId: 1, clientX: 340, clientY: 300 });
            console.log(Math.abs(view.k - held) < 1e-9 ? 'steady' : 'jumped');
        """)
        self.assertEqual(out, "steady")


if __name__ == "__main__":
    unittest.main()
