"""Review candidates carry enough detail to be judged from the queue alone."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_catalogs


SHOW = {"id": 273233, "name": "Crimewatch Caught", "origin_country": ["GB"],
        "first_air_date": update_catalogs.months_ago(12).isoformat(),
        "genres": [{"id": 99, "name": "Documentary"}], "networks": [{"id": 4, "name": "BBC One"}],
        "number_of_episodes": 27, "number_of_seasons": 3, "poster_path": "/poster.jpg",
        "overview": "How detectives solved some of the most  significant crimes. " * 8}


class FakeTMDB:
    cache = {}

    def get(self, path, query=None):
        return {"total_pages": 1, "results": [SHOW]}

    def detail(self, tmdb_id):
        return SHOW


class ReviewQueueTests(unittest.TestCase):
    def test_a_candidate_lists_its_channel_episodes_and_a_short_synopsis(self):
        editorial = {"firstAiredWithinYears": 5, "excludedTmdbIds": [],
                     "categories": {"on-duty": {}, "police-investigations": {}}}
        fresh, review, _, _ = update_catalogs.discover(FakeTMDB(), editorial, {"candidates": []}, {}, set())
        self.assertEqual(fresh, {"on-duty": [], "police-investigations": []})
        candidate, = review["candidates"]
        self.assertEqual((candidate["network"], candidate["episodes"], candidate["seasons"]), ("BBC One", 27, 3))
        self.assertTrue(candidate["overview"].startswith("How detectives solved some of the most significant"))
        self.assertLessEqual(len(candidate["overview"]), 241)
        self.assertTrue(candidate["overview"].endswith("…"))

    def test_true_crime_without_police_goes_to_review_for_the_true_crime_row(self):
        case = {**SHOW, "id": 157275, "name": "Murder in the Valleys",
                "overview": "The killing of a family in Wales, and the trial that followed."}
        editorial = {"firstAiredWithinYears": 5, "excludedTmdbIds": [],
                     "categories": {"on-duty": {}, "police-investigations": {}, "true-crime": {}}}

        class Queue(FakeTMDB):
            def get(self, path, query=None):
                return {"total_pages": 1, "results": [case]}

            def detail(self, tmdb_id):
                return case

        fresh, review, _, _ = update_catalogs.discover(Queue(), editorial, {"candidates": []}, {}, set())
        self.assertEqual(fresh, {"on-duty": [], "police-investigations": [], "true-crime": []})
        self.assertEqual([c["category"] for c in review["candidates"]], ["true-crime"])


class TrueCrimeRoutingTests(unittest.TestCase):
    def test_police_led_shows_keep_their_police_rows(self):
        self.assertEqual(update_catalogs.confidence(SHOW, true_crime=True)["category"], "police-investigations")

    def test_a_real_case_without_police_is_true_crime_only_when_the_row_exists(self):
        case = {**SHOW, "name": "The Man in the Mask: An Orkney Murder",
                "overview": "A woman was killed on Orkney and the case went unsolved for years."}
        self.assertIsNone(update_catalogs.confidence(case))
        self.assertEqual(update_catalogs.confidence(case, true_crime=True)["category"], "true-crime")

    def test_scripted_drama_and_unrelated_documentaries_stay_out(self):
        drama = {**SHOW, "name": "Murder Island", "overview": "A crime drama about a killing on a remote island."}
        nature = {**SHOW, "name": "Wild Isles", "overview": "Wildlife across Britain and Ireland."}
        self.assertIsNone(update_catalogs.confidence(drama, true_crime=True))
        self.assertIsNone(update_catalogs.confidence(nature, true_crime=True))


if __name__ == "__main__":
    unittest.main()
