"""Mirror validated boatraceopenapi/api JSON using Python's standard library."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
BASE = 'https://boatraceopenapi.github.io/api/v1'
GRADES = {1: 'SG', 2: 'G1', 3: 'G2', 4: 'G3', 5: '一般', 6: 'PG1'}
MAX_BYTES = 20 * 1024 * 1024


class InvalidData(ValueError):
    pass


def timestamp():
    return datetime.now(JST).isoformat(timespec='seconds')


def decode(raw):
    def reject_constant(value):
        raise InvalidData(f'Invalid JSON number: {value}')
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise InvalidData(f'Duplicate JSON key: {key}')
            obj[key] = value
        return obj
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique)


def validate(payload, target):
    if not isinstance(payload, dict):
        raise InvalidData('Root must be an object')
    programs = payload.get('programs')
    if not isinstance(programs, dict):
        raise InvalidData('programs must be an object')
    stadiums = programs.get('stadiums')
    if not isinstance(stadiums, dict) or not stadiums:
        raise InvalidData('No stadiums; empty data cannot establish a non-racing day')
    records, grades = {}, Counter()
    for stadium_key, stadium in stadiums.items():
        if not stadium_key.isdigit() or not 1 <= int(stadium_key) <= 24:
            raise InvalidData('Invalid stadium key')
        if not isinstance(stadium, dict):
            raise InvalidData('Invalid stadium object')
        races = stadium.get('races')
        if not isinstance(races, dict) or not races:
            raise InvalidData('Missing races')
        for race_key, race in races.items():
            if not race_key.isdigit() or not 1 <= int(race_key) <= 12:
                raise InvalidData('Invalid race key')
            if not isinstance(race, dict) or race.get('date') != target.isoformat():
                raise InvalidData('Race date does not match requested JST date')
            if type(race.get('stadium_number')) is not int or race['stadium_number'] != int(stadium_key):
                raise InvalidData('Stadium number mismatch')
            if type(race.get('race_number')) is not int or race['race_number'] != int(race_key):
                raise InvalidData('Race number mismatch')
            racers = race.get('racers')
            if not isinstance(racers, dict) or not 1 <= len(racers) <= 6:
                raise InvalidData('Missing/invalid racers')
            for entry, racer in racers.items():
                if not entry.isdigit() or not 1 <= int(entry) <= 6 or not isinstance(racer, dict):
                    raise InvalidData('Invalid racer entry')
                if type(racer.get('entry_number')) is not int or racer['entry_number'] != int(entry):
                    raise InvalidData('Racer entry number mismatch')
                number = racer.get('number')
                if number is not None and (type(number) is not int or number <= 0):
                    raise InvalidData('Invalid racer registration number')
            for kind in ('preview', 'result'):
                section = race.get(kind)
                if section is not None and not isinstance(section, dict):
                    raise InvalidData(f'{kind} must be object or null')
                if section:
                    if section.get('date') != target.isoformat():
                        raise InvalidData(f'{kind} date mismatch')
                    if section.get('stadium_number') != int(stadium_key) or section.get('race_number') != int(race_key):
                        raise InvalidData(f'{kind} race identity mismatch')
            grade = race.get('grade_number')
            if grade is not None and type(grade) is not int:
                raise InvalidData('grade_number must be integer or null')
            grades[GRADES.get(grade, '不明')] += 1
            records[f'{int(stadium_key):02d}/{int(race_key):02d}'] = race
    return records, {
        'stadium_count': len(stadiums), 'race_count': len(records),
        'with_preview_count': sum(bool(r.get('preview')) for r in records.values()),
        'with_result_count': sum(bool(r.get('result')) for r in records.values()),
        'grade_race_counts': dict(sorted(grades.items())),
    }


def check_regression(old, new):
    if old.keys() - new.keys():
        raise InvalidData('Existing races disappeared; preserve saved data and review source')
    for key, race in old.items():
        for section in ('preview', 'result'):
            if race.get(section) and not new[key].get(section):
                raise InvalidData(f'Existing {section} disappeared at {key}')


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode())


def fetch(target):
    url = f'{BASE}/{target:%Y}/{target:%Y%m%d}.json'
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'boatrace-data/0.1'})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise InvalidData('Response exceeds 20 MiB')
                return raw, {'url': url, 'http_last_modified': response.headers.get('Last-Modified'),
                             'etag': response.headers.get('ETag')}
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** (attempt + 1))


def read_status(root):
    path = root / 'status.json'
    if not path.exists():
        return {'schema_version': 1, 'dates': {}}
    value = decode(path.read_bytes())
    if not isinstance(value, dict) or not isinstance(value.get('dates'), dict):
        raise InvalidData('Invalid local status.json; refusing to overwrite')
    return value


def sync_dates(targets, root, fetcher=fetch, today=None):
    root = Path(root)
    today = today or datetime.now(JST).date()
    status = read_status(root)
    today_path = root / 'today.json'
    # Do not present yesterday's content under today.json, even if upstream fails.
    if today_path.exists():
        try:
            validate(decode(today_path.read_bytes()), today)
        except (ValueError, UnicodeError):
            today_path.unlink()
    failed = False
    for target in targets:
        checked_at = timestamp()
        previous = status['dates'].get(target.isoformat(), {})
        entry = dict(previous)
        entry.update({'last_checked_at': checked_at, 'status': 'error'})
        try:
            raw, source = fetcher(target)
            payload = decode(raw)
            records, summary = validate(payload, target)
            daily = root / f'{target:%Y}/{target:%Y%m%d}.json'
            if daily.exists():
                old, _ = validate(decode(daily.read_bytes()), target)
                check_regression(old, records)
            # Keep original upstream JSON bytes; do not add fields to the payload.
            atomic_write(daily, raw)
            if target == today:
                atomic_write(today_path, raw)
            entry.update({'status': 'ok', 'last_success_at': timestamp(), 'source': source,
                          'sha256': hashlib.sha256(raw).hexdigest(), 'summary': summary})
            entry.pop('error', None)
            print(f'{target}: saved {summary["stadium_count"]} stadiums / {summary["race_count"]} races')
        except (ValueError, UnicodeError, OSError, URLError) as exc:
            entry['error'] = f'{type(exc).__name__}: {exc}'
            failed = True
            print(f'{target}: {entry["error"]}', file=sys.stderr)
        status['dates'][target.isoformat()] = entry
    status.update({'updated_at': timestamp(), 'today_date': today.isoformat(),
                   'today_available': today_path.exists()})
    write_json(root / 'status.json', status)
    available = []
    for key, entry in sorted(status['dates'].items()):
        d = date.fromisoformat(key)
        if (root / f'{d:%Y}/{d:%Y%m%d}.json').exists():
            available.append({'date': key, 'path': f'{d:%Y}/{d:%Y%m%d}.json',
                              'last_success_at': entry.get('last_success_at')})
    write_json(root / 'index.json', {'schema_version': 1, 'dates': available})
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat, help='JST date YYYY-MM-DD; default: today')
    parser.add_argument('--recent-days', type=int, default=1, help='Include previous days, 1..7')
    parser.add_argument('--output', type=Path, default=Path('docs/v1'))
    args = parser.parse_args()
    today = datetime.now(JST).date()
    if not 1 <= args.recent_days <= 7:
        parser.error('--recent-days must be 1..7')
    target = args.date or today
    if target > today or target - timedelta(days=args.recent_days - 1) < date(2026, 1, 1):
        parser.error('Requested dates must be between 2026-01-01 and today JST')
    targets = [target - timedelta(days=n) for n in range(args.recent_days)]
    return sync_dates(targets, args.output, today=today)


if __name__ == '__main__':
    sys.exit(main())
