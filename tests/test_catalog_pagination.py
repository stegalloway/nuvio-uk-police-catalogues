"""Exercise the requests Nuvio makes as catalogue sizes change."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from update_catalogs import write_catalog_pages


class CatalogPaginationTests(unittest.TestCase):
    def test_partial_pages_and_cached_offsets_terminate_after_refreshes(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            old_offsets = set()
            for count in (28, 55, 26, 40, 61, 29, 0, 20):
                with self.subTest(count=count):
                    metas = [{"id": f"tt{i}"} for i in range(count)]
                    write_catalog_pages(folder, "test", metas)
                    first = json.loads((folder / "test.json").read_text())["metas"]
                    self.assertEqual(first, metas[:20])

                    # Resume both a fresh client and any offset published before
                    # this refresh, including old end-of-list and removed pages.
                    for start in old_offsets | {0, count}:
                        skip = start
                        received = []
                        for _ in range(count + 2):
                            path = folder / "test" / f"skip={skip}.json"
                            self.assertTrue(path.is_file(), (count, start, skip))
                            page = json.loads(path.read_text())["metas"]
                            self.assertLessEqual(len(page), 20)
                            if not page:
                                break
                            received.extend(page)
                            skip += len(page)
                        else:
                            self.fail(f"Pagination did not terminate: {count}, {start}")
                        self.assertEqual(received, metas[start:])

                    old_offsets = {
                        int(path.stem.removeprefix("skip="))
                        for path in (folder / "test").glob("skip=*.json")
                    }


if __name__ == "__main__":
    unittest.main()
