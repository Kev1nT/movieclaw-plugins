"""只测纯函数：不启动宿主，不发网络请求。

榜单条目的排序合并、排除词匹配、季号推断、已有订阅的比对键都在模块顶层的纯函数里，
改榜单口径时最容易坏的就是这几处，所以钉在这里。
"""

import unittest

from douban_hot_subscribe import (
    keyword_hit,
    interleave,
    normalize_collections,
    pick_candidates,
    season_plan,
    title_key,
)


class NormalizeCollectionsTest(unittest.TestCase):
    def test_alias_refs_pass_through(self):
        refs, bad = normalize_collections(["douban:movie:123", "tmdb:tv:9"])
        self.assertEqual(refs, ["douban:movie:123", "tmdb:tv:9"])
        self.assertEqual(bad, [])

    def test_split_on_comma_and_semicolon(self):
        refs, _ = normalize_collections("douban:movie:1，tmdb:movie:2；douban:tv:3")
        self.assertEqual(refs, ["douban:movie:1", "tmdb:movie:2", "douban:tv:3"])

    def test_dedupe_keeps_order(self):
        refs, _ = normalize_collections(["tmdb:movie:2", "douban:movie:1", "tmdb:movie:2"])
        self.assertEqual(refs, ["tmdb:movie:2", "douban:movie:1"])

    def test_unknown_entry_reported(self):
        refs, bad = normalize_collections(["乱七八糟"])
        self.assertEqual(refs, [])
        self.assertEqual(bad, ["乱七八糟"])

    def test_empty(self):
        self.assertEqual(normalize_collections(None), ([], []))
        self.assertEqual(normalize_collections([]), ([], []))


class KeywordHitTest(unittest.TestCase):
    def test_chinese_substring(self):
        self.assertEqual(keyword_hit("某综艺节目", ["综艺"]), "综艺")

    def test_english_word_boundary(self):
        self.assertEqual(keyword_hit("Some TS Show", ["TS"]), "TS")
        self.assertIsNone(keyword_hit("Hits 2024", ["ts"]))
        self.assertIsNone(keyword_hit("TCA 2024", ["TC"]))

    def test_no_keywords(self):
        self.assertIsNone(keyword_hit("随便什么", []))


class InterleaveTest(unittest.TestCase):
    def test_round_robin_between_collections(self):
        rows = [
            {"collection": "a", "n": 1},
            {"collection": "a", "n": 2},
            {"collection": "b", "n": 3},
        ]
        self.assertEqual([row["n"] for row in interleave(rows)], [1, 3, 2])

    def test_single_collection_keeps_order(self):
        rows = [{"collection": "a", "n": 1}, {"collection": "a", "n": 2}]
        self.assertEqual([row["n"] for row in interleave(rows)], [1, 2])


class SeasonPlanTest(unittest.TestCase):
    def test_movie(self):
        self.assertEqual(season_plan("某种物质", "movie", True), ([], False, "电影"))

    def test_tv_with_season_in_title(self):
        self.assertEqual(season_plan("相对宇宙 第二季", "tv", True), ([2], True, "按第 2 季订阅"))

    def test_tv_without_season_defaults_to_first(self):
        self.assertEqual(season_plan("某部剧", "tv", True), ([1], True, "标题没写第几季，按第 1 季订阅"))

    def test_tv_without_backfill_only_follows_new(self):
        self.assertEqual(season_plan("相对宇宙 第二季", "tv", False), ([], True, "只追订阅之后播出的新集"))


class TitleKeyTest(unittest.TestCase):
    def test_strips_whitespace_and_lowercases(self):
        self.assertEqual(title_key("The  Expanse", 2020), "theexpanse|2020")

    def test_year_must_be_int(self):
        self.assertEqual(title_key("某剧", "2020"), "某剧|")
        self.assertEqual(title_key("某剧", None), "某剧|")


class PickCandidatesTest(unittest.TestCase):
    def test_keeps_only_entries_with_title_ref(self):
        details = [
            {"title_ref": "tmdb:movie:1", "title": "甲", "year": 2020},
            {"title": "没有 ref"},
            "不是字典",
        ]
        self.assertEqual(
            pick_candidates(details), [{"title_ref": "tmdb:movie:1", "title": "甲", "year": 2020}]
        )

    def test_none(self):
        self.assertEqual(pick_candidates(None), [])


if __name__ == "__main__":
    unittest.main()
