import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from official_results import cancellations, parse_result
from recovery import reconcile
from sync import sync_dates

DAY = date(2026, 11, 19)
NOW = datetime(2026, 11, 19, 13, 10, tzinfo=timezone(timedelta(hours=9)))
WIN = {'combination': '1-4-2', 'amount': 1210, 'label': None}


def program(race, damaged=False, paid=False):
    return dict(date=DAY.isoformat(), stadium_number=4, race_number=race,
                closed_at=None if damaged else f'{DAY} 12:12:00',
                racers={'1': {'entry_number': 1, 'name': None if damaged else '選手',
                              'number': None if damaged else 5076}},
                result={'date': DAY.isoformat(), 'stadium_number': 4, 'race_number': race,
                        'payouts': {'trifecta': [WIN] if paid else []}, 'refunds': []})


def doc(rows):
    return json.dumps({'programs': {'stadiums': {'4': {'races': {str(r['race_number']): r for r in rows}}}}}).encode()


def markup(combo='1-4-2', amount='¥1,210', refund='', extra=''):
    return f'''<a href="/owpc/pc/race/racelist?rno=4&amp;jcd=04&amp;hd=20261119">出走表</a>
    <table><tr><th>勝式</th><th>組番</th><th>払戻金</th><th>人気</th></tr>
    <tr><td>3連単</td><td>{combo}</td><td>{amount}</td><td>2</td></tr>{extra}
    <tr><td>3連複</td></tr></table><table><tr><th>返還</th></tr>
    <tr><td><div class="numberSet1_row">{refund}</div></td></tr></table>'''


class RecoveryTests(unittest.TestCase):
    def test_targeted_merge_retry_correction_and_upstream_recovery(self):
        before = doc([program(4, paid=True), program(5), program(6)])
        incoming = doc([program(4, damaged=True), program(5, damaged=True), program(6, paid=True)])
        corrected = dict(WIN, amount=500)
        def checked(venue, race):
            self.assertEqual(venue, 4)
            return {'payouts': {'trifecta': [corrected]}, 'refunds': [3], 'remarks': '【返還艇あり】'}
        with TemporaryDirectory() as temp:
            root = Path(temp)
            daily = root / '2026/20261119.json'
            def sync(body, lookup, now=NOW):
                self.assertEqual(sync_dates([DAY], root, lambda _: (body, {}), DAY,
                                            official_factory=lambda _: lookup, clock=lambda: now), 0)
                self.assertEqual((root/'today.json').read_bytes(), daily.read_bytes())
                return json.loads(daily.read_bytes())['programs']['stadiums']['4']['races']
            sync(before, lambda *_: self.fail())
            def fail(*_):
                raise TimeoutError()
            rows = sync(incoming, fail)
            self.assertEqual(rows['4']['result']['payouts']['trifecta'], [WIN])
            self.assertEqual(rows['5']['result']['payouts']['trifecta'], [])
            self.assertEqual(rows['6']['result']['payouts']['trifecta'], [WIN])
            self.assertTrue(all(row['closed_at'] for row in rows.values()))
            rows = sync(incoming, checked, NOW+timedelta(minutes=5))
            self.assertEqual(rows['4']['result']['refunds'], [3])
            self.assertEqual(rows['5']['result']['payouts']['trifecta'], [corrected])
            self.assertEqual(sync(incoming, lambda *_: self.fail(), NOW+timedelta(minutes=10)), rows)
            sync(doc([program(i, paid=True) for i in (4, 5, 6)]), lambda *_: self.fail())
            self.assertEqual(json.loads((root/'status.json').read_bytes())['dates'][str(DAY)]['race_recovery'], {})

    def test_existing_damaged_file_seeds_from_history(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            daily = root/'2026/20261119.json'
            daily.parent.mkdir()
            damaged = doc([program(4, damaged=True)])
            daily.write_bytes(damaged)
            self.assertEqual(sync_dates([DAY], root, lambda _: (damaged, {}), DAY,
                                        baseline_fetch=lambda _: doc([program(4)]),
                                        official_factory=lambda _: lambda *_: None, clock=lambda: NOW), 0)
            row = json.loads(daily.read_bytes())['programs']['stadiums']['4']['races']['4']
            self.assertEqual(row['closed_at'], str(DAY)+' 12:12:00')
            self.assertFalse(row['result']['payouts']['trifecta'])

    def test_future_and_bounded_retry_order(self):
        r = program(4)
        r['closed_at'] = str(DAY)+' 16:00:00'
        incoming = {'04/04': program(4, damaged=True)}
        _, issues = reconcile(incoming, {'04/04': r}, {}, NOW, lambda *_: self.fail())
        self.assertEqual(issues['04/04']['state'], 'scheduled')
        rows = {str(i): program(i, damaged=True) for i in range(20)}
        old = {k: program(v['race_number']) for k, v in rows.items()}
        calls = []
        lookup = lambda _, n: calls.append(n)
        _, previous = reconcile(copy.deepcopy(rows), old, {}, NOW, lookup)
        self.assertEqual(len(calls), 12)
        reconcile(copy.deepcopy(rows), old, previous, NOW+timedelta(minutes=5), lookup)
        self.assertEqual(len(set(calls)), 20)

    def test_official_outcomes(self):
        read = lambda body: parse_result(body, DAY, 4, 4)
        self.assertEqual(read(markup())['payouts']['trifecta'], [WIN])
        self.assertEqual(read(markup(refund='3'))['refunds'], [3])
        self.assertEqual(read(markup('不成立', '¥100', '4'))['payouts']['trifecta'][0]['label'], '不成立')
        extra = '<tr><td>1-2-4</td><td>¥590</td><td>8</td></tr>'
        self.assertEqual(len(read(markup(extra=extra))['payouts']['trifecta']), 2)
        for body in [markup(amount=''), markup().replace('20261119', '20261118'),
                     markup().replace('numberSet1_row','unexpected')]:
            with self.assertRaises(ValueError):
                read(body)

    def test_explicit_cancellation(self):
        body = '''<table><tr><th></th><th colspan="3"><img alt="津"></th>
        <th colspan="3"><img alt="戸田"></th></tr><tr><th>5R</th><td colspan="3">レース中止</td>
        <td colspan="3"><a href="/owpc/pc/race/racelist?hd=20261119&amp;jcd=02&amp;rno=5">12:33</a></td></tr></table>'''
        self.assertEqual(cancellations(body, DAY), {(9, 5)})
        self.assertEqual(cancellations(body, date(2026, 11, 18)), set())
        old = {'09/05': program(5, paid=True)}
        raw = {'09/05': program(5, damaged=True)}
        cancelled = {'payouts': {'trifecta': []}, 'remarks': 'レース中止', 'refunds': None}
        reconcile(raw, old, {}, NOW, lambda *_: cancelled)
        self.assertEqual(raw['09/05']['result']['payouts']['trifecta'], [])
        self.assertEqual(raw['09/05']['result']['remarks'], 'レース中止')

    def test_deadline_requires_matching_official_column(self):
        url = '/owpc/pc/race/raceresult?hd=20261119&amp;jcd=04&amp;rno='
        grid = f'''<table><tr><th>レース</th><th><a href="{url}3">3R</a></th>
        <th><a href="{url}4">4R</a></th></tr><tr><td>締切予定時刻</td>
        <td>11:59</td><td>12:13</td></tr></table>'''
        def parse(grid):
            return parse_result(markup() + grid, DAY, 4, 4)
        self.assertEqual(parse(grid)['closed_at'], f'{DAY} 12:13:00')
        for bad in (grid.replace('20261119', '20261118'), grid.replace('jcd=04', 'jcd=17'),
                    grid.replace('rno=4', 'rno=5'), grid.replace('12:13', '12:99'),
                    grid.replace('<td>11:59</td>', ''), grid + grid.replace('12:13', '12:14')):
            outcome = parse(bad)
            self.assertNotIn('closed_at', outcome)
            self.assertEqual(outcome['payouts']['trifecta'], [WIN])

    def test_already_verified_program_uses_official_deadline(self):
        key = '04/04'
        old = {key: program(4, paid=True)}
        state = {key: {'verified_at': int(NOW.timestamp()) - 300}}
        rows = {key: program(4, damaged=True)}
        official = dict(parse_result(markup(), DAY, 4, 4), closed_at=f'{DAY} 12:13:00')
        _, state = reconcile(rows, old, state, NOW, lambda *_: official)
        self.assertEqual(rows[key]['closed_at'], official['closed_at'])
        self.assertNotIn('closed_at', rows[key]['result'])
        again = {key: program(4, damaged=True)}
        reconcile(again, rows, state, NOW + timedelta(minutes=5), lambda *_: self.fail())
        self.assertEqual(again, rows)
