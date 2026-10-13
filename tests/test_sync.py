import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from sync import InvalidData, decode, sync_dates, validate


def payload(day='2026-10-12', grade=2, result=True):
    race = {'date': day, 'stadium_number': 14, 'race_number': 11,
            'grade_number': grade, 'racers': {'1': {'entry_number': 1, 'number': 4320}},
            'preview': {}, 'result': {}}
    if result:
        race['result'] = {'date': day, 'stadium_number': 14, 'race_number': 11,
                          'remarks': 'fixture'}
    return {'programs': {'stadiums': {'14': {'races': {'11': race}}}}}


def response(value):
    return json.dumps(value, ensure_ascii=False).encode(), {'url': 'fixture', 'http_last_modified': None, 'etag': None}


class SyncTests(unittest.TestCase):
    def test_grade_mapping_and_unknown(self):
        for grade, label in [(1, 'SG'), (2, 'G1'), (3, 'G2'), (4, 'G3'), (5, '一般'), (6, 'PG1'), (None, '不明'), (99, '不明')]:
            _, summary = validate(payload(grade=grade), date(2026, 10, 12))
            self.assertEqual(summary['grade_race_counts'], {label: 1})

    def test_pending_result_is_allowed(self):
        _, summary = validate(payload(result=False), date(2026, 10, 12))
        self.assertEqual(summary['with_result_count'], 0)

    def test_date_identity_and_empty_rejected(self):
        for value in [payload('2026-10-11'), {'programs': []}, {'programs': {'stadiums': {}}}]:
            with self.assertRaises(InvalidData):
                validate(value, date(2026, 10, 12))
        value = payload()
        value['programs']['stadiums']['14']['races']['11']['result']['date'] = '2026-10-11'
        with self.assertRaises(InvalidData):
            validate(value, date(2026, 10, 12))

    def test_invalid_json_and_duplicates_rejected(self):
        for raw in [b'<html>error</html>', b'{"x": NaN}', b'{"x":1,"x":2}']:
            with self.assertRaises(ValueError):
                decode(raw)

    def test_save_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 10, 12)
            raw, headers = response(payload())
            root = Path(tmp)
            self.assertEqual(sync_dates([day], root, lambda _: (raw, headers), day), 0)
            self.assertEqual((root / '2026/20261012.json').read_bytes(), raw)
            self.assertEqual((root / 'today.json').read_bytes(), raw)

    def test_error_and_regression_preserve_good_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 10, 12)
            root = Path(tmp)
            sync_dates([day], root, lambda _: response(payload()), day)
            original = (root / 'today.json').read_bytes()
            for value in [payload(result=False), payload('2026-10-11')]:
                self.assertEqual(sync_dates([day], root, lambda _: response(value), day), 1)
                self.assertEqual((root / 'today.json').read_bytes(), original)
                self.assertEqual((root / '2026/20261012.json').read_bytes(), original)
            status = json.loads((root / 'status.json').read_text())
            self.assertEqual(status['dates'][day.isoformat()]['status'], 'error')
            self.assertIn('last_success_at', status['dates'][day.isoformat()])

    def test_disappearing_race_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 10, 12)
            value = payload()
            other = copy.deepcopy(value['programs']['stadiums']['14']['races']['11'])
            other['race_number'] = 12
            other['result']['race_number'] = 12
            value['programs']['stadiums']['14']['races']['12'] = other
            root = Path(tmp)
            sync_dates([day], root, lambda _: response(value), day)
            self.assertEqual(sync_dates([day], root, lambda _: response(payload()), day), 1)
            self.assertEqual(len(json.loads((root / 'today.json').read_text())['programs']['stadiums']['14']['races']), 2)

    def test_rollover_with_fetch_failure_removes_stale_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_day, new_day = date(2026, 10, 12), date(2026, 10, 13)
            sync_dates([old_day], root, lambda _: response(payload()), old_day)
            def failing(_):
                raise TimeoutError('test timeout')
            self.assertEqual(sync_dates([new_day], root, failing, new_day), 1)
            self.assertFalse((root / 'today.json').exists())
            self.assertTrue((root / '2026/20261012.json').exists())
            self.assertFalse(json.loads((root / 'status.json').read_text())['today_available'])


if __name__ == '__main__':
    unittest.main()
