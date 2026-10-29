"""Focused contracts for watched exclusion, returning seasons and output quality."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import unittest

from . import watchly_base as w
from .engine import (
    FOR_YOU_SOURCE, LOVED_EVIDENCE, PROFILES, EngineConfig, GenerationError, aired_by_season,
    apply_dismissal_signal, binged, derive_episode_progress, evaluate_returning, for_you_queries, freshness,
    generate_catalogues, jenny_for_you_queries, jenny_theme, place_keyword,
    rank_and_mix_series, rt_bonus, series_viewing, thematic_signals, viewing_pattern, without_place_keywords,
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

    def test_named_series_first_rows_watch_only_exclusion_and_clean_posters(self):
        result = self.run_engine()
        self.assertEqual(result["catalogue_order"], ["series", "movie", "upcoming"])
        self.assertEqual(set(result["catalogues"]), {"series", "movie", "upcoming"})
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

    def test_returning_season_merges_into_series_without_its_own_row(self):
        result = self.run_engine(trakt_fetch=lambda path, params: [season(1), season(2, premiered=NOW)])
        metas = result["catalogues"]["series"]["payload"]["metas"]
        self.assertEqual(metas[4]["id"], "tt10")
        self.assertTrue(metas[4]["description"].startswith("Season 2 has started."))
        self.assertEqual(set(result["catalogues"]), {"series", "movie", "upcoming"})
        self.assertNotIn("tt10", {m["id"] for m in result["catalogues"]["upcoming"]["payload"]["metas"]})
        first_cards = result["diagnostics"]["media"]["series"]["selected_cards"][:4]
        self.assertTrue(all(c["theme"]["gangster_priority"] and c["theme"]["british_or_irish"] for c in first_cards))
        self.assertEqual(result["diagnostics"]["media"]["series"]["primary_crime_prefix_count"], 4)

    def test_a_show_on_new_and_upcoming_is_left_out_of_suggested(self):
        aired = NOW - timedelta(days=1)
        fetch = lambda path, params: [season(1), season(2, premiered=aired)]
        before = self.run_engine(trakt_fetch=fetch)
        self.assertIn("tt10", [m["id"] for m in before["catalogues"]["series"]["payload"]["metas"]])
        # Season 2 is out and unwatched, so New & Upcoming now carries the news.
        self.api.details[("tv", 10)]["last_episode_to_air"] = {"season_number": 2, "episode_number": 2,
                                                                "air_date": aired.date().isoformat()}
        result = self.run_engine(trakt_fetch=fetch)
        upcoming = [m["id"] for m in result["catalogues"]["upcoming"]["payload"]["metas"]]
        series = [m["id"] for m in result["catalogues"]["series"]["payload"]["metas"]]
        self.assertEqual(upcoming, ["tt10"])
        self.assertNotIn("tt10", series)
        self.assertEqual(set(series), {m["id"] for m in before["catalogues"]["series"]["payload"]["metas"]} - {"tt10"})
        self.assertEqual(result["diagnostics"]["media"]["series"]["on_upcoming_suppressed_count"], 1)

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

    def test_not_interested_titles_are_hidden_like_watched_ones(self):
        baseline = self.run_engine()
        self.assertEqual(self.run_engine(dismissed=[])["catalogues"], baseline["catalogues"])
        movie, other_movie = [m["id"] for m in baseline["catalogues"]["movie"]["payload"]["metas"][:2]]
        series = baseline["catalogues"]["series"]["payload"]["metas"][0]["id"]
        result = self.run_engine(dismissed=[
            {"type": "movie", "movie": {"ids": {"imdb": movie, "tmdb": int(movie[2:])}}},
            # An IMDb-only episode entry hides its whole show.
            {"type": "episode", "episode": {"season": 1, "number": 1}, "show": {"ids": {"imdb": series}}},
            # A show sharing a film's TMDB number never hides that film.
            {"type": "show", "show": {"ids": {"tmdb": int(other_movie[2:])}}},
            {"type": "person", "person": {"ids": {"tmdb": 123}}},
        ])
        movie_ids = {m["id"] for m in result["catalogues"]["movie"]["payload"]["metas"]}
        self.assertNotIn(movie, movie_ids)
        self.assertIn(other_movie, movie_ids)
        self.assertNotIn(series, {m["id"] for m in result["catalogues"]["series"]["payload"]["metas"]})
        for kind in ("movie", "series"):
            self.assertGreaterEqual(result["diagnostics"]["media"][kind]["not_interested"]["suppressed_count"], 1)
        with self.assertRaises(GenerationError):
            self.run_engine(dismissed="not a list")

    def test_not_interested_is_a_mild_dislike_of_similar_titles_only(self):
        # A dismissed film, never a candidate: police keyword (2), its own cast.
        self.api.details[("movie", 900)] = dict(self.api.detail(900), credits={"cast": [{"id": 777}], "crew": []})
        self.api.for_you_only.add(("movie", 900))
        def scores(result):
            return {c["id"]: c["profile_score"] for c in result["diagnostics"]["media"]["movie"]["selected_cards"]}
        before = scores(self.run_engine())
        result = self.run_engine(dismissed=[{"type": "movie", "listed_at": "2026-10-18T05:00:00.000Z",
                                             "movie": {"ids": {"tmdb": 900, "imdb": "tt900"}}}])
        after = scores(result)
        self.assertEqual(result["diagnostics"]["media"]["movie"]["not_interested"]["disliked_profile_titles"], 1)
        self.assertEqual(result["diagnostics"]["media"]["series"]["not_interested"]["disliked_profile_titles"], 0)
        police = [i for i in ("tt105", "tt108") if i in before and i in after]
        organised = [i for i in ("tt100", "tt104", "tt106", "tt107") if i in before and i in after]
        self.assertTrue(police and organised)
        for card in police:
            self.assertLess(after[card], before[card], card + " shares the dismissed keyword")
        for card in organised:
            self.assertEqual(after[card], before[card], card + " shares nothing but genre and country")

    def test_not_interested_show_gets_no_returning_season(self):
        result = self.run_engine(trakt_fetch=lambda path, params: [season(1), season(2, premiered=NOW)],
                                 dismissed=[{"type": "season", "season": {"number": 2}, "show": show()}])
        self.assertNotIn("tt10", {m["id"] for m in result["catalogues"]["series"]["payload"]["metas"]})
        reasons = {d.get("reason") for d in result["diagnostics"]["media"]["series"]["returning_decisions"]}
        self.assertIn("not_interested", reasons)


class DislikeSignalContracts(unittest.TestCase):
    def test_touches_keywords_cast_and_creators_but_never_genres(self):
        scores = {"genres": {80: 10.}, "keywords": {1: 4., 2: 2.}, "cast": {123: 3.},
                  "directors": {}, "eras": {"2020s": 2.}, "countries": {"GB": 1.}}
        detail = dict(FixtureAPI.detail(900), created_by=[{"id": 55}])  # police keyword 2, cast 123
        adjusted, normalised = apply_dismissal_signal(scores, [(detail, NOW.isoformat())], NOW)
        for untouched in ("genres", "eras", "countries"):
            self.assertEqual(adjusted[untouched], scores[untouched])
        self.assertAlmostEqual(adjusted["keywords"][2], 2. - .2 * .7)
        self.assertAlmostEqual(adjusted["cast"][123], 3. - .2 * .9)
        self.assertAlmostEqual(adjusted["directors"][55], -.2 * .9)
        self.assertLess(normalised["directors"][55], 0)  # an otherwise unknown creator turns negative
        self.assertEqual(normalised["keywords"][1], 1.)
        self.assertEqual(scores["keywords"][2], 2.)  # the input is not changed
        # An old listing fades like an old watch, to a tenth.
        old, _ = apply_dismissal_signal(scores, [(detail, (NOW - timedelta(days=400)).isoformat())], NOW)
        self.assertAlmostEqual(old["keywords"][2], 2. - .2 * .1 * .7)


class UpcomingContracts(unittest.TestCase):
    """Upcoming Episodes: caught-up shows whose next episode has a date."""

    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = list(fixture_inputs())
        self.config = EngineConfig(catalogue_limit=10, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)

    def schedule(self, number=10, days=10, last=(1, 2), **episode):
        # The fixture viewer has watched S1 E1-E2 of show 10.
        detail = self.api.details[("tv", number)]
        detail["last_episode_to_air"] = {"season_number": last[0], "episode_number": last[1]}
        detail["next_episode_to_air"] = dict({"air_date": (NOW + timedelta(days=days)).date().isoformat(),
                                              "season_number": 2, "episode_number": 1, "name": "Episode 1"}, **episode)

    def upcoming(self, **kwargs):
        result = generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, **kwargs)
        return result, result["catalogues"]["upcoming"]["payload"]["metas"]

    def test_caught_up_show_with_a_dated_next_episode_is_listed(self):
        self.schedule()
        result, metas = self.upcoming()
        self.assertEqual(result["catalogues"]["upcoming"]["name"], "New & Upcoming Episodes")
        self.assertEqual(result["catalogues"]["upcoming"]["type"], "series")
        self.assertEqual([(m["id"], m["type"]) for m in metas], [("tt10", "series")])
        self.assertTrue(metas[0]["description"].startswith("S2 E1 · Wed 28 Oct\n\n"))
        self.assertEqual(metas[0]["releaseInfo"], "Wed 28 Oct · S2 E1")  # shown under the poster
        self.assertTrue(metas[0]["poster"].startswith("https://image.tmdb.org/t/p/w500/"))

    def test_an_empty_row_is_valid(self):
        result, metas = self.upcoming()
        self.assertEqual(metas, [])
        self.assertEqual(result["catalogue_order"], ["series", "movie", "upcoming"])

    def test_only_caught_up_shows_inside_the_horizon(self):
        # Not caught up (S1 E3 aired, unwatched), past the 90 days, or already aired.
        for last, days in (((1, 3), 10), ((1, 2), 91), ((1, 2), -1)):
            with self.subTest(last=last, days=days):
                self.api = FixtureAPI()
                self.schedule(days=days, last=last)
                self.assertEqual(self.upcoming()[1], [])
        self.api = FixtureAPI()
        self.schedule(days=90)
        self.assertEqual(len(self.upcoming()[1]), 1)
        del self.api.details[("tv", 10)]["next_episode_to_air"]  # ended show
        self.assertEqual(self.upcoming()[1], [])

    def test_suggestion_filters_do_not_apply_but_not_interested_does(self):
        self.schedule(name="The Return")
        self.api.details[("tv", 10)]["vote_average"] = 5.0  # below the suggestion quality floor
        metas = self.upcoming()[1]
        self.assertEqual([m["id"] for m in metas], ["tt10"])
        self.assertTrue(metas[0]["description"].startswith("S2 E1 · The Return · Wed 28 Oct"))
        self.assertEqual(self.upcoming(dismissed=[{"type": "show", "show": show()}])[1], [])

    def test_soonest_first_with_today_and_tomorrow(self):
        self.inputs[1] = self.inputs[1] + [{"show": show(11), "plays": 1, "last_watched_at": "2026-10-17T00:00:00Z"}]
        self.inputs[4] = self.inputs[4] + [event(3, 8, number=11)]
        self.api.details[("tv", 11)] = self.api.detail(11)
        self.schedule(days=1)
        self.schedule(number=11, days=0, last=(3, 8), season_number=4)
        metas = self.upcoming()[1]
        self.assertEqual([m["id"] for m in metas], ["tt11", "tt10"])
        self.assertTrue(metas[0]["description"].startswith("S4 E1 · Today\n\n"))
        self.assertTrue(metas[1]["description"].startswith("S2 E1 · Tomorrow\n\n"))
        self.assertEqual([m["releaseInfo"] for m in metas], ["Today · S4 E1", "Tomorrow · S2 E1"])

    def test_next_year_date_keeps_the_year_out_of_the_card_line(self):
        # Nuvio's Hide Unreleased treats any year after this one in releaseInfo as unreleased.
        self.schedule(days=80, season_number=9)
        meta = self.upcoming()[1][0]
        self.assertEqual(meta["releaseInfo"], "Wed 6 Jan · S9 E1")
        self.assertNotRegex(meta["releaseInfo"], r"\b(19|20)\d{2}\b")
        self.assertTrue(meta["description"].startswith("S9 E1 · Wed 6 Jan 2027\n\n"))

    # "Out now", the 30-day rule (Jenny's): the viewer was caught up before a
    # release and hasn't watched it yet, for 30 days.
    def thirty_day_rule(self):
        for profile in PROFILES.values():
            kind = type(profile)
            self.addCleanup(setattr, kind, "out_now_until_played", kind.out_now_until_played)
            kind.out_now_until_played = False

    def release(self, aired, latest, upcoming_days=None):
        """Trakt seasons for show 10 from {season: [days ago per episode]} (negative = future)."""
        detail = self.api.details[("tv", 10)]
        detail["last_episode_to_air"] = {"season_number": latest[0], "episode_number": latest[1],
                                         "air_date": (NOW - timedelta(days=aired[latest[0]][latest[1] - 1])).date().isoformat()}
        if upcoming_days is not None:
            detail["next_episode_to_air"] = {"season_number": latest[0], "episode_number": latest[1] + 1,
                                             "air_date": (NOW + timedelta(days=upcoming_days)).date().isoformat()}
        seasons = [{"number": s, "episode_count": len(days), "episodes": [
            {"number": i + 1, "first_aired": (NOW - timedelta(days=d)).isoformat()} for i, d in enumerate(days)]}
            for s, days in aired.items()]
        return lambda path, params: seasons

    def test_new_episode_shows_as_out_now(self):
        self.thirty_day_rule()
        # Watched S1 E1-E2; E3 came out two days ago, E4 is due in five.
        fetch = self.release({1: [30, 23, 2, -5]}, latest=(1, 3), upcoming_days=5)
        result, metas = self.upcoming(trakt_fetch=fetch)
        self.assertEqual([(m["id"], m["releaseInfo"]) for m in metas], [("tt10", "Out now · S1 E3")])
        self.assertTrue(metas[0]["description"].startswith("S1 E3 · out Fri 16 Oct\n\n"))
        self.assertEqual(result["diagnostics"]["media"]["upcoming"]["out_now_count"], 1)

    def test_a_whole_new_season_dropped_at_once(self):
        self.thirty_day_rule()
        fetch = self.release({1: [60, 53], 2: [1, 1, 1]}, latest=(2, 3))
        metas = self.upcoming(trakt_fetch=fetch)[1]
        self.assertEqual([(m["id"], m["releaseInfo"]) for m in metas], [("tt10", "Out now · S2 E1–3")])
        self.assertTrue(metas[0]["description"].startswith("S2 E1–3 · out yesterday\n\n"))

    def test_unwatched_episodes_inside_30_days_stay_together(self):
        self.thirty_day_rule()
        # Watched E2; E3 ten days ago and E4 two days ago are both still new.
        fetch = self.release({1: [60, 40, 10, 2]}, latest=(1, 4))
        metas = self.upcoming(trakt_fetch=fetch)[1]
        self.assertEqual([(m["id"], m["releaseInfo"]) for m in metas], [("tt10", "Out now · S1 E3–4")])

    def test_not_caught_up_or_past_30_days_is_left_out(self):
        self.thirty_day_rule()
        # E3 has sat unwatched for 40 days, so E4 two days ago is not "new to a caught-up viewer".
        fetch = self.release({1: [70, 60, 40, 2]}, latest=(1, 4))
        self.assertEqual(self.upcoming(trakt_fetch=fetch)[1], [])
        self.api = FixtureAPI()
        fetch = self.release({1: [70, 60, 31]}, latest=(1, 3))  # out 31 days ago
        self.assertEqual(self.upcoming(trakt_fetch=fetch)[1], [])

    def test_out_now_comes_before_upcoming(self):
        self.thirty_day_rule()
        self.inputs[1] = self.inputs[1] + [{"show": show(11), "plays": 1, "last_watched_at": "2026-10-17T00:00:00Z"}]
        self.inputs[4] = self.inputs[4] + [event(3, 8, number=11)]
        self.api.details[("tv", 11)] = self.api.detail(11)
        self.schedule(number=11, days=1, last=(3, 8), season_number=4)
        fetch = self.release({1: [30, 23, 2]}, latest=(1, 3))
        metas = self.upcoming(trakt_fetch=fetch)[1]
        self.assertEqual([(m["id"], m["releaseInfo"]) for m in metas],
                         [("tt10", "Out now · S1 E3"), ("tt11", "Tomorrow · S4 E1")])


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


def keywords(*ids):
    return {"keywords": {"results": [{"id": number, "name": "fixture"} for number in ids]}}


class JennyProfileContracts(unittest.TestCase):
    """Jenny's profile: her For You queries, broader gates and a theme boost."""

    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = fixture_inputs()
        self.config = EngineConfig(catalogue_limit=20, candidate_enrichment_limit=40, minimum_items=3,
                                   max_workers=2, profile="jenny")

    def run_engine(self, **kwargs):
        return generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, **kwargs)

    def ids(self, result, kind):
        return [m["id"] for m in result["catalogues"][kind]["payload"]["metas"]]

    def test_queries_reproduce_her_discover_plus_for_you_catalogues(self):
        movie = {"sort_by": "primary_release_date.desc", "include_adult": "false", "include_video": "false",
                 "with_original_language": "en", "include_image_language": "en-GB,null",
                 "with_release_type": "4", "primary_release_date.lte": "2026-10-18"}
        self.assertEqual(jenny_for_you_queries("movie", NOW, 1), [
            dict(movie, page=1, **{"vote_count.gte": 30, "vote_average.gte": 6.5, "with_keywords": "33722"}),
            dict(movie, page=1, **{"vote_count.gte": 30, "vote_average.gte": 6.5, "with_genres": "9648|80",
                                   "with_keywords": "207046|10714"}),
            dict(movie, page=1, **{"vote_count.gte": 50, "vote_average.gte": 6, "with_genres": "27"})])
        series = {"sort_by": "first_air_date.desc", "include_adult": "false",
                  "with_original_language": "en", "include_image_language": "en-GB,null",
                  "with_status": "0|3|4|5", "first_air_date.lte": "2026-10-18"}
        self.assertEqual(jenny_for_you_queries("series", NOW, 1), [
            dict(series, page=1, **{"vote_count.gte": 20, "vote_average.gte": 7, "with_keywords": "33722"}),
            dict(series, page=1, **{"vote_count.gte": 20, "vote_average.gte": 7, "with_genres": "9648|80",
                                    "with_keywords": "207046|10714"}),
            dict(series, page=1, **{"vote_count.gte": 20, "vote_average.gte": 6.5, "with_keywords": "315058"})])
        self.assertEqual([q["page"] for q in jenny_for_you_queries("movie", NOW, 2)], [1, 2, 1, 2, 1, 2])
        self.run_engine()
        sent = [(path, params) for path, params in self.api.calls if self.api.is_for_you(params)]
        self.assertEqual(sorted(path for path, _ in sent), ["/discover/movie"] * 6 + ["/discover/tv"] * 6)
        self.assertTrue(all(q["language"] == "en-GB" and q["with_original_language"] == "en" for _, q in sent))
        self.assertFalse(any(q.get("with_genres") == "80|53|9648" for _, q in sent), "Ste's query must not be sent")

    def test_her_gates_admit_horror_documentaries_and_her_floors_in_english_only(self):
        self.api.details[("movie", 109)] = dict(self.api.detail(109), genres=[{"id": 16, "name": "Animation"}])
        self.api.details[("tv", 209)] = dict(self.api.detail(209), genres=[{"id": 99, "name": "Documentary"},
                                                                         {"id": 80, "name": "Crime"}])
        result = self.run_engine()
        movies, series = self.ids(result, "movie"), self.ids(result, "series")
        self.assertIn("tt102", movies, "Horror is one of her themes; Ste's gates exclude it")
        self.assertNotIn("tt101", movies, "Spanish original: Ste's English-original rule applies to her too")
        self.assertIn("tt202", series, "6.9 clears her 6.5 series floor")
        self.assertIn("tt209", series, "True-crime documentary series stay eligible")
        self.assertNotIn("tt109", movies, "Animation stays out")
        self.assertNotIn("tt103", movies, "Adult titles stay out")
        self.assertNotIn("tt1", movies)
        self.assertNotIn("tt10", series)
        self.assertNotIn("tt201", series, "No missing-poster filler cards")

    def test_theme_strengths_come_from_ids_not_titles(self):
        self.assertEqual(jenny_theme(keywords(33722))["true_crime"], 1.)
        self.assertEqual(jenny_theme({"genres": [{"id": 80}, {"id": 99}]})["true_crime"], .5)
        self.assertEqual(jenny_theme(keywords(207046))["murder_mystery"], 1.)
        self.assertEqual(jenny_theme(keywords(10714))["murder_mystery"], 1.)
        self.assertEqual(jenny_theme({"genres": [{"id": 9648}]})["murder_mystery"], .5)
        self.assertEqual(jenny_theme({"genre_ids": [27]})["horror"], 1.)
        # Film details nest keywords under "keywords", TV details under "results".
        self.assertEqual(jenny_theme({"keywords": {"keywords": [{"id": 315058}]}})["horror"], 1.)
        self.assertEqual(jenny_theme(dict(keywords(1), name="True Crime Horror Murder Mystery",
                                          genres=[{"id": 80}]))["strength"], 0.)
        self.assertEqual(jenny_theme(dict(keywords(33722), genre_ids=[9648]))["strength"], 1.)

    def test_her_themes_are_boosted_not_required(self):
        # Identical films except one unknown keyword: 999 on 150, "true crime" on 151.
        self.api.details[("movie", 150)] = dict(self.api.detail(150), **keywords(2, 999))
        self.api.details[("movie", 151)] = dict(self.api.detail(151), **keywords(2, 33722))
        cards = self.run_engine()["diagnostics"]["media"]["movie"]["selected_cards"]
        ids = [c["id"] for c in cards]
        self.assertLess(ids.index("tt151"), ids.index("tt150"))
        by_id = {c["id"]: c for c in cards}
        self.assertEqual(by_id["tt150"]["profile_score"], by_id["tt151"]["profile_score"])
        self.assertEqual(by_id["tt150"]["theme"]["strength"], 0.)
        self.assertAlmostEqual(by_id["tt151"]["suggested_score"] - by_id["tt150"]["suggested_score"], .25)

    def test_series_follow_score_with_no_prefix_and_returners_from_third_place(self):
        result = self.run_engine()
        media = result["diagnostics"]["media"]["series"]
        scores = [c["suggested_score"] for c in media["selected_cards"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(media["primary_crime_prefix_count"], 0)
        metas = self.run_engine(trakt_fetch=lambda path, params: [season(1), season(2, premiered=NOW)])[
            "catalogues"]["series"]["payload"]["metas"]
        self.assertEqual(metas[2]["id"], "tt10")
        self.assertTrue(metas[2]["description"].startswith("Season 2 has started."))

    def test_not_interested_and_upcoming_work_unchanged(self):
        first = self.ids(self.run_engine(), "movie")[0]
        result = self.run_engine(dismissed=[{"type": "movie", "movie": {"ids": {"imdb": first, "tmdb": int(first[2:])}}}])
        self.assertNotIn(first, self.ids(result, "movie"))
        detail = self.api.details[("tv", 10)]
        detail["last_episode_to_air"] = {"season_number": 1, "episode_number": 2}
        detail["next_episode_to_air"] = {"air_date": (NOW + timedelta(days=10)).date().isoformat(),
                                         "season_number": 2, "episode_number": 1}
        self.assertEqual(self.ids(self.run_engine(), "upcoming"), ["tt10"])

    def test_profile_defaults_to_ste_and_unknown_names_fail(self):
        self.assertEqual(EngineConfig().profile, "ste")
        base = dict(catalogue_limit=10, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)
        default = generate_catalogues(*self.inputs, FixtureAPI(), now=NOW, config=EngineConfig(**base))
        explicit = generate_catalogues(*self.inputs, FixtureAPI(), now=NOW, config=EngineConfig(profile="ste", **base))
        self.assertEqual(default["catalogues"], explicit["catalogues"])
        self.assertNotEqual(self.run_engine()["catalogues"]["movie"], default["catalogues"]["movie"])
        with self.assertRaises(ValueError):
            generate_catalogues(*self.inputs, FixtureAPI(), now=NOW, config=EngineConfig(profile="nobody"))


class UntilPlayedContracts(UpcomingContracts):
    """Ste's New & Upcoming: an episode out since the last watch stays until it is played."""

    def watched(self, *episodes):
        """Show 10's watch history: (season, episode, days ago)."""
        self.inputs[4] = [event(s, e, when=(NOW - timedelta(days=d)).isoformat()) for s, e, d in episodes]

    def out_now(self, aired, latest):
        return [(m["id"], m["releaseInfo"]) for m in self.upcoming(trakt_fetch=self.release(aired, latest))[1]]

    def test_a_new_episode_stays_until_it_is_played(self):
        # E3 came out 45 days ago, after E2 was watched: past 30 days, still new.
        self.watched((1, 1, 60), (1, 2, 50))
        self.assertEqual(self.out_now({1: [70, 55, 45, 2]}, (1, 4)), [("tt10", "Out now · S1 E3–4")])
        self.watched((1, 1, 60), (1, 2, 50), (1, 3, 5))     # E3 played, then E4 came out: new again
        self.assertEqual(self.out_now({1: [70, 55, 45, 2]}, (1, 4)), [("tt10", "Out now · S1 E4")])
        self.watched((1, 1, 60), (1, 2, 50), (1, 3, 1))     # E3 played after E4 was out: Continue Watching
        self.assertEqual(self.out_now({1: [70, 55, 45, 2]}, (1, 4)), [])

    def test_a_season_not_begun_shows_though_it_was_out_before_the_finale_was_watched(self):
        self.watched((1, 1, 30), (1, 2, 1))
        self.assertEqual(self.out_now({1: [60, 53], 2: [20, 20, 20]}, (2, 3)), [("tt10", "Out now · S2 E1–3")])

    def test_partway_through_episodes_already_out_is_left_to_continue_watching(self):
        self.watched((1, 1, 1), (1, 2, 1))
        self.assertEqual(self.out_now({1: [20, 20, 20, 20]}, (1, 4)), [])

    def test_an_old_release_names_its_year_in_the_description_only(self):
        self.watched((1, 1, 400), (1, 2, 399))
        meta = self.upcoming(trakt_fetch=self.release({1: [420, 410], 2: [380, 380]}, (2, 2)))[1][0]
        out = (NOW - timedelta(days=380)).date()
        self.assertEqual(meta["releaseInfo"], "Out now · S2 E1–2")
        self.assertTrue(meta["description"].startswith(f"S2 E1–2 · out {out:%a} {out.day} {out:%b} {out.year}\n\n"))

    def test_jenny_also_keeps_a_new_episode_until_it_is_played(self):
        self.watched((1, 1, 60), (1, 2, 50))
        self.config = EngineConfig(**dict(vars(self.config), profile="jenny"))
        self.assertEqual(self.out_now({1: [70, 55, 45, 2]}, (1, 4)), [("tt10", "Out now · S1 E3–4")])


class WatchlistContracts(unittest.TestCase):
    """The watchlist hides suggestions and adds premieres of shows not started yet."""

    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = list(fixture_inputs())
        self.config = EngineConfig(catalogue_limit=10, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)

    def run_engine(self, watchlist=None):
        return generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, watchlist=watchlist)

    def ids(self, result, kind):
        return [m["id"] for m in result["catalogues"][kind]["payload"]["metas"]]

    def new_show(self, number=300, **fields):
        detail = dict(self.api.detail(number), **fields)
        self.api.details[("tv", number)] = detail
        return [{"type": "show", "show": {"ids": {"tmdb": number, "imdb": "tt" + str(number)}}}]

    def test_watchlisted_titles_leave_the_suggestions_without_counting_as_a_dislike(self):
        before = self.ids(self.run_engine(), "movie")
        saved = before[0]
        result = self.run_engine([{"type": "movie", "movie": {"ids": {"imdb": saved, "tmdb": int(saved[2:])}}}])
        after = self.ids(result, "movie")
        self.assertNotIn(saved, after)
        self.assertTrue(set(before) - {saved} <= set(after))
        diagnostics = result["diagnostics"]["media"]["movie"]
        self.assertGreaterEqual(diagnostics["watchlist"]["suppressed_count"], 1)
        self.assertEqual(diagnostics["not_interested"]["disliked_profile_titles"], 0)

    def test_a_season_premiere_of_an_unstarted_watchlisted_show_joins_upcoming(self):
        premiere = {"air_date": (NOW + timedelta(days=10)).date().isoformat(),
                    "season_number": 1, "episode_number": 1, "name": "Pilot"}
        watchlist = self.new_show(next_episode_to_air=premiere)
        metas = self.run_engine(watchlist)["catalogues"]["upcoming"]["payload"]["metas"]
        self.assertEqual([m["id"] for m in metas], ["tt300"])
        self.assertEqual(metas[0]["releaseInfo"], "Wed 28 Oct · S1 E1")
        self.assertTrue(metas[0]["description"].startswith("On your watchlist · S1 E1 · Pilot · Wed 28 Oct\n\n"))
        # Mid-season: someone who hasn't started a show starts at its beginning.
        self.new_show(next_episode_to_air=dict(premiere, episode_number=5))
        self.assertEqual(self.ids(self.run_engine(watchlist), "upcoming"), [])

    def test_a_new_watchlisted_show_that_just_premiered_is_out_now(self):
        aired = (NOW - timedelta(days=2)).date().isoformat()
        watchlist = self.new_show(seasons=[{"season_number": 1, "air_date": aired}],
                                  last_episode_to_air={"season_number": 1, "episode_number": 8, "air_date": aired})
        metas = self.run_engine(watchlist)["catalogues"]["upcoming"]["payload"]["metas"]
        self.assertEqual([(m["id"], m["releaseInfo"]) for m in metas], [("tt300", "Out now · S1 E1–8")])
        self.assertTrue(metas[0]["description"].startswith("On your watchlist · S1 E1–8 · out Fri 16 Oct\n\n"))

    def test_a_watchlisted_premiere_stays_out_now_for_30_days(self):
        for days, listed in ((25, True), (31, False)):
            with self.subTest(days=days):
                aired = (NOW - timedelta(days=days)).date().isoformat()
                watchlist = self.new_show(seasons=[{"season_number": 1, "air_date": aired}],
                                          last_episode_to_air={"season_number": 1, "episode_number": 6, "air_date": aired})
                self.assertEqual(self.ids(self.run_engine(watchlist), "upcoming"), ["tt300"] if listed else [])

    def test_a_later_season_of_an_unstarted_show_says_where_to_start(self):
        premiere = {"air_date": (NOW + timedelta(days=10)).date().isoformat(),
                    "season_number": 4, "episode_number": 1, "name": "Episode 1"}
        metas = self.run_engine(self.new_show(next_episode_to_air=premiere))["catalogues"]["upcoming"]["payload"]["metas"]
        self.assertEqual(metas[0]["releaseInfo"], "Wed 28 Oct · S4 E1")
        self.assertTrue(metas[0]["description"].startswith("On your watchlist · S4 E1 · Wed 28 Oct · start at S1 E1\n\n"))

    def test_the_jenny_profile_ignores_the_watchlist(self):
        premiere = {"air_date": (NOW + timedelta(days=10)).date().isoformat(),
                    "season_number": 1, "episode_number": 1, "name": "Pilot"}
        self.config = EngineConfig(**dict(vars(self.config), profile="jenny"))
        self.assertEqual(self.ids(self.run_engine(self.new_show(next_episode_to_air=premiere)), "upcoming"), [])

    def test_started_shows_keep_their_usual_upcoming_card(self):
        detail = self.api.details[("tv", 10)]
        detail["last_episode_to_air"] = {"season_number": 1, "episode_number": 2}
        detail["next_episode_to_air"] = {"air_date": (NOW + timedelta(days=10)).date().isoformat(),
                                         "season_number": 2, "episode_number": 1, "name": "Episode 1"}
        metas = self.run_engine([{"type": "show", "show": show()}])["catalogues"]["upcoming"]["payload"]["metas"]
        self.assertEqual([m["id"] for m in metas], ["tt10"])
        self.assertTrue(metas[0]["description"].startswith("S2 E1 · Wed 28 Oct"))


class EvidenceOrderContracts(unittest.TestCase):
    """Ste: watched counts most, then the watchlist, then ratings."""

    def evidence(self, profile, **item):
        return PROFILES[profile].evidence(dict({"last_watched": NOW.isoformat()}, **item), NOW)

    def test_watched_beats_the_watchlist_which_beats_ratings(self):
        watched = self.evidence("ste", watch_count=1, completion=1.0)
        loved_watched = self.evidence("ste", watch_count=1, completion=1.0, rating=10)
        rated_only = self.evidence("ste", watch_count=0, completion=0.0, rating=10)
        from .engine import WATCHLIST_EVIDENCE
        self.assertEqual(watched, 1.0)
        self.assertEqual(loved_watched, watched)
        self.assertEqual(self.evidence("ste", watch_count=2, completion=1.0), 2.5)
        self.assertGreater(watched, WATCHLIST_EVIDENCE)
        self.assertGreater(WATCHLIST_EVIDENCE, rated_only)
        self.assertEqual(rated_only, 0.4)

    def test_jenny_keeps_watchlys_weighting(self):
        self.assertEqual(self.evidence("jenny", watch_count=0, completion=0.0, rating=10), 2.5)

    def test_watchlist_titles_count_as_a_like_that_does_not_fade(self):
        detail = {"keywords": {"results": [{"id": 7, "name": "heist"}]},
                  "credits": {"cast": [{"id": 5}], "crew": [{"id": 9, "job": "Director"}]}}
        from .engine import WATCHLIST_EVIDENCE
        for listed in ("2026-10-17T00:00:00Z", "2025-01-01T00:00:00Z"):
            scores, _ = apply_dismissal_signal({}, [(detail, listed)], NOW, evidence=WATCHLIST_EVIDENCE, decays=False)
            self.assertGreater(scores["cast"][5], 0)
            self.assertAlmostEqual(scores["directors"][9], WATCHLIST_EVIDENCE * .9)
        self.assertNotIn("genres", scores)


class ViewingContracts(unittest.TestCase):
    """Ste's series evidence comes from how much of a show he watched."""

    def state(self, episodes, plays=None, idle_days=1, times=()):
        return {"episodes": {(1, n) for n in range(1, episodes + 1)}, "plays": episodes if plays is None else plays,
                "times": list(times), "last": NOW - timedelta(days=idle_days)}

    def test_patterns_follow_the_share_of_aired_episodes(self):
        cases = ((self.state(8), 10, "finished"), (self.state(5), 10, "most"),
                 (self.state(2, idle_days=10), 10, "watching"), (self.state(2, idle_days=90), 10, "dropped"),
                 (self.state(3, idle_days=90), 6, "dropped"), (self.state(4, idle_days=90), 6, "most"),
                 (self.state(5, idle_days=400), 5, "finished"), (self.state(5), None, "unknown"))
        for state, aired, pattern in cases:
            with self.subTest(pattern=pattern, aired=aired):
                self.assertEqual(viewing_pattern(state, {1: aired} if aired else None, NOW)["pattern"], pattern)
        self.assertEqual(viewing_pattern(self.state(1, idle_days=200), {1: 333}, NOW, factual=True)["pattern"], "factual")

    def test_completion_counts_the_seasons_started_only(self):
        # Season 1 finished, season 2 out but not begun: finished, not half watched.
        finished = viewing_pattern(self.state(8, idle_days=200), {1: 8, 2: 8}, NOW)
        self.assertEqual((finished["pattern"], finished["episodes_aired"], finished["seasons_started"]),
                         ("finished", 8, [1]))
        # Only season 3 logged (1 and 2 watched outside Trakt): judged on season 3.
        later = dict(self.state(0, idle_days=5), episodes={(3, n) for n in range(1, 8)})
        self.assertEqual(viewing_pattern(later, {1: 8, 2: 8, 3: 8}, NOW)["pattern"], "finished")
        # Part-way through a started season still counts as only part watched.
        partway = dict(self.state(8), episodes={(1, n) for n in range(1, 9)} | {(2, 1), (2, 2)})
        self.assertEqual(viewing_pattern(partway, {1: 8, 2: 8}, NOW)["pattern"], "most")
        self.assertEqual(viewing_pattern(self.state(1, idle_days=90), {1: 12, 2: 12}, NOW)["pattern"], "dropped")

    def test_only_repeats_a_day_apart_count_as_a_rewatch(self):
        start = NOW - timedelta(days=30)
        def times(gap):
            return {(1, n): [start + timedelta(hours=n), start + timedelta(hours=n) + gap] for n in range(1, 11)}
        rewatched = viewing_pattern(dict(self.state(10, plays=20), episode_times=times(timedelta(days=3))), {1: 10}, NOW)
        self.assertTrue(rewatched["rewatched"])
        self.assertEqual((rewatched["repeats_day_apart"], rewatched["repeats_same_day"]), (10, 0))
        doubled = viewing_pattern(dict(self.state(10, plays=20), episode_times=times(timedelta(minutes=4))), {1: 10}, NOW)
        self.assertFalse(doubled["rewatched"], "A duplicate scrobble minutes later is not a rewatch")
        self.assertEqual((doubled["repeats_day_apart"], doubled["repeats_same_day"]), (0, 10))
        self.assertFalse(viewing_pattern(dict(self.state(10, plays=30)), {1: 10}, NOW)["rewatched"],
                         "Show plays alone, without dated repeats, are not a rewatch")
        few = dict(self.state(2, plays=6), episode_times=times(timedelta(days=3)))
        self.assertFalse(viewing_pattern(few, {1: 10}, NOW)["rewatched"])

    def test_a_binge_is_watched_not_marked(self):
        start = NOW - timedelta(days=3)
        self.assertTrue(binged([start + timedelta(minutes=45 * n) for n in range(3)]))
        self.assertTrue(binged([start, start + timedelta(hours=20), start + timedelta(hours=47)]))
        self.assertFalse(binged([start] * 10), "Marking a season watched at once is not a binge")
        self.assertFalse(binged([start + timedelta(days=2 * n) for n in range(5)]))
        self.assertFalse(binged([start, start + timedelta(hours=1)]))

    def test_aired_episodes_count_regular_seasons_up_to_the_latest_episode(self):
        detail = {"seasons": [{"season_number": 0, "episode_count": 4}, {"season_number": 1, "episode_count": 10},
                              {"season_number": 2, "episode_count": 8}, {"season_number": 3, "episode_count": 10}],
                  "last_episode_to_air": {"season_number": 3, "episode_number": 4}}
        self.assertEqual(aired_by_season(detail), {1: 10, 2: 8, 3: 4})
        self.assertEqual(aired_by_season({"seasons": [{"season_number": 1, "episode_count": 6}]}), {1: 6})
        self.assertIsNone(aired_by_season({"number_of_episodes": 13}))
        self.assertIsNone(aired_by_season({}))

    def test_history_and_seasons_join_by_show_and_ignore_reset(self):
        entry = {"show": show(), "plays": 7, "last_watched_at": "2026-10-10T20:00:00Z", "reset_at": "2026-10-01T00:00:00Z",
                 "seasons": [{"number": 1, "episodes": [{"number": 1, "plays": 2}, {"number": 2, "plays": 1}]},
                             {"number": 0, "episodes": [{"number": 1, "plays": 1}]}]}
        trakt_only = {"show": {"ids": {"trakt": 10}}, "episode": {"season": 2, "number": 1},
                      "watched_at": "2026-10-17T21:00:00Z"}
        special = {"show": show(), "episode": {"season": 0, "number": 1}, "watched_at": "2026-10-16T21:00:00Z"}
        state = series_viewing([entry], [event(1, 2), trakt_only, special])["tt10"]
        self.assertEqual(state["episodes"], {(1, 1), (1, 2), (2, 1)})
        self.assertEqual(state["plays"], 7)
        self.assertEqual(state["last"], datetime(2026, 10, 17, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(len(state["times"]), 2)
        self.assertEqual(set(state["episode_times"]), {(1, 2), (2, 1)})
        self.assertEqual(state["specials"], 1)

    def test_ste_evidence_by_pattern_with_bonuses_and_loved(self):
        ste = PROFILES["ste"]
        def evidence(pattern, rewatched=False, binged=False, **item):
            viewing = {"pattern": pattern, "rewatched": rewatched, "binged": binged}
            return ste.evidence(dict({"last_watched": NOW.isoformat(), "viewing": viewing}, **item), NOW)
        self.assertEqual(evidence("finished"), 1.5)
        self.assertAlmostEqual(evidence("finished", binged=True), 1.8)
        self.assertAlmostEqual(evidence("finished", rewatched=True, binged=True), 2.3)
        self.assertEqual(evidence("most"), 1.0)
        self.assertEqual(evidence("watching"), .6)
        self.assertEqual(evidence("dropped", binged=True), -.2)
        self.assertEqual(evidence("factual"), 0.)
        self.assertEqual(evidence("unknown"), 1.0)
        # Loved is the strongest single signal and does not fade.
        self.assertEqual(evidence("dropped", loved=True, last_watched="2020-01-01T00:00:00Z"), LOVED_EVIDENCE)
        self.assertEqual(ste.evidence({"loved": True, "watch_count": 0, "completion": 0.}, NOW), LOVED_EVIDENCE)

    def test_ste_seeds_follow_viewing_not_ratings(self):
        items = [{"imdb_id": "tt1", "rating": 10, "watch_count": 0},
                 {"imdb_id": "tt2", "viewing": {"pattern": "dropped"}},
                 {"imdb_id": "tt3", "viewing": {"pattern": "finished"}, "rating": 3},
                 {"imdb_id": "tt4", "loved": True, "rating": 2}]
        pool = PROFILES["ste"].seed_pool(items, "series")
        self.assertEqual([i["imdb_id"] for i in pool], ["tt3", "tt4"])
        self.assertTrue(all(i["rating"] is None for i in pool))
        self.assertEqual(w._bucket(pool[1]), "loved")
        self.assertEqual(len(PROFILES["ste"].seed_pool(items, "movie")), 4)
        self.assertEqual(len(PROFILES["jenny"].seed_pool(items, "series")), 4)


class TrustedSignalContracts(unittest.TestCase):
    """The Loved list, Trakt related titles and Trakt's community score."""

    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = list(fixture_inputs())
        self.config = EngineConfig(catalogue_limit=10, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)

    def run_engine(self, **kwargs):
        return generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, **kwargs)

    def ids(self, result, kind):
        return [m["id"] for m in result["catalogues"][kind]["payload"]["metas"]]

    def test_a_watched_loved_show_is_flagged_and_an_unwatched_one_is_hidden_and_liked(self):
        before = self.ids(self.run_engine(), "movie")
        loved = [{"type": "show", "show": show()},
                 {"type": "movie", "movie": {"ids": {"imdb": before[0], "tmdb": int(before[0][2:])}}}]
        result = self.run_engine(loved=loved)
        self.assertNotIn(before[0], self.ids(result, "movie"))
        movie, series = result["diagnostics"]["media"]["movie"], result["diagnostics"]["media"]["series"]
        self.assertEqual(movie["loved"]["liked_profile_titles"], 1)
        self.assertGreaterEqual(movie["loved"]["suppressed_count"], 1)
        self.assertEqual(series["loved"]["history_titles"], 1)
        self.assertEqual(series["loved"]["liked_profile_titles"], 0)
        self.assertTrue(series["series_viewing"][0]["loved"])
        self.assertEqual(series["series_viewing"][0]["evidence"], LOVED_EVIDENCE)

    def test_a_factual_show_leaves_the_taste_profile_but_stays_watched(self):
        self.api.details[("tv", 11)] = self.api.detail(11, org=True)
        self.inputs[1] = self.inputs[1] + [{"show": show(11), "plays": 2, "last_watched_at": "2026-10-17T00:00:00Z"}]
        self.inputs[4] = self.inputs[4] + [event(1, 1, number=11), event(1, 2, number=11)]
        before = self.run_engine()["diagnostics"]["media"]["series"]
        self.assertEqual(before["profile_input_count"], 2)
        self.api.details[("tv", 10)]["genres"] = [{"id": 99, "name": "Documentary"}, {"id": 80, "name": "Crime"}]
        result = self.run_engine()
        series = result["diagnostics"]["media"]["series"]
        self.assertEqual(series["profile_input_count"], 1)
        factual = [v for v in series["series_viewing"] if v["imdb_id"] == "tt10"]
        self.assertEqual([(v["pattern"], v["evidence"]) for v in factual], [("factual", 0.)])
        self.assertNotIn("tt10", self.ids(result, "series"))

    def test_the_jenny_profile_ignores_the_loved_list_and_trakt(self):
        self.config = EngineConfig(**dict(vars(self.config), profile="jenny"))
        calls = []
        result = self.run_engine(loved=[{"type": "show", "show": show()}],
                                 trakt_lookup=lambda path, params: calls.append(path))
        self.assertEqual(calls, [])
        self.assertEqual(result["diagnostics"]["media"]["series"]["loved"]["history_titles"], 0)
        self.assertEqual(result["diagnostics"]["media"]["series"]["series_viewing"], [])

    def test_trakt_related_titles_join_the_candidates(self):
        self.api.details[("tv", 500)] = self.api.detail(500, org=True)
        self.api.for_you_only.add(("tv", 500))
        self.assertNotIn("tt500", self.ids(self.run_engine(), "series"))
        paths = []
        def lookup(path, params):
            paths.append((path, params))
            return [{"title": "Show 500", "ids": {"tmdb": 500, "imdb": "tt500"}}] if path.endswith("/related") else None
        result = self.run_engine(trakt_lookup=lookup)
        self.assertIn("tt500", self.ids(result, "series"))
        self.assertIn(("/shows/tt10/related", {"limit": 10}), paths)
        related = result["diagnostics"]["media"]["series"]["trakt_related"]
        self.assertIn({"id": "tt500", "position": self.ids(result, "series").index("tt500") + 1,
                       "only_trakt_related": True}, related["positions"])

    def test_a_title_trakt_scores_low_leaves_and_the_row_refills(self):
        # A 4-title row over 6 eligible films leaves spares to refill from.
        self.config = EngineConfig(**dict(vars(self.config), catalogue_limit=4))
        before = self.ids(self.run_engine(), "movie")
        self.assertEqual(len(before), 4)
        def lookup(votes):
            def answer(path, params):
                if path == "/movies/" + before[0] + "/ratings":
                    return {"rating": 6.2, "votes": votes}
                return {"rating": 7.9, "votes": 900} if path.endswith("/ratings") else []
            return answer
        result = self.run_engine(trakt_lookup=lookup(500))
        after = self.ids(result, "movie")
        self.assertNotIn(before[0], after)
        self.assertEqual(len(after), len(before))
        dropped = result["diagnostics"]["media"]["movie"]["trakt_rating"]["dropped"]
        self.assertEqual([(d["id"], d["rating"], d["votes"]) for d in dropped], [(before[0], 6.2, 500)])
        # Too few Trakt votes: TMDB alone decides.
        self.assertIn(before[0], self.ids(self.run_engine(trakt_lookup=lookup(50)), "movie"))

    def test_a_failing_trakt_lookup_changes_nothing(self):
        before = self.run_engine()
        def failing(path, params):
            raise TimeoutError("fixture")
        after = self.run_engine(trakt_lookup=failing)
        for kind in ("series", "movie", "upcoming"):
            self.assertEqual(self.ids(after, kind), self.ids(before, kind))


class SeriesTasteContracts(unittest.TestCase):
    """Ste's series: wider gate, no place keywords, lasting Not Interested, Loved seeds and votes."""

    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = list(fixture_inputs())
        self.config = EngineConfig(catalogue_limit=20, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)

    def run_engine(self, **kwargs):
        return generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, **kwargs)

    def ids(self, result, kind="series"):
        return [m["id"] for m in result["catalogues"][kind]["payload"]["metas"]]

    def show_detail(self, number, genres, keywords):
        detail = dict(self.api.detail(number), genres=[{"id": g, "name": str(g)} for g in genres],
                      keywords={"results": [{"id": i, "name": n} for i, n in keywords]})
        self.api.details[("tv", number)] = detail
        return detail

    def loved(self, *numbers):
        return [{"type": "show", "show": {"ids": {"tmdb": n, "imdb": "tt" + str(n)}}} for n in numbers]

    def test_place_keywords_never_reach_matching(self):
        self.assertTrue(place_keyword({"name": "london, england"}))
        self.assertTrue(place_keyword({"name": "Los Angeles, California"}))
        self.assertTrue(place_keyword({"name": "new york city"}))
        self.assertFalse(place_keyword({"name": "lapd"}))
        self.assertFalse(place_keyword({"name": "british spy"}))
        dropped = Counter()
        fetch = without_place_keywords(lambda path, params: {"keywords": {"results": [
            {"id": 1, "name": "london, england"}, {"id": 2, "name": "espionage"}]}}, dropped)
        self.assertEqual(fetch("/tv/1", None)["keywords"]["results"], [{"id": 2, "name": "espionage"}])
        self.assertEqual(dropped, Counter({"london, england": 1}))

    def test_series_may_be_action_war_or_legal_drama_but_not_any_drama(self):
        ste, jenny = PROFILES["ste"], PROFILES["jenny"]
        action = {"genres": [{"id": 10759}], "keywords": {"results": []}}
        legal = {"genres": [{"id": 18}], "keywords": {"results": [{"id": 9, "name": "law firm"}]}}
        plain = {"genres": [{"id": 18}], "keywords": {"results": [{"id": 8, "name": "hospital"}]}}
        self.assertTrue(ste.final_gate(action, "series"))
        self.assertTrue(ste.final_gate(legal, "series"))
        self.assertFalse(ste.final_gate(plain, "series"))
        self.assertTrue(ste.final_gate(plain, "movie"))
        self.assertTrue(jenny.final_gate(plain, "series"))
        self.assertIn(10768, ste.policy("series").required_any_genres)

    def test_loved_shows_seed_a_legal_drama_and_vote_for_it(self):
        self.show_detail(600, [10768, 18], [(80, "military"), (81, "special forces")])
        self.show_detail(601, [18], [(50, "lawyer")])
        self.show_detail(602, [18], [(51, "hospital")])
        self.assertNotIn("tt601", self.ids(self.run_engine()), "Without a Loved seed a drama never qualifies")
        result = self.run_engine(loved=self.loved(600))
        series = self.ids(result)
        self.assertIn("tt601", series)
        self.assertNotIn("tt602", series)
        seeds = result["diagnostics"]["media"]["series"]["loved_seeds"]
        self.assertEqual(seeds["titles"], ["Show 600"])
        legal = [p for p in seeds["positions"] if p["id"] == "tt601"][0]
        self.assertTrue(legal["legal_drama_only"])
        self.assertEqual(legal["loved_votes"], ["Show 600"])

    def test_each_loved_show_that_points_to_a_title_adds_a_capped_bonus(self):
        for number in (600, 606, 607, 608):
            self.show_detail(number, [80], [(80 + number, "theme " + str(number))])
        def scores(loved, weight):
            original = PROFILES["ste"].loved_vote_weight
            type(PROFILES["ste"]).loved_vote_weight = weight
            try:
                diagnostics = self.run_engine(loved=self.loved(*loved))["diagnostics"]["media"]["series"]
            finally:
                type(PROFILES["ste"]).loved_vote_weight = original
            return {c["title"]: c for c in diagnostics["loved_seeds"]["card_scores"]}
        two, none = scores((600, 606), .05), scores((600, 606), 0.)
        self.assertEqual(two["Movie 203"]["loved_votes"], 2)
        self.assertAlmostEqual(two["Movie 203"]["score"] - none["Movie 203"]["score"], .10, places=3)
        four, none = scores((600, 606, 607, 608), .05), scores((600, 606, 607, 608), 0.)
        self.assertEqual(four["Movie 203"]["loved_votes"], 4)
        self.assertAlmostEqual(four["Movie 203"]["score"] - none["Movie 203"]["score"], .15, places=3)

    def test_a_loved_film_seeds_films_and_votes_for_them(self):
        self.api.details[("movie", 109)] = self.api.detail(109)
        loved = [{"type": "movie", "movie": {"ids": {"tmdb": 109, "imdb": "tt109"}}}]
        result = self.run_engine(loved=loved)
        films = result["diagnostics"]["media"]["movie"]["loved_seeds"]
        self.assertEqual(films["titles"], ["Movie 109"])
        self.assertTrue(all(c["voters"] == ["Movie 109"] for c in films["card_scores"]))
        self.assertNotIn("tt109", self.ids(result, "movie"), "A Loved film is not suggested")
        self.assertEqual(result["diagnostics"]["media"]["series"]["loved_seeds"]["titles"], [])

    def test_a_single_vote_film_needs_a_tomatometer_of_80(self):
        self.api.details[("movie", 600)] = self.api.detail(600)
        self.api.details[("movie", 701)] = self.api.detail(701)
        self.api.for_you_only.add(("movie", 701))      # only the Loved film's recommendations return it
        def fetch(path, params=None):
            value = self.api(path, params)
            if path.startswith("/movie/600/recommendations"):
                value = {"results": value["results"] + [self.api.raw(self.api.details[("movie", 701)])]}
            return value
        loved = [{"type": "movie", "movie": {"ids": {"tmdb": 600, "imdb": "tt600"}}}]
        def run(critics):
            lookup = None if critics == "no lookup" else (
                lambda kind, tmdb_id, imdb_id: {"tomatometer": critics if imdb_id == "tt701" else 90., "popcornmeter": None})
            return generate_catalogues(*self.inputs, fetch, now=NOW, config=self.config, loved=loved, rt_lookup=lookup)
        self.assertIn("tt701", self.ids(run(85.), "movie"))
        low = run(70.)
        self.assertNotIn("tt701", self.ids(low, "movie"))
        self.assertEqual([d["title"] for d in low["diagnostics"]["media"]["movie"]["loved_seeds"]["rt_single_vote"]["dropped"]],
                         ["Movie 701"])
        self.assertNotIn("tt701", self.ids(run(None), "movie"), "No critics score: it stays out")
        self.assertNotIn("tt701", self.ids(run("no lookup"), "movie"), "Without MDBList the two-vote rule stands")

    def test_a_title_only_loved_seeds_found_needs_two_loved_votes(self):
        for number in (600, 606):
            self.show_detail(number, [80], [(90 + number, "theme " + str(number))])
        self.api.details[("tv", 701)] = self.api.detail(701, org=True)
        self.api.for_you_only.add(("tv", 701))      # no history seed or discover query returns it
        def fetch(voters):
            def call(path, params=None):
                value = self.api(path, params)
                if any(path.startswith("/tv/" + str(n) + "/recommendations") for n in voters):
                    value = {"results": value["results"] + [self.api.raw(self.api.details[("tv", 701)])]}
                return value
            return call
        def run(voters):
            return generate_catalogues(*self.inputs, fetch(voters), now=NOW, config=self.config,
                                       loved=self.loved(600, 606))
        one = run((600,))
        self.assertNotIn("tt701", self.ids(one))
        self.assertIn("Show 701", one["diagnostics"]["media"]["series"]["loved_seeds"]["loved_only_dropped"])
        self.assertIn("tt701", self.ids(run((600, 606))))

    def test_not_interested_no_longer_fades(self):
        detail = {"keywords": {"results": [{"id": 7, "name": "heist"}]}, "credits": {"cast": [], "crew": []}}
        old = "2025-01-01T00:00:00Z"
        lasting, _ = apply_dismissal_signal({}, [(detail, old)], NOW, decays=False)
        faded, _ = apply_dismissal_signal({}, [(detail, old)], NOW)
        self.assertAlmostEqual(lasting["keywords"][7], -.2 * .7)
        self.assertGreater(faded["keywords"][7], lasting["keywords"][7])

    def test_the_jenny_profile_keeps_her_series_taste(self):
        self.show_detail(600, [10768], [(80, "military")])
        self.config = EngineConfig(**dict(vars(self.config), profile="jenny"))
        result = self.run_engine(loved=self.loved(600))
        self.assertEqual(result["diagnostics"]["media"]["series"]["loved_seeds"]["titles"], [])
        self.assertEqual(result["diagnostics"]["place_keywords_dropped"], [])


class RottenTomatoesContracts(unittest.TestCase):
    """High Tomatometer and Popcornmeter scores lift a title; a missing score is neutral."""

    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = list(fixture_inputs())
        self.config = EngineConfig(catalogue_limit=10, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)

    def run_engine(self, **kwargs):
        return generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, **kwargs)

    def ids(self, result, kind="movie"):
        return [m["id"] for m in result["catalogues"][kind]["payload"]["metas"]]

    def test_the_bonus_scales_from_70_to_100(self):
        self.assertEqual(rt_bonus(None, .1, 70), 0.)
        self.assertEqual(rt_bonus(60, .1, 70), 0.)
        self.assertEqual(rt_bonus(70, .1, 70), 0.)
        self.assertAlmostEqual(rt_bonus(85, .1, 70), .05)
        self.assertAlmostEqual(rt_bonus(100, .1, 70), .1)

    def test_a_well_reviewed_title_rises_and_a_missing_score_changes_nothing(self):
        before = self.ids(self.run_engine())
        last = before[-1]
        calls = []
        def lookup(kind, tmdb_id, imdb_id):
            calls.append((kind, imdb_id))
            return {"tomatometer": 100., "popcornmeter": 100.} if imdb_id == last else {"tomatometer": None, "popcornmeter": None}
        result = self.run_engine(rt_lookup=lookup)
        self.assertLess(self.ids(result).index(last), before.index(last))
        self.assertEqual(sorted(self.ids(result)), sorted(before))
        diagnostics = result["diagnostics"]["media"]["movie"]["rotten_tomatoes"]
        self.assertEqual((diagnostics["tomatometer_count"], diagnostics["popcornmeter_count"]), (1, 1))
        self.assertEqual([c["bonus"] for c in diagnostics["cards"] if c["id"] == last], [.2])
        self.assertTrue({kind for kind, _ in calls} == {"series", "movie"})
        def failing(kind, tmdb_id, imdb_id):
            raise TimeoutError("fixture")
        self.assertEqual(self.ids(self.run_engine(rt_lookup=failing)), before)
        self.assertEqual(self.ids(self.run_engine(rt_lookup=lambda *a: None)), before)

    def test_every_series_the_row_shows_is_looked_up_beyond_the_best_scores(self):
        ste = type(PROFILES["ste"])
        original = ste.rt_candidates, ste.rt_film_candidates
        ste.rt_candidates = ste.rt_film_candidates = 1
        looked = []
        try:
            result = self.run_engine(rt_lookup=lambda kind, tmdb_id, imdb_id: looked.append((kind, imdb_id)) or None)
        finally:
            ste.rt_candidates, ste.rt_film_candidates = original
        shown = set(self.ids(result, "series"))
        self.assertTrue(shown <= {imdb for kind, imdb in looked if kind == "series"})
        self.assertEqual(sum(kind == "movie" for kind, _ in looked), 1, "Films keep the score cut")

    def test_titles_just_beyond_the_series_row_are_looked_up_too(self):
        ste = type(PROFILES["ste"])
        saved = ste.rt_candidates, ste.rt_row_margin
        self.config = EngineConfig(**dict(vars(self.config), catalogue_limit=3))
        def looked(margin):
            ste.rt_candidates, ste.rt_row_margin = 1, margin
            calls = []
            try:
                self.run_engine(rt_lookup=lambda kind, tmdb_id, imdb_id: calls.append((kind, imdb_id)) or None)
            finally:
                ste.rt_candidates, ste.rt_row_margin = saved
            return {imdb for kind, imdb in calls if kind == "series"}
        without, margin = looked(0), looked(2)
        self.assertLess(without, margin)
        self.assertGreaterEqual(len(margin), 5)

    def test_the_film_floor_keeps_only_films_looked_up_and_scoring_well(self):
        ste = type(PROFILES["ste"])
        before = self.ids(self.run_engine(), "movie")
        low, unknown = before[0], before[1]
        def lookup(kind, tmdb_id, imdb_id):
            if imdb_id == low:
                return {"tomatometer": 95., "popcornmeter": 60.}
            if imdb_id == unknown:
                return {"tomatometer": None, "popcornmeter": None}
            return {"tomatometer": 90., "popcornmeter": 90.}
        ste.rt_film_floor = 70.
        try:
            result = self.run_engine(rt_lookup=lookup)
        finally:
            ste.rt_film_floor = None
        films = self.ids(result, "movie")
        self.assertNotIn(low, films, "One score under the floor is enough to leave")
        self.assertIn(unknown, films, "Unknown scores don't count against a film")
        dropped = result["diagnostics"]["media"]["movie"]["loved_seeds"]["rt_floor_dropped"]
        self.assertEqual([(d["tomatometer"], d["popcornmeter"]) for d in dropped], [(95., 60.)])

    def test_the_jenny_profile_never_looks_up_rotten_tomatoes(self):
        self.config = EngineConfig(**dict(vars(self.config), profile="jenny"))
        calls = []
        self.run_engine(rt_lookup=lambda *a: calls.append(a))
        self.assertEqual(calls, [])


class AcclaimedFilmContracts(unittest.TestCase):
    """Ste's films: another genre joins only with both Rotten Tomatoes scores at 80 or more."""

    def setUp(self):
        self.api = FixtureAPI()
        self.inputs = list(fixture_inputs())
        self.config = EngineConfig(catalogue_limit=20, candidate_enrichment_limit=20, minimum_items=3, max_workers=2)
        self.api.details[("movie", 710)] = dict(self.api.detail(710), genres=[{"id": 18, "name": "Drama"}])
        self.api.details[("movie", 711)] = dict(self.api.detail(711), genres=[{"id": 16, "name": "Animation"},
                                                                             {"id": 18, "name": "Drama"}])

    def run_engine(self, scores, **kwargs):
        lookup = None if scores is None else (lambda kind, tmdb_id, imdb_id: scores.get(imdb_id, {"tomatometer": 75.,
                                                                                                   "popcornmeter": 75.}))
        return generate_catalogues(*self.inputs, self.api, now=NOW, config=self.config, rt_lookup=lookup, **kwargs)

    @staticmethod
    def films(result):
        return [m["id"] for m in result["catalogues"]["movie"]["payload"]["metas"]]

    def test_another_genre_joins_only_when_both_scores_reach_80(self):
        acclaimed = {"tt710": {"tomatometer": 85., "popcornmeter": 90.}, "tt711": {"tomatometer": 95., "popcornmeter": 95.}}
        result = self.run_engine(acclaimed)
        self.assertIn("tt710", self.films(result))
        self.assertNotIn("tt711", self.films(result), "Animation stays out however acclaimed")
        report = result["diagnostics"]["media"]["movie"]["acclaimed"]
        self.assertEqual([k["title"] for k in report["kept"]], ["Movie 710"])
        self.assertEqual([p["title"] for p in report["positions"]], ["Movie 710"])
        for scores in ({"tomatometer": 85., "popcornmeter": 70.}, {"tomatometer": 70., "popcornmeter": 95.},
                       {"tomatometer": 85., "popcornmeter": None}):
            self.assertNotIn("tt710", self.films(self.run_engine({"tt710": scores})), scores)
        self.assertNotIn("tt710", self.films(self.run_engine(None)), "Without MDBList only the core genres")

    def test_the_acclaimed_path_never_reorders_the_core_films(self):
        result = self.run_engine({"tt710": {"tomatometer": 85., "popcornmeter": 90.}})
        ste = type(PROFILES["ste"])
        original = ste.acclaimed_kinds
        try:
            ste.acclaimed_kinds = frozenset()
            core = self.run_engine({})
        finally:
            ste.acclaimed_kinds = original
        self.assertEqual([i for i in self.films(result) if i != "tt710"], self.films(core))

    def test_wider_profile_queries_find_acclaimed_science_fiction(self):
        self.api.details[("movie", 712)] = dict(self.api.detail(712), genres=[{"id": 878, "name": "Science Fiction"},
                                                                             {"id": 18, "name": "Drama"}])
        self.api.for_you_only.add(("movie", 712))      # no seed or core query returns it
        def fetch(path, params=None):
            value = self.api(path, params)
            if path.startswith("/discover/movie") and "878" not in str((params or {}).get("without_genres", "")).split("|"):
                value = {"results": value["results"] + [self.api.raw(self.api.details[("movie", 712)])]}
            return value
        def run(scores):
            return generate_catalogues(*self.inputs, fetch, now=NOW, config=self.config,
                                       rt_lookup=lambda kind, tmdb_id, imdb_id: scores.get(imdb_id, {}))
        result = run({"tt712": {"tomatometer": 91., "popcornmeter": 91.}})
        self.assertIn("tt712", self.films(result))
        self.assertIn("Movie 712", [r["title"] for r in result["diagnostics"]["media"]["movie"]["acclaimed"]["ranked"]])
        self.assertNotIn("tt712", self.films(run({"tt712": {"tomatometer": 91., "popcornmeter": 79.}})))
        core = [i for i in self.films(result) if i != "tt712"]
        self.assertEqual(core, self.films(run({})), "The wider queries never change the core films")

    def test_jenny_has_no_acclaimed_path(self):
        self.config = EngineConfig(**dict(vars(self.config), profile="jenny"))
        report = self.run_engine({"tt710": {"tomatometer": 85., "popcornmeter": 90.}})["diagnostics"]["media"]["movie"]["acclaimed"]
        self.assertEqual(report["enriched_count"], 0)


class FilmAgeContracts(unittest.TestCase):
    FILM = {"release_date": "1999-12-31", "genre_ids": [80], "original_language": "en",
            "vote_average": 8.0, "vote_count": 5000}

    def test_ste_films_start_in_2000(self):
        policy = PROFILES["ste"].policy("movie")
        self.assertEqual(policy.year_min, 2000)
        self.assertFalse(w.passes_filters(self.FILM, policy, NOW))
        self.assertTrue(w.passes_filters(dict(self.FILM, release_date="2000-01-01"), policy, NOW))

    def test_film_genre_rules_can_be_widened_or_removed(self):
        ste = type(PROFILES["ste"])
        war = dict(self.FILM, release_date="2016-01-15", genre_ids=[10752, 18])
        drama = dict(war, genre_ids=[18])
        horror = dict(self.FILM, release_date="2016-01-15", genre_ids=[27, 53])
        self.assertTrue(w.passes_filters(war, PROFILES["ste"].policy("movie"), NOW), "Action and war are core")
        self.assertFalse(w.passes_filters(drama, PROFILES["ste"].policy("movie"), NOW))
        original = ste.film_required_genres
        try:
            ste.film_required_genres, ste.film_excluded_genres = (), ()
            policy = PROFILES["ste"].policy("movie")
            self.assertTrue(w.passes_filters(horror, policy, NOW))
            self.assertEqual(policy.year_min, 2000)
        finally:
            ste.film_required_genres, ste.film_excluded_genres = original, None
        self.assertFalse(w.passes_filters(horror, PROFILES["ste"].policy("movie"), NOW))

    def test_series_and_jenny_keep_their_floors(self):
        self.assertIsNone(PROFILES["ste"].policy("series").year_min)
        self.assertIsNone(PROFILES["jenny"].policy("movie").year_min)
        self.assertIsNone(PROFILES["jenny"].policy("series").year_min)


if __name__ == "__main__":
    unittest.main()
