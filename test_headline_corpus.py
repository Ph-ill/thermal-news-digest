"""Filter and de-dupe cases loaded from tests/fixtures/headlines.yml."""

from pathlib import Path
import unittest

import digest_picker as dp


def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    if text == "true":
        return True
    if text == "false":
        return False
    return text


def load_corpus(path: Path) -> tuple[list[dict], list[dict]]:
    """Line-oriented reader for the headline table. The suite does not use PyYAML."""
    headlines: list[dict] = []
    pairs: list[dict] = []
    mode = ""
    current: dict | None = None
    bucket = headlines
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.lstrip().startswith("#") or not raw.strip():
            continue
        if raw.strip() == "headlines:":
            if current is not None:
                bucket.append(current)
                current = None
            mode = "headlines"
            bucket = headlines
            continue
        if raw.strip() == "pairs:":
            if current is not None:
                bucket.append(current)
                current = None
            mode = "pairs"
            bucket = pairs
            continue
        if raw.startswith("  - "):
            if current is not None:
                bucket.append(current)
            key, _, value = raw.strip()[2:].partition(":")
            current = {key.strip(): _scalar(value)}
            continue
        if raw.startswith("    ") and current is not None and ":" in raw:
            key, _, value = raw.strip().partition(":")
            current[key.strip()] = _scalar(value)
            continue
        raise ValueError(f"unreadable corpus line: {raw}")
    if current is not None:
        bucket.append(current)
    if mode not in {"headlines", "pairs"}:
        raise ValueError("corpus has no headlines")
    return headlines, pairs


class HeadlineCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent / "tests" / "fixtures" / "headlines.yml"
        cls.headlines, cls.pairs = load_corpus(path)

    def test_table_is_large_enough_to_hold_the_review_cases(self):
        self.assertGreaterEqual(len(self.headlines), 50)
        self.assertGreaterEqual(len(self.pairs), 12)
        expects = {row["expect"] for row in self.headlines}
        self.assertEqual(expects, {"exclude", "keep", "fallback-prints"})
        self.assertEqual({row["expect"] for row in self.pairs}, {"same", "different"})

    def test_each_headline_matches_its_label(self):
        for index, row in enumerate(self.headlines):
            title = row["title"]
            summary = row.get("summary") or ""
            item = {
                "title": title,
                "summary": summary,
                "url": f"https://example.test/corpus/{index}",
                "topic": row.get("topic") or "",
            }
            section = row["section"]
            expect = row["expect"]
            with self.subTest(title=title):
                if section == "local":
                    kept = dp.keep_local(item, require_place=bool(row.get("require_place")))
                    chosen = dp.fallback_pick(
                        [item],
                        1,
                        dp.Seen([], "2026-11-26"),
                        section_id="local",
                    )
                    printed = chosen[0]["title"] == title
                    if expect == "exclude":
                        self.assertFalse(kept)
                        if not row.get("require_place"):
                            self.assertFalse(printed)
                    elif expect == "keep":
                        self.assertTrue(kept)
                        self.assertFalse(printed)
                    else:
                        self.assertEqual(expect, "fallback-prints")
                        self.assertTrue(kept)
                        self.assertTrue(printed)
                elif section == "companies":
                    kept = dp.keep_company(item)
                    self.assertEqual(kept, expect == "keep")
                elif section == "global":
                    blob = f"{title} {summary}"
                    dropped = dp.is_obituary(blob) or dp.is_spam(blob)
                    self.assertEqual(dropped, expect == "exclude")
                else:
                    self.fail(section)

    def test_each_pair_matches_its_label(self):
        for row in self.pairs:
            with self.subTest(left=row["left"], right=row["right"]):
                same = dp.titles_similar(row["left"], row["right"])
                self.assertEqual(same, row["expect"] == "same")


if __name__ == "__main__":
    unittest.main()
