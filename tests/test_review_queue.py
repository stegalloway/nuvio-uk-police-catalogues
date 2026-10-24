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
        editorial = {"firstAiredWithinYears": 5, "excludedTmdbIds": []}
        fresh, review, _, _ = update_catalogs.discover(FakeTMDB(), editorial, {"candidates": []}, {}, set())
        self.assertEqual(fresh, {"on-duty": [], "police-investigations": []})
        candidate, = review["candidates"]
        self.assertEqual((candidate["network"], candidate["episodes"], candidate["seasons"]), ("BBC One", 27, 3))
        self.assertTrue(candidate["overview"].startswith("How detectives solved some of the most significant"))
        self.assertLessEqual(len(candidate["overview"]), 241)
        self.assertTrue(candidate["overview"].endswith("…"))


if __name__ == "__main__":
    unittest.main()
