# A local stand-in for the online servers of MLB The Show 23, first stage: it accepts the game's
# connections, answers the sign-in exchange and the keep-alive, logs every message, and answers
# every "x.y" service request with a canned XML body (replies\<name>.xml next to this file if it
# exists, otherwise an empty document). It talks to nothing but the game on this machine.
#
#   python tools\olserver\olserver.py gen-certs     make our own authority and server certificate
#   python tools\olserver\olserver.py selftest      codec and TLS round trip against itself
#   python tools\olserver\olserver.py run           listen on 127.0.0.1:10000 (auth and session)
#
# Protocol facts (HANDOFF "Later, after the main work"): a frame is
# [u32 total length incl. these 8 bytes][u32 type id][body], little-endian. A string is a u32
# length and, if not empty, the length again as a base-128 varint and then the bytes. A block of
# bytes is a u32 length and the bytes. Type IDs come from the packaged message table or the research table.
#
# Installed run: python server\olserver.py run --install <folder>
# The install launcher passes --show23-local-ca with the ca.pem from data\profiles\default\server.
# Development uses probes\olserver and the research launcher settings.
import asyncio, base64, csv, datetime, hashlib, json, math, os, random, re, secrets, ssl, struct, sys, tempfile, time, zipfile
import xml.etree.ElementTree as ET
from collections import ChainMap, Counter
from functools import lru_cache
from typing import NamedTuple
from uuid import uuid4

class ServerLayout(NamedTuple):
    install: str | None
    base: str
    update: str | None
    work: str
    state: str
    logs: str
    replies: str
    messages: str
    catalogue: str
    executables: tuple = ()


def install_option(args):
    install = None
    command = []
    it = iter(args)
    for arg in it:
        if arg == '--install' or arg.startswith('--install='):
            value = next(it, '') if arg == '--install' else arg.split('=', 1)[1]
            if not value or value.startswith('--') or install is not None:
                sys.exit('install layout: give one folder with --install')
            install = os.path.abspath(value)
        else:
            command.append(arg)
    return install, command


def resolve_layout(here, root, install):
    packaged_table = os.path.join(here, 'tables', 'ol-messages.csv')
    if install is None:
        work = os.path.join(root, 'probes', 'olserver')
        local_state = os.path.dirname(here) == os.path.join(root, 'probes') and (
            os.path.basename(here).startswith('conquest-') or os.path.basename(here) in {
            'equip-faces', 'equip-work', 'exchange-work', 'exchange-repeatable',
            'exchange-carousel', 'shop-work', 'shop-polish', 'programs-work',
            'affinity-work', 'affinity-divisions', 'affinity-repeatable', 'affinity-open',
            'affinity-open2', 'showdown-work', 'showdown-stage3',
        })
        return ServerLayout(None, os.path.join(root, 'Extracted', 'CUSA35521'),
                            os.path.join(root, 'Extracted', 'patch-0121', 'CUSA35521-patch'),
                            work, os.path.join(here if local_state else work, 'state.json'),
                            os.path.join(root, 'logs', 'olserver'), os.path.join(root, 'tools', 'olserver', 'replies'),
                            packaged_table if os.path.isfile(packaged_table) else
                            os.path.join(root, 'reports', 'online', 'ol-messages.csv'),
                            os.path.join(root, 'snapshots', 'theshow23-site'))

    data = os.path.join(install, 'data')
    record_path = os.path.join(data, 'games', 'CUSA35521', 'game.json')
    try:
        with open(record_path, encoding='utf-8') as f:
            record = json.load(f)
    except FileNotFoundError:
        sys.exit('install layout: game.json not found under data/games/CUSA35521')
    except (OSError, ValueError):
        sys.exit('install layout: cannot read a valid game.json')
    if not isinstance(record, dict) or type(record.get('schema')) is not int or record['schema'] != 1:
        sys.exit('install layout: game.json schema must be 1')
    if record.get('title_id') != 'CUSA35521':
        sys.exit('install layout: game.json title_id must be CUSA35521')

    def game_folder(kind):
        entry = record.get(kind)
        path = entry.get('path') if isinstance(entry, dict) else None
        if not isinstance(path, str) or not os.path.isabs(path):
            sys.exit(f'install layout: game.json {kind}.path must be an absolute folder')
        if not isinstance(entry.get('version'), str) or not entry['version'].strip():
            sys.exit(f'install layout: game.json {kind}.version must be a string')
        if not os.path.isdir(path):
            sys.exit(f'install layout: game.json {kind} folder does not exist')
        return os.path.abspath(path)

    base = game_folder('base')
    update = game_folder('update') if 'update' in record else None
    replies = os.path.join(here, 'replies')
    if not os.path.isdir(replies):
        sys.exit('install layout: packaged replies folder is missing beside olserver.py')
    if not os.path.isfile(packaged_table):
        sys.exit('install layout: packaged tables/ol-messages.csv is missing beside olserver.py')
    work = os.path.join(data, 'profiles', 'default', 'server')
    return ServerLayout(install, base, update, work, os.path.join(work, 'state.json'),
                        os.path.join(data, 'logs'), replies, packaged_table,
                        os.path.join(data, 'import-cache'),
                        tuple(json.dumps(record.get(kind, {}).get('executable'), sort_keys=True)
                              for kind in ('base', 'update')))


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
_install, _command_args = install_option(sys.argv[1:] if __name__ == '__main__' else [])
LAYOUT = globals().get('_INHERITED_LAYOUT') or resolve_layout(HERE, ROOT, _install)
WORK = LAYOUT.work
REPLIES_DIR = LAYOUT.replies
LOGDIR = LAYOUT.logs
CATALOGUE_DIR = LAYOUT.catalogue
HOSTS = ['production.mlb.mlbtheshow.net', 'production.mlb23.mlbtheshow.net']
PORT = 10000
ACCOUNT_ID = 0x0123456789abcdef  # the emulator's local account id (scout_online.h)
USER_NAME = 'ScoutLocal'

NAMES = {}
IDS = {}
with open(LAYOUT.messages) as f:
    for row in csv.DictReader(f):
        if row['type_id']:
            NAMES[int(row['type_id'], 16)] = row['name']
            IDS[row['name']] = int(row['type_id'], 16)


# ---------------------------------------------------------------- codec
class Writer:
    def __init__(self):
        self.b = bytearray()

    def u8(self, v):
        self.b += struct.pack('<B', v & 0xff)
        return self

    def u16(self, v):
        self.b += struct.pack('<H', v & 0xffff)
        return self

    def u32(self, v):
        self.b += struct.pack('<I', v & 0xffffffff)
        return self

    def u64(self, v):
        self.b += struct.pack('<Q', v & 0xffffffffffffffff)
        return self

    def raw(self, data):
        self.b += data
        return self

    def string(self, s):
        data = s.encode('utf-8') if isinstance(s, str) else bytes(s)
        self.u32(len(data))
        if data:
            n = len(data)
            while n > 0x7f:
                self.b.append((n & 0x7f) | 0x80)
                n >>= 7
            self.b.append(n)
            self.b += data
        return self

    def blob(self, data):
        self.u32(len(data))
        self.b += data
        return self


class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def left(self):
        return len(self.d) - self.p

    def take(self, n):
        if self.left() < n:
            raise ValueError(f'short read: want {n}, have {self.left()}')
        out = self.d[self.p:self.p + n]
        self.p += n
        return out

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack('<H', self.take(2))[0]

    def u32(self):
        return struct.unpack('<I', self.take(4))[0]

    def u64(self):
        return struct.unpack('<Q', self.take(8))[0]

    def string(self, check_length=False):
        n = self.u32()
        if n == 0:
            return ''
        m, shift = 0, 0
        while True:
            c = self.u8()
            m |= (c & 0x7f) << shift
            shift += 7
            if not c & 0x80:
                break
            if check_length and shift >= 35:    # repeated length must fit the preceding u32
                raise ValueError('unterminated string length')
        if check_length and m != n:
            raise ValueError('mismatched string lengths')
        return self.take(m).decode('utf-8', 'replace')

    def blob(self):
        return self.take(self.u32())


def frame(name, body):
    body = bytes(body.b if isinstance(body, Writer) else body)
    return struct.pack('<II', len(body) + 8, IDS[name]) + body


# Below the frames there is a chunk layer (connection update routine at image offset 0x16ce9d0):
# the byte stream of frames is cut into pieces of at most 0x7fc0 bytes, each sent as
# [0xa5][flags: 2, plus 4 if the piece is LZ4-compressed][u32 length][bytes]. The receiver drops
# the connection on any other first byte or a length above 0x8000.
CHUNK_MAX = 0x7fc0


def chunk(data):
    out = bytearray()
    for i in range(0, len(data), CHUNK_MAX):
        piece = data[i:i + CHUNK_MAX]
        out += b'\xa5\x02' + struct.pack('<I', len(piece)) + piece
    return bytes(out)


def lz4_block_decode(src, limit=0x8000):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        length = token & 15
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        if offset == 0 or offset > len(out):
            raise ValueError('bad LZ4 offset')
        for _ in range(length):
            out.append(out[-offset])
        if len(out) > limit:
            raise ValueError('LZ4 output too long')
    return bytes(out)


async def read_chunk(reader):
    """One chunk from the wire: (flags, plain bytes)."""
    head = await reader.readexactly(6)
    length = struct.unpack_from('<I', head, 2)[0]
    if head[0] != 0xa5 or length > 0x8000:
        rest = b''
        try:
            rest = await asyncio.wait_for(reader.read(64), 0.5)
        except Exception:
            pass
        raise ValueError('not a chunk header: ' + (head + rest).hex(' '))
    data = await reader.readexactly(length)
    return head[1], (lz4_block_decode(data) if head[1] & 4 else data)


def expiry(hours=24):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    return t.strftime('%Y-%m-%dT%H:%M:%S.00Z')


def auth_ticket(w):
    # expiry, 16 bytes, two blocks (they look like a token and its signature)
    return w.string(expiry()).raw(b'SCOUT-AUTH-TKT01').blob(b'scout-token').blob(b'scout-signature')


def session_ticket(w):
    # two 64-bit numbers, name, expiry, 16 bytes, two blocks
    return (w.u64(ACCOUNT_ID).u64(ACCOUNT_ID).string(USER_NAME).string(expiry())
            .raw(b'SCOUT-SESS-TKT01').blob(b'scout-session').blob(b'scout-signature'))


def build_auth_result():
    w = Writer().u8(1)
    auth_ticket(w)
    return frame('olAuthLoginResultV3', w.u32(0).string('').u64(0))


def build_account_response():
    w = Writer().u8(0)
    return frame('olAccountLoginResponse', session_ticket(w))


def build_service_answer():
    # Reply to olQueryService ("which server do I use"): a flag byte; an address as the game
    # stores one (IPv4 bytes in network order, ff ff, ten zero bytes) and a port; 16 bytes (the
    # node id the game echoes in olServiceRegisterClient) and a 64-bit number the game's callback
    # ignores (reader 0x16c5340, handler 0xa5b410, callback 0xa0b710).
    # The flag byte must NOT be 0: the handler passes (byte == 0) to the callback, which then
    # throws the sign-in ticket away (0xb13e00) before it takes the node. Runs 72 to 76 sent 0;
    # that is why the game signed in a second time and registered with an all-zero ticket.
    w = Writer().u8(1)
    # The port goes out in network byte order: run 73 sent it little-endian and the game
    # connected to 4135 (0x1027) instead of 10000 (0x2710).
    w.raw(bytes([127, 0, 0, 1, 0xff, 0xff]) + bytes(10)).raw(struct.pack('>H', PORT))
    w.raw(b'SCOUT-NODE-ID-01').u64(1)
    return frame('olServiceAnswer', w)


def parse_service_request(body):
    r = Reader(body)
    out = {'id': r.u32(), 'name': r.string(), 'params': r.string(), 'attachments': {}, 'blobs': {}}
    if r.left():
        names = [r.string() for _ in range(r.u32())]
        # Writer 0x16c4130: a separate blob count follows the list of names. If the bytes do not fit that
        # reading, keep the request anyway (a request that cannot be read is never answered and the game
        # would wait): the old reading counted sizes only.
        try:
            if (names or r.left()) and r.u32() != len(names):
                raise ValueError('attachment name and blob counts differ')
            for n in names:
                data = r.blob()
                out['attachments'][n] = len(data)
                out['blobs'][n] = data
        except Exception as e:
            out['attachments'] = {n: -1 for n in names}
            out['blobs'] = {}
            out['attachment_error'] = repr(e)
    return out


def build_service_response(req_id, name, body):
    return frame('olServiceResponse', Writer().u32(req_id).string(name).string(body))


# The reply to a service request is an XML document with any root element and two children:
#   <status><id>0</id><message>text</message></status>   id 0 = good (validator 0xa79db0; without
#                                                         it the game shows "Response Error. Code 2.")
#   <response ...>...</response>                          what the request's handler reads; missing
#                                                         gives "Response Error. Code 1."
# Inside <response> the handlers read values from ATTRIBUTES (accessors 0xacd5a0 text, 0xacdc10
# and 0xacda50 numbers); child elements are sub-records. Run 77 sent a bare <response/> and got
# code 2.
def envelope(inner='<response/>', status=0, message='OK'):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<result><status><id>%d</id><message>%s</message></status>'
            '%s</result>\n' % (status, message, inner))


EMPTY_XML = envelope()
POST_GAME_FALLBACK = envelope('<response><post_game/></response>')  # required by 0xbfbd30 / 0xbfc05c


# ---------------------------------------------------------------- content updates (rosters)
# The game asks content_update.latest {"content_update_type": T} for each type and expects
#   <response><content_update uuid= created_at= content_url= md5= description= id= name= size=/></response>
# (parser 0xaebf10; all eight attributes are required). If md5 and size differ from the zip it
# has cached, it downloads content_url (plain HTTP GET, which the emulator's sink sends to our web
# listener) and reads one named file out of the zip (0xa08610). Without the element the game
# reports "Failed to Download Live Roster." and Diamond Dynasty refuses to open (run 78).
# We hand the game the roster files of its own disc. The zips are made from the user's dump and
# stay in probes\olserver\content (local only, never in a repo).
CONTENT_DIR = os.path.join(WORK, 'content')
CONTENT_URL = 'https://s3.amazonaws.com/mlb23-production-2023/scout-content/'
# Where the roster files come from: "base" is the v1.00 disc (launch-day rosters), "patch" the
# v1.21 update unpacked into Extracted\\patch-0121 (rosters of October 2023; same size, same
# format version 59). The default stays "base" until the patch rosters have been tried in the
# v1.00 game.
ROSTER_DIRS = {'base': os.path.join(LAYOUT.base, 'FRONTEND', 'STATS')}
if LAYOUT.update is not None:
    ROSTER_DIRS['patch'] = os.path.join(LAYOUT.update, 'FRONTEND', 'STATS')
ROSTER_SOURCE = 'base'
CONTENT = {  # type: (file name inside the zip, file on the disc)
    'LIVE_ROSTERS': ('roster_LIVE.dat', 'ROSTER_LIVE.DAT'),
    'DEFAULT_ROSTERS': ('roster.dat', 'ROSTER.DAT'),
    'MTO_ROSTERS': ('roster_MTO.dat', 'ROSTER_MTO.DAT'),
}
_content_info = {}


def build_content(log=print):
    """Make the zips that are missing; remember size and MD5 of each."""
    folder = os.path.join(CONTENT_DIR, ROSTER_SOURCE)
    os.makedirs(folder, exist_ok=True)
    log(f'content: rosters from "{ROSTER_SOURCE}" ({ROSTER_DIRS[ROSTER_SOURCE]})')
    for kind, (inner, disc_name) in CONTENT.items():
        path = os.path.join(folder, kind + '.zip')
        src = os.path.join(ROSTER_DIRS[ROSTER_SOURCE], disc_name)
        if not os.path.exists(path):
            if not os.path.exists(src):
                log(f'content {kind}: {src} not found, the game will be told nothing')
                continue
            with zipfile.ZipFile(path + '.part', 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                z.write(src, inner)
            os.replace(path + '.part', path)
        data = open(path, 'rb').read()
        _content_info[kind] = {'size': len(data), 'md5': hashlib.md5(data).hexdigest()}
        log(f'content {kind}: {inner} in {os.path.basename(path)}, {len(data)} bytes, md5 {_content_info[kind]["md5"]}')


def content_update_reply(params):
    try:
        kind = json.loads(params or '{}').get('content_update_type', '')
    except ValueError:
        kind = ''
    info = _content_info.get(kind)
    if not info:
        return None
    return ('<response><content_update uuid="5c0a7000-0000-4000-8000-%012d" created_at="2023-03-28T00:00:00Z" '
            'content_url="%s%s.zip" md5="%s" description="Roster from the disc" id="%d" name="%s" size="%d"/></response>'
            % (list(CONTENT).index(kind) + 1, CONTENT_URL, kind, info['md5'], list(CONTENT).index(kind) + 1, kind, info['size']))


# ---------------------------------------------------------------- what the server remembers
# One JSON file for the local player (probes\\olserver\\state.json, local only). So far: the
# Diamond Dynasty team made with arena_team.create.
STATE_FILE = LAYOUT.state
DD_TEAM_ID = 10001       # custom logo and uniform branches (0x9e7885, 0x199723a)
LEGACY_DD_TEAM_ID = 1


def migrate_local_team(state):
    team = state.get('arena_team')
    old_ids = (LEGACY_DD_TEAM_ID, str(LEGACY_DD_TEAM_ID))
    if not isinstance(team, dict) or team.get('id') not in old_ids:
        return False
    team['id'] = DD_TEAM_ID
    if team.get('arena_team_id') in old_ids:
        team['arena_team_id'] = DD_TEAM_ID
    for side in ('home', 'away'):
        key = f'{side}_uniform_id'
        # An absent selector inherited the custom ID. An explicit 1 can also mean Boston
        # home (0xdb8a18). GUESS for old saves with no selection history: saved parts on
        # this side indicate a custom save, as in run 85.
        if key not in team or (team[key] in old_ids and any(
                name.startswith('uniform_') and name.endswith(f'_{side}') for name in team)):
            team[key] = DD_TEAM_ID
    return True


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    if migrate_local_team(state):
        save_state(state)
    return state


def save_state(state):
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)
    with open(STATE_FILE + '.part', 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1)
    os.replace(STATE_FILE + '.part', STATE_FILE)


# ---------------------------------------------------------------- profile XP and rewards
# Grant amounts by index (0xa92550). Which of the six extra bytes holds the difficulty bucket is unknown.
XP_GRANT = [50, 25, 40, 100, 150, 8, 15, -30, 75, -25, -20, -10, -30, 30, 15, 75, 15, 60, 60, -50,
            5, 75, -25, -25, 0]
UP_XP_PER_LEVEL = 10000                   # invented level curve
PATH_LEVELS = 40                          # invented reward path, including level 1 at 0 tickets
PATH_STEP = 2500                          # invented tickets per tier
PATH_TOTAL = (PATH_LEVELS - 1) * PATH_STEP
BONUS_STEP = 25000                        # shown by the game's Unlimited Bonus Rewards text
STUBS_PER_GAME = 200                      # invented flat amount until win/loss is known
STARTING_STUBS = 5000                     # a new install's opening balance, enough for a few packs
PROGRAM_ID = 1                            # local reward path
PROGRAM_NAME = 'Season 1 XP Reward Path'
PROGRAM_DESCRIPTION = 'Earn XP by playing games.'
STUBS_EXTRA_TYPE = 0                       # GUESS: currency icon index in the extra-reward widgets


class PathTier(NamedTuple):
    level: int
    threshold: int
    stubs: int
    pack_id: int


PATH_TIERS = tuple(
    PathTier(level, (level - 1) * PATH_STEP, 0 if level % 5 == 0 else 200,
             510003 if level == PATH_LEVELS else 510001 if level % 5 == 0 else 0)
    for level in range(1, PATH_LEVELS + 1)
)


def program_tickets(profile):
    return max(0, profile.get('program_tickets', 0))


def grant_program_tiers(state, tickets):
    """Grant eligible tiers before persisting rewards and their claim IDs together."""
    profile = state.setdefault('profile', {})
    claimed = set(profile.get('program_tier_claims', ()))
    eligible = [tier for tier in PATH_TIERS if tier.threshold <= tickets and tier.level not in claimed]
    if not eligible:
        return False
    for tier in eligible:
        if tier.pack_id:
            grant_owned_pack(state.setdefault('pack_shop', {}), tier.pack_id)
        else:
            profile['stubs'] = profile.get('stubs', 0) + tier.stubs
        claimed.add(tier.level)
    profile['program_tier_claims'] = sorted(claimed)
    return True


def program_fields(profile, *, available=True):
    earned = program_tickets(profile)
    capped = min(earned, PATH_TOTAL)
    level = capped // PATH_STEP + 1
    next_level = min(level + 1, PATH_LEVELS)
    name = PROGRAM_NAME if available else 'Program unavailable'
    desc = PROGRAM_DESCRIPTION if available else 'This program is unavailable on the local server.'
    return dict(id=PROGRAM_ID if available else 0, name=name, name_short=name, subtitle=desc, description=desc,
                tickets_earned=capped, total_program_tickets=PATH_TOTAL, xp_earned=earned,
                current_level=level, next_level=next_level, xp_to_next_level=(next_level - 1) * PATH_STEP - capped,
                percent_complete=100 * capped // PATH_TOTAL, is_complete=int(capped == PATH_TOTAL),
                current_stage=0, remaining_time=0, ui_type=0,
                unlimited_tickets_earned=max(0, earned - PATH_TOTAL) % BONUS_STEP,
                texture_name='', texture_name_bkg='',
                xp_earned_apao34n_=earned,  # script 0xe344726d, amount paired with progress_subtitle
                bonus_xp_aaaaaaavtey_=max(0, earned - PATH_TOTAL) % BONUS_STEP,  # script 0xc4737c06
                program_banner_aepd1z99='',  # script 0x861a496e, optional header binding
                reward_unlocks_aaaa0aaa_yvx=level if available else 0)  # script 0xf97c6860, P_DATA count


def program_goal(program, tickets, *, available=True, maximum=PATH_TOTAL, goal_name='Earn XP',
                 show_xp_tier=True):
    # Use one unlocked stage and one non-moment goal. 0xf0b888 selects stage index zero.
    stage = mini_node(ET.SubElement(program, 'stages'), 'stage', stage=0, is_unlocked=1)
    name = goal_name if available else 'Unavailable'
    maximum = maximum if available else 1
    description = program.attrib['description']
    if available and show_xp_tier:
        level = int(program.attrib['current_level'])
        remaining = int(program.attrib['xp_to_next_level'])
        description = (f'Tier {level} of {PATH_LEVELS}. {remaining} XP to the next tier.'
                       if remaining else f'Tier {PATH_LEVELS} of {PATH_LEVELS}. Reward path complete.')
    common = dict(id=1, name=name, description=description, type=10,
                  type_tab=0, is_complete=int(tickets >= maximum), tickets=0)
    goals = mini_node(stage, 'goals', is_group=0, mode_name='', **common)
    goal = mini_node(goals, 'goal', progress_current=tickets, progress_max=maximum,
                     mission_criteria_id=0, preview_count=0, stat='', **common)
    ET.SubElement(goal, 'items')


def program_pack_fields(pack_id):
    pack = SHOP_PACKS[pack_id]
    return dict(id=pack_id, item_id=pack_id, type_id=4, name=pack.name, description=shop_description(pack, None),
                texture_name=pack.texture, quantity=1, rank=0,
                rarity_id=max(RARITY_ID.get(draw.rarity, 0) for draw in pack.draws),
                no_sell=1, odds_valid=0, item_draft_pack_id=0, anim_index=0,
                is_live_set=0, captain=0, captained=0, set_name='',
                item_label_aaadeb49z9__=pack.name)  # script 0xf1697e59


def program_item(parent, tier):
    fields = program_pack_fields(tier.pack_id) if tier.pack_id else dict(
        id=0, item_id=0, type_id=-1, name='Stubs', description='Stubs credited to your wallet.',
        quantity=0, rank=0, rarity_id=0, texture_name='', item_label_aaadeb49z9__='Stubs')
    item = mini_node(parent, 'item', **fields)
    ET.SubElement(item, 'item_exclusives')


def program_extra(parent, tag, amount):
    mini_node(parent, tag, amount=amount, type_index=STUBS_EXTRA_TYPE, label='Stubs')


def program_tier_row(parent, tier, tickets, claimed):
    is_pack = bool(tier.pack_id)
    name = SHOP_PACKS[tier.pack_id].name if is_pack else 'Stubs'
    row = mini_node(parent, 'reward', id=tier.pack_id, level=tier.level, name=name,
                    type_id=4 if is_pack else -1, tickets_required=tier.threshold, cur_val=tickets,
                    start_of_stage=0, no_sell=1, is_featured=0,
                    display_index=0 if is_pack else 1, label_index=1 if is_pack else 2,
                    is_claimed=int(claimed), status_index=int(claimed),
                    stage_level_9irt____=tier.level,  # script 0x86784c76
                    reward_status_aafaaa9agx_5=tier.threshold)  # script 0xbb1f070a
    program_item(row, tier)
    # The label template binds extra[0].amount even when it picks the pack name.
    program_extra(ET.SubElement(row, 'extras'), 'extra', tier.stubs)
    return row


def program_view(params):
    query = params_of(params)
    program_id = query.get('program_id', 0) if isinstance(query, dict) else None
    division = affinity_index(program_id, AFFINITY_PROGRAM_BASE, len(AFFINITY_DIVISIONS))
    if division is None:
        club = affinity_index(program_id, AFFINITY_LEGACY_PROGRAM_BASE, len(MLB_TEAMS))
        if club is not None:
            division = club // 5
    if division is not None:
        return affinity_view(division)
    available = str(program_id) in ('0', str(PROGRAM_ID))
    state = load_state() if available else {}
    profile = state.get('profile', {})
    tickets = program_tickets(profile)
    # Reconcile existing saves and never consume the pending delta before view_post_game.
    if available and grant_program_tiers(state, tickets):
        save_state(state)
        profile = state['profile']
    response = ET.Element('response')
    # Program_IsComplete (0xf0e522) uses max_program_tickets on detail.
    # total_program_tickets is the separate progress-list field (0x9c3f32).
    program = mini_node(response, 'program', max_program_tickets=PATH_TOTAL,
                        max_bonus_tickets=BONUS_STEP, has_unlimited_reward='False',
                        just_completed_program='False', **program_fields(profile, available=available))
    program_goal(program, min(tickets, PATH_TOTAL), available=available)
    rewards = ET.SubElement(program, 'rewards')
    if available:
        claimed = set(profile.get('program_tier_claims', ()))
        for tier in PATH_TIERS:
            program_tier_row(rewards, tier, min(tickets, PATH_TOTAL), tier.level in claimed)
    unlimited = ET.SubElement(program, 'unlimited_rewards')
    for _ in range(6):  # Indexed texture paths 0x360670–0x3608b0.
        mini_node(unlimited, 'unlimited_reward', texture_name='')
    return mini_xml(response)


def program_list(params):
    query = params_of(params)
    group = str(query.get('program_group_id', 10000)) if isinstance(query, dict) else ''
    if group == '10002':
        return affinity_list(range(len(AFFINITY_DIVISIONS)))
    club = affinity_index(group, AFFINITY_CLUB_GROUP_BASE, len(MLB_TEAMS))
    if club is not None:
        return affinity_list((club // 5,))
    # 10000 is the default catalogue. Unimplemented groups have countable empty lists.
    response = ET.Element('response')
    fields = program_fields(load_state().get('profile', {})) if group == '10000' else None
    mini_node(response, 'active_program', id=fields['id'] if fields else 0,
              name_short=fields['name_short'] if fields else '', texture_name='')
    programs = mini_node(response, 'programs', display_mode='list')
    if fields:
        program = mini_node(programs, 'program', **fields, featured_label_rtay_8i_='Tier Rewards')
        featured = ET.SubElement(program, 'featured_items')
        for pack_id in (510001, 510003):
            item = mini_node(featured, 'featured_item', **program_pack_fields(pack_id))
            ET.SubElement(item, 'item_exclusives')
    return mini_xml(response)


def add_bonus_xp(params):
    # Both callbacks immediately return (0xbfd550, 0xf0bc50). No delta or claim is read.
    return '<response><bonus_xp amount="0"/></response>'


def mission_slots(params):
    # 0xf0bc60 and 0xf07f80 clear DATA_M and bind response/missions, even for
    # zero daily slots. Without refresh_time neither creates a refresh timer.
    return '<response><missions/></response>'


# One Team Affinity track per division. Store club points as the source of truth.
AFFINITY_PROGRAM_BASE = 20100
AFFINITY_LEGACY_PROGRAM_BASE = 20000
AFFINITY_CLUB_GROUP_BASE = 10030
AFFINITY_DIVISIONS = ('AL East', 'AL Central', 'AL West', 'NL East', 'NL Central', 'NL West')
AFFINITY_SEASON = 5
AFFINITY_ART_BASE = 520100
AFFINITY_GAME_CREDITS = 1
AFFINITY_WIN_BONUS = 1
AFFINITY_TASK_CREDITS = 10
AFFINITY_STOP_TA = 5000
AFFINITY_BONUS_STUBS = 100
AFFINITY_CARD_FALLBACK_STUBS = 300
AFFINITY_LEGACY_CLUB_CAP = 60
AFFINITY_LEGACY_TRACK_TA = 200000  # Fixed migration scale after the old 300-point track.
# Stable claim key, kind, quantity, pack product or card club slot. None selects the division pack.
AFFINITY_TIERS = (
    ('pack', 'pack', 1, None),
    ('stubs', 'stubs', 150, None),
    ('xp', 'xp', 250, None),
    ('pack-04', 'pack', 1, 510001),
    ('stubs-05', 'stubs', 350, None),
    ('card', 'card', 1, 0),
    ('xp-07', 'xp', 500, None),
    ('pack-08', 'pack', 1, 510004),
    ('stubs-09', 'stubs', 500, None),
    ('xp-10', 'xp', 750, None),
    ('pack-11', 'pack', 1, 510002),
    ('stubs-12', 'stubs', 650, None),
    ('card', 'card', 1, 1),
    ('xp-14', 'xp', 1000, None),
    ('pack-15', 'pack', 1, 510005),
    ('stubs-16', 'stubs', 750, None),
    ('xp-17', 'xp', 1250, None),
    ('pack-18', 'pack', 1, 510001),
    ('stubs-19', 'stubs', 1100, None),
    ('card', 'card', 1, 2),
    ('xp-21', 'xp', 1500, None),
    ('pack-22', 'pack', 1, 510006),
    ('stubs-23', 'stubs', 1250, None),
    ('xp-24', 'xp', 1750, None),
    ('pack-25', 'pack', 1, 510002),
    ('stubs-26', 'stubs', 1500, None),
    ('pack-27', 'pack', 1, 510004),
    ('xp-28', 'xp', 2000, None),
    ('stubs-29', 'stubs', 1750, None),
    ('card', 'card', 1, 3),
    ('xp-31', 'xp', 2250, None),
    ('pack-32', 'pack', 1, 510005),
    ('stubs-33', 'stubs', 2000, None),
    ('xp-34', 'xp', 2500, None),
    ('pack-35', 'pack', 1, 510002),
    ('stubs-36', 'stubs', 2250, None),
    ('xp-37', 'xp', 3000, None),
    ('pack-38', 'pack', 1, 510006),
    ('stubs-39', 'stubs', 2500, None),
    ('card', 'card', 1, 4),
)
AFFINITY_DIVISION_CAP = len(AFFINITY_TIERS) * AFFINITY_STOP_TA
_affinity_lineup = None


def affinity_index(value, base, count):
    try:
        index = int(str(value)) - base
    except ValueError:
        return None
    return index if 0 <= index < count else None


def affinity_task_credits(state, club):
    return state.get('team_affinity', {}).get('task_credits', {}).get(str(club), 0)


def affinity_division_clubs(division):
    return range(division * 5, division * 5 + 5)


def affinity_division_points(state, division):
    clubs = affinity_division_clubs(division)
    old = state.get('team_affinity', {}).get('points', {})
    legacy = sum(min(old.get(str(club), 0), AFFINITY_LEGACY_CLUB_CAP) for club in clubs)
    old_cap = 5 * AFFINITY_LEGACY_CLUB_CAP
    # Preserve the fractional old progress, rounded up to the next whole TA point.
    migrated = (legacy * AFFINITY_LEGACY_TRACK_TA + old_cap - 1) // old_cap
    completed = sum(affinity_task_credits(state, club) // AFFINITY_TASK_CREDITS for club in clubs)
    return migrated + completed * AFFINITY_STOP_TA


def affinity_pack_id(division):
    return 510010 + division


def affinity_basic_card(club):
    # A deterministic Live Series reward. Prefer a Bronze/Silver-level card when possible.
    pool = [card for card in all_cards() if card['team_id'] == club and card.get('series') == 'Live'
            and card['roster_player_id'] > 0 and card['display_position'] in FIELD_NUMBER]
    return max(pool, key=lambda card: (card['ovr'] < 80, card['ovr'], -card['item_id']), default=None)


def affinity_art_name(division, season):
    division_key = AFFINITY_DIVISIONS[division].replace(' ', '').upper()
    return f'PRO_TA_{division_key}_{season:02d}'


@lru_cache(maxsize=2)
def affinity_tile_art(update_dir):
    """Register the six stock division tiles without creating files."""
    registry = {'entries': [], 'files': {}, 'names': set()}
    if update_dir is None:
        return registry
    directory = os.path.join(update_dir, 'CROSS', 'LIVE')
    for division in range(len(AFFINITY_DIVISIONS)):
        name = affinity_art_name(division, AFFINITY_SEASON)
        try:
            with open(os.path.join(directory, name + '.WEBP'), 'rb') as source:
                data = source.read(512 * 1024 + 1)
        except OSError:
            continue
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            if register_picture(registry, name, AFFINITY_ART_BASE + division, f'affinity-{division}', data):
                registry['names'].add(name)
    return registry


def affinity_fields(state, division):
    division_name = AFFINITY_DIVISIONS[division]
    points = affinity_division_points(state, division)
    reached = min(len(AFFINITY_TIERS), points // AFFINITY_STOP_TA)
    next_level = min(len(AFFINITY_TIERS), reached + 1)
    preferred = affinity_art_name(division, AFFINITY_SEASON)
    texture = preferred if preferred in affinity_tile_art(UPDATE_DIR)['names'] else affinity_art_name(division, 1)
    fields = program_fields({})
    fields.update(id=AFFINITY_PROGRAM_BASE + division, name=f'{division_name} - Season {AFFINITY_SEASON}',
                  name_short=division_name.upper(), subtitle=f'SEASON {AFFINITY_SEASON}',
                  description=f'Play with {division_name} players to earn Team Affinity points.',
                  program_group_id=10002, texture_name=texture,
                  tickets_earned=points, total_program_tickets=AFFINITY_DIVISION_CAP,
                  max_tickets=AFFINITY_DIVISION_CAP, max_program_tickets=AFFINITY_DIVISION_CAP,
                  current_level=max(1, reached), next_level=next_level,
                  xp_to_next_level=max(0, next_level * AFFINITY_STOP_TA - points),
                  percent_complete=min(100, points * 100 // AFFINITY_DIVISION_CAP),
                  is_complete=int(points >= AFFINITY_DIVISION_CAP), ui_type=2,
                  max_bonus_tickets=BONUS_STEP, has_unlimited_reward='False', just_completed_program='False',
                  xp_earned_apao34n_=points, reward_unlocks_aaaa0aaa_yvx=reached)
    return fields


def affinity_goals(program, state, division):
    stage = mini_node(ET.SubElement(program, 'stages'), 'stage', stage=0, is_unlocked=1)
    modes = 'Play vs. CPU, Mini Seasons, Conquest, and Showdown'
    group = mini_node(stage, 'goals', id=division + 1, name='Team Build Missions',
                      description='Repeat club tasks to earn Team Affinity points.',
                      type=2, type_tab=0, is_group=1, is_complete=0, tickets=0, mode_name=modes)
    for club in affinity_division_clubs(division):
        club_name = MLB_TEAMS[club][2]
        credits = affinity_task_credits(state, club) % AFFINITY_TASK_CREDITS
        description = (f'Finish a game with a {club_name} player in the lineup or rotation. '
                       f'A finish gives {AFFINITY_GAME_CREDITS} credit and a win adds {AFFINITY_WIN_BONUS}. '
                       'Only successful Showdown moments count. '
                       f'After the track, every {AFFINITY_STOP_TA} TA pays {AFFINITY_BONUS_STUBS} Stubs.')
        goal = mini_node(group, 'goal', id=club + 1, type=2, type_tab=0, mode_name=modes,
                         name=f'REPEATABLE: {AFFINITY_TASK_CREDITS} game credits with {club_name}',
                         description=description, is_complete=0, tickets=AFFINITY_STOP_TA,
                         progress_current=credits, progress_max=AFFINITY_TASK_CREDITS,
                         mission_criteria_id=0, preview_count=0, stat='GAME CREDITS')
        ET.SubElement(goal, 'items')


def affinity_reward_key(kind, club):
    return f'card:{club}' if kind == 'card' else kind


def affinity_tiers(division):
    for level, (key, kind, amount, target) in enumerate(AFFINITY_TIERS, 1):
        if kind == 'card':
            target = division * 5 + target
            key = affinity_reward_key(kind, target)
        elif kind == 'pack' and target is None:
            target = affinity_pack_id(division)
        yield level, level * AFFINITY_STOP_TA, key, kind, amount, target


def affinity_claims(affinity, division):
    claims = set(affinity.get('division_claims', {}).get(str(division), ()))
    old_claims = affinity.get('claims', {})
    for club in affinity_division_clubs(division):
        for tier in old_claims.get(str(club), ()):
            # Legacy tiers 1-3 were the same wallet/XP/division pack per club.
            if tier in (1, 2, 3):
                claims.add(('stubs', 'xp', 'pack')[tier - 1])
            elif tier == 4:
                claims.add(affinity_reward_key('card', club))
    return claims


def affinity_view(division):
    state = load_state()
    fields = affinity_fields(state, division)
    points = fields['tickets_earned']
    if affinity_grant_tiers(state, division, points):
        save_state(state)
    claimed = affinity_claims(state.get('team_affinity', {}), division)
    response = ET.Element('response')
    program = mini_node(response, 'program', **fields)
    affinity_goals(program, state, division)
    rewards = ET.SubElement(program, 'rewards')
    for level, threshold, key, kind, amount, target in affinity_tiers(division):
        pack_id = target if kind == 'pack' else 0
        card = affinity_basic_card(target) if kind == 'card' else None
        stubs = amount if kind == 'stubs' else 0
        if kind == 'card' and card is None:
            stubs = AFFINITY_CARD_FALLBACK_STUBS
        paid = key in claimed
        row = program_tier_row(rewards, PathTier(level, threshold, stubs, pack_id), points, paid)
        # A migrated payout can already be paid even when its new stop is ahead.
        row.set('status_index', str(int(points >= threshold)))
        if card is not None:
            row.remove(row.find('item'))
            item = ET.fromstring(shop_revealed_card(card, amount, is_new=False))
            item.set('item_label_aaadeb49z9__', card['name'])
            ET.SubElement(item, 'item_exclusives')
            row.insert(0, item)
            for key, value in dict(id=card['item_id'], type_id=0, name=card['name'],
                                   display_index=0, label_index=1).items():
                row.set(key, str(value))
        elif kind == 'xp':
            row.set('name', 'XP')
            row.find('item').set('item_label_aaadeb49z9__', 'XP')
            extra = row.find('extras/extra')
            extra.attrib.update(amount=str(amount), type_index='1', label='XP')
        elif kind == 'pack':
            row.find('item').set('quantity', str(amount))
    unlimited = ET.SubElement(program, 'unlimited_rewards')
    for _ in range(6):
        mini_node(unlimited, 'unlimited_reward', texture_name='')
    return mini_xml(response)


def affinity_list(divisions):
    state = load_state()
    response = ET.Element('response')
    rows = [(division, affinity_fields(state, division)) for division in divisions]
    first = rows[0][1]
    mini_node(response, 'active_program', id=first['id'], name_short=first['name_short'],
              texture_name=first['texture_name'])
    # FRONTEND 0x7410a8 BranchValue selects TeamProgramList with decimal mode 0.
    # The literal "list" is a separate argument to the tile selection command.
    programs = mini_node(response, 'programs', display_mode=0)
    # ol_ProgramGroups reads only row identity, the tile labels, art and progress.
    # Keep the Program detail attributes on program.view.
    list_fields = ('id', 'name', 'name_short', 'subtitle', 'texture_name', 'percent_complete',
                   'is_complete', 'tickets_earned', 'max_tickets')
    for division, fields in rows:
        program = mini_node(programs, 'program', **{name: fields[name] for name in list_fields},
                            featured_label_rtay_8i_='Division Rewards')
        featured = ET.SubElement(program, 'featured_items')
        for pack_id in (affinity_pack_id(division), 510001):
            item = mini_node(featured, 'featured_item', **program_pack_fields(pack_id))
            ET.SubElement(item, 'item_exclusives')
    return mini_xml(response)


def affinity_home_panel(state):
    panel = ET.Element('TeamAffinity')
    for division, name in enumerate(AFFINITY_DIVISIONS):
        points = min(affinity_division_points(state, division), AFFINITY_DIVISION_CAP)
        mini_node(panel, 'program', name_short=name, tickets_earned=points, max_tickets=AFFINITY_DIVISION_CAP,
                  affinity_percent_aaaaaaahpal_=points * 100 // AFFINITY_DIVISION_CAP)  # script 0xea787a3a
    return ET.tostring(panel, encoding='unicode')


def affinity_card_clubs(cards):
    return sorted({card['team_id'] for card in cards if card and 0 <= card['team_id'] < len(MLB_TEAMS)})


def affinity_squad_clubs(squad):
    lineup, dh, _, rotation, _ = squad
    return affinity_card_clubs(lineup + dh + rotation)


def affinity_upload_clubs(state, record):
    global _affinity_lineup
    ids = [record['ids'][side] for side in SUMMARY_SIDES]
    setup, _affinity_lineup = _affinity_lineup, None
    if setup and setup[0] == ids:
        return setup[1]
    return affinity_squad_clubs(current_squad(state.get('squad') or default_squad()))


def affinity_grant_tiers(state, division, points):
    affinity = state.get('team_affinity', {})
    key = str(division)
    claimed = affinity_claims(affinity, division)
    for _, threshold, reward, kind, amount, target in affinity_tiers(division):
        if points < threshold or reward in claimed:
            continue
        profile = state.setdefault('profile', {})
        if kind == 'pack':
            shop = state.setdefault('pack_shop', {})
            for _ in range(amount):
                grant_owned_pack(shop, target)
        elif kind == 'xp':
            profile['xp'] = min(0xffffffff, profile.get('xp', 0) + amount)
        elif kind == 'card':
            card = affinity_basic_card(target)
            if card is None:
                profile['stubs'] = profile.get('stubs', 0) + AFFINITY_CARD_FALLBACK_STUBS
            else:
                gains = state.setdefault('pack_shop', {}).setdefault('gained_cards', {})
                gains[card['uuid']] = gains.get(card['uuid'], 0) + amount
        else:
            profile['stubs'] = profile.get('stubs', 0) + amount
        claimed.add(reward)
    changed = claimed != set(affinity.get('division_claims', {}).get(key, ()))
    if changed:
        state.setdefault('team_affinity', {}).setdefault('division_claims', {})[key] = sorted(claimed)
    cycles = max(0, (points - AFFINITY_DIVISION_CAP) // AFFINITY_STOP_TA)
    paid_cycles = affinity.get('bonus_cycles', {}).get(key, 0)
    if cycles > paid_cycles:
        profile = state.setdefault('profile', {})
        profile['stubs'] = profile.get('stubs', 0) + (cycles - paid_cycles) * AFFINITY_BONUS_STUBS
        state.setdefault('team_affinity', {}).setdefault('bonus_cycles', {})[key] = cycles
        changed = True
    return changed


def affinity_record_game(state, receipt, clubs, won):
    affinity = state.setdefault('team_affinity', {})
    receipts = affinity.setdefault('game_receipts', {})
    if receipt in receipts:
        return
    clubs = sorted(set(clubs))
    receipts[receipt] = clubs
    credits = affinity.setdefault('task_credits', {})
    gained = AFFINITY_GAME_CREDITS + (AFFINITY_WIN_BONUS if won else 0)
    for club in clubs:
        key = str(club)
        credits[key] = credits.get(key, 0) + gained
    for division in sorted({club // 5 for club in clubs}):
        affinity_grant_tiers(state, division, affinity_division_points(state, division))


def affinity_cpu_upload(state, game_data):
    try:
        record = decode_summary_record(game_data)
        if (record['game_type'], record['sub_mode'], record['outcome']) != ('ARENA', 3, 'FINISHED'):
            return
        if not record['uuid'] or len(record['uuid']) > 128:
            return
        teams = summary_teams(record, summary_local_team(state))
    except (ValueError, struct.error):
        return
    local, = (side for side, team in teams.items() if team['owner_id'] == ACCOUNT_ID)
    away, home = (record['totals'][side]['runs'] for side in ('away', 'home'))
    if away == home:
        return
    receipt = hashlib.sha256(game_data).hexdigest()
    if receipt in state.get('team_affinity', {}).get('game_receipts', {}):
        return
    affinity_record_game(state, receipt, affinity_upload_clubs(state, record), (away > home) == (local == 'away'))


def up_levels(xp):
    # GUESS: level_prog is a percent and cur_level_xp is XP into this level (0xaa1e30).
    level, into = divmod(xp, UP_XP_PER_LEVEL)
    return level, into * 100 // UP_XP_PER_LEVEL, into, UP_XP_PER_LEVEL


def xp_grant_answer(body):
    # 0x16c6ac0: count, 18 bytes per entry, and two u64s at the end (zero and send time).
    r = Reader(body)
    n = r.u32()
    if r.left() != n * 18 + 16:
        raise ValueError(f'XP grant count {n} does not match body length {len(body)}')
    state = load_state()
    p = state.setdefault('profile', {})
    mark = p.get('grant_mark', {})
    mark_t, mark_counts = mark.get('time', -1), mark.get('counts', {})
    newest_t, newest_counts = mark_t, Counter(mark_counts)
    seen_at_mark = Counter()
    added, counted, unknown = 0, 0, 0
    diagnostic = SHOWDOWN_BLUE_TEST and SHOWDOWN_BLUE_GAME.get()
    # The queue is appended in time order. There is no unique grant ID. Keep the highest time
    # and the count of each wire record at that time; distinct records can share a second.
    for _ in range(n):
        event = r.take(18)
        idx, t = struct.unpack_from('<IQ', event)
        if t < mark_t:
            continue
        if t > newest_t:
            newest_t, newest_counts = t, Counter()
        identity = event.hex()
        if t == mark_t:
            seen_at_mark[identity] += 1
            if seen_at_mark[identity] <= mark_counts.get(identity, 0):
                continue
        if t == newest_t:
            newest_counts[identity] += 1
        if idx < len(XP_GRANT):
            if not diagnostic:
                added += XP_GRANT[idx]
        else:
            unknown += 1
        counted += 1

    if n:
        p['grant_mark'] = {'time': newest_t, 'counts': dict(newest_counts)}
    p['xp'] = max(0, min(0xffffffff, p.get('xp', 0) + added))
    p['pending_xp'] = p.get('pending_xp', 0) + added
    level, prog, into, need = up_levels(p['xp'])
    # Reader 0x16c72c0, consumer 0xb14ad0: accepted clears the queue, cap_seconds 0 means no cap.
    w = (Writer().u32(p['xp']).u32(level).u32(prog).u32(p.get('prestige', 0)).u32(into).u32(need)
         .u8(1).u64(0))
    answer = frame('olUniversalProfileXPGrantEventResponse', w)
    save_state(state)
    note = 'first answer/backlog: ' if n and not mark else ''
    note += f'{n} grants, {counted} new, {added:+d} XP, total {p["xp"]}, level {level + 1}'
    if diagnostic:
        note += ', Blue Test diagnostic'
    if unknown:
        note += f', {unknown} unknown grant indices (0 XP)'
    return answer, note


def game_create(params, game_data=None):
    # 0x9e8460: keep the empty completion reply for mlb_game.create.
    state = load_state()
    p = state.setdefault('profile', {})
    p['games'] = p.get('games', 0) + 1
    p['stubs'] = p.get('stubs', 0) + STUBS_PER_GAME
    if game_data is not None:
        affinity_cpu_upload(state, game_data)
        remember_summary_upload(state, game_data)
    save_state(state)
    return '<response/>'


def view_post_game(params):
    # 0xbfbd30 reads every threshold and the newly crossed tiers. Generic rewards
    # stay empty so only the dedicated tier widgets see the Stubs and packs.
    state = load_state()
    p = state.setdefault('profile', {})
    earned = max(0, p.get('pending_xp', 0))
    before = program_tickets(p)
    after = before + earned
    p['program_tickets'], p['last_game_tickets'], p['pending_xp'] = after, earned, 0
    grant_program_tiers(state, after)
    response = ET.Element('response')
    post = mini_node(response, 'post_game', earned_tickets=earned)
    ET.SubElement(post, 'rewards')
    program = mini_node(post, 'program', ticket_earned=min(after, PATH_TOTAL), total_tickets=PATH_TOTAL,
                        **program_fields(p))
    for tier in PATH_TIERS:
        mini_node(program, 'reward_data', level=tier.level, tickets_required=tier.threshold)
    crossed = [tier for tier in PATH_TIERS if before < tier.threshold <= after]
    for tier in crossed[:8]:  # 0xbfcfff and 0xbfd238 populate only eight per-row display selectors.
        row = mini_node(program, 'xp_reward', level=tier.level, tickets_required=tier.threshold)
        extras = ET.SubElement(row, 'extra_reward')
        if tier.pack_id:
            program_item(ET.SubElement(row, 'reward'), tier)
        else:
            program_extra(extras, 'reward', tier.stubs)
    reply = mini_xml(response)
    save_state(state)
    return reply


def all_progress(params):
    # 0x9c3ae0 / 0xbfde90: stable program, stage and goal shape; the total is a divisor.
    fields = program_fields(load_state().get('profile', {}))
    response = ET.Element('response')
    program = mini_node(response, 'program', **fields)
    program_goal(program, fields['tickets_earned'])
    return mini_xml(response)


def update_currency(params):
    # 0xaa13d0: the server's wallet values overwrite the sent values. GUESS: currency is stubs.
    stubs = load_state().get('profile', {}).get('stubs', 0)
    return ('<response><wallet currency="%d" currency_earned="%d" currency_platform="0" currency_other="0" '
            'faction_tickets="0" client_currency_wallet_purchased="0" client_currency_wallet_earned="%d" '
            'client_faction_tickets="0" stub_cap_reached="0"/></response>' % (stubs, stubs, stubs))


def params_of(params):
    try:
        return json.loads(params or '{}')
    except ValueError:
        return {}


def arena_team_create(params):
    # Reply read by 0xdf1270: child arena_team with attribute id.
    state = load_state()
    team = params_of(params)
    team.pop('arena_team_id', None)
    team['id'] = DD_TEAM_ID
    state['arena_team'] = team
    save_state(state)
    return '<response><arena_team id="%d"/></response>' % DD_TEAM_ID


def arena_team_update(params, custom=False):
    # The game's uniform editor (arena_team.update_uniform, arena_team.update_uniform_id) and any other
    # arena_team.update_* request: keep whatever it sends with the team, so the next arena_team.view can
    # give it back. SEEN in run 85: arena_team.update_uniform sends arena_team_id, home_uniform_id,
    # away_uniform_id, uniform_favorite_colors and the twenty lists uniform_<part>_home / _away
    # (cap, jersey, sleeve, namenum, pants, helmet, sockshoes, batting_gloves, jacket, catcher_gear),
    # each "v,v,v," with a trailing comma: the very names the team reply readers 0x9a7590 / 0x9b07b0 want.
    state = load_state()
    team = state.setdefault('arena_team', {'id': DD_TEAM_ID})
    query = params_of(params)
    old_ids = (LEGACY_DD_TEAM_ID, str(LEGACY_DD_TEAM_ID))
    for k, v in query.items():
        if k in ('arena_team_id', 'id'):
            continue
        if k in ('home_uniform_id', 'away_uniform_id'):
            if v in (-1, '-1'):
                continue    # a custom uniform save only selects the side it is for
            # Only a custom editor save means "my kit" (0xbf04d7). A kit picker can
            # explicitly send Boston home, even from a client with the old team cached.
            if custom and query.get('arena_team_id') in old_ids and v in old_ids:
                v = DD_TEAM_ID
        team[k] = ','.join(str(x) for x in v) if isinstance(v, (list, tuple)) else v
    save_state(state)
    # arena_team.update_uniform_id's reply is read by 0x9b4140: <uniforms home_uniform_id= away_uniform_id=/>
    team_id = team.get('id', DD_TEAM_ID)
    return ('<response><arena_team id="%s"/><uniforms home_uniform_id="%s" away_uniform_id="%s"/></response>'
            % (team_id, team.get('home_uniform_id', team_id), team.get('away_uniform_id', team_id)))


def arena_state(params):
    # Sent by the front-end scripts (UI\\FRONTEND\\FRONTEND.PS4), which keep the reply as TEAMSTATE
    # and read [ONLINE.TEAMSTATE.CONTAINER.HASH.<NAME>] for HAS_TEAM, NEEDS_DRAFT, SEASON_LOCKED,
    # NEW_SEASON, HAS_REWARDS, ONRAMP_DONE and show_content_szn_intro. Whether CONTAINER and HASH
    # are elements of the reply or the script's words for "this node's attributes" is not known,
    # nor the letter case of the names or how true is written, so the reply carries every reading:
    # the flags as attributes in both cases on <response>, on <container><hash/> and on <hash/>.
    has_team = 1 if load_state().get('arena_team') else 0
    flags = {'HAS_TEAM': has_team, 'NEEDS_DRAFT': 0, 'SEASON_LOCKED': 0, 'NEW_SEASON': 0, 'HAS_REWARDS': 0,
             'ONRAMP_DONE': has_team, 'show_content_szn_intro': 0}
    attrs = ' '.join('%s="%d"' % (k, v) for name, v in flags.items() for k in sorted({name.upper(), name.lower()}))
    return '<response %s><container %s><hash %s/></container><hash %s/></response>' % (attrs, attrs, attrs, attrs)


# ---------------------------------------------------------------- cards and the squad
# A card of the site catalogue (snapshots\\theshow23-site, local) is joined to a player of the
# roster we serve by name; the roster gives the player number, the catalogue the card's ratings.
# The account's collection is every card that has a roster player ("all") or the user's real
# collection from the inventory snapshot ("mine"). FIRST ATTEMPT, NOT YET CONFIRMED BY THE GAME:
# the meaning of team_id, roster_player_id, position and new_position in a card entry is read
# from the handlers and the front-end script (see HANDOFF) but has not been seen working.
COLLECTION = 'all'
CARD_SOURCE = 'disc' if LAYOUT.install else 'site'
RARITY_ID = {'Common': 0, 'Bronze': 1, 'Silver': 2, 'Gold': 3, 'Diamond': 4}
FIELD_NUMBER = {'SP': 0, 'RP': 0, 'CP': 0, 'C': 1, '1B': 2, '2B': 3, '3B': 4, 'SS': 5, 'LF': 6, 'CF': 7, 'RF': 8, 'DH': 9}
RATINGS = ['ovr', 'stamina', 'pitching_clutch', 'hits_per_bf', 'k_per_bf', 'bb_per_bf', 'hr_per_bf', 'pitch_velocity',
           'pitch_control', 'pitch_movement', 'contact_left', 'contact_right', 'power_left', 'power_right',
           'plate_vision', 'plate_discipline', 'batting_clutch', 'bunting_ability', 'drag_bunting_ability',
           'hitting_durability', 'fielding_durability', 'fielding_ability', 'arm_strength', 'arm_accuracy',
           'reaction_time', 'blocking', 'speed', 'baserunning_ability', 'baserunning_aggression']
_cards = None
_all_cards = None


def roster_tables():
    """(players by name, team index by short name) of the roster source in use."""
    folder = ROSTER_DIRS[ROSTER_SOURCE]
    d = open(os.path.join(folder, 'PLAYERS.BIN'), 'rb').read()
    data = 8 + struct.unpack_from('<I', d, 4)[0]
    count, = struct.unpack_from('<I', d, data + 22)
    rec0 = data + 30
    for shift in range(-8, 9):
        r = d[rec0 + shift:rec0 + shift + 299]
        if r[36:37].isalpha() and r[52:53].isalpha() and r[51] == 0:
            rec0 += shift
            break
    players = {}
    for i in range(count):
        r = d[rec0 + i * 299:rec0 + (i + 1) * 299]
        if len(r) < 299:
            break
        first = r[36:52].split(b'\0')[0].decode('latin-1')
        last = r[52:68].split(b'\0')[0].decode('latin-1')
        if first or last:
            players.setdefault((first + ' ' + last).strip(), {'index': i, 'player_id': struct.unpack_from('<H', r, 32)[0],
                                                               'online_id': struct.unpack_from('<I', r, 80)[0]})
    t = open(os.path.join(folder, 'ROSTERS.BIN'), 'rb').read()
    tdata = 8 + struct.unpack_from('<I', t, 4)[0]
    tcount, = struct.unpack_from('<I', t, tdata + 22)
    teams = {}
    for i in range(tcount):
        r = t[tdata + 26 + i * 95:tdata + 26 + (i + 1) * 95]
        short = r[40:44].split(b'\0')[0].decode('latin-1')
        if short and short.isalnum():
            teams.setdefault(short, struct.unpack_from('<I', r, 46)[0])
    return players, teams


DISC_ROSTER_FILES = ('disc-cards.json', 'roster-records.json', 'appearance-fields.json', 'pitch-table.json')
_disc_decoder = None


def disc_decoder():
    global _disc_decoder
    if _disc_decoder is None:
        import importlib.util
        path = os.path.join(HERE, 'roster_decode.py')
        spec = importlib.util.spec_from_file_location('roster_decode', path)
        module = importlib.util.module_from_spec(spec)
        # The release program folder can be read-only.
        with open(path, 'rb') as file:
            exec(compile(file.read(), path, 'exec'), module.__dict__)
        _disc_decoder = module
    return _disc_decoder


def derived_roster_path(name):
    folder = os.path.join(WORK, 'disc-roster') if CARD_SOURCE == 'disc' and not LAYOUT.install else WORK
    return os.path.join(folder, name)


def prepare_disc_roster(log=None):
    from pathlib import Path
    stamp_path = Path(derived_roster_path('disc-roster-stamp.json'))
    stamp = {'schema': 1, 'base': LAYOUT.base, 'update': LAYOUT.update,
             'executables': list(LAYOUT.executables), 'roster_source': ROSTER_SOURCE}
    try:
        with stamp_path.open(encoding='utf-8') as file:
            current = json.load(file)
    except (OSError, ValueError):
        current = None
    if current == stamp and all(os.path.isfile(derived_roster_path(name)) for name in DISC_ROSTER_FILES):
        return
    decoder = disc_decoder()
    datasets = decoder.server_roster(Path(ROSTER_DIRS[ROSTER_SOURCE]), [club[0] for club in MLB_TEAMS])
    for name in DISC_ROSTER_FILES:
        decoder.write_json(Path(derived_roster_path(name)), datasets[name])
    decoder.write_json(stamp_path, stamp)
    if log is not None:
        log(f'disc roster: built {len(datasets["disc-cards.json"])} cards from {ROSTER_SOURCE}')


def choose_disc_starter(pool):
    """Pick low-rated Commons and assign them to a complete starter squad."""
    commons = sorted((c for c in pool if c['rarity'] == 'Common'),
                     key=lambda c: (c['ovr'], c['roster_player_id']))
    chosen = []
    names = set()

    def add(card):
        names.add(card['name'])
        chosen.append(card['uuid'])
        return card

    def take(position, count):
        available = [c for c in commons if c['display_position'] == position]
        selected = []
        for card in available:
            if card['name'] not in names:
                selected.append(add(card))
                if len(selected) == count:
                    return selected
        raise ValueError(f'disc starter needs {count} Common {position} players')

    fields = ('C', '1B', '2B', '3B', 'SS', 'LF', 'CF', 'RF')
    lineup = [take(position, 1)[0] for position in fields]
    bench = take('C', 1)
    starters = take('SP', 5)
    relief = take('CP', 1) + take('RP', 7)
    needed = set(fields) - {'C'}

    def positions(card):
        return {card['display_position'], *card['display_secondary_positions'].split(',')}

    for _ in range(3):
        available = [c for c in commons if c['is_hitter'] and c['name'] not in names]
        if not available:
            raise ValueError('disc starter needs three flexible Common bench hitters')
        card = max(available, key=lambda c: len(needed & positions(c)))
        needed.difference_update(positions(card))
        bench.append(add(card))
    if needed:
        raise ValueError('disc starter lacks Common backups for ' + ', '.join(sorted(needed)))
    dh = next((c for c in commons if c['is_hitter'] and c['name'] not in names), None)
    if dh is None:
        raise ValueError('disc starter needs a Common designated hitter')
    lineup.append(add(dh))
    lineup.sort(key=lambda c: -(c['contact_right'] + c['power_right']))
    squad = {'lineup': [[c['item_id'], 9 if c is dh else FIELD_NUMBER[c['display_position']]] for c in lineup],
             'bench': [c['item_id'] for c in bench],
             'starters': [c['item_id'] for c in starters], 'relief': [c['item_id'] for c in relief]}
    return chosen, squad


def grant_starting_stubs(state):
    """Give a new player their opening Stubs once, with the starter squad."""
    profile = state.setdefault('profile', {})
    if not profile.get('starting_stubs_granted'):
        profile['stubs'] = profile.get('stubs', 0) + STARTING_STUBS
        profile['starting_stubs_granted'] = True


def load_disc_cards():
    prepare_disc_roster()
    with open(derived_roster_path('disc-cards.json'), encoding='utf-8') as file:
        pool = sorted(json.load(file), key=lambda c: (-c['ovr'], c['name'], c['roster_player_id']))
    starter = load_state().get('disc_starter')
    if starter is None:
        starter, _ = choose_disc_starter(pool)
    starter = set(starter)
    return [c for c in pool if c['uuid'] in starter], pool


def base_cards():
    """The read-only snapshot collection, best overall first."""
    global _cards, _all_cards
    if _cards is not None:
        return _cards
    if CARD_SOURCE == 'disc':
        _cards, _all_cards = load_disc_cards()
        return _cards
    _cards, _all_cards = [], []
    snap = CATALOGUE_DIR
    days = sorted(os.listdir(snap)) if os.path.isdir(snap) else []
    if not days:
        return _cards
    catalogue = sorted(json.load(open(os.path.join(snap, days[-1], 'items-mlb_card.json'), encoding='utf-8')),
                       key=lambda c: c['uuid'])
    owned = None
    inv = os.path.join(snap, days[-1], 'inventory-owned.json')
    if COLLECTION == 'mine' and os.path.exists(inv):
        owned = {r['uuid']: int(r['qty']) for r in json.load(open(inv, encoding='utf-8'))['items']['mlb_card']}
    players, teams = roster_tables()
    for n, c in enumerate(catalogue):
        player = players.get(c['name'].strip())
        if not player:
            continue
        card = dict(c, item_id=100000 + n, roster_player_id=player['player_id'],
                    # the site writes WAS where the roster has WSH; free agents (FA) stay -1,
                    # which is also the site's own number for them
                    team_id=teams.get({'WAS': 'WSH'}.get(c.get('team_short_name', ''), c.get('team_short_name', '')), -1),
                    quantity=(owned or {}).get(c['uuid'], 1))
        _all_cards.append(card)
        if owned is None or c['uuid'] in owned:
            _cards.append(card)
    _cards.sort(key=lambda c: (-c['ovr'], c['name']))
    _all_cards.sort(key=lambda c: (-c['ovr'], c['name']))
    return _cards


def cards(state=None):
    """Apply local gains and exchange spends by UUID without modifying the snapshot cache."""
    owned = base_cards()
    if state is None:
        state = load_state()
    gains = state.get('pack_shop', {}).get('gained_cards', {})
    spent = state.get('exchanges', {}).get('spent_cards', {})
    if not gains and not spent:
        return owned
    owned_by_uuid = {c['uuid']: c for c in owned}
    merged = []
    for c in _all_cards:
        card = owned_by_uuid.get(c['uuid'])
        delta = gains.get(c['uuid'], 0) - spent.get(c['uuid'], 0)
        if delta:
            quantity = (card['quantity'] if card else 0) + delta
            if quantity > 0:
                merged.append(dict(c, quantity=quantity))
        elif card is not None:
            merged.append(card)
    return merged


def all_cards():
    base_cards()
    return _all_cards


ACTIONSHOT_DIR = os.path.join(LAYOUT.base, 'PS4', 'UI', 'ART', 'ACTIONSHOTS')
# Programme pictures on the disc are <prefix>_<TEAM>_[n_]LAST_FIRST (GUESS from the badge names)
SERIES_SHOT = {'All-Star': 'AS', 'Rookie': 'RC', 'Veteran': 'V', 'Breakout': 'B', '2nd Half Heroes': '2H', 'Postseason': 'P'}
SHOT_TEAM = {'WAS': 'WSH', 'CHW': 'CWS'}
_actionshots = None


def _shot_part(text):
    # what the game does to a roster name for a file name: drop ' , - . ` and spaces, upper case (accents: GUESS)
    import unicodedata
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return ''.join(ch for ch in text if ch not in " '.,-`").upper()


def actionshot_handle(c):
    """The disc picture for a card: its programme picture, else LAST_FIRST, else the generic one.

    The card template draws the picture with ONLINE.UTILS.PLAYER_TEXTURE[card] (case 7 of 0xa1a970): it wants
    BOTH actionshot_handle and actionshot_id on the card element and returns no texture without them (the blank
    pictures of runs 79 to 89). With them it looks in the downloaded-texture cache (empty here) and then loads
    <handle>.TEX from the disc's PS4\\UI\\ART\\ACTIONSHOTS (0xff3540, 0x8ce4d0; 20 slots keyed by actionshot_id;
    a missing file gives ActionGeneric). Study: reports\\online\\proposals\\card-images.md."""
    global _actionshots
    if _actionshots is None:
        try:
            _actionshots = {f[:-4].upper() for f in os.listdir(ACTIONSHOT_DIR) if f.upper().endswith('.TEX')}
        except OSError:
            _actionshots = set()
    first, _, last = c['name'].strip().partition(' ')
    base = _shot_part(last) + '_' + _shot_part(first)
    prefix = SERIES_SHOT.get(c.get('series'))
    if prefix:
        team = SHOT_TEAM.get(c.get('team_short_name', ''), c.get('team_short_name', ''))
        for cand in ['%s_%s_%s' % (prefix, team, base)] + ['%s_%s_%d_%s' % (prefix, team, n, base) for n in range(1, 6)]:
            if cand in _actionshots:
                return cand
    if base in _actionshots:
        return base
    head, _, tail = c['name'].strip().rpartition(' ')          # "Michael A. Taylor" is TAYLOR_MICHAELA on the disc
    alt = _shot_part(tail) + '_' + _shot_part(head)
    # Players the disc names differently (CASTILLO_LUISM ...) are in PS4\CHARACTERS\ASSETNAMES.CSV: for those the
    # game itself swaps the handle for its asset name (0xff3540), whatever is sent here.
    return alt if alt in _actionshots else 'ACTIONGENERIC'


def card_attrs(c, **extra):
    first, _, last = c['name'].strip().partition(' ')
    a = {'id': c['item_id'], 'item_id': c['item_id'], 'uuid': c['uuid'], 'team_id': c['team_id'],
         'roster_player_id': c['roster_player_id'], 'rarity_id': RARITY_ID.get(c['rarity'], 0),
         'rank': c['ovr'], 'original_rank': c['ovr'],          # the card reader 0x9c0900 calls the overall "rank"
         'first_name': first, 'last_name': last, 'name': c['name'].strip(), 'display_position': c['display_position'],
         'series': c.get('series', ''), 'quantity': c['quantity']}
    for k in RATINGS:
        a[k] = c[k] if c.get(k) is not None else 0     # always present: the squad screen subtracts them
    # the card picture: the update package's or the website's own art (card_art()), else the disc's action shot
    a['actionshot_handle'], a['actionshot_id'] = player_art()['override'].get(c['item_id'], (actionshot_handle(c), c['item_id']))
    a.update(extra)
    esc = lambda v: str(v).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')
    return ' '.join('%s="%s"' % (k, esc(v)) for k, v in a.items())


def pick_squad(pool=None):
    """Best card per fielding position, then bench, five starters and eight relievers."""
    pool = cards() if pool is None else pool
    used = set()

    def take(test, count=1):
        out = []
        for c in pool:
            if len(out) == count:
                break
            if c['name'] not in used and test(c):       # one card per person in a squad
                used.add(c['name'])
                out.append(c)
        return out
    lineup = []
    for pos in ('C', '1B', '2B', '3B', 'SS', 'LF', 'CF', 'RF'):
        lineup += take(lambda c, pos=pos: c['display_position'] == pos)
    dh = take(lambda c: c.get('is_hitter'))
    starters = take(lambda c: c['display_position'] == 'SP', 5)
    relief = take(lambda c: c['display_position'] in ('RP', 'CP'), 8)
    bench = take(lambda c: c.get('is_hitter'), 4)      # 9 + 4 + 5 + 8 = 26 players
    return lineup, dh, bench, starters, relief


# THE SAVED SQUAD (state.json "squad"): four groups named as the game names them in its substitution
# requests (table at 0x243e588: starters, lineup, bench, relief). A lineup place is [card id, position
# code]; the other groups are card ids. Made once from pick_squad(), then changed by
# arena_squad.assign_player {item_mlb_player_id, slot, type}, arena_squad.swap_player {slot_from,
# slot_to, type1, type2} and arena_squad.swap_position {slot_from, slot_to}.
# GUESS, not yet seen: slot is the place's index from 0 within its group, in the order we list it.
GROUPS = ('starters', 'lineup', 'bench', 'relief')


def card_by_id(item_id):
    for c in all_cards():
        if c['item_id'] == item_id:
            return c
    return None


def squad_state():
    state = load_state()
    sq = state.get('squad')
    if not sq:
        sq = default_squad()
        if sq['lineup']:
            state['squad'] = sq
            save_state(state)
    return sq


def default_squad():
    lineup, dh, bench, starters, relief = pick_squad()
    order = sorted(lineup + dh, key=lambda c: -(c.get('contact_right', 0) + c.get('power_right', 0)))
    return {'lineup': [[c['item_id'], 9 if c in dh else FIELD_NUMBER.get(c['display_position'], 0)] for c in order],
            'bench': [c['item_id'] for c in bench], 'starters': [c['item_id'] for c in starters],
            'relief': [c['item_id'] for c in relief]}


def current_squad(sq=None):
    """(lineup, dh, bench, starters, relief) as card records; lineup cards carry lineup_pos and come in batting order."""
    if sq is None:
        sq = squad_state()
    lineup, dh = [], []
    for item_id, pos in sq.get('lineup', []):
        c = card_by_id(item_id)
        if c:
            (dh if pos == 9 else lineup).append(dict(c, lineup_pos=pos, lineup_ord=len(lineup) + len(dh)))
    cards_of = lambda ids: [c for c in (card_by_id(i) for i in ids) if c]
    return lineup, dh, cards_of(sq.get('bench', [])), cards_of(sq.get('starters', [])), cards_of(sq.get('relief', []))


def squad_ids(sq):
    return [e[0] for e in sq.get('lineup', [])] + sq.get('bench', []) + sq.get('starters', []) + sq.get('relief', [])


def _place(sq, group, slot):
    places = sq.get(group, [])
    return places if 0 <= slot < len(places) else None


def assign_squad_slot(sq, q):
    group = q.get('type', 'lineup')
    try:
        slot, new_id = int(q.get('slot', -1)), int(q.get('item_mlb_player_id', -1))
    except (TypeError, ValueError):
        slot, new_id = -1, -1
    places = _place(sq, group, slot)
    if places is not None and card_by_id(new_id):
        old = places[slot][0] if group == 'lineup' else places[slot]
        # a card that already sits elsewhere in the squad changes places with the one it replaces
        for g in GROUPS:
            for i, e in enumerate(sq.get(g, [])):
                if (e[0] if g == 'lineup' else e) == new_id:
                    if g == 'lineup':
                        e[0] = old
                    else:
                        sq[g][i] = old
        if group == 'lineup':
            places[slot][0] = new_id
        else:
            places[slot] = new_id
        return True
    return False


def squad_assign(params):
    state = load_state()
    sq = squad_state()
    if assign_squad_slot(sq, params_of(params)):
        state['squad'] = sq
        save_state(state)
    return view_lineup(params)


def swap_squad_slots(sq, q):
    g1, g2 = q.get('type1', 'lineup'), q.get('type2', q.get('type1', 'lineup'))
    try:
        a, b = int(q.get('slot_from', -1)), int(q.get('slot_to', -1))
    except (TypeError, ValueError):
        a, b = -1, -1
    p1, p2 = _place(sq, g1, a), _place(sq, g2, b)
    if p1 is not None and p2 is not None:
        get = lambda g, places, i: places[i][0] if g == 'lineup' else places[i]

        def put(g, places, i, v):
            if g == 'lineup':
                places[i][0] = v
            else:
                places[i] = v
        if g1 == g2 == 'lineup' and 'type1' not in q:      # swap_position: two batting order places trade
            p1[a], p1[b] = p1[b], p1[a]
        else:
            x, y = get(g1, p1, a), get(g2, p2, b)
            put(g1, p1, a, y)
            put(g2, p2, b, x)
        return True
    return False


def squad_swap(params):
    state = load_state()
    sq = squad_state()
    if swap_squad_slots(sq, params_of(params)):
        state['squad'] = sq
        save_state(state)
    return view_lineup(params)


def filter_inventory(params, squad=None):
    # The substitution list of Manage Squad: arena_squad.filter_inventory {"filt_pos":N,"filt_role":R,
    # "filt_type":T,"page":P,"per_page":7} (handler 0xe93fd0: <arena_cards><arena_card .../>..., each card
    # kept as ARENACARD<n>). filt_type 0 = cards whose own position is N, 1 = N among their secondary
    # positions, 2 = out of position; for pitchers (N = 0) filt_role 0 is a starter, 1 a reliever.
    # GUESS: the element's count attributes and which card attributes the list shows.
    q = params_of(params)
    num = lambda k, d: int(q.get(k, d)) if str(q.get(k, d)).lstrip('-').isdigit() else d
    pos, role, kind, page, per_page = num('filt_pos', -1), num('filt_role', -1), num('filt_type', 0), num('page', 0), max(1, num('per_page', 7))
    in_squad = set(squad_ids(squad_state() if squad is None else squad))
    code_of = lambda c: FIELD_NUMBER.get(c['display_position'], 0)
    others_of = lambda c: set(FIELD_NUMBER.get(x.strip(), -1) for x in str(c.get('display_secondary_positions') or '').split(','))
    is_pitcher = lambda c: not c.get('is_hitter')

    def wanted(c):
        if c['item_id'] in in_squad:
            return False
        if pos < 0:
            return True
        if pos == 0:                                   # a pitching place
            if not is_pitcher(c):
                return False
            starter = c['display_position'] == 'SP'
            own = role < 0 or (role == 0) == starter
            return own if kind == 0 else (False if kind == 1 else not own)
        if is_pitcher(c):
            return False
        if pos == 9:                                   # designated hitter: any hitter
            return kind == 0
        own, second = code_of(c) == pos, pos in others_of(c)
        return own if kind == 0 else (second and not own if kind == 1 else not own and not second)
    seen, pool = set(), []
    for c in cards():
        if wanted(c) and c['name'] not in seen:        # one card per person, the best first
            seen.add(c['name'])
            pool.append(c)
    # Page numbers (study of 2026-11-19, reports\online\proposals\manage-squad-polish.md): the REQUEST's page
    # counts from 0 and is left out when the script's FILT_PAGE is negative; the REPLY's cur_page counts from 1
    # and max_page is the number of pages. The script prints both as sent, enables the arrows with
    # QCOMP[cur_page,!=,1] / QCOMP[cur_page,!=,max_page], and sets FILT_PAGE = cur_page - 1 after every reply.
    # Run 89 sent cur_page 0, so FILT_PAGE became -1 and no page could be turned.
    pages = max(1, -(-len(pool) // per_page))
    page = min(max(page, 0), pages - 1)
    chosen = pool[page * per_page:(page + 1) * per_page]
    # The run 85 crash blamed on extra attributes here was the script's IF_Q_ELSE_INT (see compare_attrs and the
    # emulator fix d61abd60), so a card may carry more than a collection card.
    eligible = 'false' if squad is None else '0'
    items = ''.join('<arena_card %s/>' % card_attrs(c, position=code_of(c), new_position=max(pos, 0), type_id=0, out_of_season=eligible,
                                                    order=page * per_page + i + 1, display_order=page * per_page + i + 1,
                                                    **compare_attrs(c, pos), **squad_card_text(c, pos))
                    for i, c in enumerate(chosen))
    return ('<response><arena_cards count="%d" total="%d">%s</arena_cards><page_stats cur_page="%d" max_page="%d" total="%d" per_page="%d"/></response>'
            % (len(chosen), len(pool), items, page + 1, pages, len(pool), per_page))


def view_item(params):
    # The card view: inventory.view_item {"game_mode_id":6,"item_id":N} (handler 0xb22a50). It wants a
    # <player> element, read by the same all-or-nothing player parser as a squad member (0x9bf200) and
    # the looks reader, with appearance_names / attribute_names on the element itself; kept as
    # DATA_ITEM_PLAYER for the screen. Empty reply: "Starting Pitcher", 0 overall, 100 lbs, Alaska.
    q = params_of(params)
    try:
        c = card_by_id(int(q.get('item_id', -1)))
    except (TypeError, ValueError):
        c = None
    if not c:
        return None
    fields = player_fields(c)
    fields.update(position=FIELD_NUMBER.get(c['display_position'], 0),
                  role={'SP': 0, 'RP': 1, 'CP': 2}.get(c['display_position'], 7), series_id=0, remaining_time=0,
                  appearance_names='|'.join(f['name'] for f in looks_fields()), attribute_names=RATING_NAMES,
                  stat_names='ENERGY', player_appearances=player_looks(c), player_attributes=player_ratings(c),
                  player_stats='255', is_active='true', type_id=0, has_actionshot='false')
    return '<response><player %s/></response>' % card_attrs(c, **fields)


def compare_attrs(c, pos):
    """What the Manage Squad comparison panel reads from a card besides the ratings.

    The screen's script (FRONTEND.PS4) computes, for the card in the squad (stash ORIGINAL) and the card
    under the cursor (REPLACEMENT), things like
        [ONLINE.IF_Q_ELSE_INT[ONLINE.QCOMP[ONLINE.ORIGINAL.IS_OOP,==,1],ONLINE.ORIGINAL.OOP_ARM_ACCURACY,ONLINE.ORIGINAL.ARM_ACCURACY]]
        [ONLINE.SUBINT[ONLINE.ORIGINAL.CONTACT_LEFT,ONLINE.REPLACEMENT.CONTACT_LEFT]]
    and its arithmetic reads a missing operand as a handle: runs 85 and 86 crashed in 0xa3caf0 (cases
    0xc-0xf, the integer operations) as soon as the substitution list was drawn, because is_oop and the
    oop_ values were missing. is_oop is compared with the number 1."""
    own = FIELD_NUMBER.get(c['display_position'], 0)
    others = set(FIELD_NUMBER.get(x.strip(), -1) for x in str(c.get('display_secondary_positions') or '').split(','))
    oop = 0 if (pos in (own, 9) or pos in others or pos < 0 or (own == 0 and pos == 0)) else 1
    val = lambda k: int(c.get(k) or 0)
    worse = lambda k: val(k) * 6 // 10 if oop else val(k)          # GUESS at the out-of-position penalty
    return {'is_oop': oop, 'oop_arm_accuracy': worse('arm_accuracy'), 'oop_blocking': worse('blocking'),
            'oop_fielding_ability': worse('fielding_ability'), 'oop_reaction_time': worse('reaction_time'),
            'role': {'SP': 0, 'RP': 1, 'CP': 2}.get(c['display_position'], 7)}


SLOT_TEXT = {1: 'C', 2: '1B', 3: '2B', 4: '3B', 5: 'SS', 6: 'LF', 7: 'CF', 8: 'RF', 9: 'DH'}
POSAMT_ORDER = ('SP', 'RP', 'CP', 'C', '1B', '2B', '3B', 'SS', 'LF', 'CF', 'RF', 'DH')


def squad_card_text(c, slot_pos):
    """Text forms the Manage Squad script prints and the client's card routine 0x9e1fa0 reads.

    Only for arena_squad.filter_inventory cards and arena_squad.view_lineup positions. The script prints
    throw_hand / bat_hand raw ("Throws: R"), so they are LETTERS here, while the card view (inventory.view_item,
    0xb22a50) wants numbers and makes the letters itself. display_new_position is what the tabs print
    ("LF" / "Secondary LF"): its absence was the "0" / "Secondary 0" of run 89. sec_positions is a list of
    position codes the card routine uses for is_2nd and the out-of-position penalty."""
    a = {'throw_hand': c.get('throw_hand') if c.get('throw_hand') in ('L', 'R') else 'R',
         'bat_hand': c.get('bat_hand') if c.get('bat_hand') in ('L', 'R', 'S') else 'R',
         'display_new_position': SLOT_TEXT.get(slot_pos, c['display_position'])}
    sec = [FIELD_NUMBER[x.strip()] for x in str(c.get('display_secondary_positions') or '').split(',')
           if FIELD_NUMBER.get(x.strip(), 0) > 0]
    if sec:                                            # left out rather than sent empty
        a['sec_positions'] = ','.join(str(x) for x in sec)
    return a


def rarity_counts(params, squad=None, team=None):
    # arena_squad.rarity_counts (0xe964c0 keeps the whole reply as RARITY_COUNTS; the shape is the script's).
    # COUNTS.POSITION[POSAMTIDX].RARITY[0..4].AMOUNT (common ... diamond): the script sets POSAMTIDX 0 for a
    # starter's place, 1 for a reliever's, FILT_POS + 2 (3..11) for C ... DH; index 2 is taken to be the
    # closers (INFERENCE: the game's usual order SP, RP, CP, C ... DH). SQUADS: active (from 0),
    # has_curr_szn_squad, SQUAD rows with name, ovr, is_active, is_season_squad, season_name.
    # SQUAD_OPTIONS.EXISTING / NEW must both exist: an IF_Q_ELSE_INT reads OPT.VCOUNT of both.
    rows = []
    for i, pos in enumerate(POSAMT_ORDER):
        amounts = [0] * 5
        for c in cards():
            if (c.get('is_hitter') if pos == 'DH' else c['display_position'] == pos):
                amounts[RARITY_ID.get(c['rarity'], 0)] += 1
        rows.append('<position id="%d">%s</position>' % (i, ''.join('<rarity id="%d" amount="%d"/>' % (k, n) for k, n in enumerate(amounts))))
    if team is None:
        team = load_state().get('arena_team', {})
    everyone = [c for group in current_squad(squad) for c in group]
    ovr = sum(c['ovr'] for c in everyone) // max(1, len(everyone))
    return ('<response><counts>%s</counts><squads active="0" has_curr_szn_squad="0">'
            '<squad id="0" name="%s" ovr="%d" is_active="1" is_season_squad="0" season_name=""/></squads>'
            '<squad_options><existing/><new/></squad_options></response>' %
            (''.join(rows), team.get('name', 'Squad 1') if squad is None else escape_mini_attr(team.get('name', 'Squad 1')), ovr))


def squad_ratings(hitters, pitchers):
    # The squad header ("Squad Attributes": Contact, Power, Speed, Defense, Pitching) prints LINEUPS.STATS.CON / PWR /
    # SPD / DEF / PIT as sent; without them run 93 showed stray numbers. Averages of the squad's own ratings (our
    # choice of formula). The sixth number, "Rating Mod", reads a value not yet named.
    avg = lambda cs, *keys: int(sum(sum(c.get(k) or 0 for k in keys) / len(keys) for c in cs) / max(1, len(cs)))
    return dict(con=avg(hitters, 'contact_left', 'contact_right'), pwr=avg(hitters, 'power_left', 'power_right'),
                spd=avg(hitters, 'speed'), def_=avg(hitters, 'fielding_ability'), pit=avg(pitchers, 'ovr'))


def squad_stats(hitters, pitchers):
    ratings = squad_ratings(hitters, pitchers)
    return 'con="{con}" pwr="{pwr}" spd="{spd}" def="{def_}" pit="{pit}"'.format(**ratings)


def view_lineup(params):
    return lineup_reply(current_squad(), load_state().get('arena_team', {}))


def lineup_reply(squad, team, *, mini=False):
    lineup, dh, bench, starters, relief = squad
    if not lineup:
        return None
    order = sorted(lineup + dh, key=lambda c: c.get('lineup_ord', 0))
    rows = lambda cs, group: ''.join(
        '<position %s/>' % card_attrs({k: v for k, v in c.items() if not k.startswith('lineup_')},
                                      position=FIELD_NUMBER.get(c['display_position'], 0),
                                      new_position=c.get('lineup_pos', 0),
                                      order=i + 1, display_order=i + 1, group=group,
                                      **compare_attrs(c, c.get('lineup_pos', FIELD_NUMBER.get(c['display_position'], 0))),
                                      **squad_card_text(c, c.get('lineup_pos', 0)))
        for i, c in enumerate(cs))
    everyone = lineup + dh + bench + starters + relief
    ovr = sum(c['ovr'] for c in everyone) // max(1, len(everyone))
    # curr_season_eligible picks the gold "Eligible" label instead of the red "Season ^d0^ Ineligible"; both print
    # season.short_name (run 90 showed "Season 925261104" without it). Study: proposals\home-screen.md section 5.
    inner = ('<arena_lineups name="%s" is_dd="1" is_season_squad="0" has_ineligible_set="0" has_dh="1" capt_pos="0" capt_pit="0" curr_season_eligible="1">'
             '<season name="Season 1" short_name="1"/><stats ovr="%d" %s/><lineup>%s</lineup><bench>%s</bench><starters>%s</starters><relief>%s</relief>'
             '</arena_lineups>' % (escape_mini_attr(team.get('name', 'Squad 1')) if mini else team.get('name', 'Squad 1'),
                                   ovr, squad_stats(lineup + dh, starters + relief), rows(order, 'lineup'), rows(bench, 'bench'),
                                   rows(starters, 'starters'), rows(relief, 'relief')))
    if mini:
        inner = inner.replace(' curr_season_eligible="1">', ' curr_season_eligible="1" ovr_limit="99">', 1)
    return '<response>%s</response>' % inner


# ---------------------------------------------------------------- Showdown
# Wire layouts and handler offsets: reports/online/proposals/showdown.md.
from contextvars import ContextVar

SHOWDOWN_RAID_ID = 7001
SHOWDOWN_BLUE_TEST = True
SHOWDOWN_BLUE_RAID_ID = 7002
SHOWDOWN_NAME = 'STARTER'
SHOWDOWN_DESCRIPTION = ('Draft a squad, win short rallies, and earn upgrades for the final score chase. '
                        'This introduction takes its inspiration from the 2022 Cy Young winners, '
                        'Justin Verlander and Sandy Alcantara.')
SHOWDOWN_FEE = 0
SHOWDOWN_UPGRADES = True
SHOWDOWN_CHOICES = 4
SHOWDOWN_ROUNDS = (
    ('lineup', ('C',)),
    ('lineup', ('SS',)),
    ('starters', ('SP',)),
    ('relief', ('RP', 'CP')),
    ('lineup', ('CF',)),
    ('lineup', ('1B',)),
    ('lineup', ('2B',)),
    ('lineup', ('RF',)),
    ('perk', ()),
    ('perk', ()),
)


class ShowdownMoment(NamedTuple):
    identifier: int
    kind: int
    name: str
    art: str
    opponent: int
    cpu_score: int
    outs: int
    banked_runs: int
    difficulty: str
    ends_run_on_loss: bool
    stubs: int = 0
    xp: int = 0
    batter_upgrade: bool = False
    perk_rarity: str | None = None
    second_base: int | None = None


# Locally authored score chases. Types 3 and 4 use the native Showdown outs
# evaluator without depending on an unverified ordinary-Moments stat binder.
SHOWDOWN_DIFFICULTIES = ('Rookie', 'Veteran', 'All-Star', 'Hall of Fame', 'Legend')
SHOWDOWN_LEGACY_MOMENTS = (
    ShowdownMoment(7101, 3, 'Opening Rally', 'MOM_STARTERPROGRAM_BKG', 0, 0, 3, 1,
                   difficulty='Rookie', ends_run_on_loss=True),
    ShowdownMoment(7102, 3, 'One Run Down', 'MOM_STARTERPROGRAM_BKG', 1, 1, 3, 1,
                   difficulty='Rookie', ends_run_on_loss=True),
    ShowdownMoment(7103, 3, 'Keep the Rally Going', 'MOM_STARTERPROGRAM_BKG', 2, 2, 6, 1,
                   difficulty='Rookie', ends_run_on_loss=True),
    ShowdownMoment(7104, 4, 'Final Showdown', 'BKG_SHOWDOWN_BOSS', 0, 4, 9, 0,
                   difficulty='Rookie', ends_run_on_loss=True, stubs=300, xp=500),
)
SHOWDOWN_NINTH_INNING_MOMENTS = (
    SHOWDOWN_LEGACY_MOMENTS[0]._replace(stubs=100, batter_upgrade=True, perk_rarity='Diamond'),
    SHOWDOWN_LEGACY_MOMENTS[1]._replace(stubs=75, batter_upgrade=True, perk_rarity='Gold'),
    SHOWDOWN_LEGACY_MOMENTS[3],
)
SHOWDOWN_MOMENTS = (
    SHOWDOWN_NINTH_INNING_MOMENTS[0]._replace(second_base=8),
    SHOWDOWN_NINTH_INNING_MOMENTS[1]._replace(second_base=8),
    SHOWDOWN_NINTH_INNING_MOMENTS[-1],
)
# The client recap asks for the previous tile after a nonterminal attempt.
# Keep that position backed by a valid, settled mission on every diagnostic.
SHOWDOWN_BLUE_RECAP = SHOWDOWN_NINTH_INNING_MOMENTS[0]._replace(
    identifier=7190, name='Recap marker', stubs=0, banked_runs=0,
    batter_upgrade=False, perk_rarity=None, ends_run_on_loss=False)
SHOWDOWN_BLUE_CASES = (
    (0, 'WINS', 'team'),
    (1, 'R', 'team'),
    (2, 'R', 'leadoff'),
    (3, None, None),  # Known raid-outs control, with the shipped start situation.
)
SHOWDOWN_BLUE_MOMENTS = tuple(
    SHOWDOWN_BLUE_RECAP._replace(
        identifier=7191 + i, kind=kind,
        name=f'Blue Test {i + 1}/4 - T{kind} {field or "raid outs"}'
             + (' (leadoff)' if actor == 'leadoff' else ''))
    for i, (kind, field, actor) in enumerate(SHOWDOWN_BLUE_CASES)
)
# CRaidProgressView_StashRaidData (0xc83e6f) requires a type-4
# mission to populate the final panel even when the run is diagnostic.
SHOWDOWN_BLUE_FINAL = SHOWDOWN_MOMENTS[-1]._replace(
    identifier=7195, name='Final panel reference (locked)', stubs=0, xp=0)
# Local XML name with the exact hash the native Rewards reader compares
# at 0xa60952. The client sums it for completed moments at 0xa65faf.
SHOWDOWN_RUNS_ATTR = 'boss_runs3_mdORt'
# The native situation request has no team or mission identifiers. The
# asyncio service connection scopes it to the current Showdown screen flow.
SHOWDOWN_SITUATION_RUN = ContextVar('showdown_situation_run', default=None)
SHOWDOWN_OPPONENT = ContextVar('showdown_opponent', default=None)
SHOWDOWN_BLUE_GAME = ContextVar('showdown_blue_game', default=False)


class ShowdownRefusal(ValueError):
    pass


def showdown_is_blue(run):
    return bool(run and run.get('track') == 'blue_test')


def showdown_blue_order(candidate):
    others = SHOWDOWN_BLUE_MOMENTS[candidate:] + SHOWDOWN_BLUE_MOMENTS[:candidate]
    return (SHOWDOWN_BLUE_RECAP,) + others + (SHOWDOWN_BLUE_FINAL,)


def showdown_raid_id(run):
    return SHOWDOWN_BLUE_RAID_ID if showdown_is_blue(run) else SHOWDOWN_RAID_ID


def showdown_moments(run=None):
    if showdown_is_blue(run):
        return showdown_blue_order(run['candidate'])
    track = run.get('track') if run else 'starter_runner'
    if track == 'starter_runner':
        return SHOWDOWN_MOMENTS
    if track == 'starter':
        return SHOWDOWN_NINTH_INNING_MOMENTS
    return SHOWDOWN_LEGACY_MOMENTS


def showdown_initial_rounds(run):
    # Runs created before the ten-round draft keep their persisted offers.
    return run.get('initial_rounds', 5) if run else len(SHOWDOWN_ROUNDS)


def showdown_ready(run):
    # Reward rounds stop entry to the next moment without hiding the hub.
    return bool(run and run['paid'] and len(run['picks']) >= showdown_initial_rounds(run))


def showdown_draft_pending(run):
    return bool(run and run['paid'] and len(run['picks']) < len(run['draft']))


def showdown_terminal(run):
    results = run.get('moment_results', []) if run else []
    if showdown_is_blue(run):
        return 3 if len(results) > 1 else None
    if run and run.get('eliminated'):
        return 4
    if len(results) == len(showdown_moments(run)):
        return 3 if results[-1] else 4
    return None


def showdown_progress(parent, run=None):
    ready = showdown_ready(run)
    terminal = showdown_terminal(run)
    state = terminal or (2 if ready else 1 if run else 0)
    perks = run.get('equipped_perks', [run['perk']] if run.get('perk') else []) if run else []
    progress = mini_node(parent, 'RaidProgress', State=state, TeamId=run['id'] if run else '',
                         PlayerIds=str(ACCOUNT_ID) if run else '', EquippedPerks=','.join(map(str, perks)))
    items = ET.SubElement(progress, 'items')
    for perk in perks:
        items.append(showdown_perk(perk))
    data = ET.SubElement(progress, 'Data')
    results = run.get('moment_results', []) if run else []
    for index, moment in enumerate(showdown_moments(run)):
        node_state = 4
        if index < len(results):
            node_state = int(results[index])
        elif index == len(results) and ready and terminal is None:
            # State 7 triggers an automatic loss on Continue. A pending
            # attempt from before the elimination rule can be retried first.
            unfinished = run.get('pending_moment') == index and run.get('pending_loss_on_resume')
            node_state = 7 if unfinished else 3
        mini_node(data, 'Node', State=node_state, MomentId=moment.identifier)


def showdown_catalogue(run=None, diagnostic=None):
    response = ET.Element('response')
    available = ET.SubElement(response, 'AvailableRaids')
    in_progress = ET.SubElement(response, 'InProgressRaids')
    entries = [(run, SHOWDOWN_RAID_ID, SHOWDOWN_NAME, SHOWDOWN_DESCRIPTION, showdown_moments)]
    if SHOWDOWN_BLUE_TEST:
        diagnostic = diagnostic or {}
        case = diagnostic.get('next', 0)
        label = SHOWDOWN_BLUE_MOMENTS[case].name
        description = 'Practice a diagnostic chase. Forfeit to move on. This entry awards no prizes.'
        entries.append((diagnostic.get('run'), SHOWDOWN_BLUE_RAID_ID, label, description,
                        lambda run: showdown_blue_order(case)))
    for run, raid_id, label, description, moments in entries:
        active_run = run if showdown_terminal(run) is None else None
        # Continue is the progress screen. The available entry's Start Draft
        # flow can recover the saved offers and initial draft counter.
        resumable = showdown_ready(active_run)
        if showdown_is_blue(active_run):
            label = SHOWDOWN_BLUE_MOMENTS[active_run['candidate']].name
        showdown_catalogue_raid(in_progress if resumable else available, active_run, raid_id,
                               label, description, moments(active_run), include_progress=resumable)
    return mini_xml(response)


def showdown_catalogue_raid(parent, run, raid_id, label, description, moments, *, include_progress):
    # The diagnostic entry gets another stock cover: with the Starter's it was
    # taken for a duplicate on the select screen (run 114).
    cover = 'SHOWDOWN_TA1_EAST' if raid_id == SHOWDOWN_BLUE_RAID_ID else 'SHOWDOWN_STARTER'
    raid = mini_node(parent, 'Raid', Id=raid_id, Name=label, Description=description,
                     CoverTexture=cover, Timed='false', TimeText='', IsCoop='false',
                     EntryPrice=SHOWDOWN_FEE, DraftData='{}')
    for index, moment in enumerate(moments):
        node = mini_node(raid, 'Node', type=moment.kind,
                         fail='true' if moment.ends_run_on_loss else 'false')
        rewards = mini_node(node, 'Rewards', stubs=moment.stubs, xp=moment.xp, item_quantity=0,
                            **{SHOWDOWN_RUNS_ATTR: moment.banked_runs})
        ET.SubElement(rewards, 'PlayerItems')
        upgrades = ET.SubElement(rewards, 'RaidItems')
        for entry in showdown_upgrade_options(run, index, moment):
            mini_node(upgrades, 'item', **entry['reward'])
    if include_progress:
        showdown_progress(raid, run)


def showdown_perk(perk_id):
    # Native progress needs the disc's benefit fields. Use the inventory
    # helper for text and art, and keep the disc ID stable across ownership changes.
    source = next((d for d in inventory_data()['perks'].values() if int(d['id']) == perk_id), None)
    if source is None:
        raise ShowdownRefusal('This Showdown perk is missing from the local inventory data.')
    item = inventory_item('Perk', dict(name=source['name'], item_id=perk_id,
                                       uuid=f'local-showdown-perk-{perk_id}', quantity=1, no_sell=1))
    if not item:
        raise ShowdownRefusal('Showdown perk art is unavailable.')
    row = ET.fromstring(item)
    row.attrib.update({k: str(v) for k, v in source.items() if k not in row.attrib})
    row.set('series_id', '0')
    row.set('is_discovered', '1')
    return row


def showdown_perk_pool():
    pool = {}
    for definition in inventory_data()['perks'].values():
        identifier = int(definition['id'])
        try:
            row = showdown_perk(identifier)
        except ShowdownRefusal:
            continue
        pool[identifier] = row.get('rarity_id')
    return pool


def showdown_upgrade_options(run, index, moment):
    # Advertise only rounds that can yield all four entries. Completed nodes
    # keep the exact descriptors attached to their persisted draft rounds.
    if not SHOWDOWN_UPGRADES or not (moment.batter_upgrade or moment.perk_rarity):
        return []
    if run and index < len(run.get('moment_results', [])):
        return [entry for entry in run['draft'] if entry.get('upgrade_node') == index]

    cards_by_id = {card['item_id']: card for card in all_cards()}
    squad = run['squad'] if run else {}
    used = {identifier for identifier, _ in squad.get('lineup', ())}
    for group in ('bench', 'starters', 'relief'):
        used.update(squad.get(group, ()))
    for entry in run['draft'] if run else ():
        if entry['group'] != 'perk':
            used.update(entry['offers'])
    used_players = {cards_by_id[identifier]['roster_player_id'] for identifier in used if identifier in cards_by_id}
    eligible = {}
    positions = {position for _, position in squad['lineup']} if run else set(range(1, 10))
    for card in cards_by_id.values():
        player = card['roster_player_id']
        if (player > 0 and player not in used_players and card['is_hitter']
                and FIELD_NUMBER.get(card['display_position']) in positions
                and card['series'] == 'Live' and card['rarity'] == 'Diamond'):
            eligible.setdefault(player, card['item_id'])

    # Keep four choices available after each earlier draft consumes one perk.
    picks = run['picks'] if run else ()
    settled = len(run.get('moment_results', ())) if run else 0
    pending_perks = (sum(entry['group'] == 'perk' for entry in run['draft'][len(picks):])
                     if run else sum(group == 'perk' for group, _ in SHOWDOWN_ROUNDS))
    reserve = pending_perks + index - settled
    perk_pool = showdown_perk_pool()
    options = []
    if moment.batter_upgrade and len(eligible) >= SHOWDOWN_CHOICES * (1 + index - settled):
        options.append({'group': 'lineup', 'offers': sorted(eligible.values()), 'upgrade_node': index,
                        'reward': {'id': 1, 'type_id': 4, 'rarity_id': 4,
                                   'short_description': 'Diamond Live Series Batter'}})
    for rarity in (moment.perk_rarity, 'Bronze') if moment.perk_rarity else ():
        offers = sorted(identifier for identifier, tier in perk_pool.items()
                        if tier == str(RARITY_ID[rarity]) and identifier not in picks)
        if len(offers) >= SHOWDOWN_CHOICES + reserve:
            options.append({'group': 'perk', 'offers': offers, 'upgrade_node': index,
                            'reward': {'id': 2, 'type_id': 6, 'rarity_id': RARITY_ID[rarity],
                                       'short_description': f'{rarity} Perk'}})
            break
    return options


def showdown_number(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch('[0-9]{1,20}', value):
        return int(value)
    return None


def showdown_id(value):
    if isinstance(value, str):
        value = value.lower().replace('-', '')
        if re.fullmatch('[a-f0-9]{32}', value):
            return value
    return None


def showdown_run_for(run, query, *, required=False):
    requested = query.get('raid_team_id', query.get('team_id'))
    parsed = showdown_id(requested)
    if run is not None:
        if parsed and parsed == showdown_id(run['id']):
            return run
        if not required and showdown_terminal(run) is None and (
                requested in (None, '', 0, '0') or parsed == '0' * 32):
            return run
    if required:
        raise ShowdownRefusal('This Showdown entry could not be found. Open Showdown Select again.')
    return None


def showdown_entry_account(account, query):
    if SHOWDOWN_BLUE_TEST:
        diagnostic = account.get('blue_test', {})
        requested = showdown_id(query.get('raid_team_id', query.get('team_id')))
        run = diagnostic.get('run') or {}
        if requested and requested in (showdown_id(run.get('id')), showdown_id(diagnostic.get('last_abandoned'))):
            return diagnostic
        if not requested and showdown_number(query.get('raid_id')) == SHOWDOWN_BLUE_RAID_ID:
            return diagnostic
    return account


def showdown_blue_rotate(account, run):
    account['next'] = (run['candidate'] + 1) % len(SHOWDOWN_BLUE_CASES)
    if showdown_ready(run):
        account['roster'] = {key: run[key] for key in
                             ('squad', 'draft', 'picks', 'initial_rounds', 'perk', 'equipped_perks')}


def showdown_blue_log(moment):
    route = 'ordinary' if moment.kind < 3 else 'raid_outs'
    return f'{moment.name} type={moment.kind} route={route} node=1'


def showdown_create(state, account, query):
    raid_id = showdown_number(query.get('raid_id'))
    blue = SHOWDOWN_BLUE_TEST and raid_id == SHOWDOWN_BLUE_RAID_ID
    if not blue and raid_id != SHOWDOWN_RAID_ID:
        raise ShowdownRefusal('This Showdown is unavailable.')
    state['showdown'] = account
    if blue:
        account = account.setdefault('blue_test', {})
    existing = account.get('run')
    if existing is not None and showdown_terminal(existing) is None:
        if not existing['paid']:
            showdown_pay(state, existing, query)
        return existing
    if blue and account.get('roster'):
        run = dict(account['roster'], id=str(uuid4()), track='blue_test', candidate=account.get('next', 0),
                   fee=0, paid=True, moment_results=[False])
        account['run'] = run
        save_state(state)
        return run
    rounds = SHOWDOWN_ROUNDS[:2] if blue else SHOWDOWN_ROUNDS
    # Draft loans are drawn from the catalogue. The owner's held variants
    # carry the usual display data. Never change either source inventory.
    pool = {c['item_id']: c for c in all_cards()}
    pool.update((c['item_id'], c) for c in cards(state))
    eligible = sorted((c for c in pool.values() if c['roster_player_id'] > 0
                       and c['display_position'] in FIELD_NUMBER), key=lambda c: (c['ovr'], c['item_id']))
    lineup, dh, bench, starters, relief = pick_squad(eligible)
    starter = lineup + dh + bench + starters + relief
    used = {c['roster_player_id'] for c in starter}
    if tuple(map(len, (lineup, dh, bench, starters, relief))) != (8, 1, 4, 5, 8) or len(used) != 26:
        raise ShowdownRefusal('The local catalogue cannot make a complete Showdown squad.')
    squad = {'lineup': [[c['item_id'], FIELD_NUMBER[c['display_position']]] for c in lineup]
                       + [[dh[0]['item_id'], 9]], 'bench': [c['item_id'] for c in bench],
             'starters': [c['item_id'] for c in starters], 'relief': [c['item_id'] for c in relief]}
    draft = []
    rng = random.SystemRandom()
    remaining_perk_rounds = sum(group == 'perk' for group, _ in rounds)
    perk_ids = sorted(showdown_perk_pool()) if remaining_perk_rounds else []
    if remaining_perk_rounds and len(perk_ids) < SHOWDOWN_CHOICES + remaining_perk_rounds - 1:
        raise ShowdownRefusal('The local catalogue needs five Showdown perks with art.')
    for group, positions in rounds:
        if group == 'perk':
            # Later perk offers are sampled after the previous pick, so
            # both rounds have four different cards and cannot repeat a pick.
            offers = perk_ids if draft[-1]['group'] == 'perk' else rng.sample(perk_ids, SHOWDOWN_CHOICES)
            draft.append({'group': group, 'offers': offers})
            continue
        # Use distinct people across the roster and all player offers,
        # including alternate card versions.
        choices = {c['roster_player_id']: c['item_id'] for c in eligible
                   if c['display_position'] in positions and c['roster_player_id'] not in used}
        if len(choices) < SHOWDOWN_CHOICES:
            labels = '/'.join(positions)
            raise ShowdownRefusal(f'The local catalogue needs more {labels} cards for this draft.')
        draw = rng.sample(list(choices), SHOWDOWN_CHOICES)
        used.update(draw)
        slot = next(i for i, (_, pos) in enumerate(squad['lineup']) if pos == FIELD_NUMBER[positions[0]]) if group == 'lineup' else 0
        draft.append({'group': group, 'slot': slot, 'offers': [choices[i] for i in draw]})
    run = {'id': str(uuid4()), 'track': 'starter_runner', 'fee': SHOWDOWN_FEE, 'paid': True, 'squad': squad,
           'draft': draft, 'initial_rounds': len(draft), 'picks': [], 'perk': None, 'equipped_perks': []}
    if blue:
        run.update(track='blue_test', candidate=account.get('next', 0), moment_results=[False])
    account['run'] = run
    save_state(state)
    return run


def showdown_pay(state, run, query):
    if showdown_number(query.get('raid_id')) != showdown_raid_id(run):
        raise ShowdownRefusal('The entry fee does not match this Showdown.')
    if not run['paid']:
        run.update(fee=SHOWDOWN_FEE, paid=True)
        save_state(state)
    return '<response><StateId>1</StateId></response>'


def showdown_pick(state, run, query):
    selected = showdown_number(query.get('item_mlb_player_id'))
    if selected in run['picks']:
        return True
    if not showdown_draft_pending(run) or showdown_terminal(run) is not None:
        return False
    entry = run['draft'][len(run['picks'])]
    if selected not in entry['offers']:
        return False
    group = entry['group']
    if group == 'perk':
        run['perk'] = selected
        if 'equipped_perks' in run:
            run['equipped_perks'] = (run['equipped_perks'] + [selected])[-3:]
        next_index = len(run['picks']) + 1
        if next_index < showdown_initial_rounds(run) and run['draft'][next_index]['group'] == 'perk':
            next_entry = run['draft'][next_index]
            remaining = [identifier for identifier in next_entry['offers'] if identifier != selected]
            next_entry['offers'] = random.SystemRandom().sample(remaining, SHOWDOWN_CHOICES)
    elif group == 'lineup':
        lineup = run['squad']['lineup']
        if 'upgrade_node' in entry:
            position = FIELD_NUMBER[card_by_id(selected)['display_position']]
            slot = next(i for i, (_, pos) in enumerate(lineup) if pos == position)
        else:
            slot = entry['slot']
        lineup[slot][0] = selected
    else:
        run['squad'][group][entry['slot']] = selected
    run['picks'].append(selected)
    save_state(state)
    return True


def showdown_picks(run=None):
    response = ET.Element('response')
    index = len(run['picks']) if run else 0
    total = len(run['draft']) if run and run['paid'] else 0
    pending = index < total
    mini_node(response, 'draft_state', round=min(index + 1, total) if total else 1,
              max_round=total, is_first_draft=int(pending and index < showdown_initial_rounds(run)))
    count = SHOWDOWN_CHOICES if pending else 0
    offers = mini_node(response, 'arena_cards', count=count, total=count)
    rewards = ET.SubElement(response, 'rewards')
    if pending:
        entry = run['draft'][index]
        for identifier in entry['offers']:
            if entry['group'] == 'perk':
                row = showdown_perk(identifier)
                row.attrib.update(dict.fromkeys(RATINGS, '0'))
                row.attrib.update(position='0', new_position='0', role='0', roster_player_id='0', rank='0',
                                  is_oop='0', oop_arm_accuracy='0', oop_fielding_ability='0',
                                  oop_reaction_time='0', oop_blocking='0')
            else:
                card = card_by_id(identifier)
                position = FIELD_NUMBER[card['display_position']]
                row = ET.fromstring('<arena_card %s/>' % card_attrs(
                    card, position=position, new_position=position, type_id=0,
                    **compare_attrs(card, position), **squad_card_text(card, position)))
            row.attrib.update(pick0='0', pick1='0', quantity='1', is_live_set='0',
                              captained='0', marketplace_best_buy='0', quick_sell_value='0')
            row.tag = 'arena_card'
            offers.append(row)
            mini_node(rewards, 'card', **row.attrib)
    return mini_xml(response)


def showdown_lineup(run):
    body = lineup_reply(current_squad(run['squad']), {'name': SHOWDOWN_NAME}, mini=True)
    response = ET.fromstring(body or showdown_neutral('raids.view_lineup'))
    lineups = response.find('arena_lineups')
    lineups.attrib.update(draft_state=str(len(run['picks'])),
                          draft_max=str(len(run['draft']) if run['paid'] else 0))
    for row in lineups.findall('.//position'):
        row.attrib.update(type_id='0', is_live_set='0', captained='0')
    return mini_xml(response)


def showdown_options(moment):
    # 0xae2699–0xae2bc9 immediately dereferences several of these lookups.
    # 0xae27e9 to 0xae2910 maps JSON strings '0' through '4' to native values 4 through 8.
    difficulty = str(SHOWDOWN_DIFFICULTIES.index(moment.difficulty))
    gameplay = dict(Balks='Off', Ejections='Off', GuessPitch='Off', HittingDifficulty=difficulty,
                    PitchingDifficulty=difficulty, Injuries='Off', NumInnings=9, PresMode='Fast Play',
                    QuickCounts='Off', Stadium=str(moment.opponent), Time='1100',
                    UmpireClosePlays='On', UmpiringAccuracy='Perfect', Weather='0',
                    BlackAndWhiteMode=False, VideoName='', StrikeZone='On', HotZones='Off')
    option = dict(GameplaySetupData=gameplay,
                  OpponentTeamData=dict(TeamType='mlb_team', TeamId=moment.opponent),
                  UsersTeamData=dict(TeamType='user_dd_team', TeamId=DD_TEAM_ID,
                                     allowLineupEditing=False),  # Required at 0xae3433.
                  StartingSituationData=dict(situationId=moment.identifier, userCurrentBatter=0,
                                             cpuCurrentBatter=0, cpuCurrentPitcher=0,
                                             userCurrentPitcher=0, cpuCurrentPitcherIsStarter=True,
                                             userCurrentPitcherIsStarter=True),
                  DurationData=dict(Type='outs', Value=moment.outs))
    if moment.second_base is not None:
        option['StartingSituationData'].update(firstBase=-1, secondBase=moment.second_base, thirdBase=-1)
    return json.dumps({'OptionsList': [option]})


def showdown_banked_runs(run):
    return sum(moment.banked_runs for moment, won in zip(showdown_moments(run), run.get('moment_results', ())) if won)


def showdown_blue_criteria(field, threshold, actor=-1):
    # -1 is the native fallback sentinel. The team/actor binding remains an
    # experiment. The tokens, integer types, and complete row shape are known.
    row = dict(mode='batting', field=field, condition='>=', value=threshold, id=actor,
               pre='Reach ', post=f' {field}', post_singular=f' {field}')
    return json.dumps(dict(stat_binder=0, stat_conditions=[row]))


def showdown_mission_definitions(ids, run=None, *, blue=False):
    response = ET.Element('response')
    mission_set = ET.SubElement(response, 'SinglesMissionSet')
    ready = showdown_ready(run)
    results = run.get('moment_results', []) if ready else []
    banked = showdown_banked_runs(run) if ready else 0
    moments = showdown_moments(run) if run or not blue else showdown_blue_order(0)
    for index, moment in enumerate(moments):
        if moment.identifier in ids:
            name = moment.name
            # The native recap focuses the previous node (0xc84e70). Label
            # it as settled while keeping the active node's wire state intact.
            if index < len(results):
                result = 'won' if results[index] else 'lost'
                name = f'Played and {result} - {name}'
            elif ready and index == len(results) and showdown_terminal(run) is None:
                name = f'Next - {name}'
            user_score = banked if moment.kind == 4 else 0
            desc = f'Start {user_score}-{moment.cpu_score}. Take the lead before making {moment.outs} outs.'
            if moment.second_base is not None:
                goal = 'Take the lead' if moment.cpu_score else 'Score'
                desc = (f'Start 0-{moment.cpu_score}. Top of the 10th, runner on second. '
                        f'{goal} before making three outs.')
            win = json.dumps({'outs_remaining': moment.outs})
            lose = '{"stat_conditions":[]}'
            raid_id = SHOWDOWN_BLUE_RAID_ID if blue else SHOWDOWN_RAID_ID
            if blue and moment.kind < 3:
                _, field, actor = SHOWDOWN_BLUE_CASES[moment.kind]
                player = card_by_id(run['squad']['lineup'][0][0]) if run and actor == 'leadoff' else None
                win = showdown_blue_criteria(field, 1, player['roster_player_id'] if player else -1)
                lose = showdown_blue_criteria('OUTS', 3)
                desc = 'Take the lead in the top of the tenth before making three outs.'
                if actor == 'leadoff':
                    desc += ' The tested condition targets the leadoff batter.'
            mini_node(mission_set, 'SinglesMission', Id=moment.identifier,
                      GroupId=raid_id, SetsId=raid_id,
                      Name=name, Description=desc, Large_Texture=moment.art, Order=index, StartDate=0,
                      EndDate=0, Timed='false', Repeatable='false', RewardsXp=moment.xp, RewardsStubs=moment.stubs,
                      RewardsItem=0, NumGames=1,
                      UnlockCriteriaType=0, UnlockCriteriaDesc='', UnlockCriteriaDetail='{}',
                      WinningCriteriaType=0, WinningCriteriaDetail=win,
                      LosingCriteriaType=0, LosingCriteriaDetail=lose,
                      PerGameOptions=showdown_options(moment), UserJerseyTeamId=0,
                      OppJerseyTeamId=moment.opponent, UserAbbreviation='LOC',
                      OppAbbreviation=MLB_TEAMS[moment.opponent][0])
    return mini_xml(response)


def showdown_situations(run):
    response = ET.Element('response')
    for moment in showdown_moments(run):
        # The final boss adds the accrued runs locally at 0x124b66f/0x124b739.
        runner_on_second = moment.second_base is not None
        top = runner_on_second or (showdown_is_blue(run) and moment.kind < 3)
        situation = dict(version=1, name=moment.name, inning=10 if top else 9, inningIsTop=top,
                         scoreCPU=moment.cpu_score, scoreUser=0, userIsHome=not top, balls=0, strikes=0, outs=0,
                         runnerOnFirst=False, runnerOnSecond=runner_on_second, runnerOnThird=False)
        mini_node(response, 'Situation', id=moment.identifier, data=json.dumps(situation))
    return mini_xml(response)


def showdown_team_reply(state, run):
    team = dict(_cpu_team(0, 'LOC', '', SHOWDOWN_NAME), **state.get('arena_team', {}))
    squad = current_squad(run['squad'])
    if tuple(map(len, squad)) != (8, 1, 4, 5, 8):
        raise ShowdownRefusal('The saved Showdown roster needs 26 players.')
    response = ET.fromstring(team_reply(squad, team))
    # The Singles callback reads this in addition to the shared builder's
    # has_dh bit (0xb68cd2 and 0x9d3fa0).
    response.find('arena_team').set('use_dh', 'true')
    return mini_xml(response)


def showdown_start_node(state, run, query):
    index = showdown_number(query.get('node_index'))
    if not (showdown_ready(run) and showdown_terminal(run) is None
            and not showdown_draft_pending(run) and index == len(run.get('moment_results', []))):
        return False
    if run.get('pending_moment') != index or not run.get('pending_loss_on_resume'):
        run['pending_moment'] = index
        run['pending_loss_on_resume'] = True
        save_state(state)
    return True


def showdown_record_result(state, run, query):
    if (not showdown_ready(run) or showdown_terminal(run) is not None
            or showdown_number(query.get('raid_id')) != showdown_raid_id(run)
            or showdown_number(query.get('player_id')) != ACCOUNT_ID):
        return None
    index = showdown_number(query.get('node_index'))
    if index is None or index != run.get('pending_moment') or index != len(run.get('moment_results', [])):
        return
    raw = query.get('raid_success')
    if type(raw) not in (bool, int, str) or raw not in (True, False, 'true', 'false', '0', '1'):
        return
    won = raw in (True, 'true', '1')
    moment = showdown_moments(run)[index]
    if showdown_is_blue(run):
        del run['pending_moment']
        run.pop('pending_loss_on_resume', None)
        account = state['showdown']['blue_test']
        counters = account.setdefault('results', {}).setdefault(str(run['candidate']), {})
        result = 'wins' if won else 'losses'
        counters[result] = counters.get(result, 0) + 1
        if won:
            run['moment_results'].append(True)
            showdown_blue_rotate(account, run)
        save_state(state)
        return won
    rounds = showdown_upgrade_options(run, index, moment) if won else ()
    rng = random.SystemRandom()
    for entry in rounds:
        entry['offers'] = rng.sample(entry['offers'], SHOWDOWN_CHOICES)
        run['draft'].append(entry)
    run.setdefault('moment_results', []).append(won)
    del run['pending_moment']
    run.pop('pending_loss_on_resume', None)
    if not won and moment.ends_run_on_loss:
        run['eliminated'] = True
    if won and (moment.stubs or moment.xp):
        profile = state.setdefault('profile', {})
        profile['stubs'] = profile.get('stubs', 0) + moment.stubs
        profile['xp'] = min(0xffffffff, profile.get('xp', 0) + moment.xp)
    if won:  # The hub can also send false to close an unfinished attempt (0xc870e2).
        affinity_record_game(state, f'showdown:{run["id"]}:{index}',
                             affinity_squad_clubs(current_squad(run['squad'])), True)
    # Store the result and wallet change together. The consumed index and the
    # terminal state reject retries, including after a reload.
    save_state(state)


def showdown_shared(name, params):
    tutorial = 'arena_tutorial.singles_mission.'
    relevant = ('singles_mission.view_missions_by_ids', 'singles_mission.view_teams',
                tutorial + 'view_situations', 'textures.screen', 'arena_team.view')
    if name not in relevant:
        if name.startswith(tutorial):
            SHOWDOWN_SITUATION_RUN.set(None)
            SHOWDOWN_OPPONENT.set(None)
        return None
    query = params_of(params)
    if not isinstance(query, dict):
        return None
    if name == 'textures.screen':
        screen = str(query.get('screen', '')).lower()
        if screen == 'dd_navdiamonddynasty':
            SHOWDOWN_BLUE_GAME.set(False)
        if screen not in ('r_progress', 'r_startnew', 'r_draft', 'r_perks'):
            SHOWDOWN_SITUATION_RUN.set(None)
            SHOWDOWN_OPPONENT.set(None)
        return None
    if name == 'arena_team.view':
        # The Singles queue can choose the generic team view module for the
        # CPU (0xb57a68). A preceding raid roster request scopes this lookup.
        # The by-team route already serves clubs.
        opponent = SHOWDOWN_OPPONENT.get()
        if (opponent is not None and 'player_id' not in query and 'mini_run_id' not in query
                and showdown_number(query.get('team_id')) == opponent
                and showdown_number(query.get('squad_type')) in (0, 4)):
            try:
                inner = view_by_team(params)
                if inner:
                    return envelope(inner), 'showdown opponent'
            except Exception as error:
                print(f'Showdown opponent failed ({type(error).__name__})', file=sys.stderr)
            return envelope(status=1, message='The local Showdown opponent could not be prepared.'), 'refused'
        return None
    if name == 'singles_mission.view_missions_by_ids':
        ids = {showdown_number(part.strip()) for part in str(query.get('moment_ids', '')).split(',')}
        blue = SHOWDOWN_BLUE_TEST and ids <= {moment.identifier for moment in showdown_blue_order(0)}
        if blue or ids <= {moment.identifier for moment in SHOWDOWN_LEGACY_MOMENTS}:
            try:
                account = load_state().get('showdown', {})
                run = (account.get('blue_test', {}) if blue else account).get('run')
                return envelope(showdown_mission_definitions(ids, run, blue=blue)), 'showdown definitions'
            except Exception as error:
                print(f'Showdown definitions failed ({type(error).__name__})', file=sys.stderr)
                return envelope(showdown_mission_definitions(ids, blue=blue)), 'showdown neutral'
        SHOWDOWN_SITUATION_RUN.set(None)
        SHOWDOWN_OPPONENT.set(None)
        return None
    situations = name == tutorial + 'view_situations'
    identifier = SHOWDOWN_SITUATION_RUN.get() if situations else query.get('raid_team_id')
    if not showdown_id(identifier) or showdown_id(identifier) == '0' * 32:
        return None
    try:
        state = load_state()
        team_query = {'team_id': identifier}
        account = showdown_entry_account(state.get('showdown', {}), team_query)
        run = showdown_run_for(account.get('run'), team_query)
        if not showdown_ready(run):
            body = '<response><Situations/></response>' if situations else showdown_neutral('raids.view_team')
            return envelope(body), 'showdown neutral'
        if situations:
            return envelope(showdown_situations(run)), 'showdown situations'
        body = showdown_team_reply(state, run)
        cursor = len(run.get('moment_results', []))
        moments = showdown_moments(run)
        SHOWDOWN_OPPONENT.set(moments[cursor].opponent if cursor < len(moments) else None)
        return envelope(body), 'showdown drafted roster'
    except Exception as error:
        print(f'Showdown {name} failed ({type(error).__name__})', file=sys.stderr)
        if situations:
            return envelope('<response><Situations/></response>'), 'showdown neutral'
        return envelope(status=1, message='The local Showdown roster could not be prepared.'), 'refused'


def showdown_neutral(name, run=None):
    if name == 'raids.enter':
        return showdown_catalogue()
    if name == 'raids.get_picks':
        return showdown_picks()
    if name == 'raids.pick_card':
        return '<response><raid pick_accepted="0"/></response>'
    if name in ('raids.get_progress', 'raids.update_progress', 'raids.join_team'):
        response = ET.Element('response')
        if name == 'raids.update_progress':
            ET.SubElement(ET.SubElement(response, 'NodeRewards'), 'PlayerItems')
        if name == 'raids.join_team':
            ET.SubElement(response, 'TeamId').text = run['id'] if run else ''
        showdown_progress(response, run)
        return mini_xml(response)
    if name == 'raids.view_lineup':
        return ('<response><arena_lineups name="STARTER" draft_state="0" draft_max="0" '
                'is_dd="1" is_season_squad="0" has_ineligible_set="0" has_dh="1" capt_pos="0" capt_pit="0" '
                'curr_season_eligible="1" ovr_limit="99"><season short_name="1" name="Season 1"/>'
                '<stats ovr="0" con="0" pwr="0" spd="0" def="0" pit="0"/>'
                '<lineup/><bench/><starters/><relief/></arena_lineups></response>')
    if name == 'raids.view_team':
        response = ET.Element('response')
        mini_node(response, 'arena_team', id=DD_TEAM_ID, name=SHOWDOWN_NAME, location='', abbreviation='LOC',
                  has_dh=1, is_logo_approved='true', mini_cpu=0, wins=0, losses=0, draft_state=0, capt_pos=0,
                  capt_pit=0, home_uniform_team_id=0, away_uniform_team_id=0)
        mini_node(response, 'arena_players', total=0, appearance_names='', attribute_names=RATING_NAMES,
                  stat_names='ENERGY')
        return mini_xml(response)
    if name == 'raids.rarity_counts':
        response = ET.Element('response')
        counts = ET.SubElement(response, 'counts')
        for index, _ in enumerate(POSAMT_ORDER):
            position = mini_node(counts, 'position', id=index)
            for rarity in range(5):
                mini_node(position, 'rarity', id=rarity, amount=0)
        squads = mini_node(response, 'squads', active=0, has_curr_szn_squad=0)
        mini_node(squads, 'squad', id=0, name=SHOWDOWN_NAME, ovr=0, is_active=1, is_season_squad=0, season_name='')
        options = ET.SubElement(response, 'squad_options')
        ET.SubElement(options, 'existing')
        ET.SubElement(options, 'new')
        return mini_xml(response)
    return {
        'raids.start_node': '<response><raid started="0"/></response>',
        'raids.check_for_draft': '<response><raid needs_draft="0"/></response>',
        'raids.get_filter': '<response><search_filters/></response>',
        'raids.filter_inventory': '<response><arena_cards count="0" total="0"/>'
                                  '<page_stats cur_page="1" max_page="1" total="0" per_page="4"/></response>',
        'raids.captain_info': '<response><captain active="0" enabled="0"/></response>',
    }.get(name)


def showdown_reply(name, params):
    if not name.startswith('raids.'):
        return showdown_shared(name, params)
    try:
        query = params_of(params)
        if not isinstance(query, dict):
            raise ShowdownRefusal('Invalid Showdown request.')
        state = load_state()
        account = state.get('showdown', {'version': 1})
        if account.get('version') != 1:
            raise ShowdownRefusal('This Showdown save version is unavailable.')
        root_account = account
        account = showdown_entry_account(account, query)
        run = account.get('run')
        kind = 'made'
        if name == 'raids.enter':
            SHOWDOWN_SITUATION_RUN.set(None)
            SHOWDOWN_OPPONENT.set(None)
            SHOWDOWN_BLUE_GAME.set(False)
            inner = showdown_catalogue(root_account.get('run'), root_account.get('blue_test'))
        elif name == 'raids.create_team':
            created = showdown_create(state, root_account, query)
            inner = f'<response><TeamId>{created["id"]}</TeamId></response>'
        elif name == 'raids.pick_card':
            requested = showdown_id(query.get('raid_team_id', query.get('team_id')))
            matched = run and requested == showdown_id(run['id'])
            accepted = bool(matched and showdown_pick(state, run, query))
            if accepted and showdown_ready(run):
                SHOWDOWN_SITUATION_RUN.set(run['id'])
            inner = f'<response><raid pick_accepted="{int(accepted)}"/></response>'
        elif name in ('raids.pay_entry_fees', 'raids.abandon'):
            abandoned = showdown_id(account.get('last_abandoned'))
            if name == 'raids.abandon' and abandoned and showdown_id(query.get('team_id')) == abandoned:
                return envelope('<response><raid abandoned="1"/></response>'), 'made'
            run = showdown_run_for(run, query, required=True)
            if name == 'raids.pay_entry_fees':
                inner = showdown_pay(state, run, query)
            else:
                if showdown_is_blue(run):
                    showdown_blue_rotate(account, run)
                    SHOWDOWN_BLUE_GAME.set(False)
                account.update(run=None, last_abandoned=run['id'])
                save_state(state)
                SHOWDOWN_SITUATION_RUN.set(None)
                SHOWDOWN_OPPONENT.set(None)
                inner = '<response><raid abandoned="1"/></response>'
        else:
            run = showdown_run_for(run, query)
            if name == 'raids.start_node':
                SHOWDOWN_SITUATION_RUN.set(None)
                SHOWDOWN_OPPONENT.set(None)
                if not showdown_id(query.get('team_id')):
                    run = None
                if showdown_start_node(state, run, query):
                    # This native ACK has no response-body reader (0x9e8460).
                    inner = '<response><raid started="1"/></response>'
                    diagnostic = showdown_is_blue(run)
                    SHOWDOWN_BLUE_GAME.set(diagnostic)
                    if diagnostic:
                        moment = showdown_moments(run)[run['pending_moment']]
                        kind = f'blue test started {showdown_blue_log(moment)} success=pending'
                    else:
                        kind = f'showdown started node={run["pending_moment"]}'
                else:
                    inner = showdown_neutral(name)
                    kind = 'showdown neutral'
                    index = showdown_number(query.get('node_index'))
                    if run and index is not None and 0 <= index < len(run.get('moment_results', [])):
                        kind = f'showdown ignored settled node={index}'
            elif name == 'raids.update_progress':
                if not showdown_id(query.get('team_id')):
                    run = None
                previous = len(run.get('moment_results', [])) if run else 0
                diagnostic_result = showdown_record_result(state, run, query)
                results = run.get('moment_results', []) if run else []
                kind = 'showdown neutral'
                if showdown_is_blue(run) and diagnostic_result is not None:
                    moment = showdown_moments(run)[previous]
                    disposition = 'advance' if diagnostic_result else 'retry'
                    kind = (f'blue test result {showdown_blue_log(moment)} '
                            f'success={int(diagnostic_result)} {disposition}')
                    SHOWDOWN_SITUATION_RUN.set(run['id'])
                elif len(results) > previous:
                    banked = showdown_banked_runs(run)
                    kind = f'showdown result node={previous} success={int(results[-1])} banked={banked}'
                    terminal = showdown_terminal(run)
                    if terminal is not None:
                        kind += ' completed' if terminal == 3 else ' defeated'
                    SHOWDOWN_SITUATION_RUN.set(run['id'])
                inner = showdown_neutral(name, run)
            elif name == 'raids.get_picks':
                inner = showdown_picks(run)
                if showdown_draft_pending(run) and showdown_ready(run):
                    kind = f'showdown upgrade round={len(run["picks"]) + 1}/{len(run["draft"])}'
            elif name == 'raids.view_lineup' and run:
                inner = showdown_lineup(run)
            elif name == 'raids.check_for_draft':
                needs_draft = int(showdown_draft_pending(run))
                if showdown_ready(run):
                    SHOWDOWN_SITUATION_RUN.set(run['id'])
                inner = f'<response><raid needs_draft="{needs_draft}"/></response>'
                kind = f'showdown needs_draft={needs_draft}'
            elif name == 'raids.view_team' and run:
                inner = showdown_team_reply(state, run)
            elif name == 'raids.rarity_counts':
                squad = run['squad'] if run else {}
                inner = rarity_counts(params, squad, {'name': SHOWDOWN_NAME})
            else:
                if name == 'raids.get_progress' and showdown_ready(run):
                    if showdown_is_blue(run) and 'pending_moment' not in run:
                        SHOWDOWN_BLUE_GAME.set(False)
                    SHOWDOWN_SITUATION_RUN.set(run['id'])
                    results = run.get('moment_results', [])
                    cursor = len(results)
                    next_node = (f'{cursor} moment={showdown_moments(run)[cursor].identifier}'
                                 if showdown_terminal(run) is None else 'none')
                    kind = f'showdown progress next_node={next_node} banked={showdown_banked_runs(run)}'
                inner = showdown_neutral(name, run)
        if not inner:
            if name == 'raids.skip_ahead':
                raise ShowdownRefusal('Play each rally before entering the final boss.')
            raise ShowdownRefusal('This Showdown action is not available on the local server.')
        return envelope(inner), kind
    except Exception as error:
        if not isinstance(error, ShowdownRefusal):
            print(f'Showdown {name} failed ({type(error).__name__})', file=sys.stderr)
        neutral = showdown_neutral(name)
        if neutral is not None:
            return envelope(neutral), 'showdown neutral'
        message = str(error) if isinstance(error, ShowdownRefusal) else 'The local Showdown request could not be completed.'
        return envelope(status=1, message=escape_mini_attr(message)), 'refused'


# ---------------------------------------------------------------- My Inventory: items that are not player cards
# Study: reports\online\proposals\inventory-numbering.md (2026-11-19). type_id: 0 MLB player,
# 1 stadium, 2 equipment, 3 sponsorship, 4 card pack, 5 souvenir, 6 choice pack, 7 unlockable, 8 perk, 9 archetype,
# 10 stadium prop, 11 pitch. Every item is drawn by the same card template. Operands that MUST be numbers on a row:
# type_id, is_live_set, captained; slot_type_id on equipment; sub_category_id on unlockables (10000-10008 choose
# the layout; 10005 profile icon reads texture_name_lg, 10006 banner reads texture_name_sm). Pictures are names
# looked up among the disc's CROSS\LIVE\*.WEBP stems and the front-end texture bank; a sponsorship draws its
# brand's logo from brand_id instead. Records come from the catalogue snapshot (and the disc's own item table for
# perks); which pictures exist is checked against the disc, with a generic picture of the kind as the fallback.
ITEM_TYPE = {'Stadium': 1, 'Equipment': 2, 'Sponsorship': 3, 'Unlockable': 7, 'Perk': 8}
ITEM_FILE = {'Stadium': 'stadium', 'Equipment': 'equipment', 'Sponsorship': 'sponsorship', 'Unlockable': 'unlockable', 'Perk': 'perk'}
ITEM_ID_BASE = {'Stadium': 200000, 'Equipment': 210000, 'Sponsorship': 220000, 'Unlockable': 230000, 'Perk': 240000}
STADIUM_ART = {'HUBERT_H_HUMPHREY_METRODOME': 'STADIUM_HUBERT_H_HUMPHREY', 'SOUTH_SIDE_PARK_NLB': 'STADIUM_SOUTHSIDE_PARK_NLB',
               'YANKEE_STADIUM_1976_2008': 'STADIUM_YANKEE_STADIUM_OLD', 'SPORTSMAN_S_PARK': 'STADIUM_SPORTSMANS_PARK'}
SLOT_ART = {0: 'COLLECT_EQUIPMENT_BATS', 1: 'COLLECT_EQUIPMENT_BATTINGGLOVES', 2: 'COLLECT_EQUIPMENT_FIELDINGGLOVES',
            3: 'COLLECT_EQUIPMENT_CLEATS', 4: 'COLLECT_EQUIPMENT_CATCHERMASKS', 5: 'COLLECT_EQUIPMENT_CHESTPROTECTORS',
            6: 'COLLECT_EQUIPMENT_LEGGUARDS', 7: 'COLLECT_EQUIPMENT_ELBOWGUARDS', 8: 'COLLECT_EQUIPMENT_SHINGUARDS',
            9: 'COLLECT_EQUIPMENT_WRISTGUARDS', 10: 'COLLECT_EQUIPMENT_COMPRESSIONSLEEVES', 11: 'COLLECT_EQUIPMENT_BATGRIPS',
            12: 'COLLECT_EQUIPMENT_EYEWEAR', 13: 'COLLECT_EQUIPMENT_RITUALS', 14: 'COLLECT_EQUIPMENT_SOCKS'}
UNLOCKABLE_ART = {10000: 'COLLECT_UNLOCKABLES_ANIMATIONS', 10004: 'COLLECT_UNLOCKABLES_ANIMATIONS', 10001: 'COLLECT_UNLOCKABLES_STADIUMSOUNDS',
                  10002: 'COLLECT_UNLOCKABLES_AUDIOCALLS', 10003: 'COLLECT_UNLOCKABLES_AUDIOCALLS', 10007: 'COLLECT_UNIFORMS',
                  10008: 'COLLECT_UNIFORMS'}
PERK_CATEGORY = {'power': 0, 'contact': 0, 'vision': 0, 'clutch': 0, 'discipline': 0, 'bunt': 0, 'fielding': 1, 'arm': 1,
                 'reaction': 1, 'blocking': 1, 'speed': 2, 'steal': 2, 'baserunning': 2, 'break': 3, 'control': 3, 'velocity': 3,
                 'stamina': 3, 'k/9': 3, 'h/9': 3, 'bb/9': 3, 'hr/9': 3}      # GUESS from the perk's name: 0 hitting, 1 fielding, 2 baserunning, 3 pitching
_inventory = None


def _item_key(value):
    import html, re as _re
    text = html.unescape(str(value)).lower().replace('^r^', '').replace('^t^', '').replace('®', '').replace('™', '')
    return _re.sub(r'[^a-z0-9]', '', text)


def _item_slug(value):
    import html, re as _re
    text = html.unescape(str(value)).replace('®', '').replace('™', '').replace('&reg;', '').replace('&trade;', '')
    return _re.sub(r'[^A-Z0-9]+', '_', text.upper()).strip('_')


def inventory_data():
    """Disc indexes and catalogue records for the non-card item kinds (built once)."""
    global _inventory
    if _inventory is not None:
        return _inventory
    import ast
    import xml.etree.ElementTree as _ET
    disc = LAYOUT.base
    inv = {'brands': {}, 'slots': {}, 'stems': set(), 'perks': {}, 'records': {}, 'catalogue': {}}
    try:
        defs = _ET.parse(os.path.join(disc, 'FRONTEND', 'RTTS', 'INVENTORYDATA.XML'))
        inv['brands'] = {_item_key(x.get('name')): int(x.get('id')) for x in defs.findall('./brands/brand')}
        inv['brands'].update({'underarmour': 7, 'lizardskins': 16, 'lizardskin': 16, '100eyewear': 31, '100percent': 31, 'ariagear': 36,
                              'aria': 36, 'absolutelyridiculous': 36, 'cooperstown': 37, 'cooperstownbatco': 37, 'blast': 38,
                              'blastmotion': 38, 'newera': 39, 'force3': 40, 'force3progear': 40})
        for short, full in (('birdman', 'birdmanbats'), ('chandler', 'chandlerbats'), ('axe', 'axebat')):
            if full in inv['brands']:
                inv['brands'].setdefault(short, inv['brands'][full])
        inv['slots'] = {_item_key(x.get('name')): int(x.get('id')) for x in defs.findall('./slot_types/slot_type')}
        inv['slots'].update({'catchermask': 4, 'catchersmask': 4, 'batgrip': 11, 'batgrips': 11, 'legguard': 6, 'wristguard': 9,
                             'sock': 14, 'cleat': 3})
        for x in defs.findall("./items/item[@type_id='8']"):
            inv['perks'][_item_key(x.get('name'))] = dict(x.attrib)
        inv['stems'] = {f[:-5].upper() for f in os.listdir(os.path.join(disc, 'CROSS', 'LIVE')) if f.upper().endswith('.WEBP')}
        with open(os.path.join(disc, 'PS4', 'UI', 'FRONTEND', 'FRONTEND.TEX'), 'rb') as f:
            head = f.read(0x30)
            if head[:8] == b'IFF0TX00':
                for _ in range(struct.unpack_from('<I', head, 0x14)[0]):
                    inv['stems'].add(f.read(0xa0).split(b'\0', 1)[0].decode('ascii', 'replace').upper())
    except Exception as e:
        print('inventory: disc indexes incomplete (%r)' % (e,))
    if CARD_SOURCE == 'disc' or (LAYOUT.install and not os.path.isdir(CATALOGUE_DIR)):
        _inventory = inv
        return inv
    try:
        snap = CATALOGUE_DIR
        day = os.path.join(snap, sorted(os.listdir(snap))[-1])
        owned = {}
        ownfile = os.path.join(day, 'inventory-owned.json')
        if COLLECTION == 'mine' and os.path.exists(ownfile):
            owned = json.load(open(ownfile, encoding='utf-8'))['items']
        inv['equip_ownership_known'] = COLLECTION != 'mine' or bool(owned)
        for kind, short in ITEM_FILE.items():
            have = {r['uuid']: r for r in owned.get(short, [])} if owned else None
            rows = []
            catalogue = []
            path = os.path.join(day, 'items-%s.json' % short)
            if os.path.exists(path):
                for n, r in enumerate(sorted(json.load(open(path, encoding='utf-8')), key=lambda r: r['uuid'])):
                    item = dict(r, item_id=ITEM_ID_BASE[kind] + n, quantity=1, no_sell=0)
                    if kind == 'Equipment':
                        owned_record = have.get(r['uuid'], {}) if have is not None else {}
                        item['equip_no_sell'] = owned_record.get('no_sell', r.get('no_sell'))
                    catalogue.append(item)
                    if have is None or r['uuid'] in have:
                        qty = int(have[r['uuid']].get('qty', 1)) if have is not None else 1
                        rows.append(dict(item, quantity=qty,
                                         no_sell=1 if have is not None and str(have[r['uuid']].get('no_sell')) == 'True' else 0))
            elif have is not None:                       # perks: no catalogue file; the owned row's first cell is the name
                for n, r in enumerate(sorted(have.values(), key=lambda r: r['uuid'])):
                    cells = r.get('cells')
                    cells = ast.literal_eval(cells) if isinstance(cells, str) else (cells or [])
                    if cells:
                        rows.append({'uuid': r['uuid'], 'name': cells[0], 'rarity': 'Common', 'item_id': ITEM_ID_BASE[kind] + n,
                                     'quantity': int(r.get('qty', 1)), 'no_sell': 1 if str(r.get('no_sell')) == 'True' else 0})
            inv['records'][kind] = rows
            inv['catalogue'][kind] = catalogue or rows
    except Exception as e:
        print('inventory: catalogue records incomplete (%r)' % (e,))
    _inventory = inv
    return inv


def inventory_item(kind, r, **extra):
    """One <item .../> of a non-card kind, or None when a required operand cannot be given."""
    import ast, html
    inv = inventory_data()
    stems = inv['stems']
    name = html.unescape(str(r['name'])).replace('®', '').replace('™', '')
    a = {'id': r['item_id'], 'item_id': r['item_id'], 'uuid': r['uuid'], 'type_id': ITEM_TYPE[kind], 'name': name,
         'rarity_id': RARITY_ID.get(str(r.get('rarity', 'Common')).title(), 0), 'quantity': r['quantity'], 'no_sell': r.get('no_sell', 0),
         'is_live_set': 0, 'captained': 0, 'captain': 0, 'is_discovered': 0, 'has_rank_change': 0, 'in_squad': 0}
    if r.get('description'):
        a['description'] = html.unescape(str(r['description']))
    slug = _item_slug(r['name'])
    field, candidates = 'texture_name', []
    if kind in ('Equipment', 'Sponsorship'):
        brand = html.unescape(str(r.get('brand', ''))).replace('®', '').strip()
        a['brand_name'] = brand
        brand_id = inv['brands'].get(_item_key(brand))
        if brand_id is not None:
            a['brand_id'] = brand_id
    if kind == 'Stadium':
        for src, dst in (('built', 'built'), ('surface', 'display_grass'), ('elevation', 'display_elevation'), ('capacity', 'display_capacity')):
            if r.get(src) not in (None, ''):
                a[dst] = r[src]
        nlb = slug[4:] + '_NLB' if slug.startswith('NLB_') else slug
        candidates = [STADIUM_ART.get(nlb), 'STADIUM_' + nlb, 'COLLECT_STADIUMS_CLASSIC']
    elif kind == 'Equipment':
        slot = inv['slots'].get(_item_key(r.get('slot', '')))
        if slot is None:
            return None                                  # slot_type_id is an operand of the card template
        a['slot_type_id'], a['slot_type_name'], a['is_official'] = slot, r.get('slot', ''), 0
        lists = []
        for k in ('attribute_names', 'attribute_values'):
            v = r.get(k)
            try:
                v = ast.literal_eval(v) if isinstance(v, str) and v.strip().startswith('[') else (v or [])
            except (ValueError, SyntaxError):
                v = []
            lists.append(list(v) if isinstance(v, (list, tuple)) else [])
        pairs = list(zip(*lists))
        a['total_attributes'] = len(pairs)
        for i, (label, value) in enumerate(pairs):
            # The value must be a bare NUMBER: the panel formats it with COLOR_NEG (0xa1b0b3 inside 0xa1a970) and adds
            # the sign itself. The catalogue writes "+9"; sent as it was, that crashed Team Equipment in run 94.
            try:
                number = int(str(value).replace('+', '').strip())
            except ValueError:
                number = 0
            a['attribute_%d_short_name' % i], a['attribute_%d_value' % i] = label, number
        prefix = {0: 'BATSKIN_', 13: 'RITUAL_', 14: 'SOCK_'}.get(slot)
        candidates = [slug, prefix + slug if prefix else None, 'BAT3_' + slug if slot == 0 else None, SLOT_ART.get(slot),
                      'COLLECT_STARTER_EQUIPMENT']
    elif kind == 'Sponsorship':
        if r.get('bonus'):
            a['bonus_0_name'] = r['bonus']
        if 'brand_id' not in a:
            return None                                  # the face is the brand's logo; without it there is nothing to draw
    elif kind == 'Unlockable':
        try:
            sub = int(r['sub_category_id'])
        except (KeyError, TypeError, ValueError):
            return None                                  # sub_category_id is the SUBINT operand
        a['sub_category_id'] = sub
        a.update(equip_vanity(r, inv))
        if str(r.get('category_id', '')).lstrip('-').isdigit():
            a['category_id'] = int(r['category_id'])
        if sub == 10005:
            field, candidates = 'texture_name_lg', ['ICON_' + slug + '_LG', 'ICON_8BIT_LG']
        elif sub == 10006:
            field, candidates = 'texture_name_sm', ['PROFILEBANNER_' + slug + '_SM', 'PROFILEBANNER_80S_RETRO_SM']
        else:
            candidates = ['UNI_' + slug if sub in (10007, 10008) else None, slug, UNLOCKABLE_ART.get(sub), 'COLLECT_STARTER_UNLOCKABLE']
    else:                                                # a perk: the disc's own definition supplies picture and text
        d = inv['perks'].get(_item_key(r['name']), {})
        if d.get('description') and 'description' not in a:
            a['description'] = d['description']
        if d.get('rarity_id', '').isdigit():
            a['rarity_id'] = int(d['rarity_id'])
        words = _item_key(a.get('description', '') + ' ' + name)
        a['attribute_category'] = next((v for k, v in PERK_CATEGORY.items() if _item_key(k) in _item_key(name)),
                                       next((v for k, v in PERK_CATEGORY.items() if _item_key(k) in words), 0))
        import re as _re
        plain = _re.sub(r'_(I|II|III|IV|V)$', '', slug)                # "Break III" uses the picture perk_icon_break
        level = {'I': 1, 'II': 2, 'III': 3}.get(slug[len(plain) + 1:])  # GUESS: level I bronze, II silver, III gold
        if level and not d.get('rarity_id', '').isdigit():
            a['rarity_id'] = level
        candidates = [d.get('texture_name'), 'PERK_ICON_' + slug, 'PERK_ICON_' + plain, 'COLLECT_UTILITY_PERKS']
    if candidates:
        pick = extra.get(field) or a.get(field) or next((s for s in candidates if s and s.upper() in stems), None)
        if pick is None:
            return None
        a[field] = pick
    esc = lambda v: str(v).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')
    a.update(extra)
    return '<item %s/>' % ' '.join('%s="%s"' % (k, esc(v)) for k, v in a.items())


def owned_inventory_rows(kind, state=None):
    inventory = inventory_data()
    base = inventory['records'].get(kind, [])
    if state is None:
        state = load_state()
    gains = state.get('pack_shop', {}).get('gained_items', {}).get(kind, {})
    if not gains:
        return base
    by_uuid = {row['uuid']: row for row in base}
    for row in inventory['catalogue'].get(kind, []):
        card_id = row['uuid']
        gained = gains.get(card_id, 0)
        if gained:
            original = by_uuid.get(card_id)
            by_uuid[card_id] = dict(original or row, quantity=(original['quantity'] if original else 0) + gained,
                                    no_sell=original.get('no_sell', 0) if original else 1)
    return list(by_uuid.values())


# Definition-backed equipment. These are JSON model types, not inventory slot IDs.
EQUIP_MODEL_TYPE = (0, 1, 3, 2, 10, 11, 15, 9, 12, 14, 13, 8, 7, None, None)
EQUIP_ALLOW_NEUTRAL = True
EQUIP_STOCK = os.path.join(LAYOUT.base, 'FRONTEND')


class Wearable(NamedTuple):
    slot: int
    fields: dict
    stats: list
    matched: bool


def equip_values(record, attr_ids):
    """Pair real display labels with the native stat IDs and signed integer values."""
    import ast
    def sequence(k):
        value = record.get(k, [])
        if isinstance(value, str):
            value = ast.literal_eval(value) if value.strip() else []
        if not isinstance(value, (list, tuple)):
            raise ValueError('invalid equipment attribute list')
        return value

    if 'attribute_names' not in record or 'attribute_values' not in record:
        return None
    try:
        labels, values = sequence('attribute_names'), sequence('attribute_values')
        if len(labels) != len(values) or len(values) > 5:
            return None
        stats = []
        for label, value in zip(labels, values):
            if not re.fullmatch(r'[+-]?[0-9]+', str(value).strip()):
                return None
            number = int(value)
            # The team-slot copy sign-extends the low byte of each attribute value.
            if not -128 <= number <= 127:
                return None
            idx = attr_ids.get(_item_key(label))
            if idx is None:
                return None
            stats.append((idx, str(label), number))
        return stats
    except (SyntaxError, ValueError, TypeError):
        return None


def equip_registry():
    inv = inventory_data()
    cache_key = ('equip_registry', EQUIP_ALLOW_NEUTRAL)
    if cache_key in inv:
        return inv[cache_key]
    empty = {'equipment':{}, 'uniforms':{}, 'stock_perks':{}, 'native_ids':set(),
             'version':None, 'xml':'<response/>', 'hidden':{}}
    if not (inv.get('catalogue', {}).get('Equipment') or inv['records'].get('Equipment')):
        return empty
    try:
        info = build_equip_registry(inv)
    except (OSError, ET.ParseError, ValueError, KeyError, TypeError) as error:
        print(f'equipment definitions unavailable ({type(error).__name__})', file=sys.stderr)
        return empty
    inv[cache_key] = info
    return info


# The .PS4 option rows link a native appearance property/style to a named TEX
# descriptor. The equipment inventory slot enum is different from both.
FACE_PROPERTIES = (278, 293, 266, 379, 397, 407, 412, 304, 313, 341, 346, 284, 325)
FACE_GENERICS = ('GENERIC_BAT_1','GENERIC_BG_1','GENERIC_FG_1','GENERIC_CLEAT_1',
                 'GENERIC_CM_TRADITIONAL','GENERIC_CP_1','GENERIC_LG_1','GENERIC_EG_SMALL',
                 'GENERIC_SHING_1','GENERIC_WG_1','GENERIC_CS_1','GENERIC_GRIP_1',
                 'GENERIC_SG_1','RITUAL_COIN','SOCK_STANCE_THESHOW23')


def face_key(value):
    import html
    return _item_key(re.sub(r'\^[A-Za-z]\^', '', html.unescape(str(value))))


def face_hash(text):
    h = 0
    for ch in text.lower():
        h = ((h << 8 | h >> 24) & 0xffffffff) ^ ord(ch)
    return h | 0x80000000


def face_model_codes(text):
    return {w.lower() for w in re.findall(r'[A-Za-z0-9]+',str(text))
            if len(w) >= 3 and any(c.isdigit() for c in w) and any(c.isalpha() for c in w)}


def face_model_key(text):
    # Only strip a colour suffix. A different model with the same brand stays distinct.
    return face_key(re.sub(r'(?:[\s(/,\-]+(?:black|white|navy|blue|red|grey|gray|green|yellow|pink|purple|orange|tan|brown)\b[)\s]*)+$',
                           '', str(text), flags=re.I))


class FaceFile(NamedTuple):
    path: str
    descriptor: bytes
    surface: bytes
    header: bytes
    offset: int
    size: int


def face_bank(path):
    """Index the v7 PS4 one-surface-per-image layout used by CreatePlayerOptions."""
    with open(path, 'rb') as f:
        head = f.read(48)
        if len(head) != 48 or head[:8] != b'IFF0TX00' or head[16:20] != b'PS4\0':
            raise ValueError('unexpected equipment texture bank')
        version, = struct.unpack_from('<I', head, 8)
        count, reserved, surfaces, packed, raw = struct.unpack_from('<5I', head, 20)
        data_start = 48 + 200 * count
        if version != 7 or reserved or surfaces != count or not 0 < count < 8192 or data_start + packed != os.fstat(f.fileno()).st_size:
            raise ValueError('unsupported equipment bank layout')
        descriptors = [f.read(160) for _ in range(count)]
        result = {}
        for desc in descriptors:
            surf = f.read(40)
            name = desc[:64].split(b'\0')[0].decode('ascii').upper()
            size, pos, raw_pos, raw_size = struct.unpack_from('<4I', desc, 0x50)
            if (pos + size > packed or raw_pos + raw_size > raw or size == 0
                    or struct.unpack_from('<I',desc,0x74)[0] != 1
                    or struct.unpack_from('<II',surf) != (0, raw_size)):
                raise ValueError('inconsistent equipment texture descriptor')
            result[name] = FaceFile(path, desc, surf, head, data_start + pos, size)
    return result


def face_payload(src):
    if not 0 < src.size <= 512*1024:
        raise ValueError('outside equipment picture budget')
    with open(src.path, 'rb') as f:
        f.seek(src.offset)
        body = f.read(src.size)
    if len(body) != src.size:
        raise ValueError('incomplete equipment picture')
    if not src.descriptor:
        if body[:4] != b'RIFF' or body[8:12] != b'WEBP':
            raise ValueError('invalid equipment WebP')
        return body, '.webp'
    # Rebase the two bank-relative payload offsets. Preserve the original native
    # descriptor, surface format, swizzle, and compressed pixels.
    head, desc = bytearray(src.header), bytearray(src.descriptor)
    pad = (-len(body)) % 16
    raw_size, = struct.unpack_from('<I', desc, 0x5c)
    struct.pack_into('<5I', head, 20, 1, 0, 1, len(body) + pad, raw_size)
    struct.pack_into('<II', desc, 0x54, 0, 0)
    return bytes(head + desc + src.surface + body + bytes(pad)), '.tex'


def face_options(path, stems):
    """Yield slot, style, original option label and its actual texture stem."""
    with open(path, 'rb') as f:
        data = f.read()
    base = data.index(b'VOIDASS0') + 16
    def ptr(at):
        return struct.unpack_from('<Q',data,at)[0]
    props = base + ptr(base+16)
    start, end = base + ptr(base+64), base + ptr(base+72)
    if not (props < start <= end <= len(data)) or (end-start) % 16:
        raise ValueError('unexpected equipment option table')
    hashes = {face_hash(s):s for s in stems}
    for tex, pair in struct.iter_unpack('<QQ',data[start:end]):
        prop, style = pair & 0xffff, pair >> 16
        if prop not in FACE_PROPERTIES or tex not in hashes:
            continue
        node = props + prop*88
        table = node + struct.unpack_from('<q',data,node+48)[0]
        pos = base + ptr(table+8*style)
        label = data[pos:data.index(b'\0',pos)].decode('utf-8', errors='replace')
        yield FACE_PROPERTIES.index(prop), style, label, hashes[tex]


def equipment_face_index(disc, update):
    sources, exact, variants, codes, brands = {}, {}, {}, {}, {}
    aliases = {'ls':'louisvilleslugger','rawling':'rawlings',
               'ua':'underarmour','underarmor':'underarmour','cooperstownbatco':'cooperstown',
               'cooperstownbat':'cooperstown','birdmanbats':'birdman','chandlerbats':'chandler',
               'dingerbats':'dinger','axebat':'axe','p100':'100percent','100eyewear':'100percent'}
    def brand(name):
        k = face_key(name)
        return aliases.get(k,k)
    def add(table, key, stem):
        table.setdefault(key, []).append(stem)
    def label(slot, maker, text, stem):
        k = face_key(text)
        if not k or k.startswith('tba') or 'placeholder' in k or stem.startswith('GENERIC_'):
            return
        for word in {k,k.removeprefix(maker) if maker else k,k.removeprefix('batskin')} - {''}:
            add(exact, (slot, maker, word), stem)
            add(exact, (slot, '', word), stem)
        if maker:
            add(variants, (slot,maker,face_model_key(text).removeprefix(maker)), stem)
            for code in face_model_codes(text):
                add(codes,(slot,maker,code),stem)
            add(brands, (slot,maker), stem)

    # Sources are discovered under fixed package paths. The request never supplies a path.
    editions = []
    for is_update, root in ((False,disc),(True,update)):
        if root is None:
            continue
        folder = os.path.join(root,'CROSS','LIVE')
        try:
            filenames = sorted(os.listdir(folder))
        except OSError:
            filenames = []
        loose = set()
        for filename in filenames:
            stem, ext = os.path.splitext(filename.upper())
            if ext != '.WEBP' or not stem.startswith(('BATSKIN_', 'BAT3_', 'SOCK_', 'RITUAL_')):
                continue
            slot = 14 if stem.startswith('SOCK_') else 13 if stem.startswith('RITUAL_') else 0
            loose.add(stem)
            path = os.path.join(folder,filename)
            sources.setdefault(stem,(slot, is_update, path if is_update else None))
        ui = os.path.join(root,'PS4','UI','CREATEPLAYEROPTIONS')
        try:
            bank = face_bank(ui+'.TEX')
            opts = list(face_options(ui+'.PS4', bank))
        except (OSError, ValueError, struct.error):
            bank, opts = {}, []
        try:
            with open(os.path.join(root,'FRONTEND','EQUIPMENT','MLBEQUIPMENT.JSON'), encoding='utf-8-sig') as f:
                models = json.load(f)
        except (OSError,ValueError):
            models = {}
        makers = {r['id']:brand(r['name']) for r in models.get('makers',[])}
        rows = {(r['type'],r['style']):r for r in models.get('eqlist',[]) if r.get('enb')}
        for slot, style, title, stem in opts:
            sources.setdefault(stem,(slot, is_update, bank[stem]))
            row = rows.get((EQUIP_MODEL_TYPE[slot],style))
            maker = makers.get(row['brand'],'') if row else ''
            label(slot,maker,title,stem)
            if row and face_key(row['name']) != 'tbd':
                label(slot,maker,row['name'],stem)
        editions.append(loose)
    # Loose skin names are their own item pictures, even when the create-player
    # model table points several skins to the same generic thumbnail.
    loose_names = {}
    for stem in sorted(set.union(*editions)):
        slot = sources[stem][0]
        tail = stem.split('_',1)[1]
        key = face_key(tail)
        loose_names[slot,key] = stem
        if stem.startswith('SOCK_STANCE_'):
            loose_names[slot,key.removeprefix('stance')] = stem
            add(brands,(slot,'stance'),stem)
        for token in ('louisvilleslugger','victus','rawlings','cooperstown','dinger','ls'):
            if key.startswith(token):
                maker = brand(token)
                shortened = key[len(token):]
                if shortened:
                    add(exact,(slot,maker,shortened),stem)
                    add(brands,(slot,maker),stem)
                break
    return sources, loose_names, exact, variants, codes, brands, brand


def equipment_face_choices(row, index, slot):
    sources, loose, exact, variants, codes, brands, brand_key = index
    name = row['name']
    key = face_key(name).removeprefix('batskin')
    maker = brand_key(row.get('brand',''))
    if maker in ('', 'batskin', 'batskins'):
        # Some skins list "Bat Skin" as their brand; use a spelled-out maker
        # only when the item name actually starts with that brand.
        candidates = [b for s,b in brands if s == slot and key.startswith(b)]
        if candidates:
            maker = max(candidates, key=len)
    def own(stem):
        return 'own_update' if sources[stem][1] else 'own_disc'
    for field in ('texture_name','feimage'):
        stem = str(row.get(field,'')).strip().upper().removesuffix('.WEBP').removesuffix('.TEX')
        if stem in sources and sources[stem][0] == slot:
            yield stem, own(stem)
    keys = [key, key.removeprefix(maker) if maker else key]
    if slot == 0 and 'cityconnect' in key:
        tail = key.replace('cityconnect','').removeprefix('nike').removesuffix('nike')
        for abbr,city,team in MLB_TEAMS:
            short = {'ARI':'AZ','CHW':'CWS','WAS':'WSH'}.get(abbr,abbr)
            if tail in (face_key(city+team),face_key(team),face_key(abbr)):
                keys.insert(0,'cityconnect'+short.lower())
    if slot == 13 and key == 'goodlucktiki':
        keys.insert(0,'tiki')
    for k in keys:
        stem = loose.get((slot,k))
        if stem:
            yield stem, own(stem)
    ambiguous = []
    for k in keys:
        matches = list(dict.fromkeys(exact.get((slot,maker,k), []))) if maker else []
        if not matches:
            possible = set(exact.get((slot,'',k), []))
            if maker and (slot,maker) in brands:
                possible.intersection_update(brands[slot,maker])
            matches = sorted(possible) if len(possible) == 1 else []
        if len(matches) == 1:
            yield matches[0], own(matches[0])
        else:
            ambiguous.extend(matches)
    for stem in dict.fromkeys(ambiguous):
        yield stem, 'model_variant'
    model = face_model_key(name).removeprefix(maker)
    if maker and model:
        for stem in sorted(set(variants.get((slot,maker,model), []))):
            yield stem, 'model_variant'
        code_sets = [set(codes[slot,maker,c]) for c in face_model_codes(name) if (slot,maker,c) in codes]
        if code_sets:
            for stem in sorted(set.intersection(*code_sets)):
                yield stem, 'model_variant'
    if maker:
        for stem in sorted(set(brands.get((slot,maker), []))):
            yield stem, 'same_brand'
    generic = FACE_GENERICS[slot]
    if generic in sources:
        yield generic, 'slot_generic'


def equipment_faces():
    inv = inventory_data()
    if 'equipment_faces' in inv:
        return inv['equipment_faces']
    index = equipment_face_index(os.path.dirname(EQUIP_STOCK), UPDATE_DIR)
    sources = index[0]
    registry = {'entries':[], 'files':{}, 'choices':{}}
    records = {r['uuid']:r for r in inv.get('catalogue',{}).get('Equipment',[])}
    records.update((r['uuid'],r) for r in inv['records'].get('Equipment',[]))
    ids = {stem:61000000+i for i,stem in enumerate(sorted(sources))}
    ready, failed = set(), set()
    for uid, row in sorted(records.items()):
        raw_slot = _item_key(row.get('slot',''))
        slot = 0 if raw_slot in ('batskin','batskins') else inv['slots'].get(raw_slot)
        if slot not in range(15):
            continue
        for stem, reason in equipment_face_choices(row,index,slot):
            if stem in failed:
                continue
            source = sources[stem][2]
            if source is not None and stem not in ready:
                try:
                    if isinstance(source,str):
                        size = os.path.getsize(source)
                        if size > 512*1024:
                            raise ValueError('outside art ZIP budget')
                        source = FaceFile(source,b'',b'',b'',0,size)
                    payload, ext = face_payload(source)
                    if not register_picture(registry,stem,ids[stem],f'equip-{ids[stem]}',payload,ext):
                        raise ValueError('outside art ZIP budget')
                except (OSError,ValueError):
                    failed.add(stem)
                    continue
                ready.add(stem)
            registry['choices'][uid] = (stem, reason, slot)
            break
    inv['equipment_faces'] = registry
    return registry


def equipment_face_coverage():
    """Slot counts over owned identities and all catalogue faces; no persistent writes."""
    inv = inventory_data()
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    owned = {r['uuid'] for r in equip_owned_rows('Equipment',state if isinstance(state,dict) else {})
             if r['quantity'] > 0}
    choices = equipment_faces()['choices']
    records = {r['uuid']:r for r in inv.get('catalogue',{}).get('Equipment',[])}
    records.update((r['uuid'],r) for r in inv['records'].get('Equipment',[]))
    result = {}
    for uid,r in records.items():
        _, reason, slot = choices.get(uid, ('','unavailable',inv['slots'].get(_item_key(r.get('slot','')),-1)))
        counts = result.setdefault(slot, {'catalogue':{}, 'owned':{}})
        for scope in ('catalogue','owned') if uid in owned else ('catalogue',):
            counts[scope][reason] = counts[scope].get(reason,0) + 1
    return dict(sorted(result.items()))


def equip_description(text):
    import html
    clean = re.sub(r'\s*(?:<br\s*/?>|</p>\s*<p[^>]*>|[\r\n]+)\s*', ' ', html.unescape(text), flags=re.I)
    clean = re.sub(r'</?p(?:\s[^>]*)?>', '', clean, flags=re.I)
    return re.sub(r'(warm[- ]up partner)\s*(?=Happy Father)', r'\1. ', clean, flags=re.I).strip()


def equip_face(kind, record, **fields):
    try:
        if kind == 'Equipment':
            choice = equipment_faces()['choices'].get(record['uuid'])
            if choice is None:
                return None
            fields['texture_name'] = choice[0]
        xml = inventory_item(kind, record, **fields)
        return ET.fromstring(xml) if xml else None
    except (ValueError, ET.ParseError):
        return None


def build_equip_registry(inv):
    stock = ET.parse(os.path.join(EQUIP_STOCK, 'RTTS', 'INVENTORYDATA.XML')).getroot()
    with open(os.path.join(EQUIP_STOCK, 'EQUIPMENT', 'MLBEQUIPMENT.JSON'), encoding='utf-8') as f:
        models = json.load(f)
    art_types = stock.find('attribute_types')
    stat_labels = {int(e.get('id')):(e.get('name'),e.get('short_name')) for e in art_types}
    attrs = {_item_key(e.get(k)): int(e.get('id')) for e in art_types for k in ('name','short_name')
             if e.get(k) and int(e.get('id')) >= 0}
    attrs.update({'clutch':5, 'clt':5, 'bcl':5, 'pcl':21, 'vis':8, 'vision':8, 'platevis':8,
                  'battingcontact':1, 'battingpower':3, 'discipline':9, 'battingdiscipline':9,
                  'baserunningaggressiveness':14, 'bragg':14, 'aggressiveness':14,
                  'stealing':13, 'steal':13, 'fielding':17, 'reaction':18,
                  'dragbunt':7, 'bunt':6, 'accuracy':16, 'armacc':16, 'armstr':15})
    def brand_key(name):
        key = _item_key(name)
        return inv['brands'].get(key, key)
    makers = {r['id']: brand_key(r['name']) for r in models['makers']}
    by_model = {}
    neutral_types = set()
    for model in models['eqlist']:
        if not model['enb']:
            continue
        if model['brand'] == 0 and model['style'] == 0:
            neutral_types.add(model['type'])
        name = _item_key(model['name'])
        if name and name != 'tbd':
            k = model['type'], makers.get(model['brand']), name
            by_model.setdefault(k, set()).add(model['style'])

    info = {'equipment': {}, 'uniforms': {}, 'native_ids': set(), 'hidden': {},
            'items': stock.find('items')}
    all_rows = inv.get('catalogue', {})
    # Definitions include potentially grantable pack items. Owning a new pack item
    # changes the quantity response, never the meaning of a cached definition.
    records = {r['uuid']:r for r in all_rows.get('Equipment', [])}
    records.update((r['uuid'], r) for r in inv['records'].get('Equipment', []))
    for record in sorted(records.values(), key=lambda r: r['item_id']):
        is_bat_skin = _item_key(record.get('slot', '')) in ('batskin', 'batskins')
        slot = 0 if is_bat_skin else inv['slots'].get(_item_key(record.get('slot', '')))
        stats = equip_values(record, attrs)
        if slot is None or stats is None:
            info['hidden'][record['uuid']] = 'slot' if slot is None else 'stats'
            continue
        typ = EQUIP_MODEL_TYPE[slot]
        candidate = _item_key(record['name'])
        brand = _item_key(record.get('brand', ''))
        styles = by_model.get((typ, brand_key(brand), candidate), set())
        if not styles and brand and candidate.startswith(brand):
            styles = by_model.get((typ, brand_key(brand), candidate[len(brand):]), set())
        matched = len(styles) == 1
        sock = record.get('sock_type_id')
        if slot == 14 and str(sock).isascii() and str(sock).isdigit() and int(sock) in range(18):
            matched, style, sock = True, 0, int(sock)
        else:
            style = next(iter(styles)) if matched else 0
            sock = 0
        if not matched and not (EQUIP_ALLOW_NEUTRAL and (typ in neutral_types or slot == 14)):
            # Ritual has no drawable neutral model. Do not guess one.
            info['hidden'][record['uuid']] = 'model'
            continue
        face_record = dict(record, slot='Bat') if is_bat_skin else record
        face = equip_face('Equipment', face_record)
        if face is None:
            info['hidden'][record['uuid']] = 'face'
            continue
        fields = dict(slot_type_id=str(slot), servoy_equipment_id=str(style), sock_type_id=str(sock),
                      total_attributes=str(len(stats)))
        fields['description'] = equip_description(face.get('description', ''))
        fields['slot_type_name'] = face.get('slot_type_name')
        fields['style_name'] = record.get('style_name') or record['name']
        skin_label = any(_item_key(record.get(k,'')) in ('batskin','batskins')
                         for k in ('equipment_type','sub_category_name','type'))
        exact_skin = (face.get('texture_name','').upper().startswith('BATSKIN_')
                      and equipment_faces()['choices'][record['uuid']][1] in ('own_disc','own_update'))
        if slot == 0 and (is_bat_skin or skin_label or face.get('brand_id') == '19' or exact_skin):
            fields['brand_name'] = 'Bat Skin'
        else:
            fields['brand_name'] = face.get('brand_name', '')
        for i in range(5):
            idx, label, number = stats[i] if i < len(stats) else (-1, '', 0)
            full, short = stat_labels[idx] if idx >= 0 else ('', '')
            fields.update({f'attribute_{i}_id':str(idx), f'attribute_{i}_value':str(number),
                           f'attribute_{i}_name':full or label, f'attribute_{i}_short_name':short or label})
        iid = int(record['item_id'])
        native = {k:face.get(k,'') for k in ('id','type_id','rarity_id','name','description',
                                             'texture_name','is_official')}
        native.update(brand_id=face.get('brand_id','-1'), style_name=record['name'], is_new='0')
        native.update(fields)
        ET.SubElement(info['items'], 'item', native)
        info['equipment'][record['uuid']] = Wearable(slot, fields, stats, matched)
        info['native_ids'].add(iid)
    equip_other_definitions(info, inv, stock)
    stock.find('info').set('version', '')
    info['version'] = hashlib.sha256(ET.tostring(stock, encoding='utf-8')).hexdigest()
    stock.find('info').set('version', info['version'])
    info['xml'] = ET.tostring(stock, encoding='unicode')
    del info['items']
    return info


def equip_uniform_indexes():
    with open(MLB_UNIS, encoding='utf-8') as f:
        kits = json.load(f)['uniforms']
    by_pair, by_name = {}, {}
    for kit in kits:
        if kit['hideInFE'] or kit['caponly']:
            continue
        pair = kit['gameid'], kit['id']
        stem = ('UNI_' + kit['teamabb'] + '-' + kit['assetname']).upper()
        by_pair[pair] = stem
        keys = [stem]
        for club in (kit['teamabb'], kit['teamname']):
            for kit_name in (kit['dpy_name'], kit['assetname']):
                keys.extend((club + kit_name, club + str(kit['year']) + kit_name,
                             club + kit_name + str(kit['year'])))
        for k in keys:
            by_name.setdefault(_item_key(k), set()).add(pair)
    return by_pair, by_name


def equip_uniform(record, indexes):
    by_pair, by_name = indexes
    try:
        sub = int(record['sub_category_id'])
    except (KeyError, ValueError, TypeError):
        return None
    if sub not in (10007, 10008) and _item_key(record.get('sub_category_name','')) not in ('uniform','uniforms'):
        return None
    if 'unlock_id' in record and 'unlock_id2' in record:
        try:
            pair = int(record['unlock_id']), int(record['unlock_id2'])
        except (ValueError, TypeError):
            return None
        return (pair, by_pair[pair]) if pair in by_pair else None
    pair_set = None
    for text in (record.get('texture_name',''), record['name'],
                 str(record.get('team','')) + record['name']):
        hits = by_name.get(_item_key(text))
        if hits:
            pair_set = hits if pair_set is None else pair_set & hits
    if not pair_set or len(pair_set) != 1:
        return None
    pair, = pair_set
    return pair, by_pair[pair]


def equip_vanity(record, inv):
    sub = str(record.get('sub_category_id', ''))
    if sub not in ('10005', '10006'):
        return {}
    if '_vanity_art' not in inv:
        stems = inv['stems']
        by_name = {}
        for prefix, suffix in (('ICON_', '_LG'), ('PROFILEBANNER_', '_SM')):
            for stem in stems:
                if stem.startswith(prefix) and stem + suffix in stems:
                    k = prefix, _item_key(stem[len(prefix):])
                    by_name[k] = stem if k not in by_name else None
        inv['_vanity_art'] = by_name
    prefix, suffix, label, field = (('ICON_', '_LG', 'Icons', 'texture_name_lg') if sub == '10005'
                                    else ('PROFILEBANNER_', '_SM', 'Banners', 'texture_name_sm'))
    raw = str(record.get('texture_name') or record.get(field) or '').upper()
    stem = raw.removesuffix(suffix)
    if not (stem.startswith(prefix) and stem in inv['stems'] and stem + suffix in inv['stems']):
        name = _item_key(record['name'])
        names = (name, name.removesuffix('icon' if sub == '10005' else 'banner'))
        stem = next((inv['_vanity_art'][prefix, n] for n in names if inv['_vanity_art'].get((prefix,n))), None)
    if not stem:
        return {}
    return {'texture_name':stem, field:stem+suffix, 'sub_category_name':label}


def equip_other_definitions(info, inv, stock):
    try:
        kits = equip_uniform_indexes()
    except (OSError, ValueError, KeyError):
        kits = {}, {}
    bonuses = {_item_key(e.get(k)):e.get('id') for e in stock.findall('./partnership_bonuses/partnership_bonus')
               for k in ('name','short_name') if e.get(k)}
    perks = {_item_key(e.get('name')):e.get('id') for e in stock.findall("./items/item[@type_id='8']")}
    info['stock_perks'] = {}
    for kind in ('Unlockable','Sponsorship','Perk'):
        rows = {r['uuid']:r for r in inv.get('catalogue', {}).get(kind, [])}
        rows.update((r['uuid'],r) for r in inv['records'].get(kind, []))
        for row in sorted(rows.values(), key=lambda r: r['item_id']):
            if kind == 'Perk':
                perk_id = perks.get(_item_key(row['name']))
                if perk_id:
                    info['stock_perks'][row['uuid']] = perk_id
                continue
            if kind == 'Sponsorship':
                bonus_id = bonuses.get(_item_key(row.get('bonus','')))
                if not bonus_id:
                    continue
                extra = {'bonus_ids':bonus_id}
            else:
                kit = equip_uniform(row, kits)
                if kit is None:
                    continue
                (club, asset), stem = kit
                extra = {'unlock_id':str(club), 'unlock_id2':str(asset), 'sub_texture_name':stem}
                if stem in inv['stems']:
                    extra['texture_name'] = stem
            face = equip_face(kind, row, **extra)
            if face is not None:
                if kind == 'Unlockable':
                    info['uniforms'][row['uuid']] = extra
                    if not str(row.get('category_id','')).isdigit():
                        # Wardrobe resolves the row itself. Only install complete type-7 definitions.
                        continue
                info['items'].append(face)
                info['native_ids'].add(row['item_id'])


def equip_templates(params):
    query = params_of(params)
    if not isinstance(query, dict) or query.get('mode') != 'templates':
        return '<response/>'
    if 'Equipment' not in inventory_data()['records']:
        return '<response/>'
    reg = equip_registry()
    return '<response/>' if query.get('version') == reg['version'] else reg['xml']


def equip_owned_rows(kind, state):
    inv = inventory_data()
    if inv.get('equip_ownership_known', True):
        return owned_inventory_rows(kind, state)
    # In personal mode the legacy browse path falls back to a catalogue if its
    # ownership capture is unavailable. Only newly earned grants prove ownership here.
    gains = state.get('pack_shop', {}).get('gained_items', {}).get(kind, {})
    return [dict(r, quantity=gains[r['uuid']], no_sell=1) for r in inv['catalogue'].get(kind, [])
            if gains.get(r['uuid'], 0) > 0]


def equip_quantities(params):
    if 'Equipment' not in inventory_data()['records']:
        return '<response/>'
    reg = equip_registry()
    if reg['version'] is None:
        return '<response/>'
    try:
        root = ET.parse(os.path.join(EQUIP_STOCK, 'RTTS', 'INVQUANTITYDATA.XML')).getroot()
    except (OSError, ET.ParseError):
        return '<response/>'
    items = root.find('items')
    stock_rows = {e.get('id'):e for e in items.findall('item')}
    state = load_state()
    for kind in ('Equipment','Unlockable','Perk','Sponsorship'):
        for row in equip_owned_rows(kind, state):
            perk_id = reg['stock_perks'].get(row['uuid']) if kind == 'Perk' else None
            if perk_id and perk_id in stock_rows:
                existing = stock_rows[perk_id]
                existing.set('quantity', str(max(int(existing.get('quantity', '0')), row['quantity'])))
            elif row['quantity'] > 0 and row['item_id'] in reg['native_ids']:
                ET.SubElement(items, 'item', id=str(row['item_id']), quantity=str(row['quantity']), is_new='0')
    return ET.tostring(root, encoding='unicode')


def equip_owned(state):
    reg = equip_registry()['equipment']
    rows = [(r, reg[r['uuid']]) for r in equip_owned_rows('Equipment', state)
            if r['quantity'] > 0 and r['uuid'] in reg]
    if not LAYOUT.install and os.environ.get('SHAD_SCOUT_EQUIP_ONE_BAT') == '1':
        bats = [pair for pair in rows if pair[1].slot == 0]
        return sorted(bats, key=lambda p: (not p[1].matched, p[0]['item_id']))[:1]
    return rows


def equip_coverage():
    """Aggregate owned equipment identities, including pack gains, without names or UUIDs."""
    reg = equip_registry()
    inv = inventory_data()
    counts = {slot:dict(resolved=0, neutral=0, hidden=0) for slot in sorted(set(inv['slots'].values()))}
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    for row in equip_owned_rows('Equipment', state if isinstance(state, dict) else {}):
        if row['quantity'] <= 0:
            continue
        entry = reg['equipment'].get(row['uuid'])
        slot = entry.slot if entry else inv['slots'].get(_item_key(row.get('slot', '')), -1)
        bucket = counts.setdefault(slot, dict(resolved=0, neutral=0, hidden=0))
        status = ('resolved' if entry.matched else 'neutral') if entry else 'hidden'
        bucket[status] += 1
    return dict(sorted(counts.items()))


def equip_picker(query):
    root = ET.Element('response')
    items = ET.SubElement(root, 'items')
    state = load_state()
    kind, special = query.get('filter_type'), query.get('filter_special')
    if kind == 'Equipment':
        inv = inventory_data()
        slot = inv['slots'].get(_item_key(special))
        owned = equip_owned(state)
        selection = {row['uuid']:row for row, _ in owned}
        if LAYOUT.install or os.environ.get('SHAD_SCOUT_EQUIP_ONE_BAT') != '1':
            for record in inv.get('catalogue', {}).get(kind, []):
                if record['uuid'] not in selection:
                    selection[record['uuid']] = dict(record, quantity=0)
        registry = equip_registry()['equipment']
        for row in sorted(selection.values(), key=lambda r:(r['quantity'] <= 0, r['item_id'])):
            entry = registry.get(row['uuid'])
            if entry and entry.slot == slot:
                quantity = row['quantity']
                unsellable = quantity > 0 and any(str(row.get(k,'')).strip().casefold() in ('1','true')
                                                   for k in ('no_sell','equip_no_sell'))
                # These are presentation selectors on the DD picker only. The NO SELL
                # sprite is at index zero. The client uses INT_MAX to skip set/captain tabs.
                face = equip_face(kind, dict(row, slot=entry.fields['slot_type_name']),
                                 **entry.fields, has_item=min(quantity, 2), shade_tab_index=int(quantity <= 0),
                                 quantity_tab_index=1, no_sell=0 if unsellable else 1, is_live_set=2147483647,
                                 captained=2147483647, set_name='')
                if face is not None:
                    items.append(face)
    elif kind == 'Unlockable' and special in ('Uniforms','Icons','Banners'):
        inv = inventory_data()
        for row in equip_owned_rows(kind, state):
            if row['quantity'] <= 0:
                continue
            if special == 'Uniforms':
                extra = equip_registry()['uniforms'].get(row['uuid'])
            else:
                extra = equip_vanity(row, inv)
                if extra.get('sub_category_name') != special:
                    continue
            if extra:
                face = equip_face(kind, row, **extra)
                if face is not None:
                    items.append(face)
    count = str(len(items))
    collected = sum(int(item.get('quantity', '0')) > 0 for item in items)
    items.attrib.update(count=count, items_total=count, items_collected=str(collected))
    return ET.tostring(root, encoding='unicode')


def equip_team(params, change=False):
    state = load_state()
    team = state.get('arena_team', {})
    slots = dict(team.get('equipment_slots', {}))
    eligible = {r['item_id']:(r, spec) for r,spec in equip_owned(state)}
    if change:
        q = params_of(params)
        if not isinstance(q, dict):
            q = {}
        try:
            slot, iid = int(str(q.get('item_slot'))), int(str(q.get('item_id')))
        except (ValueError, TypeError):
            slot, iid = -1, -1
        if slot in range(15):
            if iid == 0:
                slots.pop(str(slot), None)
            elif iid in eligible:
                row, (actual, fields, stats, _) = eligible[iid]
                if actual == slot:
                    slots[str(slot)] = dict(uuid=row['uuid'], item_id=iid, stats=[list(stat) for stat in stats],
                                            style=int(fields['servoy_equipment_id']),
                                            sock_type=int(fields['sock_type_id']))
            if slots != team.get('equipment_slots', {}):
                state.setdefault('arena_team', {'id':DD_TEAM_ID})['equipment_slots'] = slots
                save_state(state)
    # Resolve by UUID on every read, including after a catalogue reorder.
    by_uuid = {(r['uuid'], spec.slot):iid for iid,(r,spec) in eligible.items()}
    root = ET.Element('response')
    ET.SubElement(root, 'items', {f'item_id_{s}':str(by_uuid.get(
        (slots.get(str(s), {}).get('uuid'), s), -1)) for s in range(15)})
    return ET.tostring(root, encoding='unicode')


def view_item_collection(kind):
    """inventory.view_collection for a non-card kind: the WHOLE category (the browse request has no page; the
    footer prints items_collected and items_total of <items>)."""
    rows = [x for x in (inventory_item(kind, r) for r in owned_inventory_rows(kind)) if x]
    return '<response><items count="%d" items_total="%d" items_collected="%d">%s</items></response>' % (
        len(rows), len(rows), len(rows), ''.join(rows))


COLLECTION_PAGE_SIZE = 25
COLLECTION_ALL_PLAYERS = 'All Players'


def collection_series():
    return ['Live Series'] + sorted({c['series'] for c in all_cards()
                                    if c.get('series') and c['series'] not in ('Live', 'Live Series')})


def collection_filter(parent, name, label, values, selected, *, tag='search_filter', read_only=False):
    choices = ','.join(values)
    ET.SubElement(parent, tag, opt_type='enum', name=name, display_name=label,
                  values=choices, display_values=choices, tool_tip='', min_length='0', max_length='0',
                  step='1', default_value=selected, read_only='true' if read_only else 'false')


def get_collection_filter(params):
    # 0xdbc800 reads search_filters/search_filter and clears the active flag on each enum.
    response = ET.Element('response')
    filters = ET.SubElement(response, 'search_filters')
    collection_filter(filters, 'filter_series', 'Series', ['All Series', *collection_series()], 'All Series')
    return ET.tostring(response, encoding='unicode')


def player_collection(query, *, search=False):
    series = collection_series()
    default = 'All Series' if search else 'Live Series'
    selected = query.get('filter_series') or default
    if selected == 'Default Series':
        selected = default
    wanted = 'Live' if selected == 'Live Series' else selected
    if isinstance(wanted, str) and wanted not in series and wanted.endswith(' Series') and wanted != 'All Series':
        wanted = wanted[:-7]                       # accept the retail suffixed form as well
    supported = query.get('filter_type', 'Mlb Player') == 'Mlb Player'
    if search and str(query.get('role', -1)) != '-1':
        supported = False                         # role-scoped picker ABI is unverified
    match = lambda c: supported and ((search and wanted == 'All Series') or c.get('series') == wanted)
    catalogue_count = sum(match(c) for c in all_cards())
    owned = [c for c in cards() if c['quantity'] > 0 and match(c)]
    collected = len(owned)
    page, pages = 0, 1
    if search:
        pages = max(1, (collected + COLLECTION_PAGE_SIZE - 1) // COLLECTION_PAGE_SIZE)
        try:
            page = max(0, min(int(query.get('page', 0)), pages - 1))
        except (TypeError, ValueError, OverflowError):
            page = 0
        start = page * COLLECTION_PAGE_SIZE
        owned = owned[start:start + COLLECTION_PAGE_SIZE]
    items = ''.join('<item %s/>' % card_attrs(c, type_id=0, order=i + 1, seq_level=0,
                                             is_live_set=0, captain=0, captained=0)
                    for i, c in enumerate(owned))
    body = '<items count="%d" total="%d" page="%d" pages="%d" items_collected="%d" items_total="%d">%s</items>' % (
        len(owned), collected, page + 1, pages, collected, catalogue_count, items)
    if search:
        body += '<page_stats cur_page="%d" max_page="%d" total="%d" per_page="%d"/>' % (
            page + 1, pages, collected, COLLECTION_PAGE_SIZE)
    else:
        # Browse has no page request. The dynamic series enum describes the active series.
        active = 'Live Series' if wanted == 'Live' else wanted
        if active in series and query.get('filter_special') in (None, '', COLLECTION_ALL_PLAYERS):
            filters = ET.Element('dynamic_filters')
            # 0xdbbb83-0xdbbba1 parse the three named children directly in this order.
            # CC_FILTER.VALUE[0] and [2] display the first and third options.
            collection_filter(filters, 'filter_special', 'Special', [COLLECTION_ALL_PLAYERS],
                              COLLECTION_ALL_PLAYERS, tag='filter_special', read_only=True)
            collection_filter(filters, 'filter_type', 'Type', ['Mlb Player'], 'Mlb Player',
                              tag='filter_type', read_only=True)
            collection_filter(filters, 'filter_series', 'Series', series, active, tag='filter_series')
            body += ET.tostring(filters, encoding='unicode')
    return '<response>%s</response>' % body


def view_collection(params):
    query = params_of(params)
    kind = query.get('filter_type', 'Mlb Player')
    if kind == 'Mlb Player':
        return player_collection(query)
    # Non-player equipment and uniform pickers use a different definition contract.
    if query.get('filter_special'):
        return equip_picker(query)
    return view_item_collection(kind) if kind in ITEM_TYPE else None


# The team the game PLAYS with comes from arena_team.view (handler 0x9c8f60, team builder
# 0x9d3fa0): <arena_team ...><arena_players total= appearance_names= attribute_names= stat_names=>
# with one <arena_player> per squad member. A player's ratings come as player_attributes="v|v|..."
# in the order of attribute_names, where a name is a FIELD of the game's rating block and a value
# may be a list "a,b,c" (see player_ratings below). Lineup slots: <who>_pos is the position code (pitcher 0 ... right field 8,
# designated hitter 9) and <who>_ord the batting order from 0, for rwdh (against a right-hander,
# with DH), rwodh (without DH), lwdh and lwodh.
# EVERY active player must carry BOTH pitching_role and pitching_position, or the builder never
# puts him on the roster (run 80: all 26 parsed, none placed, "ten position players" box).
# pitching_role is the roster role: 1 rotation (pitching_position 0-4), 2 closer (0), 3 setup
# (0-1), 4 middle relief (0-3), 5 long relief (0-2), 6 further pitchers (0-14). Any other value
# (we send 0) lets the game place the player itself: a position player (position code not 0) goes
# to the 40 position-player places, which is what the "ready to play" check counts.
# The player parser 0x9bf200 writes NOTHING into the player's record (not even his position) unless
# every one of these attributes is present: id, roster_player_id, first_name, last_name,
# first_name_id, last_name_id, age, then the list below in one all-or-nothing chain. Run 80, second
# try: with the list missing all 26 players reached the roster with position 0, so the game took
# every one for a pitcher and the 40 position-player places stayed empty.
# Numbers we do not know yet (looks, stances, announcer names) are sent as 0: the players will
# look and stand alike until the real values are read from the game's own roster records.
# true/false attributes are compared with the word "true" (reader 0xacd700).
HAND = {'R': 0, 'L': 1, 'S': 2}                       # R and L confirmed against real records (run 82)
PLAYER_ZERO_FIELDS = ['ethnicity', 'player_icon', 'birthplace', 'anim_walkup', 'anim_take_pitch_strike', 'anim_practice_swing',
                      'anim_stride', 'anim_take_pitch', 'anim_miss', 'anim_follow_through', 'anim_batting_style',
                      'anim_home_run_celeb', 'anim_pitching_style', 'anim_homerun_left', 'anim_homerun_right',
                      'bs_hand_offset_x', 'bs_hand_offset_y', 'bs_hand_offset_z', 'bs_hand_rot_z', 'bs_hand_waggle_rot_z',
                      'bs_back_elbow_offset_y', 'bs_front_elbow_offset_y', 'bs_hips_offset_x', 'bs_hips_offset_y',
                      'bs_hips_offset_z', 'bs_forward_foot_offset_x', 'bs_forward_foot_offset_z', 'years_pro',
                      'mlb_service_years', 'mlb_service_days', 'first_name_id', 'last_name_id']
SECONDARY = {'SP': 'sp', 'RP': 'rp', 'CP': 'cp', 'C': 'c', '1B': '1b', '2B': '2b', '3B': '3b', 'SS': 'ss', 'LF': 'lf', 'CF': 'cf', 'RF': 'rf'}


# THE REAL PLAYER. tools\roster-dump.py copies every roster player's record and looks block out of
# the running game (probes\olserver\roster-records.json, local). From them we send back what the
# reply would otherwise wipe: looks and body shape (player_appearances), stance, animations,
# announcer name, birthplace. The record's layout is the inverse of the parser 0x9bf200.
_roster_records = None
_looks_fields = None


def roster_record(c):
    global _roster_records
    if _roster_records is None:
        try:
            _roster_records = json.load(open(derived_roster_path('roster-records.json'), encoding='utf-8'))
        except (OSError, ValueError):
            _roster_records = {}
    return _roster_records.get(c.get('roster_record_key', c['name'].strip()))


def looks_fields():
    global _looks_fields
    if _looks_fields is None:
        try:
            fields = json.load(open(derived_roster_path('appearance-fields.json'), encoding='utf-8'))
            _looks_fields = sorted((f for f in fields if f.get('offset') is not None), key=lambda f: f['offset'])
        except (OSError, ValueError):
            _looks_fields = []
    return _looks_fields


def player_looks(c):
    """Value text for appearance_names: the real player's looks block, field by field."""
    import base64
    rec = roster_record(c)
    if not rec or not looks_fields():
        return ''
    block = base64.b64decode(rec['looks'])
    out = []
    for f in looks_fields():
        raw = block[f['offset']:f['offset'] + f['bytes']]
        if f['elem'] == 4:
            vals = struct.unpack('<%dI' % f['count'], raw)
        elif f['type'] == 5:                                    # signed bytes
            vals = struct.unpack('<%db' % f['count'], raw)
        else:
            vals = raw
        out.append(','.join(str(v) for v in vals))
    return '|'.join(out)


def player_fields(c):
    import base64
    import re

    def number(text, default):
        m = re.search(r'\d+', str(text or ''))
        return int(m.group()) if m else default
    feet = re.match(r"\s*(\d+)'\s*(\d+)", str(c.get('height') or ''))
    a = {k: 0 for k in PLAYER_ZERO_FIELDS}
    jersey = number(c.get('jersey_number'), 0)
    # WEIGHT IS POUNDS MINUS 100 (one byte; real records: Judge 282 lb -> 182, Arraez 175 -> 75). Sent in
    # pounds, every player was 100 lb heavier ("big boy mode", run 82). Height is inches, hands are
    # R 0 / L 1 / S 2: both confirmed against real records.
    a.update(age=number(c.get('age'), 27), throw_hand=HAND.get(c.get('throw_hand'), 0), bat_hand=HAND.get(c.get('bat_hand'), 0),
             weight=max(0, min(255, number(c.get('weight'), 200) - 100)),
             height=(int(feet.group(1)) * 12 + int(feet.group(2))) if feet else 73,
             jersey_number=jersey, original_jersey_number=jersey, has_actionshot='false', i_am_legend='false', audio_data='')
    others = set(x.strip() for x in str(c.get('display_secondary_positions') or '').split(','))
    for code, tail in SECONDARY.items():
        a['secondary_pos_' + tail] = 'true' if code in others else 'false'
    rec = roster_record(c)
    if rec:
        r = base64.b64decode(rec['record'])
        w94, w9c, wa4 = struct.unpack_from('<QQQ', r, 0x94)
        wac, = struct.unpack_from('<I', r, 0xac)
        flags, = struct.unpack_from('<I', r, 0x4c)
        a.update(anim_batting_style=struct.unpack_from('<H', r, 0)[0], anim_home_run_celeb=struct.unpack_from('<H', r, 2)[0],
                 anim_pitching_style=struct.unpack_from('<H', r, 4)[0], anim_homerun_left=r[6], anim_homerun_right=r[7],
                 anim_walkup=(wa4 >> 32) & 15, anim_take_pitch_strike=(wa4 >> 36) & 15, anim_practice_swing=(wa4 >> 40) & 15,
                 anim_stride=(wa4 >> 44) & 15, anim_take_pitch=(wa4 >> 48) & 15, anim_miss=(wa4 >> 52) & 15,
                 anim_follow_through=(w9c >> 13) & 15, ethnicity=(w94 >> 25) & 7, mlb_service_years=(w94 >> 42) & 63,
                 years_pro=(w9c >> 26) & 63, player_icon=(w9c >> 37) & 31, birthplace=(wac >> 14) & 127, mlb_service_days=r[0xc5],
                 has_actionshot='true' if (w9c >> 7) & 1 else 'false', i_am_legend='true' if (flags >> 18) & 1 else 'false',
                 audio_data=','.join(str(v) for v in struct.unpack_from('<4I', r, 8)))
        for n, key in enumerate(('bs_hand_offset_x', 'bs_hand_offset_y', 'bs_hand_offset_z', 'bs_hand_rot_z', 'bs_hand_waggle_rot_z',
                                 'bs_back_elbow_offset_y', 'bs_front_elbow_offset_y', 'bs_hips_offset_x', 'bs_hips_offset_y',
                                 'bs_hips_offset_z', 'bs_forward_foot_offset_x', 'bs_forward_foot_offset_z')):
            a[key] = r[0x54 + n]
    return a


# RATINGS. The reader 0x9ae2b0 knows only these names (hash table at 0x2dcfba0 and 0x2dcd030):
# PLR_ATTRIBUTES, PITCH_TYPES, PITCHER_ATTS, HIT_ZONES, BAT_PRC_FLY_GB, BAT_PRC_L_FIELD,
# BAT_PRC_R_FIELD, WAR_MOD_RANK, MORALE (and "ovr"). One name per rating (CON_L, POW_R ...) is
# NOT understood: with those every rating but the overall stayed 0 (run 81: "CON R 0, POW R 0").
# PLR_ATTRIBUTES is 20 numbers separated by commas, in the order below (worked out by reading real
# players' blocks in the running game and comparing with their cards: Cole, Judge, Realmuto ...).
# PITCH_TYPES is 6 pitch type codes (255 = none); PITCHER_ATTS is 24 numbers: stamina, clutch,
# H/BF, HR/BF, K/BF, BB/BF, then control, velocity rating and movement for each of the 6 pitches.
# Type codes and the velocity rating (the site only gives mph) come from tools\pitch-table.py,
# which pairs 956 pitchers of the game's own roster with their cards (probes\olserver, local).
PLR_ATTRIBUTES = ['ovr', 'contact_left', 'contact_right', 'power_left', 'power_right', 'batting_clutch', 'bunting_ability',
                  'drag_bunting_ability', 'plate_vision', 'plate_discipline', 'ovr',      # the 11th is the potential
                  'durability', 'speed', 'baserunning_ability', 'baserunning_aggression', 'arm_strength', 'arm_accuracy',
                  'fielding_ability', 'reaction_time', 'blocking']
RATING_NAMES = 'PLR_ATTRIBUTES|PITCH_TYPES|PITCHER_ATTS'
_pitch_table = None


def pitch_table():
    global _pitch_table
    if _pitch_table is None:
        try:
            _pitch_table = json.load(open(derived_roster_path('pitch-table.json'), encoding='utf-8'))
        except (OSError, ValueError):       # without the table: the common pitches by their codes, flat velocity
            _pitch_table = {'types': {'4-Seam Fastball': 0, 'Sinker': 1, 'Curveball': 2, 'Slider': 3, 'Changeup': 6,
                                      '2-Seam Fastball': 8, 'Cutter': 9, 'Circle Change': 10}, 'velocity': {}}
    return _pitch_table


def player_ratings(c):
    """The value text for RATING_NAMES."""
    byte = lambda v: max(0, min(255, int(v or 0)))
    get = lambda k: (c.get('hitting_durability') or c.get('fielding_durability')) if k == 'durability' else c.get(k)
    ratings = [byte(get(k)) for k in PLR_ATTRIBUTES]
    native = c.get('disc_pitch')
    if native is not None:
        ratings[10] = byte(c.get('potential', c['ovr']))
        return '|'.join(','.join(map(str, group)) for group in (ratings, native[:6], native[6:30]))
    main = ','.join(map(str, ratings))
    table = pitch_table()
    pitches = [x for x in (c.get('pitches') or []) if x.get('name') in table['types']][:6] if not c.get('is_hitter') else []
    if not pitches:                         # what the game's own roster holds for a position player
        return main + '|0,1,2,255,255,255|0,0,0,0,0,0,10,10,10,0,0,0,0,0,0,0,0,0,10,10,10,0,0,0'
    codes, control, velocity, movement = [], [], [], []
    for x in pitches:
        code = table['types'][x['name']]
        slope, intercept = (table['velocity'].get(str(code)) or [0, 70])[:2]
        codes.append(code)
        control.append(byte(x.get('control')))
        velocity.append(max(1, min(99, int(round(slope * (x.get('speed') or 0) + intercept)))))
        movement.append(byte(x.get('movement')))
    pad = 6 - len(pitches)
    text = lambda vals, fill: ','.join(str(v) for v in vals + [fill] * pad)
    head = ','.join(str(byte(c.get(k))) for k in ('stamina', 'pitching_clutch', 'hits_per_bf', 'hr_per_bf', 'k_per_bf', 'bb_per_bf'))
    return '%s|%s|%s,%s,%s,%s' % (main, text(codes, 255), head, text(control, 0), text(velocity, 0), text(movement, 0))


MLB_TEAMS = [('BAL', 'Baltimore', 'Orioles'), ('BOS', 'Boston', 'Red Sox'), ('NYY', 'New York', 'Yankees'), ('TB', 'Tampa Bay', 'Rays'),
             ('TOR', 'Toronto', 'Blue Jays'), ('CWS', 'Chicago', 'White Sox'), ('CLE', 'Cleveland', 'Guardians'), ('DET', 'Detroit', 'Tigers'),
             ('KC', 'Kansas City', 'Royals'), ('MIN', 'Minnesota', 'Twins'), ('HOU', 'Houston', 'Astros'), ('LAA', 'Los Angeles', 'Angels'),
             ('OAK', 'Oakland', 'Athletics'), ('SEA', 'Seattle', 'Mariners'), ('TEX', 'Texas', 'Rangers'), ('ATL', 'Atlanta', 'Braves'),
             ('MIA', 'Miami', 'Marlins'), ('NYM', 'New York', 'Mets'), ('PHI', 'Philadelphia', 'Phillies'), ('WSH', 'Washington', 'Nationals'),
             ('CHC', 'Chicago', 'Cubs'), ('CIN', 'Cincinnati', 'Reds'), ('MIL', 'Milwaukee', 'Brewers'), ('PIT', 'Pittsburgh', 'Pirates'),
             ('STL', 'St. Louis', 'Cardinals'), ('ARI', 'Arizona', 'Diamondbacks'), ('COL', 'Colorado', 'Rockies'), ('LAD', 'Los Angeles', 'Dodgers'),
             ('SD', 'San Diego', 'Padres'), ('SF', 'San Francisco', 'Giants')]       # the roster's team order (D_TEAM_PROF 0-29)
LEGACY_CPU_TEAM_BASE = 101    # older captures and any in-flight requests can carry index + 101
MLB_UNIS = os.path.join(LAYOUT.base, 'FRONTEND', 'UNIFORMS', 'MLBUNIS.JSON')
_mlb_uniforms = None


def cpu_squad(index):
    """The CPU opponent: the Live Series cards of one MLB team, filled up from the rest of the series."""
    live = [c for c in all_cards() if c.get('series') == 'Live']
    own = [c for c in live if c['team_id'] == index]
    return pick_squad(own + [c for c in live if c['team_id'] != index and c['ovr'] < 75])


def cpu_team(index):
    global _mlb_uniforms
    if _mlb_uniforms is None:
        with open(MLB_UNIS, encoding='utf-8') as f:
            rows = json.load(f)['uniforms']
        _mlb_uniforms = {}
        for row in rows:
            _mlb_uniforms.setdefault(row['gameid'], []).append(row)
    team = _cpu_team(index, *MLB_TEAMS[index])
    for side in ('home', 'away'):
        ordinal, = [i for i, row in enumerate(_mlb_uniforms[index]) if row['assetname'] == side]
        # VERIFIED picker layout (0xdb8a18, 0xdb7499): club in the low 16 bits,
        # club-relative uniform ordinal in each of the three high lanes.
        team[f'{side}_uniform_id'] = index | (ordinal * 0x0001000100010000)
    return team


def _cpu_team(index, short, city, name):
    return {'id': index, 'name': name, 'location': city, 'abbreviation': short, 'mini_cpu': 0,
            'primary_hue': 0, 'primary_saturation': 0, 'primary_brightness': 60, 'secondary_hue': 0, 'secondary_saturation': 0,
            'secondary_brightness': 255, 'tertiary_hue': 0, 'tertiary_saturation': 0, 'tertiary_brightness': 0,
            'audio_comm_location': 0, 'audio_comm_name': 0}


def view_team(params):
    # our own team: arena_team.view {"player_id":<account>,"squad_type":0}
    return team_reply(current_squad(), load_state().get('arena_team'))


def view_by_team(params):
    # THE OPPONENT (Play vs. CPU asks arena_team.view_by_team {"squad_type":0,"team_id":N} after the
    # home/away choice; the reply goes through the same builder 0x9d3fa0 into team record 1, and
    # 0xb70dc0 then wants 5 starters and 8 relievers there or says "Your Opponent has an invalid
    # roster"). team_id 0 is what the game sends while the team list is empty.
    # VERIFIED: the reply id replaces the chosen club index at logic+0x5d8 (0xea7f72, 0x9d44a6).
    # Echo 0-29 for the real uniform (0x1997313) and logo (0x9e7885) loaders.
    q = params_of(params)
    try:
        index = int(q.get('team_id', 0))
    except (TypeError, ValueError):
        index = 0
    if index >= LEGACY_CPU_TEAM_BASE:
        index -= LEGACY_CPU_TEAM_BASE
    index = index if 0 <= index < len(MLB_TEAMS) else 0
    return team_reply(cpu_squad(index), cpu_team(index))


def collections_reply(name, params):
    """Collect > Collections (study: reports\\online\\proposals\\collections.md, 2026-11-19).

    collection.view_groups (0xeff5d0) stashes response/collection_groups as DATA_CGS; view_collections
    (0xeff670) stashes response/collections as DATA_CS; collection.view (0xeff730) stashes response/collection
    and each collection/items/item as CARD<n>, reads rewards.progress_max and, with one reward holding an item,
    the reward item. Tiles draw UTILS.TEXTURE[texture_name] from the disc's CROSS\\LIVE\\COLLECT_*.WEBP (bare
    stem). An item's collection_index is a STATE: 0 not collected, 1 collected, 2 picked locally; quantity > 0
    lets it be picked. collection.advance (lock-in) is refused in reply_body. Ids, membership and rewards are
    our choice: one group "Live Series" (id 1), one collection per MLB club (id 101 + club number)."""
    q = params_of(params)
    stem = lambda i: 'COLLECT_' + MLB_TEAMS[i][2].upper().replace(' ', '').replace('.', '')
    if name == 'collection.view_groups':
        return ('<response><collection_groups><collection_group id="1" name="Live Series" texture_name="COLLECT_LIVESERIES"/>'
                '</collection_groups></response>')
    if name == 'collection.view_collections':
        if str(q.get('collection_group_id')) != '1':
            return '<response><collections/></response>'
        return '<response><collections>%s</collections></response>' % ''.join(
            '<collection id="%d" name="%s %s" texture_name="%s" is_complete="false"/>' % (101 + i, city, nick, stem(i))
            for i, (short, city, nick) in enumerate(MLB_TEAMS))
    if name == 'collection.view':
        try:
            i = int(q.get('collection_id', 0)) - 101
        except (TypeError, ValueError):
            return None
        if not 0 <= i < len(MLB_TEAMS):
            return None
        team = [c for c in all_cards() if c.get('series') == 'Live' and c['team_id'] == i]
        if not team:
            return None
        owned = {c['item_id']: c['quantity'] for c in cards()}
        item = lambda c: '<item %s/>' % card_attrs(c, quantity=owned.get(c['item_id'], 0), collection_index=0, type_id=0,
                                                   slot_type_id=0, sub_category_id=0, seq_level=0, short_description='Player card')
        top = max(team, key=lambda c: c['ovr'])
        n = len(team)
        return ('<response><collection id="%d" name="%s %s" description="Live Series %s. Collecting is view-only on the local server.">'
                '<items>%s</items><rewards progress_current="0" progress_max="%d">'
                '<reward item_id="%d" qty_required="%d" has_collected="false" xp="0" stubs="0">%s</reward></rewards></collection></response>'
                % (101 + i, MLB_TEAMS[i][1], MLB_TEAMS[i][2], MLB_TEAMS[i][2], ''.join(item(c) for c in team), n,
                   top['item_id'], n, item(top)))
    return None


def home_panels(params):
    """arena_home.panels (0xe203a0 -> 0xe1e770, stash ARENA_PANELS); study proposals\\home-screen.md.

    <roster><position/> x10 become BADGE0..BADGE9 in XML order: the ten squad cards on the Home field diagram
    (order P, C, 1B, 2B, 3B, SS, LF, CF, RF, DH is INFERRED from the field coordinates; the pitcher is the first
    starter). <curr_season><season short_name= time_remaining= all_sets_eligible=><sets/></season> fills the
    "Season Squad Set Requirements" box (short_name 1 shows "S1"). Other panels (best, program, packs...) are
    optional and left out."""
    lineup, dh, bench, starters, relief = current_squad()
    slots = {c.get('lineup_pos'): c for c in lineup + dh}
    if starters:
        slots[0] = starters[0]
    season = ('<curr_season><season name="Season 1" short_name="1" time_remaining="0" all_sets_eligible="1"><sets/></season>'
              '</curr_season>')
    affinity = affinity_home_panel(load_state())
    if any(i not in slots for i in range(10)):
        return '<response>%s%s</response>' % (season, affinity)
    rows = ''.join('<position %s/>' % card_attrs(slots[i], position=i, new_position=i, captain=0, captained=0, is_oop=0,
                                                 is_2nd=0, has_rank_change=0, has_matchup=0, has_augment=0)
                   for i in range(10))
    return '<response><roster>%s</roster>%s%s</response>' % (rows, season, affinity)


CARD_ART_HOST = 's3.amazonaws.com'
CARD_ART_PATH = '/mlb23-production-2023/scout-card-art/'
_card_art = None

# THE GAME'S OWN UPDATE PACKAGE (v1.21, unpacked beside the base game) carries the real art of the special cards:
# CROSS\LIVE\AS_<handle>_<id>.WEBP, 2,674 files, named exactly like the download cache key as_<handle>_<id>, and
# 457 more action shots under PS4\UI\ART\ACTIONSHOTS. The v1.00 game does not see that folder, so the server
# hands the files over through the same download route as the website art. <handle> is [series code]_[team]_
# LAST_FIRST; the series codes below are the dominant code among each series' cards (counted 2026-11-19).
UPDATE_DIR = LAYOUT.update
SERIES_ART = {'Captain': 'CPT', 'Topps Now': 'TN', '2023 Finest': 'F23', 'Monthly Awards': 'MA', 'Kaiju': 'KAI',
              'World Baseball Classic': 'WBC', 'Future Stars': 'FS', 'Finest': 'F', 'Charisma': 'CAR', 'Snapshot': 'SNP',
              '2023 All-Star': 'AS23', '2023 Postseason': 'P23', 'Awards': 'AW', 'Milestone': 'MS', 'All-Star Game': 'ASG',
              'Home Run Derby': 'HRD', "Great Race of '98": 'GR98', 'Postseason': 'P', 'Signature': 'S', 'Prime': 'PR',
              '2023 Home Run Derby': 'HR23', 'Jin Kim': 'WTR', 'Sanford Greene': 'SG', 'London': 'LON', 'Mexico City': 'MXS',
              '2023 Draft': 'DRFT', 'Incognito': 'IC', '2nd Half Heroes': '2H', 'All-Star': 'AS', 'Rookie': 'RC',
              'Veteran': 'V', 'Breakout': 'B'}                    # GUESS for the last five and for Jin Kim / Sanford Greene
_update_art = None


def update_art(c):
    """(handle, id, file path, 'webp' | 'tex') of a card's own picture in the update package, or None."""
    global _update_art
    if UPDATE_DIR is None:
        return None
    if _update_art is None:
        import re as _re
        _update_art = {'live': {}, 'tex': {}}
        try:
            for f in os.listdir(os.path.join(UPDATE_DIR, 'CROSS', 'LIVE')):
                m = _re.match(r'^AS_(.+)_(\d+)\.WEBP$', f.upper())
                if m:
                    tail = '_'.join(m.group(1).split('_')[-2:])
                    _update_art['live'].setdefault(tail, []).append((m.group(1), int(m.group(2)), os.path.join(UPDATE_DIR, 'CROSS', 'LIVE', f)))
            shots = os.path.join(UPDATE_DIR, 'PS4', 'UI', 'ART', 'ACTIONSHOTS')
            for f in os.listdir(shots):
                if f.upper().endswith('.TEX'):
                    _update_art['tex'][f[:-4].upper()] = os.path.join(shots, f)
        except OSError:
            pass
    first, _, last = c['name'].strip().partition(' ')
    base = _shot_part(last) + '_' + _shot_part(first)
    cands = _update_art['live'].get(base, [])
    series = c.get('series')
    if series == 'Live':
        plain = sorted(x for x in cands if x[0] == base)
        if plain:
            return plain[-1] + ('webp',)                       # the newest plain picture
    else:
        code = SERIES_ART.get(series)
        if code:
            team = SHOT_TEAM.get(c.get('team_short_name', ''), c.get('team_short_name', ''))
            mine = sorted(x for x in cands if x[0].startswith(code + '_'))
            exact = [x for x in mine if x[0] == '%s_%s_%s' % (code, team, base)]
            if exact or mine:
                return (exact or mine)[0] + ('webp',)          # several cards of one series and player: the first
    if actionshot_handle(c) == 'ACTIONGENERIC' and base in _update_art['tex']:
        return (base, c['item_id'], _update_art['tex'][base], 'tex')
    return None


def register_picture(registry, name, art_id, url_id, data, extension='.webp'):
    """Publish one texture ZIP using the existing manifest and HTTP routes."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED, allowZip64=False) as z:
        z.writestr(zipfile.ZipInfo(name + extension), data)
    body = buf.getvalue()
    if len(body) > 512 * 1024:
        return False
    digest = hashlib.md5(body).hexdigest()
    path = '%s%s-%s.zip' % (CARD_ART_PATH, url_id, digest)
    registry['entries'].append((name, art_id, digest, 'https://%s%s' % (CARD_ART_HOST, path)))
    registry['files']['/__host/' + CARD_ART_HOST + path] = body
    return True


def player_art():
    """Special cards' own art, served as downloads (study: reports\\online\\proposals\\card-art-download.md).

    The card picture function (case 7 of 0xa1a970) looks in the downloaded-texture cache for the key
    as_<actionshot_handle>_<actionshot_id> before the disc. The cache is announced by <textures><texture name= id=
    md5= content_url=/> in the mlb_login.post_update reply (0x1d41740 -> 0xa88c10); the game GETs content_url and
    wants a ZIP holding <name>.webp (0xa87019; md5 = lowercase md5 of the whole ZIP, compared with strcmp). Disc
    action shots are 256 x 256, so the art is padded onto a 256 x 256 canvas. The pictures are the site's `img` of
    the user's special cards (tools\\theshow-cardart.py; downloaded with his approval, 2026-11-19). Returns
    {'entries': [(name, id, md5, url)], 'files': {web path: zip bytes}}; empty when nothing is downloaded."""
    global _card_art
    if _card_art is not None:
        return _card_art
    _card_art = {'entries': [], 'files': {}, 'override': {}}
    import io
    files = {}
    if CARD_SOURCE == 'site':
        try:
            snap = CATALOGUE_DIR
            folder = os.path.join(snap, sorted(os.listdir(snap))[-1], 'art', 'img')
            files = {f.split('.')[0]: os.path.join(folder, f) for f in os.listdir(folder) if not f.endswith(('.json', '.part'))}
        except Exception:
            pass
    counts = {'update': 0, 'update_tex': 0, 'site': 0}

    def publish(c, handle, art_id, member_ext, data, kind):
        name = 'as_%s_%d' % (handle, art_id)
        if register_picture(_card_art, name, art_id, c['item_id'], data, member_ext):
            _card_art['override'][c['item_id']] = (handle, art_id)
            counts[kind] += 1
    seen = set()
    for c in cards():
        try:
            # 1. the game's own picture from the update package (exact art; .webp, or a .tex action shot)
            own = update_art(c)
            if own and (own[0], own[1]) not in seen:
                seen.add((own[0], own[1]))
                publish(c, own[0], own[1], '.' + own[3], open(own[2], 'rb').read(), 'update' if own[3] == 'webp' else 'update_tex')
                continue
            if own:
                _card_art['override'][c['item_id']] = (own[0], own[1])    # a second copy of the same card: same picture
                continue
            # 2. the website's picture of a special card (the user's own download; whole card, cropped)
            src = files.get(c['uuid'])
            if not src or c.get('series') == 'Live':
                continue
            from PIL import Image, ImageOps
            with Image.open(src) as pic:
                rgba = ImageOps.exif_transpose(pic).convert('RGBA')
            w, h = rgba.size
            if h > w:
                rgba = rgba.crop((0, 0, w, w))                # keep the player photo, drop the name banner
            canvas = ImageOps.pad(rgba, (256, 256), Image.Resampling.LANCZOS, color=(0, 0, 0, 0))
            webp = io.BytesIO()
            canvas.save(webp, 'WEBP', quality=90, method=4)
            publish(c, actionshot_handle(c), c['item_id'], '.webp', webp.getvalue(), 'site')
        except Exception as e:
            print('card art: %s skipped (%r)' % (c['name'], e))
    print('card art: %d from the update package (+%d action shots), %d from the website download'
          % (counts['update'], counts['update_tex'], counts['site']))
    return _card_art


def card_art():
    """Combined registry used by post_update and the HTTP handler, including after hot reload."""
    players = player_art()
    packs = shop_pack_art(UPDATE_DIR)
    affinity = affinity_tile_art(UPDATE_DIR)
    gear = equipment_faces()
    return {'entries': players['entries'] + packs['entries'] + affinity['entries'] + gear['entries'],
            'files': ChainMap(players['files'], packs['files'], affinity['files'], gear['files']),
            'override': players['override']}


def post_update(params):
    # mlb_login.post_update: the reply file, plus the <textures> list of downloadable card art (see card_art()).
    with open(os.path.join(REPLIES_DIR, 'mlb_login.post_update.xml'), encoding='utf-8') as f:
        text = f.read()
    text = (text.replace('{now}', str(int(time.time()))).replace('{account_id}', str(ACCOUNT_ID)).replace('{user_name}', USER_NAME))
    esc = lambda v: str(v).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')
    entries = card_art()['entries']
    if entries:
        textures = '<textures>%s</textures>' % ''.join('<texture name="%s" id="%d" md5="%s" content_url="%s"/>' % (esc(n), i, d, esc(u))
                                                      for n, i, d, u in entries)
        text = text.replace('</response>', textures + '</response>')
    return text.strip()


def unlocked_stadiums(params):
    # arena_buy_stadiums.unlocked_stadiums / unlocked_stadiums2 (0x9c9790, 0xcd8500): both walk
    # response/stadiums/stadium and mark each numeric id unlocked. Ids 0-29 are the thirty MLB parks (table
    # 0x249d010); 77 is the park the saved stadium choice of run 84 named. Our choice of what is unlocked.
    return '<response><stadiums>%s</stadiums></response>' % ''.join('<stadium id="%d"/>' % i for i in list(range(30)) + [77])


def view_cpu_teams(params):
    # The list behind the Play vs. CPU team select (stash VS_CPU; the screen code 0xea87b0 walks
    # VSCPU_VALUE.SKILL_LEVEL[difficulty].TEAM[index] and 0xea7980 uses the index as the MLB team
    # number). Names from the study reports\online\proposals\vs-cpu-setup.md (script hashes matched):
    # the screen reads VS_DIFFICULTY.NAME and, of the chosen TEAM, TEAM_ID (name and logo come from the
    # game's own team table), DIVISION, OVR_VAL / CON_VAL / PWR_VAL / PIT_VAL / DEF_VAL / SPD_VAL (the
    # bars), RECORD / TEAM_AVG / TEAM_ERA, and the reward chances COMMON ... DIAMOND (numbers; it adds
    # the percent sign). The row index must equal team_id. Counts come from the repeated children.
    levels = ['Rookie', 'Veteran', 'All-Star', 'Hall of Fame', 'Legend']
    divisions = ['AL East', 'AL Central', 'AL West', 'NL East', 'NL Central', 'NL West']
    avg = lambda cs, *keys: int(sum(sum(c.get(k) or 0 for k in keys) / len(keys) for c in cs) / max(1, len(cs)))
    teams = []
    for i in range(len(MLB_TEAMS)):
        lineup, dh, bench, starters, relief = cpu_squad(i)
        hit, pit = lineup + dh, starters + relief
        teams.append('<team team_id="%d" division="%s" ovr_val="%d" con_val="%d" pwr_val="%d" pit_val="%d" def_val="%d" spd_val="%d" '
                     'record="0-0" team_avg=".000" team_era="0.00" common="0" bronze="0" silver="0" gold="0" diamond="0"/>'
                     % (i, divisions[i // 5], avg(hit + pit, 'ovr'), avg(hit, 'contact_left', 'contact_right'),
                        avg(hit, 'power_left', 'power_right'), avg(pit, 'ovr'), avg(hit, 'fielding_ability'), avg(hit, 'speed')))
    rows = ''.join(teams)
    return '<response><vscpu_value>%s</vscpu_value></response>' % ''.join('<skill_level name="%s">%s</skill_level>' % (level, rows)
                                                                           for level in levels)


CUSTOM_UNIFORM_WORKS = False      # runs 84, 92 and 95: the custom loader (0x19d56e0, GeneratedUniform.bin) gives smeared players
FALLBACK_UNIFORM_CLUB = 1         # Boston, the user's choice on 2026-11-19 ("Keep Boston") until the custom design works


def worn_uniform(value, side):
    """The uniform id sent for a side. A custom selection (low 16 bits 10000 or more, 0x199723a) is replaced by
    the fallback club's real uniform while the custom path is broken; the saved choice itself is left alone, so a
    design saved in the editor comes back by itself once CUSTOM_UNIFORM_WORKS is set."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = -1
    if CUSTOM_UNIFORM_WORKS or 0 <= (value & 0xffff) < 10000:
        return value
    return cpu_team(FALLBACK_UNIFORM_CLUB)['%s_uniform_id' % side]


def team_reply(squad, team):
    lineup, dh, bench, starters, relief = squad
    if not lineup or not team:
        return None
    if any('lineup_ord' in c for c in lineup + dh):       # a saved squad: its own batting order and positions
        order = sorted(lineup + dh, key=lambda c: c.get('lineup_ord', 0))
    else:
        order = sorted(lineup + dh, key=lambda c: -(c.get('contact_right', 0) + c.get('power_right', 0)))
    order_nodh = [c for c in order if c not in dh]
    players = []

    def add(c, **slots):
        slots.update(player_fields(c))
        slots.setdefault('pitching_role', 0)
        slots.setdefault('pitching_position', 0)
        slots.setdefault('role', {'SP': 0, 'RP': 1, 'CP': 2}.get(c['display_position'], 7))   # 0 starter, 1 reliever, 2 closer, 7 position player (seen in the game's own team records)
        ratings = player_ratings(c)
        # is_active is compared with the word "true" (0x9d3fa0); "1" made the builder skip every player
        # player_stats follows stat_names (reader 0x9aebf0). The one name that matters to play is
        # ENERGY: it goes into the player's record (+0xc0); without it every pitcher starts at 0
        # ("pitcher out of energy" before the first pitch, run 81). 255 asks for the game's own
        # default by pitcher kind (tables at 0x2018c5c / 0x2018c68).
        c = {k: v for k, v in c.items() if not k.startswith('lineup_')}
        players.append('<arena_player %s/>' % card_attrs(c, is_active='true', player_attributes=ratings,
                                                         player_appearances=player_looks(c), player_stats='255', **slots))
    for c in order:
        code = c.get('lineup_pos', 9 if c in dh else FIELD_NUMBER.get(c['display_position'], 0))
        slots = {'position': FIELD_NUMBER.get(c['display_position'], 0), 'rwdh_pos': code, 'rwdh_ord': order.index(c),
                 'lwdh_pos': code, 'lwdh_ord': order.index(c)}
        if c in order_nodh:
            slots.update(rwodh_pos=code, rwodh_ord=order_nodh.index(c), lwodh_pos=code, lwodh_ord=order_nodh.index(c))
        add(c, **slots)
    for c in bench:
        add(c, position=FIELD_NUMBER.get(c['display_position'], 0))
    for i, c in enumerate(starters):
        slots = {'position': 0, 'pitching_role': 1, 'pitching_position': i}
        if i == 0:       # without a DH the first starter bats ninth
            slots.update(rwodh_pos=0, rwodh_ord=8, lwodh_pos=0, lwodh_ord=8)
        add(c, **slots)
    roles = [(2, 0), (3, 0), (3, 1), (4, 0), (4, 1), (4, 2), (4, 3), (5, 0)]     # closer, 2 setup, 4 middle, 1 long
    closers = [c for c in relief if c['display_position'] == 'CP'] + [c for c in relief if c['display_position'] != 'CP']
    for c, (role, slot) in zip(closers, roles):
        add(c, position=0, pitching_role=role, pitching_position=slot)
    esc = lambda v: str(v).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')
    colours = ' '.join('%s="%s"' % (k, esc(v)) for k, v in team.items() if k.endswith(('_hue', '_saturation', '_brightness'))
                       or k in ('audio_comm_location', 'audio_comm_name') or (k.startswith('uniform_') and k != 'uniform_id'))
    names = RATING_NAMES
    team_id = team.get('id', DD_TEAM_ID)
    return ('<response><arena_team id="%d" name="%s" location="%s" abbreviation="%s" mini_cpu="%d" tier="" trophy_data="" is_logo_approved="true" '
            'draft_state="0" has_dh="1" wins="%d" losses="%d" rating="0" rating_coop="0" logo_version="0" br_wins="0" br_losses="0" '
            'capt_pit="0" capt_pos="0" onramp_step="0" home_uniform_team_id="%d" away_uniform_team_id="%d" %s/>'
            # arena_players is a SIBLING of arena_team (0x9d3fa0 looks both up under <response>);
            # nested inside the team it was ignored and the game's roster stayed empty.
            '<arena_players total="%d" appearance_names="%s" attribute_names="%s" stat_names="ENERGY">%s</arena_players>'
            '</response>'
            % (team_id, esc(team.get('name', 'Squad')), esc(team.get('location', '')), esc(team.get('abbreviation', '')),
               team.get('mini_cpu', 0), team.get('mini_wins', 0), team.get('mini_losses', 0),
               # WHICH UNIFORM A TEAM WEARS: home_uniform_team_id / away_uniform_team_id (own team: logic+0x780 / +0x778,
               # the same fields arena_team.update_uniform_id sends as home_uniform_id / away_uniform_id). A value
               # whose low 16 bits are 10000 or more goes to the CUSTOM loader (0x199723a -> 0x19d56e0), which has never worked;
               # a smaller value is club number + packed row = that club's REAL uniform (0x19d41d0). -1 is custom too, which is what we
               # sent until run 85: no uniform textures were made and the players were smeared.
               worn_uniform(team.get('home_uniform_id', team_id), 'home'), worn_uniform(team.get('away_uniform_id', team_id), 'away'),
               colours, len(players), '|'.join(f['name'] for f in looks_fields()), names, ''.join(players)))


def loading_screen(params):
    global _affinity_lineup
    _affinity_lineup = None
    query = params_of(params)
    try:
        team_ids = [int(query[f'{side}_team_id']) for side in ('home', 'away')]
    except (KeyError, TypeError, ValueError):
        return None
    if team_ids.count(DD_TEAM_ID) != 1:
        return None
    club, = (team_id for team_id in team_ids if team_id != DD_TEAM_ID)
    if not 0 <= club < len(MLB_TEAMS):
        return None

    squads = {DD_TEAM_ID: current_squad(), club: cpu_squad(club)}
    body = loading_cards(*(squads[team_id] for team_id in team_ids))
    if body is not None:
        _affinity_lineup = (team_ids, affinity_squad_clubs(squads[DD_TEAM_ID]))
    return body


def loading_cards(home, away):
    rows = []
    for roster in (home, away):
        squad = [card for group in roster for card in group]
        # VERIFIED 0x9c7ef0: the lookup uses rows 0–25 for home and the rest for away.
        if len(squad) != 26:
            return None
        for card in squad:
            rows.append('<player %s/>' % card_attrs(
                card, bat_hand=HAND.get(card.get('bat_hand'), 0),
                throw_hand=HAND.get(card.get('throw_hand'), 0),
                is_live_set=int(card.get('series') == 'Live'),
                captain=0, captained=0, set_name=card.get('set_name') or ''))

    boosts = '<boosts>' + '<attr_boost name="" value="-1"/>' * 4 + '</boosts>'
    captain_slots = ''.join(f'<{role} name="" boost="0">{boosts}</{role}>'
                            for role in ('pitcher', 'position'))
    captains = ''.join(f'<captains_{side} has_pitcher="0" has_position="0">'
                       f'{captain_slots}</captains_{side}>' for side in ('home', 'away'))
    # VERIFIED 0xb602f0: the callback stashes this response as arena_ls_stash.
    return (f'<response><tourncheck home="0" away="0"/><cards>{"".join(rows)}</cards>'
            f'{captains}</response>')


# ---------------------------------------------------------------- Play vs. CPU postgame summary
SUMMARY_SIDES = ('home', 'away')
INNING_HEADER = re.compile(r'(?:\^c\d+\^)?Inning (\d+):(?:\^c\d+\^)?')
BATTING_HEADER = re.compile(r'(?:^|\^n\^)([^^\r\n]+?) batting\.\s')
HALF_TOTALS = re.compile(r'\^n\^Runs:\s*(\d+)\s+Hits:\s*(\d+)\s+Walks:\s*\d+'
                         r'\s+Errors:\s*(\d+)\s+Pitches:\s*\d+')
_summary_setup = None


def decode_summary_record(data):
    # VERIFIED writer order 0xb33633-0xb339d3. Strings have the same two lengths as
    # service messages (0xa74c50); enforce both lengths on this attachment.
    r = Reader(data)
    game_type, outcome, uuid = (r.string(check_length=True) for _ in range(3))
    r.u32()
    sub_mode = r.u32()  # INFERRED from ARENA logic+0xde8 (0xa75631)
    r.u64()
    r.string(check_length=True)
    r.take(28)          # u32, 16 bytes, two u32 (0xb3371f)
    players = {}
    for side in SUMMARY_SIDES:
        if r.u8() != 3:
            raise ValueError('unexpected player ID count')
        players[side] = r.u64()
        r.take(16)
    ids = {side: r.u32() for side in SUMMARY_SIDES}
    r.take(8)           # eight flags (0xb337c2)
    scheduled = r.u32()
    r.take(8)
    totals = {}
    for side in SUMMARY_SIDES:
        stats = struct.unpack('<35I', r.take(35 * 4))
        # INFERRED indices corroborated by run 91: away 6 runs, 9 hits (0xa768f7, 0xa76f67).
        totals[side] = {'runs': stats[6], 'hits': stats[0]}
    if r.u8() != 26:
        raise ValueError('unexpected counter count')
    r.take(26 * 4)      # counters are unrelated to innings (0xa70bf9)
    return dict(game_type=game_type, outcome=outcome, uuid=uuid, sub_mode=sub_mode,
                players=players, ids=ids, scheduled=scheduled, totals=totals,
                log=r.string(check_length=True))


def decode_line_score(record, teams):
    by_name = {}
    for side, team in teams.items():
        for name in (team['name'], f'{team["location"]} {team["name"]}'):
            if by_name.setdefault(name.strip().casefold(), side) != side:
                raise ValueError('ambiguous batting names')
    scores = {side: {} for side in SUMMARY_SIDES}
    hits = dict.fromkeys(SUMMARY_SIDES, 0)
    errors = dict.fromkeys(SUMMARY_SIDES, 0)
    sections = INNING_HEADER.split(record['log'])
    last = 0
    for label, section in zip(sections[1::2], sections[2::2]):
        inning = int(label)
        halves = BATTING_HEADER.split(section)
        if inning != last + 1 or (last and last not in scores['home']):
            raise ValueError('missing inning log')
        for name, half in zip(halves[1::2], halves[2::2]):
            side = by_name.get(name.strip().casefold())
            totals = HALF_TOTALS.findall(half)
            if side is None or inning in scores[side] or len(totals) != 1:
                raise ValueError('unknown, repeated or incomplete half-inning')
            runs, half_hits, field_errors = map(int, totals[0])
            scores[side][inning] = runs
            hits[side] += half_hits
            # VERIFIED 0x34b41b-0x34b579: errors are charged to the fielding side.
            errors['home' if side == 'away' else 'away'] += field_errors
        if inning not in scores['away']:
            raise ValueError('missing top half')
        last = inning
    if not last:
        raise ValueError('missing inning log')
    for side in SUMMARY_SIDES:
        aggregate = record['totals'][side]
        if (sum(scores[side].values()), hits[side]) != (aggregate['runs'], aggregate['hits']):
            raise ValueError('line score disagrees with aggregate')
    if record['outcome'] == 'FINISHED':
        home_ahead = record['totals']['home']['runs'] > record['totals']['away']['runs']
        if last < record['scheduled'] or (last not in scores['home'] and not home_ahead):
            raise ValueError('finished game log ends early')
    return last, scores, errors


# VERIFIED script aliases (FRONTEND.PS4 chunks 0x3daae0, 0x1d0720). Values are GUESS defaults.
SUMMARY_DEFAULTS = {
    's_____99z7rvf5xb__': 0,   # 0xec726f5d
    's_____99z94war_a_9': 0,   # 0x8d706872
    's_____9z99alst9_z9': 0,   # 0xbe161c74
    's_____9999vvrves9d': '',  # 0xf5635e2b
    's_____9999rsfvyz1m': '',  # 0xed6f4222
    's_____99yzyvcc_rz_': 0,   # 0xc0624c46
}
SUMMARY_TEAM_DEFAULTS = {
    's_____779z018c____': '',  # 0x87067246
    's_____79991vfv_b5z': '',  # 0x86724635
    's_____79991vfw_b5z': '',  # 0x86724634
    's_____79993war_a9y': '',  # 0x84704d32
    's_____79993war_a9x': '',  # 0x84704d33
}


def summary_teams(record, mine):
    # VERIFIED 0xa78a92-0xa78ae2: the first player on each side identifies the local account.
    local, = [side for side in SUMMARY_SIDES if record['players'][side] == ACCOUNT_ID]
    cpu = 'home' if local == 'away' else 'away'
    if record['ids'][local] not in (mine['id'], LEGACY_DD_TEAM_ID):
        raise ValueError('unknown local team')
    club = record['ids'][cpu]
    if club >= LEGACY_CPU_TEAM_BASE:  # Convert only the CPU side of an older upload (run 91).
        club -= LEGACY_CPU_TEAM_BASE
    if not 0 <= club < len(MLB_TEAMS):
        raise ValueError('unknown MLB opponent')
    opponent = _cpu_team(club, *MLB_TEAMS[club])
    return {local: mine, cpu: dict(opponent, owner_id=0, owner_name='CPU')}


def summary_local_team(state):
    team = state.get('arena_team')
    if not isinstance(team, dict):
        team = {}
    mine = dict(id=DD_TEAM_ID, name=team.get('name') or 'Squad', location=team.get('location') or '',
                owner_id=ACCOUNT_ID, owner_name=USER_NAME)
    for part in ('hue', 'saturation', 'brightness'):
        key = 'primary_' + part
        try:
            mine[key] = int(team.get(key, 0))
        except (TypeError, ValueError):
            mine[key] = 0
    return mine


def remember_summary_setup(params):
    # Run 91 loading request #109 gives both wire IDs, before the upload has a UUID.
    global _summary_setup
    _summary_setup = None
    query = params_of(params)
    try:
        ids = {side: int(query[f'{side}_team_id']) for side in SUMMARY_SIDES}
    except (KeyError, ValueError, TypeError):
        return
    _summary_setup = (ids, summary_local_team(load_state()))


def remember_summary_upload(state, game_data):
    global _summary_setup
    setup, _summary_setup = _summary_setup, None
    try:
        record = decode_summary_record(game_data)
        if record['game_type'] != 'ARENA' or not record['uuid']:
            return
        if state.get('last_game_summary', {}).get('uuid') == record['uuid']:
            return    # Retried upload: retain the original setup and capture time.
        mine = setup[1] if setup and setup[0] == record['ids'] else summary_local_team(state)
        summary_teams(record, mine)  # Validate sides before committing metadata (game_create saves).
    except ValueError:
        return
    state['last_game_summary'] = dict(uuid=record['uuid'], local_team=mine, created_at=int(time.time()))


def summary_response(teams, record=None, innings=0, scores=None, errors=None, created_at=0):
    # VERIFIED native children and colour triplets (0xb5a440, 0xbfd560). See
    # reports/online/proposals/game-summary.md for the script operands and their aliases.
    def node(parent, tag, fields):
        return ET.SubElement(parent, tag, {k: str(v) for k, v in fields.items()})

    scores = scores or {}
    errors = errors or {}
    winner = loser = None
    if record and record['outcome'] == 'FINISHED':
        home, away = (record['totals'][side]['runs'] for side in SUMMARY_SIDES)
        if home != away:
            winner, loser = SUMMARY_SIDES if home > away else SUMMARY_SIDES[::-1]
    # GUESS: only a decisive FINISHED game shows a result. A QUIT has an observed
    # line score, but run 91 cannot establish the winner or any reward changes.
    won = bool(winner and teams[winner]['owner_id'] == ACCOUNT_ID)
    lost = bool(loser and teams[loser]['owner_id'] == ACCOUNT_ID)
    attrs = dict(SUMMARY_DEFAULTS, game_type='Play vs. CPU', created_at=created_at,
                 sub_mode=record['sub_mode'] if record else 0, home_won=int(winner == 'home'),
                 is_ai_game='true', is_winning_team=str(won).lower(),
                 winning_team=teams[winner]['name'] if winner else '', display_index=0, tickets_earned=0,
                 wins=int(won), losses=int(lost), cum_wins=int(won),
                 skill_level_id=0, skill_level_name='Rookie' if record else '')  # GUESS difficulty default
    # GUESS: hold all reward thresholds above the single-match win counter on a decoded game.
    for prefix, count in (('reward', 5), ('cum_reward', 4)):
        attrs.update({f'{prefix}{i}': i + 1 if record else 0 for i in range(1, count + 1)})
    for label, side in (('home', 'home'), ('away', 'away'), ('win', winner), ('lose', loser)):
        for part in ('hue', 'saturation', 'brightness'):
            key = 'primary_' + part
            attrs[f'{label}_{key}'] = teams.get(side, {}).get(key, 0) if record else 0

    response = ET.Element('response')
    summary = node(response, 'arena_game_summary', attrs)
    game = {'innings': innings, 's_____99z9vvcviz_3': ''}  # 0xf96a6a7c, GUESS blank result detail
    game.update({f'inning_{i}': i for i in range(1, 10)})
    for side in SUMMARY_SIDES:
        team = teams[side]
        totals = record['totals'][side] if record else {}
        game.update({f'{side}_name': team['name'], f'{side}_full_id': team['owner_name'],
                     f'{side}_player_id': record['players'][side] if record else 0,
                     f'{side}_runs': totals.get('runs', 0), f'{side}_hits': totals.get('hits', 0),
                     f'{side}_errors': errors.get(side, 0)})
        for i in range(1, 10):
            game[f'{side}_runs_{i}'] = scores.get(side, {}).get(i, '-' if record else 0)
    node(summary, 'game', game)
    for side in SUMMARY_SIDES:
        team = teams[side]
        fields = dict(SUMMARY_TEAM_DEFAULTS, **{k: team[k] for k in ('id', 'name', 'location', 'owner_id', 'owner_name')})
        for suffix in ('', '2', '3'):
            for part in ('init', 'change', 'end'):
                fields[f'rating_{part}{suffix}'] = 0  # GUESS: no local rating change.
        node(summary, f'{side}_team', fields)
    return ET.tostring(response, encoding='unicode')


def arena_game_summary(params):
    state = load_state()
    mine = summary_local_team(state)
    # GUESS fallback presentation when the attachment cannot establish sides.
    teams = {'away': mine, 'home': dict(id=0, name='CPU', location='', owner_id=0, owner_name='CPU')}
    query = params_of(params)
    if not isinstance(query, dict) or not query.get('uuid'):
        return summary_response(teams)
    try:
        with open(os.path.join(WORK, 'last-game_data.bin'), 'rb') as f:
            record = decode_summary_record(f.read())
            created_at = int(os.fstat(f.fileno()).st_mtime)  # GUESS: capture time as Unix seconds
        if record['game_type'] != 'ARENA' or record['uuid'] != query['uuid']:
            raise ValueError('no matching ARENA attachment')
        saved = state.get('last_game_summary', {})
        if saved.get('uuid') == record['uuid']:
            mine = saved['local_team']
            created_at = saved['created_at']
        teams = summary_teams(record, mine)
        innings, scores, errors = decode_line_score(record, teams)
    except (OSError, ValueError):
        return summary_response(teams)
    return summary_response(teams, record, innings, scores, errors, created_at)


# ---------------------------------------------------------------- Mini Seasons Classic
# Wire contract and offsets: reports/online/proposals/mini-seasons.md.
MINI_PREFIX = 'arena_mini_season.'
MINI_CLUBS = (3, 7, 11, 15, 19, 23, 27)  # Local roster sources in league-slot order.
MINI_THEMES = (
    ('Cairo', 'Cats', 'CAI'), ('Lichtenstein', 'Whales', 'LIW'), ('Lima', 'Lemurs', 'LIM'),
    ('London', 'Lords', 'LON'), ('New Orleans', 'Pinheads', 'NOP'), ('Seoul', 'Singers', 'SS'),
    ('Tokyo', 'Kaiju', 'TOK'),
)
MINI_OWNERS = (ACCOUNT_ID, *range(21001, 21008))
MINI_TYPES = frozenset(str(value) for value in range(6, 14))
MINI_STATS = (
    'b_h b_hr b_bb b_hbp b_sf b_double b_triple b_ab b_avg b_ops b_rbi b_runs b_sb '
    'b_risp_h b_risp_rbi b_risp_ab b_risp_avg b_fielding_pct b_putout b_iso b_games '
    'p_k p_sv p_er p_ip p_era p_whip p_avg_vs_lhb p_avg_vs_rhb p_k_per_9 p_wins p_losses '
    'p_bb p_games p_blown_sv p_hr p_so p_hit_batter'
).split()  # VERIFIED consumers 0xb67282–0xb68062.
MINI_STAT_REPORT = (
    'id,b_h,b_hr,b_bb,b_hbp,b_sf,b_double,b_triple,b_ab,b_avg,b_ops,b_rbi,b_runs,b_sb,b_risp_h,b_risp_rbi,'
    'b_risp_ab,b_fielding_pct,b_putout,b_iso,b_games,b_risp_avg,p_k,p_sv,p_er,p_ip,p_era,p_whip,p_avg_vs_lhb,'
    'p_avg_vs_rhb,p_k_per_9,p_bb_per_9,p_hr_per_9,p_wins,p_losses,p_bb,p_games,p_blown_sv,p_hr,p_so,'
    'p_hit_batter,b_lhp_ab,b_rhp_ab,b_lhp_h,b_rhp_h,b_assist,b_error,energy,pos,role,used'
)  # Run 96 request #69 has pipe-delimited rows and comma-delimited columns.
MINI_STAT_COUNTS = tuple(
    name for name in MINI_STAT_REPORT.split(',')
    if name not in ('id', 'b_avg', 'b_ops', 'b_fielding_pct', 'b_iso', 'b_risp_avg', 'p_era', 'p_whip',
                    'p_avg_vs_lhb', 'p_avg_vs_rhb', 'p_k_per_9', 'p_bb_per_9', 'p_hr_per_9', 'energy', 'pos', 'role', 'used')
)
MINI_MISSIONS = (
    ('wins', 1, 'First Win', 'Win one game in this Mini Season.', 250, 100),
    ('wins', 10, 'Ten Wins', 'Win ten games in this Mini Season.', 1000, 400),
    ('games', 28, 'Complete Classic', 'Finish all 28 regular-season games.', 1500, 500),
    ('qualified', 1, 'Playoff Bound', 'Finish in the top four.', 1000, 400),
)  # Local award amounts. No cards.


class MiniSeasonRefusal(ValueError):
    pass


def mini_node(parent, tag, /, **attrs):
    return ET.SubElement(parent, tag, {key: str(value) for key, value in attrs.items()})


def mini_xml(response):
    return ET.tostring(response, encoding='unicode')


def escape_mini_attr(value):
    return str(value).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;')


def mini_ack(run, **fields):
    response = ET.Element('response')
    mini_node(response, 'mini_season', run_id=run['id'], **fields)
    return mini_xml(response)


def mini_days():
    ring = list(range(8))
    rounds = []
    for _ in range(7):
        rounds.append([(ring[i], ring[7 - i]) for i in range(4)])
        ring[1:] = ring[-1:] + ring[1:-1]
    return [[pair if (cycle + day) % 2 == 0 else pair[::-1] for pair in fixtures]
            for cycle in range(4) for day, fixtures in enumerate(rounds)]


def mini_run(state, query, *, allow_create=False):
    run = state.get('mini_season')
    # INFERRED: the client can continue sending 0 for logic+0xf54. Scope that alias to this local run.
    ids = [int(query[key]) for key in ('run_id', 'mini_run_id') if key in query]
    if run is None and allow_create and not any(ids):
        from copy import deepcopy
        run = {'version': 1, 'id': 1, 'clubs': list(MINI_CLUBS), 'days': mini_days(),
               'squad': deepcopy(state.get('squad') or default_squad()),
               'results': [], 'uploads': {}, 'pending': None, 'seconds': 0}
        state['mini_season'] = run
        return run, True
    if run is None:
        raise MiniSeasonRefusal('Start Mini Seasons Classic before requesting a run.')
    if not isinstance(run, dict) or run.get('version') != 1:
        raise MiniSeasonRefusal('This local Mini Seasons save uses an unsupported format.')
    if any(run_id not in (0, run['id']) for run_id in ids):
        raise MiniSeasonRefusal('Unknown Mini Seasons run.')
    return run, False


def mini_roster(run, slot):
    squad = current_squad(run['squad']) if slot == 0 else cpu_squad(run['clubs'][slot - 1])
    lineup, dh, bench, starters, relief = squad
    batters, pitchers = lineup + dh + bench, starters + relief
    if (tuple(map(len, squad)) != (8, 1, 4, 5, 8)
            or len({card['roster_player_id'] for card in batters + pitchers}) != 26
            or not all(card.get('is_hitter') for card in batters)
            or any(card.get('is_hitter') for card in pitchers)):
        raise MiniSeasonRefusal('Mini Seasons requires a complete 26-player squad for each side.')
    return squad


def mini_team(state, run, slot):
    if slot == 0:
        team = dict(_cpu_team(0, 'DD', '', 'Squad'), **state.get('arena_team', {}))
        team.update(id=DD_TEAM_ID, mini_cpu=0)
        return team
    city, name, short = MINI_THEMES[slot - 1]
    return dict(cpu_team(run['clubs'][slot - 1]), name=name, location=city, abbreviation=short, mini_cpu=1)


def mini_fixture(run):
    day = len(run['results'])
    if day >= len(run['days']):
        raise MiniSeasonRefusal('This regular season is complete. Local playoffs are unavailable.')
    return day, dict(zip(SUMMARY_SIDES, run['days'][day][0]))


def mini_standings(run, through=None):
    records = [[0, 0] for _ in MINI_OWNERS]
    versus = [[[0, 0] for _ in MINI_OWNERS] for _ in MINI_OWNERS]
    streaks = [0] * 8
    if run:
        peers = run.get('cpu_results', [])
        for day, result in enumerate(run['results'][:through]):
            home_wins = [result['home_won']]
            if day < len(peers):
                home_wins.extend(home > away for home, away in peers[day])
            for (home, away), home_won in zip(run['days'][day], home_wins):
                winner, loser = (home, away) if home_won else (away, home)
                records[winner][0] += 1
                records[loser][1] += 1
                versus[winner][loser][0] += 1
                versus[loser][winner][1] += 1
                streaks[winner] = max(0, streaks[winner]) + 1
                streaks[loser] = min(0, streaks[loser]) - 1
    pct = [wins / max(1, wins + losses) for wins, losses in records]
    order = sorted(range(8), key=lambda slot: (-pct[slot], -records[slot][0], records[slot][1], slot))
    return records, versus, streaks, pct, order


def mini_goals(run, wins):
    run = run or {}
    values = {'wins': wins, 'games': len(run.get('results', ())),
              'qualified': int(0 in run.get('postseason', {}).get('seeds', ()))}
    return [dict(id=index, name=name, description=description, current_progress=min(target, values[metric]),
                 max_progress=target, is_complete=int(values[metric] >= target), xp_amount=xp, stubs_amount=stubs)
            for index, (metric, target, name, description, xp, stubs) in enumerate(MINI_MISSIONS, 1)]


def mini_missions(parent, run, wins):
    goals = mini_goals(run, wins)
    missions = mini_node(parent, 'missions', num_complete=sum(goal['is_complete'] for goal in goals),
                         total_xp=sum(goal['xp_amount'] for goal in goals), total_items=0,
                         earned_xp=sum(goal['xp_amount'] for goal in goals if goal['is_complete']),  # 0x3114c8
                         ms__aadabexac__p=len(goals))  # script hash 0x8d281c2f
    for goal in goals:
        mission = mini_node(missions, 'mission', ms__aaaaasafi_78=0, **goal)
        mini_node(mission, 'item', id=0, type_id=0, name='', short_description='', texture_name='')


def mini_challenges(state):
    run = state.get('mini_season')
    if run is not None:
        run, _ = mini_run(state, {})
    records, _, _, _, order = mini_standings(run)
    wins, losses = records[0]
    response = ET.Element('response')
    description = 'Play a season with your Diamond Dynasty squad.'
    if run and run.get('postseason'):
        champion = mini_team(state, run, run['postseason']['champion'])['name']
        description = f'Season complete. Finished #{order.index(0) + 1}. Champion: {champion}.'
    row = mini_node(response, 'challenge', mission_id=1, run_id=run['id'] if run else 0,
                    start=int(run is not None), classic=1, cur_game=wins + losses, wins=wins, losses=losses,
                    rank=order.index(0) + 1, seconds_played=run['seconds'] if run else 0,
                    name='Classic', description=description,
                    texture='MINISEASONS_CLASSIC', time_left='', time_colour=0)
    mini_missions(row, run, wins)
    return mini_xml(response)


def mini_overall(squad):
    everyone = [card for group in squad for card in group]
    return sum(card['ovr'] for card in everyone) // max(1, len(everyone))


def mini_sim_game(run, home, away, key):
    rng = random.Random(f"{run['id']}:{key}:{home}:{away}")
    difference = run['strengths'][home] - run['strengths'][away]
    home_rate = max(.08, min(.65, .29 + difference * .006))
    away_rate = max(.08, min(.65, .27 - difference * .006))
    hs = sum(rng.random() < home_rate for _ in range(10))
    aws = sum(rng.random() < away_rate for _ in range(10))
    if hs == aws:
        home_won = rng.random() < max(.1, min(.9, .52 + difference * .015))
        hs += int(home_won)
        aws += int(not home_won)
    return [hs, aws]


def mini_sim_series(run, seed_a, seed_b, name):
    # Local best-of-five fallback. The interactive state-3/4 series contract is unverified.
    wins = dict.fromkeys((seed_a, seed_b), 0)
    games = []
    for game in range(5):
        home, away = (seed_a, seed_b) if game in (0, 1, 4) else (seed_b, seed_a)
        hs, aws = mini_sim_game(run, home, away, f'{name}:{game}')
        winner = home if hs > aws else away
        wins[winner] += 1
        games.append({'home': home, 'away': away, 'home_runs': hs, 'away_runs': aws})
        if wins[winner] == 3:
            return {'name': name, 'teams': [seed_a, seed_b], 'games': games, 'winner': winner}


def mini_advance_league(state, run, rosters=None):
    """Backfill saved stage-2 runs and commit peer results and goal grants with each player result."""
    changed = False
    if 'strengths' not in run:
        if rosters is None:
            rosters = [mini_roster(run, slot) for slot in range(8)]
        run['strengths'] = [mini_overall(squad) for squad in rosters]
        changed = True
    peers = run.setdefault('cpu_results', [])
    for day in range(len(peers), len(run['results'])):
        peers.append([mini_sim_game(run, home, away, f'regular:{day}:{slot}')
                      for slot, (home, away) in enumerate(run['days'][day][1:], 1)])
        changed = True
    records, _, _, _, order = mini_standings(run)
    if len(run['results']) == len(run['days']) and 'postseason' not in run:
        first = mini_sim_series(run, order[0], order[3], 'semifinal-1')
        second = mini_sim_series(run, order[1], order[2], 'semifinal-2')
        final = mini_sim_series(run, first['winner'], second['winner'], 'final')
        run['postseason'] = {'seeds': order[:4], 'series': [first, second, final], 'champion': final['winner']}
        changed = True
    claims = run.setdefault('mission_claims', [])
    for goal in mini_goals(run, records[0][0]):
        if goal['is_complete'] and goal['id'] not in claims:
            profile = state.setdefault('profile', {})
            old_xp = profile.get('xp', 0)
            profile['xp'] = min(0xffffffff, old_xp + goal['xp_amount'])
            profile['pending_xp'] = profile.get('pending_xp', 0) + profile['xp'] - old_xp
            profile['stubs'] = profile.get('stubs', 0) + goal['stubs_amount']
            claims.append(goal['id'])
            changed = True
    return changed


def mini_restart(state, old):
    from copy import deepcopy
    state.setdefault('mini_season_archive', {})[str(old['id'])] = old
    run = {'version': 1, 'id': old['id'] + 1, 'clubs': list(old['clubs']), 'days': mini_days(),
           'squad': deepcopy(old['squad']), 'results': [], 'uploads': {}, 'pending': None, 'seconds': 0}
    state['mini_season'] = run
    return run


def mini_preview(parent, team, squad):
    lineup, dh, _, starters, relief = squad
    batters = sorted(lineup + dh, key=lambda card: card.get('lineup_ord', 0))
    stats = squad_ratings(batters, starters + relief)
    row = mini_node(parent, 'squad', name=team['name'], overall=mini_overall(squad), pitching=stats['pit'],
                    contact=stats['con'], power=stats['pwr'], speed=stats['spd'], defense=stats['def_'],
                    ms__aaaaadxaxz_7=0)  # script hash 0x950c1968
    for name, cards_in_group in (('lineup', batters), ('rotation', starters)):
        group = mini_node(row, name)
        for card in cards_in_group:
            group.append(ET.fromstring('<item %s/>' % card_attrs(
                card, bat_hand=HAND.get(card.get('bat_hand'), 0), type_id=0)))


def mini_empty_stats(parent):
    return mini_node(parent, 'stats', avg_show=0, era_show=0, ops_show=0, hr_show=0, k_show=0,
                     sv_show=0, mvp_show=0, ms__aaataap9mo3_=0)  # script hash 0x801c7d4d


def mini_stat_fields(counts):
    count = Counter(counts)
    result = {key: count[key] for key in MINI_STATS}
    hits, ab, bb, hbp, sf = (count[key] for key in ('b_h', 'b_ab', 'b_bb', 'b_hbp', 'b_sf'))
    extra = count['b_double'] + 2 * count['b_triple'] + 3 * count['b_hr']
    chances = count['b_putout'] + count['b_assist']
    result.update(
        b_avg=hits / max(1, ab), b_iso=extra / max(1, ab),
        b_ops=(hits + bb + hbp) / max(1, ab + bb + hbp + sf) + (hits + extra) / max(1, ab),
        b_risp_avg=count['b_risp_h'] / max(1, count['b_risp_ab']),
        b_fielding_pct=chances / max(1, chances + count['b_error']),
        # Run 96 reports 9 outs for a complete three-inning pitcher.
        p_era=27 * count['p_er'] / max(1, count['p_ip']),
        p_k_per_9=27 * count['p_k'] / max(1, count['p_ip']),
    )
    return result


def mini_leaders(parent, run):
    stats = mini_empty_stats(parent)
    totals = run.get('player_totals', {})
    if not totals:
        return
    catalogue = {card['item_id']: card for card in all_cards()}
    players = [(int(slot), catalogue[int(item)], counts, mini_stat_fields(counts))
               for slot, rows in totals.items() for item, counts in rows.items() if int(item) in catalogue]
    categories = (
        ('avg', 'b_avg', 'b_ab', False), ('era', 'p_era', 'p_ip', True), ('ops', 'b_ops', 'b_ab', False),
        ('hr', 'b_hr', 'b_hr', False), ('k', 'p_k', 'p_k', False), ('sv', 'p_sv', 'p_sv', False),
    )
    for name, metric, qualifier, ascending in categories:
        qualified = [player for player in players if player[2].get(qualifier, 0) > 0]
        qualified.sort(key=lambda p: (p[3][metric] if ascending else -p[3][metric], p[0], p[1]['item_id']))
        leaders = qualified[:5]
        stats.set(f'{name}_show', str(len(leaders)))
        for slot, card, _, fields in leaders:
            value = fields[metric]
            attrs = card_attrs(card, team_idx=slot, type_id=0, value=f'{value:.3f}' if isinstance(value, float) else value)
            stats.append(ET.fromstring(f'<{name}_leader {attrs}/>'))


def mini_enter(state, query):
    if int(query.get('goal_id', 0)) not in (0, 1):
        raise MiniSeasonRefusal('Only Mini Seasons Classic is available.')
    run, created = mini_run(state, query, allow_create=True)
    start_new = int(query.get('start_new', 0))
    if start_new not in (0, 1, 2):
        raise MiniSeasonRefusal('Invalid Mini Seasons start selection.')
    # 0x9e09eb writes start_new=2 on Yes from the native season-complete dialog.
    complete = len(run['results']) == len(run['days'])
    if complete and start_new:
        mini_advance_league(state, run)
        run = mini_restart(state, run)
        created, complete = True, False
    day = len(run['results'])
    rosters = [mini_roster(run, slot) for slot in range(8)]
    advanced = mini_advance_league(state, run, rosters)
    teams = [mini_team(state, run, slot) for slot in range(8)]
    records, versus, streaks, pct, order = mini_standings(run)
    response = ET.Element('response')
    # 0x9cd8c0 remaps state 5 to 7, the completion branch in dd_MiniSeasonMain.
    mini = mini_node(response, 'mini_season', run_id=run['id'], state=5 if complete else 0,
                     is_new=int(created), cur_game=day,
                     name='Classic', time_played=run['seconds'], cpu_sim_day=-1, cpu_sim_game=-1,
                     pitcher_energy='', results=','.join(str(int(row['user_won'])) for row in run['results']))
    next_slots = {side: other for home, away in ([] if complete else run['days'][day])
                  for side, other in ((home, away), (away, home))}
    leader_wins, leader_losses = records[order[0]]
    for slot in order:
        wins, losses = records[slot]
        streak = streaks[slot]
        row = mini_node(mini, 'team', index=slot, name=teams[slot]['name'], abbrev=teams[slot]['abbreviation'],
                        overall=mini_overall(rosters[slot]), wins=wins, losses=losses, win_percent=f'{pct[slot]:.3f}',
                        games_back=f'{((leader_wins - wins) + (losses - leader_losses)) / 2:g}',
                        streak=(f'W{streak}' if streak > 0 else f'L{-streak}') if streak else '-',
                        next_opp='-' if complete else teams[next_slots[slot]]['abbreviation'], known=1,
                        eliminated=int(complete and slot not in run['postseason']['seeds']),
                        ms_rxm8cf=MINI_OWNERS[slot], ms_aerqklm=0)  # native account and park hashes
        for other, (head_wins, head_losses) in enumerate(versus[slot], 1):
            row.set(f'team{other}Record', f'{head_wins}-{head_losses}')
    schedule = mini_node(mini, 'schedule')
    for fixtures in run['days']:
        schedule_day = mini_node(schedule, 'day')
        for home, away in fixtures:
            mini_node(schedule_day, 'game', home_team=home, away_team=away)
    mini_leaders(mini, run)
    mini_missions(response, run, records[0][0])
    previews = mini_node(response, 'squads')
    for team, squad in zip(teams, rosters):
        mini_preview(previews, team, squad)
    reply = mini_xml(response)
    if created or advanced:
        save_state(state)
    return reply


def mini_team_slot(query):
    if 'mini_cpu_idx' in query:
        index = int(query['mini_cpu_idx'])
        if not 1 <= index <= 7:
            raise MiniSeasonRefusal('Unknown Mini Seasons opponent.')
        return index
    if 'player_id' in query:
        owner = int(query['player_id'])
        if owner not in MINI_OWNERS:
            raise MiniSeasonRefusal('Unknown Mini Seasons team owner.')
        return MINI_OWNERS.index(owner)
    slot = int(query.get('squad_type', 6)) - 6
    if not 0 <= slot <= 7:
        raise MiniSeasonRefusal('Unknown Mini Seasons squad type.')
    return slot


def mini_team_reply(state, run, slot):
    records, _, _, _, _ = mini_standings(run)
    team = mini_team(state, run, slot)
    team['mini_wins'], team['mini_losses'] = records[slot]
    return team_reply(mini_roster(run, slot), team)


def mini_side_slots(query, type_suffix, id_suffix=None):
    slots = {}
    for side in SUMMARY_SIDES:
        side_type = query.get(f'{side}_{type_suffix}')
        if side_type is None:
            raise MiniSeasonRefusal('The Mini Seasons request must name both squads.')
        slot = mini_team_slot({'squad_type': side_type})
        if id_suffix is not None and int(query.get(f'{side}_{id_suffix}', -1)) != MINI_OWNERS[slot]:
            raise MiniSeasonRefusal('Mini Seasons sides do not match the requested squads.')
        slots[side] = slot
    if slots['home'] == slots['away']:
        raise MiniSeasonRefusal('Choose two distinct Mini Seasons squads.')
    return slots


def mini_cpu_teams(state, run, query):
    # 0x9d00b0 reads repeated arena_team and a nested arena_players per side.
    slots = mini_side_slots(query, 'squad_type', 'team')
    response = ET.Element('response')
    for side, label in enumerate(SUMMARY_SIDES):
        team = ET.fromstring(mini_team_reply(state, run, slots[label]))
        row = team.find('arena_team')
        row.set('home_away', str(side))  # INFERRED convention from builder order at 0x9d0235.
        row.append(team.find('arena_players'))
        response.append(row)
    return mini_xml(response)


def mini_stats_reply(run, query):
    slots = mini_side_slots(query, 'squad')
    response = ET.Element('response')
    for side in SUMMARY_SIDES:
        group = mini_node(response, f'{side}_stats')
        for cards_in_group in mini_roster(run, slots[side]):
            for card in cards_in_group:
                card_id = card['item_id']
                totals = run.get('player_totals', {}).get(str(slots[side]), {})
                # Report #69 uses catalogue item IDs. The receiver compares player ID at +0xb4.
                mini_node(group, 'player', id=card_id, energy=255,
                          **mini_stat_fields(totals.get(str(card_id), {})))
    return mini_xml(response)


def mini_awards(run):
    response = ET.Element('response')
    awards = mini_node(response, 'awards')
    mini_leaders(awards, run)
    for group in ('mvp', 'cy', 'hank', 'roty', 'wacky', 'allstar'):
        mini_node(awards, group)
    return mini_xml(response)


def mini_start_game(state, run):
    day, slots = mini_fixture(run)
    if run['pending'] is None:
        teams, card_ids = {}, {}
        for side, slot in slots.items():
            card_ids[side] = [card['item_id'] for group in mini_roster(run, slot) for card in group]
            if slot:
                teams[side] = dict(mini_team(state, run, slot), owner_id=MINI_OWNERS[slot], owner_name='CPU')
            else:
                teams[side] = summary_local_team(state)
        run['pending'] = {'day': day, 'teams': teams, 'card_ids': card_ids, 'started_at': int(time.time())}
        save_state(state)
    return mini_ack(run, cur_game=day)


def mini_loading(state, run, query):
    # VERIFIED 0xb604be: both wire side IDs can be the local account. The fixture owns side placement.
    _, slots = mini_fixture(run)
    wire = tuple(int(query.get(f'{side}_team_id', -1)) for side in SUMMARY_SIDES)
    owners = tuple(MINI_OWNERS[slots[side]] for side in SUMMARY_SIDES)
    teams = tuple(mini_team(state, run, slots[side])['id'] for side in SUMMARY_SIDES)
    if wire not in ((ACCOUNT_ID, ACCOUNT_ID), owners, teams):
        raise MiniSeasonRefusal('The loading request does not match the scheduled Mini Seasons sides.')
    return loading_cards(*(mini_roster(run, slots[side]) for side in SUMMARY_SIDES))


def mini_game_mode(data):
    if data is not None:
        reader = Reader(data)
        if reader.string(check_length=True) == 'ARENA':
            reader.string(check_length=True)  # outcome
            reader.string(check_length=True)  # UUID
            reader.u32()
            return reader.u32()
    return None


def mini_saved_upload(state, key, digest=None):
    runs = (state.get('mini_season'), *reversed(tuple(state.get('mini_season_archive', {}).values())))
    for run in runs:
        if not isinstance(run, dict):
            continue
        uploads = run.get('uploads', {})
        saved = uploads.get(key) if digest is None else next(
            (game for game in uploads.values() if game.get('digest') == digest), None)
        if saved is not None:
            return run, saved
    return None, None


def mini_finish(state, run, data):
    record = decode_summary_record(data)
    key = record['uuid']
    if not key or len(key) > 128:
        raise MiniSeasonRefusal('The Mini Seasons upload needs a valid game ID.')
    digest = hashlib.sha256(data).hexdigest()
    # The game reuses ONE id for every game (the same uuid in runs 91, 92 and 96), so the id alone cannot tell a
    # repeated upload from the next game: the report's own bytes do. A report already recorded is acknowledged
    # again; a different report under the same id is the next game, and the earlier one is kept under id#n.
    prior_run, prior = mini_saved_upload(state, key, digest)
    if prior is not None:
        return mini_ack(prior_run, cur_game=prior['cur_game'])
    if key in run['uploads']:
        run['uploads']['%s#%d' % (key, len(run['uploads']))] = run['uploads'].pop(key)
    pending = run['pending']
    day, _ = mini_fixture(run)
    if pending is None or pending['day'] != day:
        raise MiniSeasonRefusal('No matching Mini Seasons game has been started.')
    teams = pending['teams']
    local = next(side for side in SUMMARY_SIDES if teams[side]['owner_id'] == ACCOUNT_ID)
    if (record['scheduled'] != 3
            or record['ids'] != {side: team['id'] for side, team in teams.items()}
            or [side for side in SUMMARY_SIDES if record['players'][side] == ACCOUNT_ID] != [local]):
        raise MiniSeasonRefusal('The upload does not match the scheduled three-inning Mini Seasons game.')
    home, away = (record['totals'][side]['runs'] for side in SUMMARY_SIDES)
    finished = record['outcome'] == 'FINISHED'
    if finished and home == away:
        raise MiniSeasonRefusal('A finished Mini Seasons game needs a winner.')
    if record['outcome'] not in ('FINISHED', 'QUIT', 'QUIT_ON'):
        raise MiniSeasonRefusal('Unsupported Mini Seasons game outcome.')
    try:
        innings, scores, errors = decode_line_score(record, teams)
    except ValueError:
        # INFERRED: the validated aggregate survives even when the optional inning log is incomplete.
        innings, scores, errors = (record['scheduled'] if finished else 0), {}, {}
    record.pop('log')
    now = int(time.time())
    # GUESS: a quit leaves the fixture replayable. Only a finished upload advances the season.
    if finished:
        run['results'].append({'uuid': key, 'home_won': home > away, 'user_won': (home > away) == (local == 'home')})
        run['seconds'] += max(0, now - pending['started_at'])
        profile = state.setdefault('profile', {})
        profile['games'] = profile.get('games', 0) + 1
        profile['stubs'] = profile.get('stubs', 0) + STUBS_PER_GAME
        cards = pending.get('card_ids', {}).get(local, ())
        clubs = (affinity_card_clubs(card_by_id(item_id) for item_id in cards[:9] + cards[13:18])
                 if len(cards) == 26 else affinity_squad_clubs(current_squad(run['squad'])))
        affinity_record_game(state, digest, clubs, (home > away) == (local == 'home'))
        mini_advance_league(state, run)
    run['uploads'][key] = {'teams': teams, 'record': record, 'innings': innings, 'scores': scores, 'errors': errors,
                           'created_at': now, 'cur_game': len(run['results']), 'digest': digest, 'day': day,
                           'card_ids': pending.get('card_ids', {})}
    run['pending'] = None
    save_state(state)
    return mini_ack(run, cur_game=len(run['results']))


def mini_stat_rows(text):
    columns = MINI_STAT_REPORT.split(',')
    rows = {}
    for row in text.split('|'):
        values = row.split(',')
        if len(values) != len(columns):
            raise MiniSeasonRefusal('The Mini Seasons player stat row has the wrong width.')
        fields = dict(zip(columns, values))
        item_id = int(fields['id'])
        counts = {key: int(fields[key]) for key in MINI_STAT_COUNTS}
        if item_id in rows or item_id <= 0 or any(not 0 <= n <= 100000 for n in counts.values()):
            raise MiniSeasonRefusal('Invalid Mini Seasons player statistics.')
        rows[item_id] = counts
    if len(rows) > 26:
        raise MiniSeasonRefusal('Too many Mini Seasons stat rows for one team.')
    return rows


def mini_apply_stats(state, run, query, slots):
    day = len(run['results'])
    # Request #69 in run 96 followed mlb_game.create. The report has no UUID, so only associate
    # the immediately preceding confirmed fixture while no newer game is pending.
    if (not day or run['pending'] is not None or
            [slots[side] for side in SUMMARY_SIDES] != run['days'][day - 1][0]):
        return
    result = run['results'][-1]
    saved = next((game for game in run['uploads'].values() if
                  game['cur_game'] == day and game['record']['outcome'] == 'FINISHED'), None)
    if saved is None or 'stat_digest' in saved:
        return
    if query.get('stat_names') != MINI_STAT_REPORT or int(query['winner']) != int(result['home_won']):
        raise MiniSeasonRefusal('The Mini Seasons stats do not match the uploaded game.')
    sides = {side: mini_stat_rows(query[f'{side}_stats']) for side in SUMMARY_SIDES}
    for side, rows in sides.items():
        roster = saved.get('card_ids', {}).get(side)
        totals = saved['record']['totals'][side]
        actual = tuple(sum(values[key] for values in rows.values()) for key in ('b_runs', 'b_h'))
        if (roster is not None and not rows.keys() <= set(roster)) or actual != (totals['runs'], totals['hits']):
            raise MiniSeasonRefusal('The Mini Seasons stat roster or totals disagree with the game upload.')
    payload = '|'.join(query[f'{side}_stats'] for side in SUMMARY_SIDES)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    if any(game.get('stat_digest') == digest for game in run['uploads'].values()):
        return
    totals = run.setdefault('player_totals', {})
    for side, rows in sides.items():
        players = totals.setdefault(str(slots[side]), {})
        for item_id, counts in rows.items():
            key = str(item_id)
            previous = Counter(players.get(key, {}))
            previous.update(counts)
            players[key] = dict(previous)
    saved['stat_digest'] = digest
    save_state(state)


def mini_score_ack(state, run, query):
    # CPU fixtures are generated on the server. Accept only the verified gameplay report.
    if int(query.get('sim_game', 0)) != -1:
        raise MiniSeasonRefusal('CPU league simulation is unavailable.')
    slots = mini_side_slots(query, 'squad', 'id')
    day = len(run['results'])
    reported = tuple(slots[side] for side in SUMMARY_SIDES)
    if not any(reported == tuple(fixtures[0]) for fixtures in run['days'][max(0, day - 1):day + 1]):
        raise MiniSeasonRefusal('The score report does not match a recent Mini Seasons fixture.')
    if any(f'{side}_stats' in query for side in SUMMARY_SIDES):
        mini_apply_stats(state, run, query, slots)
    response = ET.Element('response')
    mini_node(response, 'cpu_stat', cpu_sim_day=-1, cpu_sim_game=-1)
    return mini_xml(response)


def mini_summary(run, saved):
    scores = {side: {int(inning): runs for inning, runs in rows.items()} for side, rows in saved['scores'].items()}
    response = ET.fromstring(summary_response(saved['teams'], saved['record'], saved['innings'], scores,
                                              saved['errors'], saved['created_at']))
    summary = response.find('arena_game_summary')
    summary.set('game_type', 'Mini Seasons')
    # FRONTEND 0x3e2854 passes created_at directly to the text renderer.
    created = datetime.datetime.fromtimestamp(saved['created_at'], datetime.timezone.utc)
    summary.set('created_at', created.date().isoformat())
    finished = saved['record']['outcome'] == 'FINISHED'
    day = saved.get('day', saved['cur_game'] - int(finished))
    records, _, _, _, _ = mini_standings(run, through=day)
    home_won = summary.get('home_won') == '1'
    for side, slot in zip(SUMMARY_SIDES, run['days'][day][0]):
        wins, losses = records[slot]
        before = 1000 + 10 * (wins - losses)  # Local season rating.
        delta = (10 if home_won == (side == 'home') else -10) if finished else 0
        team = summary.find(f'{side}_team')
        for suffix in ('', '2', '3'):
            team.set(f'rating_init{suffix}', str(before))
            team.set(f'rating_change{suffix}', str(delta))
            team.set(f'rating_end{suffix}', str(before + delta))
    return mini_xml(response)


MINI_SERVICES = {
    'load': lambda state, run, q: mini_ack(run),
    'view_cpu_teams': mini_cpu_teams,
    'view_stats': lambda state, run, q: mini_stats_reply(run, q),
    'awards': lambda state, run, q: mini_awards(run),
    'start_game': lambda state, run, q: mini_start_game(state, run),
    'score_cpu_game': mini_score_ack,
}


def mini_scoped(query):
    types = ('squad_type', 'home_squad_type', 'away__squad_type', 'away_squad_type')
    return ('mini_run_id' in query or 'mini_cpu_idx' in query
            or any(str(query.get(name)) in MINI_TYPES for name in types))


def mini_shared(name, state, run, query, params):
    if name == 'arena_team.view':
        return mini_team_reply(state, run, mini_team_slot(query))
    if name == 'arena_squad.get_loading_screen':
        return mini_loading(state, run, query)
    if int(query.get('squad_type', 6)) != 6:
        raise MiniSeasonRefusal('Only the player squad is editable in Mini Seasons.')
    if name in ('arena_squad.view_inventory', 'arena_squad.generate_lineup'):
        return mini_ack(run)
    squad = run['squad']
    if name in ('arena_squad.filter_inventory', MINI_PREFIX + 'filtered_players'):
        reply = filter_inventory(params, squad)
        if ET.fromstring(reply).find('arena_cards/arena_card') is None:
            raise MiniSeasonRefusal('There are no eligible replacement players for this filter.')
        return reply
    team = mini_team(state, run, 0)
    if name == 'arena_squad.rarity_counts':
        return rarity_counts(params, squad, team)
    if name == 'arena_squad.view_lineup':
        return lineup_reply(mini_roster(run, 0), team, mini=True)
    if name in ('arena_squad.assign_player', 'arena_squad.swap_player', 'arena_squad.swap_position'):
        if run['pending'] is not None:
            raise MiniSeasonRefusal('Finish the current Mini Seasons game before editing its squad.')
        edit = assign_squad_slot if name.endswith('assign_player') else swap_squad_slots
        if not edit(squad, query):
            raise MiniSeasonRefusal('Invalid Mini Seasons lineup change.')
        reply = lineup_reply(mini_roster(run, 0), team, mini=True)
        save_state(state)
        return reply
    raise MiniSeasonRefusal('This Mini Seasons request is unavailable on the local server.')


def mini_reply(name, params, game_data):
    # None delegates an ordinary request to its original route.
    if (not name.startswith((MINI_PREFIX, 'arena_squad.'))
            and name not in ('arena_team.view', 'online.universal_profile_stats', 'mlb_game.create', 'arena_game.summary')):
        return None
    try:
        query = params_of(params)
        if not isinstance(query, dict):
            if name.startswith(MINI_PREFIX):
                raise MiniSeasonRefusal('Invalid Mini Seasons request parameters.')
            return None
        if name == 'mlb_game.create':
            try:
                mode = mini_game_mode(game_data)
            except (ValueError, struct.error):
                season = load_state().get('mini_season')
                if isinstance(season, dict) and season.get('pending') is not None:
                    raise MiniSeasonRefusal('The Mini Seasons game report is unreadable.') from None
                return None
            if mode != 6:
                return None
            state = load_state()
            run, _ = mini_run(state, query)
            inner = mini_finish(state, run, game_data)
        elif name == 'arena_game.summary':
            run, saved = mini_saved_upload(load_state(), query.get('uuid'))
            if saved is None:
                return None
            inner = mini_summary(run, saved)
        elif name == 'online.universal_profile_stats':
            if str(query.get('player_id')) != str(ACCOUNT_ID):
                return None
            response = ET.Element('response')
            mini_node(response, 'squad_stats', ovr=mini_overall(current_squad()))
            inner = mini_xml(response)
        elif name.startswith(MINI_PREFIX):
            service = name[len(MINI_PREFIX):]
            state = load_state()
            if service == 'view_challenges':
                inner = mini_challenges(state)
            elif service == 'enter':
                inner = mini_enter(state, query)
            elif service == 'filtered_players' or service in MINI_SERVICES:
                run, _ = mini_run(state, query)
                if service == 'filtered_players':
                    inner = mini_shared(name, state, run, query, params)
                else:
                    inner = MINI_SERVICES[service](state, run, query)
            else:
                raise MiniSeasonRefusal('This Mini Seasons request is unavailable on the local server.')
        elif mini_scoped(query):
            state = load_state()
            run, _ = mini_run(state, query)
            inner = mini_shared(name, state, run, query, params)
        else:
            return None
        if not inner:
            raise MiniSeasonRefusal('The local server could not construct the Mini Seasons reply.')
        return envelope(inner), 'made'
    except MiniSeasonRefusal as error:
        return envelope(status=1, message=escape_mini_attr(error)), 'refused'
    except Exception as error:
        print(f'Mini Seasons {name} failed ({type(error).__name__})', file=sys.stderr)
        return envelope(status=1, message='The local Mini Seasons reply could not be prepared.'), 'refused'


DYNAMIC = {'content_update.latest': content_update_reply, 'arena_team.create': arena_team_create,
           'arena_team.check_arena_state': arena_state, 'arena_squad.view_lineup': view_lineup,
           'inventory.view_collection': view_collection, 'arena_team.view': view_team,
           'inventory.client': equip_templates, 'inventory.view': equip_quantities, 'inventory.view2': equip_quantities,
           'arena_team.team_items': equip_team, 'arena_team.equip': lambda p: equip_team(p, change=True),
           'inventory.search_collection': lambda p: player_collection(params_of(p), search=True),
           'inventory.get_filter': get_collection_filter,
           'arena_team.view_by_team': view_by_team, 'arena_team.view_cpu_teams': view_cpu_teams,
           'arena_team.update_uniform': lambda p: arena_team_update(p, custom=True),
           'arena_team.update_uniform_id': arena_team_update,
           'arena_home.panels': home_panels, 'mlb_login.post_update': post_update,
           'collection.view_groups': lambda p: collections_reply('collection.view_groups', p),
           'collection.view_collections': lambda p: collections_reply('collection.view_collections', p),
           'collection.view': lambda p: collections_reply('collection.view', p),
           'arena_buy_stadiums.unlocked_stadiums': unlocked_stadiums, 'arena_buy_stadiums.unlocked_stadiums2': unlocked_stadiums,
           'arena_squad.filter_inventory': filter_inventory, 'inventory.view_item': view_item,
           'arena_squad.rarity_counts': rarity_counts,
           'arena_squad.assign_player': squad_assign, 'arena_squad.swap_player': squad_swap,
           'arena_squad.swap_position': squad_swap,
           'mlb_game.create': game_create, 'mlb_game.view_post_game': view_post_game,
           'arena_game.summary': arena_game_summary,
           'program.all_progress': all_progress, 'program.list': program_list, 'program.view': program_view,
           'mission.view_slots': mission_slots, 'reward_game.add_bonus_xp': add_bonus_xp,
           'universal_profile.update_currency': update_currency}


# Replies are remade from this file whenever it changes, WITHOUT restarting the server: a restart
# drops the game's connection and sends the player back to the main menu. Only the reply code is
# taken from the new copy; the connection handling keeps running as started.
_reload = {'mtime': None, 'module': None, 'note': None}


def live_module():
    if globals().get('_IS_RELOAD'):
        return None
    try:
        mtime = os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return _reload['module']
    if _reload['mtime'] is None:
        _reload['mtime'] = mtime
    if mtime != _reload['mtime']:
        _reload['mtime'] = mtime
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location('olserver_live', os.path.abspath(__file__))
            module = importlib.util.module_from_spec(spec)
            module._IS_RELOAD = True
            module._INHERITED_LAYOUT = LAYOUT
            spec.loader.exec_module(module)
            module.COLLECTION, module.ROSTER_SOURCE = COLLECTION, ROSTER_SOURCE
            module.CARD_SOURCE = CARD_SOURCE
            if LAYOUT.install or CARD_SOURCE == 'disc':
                module.STATE_FILE = STATE_FILE
            if LAYOUT.install:
                module._content_info = _content_info
            _reload['module'], _reload['note'] = module, 'reply code reloaded'
        except Exception as e:                      # a broken edit keeps the last good copy
            _reload['note'] = 'reply code NOT reloaded: %r' % (e,)
    return _reload['module']


# Modes whose screens lock up on empty replies get a refusal instead (status id 1 and a text), and the
# game shows the text and leaves the screen (confirmed for Conquest in run 83; it then also shows
# a "NETWORK ERROR" box with the same text). Conquest (arena_quests.*) asked view / view_challenges / content_season_check /
# skip_phase in an endless loop on empty replies and could not be left (run 82).
# Owner's decisions of 2026-11-20: the Community Market stays off for good; Stubs cannot be bought, they are earned
# by playing; packs are bought with Stubs.
EARN_STUBS_TEXT = 'Stubs cannot be bought on the local server. Play games to earn Stubs.'
EMPTY_MARKET_REPLIES = {
    'marketplace.view': ('<response><items page="1" total_pages="1" count="0" '
                         'has_newly_completed_orders="false"/><search_filters/></response>'),
    'marketplace.view_my_orders': '<response><orders page="1" total_pages="1" count="0"/></response>',
    'marketplace.view_completed_orders': '<response><orders page="1" total_pages="1" count="0"/></response>',
    'marketplace.view_listing': '<response><marketplace owned="0" sellable="0"/></response>',
}
DISABLED = (('marketplace.', 'The Community Market is not part of the local server.'),
            ('arena_tutorial.marketplace.', 'The Community Market is not part of the local server.'))
NOT_OFFERED = ('arena_matchups.',)   # arena_matchups.list answered empty crashed run 93 (read at 0x18 after the reply)


class PackDraw(NamedTuple):
    count: int
    series: tuple = ('Live',)
    rarity: str = ''
    teams: tuple = ()
    kind: str = 'Player'


class ShopPack(NamedTuple):
    name: str
    texture: str
    source: str
    cost: int
    draws: tuple
    chase: str = ''


# Names, prices and contents are local inventions except where the disc supplies a label.
# The shared pack face uses the listed art independently of these type-4 reward rules.
SHOP_PACKS = {
    # Zero cost marks reward-only packs. They are granted directly and use the regular opener.
    510060: ShopPack('Bronze Exchange Player Pack', 'PACK_EXCHANGE_BRONZE', 'base', 0, (PackDraw(1, rarity='Bronze'),)),
    510061: ShopPack('Silver Exchange Player Pack', 'PACK_EXCHANGE_SILVER', 'base', 0, (PackDraw(1, rarity='Silver'),)),
    510062: ShopPack('Gold Exchange Player Pack', 'PACK_EXCHANGE_GOLD', 'base', 0, (PackDraw(1, rarity='Gold'),)),
    510001: ShopPack('Standard Player Pack', 'PACK_THESHOW', 'base', 200, (PackDraw(4),)),
    510002: ShopPack('Player Pack Bundle', 'PACK_SCOUTINGREPORT', 'base', 350, (PackDraw(8),)),
    510003: ShopPack('Diamond Headliner Pack', 'PACK_HEADLINERS_01', 'base', 900,
                     (PackDraw(3), PackDraw(1, rarity='Diamond'))),
    510004: ShopPack('Bronze Player Pack', 'PACK_THESHOW', 'base', 250, (PackDraw(2), PackDraw(1, rarity='Bronze'))),
    510005: ShopPack('Silver Player Pack', 'PACK_BALLINHABIT', 'base', 350, (PackDraw(2), PackDraw(1, rarity='Silver'))),
    510006: ShopPack('Gold Player Pack', 'PACK_BALLINCONTROL', 'base', 500, (PackDraw(2), PackDraw(1, rarity='Gold'))),
    510010: ShopPack('AL East Live Pack', 'PACK_ALEAST_LIVE', 'base', 250, (PackDraw(4, teams=('BAL', 'BOS', 'NYY', 'TB', 'TOR')),)),
    510011: ShopPack('AL Central Live Pack', 'PACK_ALCENTRAL_LIVE', 'base', 250, (PackDraw(4, teams=('CHW', 'CLE', 'DET', 'KC', 'MIN')),)),
    510012: ShopPack('AL West Live Pack', 'PACK_ALWEST_LIVE', 'base', 250, (PackDraw(4, teams=('HOU', 'LAA', 'OAK', 'SEA', 'TEX')),)),
    510013: ShopPack('NL East Live Pack', 'PACK_NLEAST_LIVE', 'base', 250, (PackDraw(4, teams=('ATL', 'MIA', 'NYM', 'PHI', 'WAS')),)),
    510014: ShopPack('NL Central Live Pack', 'PACK_NLCENTRAL_LIVE', 'base', 250, (PackDraw(4, teams=('CHC', 'CIN', 'MIL', 'PIT', 'STL')),)),
    510015: ShopPack('NL West Live Pack', 'PACK_NLWEST_LIVE', 'base', 250, (PackDraw(4, teams=('ARI', 'COL', 'LAD', 'SD', 'SF')),)),
    510020: ShopPack('All-Star Pack', 'PACK_RANKEDSEASON_AS', 'base', 450, (PackDraw(2, ('All-Star', 'All-Star Game', '2023 All-Star')),)),
    510021: ShopPack('Awards Pack', 'PACK_CAREERTRIBUTE', 'update', 450, (PackDraw(2, ('Awards',)),)),
    510022: ShopPack('Milestone Pack', 'PACK_DIAMONDDREAM', 'base', 450, (PackDraw(2, ('Milestone',)),)),
    510023: ShopPack('Signature Pack', 'PACK_HEADLINERS_01', 'base', 450, (PackDraw(2, ('Signature',)),)),
    510024: ShopPack('Topps Now Pack', 'PACK_SCOUTINGREPORT', 'base', 450, (PackDraw(2, ('Topps Now',)),)),
    510025: ShopPack('Finest Pack', 'PACK_THESHOW', 'base', 450, (PackDraw(2, ('Finest', '2023 Finest')),)),
    510026: ShopPack('Future Stars Pack', 'PACK_SCOUTINGREPORT', 'base', 450, (PackDraw(2, ('Future Stars',)),)),
    510027: ShopPack('Monthly Awards Pack', 'PACK_SCOUTINGREPORT_SEPTEMBER', 'update', 450, (PackDraw(2, ('Monthly Awards',)),)),
    510028: ShopPack('Postseason Pack', 'PACK_PENNANTCHASE', 'update', 450, (PackDraw(2, ('Postseason', '2023 Postseason')),)),
    510029: ShopPack('Captain Pack', 'PACK_THESHOW', 'base', 450, (PackDraw(2, ('Captain',)),)),
    510030: ShopPack('Kaiju Pack', 'PACK_THESHOW_KAIJUTAKEOVER', 'update', 450, (PackDraw(2, ('Kaiju',)),)),
    510031: ShopPack('Home Run Derby Pack', 'PACK_BALLINCONTROL', 'base', 450, (PackDraw(2, ('Home Run Derby', '2023 Home Run Derby')),)),
    510040: ShopPack('Diamond Duos Pack', 'PACK_DIAMONDDUOS_01', 'update', 1200, (PackDraw(2, rarity='Diamond'),)),
    510041: ShopPack('Chase Pack', 'PACK_CHASE07', 'update', 450, (PackDraw(4),), 'Shohei Ohtani'),
    510042: ShopPack('Equipment Pack', 'PACK_EQUIPMENT', 'base', 200, (PackDraw(2, kind='Equipment'),)),
    510043: ShopPack('Unlockables Pack', 'PACK_UNLOCKABLE', 'base', 200, (PackDraw(2, kind='Unlockable'),)),
    510044: ShopPack('Jumbo Player Bundle', 'PACK_THESHOW_JUMBO', 'update', 500, (PackDraw(12),)),
}
PACK_WEIGHTS = {'Common': 45, 'Bronze': 32, 'Silver': 16, 'Gold': 6, 'Diamond': 1}
PACK_RANDOM = random.SystemRandom()  # tests replace this with a seeded random.Random
SHOP_RECEIPT_WINDOW = 32
CHASE_CHANCE = .05


@lru_cache(maxsize=2)
def shop_pack_art(update_dir):
    registry = {'entries': [], 'files': {}, 'names': set()}
    if update_dir is None:
        return registry
    directory = os.path.join(update_dir, 'CROSS', 'LIVE')
    for pid, pack in SHOP_PACKS.items():
        if pack.source != 'update' or pack.texture in registry['names']:
            continue
        try:
            with open(os.path.join(directory, pack.texture + '.WEBP'), 'rb') as source:
                data = source.read(512 * 1024 + 1)
        except OSError:
            continue
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            if register_picture(registry, pack.texture, pid, f'pack-{pid}', data):
                registry['names'].add(pack.texture)
    return registry


class ShopRefusal(ValueError):
    pass


def shop_number(query, key):
    raw = str(query.get(key, ''))
    if not re.fullmatch(r'[0-9]{1,10}', raw):
        raise ShopRefusal('The pack request has an invalid item, product, or quantity.')
    return int(raw)


def shop_description(pack, rewards):
    first = pack.draws[0]
    count = sum(draw.count for draw in pack.draws)
    if first.kind != 'Player':
        return f'{count} random {first.kind.lower()} items.'
    series = 'Live Series' if first.series == ('Live',) else first.series[0]
    description = f'{count} {series} players'
    if first.teams:
        description += f' from the {pack.name.split(" Live")[0]}'
    description += '.'
    for draw in pack.draws:
        if draw.rarity:
            distinct = ' distinct' if draw.count > 1 else ''
            players = 'player' if draw.count == 1 else 'players'
            description += f' Includes {draw.count}{distinct} guaranteed {draw.rarity} {players}.'
    if pack.chase:
        return description + f' {CHASE_CHANCE:.0%} chance to pull Live Series {pack.chase}.'
    if rewards and not first.rarity:
        tiers = {card['rarity'] for card in rewards[0][0]}
        if tiers == {'Diamond'}:
            description += ' All are Diamond.'
        elif 'Diamond' in tiers:
            odds = 100 * PACK_WEIGHTS['Diamond'] / sum(PACK_WEIGHTS[tier] for tier in tiers)
            description += f' {odds:.2g}% Diamond chance per random player.'
    return description


def shop_picture_available(pack):
    if pack.source == 'update':
        return pack.texture in shop_pack_art(UPDATE_DIR)['names']
    return os.path.isfile(os.path.join(LAYOUT.base, 'CROSS', 'LIVE', pack.texture + '.WEBP'))


def shop_shelf(product_id=0):
    response = ET.Element('response')
    products = ET.SubElement(response, 'products')
    for pid, pack in SHOP_PACKS.items():
        if pack.cost <= 0 or (product_id and pid != product_id):
            continue
        if not shop_picture_available(pack):
            continue
        rewards = shop_reward_pools(pack)
        if rewards is None:
            continue
        cost = str(pack.cost)
        product = ET.SubElement(
            products, 'product', id=str(pid), name=pack.name, description=shop_description(pack, rewards),
            texture_name=pack.texture, cost=cost, display_cost=cost,
            # Keep the display operands present. Zero hides the limit panel by inference.
            time_remaining='0', purchase_limit='0', purchase_limit_used='0',
            purchase_limit_tab='0', quantity_remaining='2147483647', odds_valid='0',
        )
        ET.SubElement(product, 'item_exclusives')
    return ET.tostring(response, encoding='unicode')


def shop_owned_packs(shop):
    response = ET.Element('response')
    items = ET.SubElement(response, 'items')
    descriptions = {}
    for item_id, product_id in shop.get('owned_packs', {}).items():
        pack = SHOP_PACKS[product_id]
        if product_id not in descriptions:
            descriptions[product_id] = shop_description(pack, shop_reward_pools(pack))
        item = ET.SubElement(
            items, 'item', id=item_id, item_id=item_id, type_id='4', quantity='1',
            rarity_id=str(max(RARITY_ID.get(draw.rarity, 0) for draw in pack.draws)),
            anim_index='0', item_draft_pack_id='0', name=pack.name,
            texture_name=pack.texture if shop_picture_available(pack) else 'PACK_THESHOW',
            description=descriptions[product_id], odds_valid='0', no_sell='1', quantity_tab_index='0',
            is_live_set='0', captain='0', captained='0', has_rank_change='0', in_squad='0', set_name='',
        )
        ET.SubElement(item, 'item_exclusives')
    return ET.tostring(response, encoding='unicode')


def shop_reward_pools(pack):
    pools = []
    chase = None
    aliases = {'CWS': 'CHW', 'WSH': 'WAS'}
    for draw in pack.draws:
        if draw.kind == 'Player':
            pool = [
                card for card in all_cards()
                if card.get('series') in draw.series and card['rarity'] in PACK_WEIGHTS
                and card['roster_player_id'] > 0 and card['team_id'] >= 0 and card['display_position'] in FIELD_NUMBER
                and (not draw.rarity or card['rarity'] == draw.rarity)
                and (not draw.teams or aliases.get(card.get('team_short_name'), card.get('team_short_name')) in draw.teams)
            ]
            if pack.chase:
                chase = next((c for c in pool if c['name'].strip() == pack.chase and c['rarity'] == 'Diamond'), None)
                if chase is None:
                    return None
                pool = [c for c in pool if c['name'].strip() != pack.chase]
        else:
            inv = inventory_data()
            pool = []
            for item in inv['catalogue'].get(draw.kind, []):
                if draw.kind == 'Equipment' and _item_key(item.get('brand', '')) not in inv['brands']:
                    continue
                if draw.kind == 'Unlockable' and str(item.get('sub_category_id')) not in {
                    '10000', '10001', '10002', '10003', '10004', '10005', '10006', '10007', '10008'
                }:
                    continue
                if inventory_item(draw.kind, item) is not None:
                    pool.append(item)
        if len(pool) < (draw.count if draw.rarity else 1):
            return None
        pools.append(pool)
    return pools, chase


def shop_draw_rewards(pack, pools, chase):
    rewards = []
    for draw, pool in zip(pack.draws, pools):
        if draw.rarity:
            selection = PACK_RANDOM.sample(pool, draw.count)
        elif draw.kind == 'Player':
            by_rarity = {}
            for card in pool:
                by_rarity.setdefault(card['rarity'], []).append(card)
            tiers = PACK_RANDOM.choices(list(by_rarity), [PACK_WEIGHTS[r] for r in by_rarity], k=draw.count)
            selection = [PACK_RANDOM.choice(by_rarity[tier]) for tier in tiers]
        else:
            selection = PACK_RANDOM.choices(pool, k=draw.count)
        rewards.extend((draw.kind, item) for item in selection)
    if chase and PACK_RANDOM.random() < CHASE_CHANCE:
        rewards[-1] = ('Player', chase)
    return rewards


def shop_purchase_receipt(receipt_id):
    return f'<response><purchase uuid="{receipt_id}" item_type_id="4"/></response>'


def save_shop_state(state, shop):
    # Keep a finite replay window. Consumed instance IDs are never reused, so an
    # expired open cannot grant cards again.
    for key in ('purchases', 'opened_packs'):
        receipts = shop.get(key)
        if receipts is not None:
            while len(receipts) > SHOP_RECEIPT_WINDOW:
                del receipts[next(iter(receipts))]
    save_state(state)


def grant_owned_pack(shop, product_id):
    item_id = shop.get('next_pack_id', 610001)
    shop['next_pack_id'] = item_id + 1
    shop.setdefault('owned_packs', {})[str(item_id)] = product_id


def shop_buy(state, shop, query):
    pid = shop_number(query, 'product_id')
    if pid not in SHOP_PACKS or SHOP_PACKS[pid].cost <= 0:
        raise ShopRefusal('That pack is unavailable on the local server.')
    if shop_number(query, 'quantity') != 1:
        raise ShopRefusal('Buy one pack at a time.')
    pack = SHOP_PACKS[pid]
    profile = state.setdefault('profile', {})
    balance = profile.get('stubs', 0)
    if balance < pack.cost:
        raise ShopRefusal('Not enough Stubs for this pack. Play games to earn Stubs.')
    if not shop_picture_available(pack) or shop_reward_pools(pack) is None:
        raise ShopRefusal('That pack is unavailable with the local catalogue or pack art.')
    receipt_id = str(uuid4())
    grant_owned_pack(shop, pid)
    shop.setdefault('purchases', {})[receipt_id] = None
    profile['stubs'] = balance - pack.cost
    save_shop_state(state, shop)
    return shop_purchase_receipt(receipt_id)


def shop_revealed_card(card, quantity, *, is_new):
    pos = FIELD_NUMBER[card['display_position']]
    return '<item %s/>' % card_attrs(
        card, type_id=0, quantity=quantity, is_new=int(is_new), position=pos,
        anim_index=0, no_sell=1, quick_sell_value=0, marketplace_best_buy=0, quantity_tab_index=0,
        legend=0, series_year=2023, series_name='Live Series' if card['series'] == 'Live' else card['series'],
        long_position=card['display_position'],
        league_logo='', series_texture_name='', has_augment=0, has_matchup=0, has_rank_change=0,
        sequence=0, seq_level=0, stars=0, new_rank=card['ovr'], in_squad=0,
        captain=0, captained=0, is_live_set=0, set_name='', **compare_attrs(card, pos), **squad_card_text(card, pos),
    )


def shop_open(state, shop, query):
    item_id = str(shop_number(query, 'item_id'))
    opened = shop.get('opened_packs', {})
    if item_id in opened:
        return opened[item_id]
    owned = shop.get('owned_packs', {})
    if item_id not in owned:
        raise ShopRefusal('That pack is not in your inventory.')
    pack = SHOP_PACKS[owned[item_id]]
    pack_pools = shop_reward_pools(pack)
    if pack_pools is None:
        raise ShopRefusal('The local reward pool for this pack is unavailable.')
    draws = shop_draw_rewards(pack, *pack_pools)
    gained = Counter((kind, item['uuid']) for kind, item in draws)
    old_quantities = {}
    for kind in {kind for kind, _ in draws}:
        owned_rows = cards(state) if kind == 'Player' else owned_inventory_rows(kind, state)
        old_quantities.update({(kind, item['uuid']): item['quantity'] for item in owned_rows})
    rows = []
    for kind, item in draws:
        key = (kind, item['uuid'])
        previous = old_quantities.get(key, 0)
        quantity = previous + gained[key]
        if kind == 'Player':
            row = shop_revealed_card(item, quantity, is_new=not previous)
        else:
            row = inventory_item(kind, item, quantity=quantity, is_new=int(not previous),
                                 no_sell=1, quick_sell_value=0, marketplace_best_buy=0, anim_index=0,
                                 quantity_tab_index=0, set_name='', position=0, role=0,
                                 roster_player_id=0, team_id=0, rank=0, original_rank=0,
                                 series_year=0, legend=0)
        rows.append(row)
    reply = '<response><items>%s</items></response>' % ''.join(rows)
    for (kind, card_id), count in gained.items():
        if kind == 'Player':
            gains = shop.setdefault('gained_cards', {})
        else:
            gains = shop.setdefault('gained_items', {}).setdefault(kind, {})
        gains[card_id] = gains.get(card_id, 0) + count
    del owned[item_id]
    # A recent repeat returns the exact same reveal without adding ownership again.
    shop.setdefault('opened_packs', {})[item_id] = reply
    save_shop_state(state, shop)
    return reply


def shop_reply(name, params):
    if name == 'show_shop_product.view_platform_products':
        return envelope('<response><products/><redeemables/></response>'), 'made'
    if name in ('inventory.check_for_rewards', 'arena_tutorial.inventory.view_packs'):
        return envelope('<response><items/></response>'), 'made'
    if name == 'inventory.view_packs':
        return envelope(shop_owned_packs(load_state().get('pack_shop', {}))), 'made'
    if name in ('inventory.draft_pack_open', 'inventory.draft_pack_submit_picks',
                'arena_tutorial.inventory.open_pack', 'inventory.quick_sell'):
        return envelope(status=1, message='This inventory action is unavailable on the local server.'), 'refused'
    if not (name.startswith('show_shop_product.') or name == 'inventory.open_pack'):
        return None
    try:
        query = params_of(params)
        if not isinstance(query, dict):
            raise ShopRefusal('Invalid pack request.')
        state = load_state()
        shop = state.setdefault('pack_shop', {})
        if name == 'show_shop_product.view':
            if query.get('filter') == 'STUBS':
                return envelope('<response><products/></response>'), 'made'
            if query.get('filter', 'PACKS') != 'PACKS':
                raise ShopRefusal(EARN_STUBS_TEXT)
            product_id = shop_number(query, 'filter_id') if 'filter_id' in query else 0
            inner = shop_shelf(product_id)
        elif name == 'show_shop_product.buy':
            inner = shop_buy(state, shop, query)
        elif name == 'inventory.open_pack':
            inner = shop_open(state, shop, query)
        elif name == 'show_shop_product.purchase_complete':
            receipt_id = query.get('purchase_uuid')
            if not isinstance(receipt_id, str) or receipt_id not in shop.get('purchases', {}):
                raise ShopRefusal(EARN_STUBS_TEXT)
            inner = shop_purchase_receipt(receipt_id)
        elif name == 'show_shop_product.redeem_all':
            # Sign-in checks for pending platform grants. Local packs are credited during buy.
            inner = '<response><purchases/></response>'
        elif name == 'show_shop_product.redeem':
            raise ShopRefusal('Platform entitlements cannot be redeemed on the local server.')
        else:
            raise ShopRefusal('This shop request is unavailable on the local server.')
        return envelope(inner), 'made'
    except ShopRefusal as error:
        return envelope(status=1, message=escape_mini_attr(error)), 'refused'
    except OSError:
        return envelope(status=1, message='The local pack request could not be saved or read.'), 'refused'



# ---------------------------------------------------------------- Player exchanges
EXCHANGE_GROUP_ID = 720001
# Group Select has four compiled slots, each bound to the matching vector index.
EXCHANGE_GROUPS = {
    EXCHANGE_GROUP_ID: ('Players', 'EXCHANGE_PLAYERS'),
    720002: ('BATTLE ROYALE', 'EXCHANGE_BR'),
    720003: ('MY BALLPLAYER', 'EXCHANGE_BALLPLAYER'),
    720004: ('ITEMS', 'EXCHANGE_ITEMS'),
}
EXCHANGE_PAGE_SIZE = 24
EXCHANGE_DESCRIPTION = 'description_aaaa819e____'  # script hash 0xf96c6a13
EXCHANGE_SHOW_POINTS = 'show_feed_value_aaadajazg_x_'  # ItemCard visibility, script hash 0xc370616d
EXCHANGE_LIMIT_TAB = 'repeat_limit_tab_aaaa6iaa__y8'  # script hash 0x80706642
EXCHANGE_REPEAT_COUNT = 'repeats_afaaa8tty_98'  # script hash 0xea102f17


class ExchangeRule(NamedTuple):
    rarity: str
    reward_rarity: str
    points: int
    target: int
    pack_id: int

    @property
    def description(self):
        return f'Exchange {self.rarity} Players and Receive a {self.reward_rarity} Player Pack'


# Tier values and the last two targets are local. Every feed visit opens a reusable tier.
EXCHANGE_RULES = {
    720101: ExchangeRule('Common', 'Bronze', 100, 250, 510060),
    720102: ExchangeRule('Bronze', 'Silver', 150, 400, 510061),
    720103: ExchangeRule('Silver', 'Gold', 200, 550, 510062),
}


def exchange_reward_available(rule):
    pack = SHOP_PACKS[rule.pack_id]
    return shop_picture_available(pack) and shop_reward_pools(pack) is not None


def exchange_groups():
    response = ET.Element('response')
    groups = ET.SubElement(response, 'mission_groups')
    for group_id, (name, texture) in EXCHANGE_GROUPS.items():
        mini_node(groups, 'mission_group', id=group_id, name=name, texture_name=texture)
    return mini_xml(response)


def exchange_missions(group_id):
    response = ET.Element('response')
    missions = ET.SubElement(response, 'missions')
    if group_id != EXCHANGE_GROUP_ID:
        name, _ = EXCHANGE_GROUPS[group_id]
        notice = mini_node(missions, 'mission', id=group_id, name='Nothing to exchange yet',
                           description=f'{name} exchanges are unavailable on the local server.',
                           criteria_id=0, is_complete='true', time_remaining=0, repeatable_tab=0,
                           **{EXCHANGE_LIMIT_TAB: 0, EXCHANGE_REPEAT_COUNT: 0})
        # The extras layout takes a zero-row vector and has no item preview.
        ET.SubElement(mini_node(notice, 'reward', display_index=3), 'extras')
        return mini_xml(response)
    for mission_id, rule in EXCHANGE_RULES.items():
        if not exchange_reward_available(rule):
            continue
        name = f'Exchange^n^{rule.rarity} Players for a {rule.reward_rarity} Player'
        mission = mini_node(missions, 'mission', id=mission_id, name=name, description=rule.description,
                            criteria_id=2, is_complete='false',
                            time_remaining=0, repeatable_tab=0,
                            **{EXCHANGE_LIMIT_TAB: 0, EXCHANGE_REPEAT_COUNT: 0})
        reward = mini_node(mission, 'reward', display_index=0)
        preview = mini_node(reward, 'item', **program_pack_fields(rule.pack_id),
                            quantity_tab_index=0, in_squad=0, has_rank_change=0)
        ET.SubElement(preview, 'item_exclusives')
        ET.SubElement(reward, 'extras')
    return mini_xml(response)


def exchange_card_number(value):
    text = str(value)
    return int(text) if re.fullmatch(r'[0-9]{1,10}', text) else 0


def exchange_protected_cards(state):
    protected = set()

    def visit(node):
        if isinstance(node, dict):
            protected.update(exchange_card_number(slot[0]) for slot in node.get('lineup', ())
                             if isinstance(slot, (list, tuple)) and slot)
            for name in ('bench', 'starters', 'relief', 'rotation', 'bullpen'):
                protected.update(exchange_card_number(card) for card in node.get(name, ())
                                 if isinstance(card, (int, str)))
            # Showdown saves player offers alongside the assembled squad.
            for entry in node.get('draft', ()):
                if isinstance(entry, dict) and entry.get('group') != 'perk':
                    protected.update(exchange_card_number(card) for card in entry.get('offers', ()))
            for child in node.values():
                visit(child)
        elif isinstance(node, (tuple, list)):
            for child in node:
                visit(child)

    visit(state.get('squad') or default_squad())
    for key in ('mini_season', 'mini_season_archive', 'conquest', 'showdown'):
        visit(state.get(key))
    return protected


def exchange_eligible(state, rule):
    protected = exchange_protected_cards(state)
    return {card['item_id']: card for card in cards(state)
            if card['rarity'] == rule.rarity and card['quantity'] > 1
            and card['item_id'] not in protected and card['display_position'] in FIELD_NUMBER}


def exchange_feed(state, mission_id, page):
    rule = EXCHANGE_RULES.get(mission_id)
    available = rule and exchange_reward_available(rule)
    eligible = list(exchange_eligible(state, rule).values()) if available else []
    pages = max(1, -(-len(eligible) // EXCHANGE_PAGE_SIZE))
    page = min(pages, max(1, page))
    start = (page - 1) * EXCHANGE_PAGE_SIZE
    visible = eligible[start:start + EXCHANGE_PAGE_SIZE]
    description = rule.description if rule else 'Exchange unavailable'
    target = rule.target if rule else 1
    items = []
    for card in visible:
        row = ET.fromstring(shop_revealed_card(card, card['quantity'] - 1, is_new=False))
        row.set('feed_value', str(rule.points))
        row.set(EXCHANGE_SHOW_POINTS, '1')
        row.set('is_feed', '0')
        items.append(row)
    response = ET.Element('response')
    feed = mini_node(response, 'feed', page=page, total_pages=pages)
    mini_node(feed, 'mission', id=mission_id, display_type=0, max_progress=target,
              current_progress=0, description=description,
              **{EXCHANGE_DESCRIPTION: description})
    mini_node(feed, 'items', count=len(visible), total=len(eligible)).extend(items)
    reply = mini_xml(response)
    exchange = state.get('exchanges', {})
    completed = exchange.get('completed', {})
    key = str(mission_id)
    if key in completed:
        # Receipts from the one-time implementation represent one prior completion.
        exchange.setdefault('completion_counts', {}).setdefault(key, 1)
        del completed[key]
        save_state(state)
    return reply


def exchange_selection(query):
    ids = query.get('item_ids')
    qtys = query.get('item_qtys')
    if not isinstance(ids, str) or not isinstance(qtys, str):
        return None
    ids = ids.removesuffix(',').split(',')
    qtys = qtys.removesuffix(',').split(',')
    if not 1 <= len(ids) == len(qtys) <= 99:
        return None
    rows = sorted([exchange_card_number(i), exchange_card_number(q)] for i, q in zip(ids, qtys))
    if len({i for i, _ in rows}) != len(rows) or any(not i or not qty for i, qty in rows):
        return None
    return rows


def exchange_advance(state, mission_id, query):
    selection = exchange_selection(query)
    if selection is None:
        return False
    exchange = state.setdefault('exchanges', {})
    completed = exchange.setdefault('completed', {})
    key = str(mission_id)
    if key in completed:
        return selection == completed[key]
    rule = EXCHANGE_RULES[mission_id]
    if not exchange_reward_available(rule):
        return False
    points = sum(qty for _, qty in selection) * rule.points
    if not rule.target <= points < rule.target + rule.points:
        return False
    eligible = exchange_eligible(state, rule)
    if any(i not in eligible or qty >= eligible[i]['quantity'] for i, qty in selection):
        return False
    spent = exchange.setdefault('spent_cards', {})
    for i, qty in selection:
        card_id = eligible[i]['uuid']
        spent[card_id] = spent.get(card_id, 0) + qty
    completed[key] = selection
    counts = exchange.setdefault('completion_counts', {})
    counts[key] = counts.get(key, 0) + 1
    grant_owned_pack(state.setdefault('pack_shop', {}), rule.pack_id)
    save_state(state)
    return True


def exchange_reply(name, params):
    if name not in ('mission.view_groups', 'mission.view_group', 'mission.view_exchange', 'mission.advance'):
        return None
    q = params_of(params)
    if not isinstance(q, dict):
        return None
    if name == 'mission.view_groups' and q.get('mission_group_category') == 'exchange':
        return exchange_groups()
    if name == 'mission.view_group':
        group_id = exchange_card_number(q.get('mission_group_id'))
        if group_id in EXCHANGE_GROUPS:
            return exchange_missions(group_id)
    mission_id = exchange_card_number(q.get('mission_id'))
    if name == 'mission.view_exchange':
        return exchange_feed(load_state(), mission_id, exchange_card_number(q.get('page')))
    if name == 'mission.advance' and mission_id in EXCHANGE_RULES:
        advanced = exchange_advance(load_state(), mission_id, q)
        return '<response><advancement_results state_id="%d"/></response>' % int(advanced)
    return None


# ---------------------------------------------------------------- Conquest, Batter Up 1
# v1.00 fixed sprite grid and reply readers: reports/online/proposals/conquest.md.
# Only the active positions are saved. The other 360 still occupy words on the wire.
from contextvars import ContextVar

CQ_ROWS = ((8,), (19, 20), (43, 44), (71, 72, 73), tuple(range(100, 111)),
           (138, 139, 140), (167, 168), (194, 195), (221,))
CQ_IDS = tuple(i for row in CQ_ROWS for i in row)
CQ_CHALLENGE = 101
CQ_START, CQ_CAPITAL, CQ_CLUB, CQ_MINE, CQ_EMPTY = 107, 109, 3, 30, 63
CQ_LEVELS = ('Rookie', 'Veteran', 'All-Star', 'Hall of Fame', 'Legend')
CQ_NO_STORY = '<response><stories/></response>'
CQ_MUTATIONS = frozenset('attack_empty reinforce move skip_phase restart update_active fight sim'.split())
CQ_CHOOSER = ContextVar('conquest_chooser_eligibility', default=False)
CQ_STEAL_PICKER = ContextVar('conquest_steal_picker', default=False)
CQ_STRONGHOLDS = (CQ_START, CQ_CAPITAL)
# Local, nonrepeatable rewards. No historical server campaign definitions were recovered.
class ConquestGoal(NamedTuple):
    id: int
    description: str
    target: int
    stubs: int
    xp: int
    pack: int


CQ_GOALS = (
    ConquestGoal(2, 'Own two territories.', 2, 25, 0, 0),
    ConquestGoal(3, 'Build your fan base to 9M.', 9, 0, 100, 0),
    ConquestGoal(1, 'Capture every stronghold. Reward: one Standard Player Pack.', len(CQ_STRONGHOLDS), 0, 0, 510001),
)

class ConquestRefusal(ValueError):
    pass


@lru_cache(maxsize=1)
def conquest_neighbors():
    # Absolute columns of the first pictured hex in each occupied native row (1..9).
    # The central row is contiguous. The gaps in the silhouette separate rows, not row IDs.
    starts = (9, 9, 8, 8, 7, 15, 15, 15, 15)
    grid = {(x, y): tile for y, (row, start) in enumerate(zip(CQ_ROWS, starts), 1)
            for x, tile in enumerate(row, start)}
    neighbors = {}
    for (x, y), tile in grid.items():
        left = x + (y % 2) - 1
        around = ((x-1, y), (x+1, y), (left, y-1), (left+1, y-1),
                  (left, y+1), (left+1, y+1))
        neighbors[tile] = tuple(grid[p] for p in around if p in grid)
    return neighbors


def conquest_fresh(state=None):
    cells = {str(i): [CQ_EMPTY, 0] for i in CQ_IDS}
    cells[str(CQ_START)] = [CQ_MINE, 8]
    cells[str(CQ_CAPITAL)] = [CQ_CLUB, 2]
    run = dict(challenge_id=CQ_CHALLENGE, cells=cells, turn=1, phase=0, pool=0, revision=0, shown=-1,
               last=None, pending=None, receipts={}, latest=None)
    conquest_init_campaign(run)
    conquest_progress({} if state is None else state, run, entering=True)
    return run


def conquest_init_campaign(run):
    run.update(version=2, seed=uuid4().int >> 64, completed=False, progress={}, prior_claims=[],
               notices=[], presented=[], last_cpu=None)


def conquest_check(run):
    if (run['version'] not in (1, 2) or run['challenge_id'] != CQ_CHALLENGE
            or run['phase'] not in (0, 3, 4, 5, 8) or run['turn'] < 1
            or not 0 <= run['pool'] <= 127 or set(run['cells']) != {str(i) for i in CQ_IDS}):
        raise ConquestRefusal('This Conquest save has an unsupported board.')
    for tile, (owner, fans) in run['cells'].items():
        # An enemy stronghold can retain just its separately encoded garrison.
        floor = 0 if owner == CQ_EMPTY or conquest_garrison(int(tile), owner) else 1
        if (owner not in (CQ_MINE, CQ_EMPTY, CQ_CLUB) or not isinstance(fans, int)
                or not floor <= fans <= 127 or (owner == CQ_EMPTY and fans != 0)):
            raise ConquestRefusal('This Conquest territory has an invalid fan count or owner.')
    if run['version'] == 2 and not isinstance(run['seed'], int):
        raise ConquestRefusal('This Conquest campaign has an invalid turn seed.')


def conquest_number(query, key):
    raw = str(query.get(key, ''))
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,8})', raw):
        raise ConquestRefusal('The Conquest request contains an invalid number.')
    return int(raw)


def conquest_cell(run, tile):
    if tile not in CQ_IDS:
        raise ConquestRefusal('That territory is outside the Batter Up 1 map.')
    return run['cells'][str(tile)]


def conquest_component(run, source):
    if conquest_cell(run, source)[0] != CQ_MINE:
        return set()
    reached, todo = {source}, [source]
    while todo:
        for tile in conquest_neighbors()[todo.pop()]:
            if tile not in reached and conquest_cell(run, tile)[0] == CQ_MINE:
                reached.add(tile)
                todo.append(tile)
    return reached


def conquest_reconcile_completion(run, claims):
    """Derive terminal state and goal 1 from the board, without granting or clawing back rewards."""
    caps = sum(conquest_cell(run, tile)[0] == CQ_MINE for tile in CQ_STRONGHOLDS)
    won = caps == len(CQ_STRONGHOLDS)
    changed = run.get('completed') != won or run['progress'].get('1') != caps
    run['completed'] = won
    run['progress']['1'] = caps
    if won or run['phase'] == 8:
        phase = 8 if won else 0
        if phase != run['phase'] or run['pool'] != 0:
            run.update(phase=phase, pool=0, last=None)
            changed = True
    if not won:
        for key in ('notices', 'presented'):
            if 1 in run[key]:
                run[key] = [goal for goal in run[key] if goal != 1]
                changed = True
        if 1 in claims and 1 not in run['prior_claims']:
            run['prior_claims'].append(1)
            changed = True
    return changed


def conquest_progress(state, run, *, entering=False):
    owned = [fans for owner, fans in run['cells'].values() if owner == CQ_MINE]
    profile = state.setdefault('profile', {})
    claims = profile.setdefault('conquest_batter_up_1_claims', [])
    if entering:
        run['prior_claims'] = list(claims)
    conquest_reconcile_completion(run, claims)
    metrics = {2: len(owned), 3: sum(owned), 1: run['progress']['1']}
    for goal in CQ_GOALS:
        key = str(goal.id)
        value = max(run['progress'].get(key, 0), min(goal.target, metrics[goal.id]))
        run['progress'][key] = value
        if value < goal.target or goal.id in claims:
            continue
        if goal.xp:
            xp = profile.get('xp', 0)
            added = max(0, min(goal.xp, 0xffffffff - xp))
            profile['xp'] = xp + added
            profile['pending_xp'] = profile.get('pending_xp', 0) + added
        if goal.stubs:
            profile['stubs'] = profile.get('stubs', 0) + goal.stubs
        if goal.pack:
            grant_owned_pack(state.setdefault('pack_shop', {}), goal.pack)
        claims.append(goal.id)
        run['notices'].append(goal.id)


def conquest_goal(parent, run, rule, number):
    value = run['progress'][str(rule.id)]
    already_paid = rule.id in run['prior_claims']
    text = rule.description
    if already_paid:
        text += ' Reward already claimed.'
    goal = mini_node(parent, 'goal', id=rule.id, number=number, description=text,
                     complete=int(value == rule.target), is_repeatable=0, display_index=0, has_progress=1,
                     progress_cur=value, progress_max=rule.target, stubs_amount=0 if already_paid else rule.stubs,
                     xp_amount=0 if already_paid else rule.xp, item_id=0)
    mini_node(goal, 'item', id=0, type_id=0, rarity_id=0, rank=0, name='', texture_name='')


def conquest_chooser(run):
    response = ET.Element('response')
    group = mini_node(response, 'challenges', auto_load=-1)
    row = mini_node(group, 'challenge', id=run['challenge_id'], name='Batter Up 1 (local)',
                    description='Capture the Rays stronghold. In Attack, press X on your tile with 2M+ fans, then press X on an adjacent target. In Move, press X on a friendly 2M+ tile, then X on a highlighted friendly destination and choose an amount.',
                    preview_texture='CONQUEST_BATTERUP1', requires_szn=0, timed=0, time_text='')
    count = sum(run['progress'][str(g.id)] == g.target for g in CQ_GOALS)
    goals = mini_node(row, 'goals', num_complete=count)
    for number, rule in enumerate(CQ_GOALS, 1):
        conquest_goal(goals, run, rule, number)
    return mini_xml(response)


def conquest_move_targets(run, source):
    # The Move dialog increments the displayed destination total only below 99 (0x4e4158).
    return sorted(i for i in conquest_component(run, source) if i != source and conquest_cell(run, i)[1] < 99)


def conquest_move_sources(run):
    return [i for i in CQ_IDS if conquest_cell(run, i)[0] == CQ_MINE and conquest_cell(run, i)[1] >= 2
            and conquest_move_targets(run, i)]


def conquest_start_hex(run):
    if run['phase'] == 5:
        return max(conquest_move_sources(run), key=lambda i: conquest_cell(run, i)[1], default=CQ_START)
    if run['phase'] != 0:
        return CQ_START
    # ArenaQuest_GenSelList (0xe06d7a) arms a friendly stack first; only its
    # neighbors can then reach challenge_team_view. START_HEX positions the cursor.
    selected = CQ_START
    best = (False, 0, False)
    for tile, adjacent in conquest_neighbors().items():
        owner, fans = conquest_cell(run, tile)
        if owner != CQ_MINE or fans < 2:
            continue
        owners = {conquest_cell(run, other)[0] for other in adjacent}
        if not owners - {CQ_MINE}:
            continue
        rank = (CQ_CLUB in owners, fans, tile == CQ_START)
        if rank > best:
            selected, best = tile, rank
    return selected


def conquest_garrison(tile, owner):
    return int(tile == CQ_CAPITAL and owner == CQ_CLUB)


def conquest_board(run):
    words = [CQ_EMPTY << 14] * 387
    count = fans = 0
    for tile in CQ_IDS:
        owner, mobile = conquest_cell(run, tile)
        cap = tile in CQ_STRONGHOLDS
        garrison = conquest_garrison(tile, owner)
        words[tile] = (1 << 21) | (int(cap) << 20) | (owner << 14) | (mobile << 7) | garrison
        if owner == CQ_MINE:
            count += 1
            fans += mobile
    response = ET.Element('response')
    group = mini_node(response, 'quests')
    q = mini_node(group, 'quest', chal_id=run['challenge_id'], turn=run['turn'], phase=run['phase'],
                  start_hex=conquest_start_hex(run),
                  cap_tally=count, fan_tally=fans, skip=0, get_story=0, reinforce=run['pool'],
                  reinforcements=run['pool'], team_lookup=','.join(map(str, range(31))),
                  bits=','.join(map(str, words)))
    q.set('cq___a_a_d_pyxl4', 'Batter Up 1 (local)')  # hidden-reward title slot, hash 9a0c337a
    mini_node(mini_node(group, 'container', empty=1), 'hash', terr_id_them=CQ_CAPITAL,
              terr_id_me=CQ_START, difficulty=0, difficulty_name='Rookie')
    teams = mini_node(group, 'teams')
    for i in range(31):
        mini_node(teams, 'team', team_id=DD_TEAM_ID if i == CQ_MINE else i)
    goals = mini_node(group, 'completed_goals')
    for number, rule in enumerate(CQ_GOALS, 1):
        if rule.id in run['notices']:
            conquest_goal(goals, run, rule, number)
    return mini_xml(response)


def conquest_connected(ids, tag):
    response = ET.Element('response')
    group = mini_node(response, 'connected')
    for tile in sorted(ids):
        mini_node(group, tag, id=tile)
    return mini_xml(response)


def conquest_matchup():
    response = ET.Element('response')
    row = mini_node(response, 'challenge', bar_pos=4, cq________a_6cvl='Local matchup')
    # Local difficulty policy: offer every named level, including Rookie.
    for name in CQ_LEVELS:
        mini_node(row, 'skill_level', name=name, locked=0)
    return mini_xml(response)


def conquest_steal_award(run, tile, skill):
    owner, mobile = conquest_cell(run, tile)
    if run['phase'] != 3 or tile not in CQ_STRONGHOLDS or owner != CQ_CLUB:
        return 0
    # Rookie through Legend offer 1..5M. Leave the permanent garrison and
    # sufficient space in the next Reinforce pool for its normal growth.
    return min(skill + 1, mobile, 127 - conquest_growth(run, CQ_MINE))


def conquest_opponent(run, source, target):
    if source == -1:
        if not conquest_steal_award(run, target, 0):
            raise ConquestRefusal('Choose an enemy stronghold with available fans in the Steal Fans phase.')
    else:
        _, dst = conquest_edge(run, source, target)
        if dst[0] != CQ_CLUB:
            raise ConquestRefusal('Select a territory occupied by Tampa Bay.')


def conquest_steal_teams(run):
    response = ET.Element('response')
    teams = mini_node(mini_node(response, 'steal_fans'), 'teams')
    for tile in CQ_STRONGHOLDS:
        owner, mobile = conquest_cell(run, tile)
        if owner != CQ_CLUB:
            continue
        _, city, name = MLB_TEAMS[owner]
        garrison = conquest_garrison(tile, owner)
        team = mini_node(teams, 'team', team_id=owner, team_name=f'{city} {name}', aux=mobile + garrison)
        # Keep a defending team listed even when its last fan is the garrison.
        for skill, label in enumerate(CQ_LEVELS):
            mini_node(team, 'skill', skill_name=label, value=conquest_steal_award(run, tile, skill), terr_id=tile)
    return mini_xml(response)


def conquest_options():
    # ARENACONQ requires the two text attributes before setup proceeds. The
    # client applies the selected dialog row + 4 to BOTH gameplay settings
    # (0x9dde59, 0xb7391e/32). The row is only sent later, with fight.
    response = ET.Element('response')
    mini_node(response, 'game_options', innings=3, hitting_difficulty='Rookie', pitching_difficulty='Rookie',
              guess_pitch='Off', quick_counts='Off', strike_zone='On', hot_zones='Off', balks='Off',
              umpire_accuracy='Perfect', umpire_close_plays='Perfect', ejections='Off', injuries='Off',
              presentation_mode='Broadcast', extra_innings_runner='On')
    return mini_xml(response)


def conquest_ack(run):
    return f'<response><quest_ack revision="{run["revision"]}"/></response>'


def conquest_edge(run, a, d):
    src, dst = conquest_cell(run, a), conquest_cell(run, d)
    if (run['phase'] != 0 or d not in conquest_neighbors()[a] or src[0] != CQ_MINE or src[1] < 2):
        raise ConquestRefusal('Select an owned territory with at least two fans and an adjacent target.')
    return src, dst


def conquest_growth(run, owner):
    return max(1, sum(cell[0] == owner for cell in run['cells'].values()) // 3)


def conquest_cpu_turn(run):
    """Local Rays AI. Resolve one CPU attack, grow fans, then redeploy to a frontier."""
    rng = random.Random(f"{run['seed']}:{run['turn']}:rays")
    neighbors = conquest_neighbors()
    cells = {tile: conquest_cell(run, tile) for tile in CQ_IDS}
    result = dict(turn=run['turn'], attack=None, reinforcements=[], move=None)

    def enemy_neighbors(tile):
        return sum(cells[n][0] == CQ_MINE for n in neighbors[tile])

    attacks = []
    for a, (owner, fans) in cells.items():
        if owner != CQ_CLUB or fans <= 1:
            continue
        for d in neighbors[a]:
            defender, strength = cells[d]
            # The human stronghold defense prompt has no confirmed wire sequence.
            if defender == CQ_CLUB or d in CQ_STRONGHOLDS:
                continue
            priority = 2 if defender == CQ_MINE and fans - 1 > strength else int(defender == CQ_EMPTY)
            attacks.append((priority, enemy_neighbors(d), fans-1-strength, rng.random(), a, d))
    if attacks:
        *_, a, d = max(attacks)
        src, dst = cells[a], cells[d]
        attackers, defenders = src[1] - 1, dst[1]
        # Each duel removes one fan. The larger remaining force has better odds.
        while attackers and defenders:
            if rng.randrange(attackers + defenders) < attackers:
                defenders -= 1
            else:
                attackers -= 1
        src[1] = 1
        if attackers:
            dst[:] = [CQ_CLUB, attackers]
        else:
            dst[1] = defenders
        result['attack'] = dict(att_hex_id=a, def_hex_id=d, att_hex_result=1,
                                def_hex_result=dst[1], def_owner_res=dst[0])

    owned = [i for i in CQ_IDS if cells[i][0] == CQ_CLUB]
    available = conquest_growth(run, CQ_CLUB)
    for tile in sorted(owned, key=lambda i: (i != CQ_CAPITAL, -enemy_neighbors(i), i)):
        amount = min(127 - cells[tile][1], available)
        if amount:
            cells[tile][1] += amount
            result['reinforcements'].append([tile, amount])
            available -= amount
        if not available:
            break

    moves = []
    for a in owned:
        if cells[a][1] < 2:
            continue
        for d in neighbors[a]:
            if cells[d][0] != CQ_CLUB or cells[d][1] == 127 or d == CQ_CAPITAL:
                continue
            frontier = sum(cells[n][0] != CQ_CLUB for n in neighbors[d])
            if frontier and (a == CQ_CAPITAL or enemy_neighbors(d) > enemy_neighbors(a)):
                moves.append((enemy_neighbors(d), frontier, rng.random(), a, d))
    if moves:
        *_, a, d = max(moves)
        amount = min(cells[a][1] - 1, 127 - cells[d][1])
        cells[a][1] -= amount
        cells[d][1] += amount
        result['move'] = [a, d, amount]
    run['last_cpu'] = result


def conquest_cpu_story(run):
    turn = run['last_cpu']
    if not turn or not turn['attack']:
        return CQ_NO_STORY
    response = ET.Element('response')
    # ApplyStory writes absolute populations. A later reinforcement or friendly
    # move this turn may have changed either endpoint after the CPU battle.
    story = turn['attack'].copy()
    story['att_hex_result'] = conquest_cell(run, story['att_hex_id'])[1]
    story['def_owner_res'], story['def_hex_result'] = conquest_cell(run, story['def_hex_id'])
    mini_node(mini_node(response, 'stories'), 'story', **story)
    return mini_xml(response)


def conquest_mutate(state, run, service, query):
    fields = {'attack_empty': ('hex_id_from', 'hex_id_to'), 'reinforce': ('terr_id', 'amount'),
              'move': ('terr_from_id', 'terr_to_id', 'amount'),
              'sim': ('attacker_id', 'defender_id', 'attacker_score', 'defender_score')}
    values = [conquest_number(query, k) for k in fields.get(service, ())]
    last = [service, *values]
    story = service in ('attack_empty', 'skip_phase', 'sim')
    repeated = run['last'] == last
    # A board refresh arms the next skip. For move/reinforce, the eligibility query
    # for the next selection arms another identical allocation.
    if repeated and (service != 'skip_phase' or run['shown'] != run['revision']):
        if service == 'skip_phase' and run['phase'] == 0:
            return conquest_cpu_story(run)
        return CQ_NO_STORY if story else conquest_ack(run)
    pending = run['pending']
    if service != 'restart' and pending and (pending.get('options') or pending.get('uuid') or pending.get('upload')):
        raise ConquestRefusal('Finish the pending Conquest game or restart the map.')
    if service == 'restart':
        fresh = conquest_fresh(state)
        fresh.update(revision=run['revision'], receipts=run['receipts'], latest=run['latest'])
        state['conquest'] = run = fresh
    elif service == 'skip_phase':
        old = run['phase']
        if old == 8:
            return CQ_NO_STORY
        run['phase'] = {0:3, 3:4, 4:5, 5:0}[old]
        if old == 3:
            run['pool'] = conquest_growth(run, CQ_MINE)
        else:
            run['pool'] = 0
        if old == 5:
            conquest_cpu_turn(run)
            run['turn'] += 1
    elif service == 'attack_empty':
        src, dst = conquest_edge(run, *values)
        if dst[0] != CQ_EMPTY:
            raise ConquestRefusal('This territory is occupied.')
        dst[:] = [CQ_MINE, src[1]-1]
        src[1] = 1
    elif service == 'sim':
        a, d, attackers, defenders = values
        src, dst = conquest_edge(run, a, d)
        if (d in CQ_STRONGHOLDS or dst[0] != CQ_CLUB or
                not pending or (pending['from'], pending['to'], pending['revision']) != (a, d, run['revision']) or
                attackers >= src[1] or defenders > dst[1] or bool(attackers) == bool(defenders)):
            raise ConquestRefusal('The simulation must match the selected ordinary territory and its available fans.')
        # 0xe09126/38 sends survivors; 0xe09365-a7 leaves one fan at the source.
        # The client has already animated the player battle, so do not replay it as a CPU story.
        src[1] = 1
        if defenders:
            dst[1] = defenders
        else:
            dst[:] = [CQ_MINE, attackers]
    elif service == 'reinforce':
        tile, amount = values
        dst = conquest_cell(run, tile)
        if (run['phase'] != 4 or tile not in conquest_component(run, CQ_START)
                or not 1 <= amount <= min(run['pool'], 127-dst[1])):
            raise ConquestRefusal('This territory cannot receive that many reinforcements.')
        dst[1] += amount
        run['pool'] -= amount
    else:  # move
        a, d, amount = values
        src, dst = conquest_cell(run, a), conquest_cell(run, d)
        if (run['phase'] != 5 or a == d or d not in conquest_component(run, a)
                or not 1 <= amount <= min(src[1]-1, 99-dst[1])):
            raise ConquestRefusal('Move fans between connected, owned territories and leave one behind.')
        src[1] -= amount
        dst[1] += amount
    run['pending'] = None
    run['revision'] += 1
    run['last'] = last
    conquest_progress(state, run)
    save_state(state)
    if service == 'skip_phase' and run['phase'] == 0:
        return conquest_cpu_story(run)
    return CQ_NO_STORY if story else conquest_ack(run)


def conquest_settle(state, run):
    battle = run['pending']
    saved = battle.get('upload')
    if not (saved and battle.get('uuid')):
        return
    record = saved['record']
    if record['uuid'] != battle['uuid']:
        raise ConquestRefusal('The result does not match the selected Conquest game.')
    conquest_opponent(run, battle['from'], battle['to'])
    dst = conquest_cell(run, battle['to'])
    if battle['revision'] != run['revision']:
        raise ConquestRefusal('The Conquest board changed before the game finished.')
    stolen = 0
    if record['outcome'] == 'FINISHED':
        won = record['totals']['away']['runs'] > record['totals']['home']['runs']
        if battle['from'] == -1:
            if won:
                stolen = conquest_steal_award(run, battle['to'], battle['skill_level'])
                dst[1] -= stolen
            run['pool'] = conquest_growth(run, CQ_MINE) + stolen
            run['phase'] = 4
        else:
            src = conquest_cell(run, battle['from'])
            if won:
                dst[:] = [CQ_MINE, src[1]-1]
            src[1] = 1
        profile = state.setdefault('profile', {})
        profile['games'] = profile.get('games', 0) + 1
        profile['stubs'] = profile.get('stubs', 0) + STUBS_PER_GAME
        clubs = saved.get('affinity_clubs')
        if clubs is None:  # Battle staged before the affinity patch.
            clubs = affinity_upload_clubs(state, record)
        affinity_record_game(state, saved['digest'], clubs, won)
    conquest_progress(state, run)
    run['receipts'][saved['digest']] = dict(uuid=battle['uuid'], source=battle['from'],
                                            target=battle['to'], turn=run['turn'], skill_level=battle.get('skill_level', 0),
                                            outcome=record['outcome'], stolen=stolen,
                                            home=record['totals']['home']['runs'], away=record['totals']['away']['runs'])
    run['latest'] = saved
    run['pending'] = None
    run['last'] = None
    run['revision'] += 1


def conquest_upload(state, run, data):
    digest = hashlib.sha256(data).hexdigest()
    if digest in run['receipts']:
        return conquest_ack(run)
    battle = run['pending']
    if not battle or not (battle.get('options') or battle.get('uuid')):
        raise ConquestRefusal('No matching Conquest game has started.')
    if battle.get('upload'):
        if battle['upload']['digest'] != digest:
            raise ConquestRefusal('A different result is already waiting for this Conquest fight.')
        return conquest_ack(run)
    record = decode_summary_record(data)
    if (not isinstance(record['uuid'], str) or not 0 < len(record['uuid']) <= 128
            or record['scheduled'] != 3 or record['sub_mode'] != 8 or record['game_type'] != 'ARENA'
            or record['ids'] != {'home':CQ_CLUB, 'away':DD_TEAM_ID}
            or record['players']['away'] != ACCOUNT_ID or record['players']['home'] == ACCOUNT_ID
            or record['outcome'] not in ('FINISHED', 'QUIT', 'QUIT_ON')
            or (record['outcome'] == 'FINISHED' and record['totals']['home']['runs'] == record['totals']['away']['runs'])):
        raise ConquestRefusal('This report does not match a three-inning Conquest game at Tampa Bay.')
    teams = summary_teams(record, summary_local_team(state))
    try:
        innings, scores, errors = decode_line_score(record, teams)
    except ValueError:  # Same aggregate-score fallback as Mini Seasons.
        innings, scores, errors = (3 if record['outcome'] == 'FINISHED' else 0), {}, {}
    record.pop('log')
    battle['upload'] = dict(record=record, teams=teams, innings=innings, scores=scores, errors=errors,
                            digest=digest, created_at=int(time.time()))
    if record['outcome'] == 'FINISHED':
        battle['upload']['affinity_clubs'] = affinity_upload_clubs(state, record)
    conquest_settle(state, run)
    save_state(state)
    return conquest_ack(run)


def conquest_service(state, run, service, query):
    if service in ('view_challenges','update_active'):
        # On first entry v1.00 can submit its prior, uninitialized choice ("0").
        if str(query.get('challenge_id', CQ_CHALLENGE)) not in ('0', str(CQ_CHALLENGE)):
            raise ConquestRefusal('Only the local Batter Up 1 campaign is available.')
        # The map now has the just-presented rows in QUESTS. Its filtered chooser
        # refresh is separate; remove only notices actually included in that view.
        if service == 'view_challenges' and 'challenge_id' in query and run['presented']:
            run['notices'] = [i for i in run['notices'] if i not in run['presented']]
            run['presented'] = []
            save_state(state)
        return conquest_chooser(run) if service == 'view_challenges' else conquest_ack(run)
    if service == 'view':
        pending = run['pending']
        cancelled = pending and pending.get('options') and not (pending.get('uuid') or pending.get('upload'))
        if cancelled:
            run['pending'] = None
        if cancelled or run['shown'] != run['revision'] or run['presented'] != run['notices']:
            run['shown'] = run['revision']
            run['presented'] = list(run['notices'])
            save_state(state)
        return conquest_board(run)
    if service in ('skip_phase','attack_empty','reinforce','move','restart','sim'):
        return conquest_mutate(state, run, service, query)
    if service == 'steal_fans_view':
        return conquest_steal_teams(run)
    if service == 'get_connected_capital':
        ids = conquest_component(run, CQ_START)
        if run['pool'] and run['last'] and run['last'][0] == 'reinforce':
            run['last'] = None
            save_state(state)
        return conquest_connected((i for i in ids if conquest_cell(run, i)[1] < 127), 'con')
    if service == 'get_connected_terr':
        a = conquest_number(query, 'terr_id')
        ids = conquest_move_targets(run, a) if a in conquest_move_sources(run) else []
        if ids and run['last'] and run['last'][0] == 'move':
            run['last'] = None
            save_state(state)
        return conquest_connected(ids, 'con')
    if service == 'move_fans_sources':
        return conquest_connected(conquest_move_sources(run), 'owned')
    if service == 'challenge_team_view':
        a = -1 if str(query.get('terr_id_from')) == '-1' else conquest_number(query, 'terr_id_from')
        d = conquest_number(query, 'terr_id_to')
        conquest_opponent(run, a, d)
        battle = run['pending']
        if not battle or (battle['from'],battle['to']) != (a,d):
            if battle and (battle.get('options') or battle.get('uuid') or battle.get('upload')):
                raise ConquestRefusal('A different Conquest game is already pending.')
            run['pending'] = {'from':a,'to':d,'revision':run['revision']}
            save_state(state)
        return conquest_matchup()
    if service == 'fight':
        a = -1 if str(query.get('terr_from')) == '-1' else conquest_number(query, 'terr_from')
        d = conquest_number(query, 'terr_id')
        key = query.get('uuid')
        skill = conquest_number(query, 'skill_level')
        if (not isinstance(key,str) or not 0 < len(key) <= 128 or
                conquest_number(query,'action') != 1 or skill >= len(CQ_LEVELS)):
            raise ConquestRefusal('Select an available Conquest difficulty and matchup.')
        battle = run['pending']
        if not battle and any((r['uuid'], r['source'], r['target']) == (key, a, d)
                              for r in run['receipts'].values()):
            return conquest_ack(run)
        if (not battle or (battle['from'],battle['to']) != (a,d)
                or battle.get('uuid', key) != key):
            raise ConquestRefusal('Select the Conquest matchup before starting the game.')
        conquest_opponent(run, a, d)
        if battle.get('skill_level', skill) != skill:
            raise ConquestRefusal('This game already has a different difficulty.')
        battle.update(uuid=key, skill_level=skill)
        conquest_settle(state, run)
        save_state(state)
        return conquest_ack(run)
    if service in ('def_cards', 'def_pick'):
        raise ConquestRefusal('Defender assignments are unavailable on this single-stronghold map.')
    raise ConquestRefusal('This action is unavailable in the local Batter Up 1 campaign.')


def conquest_reply(name, params, game_data):
    prefix = 'arena_quests.'
    steal_picker = CQ_STEAL_PICKER.get()
    if steal_picker:
        CQ_STEAL_PICKER.set(False)
    # Scout 82: the chooser's eligibility query follows view_challenges on the same
    # service connection. Each connection runs in a separate asyncio task.
    if name == 'arena_squad.content_season_check':
        if not CQ_CHOOSER.get():
            return None
    elif CQ_CHOOSER.get():
        CQ_CHOOSER.set(False)
    shared = ('arena_squad.content_season_check','arena_team.view','game_option.view',
              'mlb_game.create','arena_game.summary')
    if not name.startswith(prefix) and name not in shared:
        return None
    try:
        q = json.loads(params or '{}') if name.startswith(prefix) else params_of(params)
        if not isinstance(q, dict):
            if not name.startswith(prefix):
                return None
            raise ValueError('params')
        if name == 'arena_team.view' and ('team_id' not in q or 'player_id' in q or mini_scoped(q)):
            return None
        if name == 'game_option.view' and q.get('game_type') != 'ARENACONQ':
            return None
        if name == 'mlb_game.create':
            try:
                if game_data is None:
                    pending = (load_state().get('conquest') or {}).get('pending')
                    if pending and (pending.get('uuid') or pending.get('options')):
                        raise ValueError('missing attachment')
                if mini_game_mode(game_data) != 8:
                    return None
            except (ValueError, struct.error):
                if not (load_state().get('conquest') or {}).get('pending'):
                    return None
                raise ConquestRefusal('The pending Conquest game report is unreadable.') from None
        state = load_state()
        prior_claims = set(state.get('profile', {}).get('conquest_batter_up_1_claims', ()))
        completion_repaired = False
        run = state.get('conquest')
        if run is None:
            if name in (prefix+'view_challenges', prefix+'view', prefix+'update_active'):
                if str(q.get('challenge_id', CQ_CHALLENGE)) not in ('0', str(CQ_CHALLENGE)):
                    raise ConquestRefusal('Only the local Batter Up 1 campaign is available.')
                if not state and os.path.isfile(STATE_FILE):
                    with open(STATE_FILE, encoding='utf-8') as f:
                        if not isinstance(json.load(f),dict):
                            raise ValueError('invalid state')
                run = state['conquest'] = conquest_fresh(state)
                save_state(state)
            elif name.startswith(prefix) or name in ('game_option.view','mlb_game.create'):
                raise ConquestRefusal('Open Batter Up 1 before starting a Conquest game.')
            else:
                return None
        battle = run['pending']
        if name == 'arena_team.view' and ((not battle and not (steal_picker and run['phase'] == 3))
                                          or str(q.get('team_id')) != str(CQ_CLUB)
                                          or str(q.get('squad_type',0)) != '0'):
            return None
        if name == 'arena_game.summary':
            # UUIDs repeat across modes. The normal transport captures the latest attachment
            # before dispatch. Never substitute a Conquest result for another mode's UUID.
            latest = (battle or {}).get('upload') or run['latest']
            if not latest or latest['record']['uuid'] != q.get('uuid'):
                return None
            try:
                with open(os.path.join(WORK,'last-game_data.bin'),'rb') as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
                    prior = run['receipts'].get(digest, {})
                    if digest != latest['digest'] and prior.get('uuid') != q['uuid']:
                        return None
            except FileNotFoundError:
                return None
            scores = {side:{int(i):v for i,v in rows.items()} for side,rows in latest['scores'].items()}
            root = ET.fromstring(summary_response(latest['teams'],latest['record'],latest['innings'],
                                                  scores,latest['errors'],latest['created_at']))
            summary = root.find('arena_game_summary')
            summary.set('game_type','Conquest')
            summary.set('created_at',datetime.datetime.fromtimestamp(latest['created_at'],datetime.timezone.utc).date().isoformat())
            inner = mini_xml(root)
        elif name == 'arena_team.view':
            inner = view_by_team(params)
        elif name == 'arena_squad.content_season_check':
            if mini_scoped(q):
                return None
            inner = ('<response><info current_szn="1" is_squad_eligible="1"/>'
                     '<squads><squad index="0" name="My Squad" is_szn_squad="1"/></squads></response>')
        else:
            conquest_check(run)
            if run['version'] == 1:
                # Preserve the old board, revision, pending fight and upload receipts.
                conquest_init_campaign(run)
                conquest_progress(state, run, entering=True)
                save_state(state)
            elif conquest_reconcile_completion(run, prior_claims):
                save_state(state)
                completion_repaired = True
            if name == 'mlb_game.create':
                inner = conquest_upload(state,run,game_data)
            elif name == 'game_option.view':
                if run['phase'] == 3 and not battle:
                    # The steal picker jumps straight to setup without a native
                    # challenge_team_view. Batter Up 1 has a single opponent.
                    conquest_opponent(run, -1, CQ_CAPITAL)
                    battle = run['pending'] = {'from': -1, 'to': CQ_CAPITAL, 'revision': run['revision']}
                if not battle:
                    raise ConquestRefusal('Select an enemy-held territory before choosing Play Game.')
                conquest_opponent(run, battle['from'], battle['to'])
                if battle['revision'] != run['revision']:
                    raise ConquestRefusal('The selected Conquest matchup has changed.')
                if not battle.get('options'):
                    battle['options'] = True
                    save_state(state)
                inner = conquest_options()
            else:
                inner = conquest_service(state,run,name[len(prefix):],q)
        kind = 'conquest'
        if name == prefix + 'view_challenges':
            CQ_CHOOSER.set(True)
        elif name == prefix + 'steal_fans_view' and run['phase'] == 3:
            CQ_STEAL_PICKER.set(True)
        elif name == prefix + 'view':
            mine = [fans for owner, fans in run['cells'].values() if owner == CQ_MINE]
            kind += f' map turn={run["turn"]} phase={run["phase"]} territories={len(mine)} fans={sum(mine)}'
            done = sum(run['progress'][str(g.id)] == g.target for g in CQ_GOALS)
            kind += f' goals={done}/{len(CQ_GOALS)} complete={int(run["completed"])}'
        elif name == prefix + 'skip_phase' and run['phase'] == 0 and run['last_cpu']:
            cpu = run['last_cpu']
            attack = cpu['attack']
            pair = f'{attack["att_hex_id"]}>{attack["def_hex_id"]}' if attack else 'none'
            move = cpu['move']
            move_text = f'{move[0]}>{move[1]}' if move else 'none'
            growth = sum(n for _, n in cpu['reinforcements'])
            kind += f' cpu turn={cpu["turn"]} attack={pair} growth={growth} move={move_text}'
        elif name == prefix + 'sim':
            tile = conquest_number(q, 'defender_id')
            kind += f' sim hex={tile} owner={conquest_cell(run, tile)[0]}'
        elif name in (prefix + 'fight', 'mlb_game.create'):
            kind += ' awaiting companion' if run['pending'] else ' recorded'
            if name == prefix + 'fight':
                skill = conquest_number(q, 'skill_level')
                kind += f' skill={skill} ({CQ_LEVELS[skill]})'
            elif game_data:
                receipt = run['receipts'].get(hashlib.sha256(game_data).hexdigest())
                if receipt:
                    kind += f' {receipt["outcome"]} away={receipt["away"]} home={receipt["home"]}'
                    if receipt.get('source') == -1:
                        kind += f' stolen={receipt.get("stolen", 0)}M'
        grants = set(state.get('profile', {}).get('conquest_batter_up_1_claims', ())) - prior_claims
        if grants:
            kind += ' granted_goals=' + ','.join(str(i) for i in sorted(grants))
        if completion_repaired:
            kind += ' completion-reconciled'
        return envelope(inner), kind
    except ConquestRefusal as exc:
        return envelope(status=1,message=escape_mini_attr(exc)), 'refused'
    except Exception as exc:
        print(f'Conquest {name} failed ({type(exc).__name__})',file=sys.stderr)
        return envelope(status=1,message='The local Conquest reply could not be prepared.'), 'refused'


def conquest_transport_reply(cache, req, mode, game_data):
    # IDs are scoped to the service connection. Preserve the original ACK even when
    # a retry arrives after the next view (and the next legal mutation is identical).
    name = req['name']
    if not name.startswith('arena_quests.'):
        return reply_body(name, mode, req['params'], game_data)
    try:
        if name.split('.', 1)[1] not in CQ_MUTATIONS:
            return reply_body(name, mode, req['params'], game_data)
        key = (req['id'], name, req['params'])
        if key not in cache:
            result = reply_body(name, mode, req['params'], game_data)
            if result[1] == 'refused':
                return result
            cache[key] = result
        return cache[key]
    except Exception as exc:
        print(f'Conquest transport failed ({type(exc).__name__})', file=sys.stderr)
        return envelope(status=1, message='The local Conquest request could not be completed.'), 'refused'


def reply_body(name, mode, params='', game_data=None):
    """The reply text for a request name: replies\\<name>.xml if it exists, else the empty reply.

    A file that starts with <response is put into the envelope; anything else is sent as it is.
    Place holders: {now} seconds since 1970, {account_id}, {user_name}."""
    live = live_module()
    if live is not None:
        return live.reply_body(name, mode, params, game_data)
    conquest = conquest_reply(name, params, game_data)
    if conquest is not None:
        return conquest
    mini = mini_reply(name, params, game_data)
    if mini is not None:
        return mini
    showdown = showdown_reply(name, params)
    if showdown is not None:
        return showdown
    if name == 'arena_squad.get_loading_screen':
        remember_summary_setup(params)
        if not LAYOUT.install and os.environ.get('SHAD_SCOUT_CAPTURE_ARM'):
            # a match is about to load: let the emulator's marker capture go (launcher -CaptureMarker)
            try:
                open(os.environ['SHAD_SCOUT_CAPTURE_ARM'], 'a').close()
            except OSError:
                pass
        inner = loading_screen(params)
        if inner is not None:
            return envelope(inner), 'made'
        return envelope(status=1, message='The local loading screen requires two complete Play vs. CPU squads.'), 'refused'
    if name in EMPTY_MARKET_REPLIES:
        return envelope(EMPTY_MARKET_REPLIES[name]), 'made'
    exchange = exchange_reply(name, params)
    if exchange is not None:
        return envelope(exchange), 'made'
    for prefix, message in DISABLED:          # parts the owner decided the local server will not offer
        if name.startswith(prefix):
            return envelope(status=1, message=message), 'refused'
    shop = shop_reply(name, params)
    if shop is not None:
        return shop
    if name.startswith(NOT_OFFERED):
        return envelope(status=1, message='This mode is not available on the local server yet.'), 'refused'
    if name == 'collection.advance':     # sent with error mode "ignore": the message shows, the screen stays
        return envelope(status=1, message='Collections are view-only on the local server.'), 'refused'
    if name in DYNAMIC:
        kwargs = {'game_data': game_data} if name == 'mlb_game.create' else {}
        inner = DYNAMIC[name](params, **kwargs)
        if inner:
            return envelope(inner), 'made'
    path = os.path.join(REPLIES_DIR, name + '.xml')
    if os.path.exists(path):
        text = open(path, encoding='utf-8').read().strip()
        text = (text.replace('{now}', str(int(time.time()))).replace('{account_id}', str(ACCOUNT_ID))
                .replace('{user_name}', USER_NAME))
        return (envelope(text) if text.startswith('<response') else text), 'file'
    if name == 'mlb_game.view_post_game':
        return POST_GAME_FALLBACK, 'required post_game fallback'
    if mode == 'none':
        return None, 'none'
    return EMPTY_XML, 'empty'


# ---------------------------------------------------------------- server
class Log:
    def __init__(self, path=None):
        self.f = open(path, 'a', encoding='utf-8') if path else None
        self.t0 = time.time()

    def __call__(self, text):
        line = f'{time.time() - self.t0:9.3f} {text}'
        print(line, flush=True)
        if self.f:
            self.f.write(line + '\n')
            self.f.flush()


def hexdump(data, limit=256):
    return data[:limit].hex(' ') + (f' ... ({len(data)} bytes)' if len(data) > limit else '')


async def handle(reader, writer, log, opts, counter=[0]):
    counter[0] += 1
    cid = counter[0]
    peer = writer.get_extra_info('peername')
    sslobj = writer.get_extra_info('ssl_object')
    log(f'[c{cid}] connected from {peer}, {sslobj.version() if sslobj else "no TLS"}, '
        f'{sslobj.cipher()[0] if sslobj else ""}')
    seen = {}
    conquest_receipts = {}

    async def send(data, what):
        writer.write(chunk(data))
        await writer.drain()
        log(f'[c{cid}] -> {what} ({len(data)} bytes)')

    plain = bytearray()
    chunks = [0]

    async def read_frame():
        while True:
            if len(plain) >= 8:
                length, type_id = struct.unpack_from('<II', plain)
                if length < 8 or length > 16 * 1024 * 1024:
                    raise ValueError(f'bad frame length {length}: {bytes(plain[:32]).hex(" ")}')
                if len(plain) >= length:
                    body = bytes(plain[8:length])
                    del plain[:length]
                    return type_id, body
            flags, data = await read_chunk(reader)
            chunks[0] += 1
            if chunks[0] <= 4 or flags != 2:
                log(f'[c{cid}] chunk {chunks[0]}: flags {flags:#04x}, {len(data)} bytes'
                    + (' (was compressed)' if flags & 4 else ''))
            plain.extend(data)

    async def pinger():
        try:
            while opts['ping'] > 0:
                await asyncio.sleep(opts['ping'])
                await send(frame('olServerPing', Writer().u8(0)), 'olServerPing')
        except (ConnectionError, asyncio.CancelledError):
            pass

    ping_task = asyncio.create_task(pinger())
    try:
        while True:
            type_id, body = await read_frame()
            name = NAMES.get(type_id, f'unknown_{type_id:08x}')
            seen[name] = seen.get(name, 0) + 1
            if name == 'olServiceRequest':
                try:
                    req = parse_service_request(body)
                except Exception as e:  # keep the bytes if our reading of the layout is wrong
                    log(f'[c{cid}] <- olServiceRequest could not be read ({e}): {hexdump(body)}')
                    continue
                log(f'[c{cid}] <- request #{req["id"]} {req["name"]} {req["params"]}'
                    + (f' attachments {req["attachments"]}' if req['attachments'] else ''))
                game_data = None
                if 'game_data' in req['blobs']:
                    try:
                        os.makedirs(WORK, exist_ok=True)
                        with open(os.path.join(WORK, 'last-game_data.bin'), 'wb') as f:
                            f.write(req['blobs']['game_data'])
                        game_data = req['blobs']['game_data']
                    except OSError as e:
                        log(f'[c{cid}] game_data capture failed: {e!r}')
                try:
                    text, kind = conquest_transport_reply(conquest_receipts, req, opts['reply'], game_data)
                except Exception as e:      # a reply that cannot be made must not end the connection
                    log(f'[c{cid}] reply for {req["name"]} failed: {e!r}')
                    if req['name'] == 'mlb_game.view_post_game':
                        text, kind = POST_GAME_FALLBACK, 'required post_game fallback'
                    else:
                        text, kind = EMPTY_XML, 'empty after an error'
                if _reload['note']:
                    log(_reload['note'])
                    _reload['note'] = None
                if text is not None:
                    await send(build_service_response(req['id'], req['name'], text),
                               f'reply #{req["id"]} {req["name"]} ({kind})')
            elif name.startswith('olAuthLogin') and not name.startswith('olAuthLoginResult'):
                log(f'[c{cid}] <- {name}: {hexdump(body)}')
                await send(build_auth_result(), 'olAuthLoginResultV3 (accepted)')
            elif name in ('olAccountLoginRequest', 'olAccountLoginWithFlagsRequest'):
                log(f'[c{cid}] <- {name}: {hexdump(body, 96)}')
                await send(build_account_response(), 'olAccountLoginResponse (accepted)')
            elif name == 'olQueryService':
                log(f'[c{cid}] <- olQueryService: {hexdump(body, 48)}')
                await send(build_service_answer(), f'olServiceAnswer (127.0.0.1:{PORT})')
            elif name == 'olClientPong':
                if seen[name] <= 3:
                    log(f'[c{cid}] <- olClientPong {body.hex()}')
            elif name == 'olUniversalProfileXPGrantMultipleEvent':
                try:
                    live = live_module()
                    fn = live.xp_grant_answer if live is not None else xp_grant_answer
                    answer, note = fn(body)
                except Exception as e:
                    answer, note = None, f'xp grant answer failed: {e!r}'
                if _reload['note']:
                    log(_reload['note'])
                    _reload['note'] = None
                log(f'[c{cid}] <- {name}: {note}')
                if answer:
                    await send(answer, 'olUniversalProfileXPGrantEventResponse')
            else:
                log(f'[c{cid}] <- {name} (no answer known): {hexdump(body)}')
    except asyncio.IncompleteReadError as e:
        log(f'[c{cid}] closed by the game' + (f'; {len(e.partial)} bytes of an unfinished chunk: {e.partial[:48].hex(" ")}' if e.partial else '')
            + (f'; {len(plain)} bytes of an unfinished frame: {bytes(plain[:48]).hex(" ")}' if plain else ''))
    except ValueError as e:
        log(f'[c{cid}] could not read the stream ({e}); closing')
    except ConnectionError as e:
        log(f'[c{cid}] connection error: {e}')
    finally:
        ping_task.cancel()
        log(f'[c{cid}] summary: ' + ', '.join(f'{k} x{v}' for k, v in sorted(seen.items())))
        try:
            writer.close()
        except Exception:
            pass


def server_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.path.join(WORK, 'server.pem'), os.path.join(WORK, 'server.key'))
    return ctx  # the game's client certificate is neither asked for nor checked


async def serve(host, port, log, opts):
    return await asyncio.start_server(lambda r, w: handle(r, w, log, opts), host, port, ssl=server_context())


# ---------------------------------------------------------------- the account service's web API
# The game asks the console's web API layer for the block list, presence and the like. With
# SHAD_SCOUT_WEBAPI_URL the emulator sends those requests here as plain HTTP.
WEB_PORT = 10080


def web_answer(method, path):
    base, _, query = path.partition('?')
    if base == '/v1/users/me/blocks':
        return 200, {'blocks': [], 'nextOffset': None, 'totalItemCount': 0}
    if base == '/v1/users/basicPresences':
        ids = [x for part in query.split('&') if part.startswith('accountIds=') for x in part[11:].split(',') if x]
        return 200, {'basicPresences': [{'accountId': i, 'onlineStatus': 'online', 'inContext': False} for i in ids]}
    if base.startswith('/v2/profanity/filter'):
        return 200, {'message': ''}
    if base.startswith('/__host/') and method in ('GET', 'HEAD'):
        art = (live_module() or sys.modules[__name__]).card_art()['files'].get(base)
        if art is not None:
            return 200, art                 # a special card's art ZIP (card_art())
    if base.startswith('/__host/'):
        # A file the game fetches from an outside host (SHAD_SCOUT_HTTP_SINK). The maintenance
        # notice is read for these three keys (parser at image offset 0xbb99f0).
        if base.endswith('/maint/message.json'):
            return 200, {'currently_under_maintenance': False, 'start_time_epoch': 0, 'message': ''}
        if '/scout-content/' in base:
            file = os.path.join(CONTENT_DIR, ROSTER_SOURCE, os.path.basename(base))
            if os.path.basename(base)[:-4] in CONTENT and os.path.exists(file):
                return 200, open(file, 'rb').read()
        return 404, {}
    return 200, {}


async def handle_web(reader, writer, log):
    try:
        while True:
            head = await reader.readuntil(b'\r\n\r\n')
            lines = head.decode('latin1').split('\r\n')
            method, path, _ = (lines[0].split(' ') + ['', ''])[:3]
            headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(':') for l in lines[1:] if l)}
            body = await reader.readexactly(int(headers.get('content-length', '0') or 0))
            status, obj = web_answer(method, path)
            if isinstance(obj, bytes):
                log(f'[web] {method} {path} -> {status} file of {len(obj)} bytes' + (f' ({headers["range"]})' if 'range' in headers else ''))
                data, kind = obj, 'application/zip'
            else:
                log(f'[web] {method} {path}' + (f' body {body[:300]!r}' if body else '')
                    + (f' -> {status} {json.dumps(obj)[:200]}' if obj != {} else f' -> {status} {{}} (no answer known)'))
                data, kind = json.dumps(obj).encode(), 'application/json; charset=utf-8'
            head = (f'HTTP/1.1 {status} {"OK" if status == 200 else "Not Found"}\r\nContent-Type: {kind}\r\n'
                    f'Content-Length: {len(data)}\r\nConnection: keep-alive\r\n\r\n').encode()
            writer.write(head if method == 'HEAD' else head + data)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------- certificates
RSA_BITS = 2048
RSA_EXPONENT = 65537
RSA_PRIME_ROUNDS = 64


@lru_cache(maxsize=1)
def small_primes():
    primes = []
    for candidate in range(2, 2000):
        if all(candidate % p for p in primes if p * p <= candidate):
            primes.append(candidate)
    return tuple(primes)


def probable_prime(candidate):
    if any(candidate % p == 0 for p in small_primes()):
        return False
    odd = candidate - 1
    shifts = (odd & -odd).bit_length() - 1
    odd >>= shifts
    for _ in range(RSA_PRIME_ROUNDS):
        witness = secrets.randbelow(candidate - 3) + 2
        value = pow(witness, odd, candidate)
        if value in (1, candidate - 1):
            continue
        for _ in range(shifts - 1):
            value = value * value % candidate
            if value == candidate - 1:
                break
        else:
            return False
    return True


def rsa_prime():
    bits = RSA_BITS // 2
    while True:
        # The high two bits guarantee a full-width modulus.
        candidate = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if math.gcd(candidate - 1, RSA_EXPONENT) == 1 and probable_prime(candidate):
            return candidate


def der(tag, payload):
    size = len(payload)
    if size < 128:
        length = bytes([size])
    else:
        raw = size.to_bytes((size.bit_length() + 7) // 8, 'big')
        length = bytes([128 | len(raw)]) + raw
    return bytes([tag]) + length + payload


def der_seq(*fields):
    return der(0x30, b''.join(fields))


def der_int(number):
    raw = number.to_bytes(max(1, (number.bit_length() + 7) // 8), 'big')
    return der(2, b'\0' + raw if raw[0] & 128 else raw)


def der_oid(*arcs):
    body = bytearray()
    for arc in (40 * arcs[0] + arcs[1], *arcs[2:]):
        groups = [arc & 127]
        arc >>= 7
        while arc:
            groups.append((arc & 127) | 128)
            arc >>= 7
        body.extend(reversed(groups))
    return der(6, bytes(body))


RSA_ALGORITHM = der_seq(der_oid(1, 2, 840, 113549, 1, 1, 1), der(5, b''))
CERT_SIGNATURE = der_seq(der_oid(1, 2, 840, 113549, 1, 1, 11), der(5, b''))
SHA256_ALGORITHM = der_seq(der_oid(2, 16, 840, 1, 101, 3, 4, 2, 1), der(5, b''))


class RsaKey(NamedTuple):
    modulus: int
    private: int
    p: int
    q: int

    def public_der(self):
        return der_seq(der_int(self.modulus), der_int(RSA_EXPONENT))

    def private_der(self):
        return der_seq(*(der_int(n) for n in (0, self.modulus, RSA_EXPONENT, self.private, self.p, self.q,
                                             self.private % (self.p - 1), self.private % (self.q - 1),
                                             pow(self.q, -1, self.p))))

    def sign(self, data):
        info = der_seq(SHA256_ALGORITHM, der(4, hashlib.sha256(data).digest()))
        width = (self.modulus.bit_length() + 7) // 8
        block = b'\x00\x01' + b'\xff' * (width - len(info) - 3) + b'\0' + info
        return pow(int.from_bytes(block, 'big'), self.private, self.modulus).to_bytes(width, 'big')


def rsa_key():
    p, q = rsa_prime(), rsa_prime()
    while p == q:
        q = rsa_prime()
    private = pow(RSA_EXPONENT, -1, math.lcm(p - 1, q - 1))
    return RsaKey(p * q, private, p, q)


def certificate_name(common_name):
    return der_seq(der(0x31, der_seq(der_oid(2, 5, 4, 3), der(0x0c, common_name.encode('utf-8')))))


def certificate_time(value):
    if 1950 <= value.year < 2050:
        return der(0x17, value.strftime('%y%m%d%H%M%SZ').encode('ascii'))
    return der(0x18, value.strftime('%Y%m%d%H%M%SZ').encode('ascii'))


def certificate_expiry(now, years):
    try:
        return now.replace(year=now.year + years)
    except ValueError:   # February 29 in a non-leap expiry year
        return now.replace(year=now.year + years, day=28)


def certificate_extension(oid, value, *, critical=False):
    flag = der(1, b'\xff') if critical else b''
    return der_seq(oid, flag, der(4, value))


def certificate(subject, issuer, key, signing_key, start, end, *, ca):
    ski = hashlib.sha1(key.public_der()).digest()
    aki = hashlib.sha1(signing_key.public_der()).digest()
    ext = [
        certificate_extension(der_oid(2, 5, 29, 19), der_seq(der(1, b'\xff') if ca else b''), critical=ca),
        certificate_extension(der_oid(2, 5, 29, 15), der(3, b'\x01\x06' if ca else b'\x05\xa0'), critical=ca),
        certificate_extension(der_oid(2, 5, 29, 14), der(4, ski)),
        certificate_extension(der_oid(2, 5, 29, 35), der_seq(der(0x80, aki))),
    ]
    if not ca:
        hosts = [der(0x82, h.encode('ascii')) for h in HOSTS + ['localhost']]
        hosts.append(der(0x87, bytes([127, 0, 0, 1])))
        ext.extend((
            certificate_extension(der_oid(2, 5, 29, 37), der_seq(der_oid(1, 3, 6, 1, 5, 5, 7, 3, 1))),
            certificate_extension(der_oid(2, 5, 29, 17), der_seq(*hosts)),
        ))
    serial = secrets.randbits(159) | (1 << 158)
    public = der_seq(RSA_ALGORITHM, der(3, b'\0' + key.public_der()))
    tbs = der_seq(der(0xa0, der_int(2)), der_int(serial), CERT_SIGNATURE, issuer,
                  der_seq(certificate_time(start), certificate_time(end)), subject,
                  public, der(0xa3, der_seq(*ext)))
    return der_seq(tbs, CERT_SIGNATURE, der(3, b'\0' + signing_key.sign(tbs)))


def pem(label, payload):
    text = base64.b64encode(payload)
    lines = b'\n'.join(text[i:i + 64] for i in range(0, len(text), 64))
    return b'-----BEGIN ' + label + b'-----\n' + lines + b'\n-----END ' + label + b'-----\n'


def gen_certs():
    ca_key, server_key = rsa_key(), rsa_key()
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    start = now - datetime.timedelta(days=1)
    issuer = certificate_name('Scout local online authority')
    ca = certificate(issuer, issuer, ca_key, ca_key, start, certificate_expiry(now, 10), ca=True)
    server = certificate(certificate_name(HOSTS[1]), issuer, server_key, ca_key,
                         start, certificate_expiry(now, 2), ca=False)
    files = {'ca.pem': pem(b'CERTIFICATE', ca), 'ca.key': pem(b'RSA PRIVATE KEY', ca_key.private_der()),
             'server.pem': pem(b'CERTIFICATE', server), 'server.key': pem(b'RSA PRIVATE KEY', server_key.private_der())}
    os.makedirs(WORK, exist_ok=True)
    staged = {}
    try:
        for name, payload in files.items():
            fd, path = tempfile.mkstemp(prefix='.local-tls-', dir=WORK)
            staged[name] = path
            with os.fdopen(fd, 'wb') as file:
                file.write(payload)
        for name, path in staged.items():
            os.replace(path, os.path.join(WORK, name))
    finally:
        for path in staged.values():
            if os.path.exists(path):
                os.unlink(path)
    print('Generated local CA and server certificates in ' + WORK)


# ---------------------------------------------------------------- self-test
async def selftest():
    if not os.path.exists(os.path.join(WORK, 'server.pem')):
        gen_certs()
    lines = []
    log = lambda t: lines.append(t)
    srv = await serve('127.0.0.1', 0, log, {'ping': 0, 'reply': 'empty'})
    port = srv.sockets[0].getsockname()[1]
    cctx = ssl.create_default_context(cafile=os.path.join(WORK, 'ca.pem'))
    r, w = await asyncio.open_connection('127.0.0.1', port, ssl=cctx, server_hostname=HOSTS[1])

    async def recv():
        flags, data = await read_chunk(r)
        length, type_id = struct.unpack_from('<II', data)
        assert flags == 2 and length == len(data)
        return NAMES[type_id], data[8:]

    send_plain = w.write
    w.write = lambda data: send_plain(chunk(data))
    # LZ4: 3 literals, a 12-byte match at distance 3, then 5 closing literals
    packed = bytes([0x38]) + b'abc' + b'\x03\x00' + bytes([0x50]) + b'xyz12'
    assert lz4_block_decode(packed) == b'abc' + b'abcabcabcabc' + b'xyz12'
    assert chunk(b'x' * (CHUNK_MAX + 1))[:6] == b'\xa5\x02' + struct.pack('<I', CHUNK_MAX)
    # the first message split over two chunks
    first = frame('olAuthLoginPS4V2', Writer().string('ScoutLocalAuthCode0001').u32(256).u8(0).u16(2023))
    send_plain(chunk(first[:10]) + chunk(first[10:]))
    name, body = await recv()
    rd = Reader(body)
    assert name == 'olAuthLoginResultV3' and rd.u8() == 1
    exp = rd.string(); rd.take(16); rd.blob(); rd.blob(); rd.u32(); rd.string(); rd.u64()
    assert rd.left() == 0 and len(exp) == 23 and exp[19] == '.' and exp.endswith('Z'), exp
    w.write(frame('olAccountLoginRequest', auth_ticket(Writer()).u8(0)))
    name, body = await recv()
    rd = Reader(body)
    assert name == 'olAccountLoginResponse' and rd.u8() == 0 and rd.u64() == ACCOUNT_ID
    rd.u64(); assert rd.string() == USER_NAME
    rd.string(); rd.take(16); rd.blob(); rd.blob()
    assert rd.left() == 0
    w.write(frame('olQueryService', session_ticket(Writer())))
    name, body = await recv()
    rd = Reader(body)
    assert name == 'olServiceAnswer' and rd.u8() == 1 and rd.take(16)[:6] == bytes([127, 0, 0, 1, 255, 255])
    assert rd.take(2) == struct.pack('>H', PORT) and len(rd.take(16)) == 16 and rd.u64() == 1 and rd.left() == 0
    long_params = json.dumps({'points': 12, 'note': 'x' * 300})
    w.write(frame('olServiceRequest', Writer().u32(7).string('challenge.post_stats').string(long_params).u32(0)))
    name, body = await recv()
    rd = Reader(body)
    assert name == 'olServiceResponse' and rd.u32() == 7 and rd.string() == 'challenge.post_stats'
    assert rd.string() == EMPTY_XML and rd.left() == 0
    w.write(frame('olUniversalProfileRegisterClient', b'\x01\x02\x03'))
    await w.drain()
    await asyncio.sleep(0.2)
    w.close()
    srv.close()
    await asyncio.sleep(0.1)
    text = '\n'.join(lines)
    assert long_params in text and 'olUniversalProfileRegisterClient (no answer known): 01 02 03' in text, text
    assert Reader(Writer().string('a' * 200).b).string() == 'a' * 200
    web = await asyncio.start_server(lambda r, w: handle_web(r, w, log), '127.0.0.1', 0)
    wr, ww = await asyncio.open_connection('127.0.0.1', web.sockets[0].getsockname()[1])
    for path, want in (('/v1/users/me/blocks?offset=0&limit=2000', b'"blocks": []'),
                       ('/__host/s3.amazonaws.com/mlb23-production-2023/maint/message.json', b'"currently_under_maintenance": false'),
                       ('/v1/users/basicPresences?accountIds=81985529216486895', b'"accountId": "81985529216486895"')):
        ww.write(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n'.encode())
        head = await wr.readuntil(b'\r\n\r\n')
        n = int([l for l in head.split(b'\r\n') if l.lower().startswith(b'content-length')][0].split(b':')[1])
        assert head.startswith(b'HTTP/1.1 200') and want in await wr.readexactly(n), path
    ww.close()
    web.close()
    print('selftest passed: TLS with our authority, sign-in, account login, a service request and an unknown message')
    print('\n'.join(lines))


def log_missing_derived(log):
    limits = {
        'roster-records.json': 'using fallback player metadata, stances and appearances',
        'appearance-fields.json': 'real player appearance fields are unavailable',
        'pitch-table.json': 'using common pitch codes and fallback velocity',
    }
    for name, limitation in limits.items():
        if not os.path.isfile(os.path.join(WORK, name)):
            log(f'install: {name} missing. {limitation.capitalize()}.')


def main():
    global CARD_SOURCE, ROSTER_SOURCE, STATE_FILE
    args = _command_args if __name__ == '__main__' else sys.argv[1:]
    cmd = args[0] if args else 'run'
    options = args[1:]
    roster_option = None
    if LAYOUT.install and '--collection=mine' in options:
        sys.exit('--collection=mine is development-only')
    for option in options:
        if option.startswith('--cards='):
            source = option.split('=', 1)[1]
            if source not in ('disc', 'site'):
                sys.exit('--cards must be disc or site')
            if LAYOUT.install and source == 'site':
                sys.exit('--cards=site is development-only')
            CARD_SOURCE = source
    if LAYOUT.install or cmd == 'build-roster':
        for arg in options:
            if arg.startswith('--rosters='):
                roster_option = arg.split('=', 1)[1]
        if roster_option is not None:
            if roster_option == 'patch' and LAYOUT.update is None:
                sys.exit('install layout: --rosters=patch requires an update; game.json has no update')
            if roster_option not in ROSTER_DIRS:
                sys.exit(f'--rosters must be one of {", ".join(ROSTER_DIRS)}')
            ROSTER_SOURCE = roster_option
    if cmd == 'build-roster':
        if not LAYOUT.install:
            CARD_SOURCE = 'disc'
        try:
            prepare_disc_roster(print)
        except (OSError, ValueError, KeyError, struct.error) as error:
            sys.exit(f'cannot build disc roster: {error}')
    elif cmd == 'gen-certs':
        gen_certs()
    elif cmd == 'build-content':
        sources = (roster_option,) if roster_option is not None else ROSTER_DIRS
        for source in sources:
            ROSTER_SOURCE = source
            build_content()
    elif cmd == 'selftest':
        if LAYOUT.install:
            log_missing_derived(print)
        asyncio.run(selftest())
    elif cmd == 'run':
        opts = {'ping': 20.0, 'reply': 'empty'}
        option_iter = iter(options)
        for a in option_iter:
            if a.startswith('--ping='):
                opts['ping'] = float(a.split('=')[1])
            elif a.startswith('--reply='):
                opts['reply'] = a.split('=')[1]
            elif a.startswith('--rosters='):
                globals()['ROSTER_SOURCE'] = a.split('=')[1]
                if ROSTER_SOURCE not in ROSTER_DIRS:
                    sys.exit(f'--rosters must be one of {", ".join(ROSTER_DIRS)}')
            elif a.startswith('--collection='):
                globals()['COLLECTION'] = a.split('=')[1]
                if COLLECTION not in ('all', 'mine'):
                    sys.exit('--collection must be all or mine')
            elif a.startswith('--log='):
                opts['log'] = a.split('=', 1)[1].strip('"')
            elif LAYOUT.install and a == '--log':
                opts['log'] = next(option_iter, '')
                if not opts['log'] or opts['log'].startswith('--'):
                    sys.exit('install layout: --log requires a path')
        if not os.path.exists(os.path.join(WORK, 'server.pem')):
            gen_certs()
        if 'log' not in opts:
            os.makedirs(LOGDIR, exist_ok=True)
            pattern = 'server-%Y%m%d-%H%M%S.log' if LAYOUT.install else 'olserver-%Y%m%d-%H%M%S.log'
            # An install names its logs in UTC, like the launcher's own logs beside them.
            opts['log'] = os.path.join(LOGDIR, time.strftime(pattern, time.gmtime() if LAYOUT.install else time.localtime()))
        elif LAYOUT.install and not os.path.isabs(opts['log']):
            opts['log'] = os.path.join(LOGDIR, opts['log'])
            os.makedirs(os.path.dirname(os.path.abspath(opts['log'])), exist_ok=True)
        log = Log(opts['log'])
        if LAYOUT.install or CARD_SOURCE == 'disc':
            if not LAYOUT.install:
                STATE_FILE = derived_roster_path('state.json')
            try:
                prepare_disc_roster(log)
                state = load_state()
                if CARD_SOURCE == 'disc' and 'disc_starter' not in state:
                    state['disc_starter'], squad = choose_disc_starter(all_cards())
                    if not state.get('squad'):
                        state['squad'] = squad
                    grant_starting_stubs(state)
                    save_state(state)
                elif not os.path.exists(STATE_FILE):
                    save_state(state)
            except (OSError, ValueError, KeyError, struct.error) as error:
                sys.exit(f'cannot prepare disc roster: {error}')
        build_content(log)
        try:
            log(f'cards: collection "{COLLECTION}", {len(cards())} cards with a roster player')
        except Exception as e:  # the snapshot or the roster may be absent; the server still runs
            log(f'cards: none ({type(e).__name__}: {e})')

        async def go():
            srv = await serve('127.0.0.1', PORT, log, opts)
            web = await asyncio.start_server(lambda r, w: handle_web(r, w, log), '127.0.0.1', WEB_PORT)
            log(f'listening on 127.0.0.1:{PORT} for {", ".join(HOSTS)}; ping every {opts["ping"]} s, '
                f'replies: {opts["reply"]}; web API on http://127.0.0.1:{WEB_PORT}')
            async with srv, web:
                await asyncio.gather(srv.serve_forever(), web.serve_forever())
        try:
            asyncio.run(go())
        except KeyboardInterrupt:
            pass
    else:
        sys.exit(__doc__ or 'usage: gen-certs | selftest | run')


if __name__ == '__main__':
    main()
