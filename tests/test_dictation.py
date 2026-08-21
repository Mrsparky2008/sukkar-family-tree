"""
Tests for reading a whole family out of one message.

The names here are invented. Real family data belongs in a database, not in a
repository.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import submissions as S  # noqa: E402
from bot import dictation  # noqa: E402


class RecognisingDictationTests(unittest.TestCase):
    def test_a_single_name_is_not_dictation(self):
        for text in ("Toufic", "Abou-Khalil", "  Sami  "):
            self.assertFalse(dictation.looks_like_dictation(text), text)

    def test_a_relationship_word_is(self):
        self.assertTrue(dictation.looks_like_dictation("his parents are Toufic"))
        self.assertTrue(dictation.looks_like_dictation("brothers: Sami, Elie"))

    def test_a_bare_list_is(self):
        self.assertTrue(dictation.looks_like_dictation("Sami, Elie and Rita"))

    def test_nonsense_is_not_parsed_into_people(self):
        self.assertEqual(dictation.parse("qqq").people, [])


class ParsingTests(unittest.TestCase):
    def read(self, text, **kwargs):
        return dictation.parse(text, **kwargs)

    def test_parents_become_a_father_and_a_mother(self):
        reading = self.read("his parents are Toufic and Cilene")
        self.assertEqual(
            [(m.role, m.given_name, m.sex) for m in reading.people],
            [(S.FATHER, "Toufic", "M"), (S.MOTHER, "Cilene", "F")],
        )

    def test_guessing_which_parent_is_which_is_flagged(self):
        reading = self.read("parents Toufic and Cilene")
        self.assertTrue(all(m.uncertain for m in reading.people))

    def test_a_named_role_is_not_a_guess(self):
        reading = self.read("his father is Toufic")
        self.assertEqual(reading.people[0].role, S.FATHER)
        self.assertEqual(reading.people[0].uncertain, [])

    def test_sisters_are_female_siblings(self):
        reading = self.read("his sisters are Dibeh, Sonia and Rima")
        self.assertEqual(len(reading.people), 3)
        for mention in reading.people:
            self.assertEqual(mention.role, S.SIBLING)
            self.assertEqual(mention.sex, "F")

    def test_brothers_are_male_siblings(self):
        reading = self.read("brothers Sami and Elie")
        self.assertEqual({m.sex for m in reading.people}, {"M"})

    def test_family_names_are_kept_per_person(self):
        reading = self.read("his sisters are Dibeh Haddad and Sonia Rahme")
        self.assertEqual(
            [(m.given_name, m.family_name) for m in reading.people],
            [("Dibeh", "Haddad"), ("Sonia", "Rahme")],
        )

    def test_lowercase_names_are_capitalised(self):
        reading = self.read("his sister is dibeh haddad")
        self.assertEqual(reading.people[0].label(), "Dibeh Haddad")

    def test_unusual_capitalisation_is_left_alone(self):
        reading = self.read("his brother is McKay AbouKhalil")
        self.assertEqual(reading.people[0].label(), "McKay AbouKhalil")

    def test_married_to_attaches_a_spouse_to_the_right_person(self):
        reading = self.read("his sisters are Dibeh, Sonia and Rima married to Jamil Tarabay")
        spouse = reading.people[-1]
        self.assertEqual(spouse.role, S.SPOUSE)
        self.assertEqual(spouse.label(), "Jamil Tarabay")
        self.assertEqual(spouse.spouse_of, "Rima")
        self.assertEqual(spouse.sex, "M")

    def test_the_subjects_own_name_is_not_added_as_a_relative(self):
        reading = self.read(
            "Toufic's parents are Semaan and Wadiha", subject_name="Toufic"
        )
        self.assertNotIn("Toufic", [m.given_name for m in reading.people])
        self.assertEqual(len(reading.people), 2)

    def test_a_possessive_without_an_apostrophe_is_still_the_subject(self):
        reading = self.read("Toufics sisters are Dibeh and Sonia", subject_name="Toufic")
        self.assertEqual(
            [m.given_name for m in reading.people], ["Dibeh", "Sonia"]
        )

    def test_several_lines_each_keep_their_own_role(self):
        reading = self.read(
            "his parents are Semaan and Wadiha\nhis brothers are Sami and Elie"
        )
        self.assertEqual(
            [(m.role, m.given_name) for m in reading.people],
            [
                (S.FATHER, "Semaan"),
                (S.MOTHER, "Wadiha"),
                (S.SIBLING, "Sami"),
                (S.SIBLING, "Elie"),
            ],
        )

    def test_a_role_carries_on_to_the_next_line(self):
        reading = self.read("his sisters\nDibeh, Sonia\nRima")
        self.assertEqual(len(reading.people), 3)
        self.assertEqual({m.role for m in reading.people}, {S.SIBLING})

    def test_remarks_that_fit_nobody_are_kept_not_guessed_at(self):
        reading = self.read("his sisters are Dibeh and Sonia, the others are single")
        self.assertIn("said to be single", reading.notes)

    def test_a_default_role_is_used_when_the_message_names_none(self):
        reading = self.read("Sami, Elie and Rita", default_role=S.CHILD)
        self.assertEqual({m.role for m in reading.people}, {S.CHILD})

    def test_no_role_and_no_default_yields_nobody(self):
        self.assertEqual(dictation.parse("Sami, Elie and Rita").people, [])

    def test_filler_words_never_become_people(self):
        reading = self.read("I think his sisters are Dibeh and Sonia")
        names = [m.given_name for m in reading.people]
        self.assertEqual(names, ["Dibeh", "Sonia"])

    def test_punctuation_does_not_cling_to_names(self):
        reading = self.read("his sisters are Dibeh, Sonia.")
        self.assertEqual([m.given_name for m in reading.people], ["Dibeh", "Sonia"])

    def test_a_realistic_message_reads_completely(self):
        reading = self.read(
            "Semaan's parents my grandparents are\n"
            "Toufic Haddad and Cilene Haddad\n"
            "Semaans sisters\n"
            "Dibeh Haddad, Sonia Haddad and rima haddad married to Jamil Tarabay "
            "the other girls are single",
            subject_name="Semaan",
        )
        self.assertEqual(
            [(m.role, m.label()) for m in reading.people],
            [
                (S.FATHER, "Toufic Haddad"),
                (S.MOTHER, "Cilene Haddad"),
                (S.SIBLING, "Dibeh Haddad"),
                (S.SIBLING, "Sonia Haddad"),
                (S.SIBLING, "Rima Haddad"),
                (S.SPOUSE, "Jamil Tarabay"),
            ],
        )
        self.assertEqual(reading.people[-1].spouse_of, "Rima Haddad")
        self.assertIn("said to be single", reading.notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
