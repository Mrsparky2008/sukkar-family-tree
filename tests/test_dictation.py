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


class DeclaredSubjectTests(unittest.TestCase):
    """A message can be about somebody other than whoever was asked about."""

    def test_daughter_of_names_its_own_subject_and_her_parents(self):
        reading = dictation.parse(
            "Wadiha is the daughter of Najib Haddad and Saide Taouk"
        )
        self.assertEqual(reading.subject, "Wadiha")
        self.assertEqual(reading.subject_sex, "F")
        self.assertEqual(
            [(m.role, m.label()) for m in reading.people],
            [(S.FATHER, "Najib Haddad"), (S.MOTHER, "Saide Taouk")],
        )

    def test_the_subject_is_never_added_as_her_own_relative(self):
        """Read backwards this makes a woman her own mother's sibling."""
        reading = dictation.parse("Wadiha is the daughter of Najib and Saide")
        self.assertNotIn("Wadiha", [m.given_name for m in reading.people])

    def test_son_of_gives_a_male_subject(self):
        reading = dictation.parse("Sami is the son of Elie and Rita")
        self.assertEqual(reading.subject_sex, "M")

    def test_a_relationship_word_before_the_name_is_not_the_name(self):
        reading = dictation.parse("my mother Wadiha is the daughter of Najib and Saide")
        self.assertEqual(reading.subject, "Wadiha")

    def test_the_declared_subject_carries_to_later_lines(self):
        reading = dictation.parse(
            "Wadiha is the daughter of Najib and Saide\n"
            "Her siblings are Khalil and Rima"
        )
        siblings = [m for m in reading.people if m.role == S.SIBLING]
        self.assertEqual([m.given_name for m in siblings], ["Khalil", "Rima"])


class NicknamesAndTitlesTests(unittest.TestCase):
    def test_a_bracketed_name_is_a_nickname(self):
        reading = dictation.parse("his brothers are Hanna (John) and Youssef (Joe)")
        self.assertEqual(
            [(m.given_name, m.also_known_as) for m in reading.people],
            [("Hanna", "John"), ("Youssef", "Joe")],
        )

    def test_a_bracketed_sentence_is_a_remark(self):
        reading = dictation.parse("his sister is Clemence (she became a nun)")
        self.assertIsNone(reading.people[0].also_known_as)
        self.assertEqual(reading.people[0].note, "she became a nun")

    def test_a_nickname_lands_on_the_right_person(self):
        """It used to be handed to whoever came first in the fragment."""
        reading = dictation.parse(
            "his brothers are Khalil Haddad Hanna (John) Haddad",
            subject_name=None,
        )
        by_name = {m.given_name: m.also_known_as for m in reading.people}
        self.assertIsNone(by_name.get("Khalil"))
        self.assertEqual(by_name.get("Hanna"), "John")

    def test_a_title_folds_into_the_person_already_named(self):
        reading = dictation.parse(
            "his sister is Clemence Haddad, sister clemence"
        )
        self.assertEqual(len(reading.people), 1)
        self.assertIn("Sister Clemence", reading.people[0].note or "")

    def test_a_missing_comma_between_two_relatives_is_recovered(self):
        import config

        family = config.FAMILY_NAME
        reading = dictation.parse(f"his brothers are Khalil {family} Hanna {family}")
        self.assertEqual(
            [m.label() for m in reading.people],
            [f"Khalil {family}", f"Hanna {family}"],
        )

    def test_an_ordinary_two_part_name_is_not_split(self):
        reading = dictation.parse("his brother is Khalil Abou Haddad")
        self.assertEqual(len(reading.people), 1)


class RealisticInputTests(unittest.TestCase):
    """The shapes people actually type, with no commas and no patience."""

    MESSAGE = (
        "Khalil Haddad never married\n"
        "Hanna (John) Haddad married to Therese Taouk\n"
        "Kids are Rohnda Jason Ronnie Jocelyn\n"
        "Youssef (joe) Haddad married to Wafaq Rahme "
        "kids are Lena Centia, Maria, Sarah and Josephine"
    )

    def setUp(self):
        self.reading = dictation.parse(self.MESSAGE, subject_name="Wadiha")

    def test_never_married_is_a_fact_not_a_new_relative(self):
        self.assertEqual(self.reading.remarks, [("Khalil", "never married")])
        self.assertNotIn("Khalil", [m.given_name for m in self.reading.people])

    def test_a_line_can_be_about_someone_other_than_the_subject(self):
        children = [m for m in self.reading.people if m.role == S.CHILD]
        self.assertEqual(
            {m.about for m in children}, {"Hanna", "Youssef"}
        )

    def test_a_run_of_names_with_no_commas_is_several_children(self):
        hanna_kids = [
            m.given_name
            for m in self.reading.people
            if m.role == S.CHILD and m.about == "Hanna"
        ]
        self.assertEqual(hanna_kids, ["Rohnda", "Jason", "Ronnie", "Jocelyn"])

    def test_a_spouse_and_children_on_one_line_both_land(self):
        for name in ("Therese", "Wafaq"):
            spouse = [m for m in self.reading.people if m.given_name == name]
            self.assertEqual(len(spouse), 1, name)
            self.assertEqual(spouse[0].role, S.SPOUSE)

    def test_an_ambiguous_pair_of_names_is_flagged_not_guessed(self):
        lena = [m for m in self.reading.people if m.given_name == "Lena"][0]
        self.assertTrue(lena.uncertain)
        self.assertIn("one person or two", lena.uncertain[0])

    def test_the_english_name_of_an_existing_relative_is_kept(self):
        """(John) belongs to a man already recorded, not to a new one."""
        self.assertEqual(
            dict(self.reading.aliases), {"Hanna": "John", "Youssef": "joe"}
        )

    def test_nobody_is_invented_from_the_relationship_words(self):
        names = {m.given_name for m in self.reading.people}
        for word in ("Kids", "Married", "Never", "Are"):
            self.assertNotIn(word, names)

    def test_a_singular_role_keeps_a_three_part_name_whole(self):
        reading = dictation.parse("his son is Khalil Abou Haddad")
        self.assertEqual(len(reading.people), 1)
        self.assertEqual(reading.people[0].label(), "Khalil Abou Haddad")


class PossessiveSubjectTests(unittest.TestCase):
    """"Kalims sisters are..." names Kalim, not a man called Kalims."""

    def test_a_known_name_before_a_role_word_is_a_possessive(self):
        reading = dictation.parse(
            "Kalims sisters are Dibeh and Sonia",
            subject_name="Wadiha",
            known_names={"Kalim", "Wadiha"},
        )
        self.assertEqual([m.given_name for m in reading.people], ["Dibeh", "Sonia"])
        self.assertEqual({m.about for m in reading.people}, {"Kalim"})

    def test_an_apostrophe_works_the_same_way(self):
        reading = dictation.parse(
            "Kalim's parents are Toufic and Cilene",
            known_names={"Kalim"},
        )
        self.assertEqual({m.about for m in reading.people}, {"Kalim"})

    def test_an_unknown_name_is_not_swallowed_as_a_possessive(self):
        reading = dictation.parse(
            "Zaher sisters are Dibeh and Sonia", known_names={"Kalim"}
        )
        self.assertIn("Zaher", [m.given_name for m in reading.people])


class MarriagesInsideAListTests(unittest.TestCase):
    """"A, B married to X, C married to Y, D and E" — one marriage per person."""

    LINE = (
        "Her siblings are Khalil, Hanna (John) married to Therese Taouk, "
        "Youssef (Joe) married to Wafaq Rahme, Waleena, Rafqa and "
        "Clemence (she became a nun)"
    )

    def setUp(self):
        self.reading = dictation.parse(self.LINE, subject_name="Wadiha")

    def test_every_sibling_stays_a_sibling(self):
        siblings = [m.given_name for m in self.reading.people if m.role == S.SIBLING]
        self.assertEqual(
            siblings,
            ["Khalil", "Hanna", "Youssef", "Waleena", "Rafqa", "Clemence"],
        )

    def test_each_spouse_attaches_to_their_own_partner(self):
        spouses = {
            m.spouse_of: m.given_name
            for m in self.reading.people
            if m.role == S.SPOUSE
        }
        self.assertEqual(
            spouses, {"Hanna (John)": "Therese", "Youssef (Joe)": "Wafaq"}
        )

    def test_nobody_is_merged_with_their_own_spouse(self):
        names = [m.given_name for m in self.reading.people]
        self.assertNotIn("Youssef Wafaq", " ".join(names))

    def test_the_nun_remark_stays_on_clemence(self):
        clemence = [m for m in self.reading.people if m.given_name == "Clemence"][0]
        self.assertIn("nun", clemence.note or "")

    def test_a_parents_pair_is_never_flagged_as_maybe_two(self):
        reading = dictation.parse("Wadiha is the daughter of Najib Haddad and Saide Taouk")
        self.assertFalse(
            [m for m in reading.people if any("one person" in u for u in m.uncertain)]
        )
