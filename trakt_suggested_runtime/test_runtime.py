"""Publication contracts: complete sources, clean output, terminal pages, rollback."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from trakt_suggested_runtime.publisher import (
    CATALOGUES, INPUT_FILES, JsonHttpClient, JsonResponse, RefreshError,
    SourceError, SourceFailures, TmdbClient, TraktClient, atomic_json,
    generate_and_publish, load_history_snapshots, main, manifest,
    pagination_offsets, publish_result, read_page, validate_output,
    write_catalog_pages,
)


def metas(count, kind):
    return [{"id": "tt" + str((1000 if kind == "series" else 2000) + index),
             "imdb_id": "tt" + str((1000 if kind == "series" else 2000) + index),
             "type": kind, "name": "Fixture " + str(index), "posterShape": "poster",
             "poster": "https://image.tmdb.org/t/p/w500/example.jpg",
             "description": "Public plot summary.", "genres": ["Crime"], "releaseInfo": "2025"}
            for index in range(count)]


def generated(count=28):
    return {"catalogue_order": ["series", "movie"], "catalogues": {
        kind: {"type": kind, "id": slug, "name": title, "payload": {"metas": metas(count, kind)}}
        for kind, slug, title in CATALOGUES
    }, "diagnostics": {"private_watched_history": ["PRIVATE-DATA-MUST-NOT-BE-PUBLISHED"]}}


def bytes_snapshot(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


class FixtureHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.failures = SourceFailures()

    def get(self, path, params=None):
        self.calls.append((path, params))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            self.failures.record("fixture", "network")
            raise value
        return value

    def fail(self, reason):
        self.failures.record("fixture", reason)
        raise SourceError(reason)


class PaginationContracts(unittest.TestCase):
    def test_exact_empty_terminal_and_cached_offsets_survive_growth_and_shrink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            old_offsets = set()
            for count in (28, 55, 26, 40, 61, 29, 20):
                with self.subTest(count=count):
                    self.assertEqual(publish_result(generated(count), target), {"series": count, "movie": count})
                    for kind, slug, _ in CATALOGUES:
                        folder = target / "catalog" / kind / slug
                        self.assertEqual(read_page(folder / f"skip={count}.json"), [])
                        for start in old_offsets | {0, count}:
                            offset, collected = start, []
                            for _ in range(count + 2):
                                page = read_page(folder / f"skip={offset}.json")
                                if not page:
                                    break
                                collected.extend(page)
                                offset += len(page)
                            else:
                                self.fail("Cached pagination did not terminate")
                            self.assertEqual(collected, metas(count, kind)[start:])
                        old_offsets |= {int(p.stem.removeprefix("skip=")) for p in folder.glob("skip=*.json")}

    def test_empty_helper_response_has_no_fake_end_card(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_catalog_pages(folder, "test", [])
            self.assertEqual(read_page(folder / "test.json"), [])
            self.assertEqual(read_page(folder / "test" / "skip=0.json"), [])

    def test_validator_rejects_missing_terminal_and_stale_cached_page(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            publish_result(generated(), target)
            terminal = target / "catalog/series/trakt-suggested-series/skip=28.json"
            terminal.unlink()
            with self.assertRaises(RefreshError):
                validate_output(target)
            atomic_json(terminal, {"metas": metas(1, "series")})
            with self.assertRaises(RefreshError):
                validate_output(target)


class PublicationContracts(unittest.TestCase):
    def test_manifest_has_exact_names_series_first_and_catalogue_only_resources(self):
        result = manifest()
        self.assertEqual(result["types"], ["series", "movie"])
        self.assertEqual(result["resources"], ["catalog"])
        self.assertEqual([c["name"] for c in result["catalogs"]], ["Trakt Suggested Series", "Trakt Suggested Movies"])

    def test_private_diagnostics_never_enter_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            publish_result(generated(), target)
            all_bytes = b"".join(bytes_snapshot(target).values())
            self.assertNotIn(b"PRIVATE-DATA", all_bytes)
            self.assertNotIn(b"private_watched_history", all_bytes)

    def test_validation_failure_leaves_every_existing_file_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            publish_result(generated(), target)
            before = bytes_snapshot(target)
            bad = generated(40)
            bad["catalogues"]["movie"]["payload"]["metas"][0]["poster"] = ""
            with self.assertRaises(RefreshError):
                publish_result(bad, target)
            self.assertEqual(bytes_snapshot(target), before)

    def test_private_fields_and_credential_artwork_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for change in ({"seed_title": "Private watched show"},
                           {"poster": "https://image.tmdb.org/t/p/w500/poster.jpg?api_key=secret"},
                           {"poster": "https://api.ratingposterdb.com/key/imdb/tt123.jpg"},
                           {"id": "__end_of_list"}):
                result = generated()
                result["catalogues"]["series"]["payload"]["metas"][0].update(change)
                with self.subTest(change=change), self.assertRaises(RefreshError):
                    publish_result(result, Path(directory) / "suggested")

    def test_caught_callback_failure_cannot_publish_partial_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            publish_result(generated(), target)
            before = bytes_snapshot(target)
            failures = SourceFailures()
            def generator(*args, **kwargs):
                failures.record("trakt", "season_metadata_timeout")
                return generated(40)
            with self.assertRaises(SourceError):
                generate_and_publish({key: [] for key in INPUT_FILES}, None, None, target,
                                     failures=failures, generator=generator)
            self.assertEqual(bytes_snapshot(target), before)

    def test_missing_input_and_engine_failure_leave_old_catalogues(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            publish_result(generated(), target)
            before = bytes_snapshot(target)
            with self.assertRaises(SourceError):
                generate_and_publish({}, None, None, target, failures=SourceFailures())
            def failed(*args, **kwargs):
                raise RuntimeError("private detail")
            with self.assertRaises(RuntimeError):
                generate_and_publish({key: [] for key in INPUT_FILES}, None, None, target,
                                     failures=SourceFailures(), generator=failed)
            self.assertEqual(bytes_snapshot(target), before)

    def test_directory_promotion_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            publish_result(generated(), target)
            before = bytes_snapshot(target)
            real_replace = os.replace
            def fail_promotion(source, destination):
                if Path(source).name == "output" and Path(destination) == target:
                    raise PermissionError("injected directory promotion failure")
                return real_replace(source, destination)
            with patch("trakt_suggested_runtime.publisher.os.replace", side_effect=fail_promotion):
                with self.assertRaises(PermissionError):
                    publish_result(generated(40), target)
            self.assertEqual(bytes_snapshot(target), before)

    def test_real_engine_with_for_you_source_publishes_only_public_fields(self):
        from trakt_suggested_engine.test_engine import NOW, fixture_inputs, for_you_fixture_api
        api, inputs = for_you_fixture_api(), fixture_inputs()
        histories = dict(zip(INPUT_FILES, inputs))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            diagnostics = Path(directory) / "private" / "diagnostics.json"
            counts = generate_and_publish(histories, api, None, target, failures=SourceFailures(),
                                          now=NOW, private_diagnostics=diagnostics)
            self.assertEqual(validate_output(target), counts)
            public = b"".join(bytes_snapshot(target).values())
            for private in (b"for_you", b"freshness", b"candidate_sources", b"suggested_score"):
                self.assertNotIn(private, public)
            self.assertIn(b"tt300", public)
            self.assertIn(b"for_you_rank", diagnostics.read_bytes())

    def test_private_diagnostics_cannot_be_written_elsewhere_under_pages_docs(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "docs/suggested"
            with self.assertRaises(RefreshError):
                generate_and_publish({key: [] for key in INPUT_FILES}, None, None, target,
                    failures=SourceFailures(), generator=lambda *a, **k: generated(),
                    private_diagnostics=Path(directory) / "docs/private.json")
            self.assertFalse((Path(directory) / "docs/private.json").exists())


class TraktSourceContracts(unittest.TestCase):
    def test_complete_public_episode_history_pagination(self):
        responses = [JsonResponse([{"id": page * 10 + i} for i in range(2)], {
            "x-pagination-page": str(page), "x-pagination-page-count": "3", "x-pagination-item-count": "6",
        }) for page in (1, 2, 3)]
        http = FixtureHTTP(responses)
        rows = TraktClient(http).paginated("/users/example/history/shows", limit=2)
        self.assertEqual(len(rows), 6)
        self.assertEqual([params["page"] for _, params in http.calls], [1, 2, 3])

    def test_failed_later_page_is_not_returned_as_a_partial_history(self):
        http = FixtureHTTP([
            JsonResponse([{"id": 1}], {"x-pagination-page-count": "2", "x-pagination-item-count": "2"}),
            SourceError("page 2 failed"),
        ])
        with self.assertRaises(SourceError):
            TraktClient(http).paginated("/users/example/history/shows", limit=1)
        with self.assertRaises(SourceError):
            http.failures.require_success()

    def test_repeated_pages_and_changing_counts_fail(self):
        cases = [
            [JsonResponse([{"id": 1}], {}), JsonResponse([{"id": 1}], {})],
            [JsonResponse([{"id": 1}], {"x-pagination-page-count": "2", "x-pagination-item-count": "2"}),
             JsonResponse([{"id": 2}], {"x-pagination-page-count": "2", "x-pagination-item-count": "3"})],
            [JsonResponse([{"id": 1}], {"x-pagination-page-count": "1", "x-pagination-item-count": "2"})],
        ]
        for responses in cases:
            with self.subTest(responses=responses), self.assertRaises(SourceError):
                TraktClient(FixtureHTTP(responses)).paginated("/users/example/history/shows", limit=1)

    def test_error_object_never_counts_as_an_empty_watched_snapshot(self):
        with self.assertRaises(SourceError):
            TraktClient(FixtureHTTP([JsonResponse({"error": "not found"}, {})])).snapshot("/users/example/watched/shows")

    def test_private_snapshot_requires_all_files_and_successful_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for filename in INPUT_FILES.values():
                atomic_json(folder / filename, {"status": 200, "data": []})
            self.assertEqual(set(load_history_snapshots(folder)), set(INPUT_FILES))
            atomic_json(folder / INPUT_FILES["rated_movies"], {"status": 503, "data": []})
            with self.assertRaises(SourceError):
                load_history_snapshots(folder)


class CacheAndTransportContracts(unittest.TestCase):
    def test_exact_legacy_cache_key_and_envelope_are_accepted(self):
        path, params = "/tv/100", {"language": "en-GB", "append_to_response": "external_ids,keywords"}
        expected = hashlib.sha256((path + json.dumps(params, sort_keys=True)).encode()).hexdigest() + ".json"
        self.assertEqual(TmdbClient.cache_key(path, params), expected)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            atomic_json(cache / expected, {"path": path, "params": params,
                "fetchedAt": datetime.now(timezone.utc).isoformat(), "data": {"id": 100}})
            http = FixtureHTTP([])
            self.assertEqual(TmdbClient(http, cache)(path, params), {"id": 100})
            self.assertEqual(http.calls, [])

    def test_stale_cache_is_refreshed_and_same_key_concurrency_shares_request(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            key = TmdbClient.cache_key("/tv/100", {})
            atomic_json(cache / key, {"path": "/tv/100", "params": {},
                "fetchedAt": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "data": {"id": 99}})
            http = FixtureHTTP([JsonResponse({"id": 100}, {})])
            client = TmdbClient(http, cache)
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda _: client("/tv/100"), range(8)))
            self.assertEqual(values, [{"id": 100}] * 8)
            self.assertEqual(len(http.calls), 1)
            self.assertEqual(json.loads((cache / key).read_text())["data"], {"id": 100})

    def test_api_key_cannot_enter_disk_cache_parameters(self):
        http = FixtureHTTP([])
        with self.assertRaises(SourceError):
            TmdbClient(http)("/movie/1", {"api_key": "private"})

    def test_http_errors_do_not_expose_credential_url(self):
        failures = SourceFailures()
        def opener(request, **kwargs):
            raise urllib.error.HTTPError(request.full_url, 401, "PRIVATE-SECRET", {}, None)
        client = JsonHttpClient("https://api.themoviedb.org/3", {}, "tmdb", failures,
                                extra_query={"api_key": "PRIVATE-SECRET"}, opener=opener)
        with self.assertRaises(SourceError) as caught:
            client.get("/movie/1")
        self.assertNotIn("PRIVATE-SECRET", str(caught.exception))
        self.assertNotIn("api_key", str(caught.exception))

    def test_configuration_failure_does_not_change_public_output_or_log_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "suggested"
            publish_result(generated(), target)
            before = bytes_snapshot(target)
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch("sys.stderr", output):
                self.assertEqual(main(["--output", str(target)]), 1)
            self.assertEqual(bytes_snapshot(target), before)
            self.assertIn("RefreshError", output.getvalue())


if __name__ == "__main__":
    unittest.main()
