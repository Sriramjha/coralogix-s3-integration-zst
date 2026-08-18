#!/usr/bin/env python3
"""Parse and SNS-unwrap tests. Run from repo root:

  PYTHONPATH=src python3 tests/test_handler.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import zstandard  # noqa: E402

import handler  # noqa: E402


class ExtractS3ObjectsTests(unittest.TestCase):
    def test_sns_wrapped_s3_event(self) -> None:
        event_path = ROOT / "events" / "sns-s3-sample.json"
        event = json.loads(event_path.read_text(encoding="utf-8"))
        objects = handler.extract_s3_objects(event)
        self.assertEqual(
            objects,
            [("adaptive-shield-raw-events", "raw/2026/08/18/events.json.zst")],
        )

    def test_direct_s3_event(self) -> None:
        event = {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "bucket-a"},
                        "object": {"key": "path/with+spaces/file.zst"},
                    }
                }
            ]
        }
        self.assertEqual(
            handler.extract_s3_objects(event),
            [("bucket-a", "path/with spaces/file.zst")],
        )

    def test_s3_test_event_ignored(self) -> None:
        event = {
            "Records": [
                {
                    "Sns": {
                        "Message": json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"})
                    }
                }
            ]
        }
        self.assertEqual(handler.extract_s3_objects(event), [])


class ParseTests(unittest.TestCase):
    def test_ndjson(self) -> None:
        text = '{"id":1}\n{"id":2}\n'
        records = list(handler.iter_records_from_text(text))
        self.assertEqual(records, [{"id": 1}, {"id": 2}])

    def test_json_array(self) -> None:
        records = list(handler.iter_records_from_text('[{"id":1},{"id":2}]'))
        self.assertEqual(records, [{"id": 1}, {"id": 2}])

    def test_json_array_from_stream(self) -> None:
        raw = io.BytesIO(b'[{"id":1},{"id":2}]')
        self.assertEqual(
            list(handler.iter_records_from_stream(raw)),
            [{"id": 1}, {"id": 2}],
        )

    def test_envelope(self) -> None:
        records = list(handler.iter_records_from_text('{"data":[{"id":1}]}'))
        self.assertEqual(records, [{"id": 1}])

    def test_zst_roundtrip(self) -> None:
        raw = '{"timestamp":"2026-08-18T06:00:00Z","message":"ok"}\n'.encode()
        compressed = zstandard.ZstdCompressor().compress(raw)
        stream = handler.decompress_stream(io.BytesIO(compressed), "events.json.zst")
        records = list(handler.iter_records_from_stream(stream))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["message"], "ok")
        expected = int(
            datetime(2026, 8, 18, 6, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.assertEqual(handler.extract_timestamp_ms(records[0]), expected)


class KeyPatternTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in (
            "S3_KEY_INCLUDE_PATTERNS",
            "S3_KEY_EXCLUDE_PATTERNS",
            "S3_KEY_SUFFIXES",
        ):
            os.environ.pop(name, None)

    def test_default_allows_zst_only(self) -> None:
        self.assertTrue(handler.key_allowed("a/b.json.zst"))
        self.assertTrue(handler.key_allowed("events.zstd"))
        self.assertFalse(handler.key_allowed("a/b.json.gz"))
        self.assertFalse(handler.key_allowed("readme.txt"))

    def test_include_prefix_pattern(self) -> None:
        os.environ["S3_KEY_INCLUDE_PATTERNS"] = "raw/*/*.zst"
        self.assertTrue(handler.key_allowed("raw/2026/events.zst"))
        self.assertFalse(handler.key_allowed("other/2026/events.zst"))

    def test_exclude_wins_over_include(self) -> None:
        os.environ["S3_KEY_INCLUDE_PATTERNS"] = "*.zst"
        os.environ["S3_KEY_EXCLUDE_PATTERNS"] = "*/tmp/*,*_debug.zst"
        self.assertTrue(handler.key_allowed("raw/events.zst"))
        self.assertFalse(handler.key_allowed("raw/tmp/events.zst"))
        self.assertFalse(handler.key_allowed("raw/events_debug.zst"))

    def test_star_include_allows_all_except_exclude(self) -> None:
        os.environ["S3_KEY_INCLUDE_PATTERNS"] = "*"
        os.environ["S3_KEY_EXCLUDE_PATTERNS"] = "*.metadata.zst"
        self.assertTrue(handler.key_allowed("notes.txt"))
        self.assertFalse(handler.key_allowed("file.metadata.zst"))


if __name__ == "__main__":
    unittest.main()
