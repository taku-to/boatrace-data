"""Preserve the last usable publication and reconcile only damaged race records."""
from datetime import datetime
from http.client import HTTPException
import json
import re
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from official_results import CANCELLED

READ_ERRORS = (ValueError, OSError, HTTPException, URLError)
MAX_CHECKS = 12


def hollow(race):
    return not race.get('closed_at') and not any(
        racer.get('name') or racer.get('number') for racer in race['racers'].values()
    )


def useful_preview(preview):
    if not isinstance(preview, dict):
        return False
    return any(preview.get(k) is not None for k in ('wind_speed', 'air_temperature', 'weather_number'))


def settled(result):
    if not isinstance(result, dict):
        return False
    if result.get('remarks') in CANCELLED:
        return True
    payouts = result.get('payouts')
    rows = payouts.get('trifecta') if isinstance(payouts, dict) else None
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict) or type(row.get('amount')) is not int or row['amount'] <= 0:
            return False
        combo = row.get('combination')
        if not (row.get('label') or (isinstance(combo, str) and re.fullmatch('[1-6]-[1-6]-[1-6]', combo)
                                     and len(set(combo.split('-'))) == 3)):
            return False
    return True


def first_snapshot(day):
    """Cold start after prior releases already wrote the damaged daily file."""
    path = f'docs/v1/{day:%Y}/{day:%Y%m%d}.json'
    api = 'https://api.github.com/repos/taku-to/boatrace-data/commits?'
    query = urlencode({'path': path, 'sha': 'main', 'per_page': 1})
    def read(page):
        with urlopen(Request(api + query + f'&page={page}', headers={'User-Agent': 'boatrace-data/1.0'}),
                     timeout=6) as response:
            return json.loads(response.read(250000)), response.headers.get('Link', '')
    commits, links = read(1)
    last = re.search(r'<[^>]*[?&]page=(\d+)(?:&[^>]*)?>; rel="last"', links)
    if last:
        commits, _ = read(int(last[1]))
    if not commits or not re.fullmatch('[0-9a-f]{40}', commits[0].get('sha', '')):
        raise ValueError('no previous publication')
    url = f'https://raw.githubusercontent.com/taku-to/boatrace-data/{commits[0]["sha"]}/{path}'
    with urlopen(url, timeout=8) as response:
        return response.read(20 * 1024 * 1024 + 1)


def reconcile(records, old, previous, now, official):
    """Mutate validated incoming records in place. All recovery state is JSON-serializable."""
    issues = {}
    due = []
    changed = False
    epoch = int(now.timestamp())
    for key, race in records.items():
        prior = old.get(key, {})
        last = previous.get(key, {})
        damaged = hollow(race)
        result = race.get('result') or {}
        prior_result = prior.get('result') or {}
        lost_result = settled(prior_result) and not settled(result)
        if not (damaged or lost_result or (last and not settled(result))):
            continue
        info = dict(last)
        issues[key] = info
        info['reason'] = 'empty_program' if damaged else 'missing_result'
        if damaged and prior and not hollow(prior):
            # Only the program is borrowed. Accept independently corrected upstream results.
            race.update({k: v for k, v in prior.items() if k not in ('preview', 'result')})
            # Empty shells contain identity and empty racer slots, so object presence is insufficient.
            if prior.get('preview') and not useful_preview(race.get('preview')):
                race['preview'] = prior['preview']
            changed = True
        if lost_result:
            race['result'] = prior_result
            changed = True
        if settled(result) and result != prior_result:
            info.pop('verified_at', None)
        closed = race.get('closed_at')
        if closed:
            try:
                closing = datetime.fromisoformat(closed).replace(tzinfo=now.tzinfo)
            except (TypeError, ValueError):
                closing = None
            if closing and closing > now:
                info['state'] = 'scheduled'
                continue
        # A confirmed result can still be corrected. Revisit it while upstream is degraded.
        if (damaged and not info.get('clock_checked')) or epoch - info.get('verified_at', 0) >= 1800:
            due.append((info.get('attempted_at', 0), key))
        info['state'] = 'verified' if info.get('verified_at') else 'pending'
    for _, key in sorted(due)[:MAX_CHECKS]:
        race = records[key]
        info = issues[key]
        info['attempted_at'] = epoch
        try:
            outcome = official(race['stadium_number'], race['race_number'])
        except READ_ERRORS:
            outcome = None
        if outcome is None:
            info['state'] = 'awaiting_official'
            continue
        outcome = dict(outcome)
        closed = outcome.pop('closed_at', None)
        if info['reason'] == 'empty_program':
            info['clock_checked'] = True
            if closed:
                race['closed_at'] = closed
        current = race.get('result') or {}
        # If the outcome changed, avoid attaching other payouts/placements from an obsolete result.
        if ((current.get('payouts') or {}).get('trifecta') != outcome['payouts']['trifecta']
                or current.get('refunds') != outcome['refunds']
                or outcome['remarks'] in CANCELLED):
            current = {}
        race['result'] = {
            **current, 'date': race['date'], 'stadium_number': race['stadium_number'],
            'race_number': race['race_number'], **outcome,
            'payouts': {**(current.get('payouts') or {}), **outcome['payouts']},
        }
        info.update(state='verified', verified_at=epoch)
        changed = True
    return changed, issues
