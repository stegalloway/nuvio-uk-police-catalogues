"""Focused contracts for watched exclusion, returning seasons and output quality."""

from datetime import datetime, timedelta, timezone
import unittest

from .engine import (
    FOR_YOU_SOURCE, EngineConfig, GenerationError, derive_episode_progress, evaluate_returning,
    for_you_queries, freshness, generate_catalogues, rank_and_mix_series, thematic_signals,
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

    def test_british_irish_crime_family_prefix_leads_higher_scored_wider_choices(self):
        primary = {"gangster_priority": True, "british_or_irish": True, "investigator_led": False}
        wider = {"gangster_priority": True, "british_or_irish": False, "investigator_led": False}
        police = {"gangster_priority": False, "british_or_irish": True, "investigator_led": True}
        cards = [{"id": "primary" + str(i), "suggested_score": .20 + i / 100, "theme": primary} for i in range(5)]
        cards += [{"id": "wider", "suggested_score": .99, "theme": wider},
                  {"id": "investigation", "suggested_score": 1.0, "theme": police}]
        ordered = rank_and_mix_series(cards, limit=7)
        self.assertEqual([c["id"] for c in ordered[:4]], ["primary4", "primary3", "primary2", "primary1"])
        self.assertEqual({c["id"] for c in ordered[4:]}, {"primary0", "wider", "investigation"})
        self.assertEqual(ordered[4]["id"], "wider")

    def test_primary_country_uses_series_origin_not_coproduction(self):
        for origin, production, expected in ((["US"], ["US", "IE"], False),
                                               (["GB"], ["GB", "US"], True),
                                               (["IE"], ["CA", "IE", "SE", "GB"], True),
                                               ([], ["GB"], False)):
            with self.subTest(origin=origin, production=production):
                theme = thematic_signals({"overview": "A crime family controls the city's criminal underworld.",
                    "origin_country": origin,
                    "production_countries": [{"iso_3166_1": country} for country in production]})
                self.assertTrue(theme["gangster_priority"])
                self.assertEqual(theme["british_or_irish"], expected)


class FixtureAPI:
    def __init__(self):
        self.calls = []
        self.failure = False
        # media -> list of pages of TMDB IDs answering the For You query;
        # None answers it like any other discover query.
        self.for_you = {"movie": None, "tv": None}
        self.for_you_only = set()
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
            if self.is_for_you(params) and self.for_you[media] is not None:
                pages = self.for_you[media]
                page = int(params["page"])
                numbers = pages[page - 1] if page <= len(pages) else []
                return {"results": [self.raw(self.details[(media, n)]) for n in numbers]}
            return {"results": self.candidates(media)}
        media, number = parts[0], int(parts[1])
        if len(parts) > 2:
            return {"results": self.candidates(media)}
        return self.details[(media, number)]

    @staticmethod
    def is_for_you(params):
        return bool(params) and ("with_release_type" in params or "with_status" in params)

    @staticmethod
    def raw(detail):
        raw = dict(detail)
        raw["genre_ids"] = [g["id"] for g in detail["genres"]]
        return raw

    def candidates(self, media):
        return [self.raw(detail) for (m, n), detail in self.details.items()
                if m == media and (m, n) not in self.for_you_only]


def fixture_inputs():
    return (
        [{"movie": {"title": "Seen", "ids": {"imdb": "tt1", "tmdb": 1}}, "plays": 1, "last_watched_at": "2026-10-17T00:00:00Z"}],
        [{"show": show(), "plays": 2, "last_watched_at": "2026-10-17T00:00:00Z"}],
        [{"movie": {"title": "Rated only", "ids": {"imdb": "tt100", "tmdb": 100}}, "rating": 10, "rated_at": "2026-10-17T00:00:00Z"}],
        [], [event(1, 1), event(1, 2)],
    )


def for_you_fixture_api():
    """FixtureAPI whose For You query returns its own titles across two pages."""
    api = FixtureAPI()
    fresh = (NOW - timedelta(days=3)).date().isoformat()
    # 300: strong history match, old. 301: weak match, old. 302: weak, fresh.
    for number, release, weak in ((300, "2025-01-01", False), (301, "2025-01-01", True), (302, fresh, True)):
        detail = api.detail(number, org=True)
        detail["release_date"] = release
        if weak:
            detail.update(origin_country=["US"], production_countries=[{"iso_3166_1": "US"}],
                          keywords={"results": [{"id": 999, "name": "heist"}]},
                          credits={"cast": [{"id": 999}], "crew": []})
        api.details[("movie", number)] = detail
    # 400: fresh British criminal-led crime family. 401: US police-led.
    api.details[("tv", 400)] = dict(api.detail(400, org=True), first_air_date=fresh)
    del api.details[("tv", 400)]["release_date"]  # TMDB TV details carry first_air_date only
    api.details[("tv", 401)] = dict(api.detail(401), origin_country=["US"],
                                    production_countries=[{"iso_3166_1": "US"}])
    api.for_you_only = {("movie", 300), ("movie", 301), ("movie", 302), ("tv", 400), ("tv", 401)}
    # Source order deliberately disagrees with taste; 104/204 overlap history
    # candidates, 300/400 repeat across pages, 1 and 10 are watched.
    api.for_you = {"movie": [[302, 301, 1, 104], [300, 302]], "tv": [[401, 10, 204], [400, 401]]}
    return api


class EndToEndContracts(unittest.TestCase):
    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = fixture_inputs()
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
        self.assertEqual(metas[4]["id"], "tt10")
        self.assertTrue(metas[4]["description"].startswith("Season 2 has started."))
        self.assertEqual(len(result["catalogues"]), 2)
        first_cards = result["diagnostics"]["media"]["series"]["selected_cards"][:4]
        self.assertTrue(all(c["theme"]["gangster_priority"] and c["theme"]["british_or_irish"] for c in first_cards))
        self.assertEqual(result["diagnostics"]["media"]["series"]["primary_crime_prefix_count"], 4)

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


class ForYouContracts(EndToEndContracts):
    """The For You query is one more source, ranked by the same weighting.

    Inherits and re-runs every end-to-end contract with For You rows present.
    """

    def setUp(self):
        super().setUp()
        self.api = for_you_fixture_api()

    def ids(self, result, kind):
        return [m["id"] for m in result["catalogues"][kind]["payload"]["metas"]]

    def test_query_reproduces_discover_plus_for_you_catalogues(self):
        movie, page_two = for_you_queries("movie", NOW, 2)
        self.assertEqual(movie, {
            "sort_by": "primary_release_date.desc", "page": 1, "include_adult": "false",
            "include_video": "false", "vote_count.gte": 100, "vote_average.gte": 7,
            "with_genres": "80|53|9648", "without_genres": "16|27|14|878|10751",
            "with_original_language": "en", "include_image_language": "en-GB,null",
            "with_release_type": "4", "primary_release_date.lte": "2026-10-18"})
        self.assertEqual(page_two, dict(movie, page=2))
        series = for_you_queries("series", NOW, 1)
        self.assertEqual(series, [{
            "sort_by": "first_air_date.desc", "page": 1, "include_adult": "false",
            "vote_count.gte": 30, "vote_average.gte": 7, "with_genres": "80|9648",
            "without_genres": "16|99|10762|10763|10764|10765|10766|10767",
            "with_original_language": "en", "include_image_language": "en-GB,null",
            "with_status": "0|3|4|5", "first_air_date.lte": "2026-10-18"}])
        self.run_engine()
        sent = [(path, params) for path, params in self.api.calls if self.api.is_for_you(params)]
        self.assertEqual(sorted((p, q["page"]) for p, q in sent),
                         [("/discover/movie", 1), ("/discover/movie", 2), ("/discover/tv", 1), ("/discover/tv", 2)])
        self.assertTrue(all(q["language"] == "en-GB" for _, q in sent))

    def test_for_you_titles_enter_the_pool_and_follow_the_weighting(self):
        result = self.run_engine()
        movies = self.ids(result, "movie")
        self.assertTrue({"tt300", "tt301", "tt302"} <= set(movies))
        # Taste first, recency second, For You's date order never.
        self.assertLess(movies.index("tt300"), movies.index("tt302"))
        self.assertLess(movies.index("tt302"), movies.index("tt301"))
        cards = {c["id"]: c for c in result["diagnostics"]["media"]["movie"]["selected_cards"]}
        self.assertEqual(cards["tt302"]["for_you_rank"], 1)
        self.assertEqual(cards["tt300"]["for_you_rank"], 5)
        self.assertEqual(cards["tt300"]["candidate_sources"], [FOR_YOU_SOURCE])
        self.assertGreater(cards["tt302"]["freshness"], .9)
        self.assertLess(cards["tt301"]["freshness"], .01)

    def test_for_you_series_use_primary_prefix_and_mix(self):
        result = self.run_engine()
        cards = result["diagnostics"]["media"]["series"]["selected_cards"]
        self.assertEqual(cards[0]["id"], "tt400", "Fresh UK criminal-led For You title leads on merit")
        self.assertTrue(all(c["theme"]["gangster_priority"] and c["theme"]["british_or_irish"] for c in cards[:4]))
        police = next(c for c in cards if c["id"] == "tt401")
        self.assertTrue(police["theme"]["investigator_led"])
        self.assertGreaterEqual(cards.index(police), 4, "Police-led For You title stays in the broader group")

    def test_for_you_overlaps_are_deduplicated(self):
        result = self.run_engine()
        for kind, overlap in (("movie", "tt104"), ("series", "tt204")):
            ids = self.ids(result, kind)
            self.assertEqual(len(ids), len(set(ids)))
            card = next(c for c in result["diagnostics"]["media"][kind]["selected_cards"] if c["id"] == overlap)
            self.assertIn(FOR_YOU_SOURCE, card["candidate_sources"])
            self.assertIn("history_recommendation", card["candidate_sources"])
        positions = result["diagnostics"]["media"]["movie"]["for_you"]["positions"]
        self.assertEqual(len(positions), len({p["id"] for p in positions}))

    def test_watched_for_you_titles_stay_suppressed(self):
        # 305: Trakt's IMDb ID differs from TMDB's, so only the TMDB-ID gate sees
        # it. 306: watched by IMDb ID alone, so only the IMDb gate sees it.
        for number in (305, 306):
            self.api.details[("movie", number)] = self.api.detail(number, org=True)
            self.api.for_you_only.add(("movie", number))
        self.api.for_you["movie"][1].extend([305, 306])
        watched = self.inputs[0] + [
            {"movie": {"title": "Seen", "ids": {"imdb": "tt9305", "tmdb": 305}}, "plays": 1},
            {"movie": {"title": "Seen", "ids": {"imdb": "tt306"}}, "plays": 1}]
        self.inputs = (watched,) + self.inputs[1:]
        # No season callback: a watched show may only return as a new season.
        result = self.run_engine()
        self.assertTrue({"tt1", "tt305", "tt306"}.isdisjoint(self.ids(result, "movie")))
        self.assertNotIn("tt10", self.ids(result, "series"))
        self.assertEqual(result["diagnostics"]["media"]["movie"]["for_you"]["watched_suppressed_count"], 3)
        self.assertEqual(result["diagnostics"]["media"]["series"]["for_you"]["watched_suppressed_count"], 1)

    def test_for_you_source_inherits_existing_gates(self):
        self.api.for_you["movie"][0].extend([101, 102, 103])
        self.api.for_you["tv"][0].extend([201, 202])
        result = self.run_engine()
        self.assertTrue({"tt101", "tt102", "tt103"}.isdisjoint(self.ids(result, "movie")))
        self.assertTrue({"tt201", "tt202"}.isdisjoint(self.ids(result, "series")))

    def test_private_ranking_fields_stay_out_of_public_metas(self):
        result = self.run_engine()
        for catalogue in result["catalogues"].values():
            for meta in catalogue["payload"]["metas"]:
                self.assertTrue({"for_you_rank", "freshness", "candidate_sources"}.isdisjoint(meta))

    def test_for_you_never_takes_a_history_enrichment_slot(self):
        # A budget of 5 binds on the 6 eligible history films; For You-only 300
        # has the highest raw score and would otherwise take one of the 5 slots.
        self.api.details[("movie", 300)].update(vote_average=9.5, vote_count=5000)
        for_you_only = {n for m, n in self.api.for_you_only if m == "movie"}
        def enriched_films(pages):
            self.api.calls.clear()
            self.config = EngineConfig(catalogue_limit=5, candidate_enrichment_limit=5, minimum_items=3,
                                       max_workers=2, for_you_pages=pages)
            self.run_engine()
            fetched = {int(path.split("/")[2]) for path, _ in self.api.calls
                       if path.count("/") == 2 and path.startswith("/movie/")}
            return fetched - for_you_only, fetched & for_you_only
        without, _ = enriched_films(0)
        history, extra = enriched_films(2)
        self.assertTrue({104, 105, 106, 107, 108} - without, "The budget must bind for this check")
        self.assertEqual(history, without)
        self.assertEqual(extra, {300, 301, 302})

    def test_zero_pages_disables_the_source(self):
        self.config = EngineConfig(**dict(vars(self.config), for_you_pages=0))
        result = self.run_engine()
        self.assertFalse(any(self.api.is_for_you(params) for _, params in self.api.calls))
        self.assertNotIn("tt300", self.ids(result, "movie"))

    def test_freshness_curve(self):
        self.assertEqual(freshness(NOW.date().isoformat(), NOW, 60), 1.)
        self.assertAlmostEqual(freshness((NOW - timedelta(days=60)).date().isoformat(), NOW, 60), .5)
        self.assertEqual(freshness((NOW + timedelta(days=1)).date().isoformat(), NOW, 60), 0.)
        self.assertEqual(freshness("", NOW, 60), 0.)
        self.assertEqual(freshness("not a date", NOW, 60), 0.)


if __name__ == "__main__":
    unittest.main()
