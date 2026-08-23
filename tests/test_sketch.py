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


if __name__ == "__main__":
    unittest.main()
