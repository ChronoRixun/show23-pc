"""Import the public player catalogue and resolve its card IDs."""

import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SITE = 'https://mlb23.theshow.com'
IMAGE_HOSTS = {'mlb23.theshow.com', 'theshownationsite-production-active-storage.s3.amazonaws.com'}
DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
UUID = re.compile(r'[a-f0-9]{32}\Z')
ASSET = re.compile(r'AS_([A-Z0-9_]+)_([0-9]+)\.WEBP\Z', re.I | re.ASCII)


def is_live(card):
    return card.get('series') in ('Live', 'Live Series')


def card_ids(cards):
    remaining = {c['uuid'] for c in cards if not is_live(c)}
    found = {}
    # Covers the observed publisher IDs (~39,000–61,000) with headroom.
    # Hash decimal IDs once and retain only hashes requested by the catalogue.
    for item_id in range(1, 100001):
        if not remaining:
            break
        digest = hashlib.md5(str(item_id).encode('ascii'), usedforsecurity=False).hexdigest()
        if digest in remaining:
            found[digest] = item_id
            remaining.remove(digest)
    return found


def disc_pictures(base, update):
    pictures = {}
    for root in (update, base):
        if root:
            for path in sorted((Path(root) / 'CROSS/LIVE').glob('*')):
                match = ASSET.fullmatch(path.name)
                if match and path.is_file():
                    pictures.setdefault(int(match[2]), (match[1].upper(), path))
    return pictures


TEAM_ALIASES = {'WAS': 'WSH', 'CHW': 'CWS', 'ANA': 'LAA', 'FLA': 'MIA', 'TBD': 'TB'}


def team_key(team):
    return TEAM_ALIASES.get(team, team) if isinstance(team, str) else None


def name_key(value):
    if not isinstance(value, str):
        return ''
    ascii_text = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return ''.join(c for c in ascii_text.upper() if c.isalnum())


def handle_names(handle):
    # A series prefix and a club commonly precede LAST_FIRST. Extra numbered
    # variants and mononyms occur as well. Match whole names and aliases.
    parts = handle.split('_')
    names = set()
    for start in range(min(4, len(parts))):
        suffix = [part for part in parts[start:] if part and not part.isdecimal()]
        if suffix:
            names.add(name_key(''.join(suffix)))
            names.add(name_key(''.join(suffix[1:] + suffix[:1])))
    return names - {''}


def number(value):
    match = re.match(r'^\s*(\d+)', str(value))
    return int(match[1]) if match else None


def bio_fields(card, places):
    height = re.fullmatch(r"""\s*(\d+)\s*'\s*(\d+)\s*"?\s*""", str(card.get('height')))
    return dict(team=team_key(card.get('team_short_name')),
                jersey=number(card.get('jersey_number')), bats=card.get('bat_hand'),
                throws=card.get('throw_hand'), age=number(card.get('age')),
                birth=places.get(name_key(card.get('born') or '')),
                height=int(height[1])*12 + int(height[2]) if height else None,
                weight=number(card.get('weight')))


def roster_identity(decoder, name, blocks, teams, edition):
    record = blocks['record']

    def get(key):
        return decoder.bits(record, decoder.BITS[key])
    first = decoder.string(record[112:128])
    last = decoder.string(record[128:144])
    team_id = get('team')
    position = get('position')
    return dict(
        pid=decoder.uint(record, 186, 2), online=decoder.uint(record, 180, 4), blocks=blocks, name=name,
        names={name_key(name), name_key(last + first)}, edition=edition,
        asset=decoder.picture_stem(first, last),
        team=teams[team_id] if team_id < len(teams) else None,
        jersey={get('jersey_number'), get('original_jersey_number')} - {127},
        age=get('age'), birth=get('birthplace'), height=get('height_inches'),
        weight=get('weight_minus_100') + 100, legend=bool(get('i_am_legend')), pitcher=position == 0,
        positions=set(decoder.POSITIONS[position:position + 1]) | {
            slot for slot in decoder.SECONDARY if get('secondary_' + slot)}, surname=name_key(last),
        bats=(decoder.HANDS + [None])[get('bat_hand')],
        throws=(decoder.HANDS + [None])[get('throw_hand')])


def update_records(decoder, stats, update):
    """Records of the 1.21 tables, unless the active roster already is the update's."""
    if not update:
        return
    folder = Path(update) / 'FRONTEND/STATS'
    try:
        if folder.resolve() == Path(stats).resolve():
            return
    except OSError:
        return
    for source in ('base', 'live'):
        try:
            raw, _, _ = decoder.source_tables(folder, source, False)
        except (OSError, ValueError):
            continue
        yield from decoder.records(raw)


def roster_identities(decoder, stats, base, update, teams, live, update_roster=False):
    raw, _, _ = decoder.source_tables(Path(stats), 'base', False)
    by_id = {}
    for name, blocks, _ in decoder.records(raw):
        pid = decoder.uint(blocks['record'], 186, 2)
        if not name or not pid:
            continue
        if pid in by_id and decoder.fullness(by_id[pid]['blocks']) >= decoder.fullness(blocks):
            continue
        by_id[pid] = roster_identity(decoder, name, blocks, teams, 'base')
    for card in live:
        if card['roster_player_id'] in by_id:
            by_id[card['roster_player_id']]['team'] = card.get('team_short_name')
    # The update spells some names differently ("Michael Harris II"). Only the
    # same roster ID with the same online ID lends its name to a model.
    added = {}
    for name, blocks, _ in update_records(decoder, stats, update):
        record = blocks['record']
        pid, online = decoder.uint(record, 186, 2), decoder.uint(record, 180, 4)
        if not name or not pid:
            continue
        row = by_id.get(pid)
        if row is not None:
            if online == row['online']:
                last, first = decoder.string(record[128:144]), decoder.string(record[112:128])
                row['names'].update((name_key(name), name_key(last + first)))
        elif update_roster and (pid not in added or
                                decoder.fullness(added[pid]['blocks']) < decoder.fullness(blocks)):
            added[pid] = roster_identity(decoder, name, blocks, teams, 'update')
    by_id.update(added)
    by_online = defaultdict(list)
    for row in by_id.values():
        if row['online']:
            by_online[row['online']].append(row)
    for edition, root in (('base', base), ('update', update)):
        if not root:
            continue
        for pid, online, stem, alias in decoder.edition_picture_names(Path(root)):
            exact = by_id.get(pid)
            if exact and exact['edition'] == edition and name_key(stem) in exact['names']:
                exact['asset'] = alias
            candidates = by_online.get(online, ())
            if len(candidates) == 1:
                row = candidates[0]
            else:
                row = exact
                if not row or name_key(stem) not in row['names']:
                    continue
            row['names'].update((name_key(stem), name_key(alias)))
    # A 1.00 game lacks the heads that only the update ships. Keep update-only
    # players whose head is on the base disc or who have no scanned head at all.
    for row in added.values():
        if ((Path(update) / 'PS4/CHARACTERS/HEADS' / row['asset']).is_dir()
                and not (Path(base) / 'PS4/CHARACTERS/HEADS' / row['asset']).is_dir()):
            del by_id[row['pid']]
    return by_id.values()


ERA = re.compile(r'(?:19|20)\d\d\Z')


def person_key(row):
    # Era versions of one player share the online ID (RUTH_BABE, RUTH_BABE1915).
    # Without one, require the same name, hands, height and birthplace.
    if row['online']:
        return 'online', row['online']
    return 'roster', name_key(row['name']), row['bats'], row['throws'], row['height'], row['birth']


def era_year(row, card):
    era = ERA.search(row['asset'] or '')
    if era:
        return int(era[0])
    year, age = card.get('series_year'), number(card.get('age'))
    if type(year) is int and age and row['age']:
        return year - age + row['age']
    return None


def choose_version(rows, card, scores):
    """Pick the version of one player whose era is closest to the card's season."""
    rows = [row for row in rows if row['edition'] == 'base'] or rows
    year = card.get('series_year')

    def key(row):
        era = era_year(row, card)
        distance = abs(era - year) if era is not None and type(year) is int else 999
        return distance, -scores.get(row['pid'], 0), row['pid']
    return min(rows, key=key)


def birthplace_codes(site, names):
    # The roster stores a numeric region, while the site names it. Learn
    # concordant codes from uniquely named identities; ignore divergent places.
    observations = defaultdict(Counter)
    seen = set()
    for card in sorted(site, key=lambda c: not is_live(c)):
        name = name_key(card.get('name') or '')
        place = name_key(card.get('born') or '')
        choices = names.get(name, ())
        if place and len(choices) == 1 and choices[0]['pid'] not in seen:
            row = choices[0]
            seen.add(row['pid'])
            observations[place][row['birth']] += 1
    result = {}
    for place, counts in observations.items():
        code, count = counts.most_common(1)[0]
        if count >= 2 and count >= sum(counts.values()) * 0.85:
            result[place] = code
    return result


def match_score(row, bio):
    weights = {'team': 2, 'jersey': 4, 'bats': 2, 'throws': 2, 'age': 1,
               'birth': 6, 'height': 3, 'weight': 3}
    score = 0
    for key, weight in weights.items():
        value = bio[key]
        if value is not None:
            result = value in row[key] if key == 'jersey' else value == row[key]
            score += weight if result else -weight if key in ('bats', 'throws', 'birth', 'height') else 0
    return score


def match_identity(card, from_name, from_art, by_height, places):
    bio = bio_fields(card, places)
    named = {row['pid'] for row in from_name}
    pictured = {row['pid'] for row in from_art}
    candidates = {row['pid']: row for row in (*from_name, *from_art)}
    if not candidates and card.get('series') == 'Incognito':
        # For a nickname with no name evidence, require exact physical data
        # and a birthplace corroborated by the numbered roster region. A real
        # name without a roster player is someone else's card ("Jacob Young"
        # had matched Ryan Miller).
        candidates = {row['pid']: row for row in by_height.get(bio['height'], ())
                      if bio['birth'] is not None and row['birth'] == bio['birth']
                      and all(bio[key] is not None and bio[key] == row[key]
                              for key in ('weight', 'bats', 'throws'))}
    scores = {pid: match_score(row, bio) + 12 * (pid in named) + 12 * (pid in pictured)
              for pid, row in candidates.items()}
    people = defaultdict(list)
    for row in candidates.values():
        people[person_key(row)].append(row)
    # Compare distinct players. Versions of the same player do not compete.
    ranked = sorted(((max(scores[row['pid']] for row in rows), key) for key, rows in people.items()),
                    key=lambda item: item[0], reverse=True)
    if not ranked:
        return None
    score, key = ranked[0]
    if len(ranked) > 1 and score - ranked[1][0] < 5:
        return None
    rows = people[key]
    pids = {row['pid'] for row in rows}
    if not pids & (named | pictured):
        if not any(bio['jersey'] in row['jersey'] for row in rows) or score < 20:
            return None
    elif (len(ranked) > 1 or not pids & named) and score < 18:
        return None
    row = rows[0] if len(rows) == 1 else choose_version(rows, card, scores)
    # A namesake shares the name but not the person ("Jacob Gonzalez").
    return None if len(contradictions(row, card, bio)) >= 2 else row


def contradictions(row, card, bio):
    found = []
    if bio['bats'] and bio['throws'] and (bio['bats'], bio['throws']) != (row['bats'], row['throws']):
        found.append('hands')
    if bio['height'] and abs(bio['height'] - row['height']) > 1:
        found.append('height')
    if bio['birth'] is not None and bio['birth'] != row['birth']:
        found.append('birthplace')
    year = card.get('series_year')
    if bio['age'] and type(year) is int and not row['legend'] and not is_live(card):
        if abs(ROSTER_SEASON - row['age'] - (year - bio['age'])) > 2:
            found.append('birth year')
    return found


def bio_key(card):
    bio = bio_fields(card, {})
    position = card.get('display_position')
    if position in ('SP', 'RP', 'CP'):
        position = 'P'
    return name_key(card.get('born') or ''), bio['height'], bio['bats'], bio['throws'], position


def match_nickname(card, references):
    key = bio_key(card)
    if not all(key):
        return None
    bio = bio_fields(card, {})
    candidates = {}
    for reference, row in references.get(key, ()):
        other = bio_fields(reference, {})
        if bio['weight'] is None or other['weight'] is None:
            continue
        difference = abs(bio['weight'] - other['weight'])
        if difference > 20:
            continue
        team = bool(bio['team'] and bio['team'] != 'FA' and bio['team'] == other['team'])
        jersey = bio['jersey'] is not None and bio['jersey'] == other['jersey']
        age = bio['age'] is not None and bio['age'] == other['age']
        year = bool(card.get('series_year') and card['series_year'] == reference.get('series_year'))
        if not ((team and jersey) or (team and age and year and difference <= 10)
                or (jersey and age and difference <= 5)):
            continue
        score = 4 * team + 5 * jersey + 4 * age + 3 * year + (20 - difference)//5
        who = person_key(row[0])
        # Multiple cards can corroborate the same player.
        if who not in candidates or candidates[who][0] < score:
            candidates[who] = score, who, row
    ranked = sorted(candidates.values(), key=lambda item: item[0], reverse=True)
    if ranked and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 4):
        rows = ranked[0][2]
        return rows[0] if len(rows) == 1 else choose_version(rows, card, {})
    return None


def people(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[person_key(row)].append(row)
    return list(groups.values())


# The 1.00 roster gives current players their age in the 2023 season. Legend
# and era versions carry the age of their era instead.
ROSTER_SEASON = 2023
OUTFIELD = {'LF', 'CF', 'RF'}


def birth_years(site, by_name):
    """Birth years implied by the named cards of each unambiguously named player."""
    years = defaultdict(set)
    for card in site:
        players = people(by_name.get(name_key(card.get('name') or ''), ()))
        age, year = number(card.get('age')), card.get('series_year')
        if is_live(card):
            year = ROSTER_SEASON          # Live cards state the current age
        if len(players) == 1 and age and type(year) is int:
            years[person_key(players[0][0])].add(year - age)
    return years


def physical_people(card, by_height, places, births, weight, years, jersey, legend_jersey=True):
    """Players with the card's height, hands, birthplace, role, weight and birth year."""
    bio = bio_fields(card, places)
    year = card.get('series_year')
    if (bio['birth'] is None or type(year) is not int
            or any(bio[key] is None for key in ('height', 'weight', 'bats', 'throws', 'age'))):
        return []
    born = year - bio['age']
    position = card.get('display_position')
    pitcher = position in ('SP', 'RP', 'CP')
    found = defaultdict(list)
    for row in by_height.get(bio['height'], ()):
        if ((row['bats'], row['throws'], row['birth'], row['pitcher'])
                != (bio['bats'], bio['throws'], bio['birth'], pitcher)):
            continue
        if abs(row['weight'] - bio['weight']) > (weight if not row['legend'] else min(weight, 20)):
            continue
        # Legend and era records state the age of their era. Without a named card that
        # gives the birth year, they need the jersey instead.
        known = births.get(person_key(row))
        if known:
            if min(abs(value - born) for value in known) > years:
                continue
        elif not row['legend'] and abs(ROSTER_SEASON - row['age'] - born) > years:
            continue
        if (jersey or (row['legend'] and legend_jersey)) and bio['jersey'] not in row['jersey']:
            continue
        if jersey and not (pitcher or position == 'DH' or position in row['positions']
                           or (position in OUTFIELD and row['positions'] & OUTFIELD)):
            continue
        found[person_key(row)].append(row)
    return list(found.values())


def match_physical(card, by_height, places, births):
    """An Incognito card hides the name. Accept one player only when the evidence is complete."""
    # A surname inside the nickname ("Funky Muncy") is name evidence.
    words = {name_key(word) for word in re.split(r'[^A-Za-z]+', card.get('name') or '')}
    named = [rows for rows in physical_people(card, by_height, places, births, 35, 2, False, False)
             if any(len(row['surname']) >= 4 and row['surname'] in words for row in rows)]
    if len(named) == 1:
        return choose_version(named[0], card, {})
    loose = physical_people(card, by_height, places, births, 35, 2, False)
    # Otherwise require the jersey and position as well, and no other player even close.
    strict = physical_people(card, by_height, places, births, 20, 1, True)
    if len(strict) == 1 and len(loose) == 1:
        return choose_version(strict[0], card, {})
    return None


def special_cards(site, pictures, decoder, stats, base, update, teams, live, positions, rarities,
                  update_roster=None):
    if update_roster is None:
        update_roster = os.environ.get('SHOW23_UPDATE_ROSTER_SPECIALS') == '1'
    by_name = defaultdict(list)
    by_height = defaultdict(list)
    for row in roster_identities(decoder, stats, base, update, teams, live, update_roster):
        for alias in row['names']:
            if alias:
                by_name[alias].append(row)
        by_height[row['height']].append(row)
    places = birthplace_codes(site, by_name)
    births = birth_years(site, by_name)
    ids = card_ids(site)
    references = defaultdict(list)
    for card in site:
        players = people(by_name.get(name_key(card.get('name') or ''), ()))
        if len(players) == 1:
            references[bio_key(card)].append((card, players[0]))
    used = {c['item_id'] for c in live}
    matched = []
    dropped = Counter()
    records = {}
    for card in site:
        if is_live(card):
            continue
        item_id = ids.get(card['uuid'])
        if not item_id or item_id in used:
            dropped['item ID'] += 1
            continue
        name = card.get('name')
        team = team_key(card.get('team_short_name'))
        if (not isinstance(name, str) or not name.strip() or not card.get('series')
                or card.get('display_position') not in positions or card.get('rarity') not in rarities
                or type(card.get('ovr')) is not int):
            dropped['card fields'] += 1
            continue
        art_names = handle_names(pictures[item_id][0]) if item_id in pictures else ()
        row = match_identity(card, by_name.get(name_key(name), ()),
                             [r for key in art_names for r in by_name.get(key, ())], by_height, places)
        if row is None and card['series'] == 'Incognito':
            row = match_nickname(card, references) or match_physical(card, by_height, places, births)
        if row is None:
            dropped['identity'] += 1
            continue
        team_id = teams.index(team) if team in teams else -1
        if team_id < 0 and team != 'FA':
            dropped['team'] += 1
            continue
        pid = row['pid']
        if pid not in records:
            records[pid] = decoder.encode_blocks(row['blocks'])
        used.add(item_id)
        matched.append(dict(card, item_id=item_id, roster_player_id=pid, team_id=team_id,
                            team_short_name=team, quantity=1, imported_special=True))
    return matched, records, dropped


def read_json(path):
    with path.open(encoding='utf-8') as file:
        return json.load(file)


def atomic_write(path, data):
    part = path.with_suffix(path.suffix + '.part')
    part.write_bytes(data)
    part.replace(path)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode('utf-8'))


def validate_cards(cards):
    if (not isinstance(cards, list) or
            any(not isinstance(c, dict) or not isinstance(c.get('uuid'), str)
                or not UUID.fullmatch(c['uuid']) for c in cards)):
        raise ValueError('invalid player catalogue items')
    return cards


def complete_catalogue(root):
    root = Path(root)
    if root.is_dir():
        for folder in sorted(root.iterdir(), reverse=True):
            if not DATE.fullmatch(folder.name):
                continue
            try:
                manifest = read_json(folder / 'manifest.json')
                if manifest.get('complete') is not True or manifest.get('date') != folder.name:
                    continue
                cards = validate_cards(read_json(folder / 'items-mlb_card.json'))
                if len(cards) == manifest.get('cards') and len({c['uuid'] for c in cards}) == len(cards):
                    return folder, cards
            except (OSError, ValueError, AttributeError):
                continue
    return None


def allowed_image_url(url):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and parts.netloc in IMAGE_HOSTS and
            (parts.netloc != 'mlb23.theshow.com' or parts.path.startswith('/rails/active_storage/')))


class ImageRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_image_url(newurl):
            raise ValueError('unexpected picture redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NetworkError(Exception):
    pass


class Fetch:
    def __init__(self):
        self.api = build_opener(NoRedirect())
        self.images = build_opener(ImageRedirect())
        self.last = None

    def __call__(self, url, *, picture=False):
        for attempt in range(4):
            if self.last is not None:
                time.sleep(max(0, 0.75 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                req = Request(url, headers={'User-Agent': 'CatalogueImporter/1.0'})
                with (self.images if picture else self.api).open(req, timeout=25) as reply:
                    data = reply.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError('response exceeds size limit')
                return data
            except HTTPError as error:
                if picture and error.code in (404, 410):
                    return b''
                retry = error.code == 429 or error.code >= 500
                delay = 2 ** (attempt + 1)
                if error.code == 429:
                    value = error.headers.get('Retry-After', '')
                    if value.isdecimal():
                        delay = max(delay, int(value))
                    elif value:
                        from email.utils import parsedate_to_datetime
                        try:
                            delay = max(delay, parsedate_to_datetime(value).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
                    delay = max(delay, 5)
                if not retry or attempt == 3:
                    raise NetworkError(f'HTTP {error.code}') from None
            except (URLError, TimeoutError, ConnectionError, HTTPException) as error:
                if attempt == 3:
                    raise NetworkError(str(error)) from None
                delay = 2 ** (attempt + 1)
            time.sleep(delay)


def validate_page(page, number, total=None):
    if not isinstance(page, dict):
        raise ValueError(f'invalid catalogue page {number}')
    pages = page.get('total_pages')
    if (type(pages) is not int or not number <= pages <= 1000 or
            (total is not None and pages != total) or
            ('page' in page and page['page'] != number)):
        raise ValueError(f'invalid pagination on page {number}')
    cards = validate_cards(page.get('items'))
    if not cards:
        raise ValueError(f'empty catalogue page {number}')
    return pages, cards


def webp_size(data):
    chunk = data[12:16]
    if chunk == b'VP8 ' and len(data) >= 30 and data[23:26] == b'\x9d\x01\x2a':
        return tuple(int.from_bytes(data[i:i+2], 'little') & 0x3fff for i in (26, 28))
    if chunk == b'VP8L' and len(data) >= 25 and data[20] == 0x2f:
        dimensions = int.from_bytes(data[21:25], 'little')
        return (dimensions & 0x3fff) + 1, ((dimensions >> 14) & 0x3fff) + 1
    if chunk == b'VP8X' and len(data) >= 38 and not data[20] & 2:
        return tuple(int.from_bytes(data[i:i+3], 'little') + 1 for i in (24, 27))
    return 0, 0


def picture_suffix(data):
    if (len(data) >= 20 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'
            and int.from_bytes(data[4:8], 'little') + 8 == len(data)):
        width, height = webp_size(data)
        if 0 < width <= 1024 and 0 < height <= 1024 and width * height <= 1024 * 512:
            return '.webp'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    return None


def convert_cached_pictures(art):
    """Keep the original and fit PNG/JPEG artwork onto the usual 512px card canvas."""
    if not art.is_dir():
        return 0
    try:
        from PIL import Image, ImageOps
    except (ImportError, OSError):
        return 0
    try:
        sources = sorted(art.iterdir())
    except OSError:
        return 0
    converted = 0
    for source in sources:
        if source.suffix.lower() not in ('.png', '.jpg', '.jpeg') or not UUID.fullmatch(source.stem):
            continue
        target = source.with_suffix('.webp')
        if target.is_file():
            continue
        try:
            with Image.open(source, formats=('PNG', 'JPEG')) as picture:
                if picture.width * picture.height > 4096 * 4096:
                    continue
                rgba = ImageOps.exif_transpose(picture).convert('RGBA')
            canvas = ImageOps.pad(rgba, (512, 512), Image.Resampling.LANCZOS, color=(0, 0, 0, 0))
            for quality in (90, 75, 60, 45, 30):
                buffer = io.BytesIO()
                canvas.save(buffer, format='WEBP', quality=quality)
                # Allow room for the headers and member name in the stored texture ZIP.
                if buffer.tell() <= 512 * 1024 - 256:
                    atomic_write(target, buffer.getvalue())
                    converted += 1
                    break
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
    return converted


def import_day(root):
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if root.is_dir():
        for folder in sorted(root.iterdir(), reverse=True):
            if not DATE.fullmatch(folder.name) or folder.name > today:
                continue
            if (folder / 'manifest.json').is_file():
                break
            if (folder / 'raw').is_dir():
                return folder.name
    return today


def import_catalogue(root, base, update, pictures=False, *, fetch=None, output=print, day=None):
    fetch = fetch or Fetch()
    root = Path(root)
    day = day or import_day(root)
    folder = root / day
    raw = folder / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    number = 1
    total = None
    cards = {}
    while total is None or number <= total:
        path = raw / f'items-mlb_card-p{number:03d}.json'
        try:
            pages, batch = validate_page(read_json(path), number, total)
        except (OSError, ValueError):
            data = fetch(f'{SITE}/apis/items.json?type=mlb_card&page={number}')
            pages, batch = validate_page(json.loads(data), number, total)
            atomic_write(path, data)
        total = pages
        for card in batch:
            if card['uuid'] in cards:
                raise ValueError(f'duplicate card on catalogue page {number}')
            cards[card['uuid']] = card
        output(f'import: page {number}/{total}')
        number += 1
    if pictures:
        art = folder / 'art' / 'img'
        art.mkdir(parents=True, exist_ok=True)
        on_disc = disc_pictures(base, update)
        ids = card_ids(cards.values())
        for card in cards.values():
            uuid = card['uuid']
            url = card.get('img')
            if is_live(card) or ids.get(uuid) in on_disc or not isinstance(url, str):
                continue
            if not allowed_image_url(url):
                continue
            if any((art / (uuid + ext)).is_file() for ext in ('.webp', '.png', '.jpg')):
                continue
            data = fetch(url, picture=True)
            extension = picture_suffix(data)
            if extension:
                atomic_write(art / (uuid + extension), data)
        convert_cached_pictures(art)
    write_json(folder / 'items-mlb_card.json', list(cards.values()))
    write_json(folder / 'manifest.json', {
        'complete': True, 'cards': len(cards), 'date': day,
        'finished_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'pages': total, 'source': 'mlb23.theshow.com',
    })
    output(f'import: done {len(cards)} cards')


def cli_import(root, base, update, pictures):
    try:
        import_catalogue(root, base, update, pictures, output=lambda line: print(line, flush=True))
    except NetworkError as error:
        print(f'import: network error: {error}', file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f'import: {error}', file=sys.stderr)
        return 1
    return 0
