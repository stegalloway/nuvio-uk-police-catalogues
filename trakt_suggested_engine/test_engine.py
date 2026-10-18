"""Focused contracts for watched exclusion, returning seasons and output quality."""

from datetime import datetime, timedelta, timezone
import unittest

from .engine import (
    EngineConfig, GenerationError, derive_episode_progress, evaluate_returning,
    generate_catalogues, rank_and_mix_series, thematic_signals,
)


NOW = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)


def show(number=10):
    return {"title": "Fixture show", "ids": {"trakt": number, "tmdb": number, "imdb": "tt" + str(number)}}


def event(season, episode, *, when="2026-10-17T12:00:00Z", number=10):
    return {"show": show(number), "episode": {"season": season, "number": episode}, "watched_at": when}


def season(number, count=2, premiered=None):
    first = premiered or NOW - timedelta(days=100)
    return {"number": number, "episode_count": count, "episodes": [
        {"number": ep, "first_aired": (first + timedelta(hours=ep - 1)).isoformat()}
        for ep in range(1, count + 1)
    ]}


class ReturningContracts(unittest.TestCase):
    def state(self, events=None, **entry):
        states = derive_episode_progress([dict(show=show(), plays=10, **entry)],
                                         events if events is not None else [event(1, 1), event(1, 2)])
        return states["trakt:10"]

    def test_public_watched_payload_does_not_imply_episode_completion(self):
        state = self.state([])
        self.assertEqual(state["watched_episodes"], {})
        result = evaluate_returning(state, [season(1), season(2)], NOW)
        self.assertEqual(result["reason"], "missing_episode_history")

    def test_history_join_uses_ids_and_deduplicates_rewatches(self):
        events = [event(1, 1), event(1, 1), event(1, 2)]
        events[0]["show"]["title"] = "Changed display name"
        self.assertEqual(self.state(events)["watched_episodes"], {1: {1, 2}})

    def test_first_regular_episode_release_boundary(self):
        for delta, route in ((1, "hidden"), (0, "returning"), (-1, "returning")):
            with self.subTest(delta=delta):
                result = evaluate_returning(self.state(), [season(1), season(2, premiered=NOW + timedelta(seconds=delta))], NOW)
                self.assertEqual(result["route"], route)
                if route == "returning":
                    self.assertEqual(result["season_number"], 2)

    def test_started_new_season_is_not_recommended_again(self):
        state = self.state([event(1, 1), event(1, 2), event(2, 1)])
        result = evaluate_returning(state, [season(1), season(2)], NOW)
        self.assertEqual(result["route"], "continue_watching")

    def test_previous_season_must_be_completed(self):
        result = evaluate_returning(self.state([event(1, 1)]), [season(1), season(2)], NOW)
        self.assertEqual(result["route"], "continue_watching")

    def test_future_episode_in_started_season_does_not_imply_completion(self):
        current = season(1)
        current["episodes"][1]["first_aired"] = (NOW + timedelta(days=1)).isoformat()
        result = evaluate_returning(self.state([event(1, 1)]), [current, season(2)], NOW)
        self.assertEqual(result["route"], "hidden")

    def test_announcements_specials_missing_dates_do_not_trigger(self):
        for next_season in (
            {"number": 2, "first_aired": "2026-01-01T00:00:00Z"},
            {"number": 2, "episodes": [{"number": 1, "first_aired": "2026-01-01"}]},
            {"number": 2, "episodes": [{"number": 1, "first_aired": "2026-01-01T00:00:00"}]},
            {"number": 2, "episodes": [{"number": 0, "first_aired": "2026-01-01T00:00:00Z"}]},
            {"number": 2, "episodes": [{"number": 1, "episode_type": "trailer", "first_aired": "2026-01-01T00:00:00Z"}]},
            season(0),
        ):
            with self.subTest(next_season=next_season):
                self.assertEqual(evaluate_returning(self.state(), [season(1), next_season], NOW)["route"], "hidden")

    def test_incomplete_full_season_metadata_fails_closed(self):
        previous = season(1)
        previous["episode_count"] = 3
        result = evaluate_returning(self.state(), [previous, season(2)], NOW)
        self.assertEqual(result["reason"], "incomplete_baseline_metadata")

    def test_reset_boundary_excludes_old_episode_events(self):
        state = self.state(reset_at="2026-10-18T00:00:00Z")
        self.assertEqual(state["watched_episodes"], {})

    def test_nested_progress_adapter_also_supported(self):
        state = self.state([], seasons=[{"number": 1, "episodes": [{"number": 1, "plays": 1}, {"number": 2, "plays": 2}]}])
        self.assertEqual(state["watched_episodes"], {1: {1, 2}})


class RankingContracts(unittest.TestCase):
    def test_theme_signals_use_metadata_not_display_title(self):
        self.assertFalse(thematic_signals({"name": "MobLand The Penguin Gangster Mafia", "overview": "Two people meet."})["gangster_priority"])
        self.assertTrue(thematic_signals({"name": "Unrelated name", "keywords": {"results": [{"id": 1, "name": "organized crime"}]}})["gangster_priority"])

    def test_gangster_first_without_losing_broader_crime(self):
        cards = [{"id": "g" + str(i), "suggested_score": .3 + i / 20, "theme": {"gangster_priority": True}} for i in range(4)]
        cards += [{"id": "p" + str(i), "suggested_score": .9 + i / 100, "theme": {"gangster_priority": False}} for i in range(3)]
        ids = [c["id"][0] for c in rank_and_mix_series(cards, limit=7)]
        self.assertEqual(ids, ["g", "g", "p", "g", "g", "p", "p"])

    def test_police_led_organised_crime_is_broader_than_crime_family_lead(self):
        keyword = {"results": [{"id": 1, "name": "organized crime"}]}
        police = thematic_signals({"keywords": keyword,
            "overview": "A detective joins an elite task force to dismantle the city's most powerful crime family."})
        criminal = thematic_signals({"keywords": keyword,
            "overview": "A crime family fights for control of the city while a police task force closes in."})
        self.assertGreater(police["organised_crime"], 0)
        self.assertTrue(police["investigator_led"])
        self.assertFalse(police["gangster_priority"])
        self.assertEqual(police["priority_organised_crime"], 0)
        self.assertFalse(criminal["investigator_led"])
        self.assertTrue(criminal["gangster_priority"])
        cards = [
            {"id": "cop", "suggested_score": .99, "theme": police},
            {"id": "gang1", "suggested_score": .5, "theme": criminal},
            {"id": "gang2", "suggested_score": .4, "theme": criminal},
        ]
        self.assertEqual([c["id"] for c in rank_and_mix_series(cards, limit=3)], ["gang1", "gang2", "cop"])

    def test_police_corruption_lead_does_not_count_as_gangster_lead(self):
        theme = thematic_signals({"keywords": {"results": [
            {"id": 1, "name": "organized crime"}, {"id": 2, "name": "police corruption"}]},
            "overview": "The rise and fall of a police department's gun trace task force and its moral collapse."})
        self.assertTrue(theme["investigator_led"])
        self.assertFalse(theme["gangster_priority"])


class FixtureAPI:
    def __init__(self):
        self.calls = []
        self.failure = False
        self.details = {}
        for media, base in (("movie", 100), ("tv", 200)):
            for offset in range(9):
                number = base + offset
                org = offset % 3 != 2
                self.details[(media, number)] = self.detail(number, org=org)
        self.details[("movie", 1)] = self.detail(1)
        self.details[("tv", 10)] = self.detail(10, org=True)
        self.details[("movie", 101)]["original_language"] = "es"
        self.details[("tv", 201)]["poster_path"] = None
        self.details[("movie", 102)]["genres"] = [{"id": 27, "name": "Horror"}, {"id": 80, "name": "Crime"}]
        self.details[("tv", 202)]["vote_average"] = 6.9
        self.details[("movie", 103)]["adult"] = True

    @staticmethod
    def detail(number, org=False):
        return {"id": number, "name": "Show " + str(number), "title": "Movie " + str(number),
                "external_ids": {"imdb_id": "tt" + str(number)},
                "original_language": "en", "genres": [{"id": 80, "name": "Crime"}],
                "first_air_date": "2025-01-01", "release_date": "2025-01-01",
                "vote_average": 8.0, "vote_count": 500, "popularity": 30,
                "poster_path": "/poster" + str(number) + ".jpg", "origin_country": ["GB"],
                "production_countries": [{"iso_3166_1": "GB"}],
                "overview": "A crime family fights a gangster war." if org else "A detective investigates missing persons.",
                "keywords": {"results": [{"id": 1 if org else 2, "name": "organized crime" if org else "police investigation"}]},
                "credits": {"cast": [{"id": 123}], "crew": []}}

    def __call__(self, path, params=None):
        self.calls.append((path, params))
        if self.failure:
            raise TimeoutError("fixture upstream failure")
        parts = path.strip("/").split("/")
        if parts[0] == "discover":
            media = parts[1]
            return {"results": self.candidates(media)}
        media, number = parts[0], int(parts[1])
        if len(parts) > 2:
            return {"results": self.candidates(media)}
        return self.details[(media, number)]

    def candidates(self, media):
        result = []
        for (m, _), detail in self.details.items():
            if m == media:
                raw = dict(detail)
                raw["genre_ids"] = [g["id"] for g in detail["genres"]]
                result.append(raw)
        return result


class EndToEndContracts(unittest.TestCase):
    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = (
            [{"movie": {"title": "Seen", "ids": {"imdb": "tt1", "tmdb": 1}}, "plays": 1, "last_watched_at": "2026-10-17T00:00:00Z"}],
            [{"show": show(), "plays": 2, "last_watched_at": "2026-10-17T00:00:00Z"}],
            [{"movie": {"title": "Rated only", "ids": {"imdb": "tt100", "tmdb": 100}}, "rating": 10, "rated_at": "2026-10-17T00:00:00Z"}],
            [], [event(1, 1), event(1, 2)],
        )
        self.config = EngineConfig(catalogue_limit=10, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)

    def run_engine(self, **kwargs):
        return generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, **kwargs)

    def test_two_named_series_first_rows_watch_only_exclusion_and_clean_posters(self):
        result = self.run_engine()
        self.assertEqual(result["catalogue_order"], ["series", "movie"])
        self.assertEqual(set(result["catalogues"]), {"series", "movie"})
        series = result["catalogues"]["series"]
        movies = result["catalogues"]["movie"]
        self.assertEqual(series["name"], "Trakt Suggested Series")
        self.assertEqual(movies["name"], "Trakt Suggested Movies")
        movie_ids = {m["id"] for m in movies["payload"]["metas"]}
        self.assertNotIn("tt1", movie_ids)
        self.assertIn("tt100", movie_ids, "Ratings alone must not mark a film watched")
        self.assertNotIn("tt101", movie_ids, "Original-language filter must survive enrichment")
        self.assertNotIn("tt102", movie_ids, "Horror exclusion must survive enrichment")
        self.assertNotIn("tt103", movie_ids, "Existing no-adult rule must remain true")
        series_ids = {m["id"] for m in series["payload"]["metas"]}
        self.assertNotIn("tt10", series_ids)
        self.assertNotIn("tt201", series_ids, "No missing-poster filler cards")
        self.assertNotIn("tt202", series_ids, "Quality floor must remain true")
        for catalogue in result["catalogues"].values():
            for meta in catalogue["payload"]["metas"]:
                self.assertEqual(meta["posterShape"], "poster")
                self.assertTrue(meta["poster"].startswith("https://image.tmdb.org/t/p/w500/"))
                self.assertNotIn("suggested_score", meta)

    def test_returning_season_merges_into_series_without_third_row(self):
        result = self.run_engine(trakt_fetch=lambda path, params: [season(1), season(2, premiered=NOW)])
        metas = result["catalogues"]["series"]["payload"]["metas"]
        self.assertEqual(metas[2]["id"], "tt10")
        self.assertTrue(metas[2]["description"].startswith("Season 2 has started."))
        self.assertEqual(len(result["catalogues"]), 2)
        first_cards = result["diagnostics"]["media"]["series"]["selected_cards"][:2]
        self.assertTrue(all(c["theme"]["gangster_priority"] for c in first_cards))

    def test_missing_episode_metadata_does_not_insert_returner(self):
        def failed(path, params):
            raise TimeoutError("fixture timeout")
        result = self.run_engine(trakt_fetch=failed)
        self.assertNotIn("tt10", {m["id"] for m in result["catalogues"]["series"]["payload"]["metas"]})
        self.assertTrue(result["diagnostics"]["media"]["series"]["returning_decisions"])

    def test_missing_or_malformed_imdb_ids_never_emit_fallback_cards(self):
        self.api.details[("movie", 105)]["external_ids"]["imdb_id"] = None
        self.api.details[("tv", 205)]["external_ids"]["imdb_id"] = "invalid-id"
        self.api.details[("tv", 10)]["external_ids"]["imdb_id"] = None
        result = self.run_engine(trakt_fetch=lambda path, params: [season(1), season(2, premiered=NOW)])
        for catalogue in result["catalogues"].values():
            for meta in catalogue["payload"]["metas"]:
                self.assertRegex(meta["id"], r"^tt[0-9]+$")
                self.assertNotIn(meta["id"], {"tt105", "tt205", "tt10", "invalid-id"})

    def test_total_upstream_failure_raises_instead_of_empty_catalogue(self):
        self.api.failure = True
        with self.assertRaises(GenerationError):
            self.run_engine()

    def test_missing_watched_list_is_not_silently_treated_as_empty_history(self):
        with self.assertRaises(GenerationError):
            generate_catalogues(None, *self.inputs[1:], self.api, now=NOW, config=self.config)


if __name__ == "__main__":
    unittest.main()
