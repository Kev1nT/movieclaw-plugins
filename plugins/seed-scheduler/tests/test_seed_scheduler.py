"""只测纯函数：不启动宿主，不发网络请求。

做种时长的口径、状态归类、以及接口返回的脏数据兜底都在模块顶层的纯函数里，
这几处一错就会误删种子或者该删的永远不删，所以钉在这里。
"""

import unittest
from datetime import datetime, timezone

from seed_scheduler import (
    has_keep_tag,
    hash_key,
    items_of,
    num,
    parse_time,
    recently_opened,
    seeded_hours,
    state_group,
    uniq,
    uniq_units,
)

NOW = 1_800_000_000.0


class ItemsOfTest(unittest.TestCase):
    def test_wrapped_and_plain(self):
        self.assertEqual(items_of({"items": [1, 2]}), [1, 2])
        self.assertEqual(items_of([3]), [3])

    def test_other_payloads(self):
        self.assertEqual(items_of(None), [])
        self.assertEqual(items_of("x"), [])
        self.assertEqual(items_of({"items": None}), [])


class NumTest(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(num("3.5"), 3.5)
        self.assertEqual(num(7), 7.0)

    def test_none_bool_and_garbage_fall_back(self):
        self.assertEqual(num(None), 0.0)
        self.assertEqual(num(True), 0.0)
        self.assertEqual(num("x"), 0.0)
        self.assertEqual(num("x", 9.0), 9.0)


class ParseTimeTest(unittest.TestCase):
    def test_zulu(self):
        moment = parse_time("2026-10-20T10:00:00Z")
        self.assertEqual(moment, datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))

    def test_naive_treated_as_utc(self):
        moment = parse_time("2026-10-20 10:00:00")
        self.assertEqual(moment.tzinfo, timezone.utc)
        self.assertEqual(moment.hour, 10)

    def test_datetime_passthrough(self):
        naive = datetime(2026, 10, 20, 10, 0)
        self.assertEqual(parse_time(naive).tzinfo, timezone.utc)

    def test_unparsable(self):
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time("not-a-time"))


class HashKeyTest(unittest.TestCase):
    def test_lowercased_and_trimmed(self):
        self.assertEqual(hash_key(" ABCdef "), "abcdef")

    def test_missing(self):
        self.assertEqual(hash_key(None), "")


class KeepTagTest(unittest.TestCase):
    def test_known_tags(self):
        self.assertTrue(has_keep_tag(["免清理"]))
        self.assertTrue(has_keep_tag(["noreap"]))
        self.assertTrue(has_keep_tag(["别的", "SEED-KEEP"]))

    def test_absent(self):
        self.assertFalse(has_keep_tag(["普通"]))
        self.assertFalse(has_keep_tag([]))
        self.assertFalse(has_keep_tag(None))


class StateGroupTest(unittest.TestCase):
    def test_known_states(self):
        self.assertEqual(state_group({"state": "uploading"}), "seeding")
        self.assertEqual(state_group({"state": "pausedUP"}), "seeding")
        self.assertEqual(state_group({"state": "downloading"}), "downloading")
        self.assertEqual(state_group({"state": "missingFiles"}), "missing")
        self.assertEqual(state_group({"state": "error"}), "errored")
        self.assertEqual(state_group({"state": "checkingUP"}), "checking")

    def test_unknown_state_falls_back_to_progress(self):
        self.assertEqual(state_group({"state": "", "completed": True}), "seeding")
        self.assertEqual(state_group({"state": "", "completed": False}), "downloading")

    def test_unknown_state_without_progress_is_other(self):
        self.assertEqual(state_group({"state": "weird"}), "other")


class SeededHoursTest(unittest.TestCase):
    def test_uses_qbittorrent_seeding_seconds(self):
        self.assertEqual(seeded_hours({"seeding_seconds": 7200}, 0, NOW), 2.0)

    def test_uses_longest_signal(self):
        brief = {"seeding_seconds": 7200, "completed_on": NOW - 36000}
        self.assertEqual(seeded_hours(brief, 0, NOW), 10.0)

    def test_added_on_counts_when_no_completion(self):
        self.assertEqual(seeded_hours({"added_on": NOW - 1800}, 0, NOW), 0.5)

    def test_falls_back_to_first_seen(self):
        self.assertEqual(seeded_hours({}, NOW - 7200, NOW), 2.0)

    def test_no_information_at_all(self):
        self.assertEqual(seeded_hours({}, 0, NOW), 0.0)


class UniqTest(unittest.TestCase):
    def test_uniq(self):
        self.assertEqual(uniq([1, 1, 2, 1]), [1, 2])

    def test_uniq_units_skips_malformed(self):
        self.assertEqual(uniq_units([["1", "2"], [1, 2], [None, "x"]]), [[1, 2]])


class RecentlyOpenedTest(unittest.TestCase):
    def test_recent_visit(self):
        self.assertTrue(recently_opened({"5": NOW - 60}, 5, NOW))

    def test_old_visit(self):
        self.assertFalse(recently_opened({"5": NOW - 7200}, 5, NOW))

    def test_missing_media(self):
        self.assertFalse(recently_opened({}, None, NOW))


if __name__ == "__main__":
    unittest.main()
