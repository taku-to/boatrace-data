"""Read only explicitly published outcomes for races whose upstream record regressed."""
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import re
import time
import unicodedata

BASE = 'https://www.boatrace.jp/owpc/pc/race/'
VENUES = '桐生 戸田 江戸川 平和島 多摩川 浜名湖 蒲郡 常滑 津 三国 びわこ 住之江 尼崎 鳴門 丸亀 児島 宮島 徳山 下関 若松 芦屋 福岡 唐津 大村'.split()
CANCELLED = {'レース中止', '中止', 'レース不成立'}


class Cell:
    def __init__(self, attrs):
        self.parts = []
        self.links = []
        self.images = []
        self.classes = set()
        self.width = int(attrs.get('colspan', '1'))

    @property
    def text(self):
        return ''.join(unicodedata.normalize('NFKC', ''.join(self.parts)).split())


class Tables(HTMLParser):
    """Collect table cells, plus same-race navigation links for identity checks."""
    def __init__(self, body):
        super().__init__()
        self.tables = []
        self.links = []
        self.table = self.row = self.cell = None
        self.feed(body)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.table = []
            self.tables.append(self.table)
            self.row = self.cell = None
        elif tag == 'tr' and self.table is not None:
            self.row = []
            self.table.append(self.row)
            self.cell = None
        elif tag in ('th', 'td') and self.row is not None:
            self.cell = Cell(attrs)
            self.row.append(self.cell)
        if tag == 'a' and 'href' in attrs:
            self.links.append(attrs['href'])
            if self.cell is not None:
                self.cell.links.append(attrs['href'])
        if self.cell is not None:
            self.cell.classes.update(attrs.get('class', '').split())
            if tag == 'img':
                self.cell.images.append(attrs.get('alt', ''))

    def handle_endtag(self, tag):
        if tag in ('th', 'td', 'tr', 'table'):
            self.cell = None
        if tag in ('tr', 'table'):
            self.row = None
        if tag == 'table':
            self.table = None

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.parts.append(text)


def race_link(href, day, page):
    path = urlsplit(href)
    if path.path != '/owpc/pc/race/' + page:
        return None
    query = parse_qs(path.query)
    if query.get('hd') != [day.strftime('%Y%m%d')]:
        return None
    try:
        return int(query['jcd'][0]), int(query['rno'][0])
    except (KeyError, ValueError):
        return None


def trifecta_rows(table):
    entries = []
    reading = False
    for row in table:
        if not row:
            continue
        if row[0].text == '3連単':
            if reading:
                raise ValueError('duplicate trifecta block')
            reading = True
            row = row[1:]
        elif not reading:
            continue
        elif row[0].text in {'3連複', '2連単', '2連複', '拡連複', '単勝', '複勝'}:
            break
        cells = [c.text for c in row]
        if not any(cells):
            continue
        if len(cells) < 2 or not re.fullmatch(r'¥(?:[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)', cells[1]):
            raise ValueError('incomplete official payout')
        combo = cells[0]
        amount = int(cells[1][1:].replace(',', ''))
        if re.fullmatch(r'[1-6]-[1-6]-[1-6]', combo) and len(set(combo.split('-'))) == 3:
            entries.append(dict(combination=combo, amount=amount, label=None))
        elif combo in ('特払', '不成立'):
            entries.append(dict(combination=None, amount=amount, label=combo))
        else:
            raise ValueError('unrecognized official combination')
    return entries


def parse_result(body, day, venue, race):
    doc = Tables(body)
    # Race-selector links point to all 12 races. The 出走表 tab identifies this exact race.
    identities = {key for h in doc.links if (key := race_link(h, day, 'racelist')) is not None}
    if identities != {(venue, race)}:
        raise ValueError('race identity not confirmed')
    entries = refunds = None
    for table in doc.tables:
        if not table or not table[0]:
            continue
        headers = [cell.text for cell in table[0]]
        if headers == ['勝式', '組番', '払戻金', '人気']:
            if entries is not None:
                raise ValueError('ambiguous payout tables')
            entries = trifecta_rows(table)
        elif headers == ['返還']:
            if refunds is not None or len(table) != 2 or len(table[1]) != 1:
                raise ValueError('unrecognized refund table')
            cell = table[1][0]
            if 'numberSet1_row' not in cell.classes or not re.fullmatch('[1-6]*', cell.text):
                raise ValueError('unrecognized refund entries')
            refunds = [int(x) for x in cell.text]
            if len(refunds) != len(set(refunds)):
                raise ValueError('duplicate refund entry')
    if not entries or refunds is None:
        return None
    return {'payouts': {'trifecta': entries}, 'refunds': refunds,
            'remarks': '【返還艇あり】' if refunds else None}


def cancellations(body, day):
    """Map explicit 中止 cells via venue header spans, corroborated by a dated race link."""
    cancelled = set()
    for table in Tables(body).tables:
        columns = []
        for row in table:
            if any(c.images and any(v in VENUES for v in c.images) for c in row):
                columns = []
                for c in row:
                    names = [VENUES.index(v) + 1 for v in c.images if v in VENUES]
                    columns.extend([names[0] if len(names) == 1 else None] * min(100, c.width))
            if not columns or not row or not re.fullmatch(r'(?:[1-9]|1[0-2])R', row[0].text):
                continue
            if sum(c.width for c in row) != len(columns):
                continue
            race = int(row[0].text[:-1])
            slot, proven, candidates = 0, False, []
            for c in row:
                venue = columns[slot]
                for h in c.links:
                    linked = race_link(h, day, 'raceresult') or race_link(h, day, 'racelist')
                    if linked:
                        if linked != (venue, race):
                            raise ValueError('ambiguous official cancellation grid')
                        proven = True
                if venue and c.text in CANCELLED and all(v == venue for v in columns[slot:slot+c.width]):
                    candidates.append((venue, race))
                slot += c.width
            if proven:
                cancelled.update(candidates)
    return cancelled


def fetch_page(page, **query):
    url = BASE + page + '?' + urlencode(query)
    req = Request(url, headers={'User-Agent': 'boatrace-data/1.0', 'Accept': 'text/html'})
    with urlopen(req, timeout=6) as res:
        if res.geturl() != url or res.headers.get_content_type() != 'text/html':
            raise ValueError('unexpected official response')
        body = res.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('oversized official page')
        return body.decode(res.headers.get_content_charset() or 'utf-8')


class OfficialResults:
    def __init__(self, day: date):
        self.day = day
        self.cancelled = None
        self.last_request = 0.0

    def read(self, page, **query):
        pause = 1.0 - (time.monotonic() - self.last_request)
        if pause > 0:
            time.sleep(pause)
        self.last_request = time.monotonic()
        return fetch_page(page, **query)

    def __call__(self, venue, race):
        ymd = self.day.strftime('%Y%m%d')
        if self.cancelled is None:
            self.cancelled = set()
            try:
                self.cancelled = cancellations(self.read('pay', hd=ymd), self.day)
            except (ValueError, OSError):
                pass  # Individual result pages remain usable if the overview is unavailable.
        if (venue, race) in self.cancelled:
            return {'payouts': {'trifecta': []}, 'refunds': None, 'remarks': 'レース中止'}
        body = self.read('raceresult', hd=ymd, jcd=f'{venue:02}', rno=race)
        return parse_result(body, self.day, venue, race)
