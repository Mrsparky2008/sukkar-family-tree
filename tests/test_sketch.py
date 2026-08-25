"""
The text sketch: what somebody entered, drawn small.

The sketch folds names carefully: a reference may complete a person already
drawn, but two entered people never silently become one — except inside a
single family node, where one couple is one couple however it was spelled.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import submissions as S  # noqa: E402
from bot import sketch  # noqa: E402


def parents_of(child, father, mother, father_family=None, mother_family=None):
    return {
        "kind": S.ADD_PARENTS,
        "about": {"label": child},
        "people": [
            S.person(S.FATHER, father, sex="M", family_name=father_family),
            S.person(S.MOTHER, mother, sex="F", family_name=mother_family),
        ],
    }


def sibling_of(person, name):
    return {
        "kind": S.ADD_SIBLING,
        "about": {"label": person},
        "people": [S.person(S.SIBLING, name, sex="M")],
    }


class OneCoupleTests(unittest.TestCase):
    def test_reentering_a_bare_name_does_not_grow_a_third_parent(self):
        # The couple stands with full labels; a later claim about the same
        # child spells the father bare. Same couple — not a second husband.
        drawing = sketch.build([
            parents_of("Kalim", "Toufic", "Seleneh",
                       father_family="Sukkar", mother_family="Sukkar"),
            parents_of("Kalim", "Toufic", "Seleneh", mother_family="Sukkar"),
        ])
        self.assertEqual(drawing.count("⚭"), 1, drawing)
        self.assertIn("Toufic Sukkar ⚭ Seleneh Sukkar", drawing)

    def test_the_fuller_spelling_wins_whichever_came_first(self):
        drawing = sketch.build([
            parents_of("Kalim", "Toufic", "Seleneh", mother_family="Sukkar"),
            parents_of("Kalim", "Toufic", "Seleneh",
                       father_family="Sukkar", mother_family="Sukkar"),
        ])
        self.assertEqual(drawing.count("⚭"), 1, drawing)
        self.assertIn("Toufic Sukkar ⚭ Seleneh Sukkar", drawing)

    def test_two_entered_men_sharing_a_name_stay_two_people(self):
        # A brother Toufic and somebody's father Toufic Sukkar are different
        # men in different families — the couple rule must not reach them.
        drawing = sketch.build([
            sibling_of("Steven", "Toufic"),
            parents_of("Maha", "Toufic", "Salma", father_family="Sukkar"),
        ])
        self.assertIn("Toufic Sukkar", drawing)
        self.assertRegex(drawing, r"[├└] Toufic\n")



def spouse_of(person, name, family=None):
    return {
        "kind": S.ADD_SPOUSE,
        "about": {"label": person},
        "people": [S.person(S.SPOUSE, name, sex="F", family_name=family)],
    }


def child_of(person, name):
    return {
        "kind": S.ADD_CHILD,
        "about": {"label": person},
        "people": [S.person(S.CHILD, name, sex="M")],
    }


class MarriageOnTheirOwnLineTests(unittest.TestCase):
    """A couple's marriage belongs on the line of whoever heads it.

    Suppressing it wherever the couple had children hid every marriage in
    the middle of a tree — a man appeared beside his parents with no wife,
    and she was left dangling at the bottom with no number.
    """

    def drawing(self):
        return sketch.build(
            [
                parents_of("Kalim", "Toufic", "Seleneh"),
                parents_of("Steven", "Kalim", "Wadiha"),
                child_of("Steven", "Henri"),
            ],
            ids={"Kalim": 27, "Wadiha": 28, "Toufic": 29, "Steven": 1},
        )

    def test_a_father_shows_his_marriage_beside_his_own_parents(self):
        drawing = self.drawing()
        self.assertIn("Kalim #27 ⚭ Wadiha #28", drawing)

    def test_the_children_still_hang_below_that_couple(self):
        drawing = self.drawing()
        lines = [line for line in drawing.split("\n") if "Steven" in line]
        self.assertTrue(lines, drawing)
        self.assertTrue(lines[0].lstrip().startswith(("├", "└")), drawing)

    def test_nobody_is_left_dangling_without_a_number(self):
        # The spouse used to reappear at the bottom, undecorated, because
        # she never made it onto anyone's line.
        drawing = self.drawing()
        self.assertEqual(drawing.count("Wadiha"), 1, drawing)
        for line in drawing.split("\n"):
            if "Wadiha" in line:
                self.assertIn("#28", line)

    def test_a_grandson_of_the_same_name_stays_unmarried(self):
        # Two men of one name are one name here, so the guard still has to
        # hold: the grandson must not inherit his grandmother.
        drawing = sketch.build(
            [
                parents_of("Steven", "Kalim", "Wadiha"),
                child_of("Steven", "Kalim"),
            ],
        )
        grandson = [
            line for line in drawing.split("\n")
            if line.strip().startswith(("├ Kalim", "└ Kalim"))
        ]
        for line in grandson:
            self.assertNotIn("⚭", line, drawing)

if __name__ == "__main__":
    unittest.main()
