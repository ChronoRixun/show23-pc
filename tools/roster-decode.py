"""Decode MLB 23 LE59 disc rosters. No emulator, website, or reference dump required.

python tools/roster-decode.py [game_folder] [records.json] [--cards cards.json]
                              [--compare captured.json]
See reports/release/roster-decoding.md for runtime defaults and evidence.
"""
import argparse
import base64
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import struct
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = ROOT / "probes/olserver/roster-records.json"
ATTRIBUTES = ("overall contact_left contact_right power_left power_right batting_clutch "
              "bunting_ability drag_bunting_ability plate_vision plate_discipline potential "
              "durability speed stealing baserunning_aggression arm_strength arm_accuracy "
              "fielding_ability reaction_time blocking").split()
POSITIONS = "P C 1B 2B 3B SS LF CF RF DH".split()
HANDS = ["R", "L", "S"]
KINDS = ["SP", "RP", "CP"]
MODES = ["vs_R_DH", "vs_R", "vs_L_DH", "vs_L", "variant_5"]
STANCE = ("hand_offset_x hand_offset_y hand_offset_z hand_rot_z hand_waggle_rot_z "
          "back_elbow_offset_y front_elbow_offset_y hips_offset_x hips_offset_y "
          "hips_offset_z forward_foot_offset_x forward_foot_offset_z").split()
LOOK_SCALES = ("SKIN_U SKIN_V WEIGHT_ADJUST HEAD_SCALE NECK_SCALE SHOULDERS_SCALE "
               "SLEEVES_SCALE SPINE_SCALE COLLARBONE_SCALE HIPS_SCALE THIGHS_SCALE "
               "GLUTES_SCALE ARMS_SCALE BROW_COLOR_U BROW_COLOR_V FACIAL_H_COLOR_U "
               "FACIAL_H_COLOR_V HAIR_COLOR_U HAIR_COLOR_V COLOR_CONTRAST HAIR_ROUGHNESS "
               "HEAD_SHAPE HAIR_STYLE").split()
# Tuples are label, disc offset, memory offset, length in bytes.
LOOK_MAP = [("PACKED_U32", 0, 0, 24), ("APPEAR_VALUES", 24, 93, 175),
            ("CURR_SKIN_EFF", 199, 268, 1), ("PIMPLES", 200, 269, 1),
            ("ACCESSORY_COLORS", 201, 24, 38), ("FACE_HAIR_STYLE", 262, 62, 4)]
LOOK_MAP.extend((name, 239+i, 66+i, 1) for i, name in enumerate(LOOK_SCALES))
LOOK_MAP.extend((name, 266+i, 89+i, 1) for i, name in enumerate(
    ["BATTING_GLOVE", "GLASSES", "GLOVE", "SHOES"]))
# Named portions of the packed words. Each tuple is memory byte, bit, width.
BITS = {
    "team": (144, 0, 9), "position": (144, 15, 6), "pitcher_kind": (144, 21, 3),
    "throw_hand": (144, 30, 2), "weight_minus_100": (148, 0, 8),
    "height_inches": (148, 9, 7), "age": (148, 16, 7), "bat_hand": (148, 23, 2),
    "ethnicity": (148, 25, 3), "mlb_service_years": (148, 42, 6),
    "has_actionshot": (156, 7, 1), "anim_follow_through": (156, 13, 4),
    "years_pro": (156, 26, 6), "player_icon": (156, 37, 5),
    "jersey_number": (164, 0, 7), "original_jersey_number": (164, 7, 7),
    "birthplace": (172, 14, 7), "i_am_legend": (76, 18, 1),
    "runtime_98_low": (152, 0, 6), "runtime_9c_low": (156, 0, 6),
    "runtime_a0_low": (160, 0, 5), "contract_grade_bits": (72, 14, 2),
    "contract_term": (72, 0, 4),
}
SECONDARY = POSITIONS[1:9] + KINDS
for idx, name in enumerate(SECONDARY):
    BITS["secondary_" + name] = (164, 21+idx if idx < 8 else 56+idx-8, 1)
for idx, name in enumerate("walkup take_pitch_strike practice_swing stride take_pitch miss".split()):
    BITS["anim_" + name] = (164, 32+4*idx, 4)


def uint(data, offset, width):
    return int.from_bytes(data[offset:offset+width], "little")


def bits(data, spec):
    offset, bit, width = spec
    return (uint(data, offset, (bit+width+7)//8) >> bit) & ((1 << width)-1)


def string(raw):
    return raw.split(b"\0", 1)[0].decode("latin-1")


def table(data, offset, name, count=None):
    if data[offset:offset+16] != name.encode().ljust(16, b"\0"):
        raise ValueError(f"expected {name} at file offset {offset:#x}")
    actual, = struct.unpack_from("<I", data, offset+16)
    if count is not None and count != actual:
        raise ValueError(f"{name} has {actual} rows, expected {count}")
    return offset+20, actual


def section(data, name):
    if data[:4] != b"LE59" or data[8:14] != b"[DESC]":
        raise ValueError(f"expected an LE59 descriptor for {name}")
    marker = 8 + uint(data, 4, 4)
    if data[marker:marker+6] != b"[DATA]":
        raise ValueError(f"invalid descriptor length for {name}")
    return table(data, marker+6, name)


def potential(age, overall, raw):
    """Deterministic branches of 0x1a7c850. Use lower bound for age 29/30.
    Under 29, zero calls the game's random picker. Retain its disc sentinel.
    """
    if age >= 31:
        divisor = 1 - 0.3999999761581421 * ((min(age, 42)-30)/12)**2
        value = math.floor(overall/divisor + 0.5)
    elif age >= 29:
        value = overall
    else:
        value = raw
    if 0 < value < 40:
        value = int(45 + 15*min(value, 35)/35)
    return min(99, value)


def single(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def first_year_salary(total, term, schedule):
    """First annual payment from 0x80b880. Each scalar operation rounds to float32."""
    average = single(single(total)/term)
    if schedule not in (1, 2):
        return int(average)
    slope = 15 if term < 2 else single(15 + single(-10*single((min(term, 15)-2)/13)))
    payment_delta = single(average*single(single((0.5-0.5*term)*slope)/100))
    if schedule == 2:
        payment_delta = -payment_delta
    return int(single(average + payment_delta))


def initialized_fields(mem, overall, raw_potential):
    age = bits(mem, BITS["age"])
    # 0x1a7cea0 sets these together when the first packed value is missing.
    # Age-clamping covers all matched rows. Use the lower bound for the draw
    # at 0x1a7d3c9 (the range collapses when the clamped age is 32).
    if raw_potential and not mem[152] & 63 and (overall >= raw_potential or age >= 33):
        mem[152] = (mem[152] & 0xc0) | min(32, max(20, age))
        mem[160] = (mem[160] & 0xe0) | 2
    # 0x80f310 subtracts a random entry age, 18..23 for ages >=23.
    mem[156] = (mem[156] & 0xc0) | (max(0, age-23) & 63)
    grade = 3
    if overall > 84:
        grade = 0
    elif overall > 77:
        grade = 1
    elif overall >= 69 and bits(mem, BITS["position"]):
        grade = 2
    mem[73] = (mem[73] & 63) | (grade << 6)


def records(data):
    start, count = section(data, "PLR_PROFS")
    app, _ = table(data, start+count*299, "D_PLAYER_APP", count)
    att, _ = table(data, app+count*270, "PLR_ATT", count)
    pit, _ = table(data, att+count*37, "PLR_PITCH_ATT", count)
    table(data, pit+count*31, "D_COACH_PROF")
    rows = []
    for i in range(count):
        offset = start + 299*i
        raw = data[offset:offset+299]
        for off, label in [(86, "D_PLYR_CONTRACT"), (117, "D_PLYR_ANIMS"),
                           (145, "D_PLYR_AUDIO"), (219, "D_PLYR_FLAGS"),
                           (243, "D_QUIRKS"), (267, "D_STANCE_DATA")]:
            table(raw, off, label, 1)
        name = (string(raw[36:52]) + " " + string(raw[52:68])).strip()
        mem = bytearray(200)
        for target, source, size in [
                (0, 137, 8), (8, 165, 54), (64, 108, 4), (68, 112, 4),
                (72, 106, 2), (74, 116, 1), (76, 239, 4), (80, 263, 4),
                (84, 287, 12), (112, 36, 32), (144, 28, 4), (148, 0, 28),
                (176, 76, 4), (180, 80, 4), (184, 68, 2), (186, 32, 2),
                (188, 34, 2), (190, 84, 2), (192, 70, 6)]:
            mem[target:target+size] = raw[source:source+size]
        # All captured player audio blocks have the same values in these bytes.
        mem[17], mem[20] = 4, 1
        team = bits(mem, BITS["team"])
        term = raw[106] & 15
        if team == 511:
            mem[64:72] = bytes(8)
            mem[74] &= 0xF8
            term = 0
        elif term and not uint(raw, 112, 4):
            annual = first_year_salary(uint(raw, 108, 4), term, raw[116] & 7)
            mem[68:72] = annual.to_bytes(4, "little")
        mem[72] = 16 + (max(1, term) if team != 511 else 0)
        a = data[app+i*270:app+(i+1)*270]
        looks = bytearray(270)
        for _, src, dst, length in LOOK_MAP:
            looks[dst:dst+length] = a[src:src+length]
        v = data[att+i*37:att+(i+1)*37]
        initialized_fields(mem, v[3], v[13])
        rating = bytearray(v[:3]+v[24:25]+v[3:23]+v[23:24]+bytes(3)+v[25:])
        rating[14] = potential(bits(mem, BITS["age"]), rating[4], v[13])
        blocks = dict(record=bytes(mem), looks=bytes(looks), rating=bytes(rating),
                      pitcher=data[pit+i*31:pit+(i+1)*31])
        rows.append((name, blocks, v[13]))
    return rows


def members(data, rows):
    start, count = section(data, "D_TEAM_PROF")
    stats, _ = table(data, start+count*95, "D_TEAM_STATS", count)
    helpers, _ = table(data, stats+314*count, "D_ROSTER_HLPR", count)
    table(data, helpers+503*count, "D_COACH_STAFF", count)
    teams, placements = {}, {}
    for i in range(count):
        profile = start + i*95
        tid = uint(data, profile+46, 4)
        teams[tid] = string(data[profile+20:profile+40])
        helper = helpers + i*503
        slots = struct.unpack_from("<95I", data, helper+4)
        rotation = uint(data, helper+384, 4)
        line, _ = table(data, helper+388, "D_LINEUP", 5)
        local = {}
        for slot, number in enumerate(slots):
            if not number:
                continue
            if not 1 <= number <= len(rows):
                raise ValueError(f"invalid player number {number} for team {tid}")
            index = number-1
            if bits(rows[index][1]["record"], BITS["team"]) != tid:
                continue  # All-Star helper references a player whose primary team owns him.
            if index in placements:
                raise ValueError(f"repeated primary team assignment for disc row {index}")
            entry = dict(roster_slot=slot, rotation_position=slot+1 if slot < 5 else None,
                         rotation_index=rotation, lineups={})
            local[slot] = placements[index] = entry
        for index, mode in enumerate(MODES):
            lineup = data[line+index*19:line+(index+1)*19]
            for order, position in enumerate(lineup[10:], 1):
                if position < 10 and lineup[position] in local:
                    entry = local[lineup[position]]
                    entry["lineups"][mode] = dict(batting_order=order, position=POSITIONS[position])
    return teams, placements


def input_clubs(path, teams):
    # The disc ships a tab-separated roster source with named organization sections.
    organizations = {teams[i]: i for i in range(30)}
    by_id = {}
    club = None
    with path.open(encoding="latin-1") as stream:
        for line in stream:
            if line.startswith("; "):
                title = line.strip()[2:].rsplit(" ", 1)[0]
                if title.rsplit(" ", 1)[-1] in ("AAA", "AA", "A"):
                    title = title.rsplit(" ", 1)[0]
                club = organizations.get(title)
            elif line.startswith(";first_name\t"):
                header = line.strip().lstrip(";").split("\t")
                cols = [header.index(k) for k in ("first_name", "last_name", "Player_ID")]
            elif club is not None and line.strip() and not line.startswith(";"):
                fields = line.rstrip("\r\n").split("\t")
                first, last, pid = (fields[col] for col in cols)
                by_id[int(pid), (first+" "+last).strip()] = club
    return by_id


def make_cards(rows, teams, places, sheet):
    clubs = input_clubs(sheet, teams)
    cards = []
    for i, (name, blocks, raw_potential) in enumerate(rows):
        r, v, pit = (blocks[k] for k in ("record", "rating", "pitcher"))
        get = lambda key: bits(r, BITS[key])
        team, pid = get("team"), uint(r, 186, 2)
        if not name or not (team < 30 or 35 <= team < 125):
            continue
        if team < 30:
            club = team
        else:
            club = clubs.get((pid, name))
            if club is None:
                raise ValueError(f"minor league affiliation missing in {sheet} for ID {pid}")
        pos, kind = get("position"), get("pitcher_kind")
        position = POSITIONS[pos] if pos < len(POSITIONS) else None
        if pos == 0 and kind < len(KINDS):
            position = KINDS[kind]
        attributes = dict(zip(ATTRIBUTES, v[4:24]))
        # This is an offline base roster card. Potential comes from the disc.
        attributes["potential"] = raw_potential
        pitches = [dict(slot=j+1, code=code, control=pit[12+j], velocity_rating=pit[18+j],
                        movement=pit[24+j]) for j, code in enumerate(pit[:6]) if code != 255]
        cards.append(dict(name=name, roster_player_id=pid, online_id=uint(r, 180, 4),
                          team_index=club, roster_team_index=team, roster_team=teams[team],
                          level="MLB" if team < 30 else "AAA" if team < 65 else "AA" if team < 95 else "A",
                          position=position, position_code=pos, pitcher_kind=kind,
                          secondary_positions=[p for p in SECONDARY if get("secondary_"+p)],
                          bats=(HANDS+[None])[get("bat_hand")], throws=(HANDS+[None])[get("throw_hand")],
                          height_inches=get("height_inches"), weight_lb=get("weight_minus_100")+100,
                          age=get("age"), jersey_number=get("jersey_number"), overall=v[4],
                          attributes=attributes, pitches=pitches, pitcher_attributes=dict(zip(
                              ["stamina", "clutch", "hits_per_bf", "hr_per_bf", "k_per_bf", "bb_per_bf"],
                              pit[6:12])), **places[i]))
    return cards


def fullness(blocks):
    looks = blocks["looks"]
    return 1000*any(looks[:24]) + sum(v != 0 for v in looks[66:])


def select_names(rows):
    chosen = {}
    for name, blocks, _ in rows:
        if name and (name not in chosen or fullness(blocks) > fullness(chosen[name])):
            chosen[name] = blocks
    return chosen


def comparison_fields():
    fields = [(k, k, 0, n*8) for k, n in (("record", 200), ("looks", 270),
                                           ("rating", 40), ("pitcher", 31))]
    def field(block, name, offset, size):
        fields.append((block+"."+name, block, 8*offset, 8*size))
    for name, (off, bit, size) in BITS.items():
        fields.append(("record."+name, "record", 8*off+bit, size))
    for name, off, n in [
            ("anim_batting_style",0,2), ("anim_home_run_celeb",2,2), ("anim_pitching_style",4,2),
            ("anim_homerun_left",6,1), ("anim_homerun_right",7,1), ("audio_data",8,16),
            ("pos_yell",24,6), ("neg_yell",30,6), ("pos_chant",36,6), ("neg_chant",42,6),
            ("walkup_handle",48,6), ("mound_handle",54,6), ("homerun_handle",60,2), ("padding_3e",62,2),
            ("total_salary",64,4), ("current_salary",68,4), ("contract_data",72,2),
            ("contract_data_byte",74,1), ("padding_4b",75,1), ("flags",76,4), ("quirks",80,4),
            ("looks_ptr",96,8), ("rating_ptr",104,8), ("first_name",112,16), ("last_name",128,16),
            ("signed_pack",144,4), ("unsigned_pack",148,28), ("waiver_claims",176,4),
            ("online_id",180,4), ("database_index",184,2), ("player_id",186,2),
            ("first_name_id",188,2), ("inj_date",190,2), ("energy",192,1), ("confidence",193,1), ("injury_dur",194,1),
            ("dl_time",195,1), ("mlb_sd_season",196,1), ("mlb_service_days",197,1),
            ("bytes_c6_c7",198,2)]:
        field("record", name, off, n)
    for i, name in enumerate(STANCE):
        field("record", "bs_"+name, 84+i, 1)
    for name, _, dst, n in LOOK_MAP:
        field("looks", name, dst, n)
    for i, name in enumerate(ATTRIBUTES):
        field("rating", name, 4+i, 1)
    for name, off, n in [("fly_gb",0,1), ("left",1,1), ("right",2,1), ("morale",3,1),
                         ("war_mod_rank",24,1), ("padding",25,3), ("hit_zones",28,12)]:
        field("rating", name, off, n)
    for base, name in [(0,"pitch_type"), (12,"control"), (18,"velocity_rating"), (24,"movement")]:
        for i in range(6):
            field("pitcher", f"{name}_{i+1}", base+i, 1)
    for i, name in enumerate("stamina clutch hits_per_bf hr_per_bf k_per_bf bb_per_bf".split()):
        field("pitcher", name, 6+i, 1)
    field("pitcher", "data", 30, 1)
    return fields


def compare(path, rows, chosen):
    with path.open(encoding="utf-8") as stream:
        capture = json.load(stream)
    by_id = {(name, uint(blocks["record"], 186, 2)): blocks for name, blocks, _ in rows if name}
    fields = comparison_fields()
    matches, emitted = Counter(), Counter()
    samples = defaultdict(list)
    paired = selected = complete = exact_output = neutral_records = neutral_sets = 0
    collisions = []
    worst = []
    for name, values in capture.items():
        actual = {k: base64.b64decode(v, validate=True) if v is not None else None for k, v in values.items()}
        pid = uint(actual["record"], 186, 2)
        blocks = by_id.get((name, pid))
        if blocks is None:
            continue
        paired += 1
        picked = chosen[name] is blocks
        if not picked:
            collisions.append((name, pid, uint(chosen[name]["record"], 186, 2)))
        selected += picked
        complete += blocks == actual
        exact_output += picked and blocks == actual
        r, expected = blocks["record"], actual["record"]
        neutral = r[:96] == expected[:96] and r[112:] == expected[112:]
        neutral_records += neutral
        neutral_sets += neutral and all(blocks[k] == actual[k] for k in ("looks", "rating", "pitcher"))
        changes = {k: sum(a != b for a,b in zip(val, actual[k])) if actual[k] is not None
                   and len(actual[k]) == len(val) else len(val) for k, val in blocks.items()}
        worst.append((sum(changes.values()), name, pid, changes))
        for label, key, bit, width in fields:
            start = bit//8
            size = (bit%8+width+7)//8
            v = actual[key]
            spec = (start, bit%8, width)
            if v is not None and len(v) >= start+size and bits(v, spec) == bits(blocks[key], spec):
                matches[label] += 1
                emitted[label] += picked
            elif len(samples[label]) < 2 and width <= 32 and v is not None:
                samples[label].append(f"{name} {bits(blocks[key], spec)} vs {bits(v, spec)}")
    print(f"VERIFIED captured={len(capture)} matched_name_and_id={paired} "
          f"selected_in_output={selected} all_four_exact={complete} emitted_all_four_exact={exact_output}")
    print(f"VERIFIED ignoring only record[96:112], record_exact={neutral_records}/{paired} "
          f"all_four_exact={neutral_sets}/{paired}")
    for name, expected_id, emitted_id in collisions:
        print(f"Name collision: {name} captured ID {expected_id}, emitted ID {emitted_id}")
    print("field  exact disc identities / matched  |  exact emitted identities / captured")
    for label, *_ in fields:
        print(f"{label:36s} {matches[label]}/{paired}  {emitted[label]}/{len(capture)}  "
              + ", ".join(samples[label]))
    print("Worst matched players by differing bytes, including both pointer slots")
    for count, name, pid, changes in sorted(worst, reverse=True)[:8]:
        print(f"  {name} (ID {pid}) {count} {changes}")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as stream:
            temp = Path(stream.name)
            json.dump(data, stream, ensure_ascii=True, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def source_tables(stats, source, need_teams):
    if source == "base":
        players = (stats/"PLAYERS.BIN").read_bytes()
        teams = (stats/"ROSTERS.BIN").read_bytes() if need_teams else None
        return players, teams, stats/"INPUT/ROSTER.XLS"
    bundle = (stats/"ROSTER_LIVE.DAT").read_bytes()
    players, teams, statistics = struct.unpack_from("<3I", bundle)
    if 12+players+teams+statistics != len(bundle):
        raise ValueError("invalid ROSTER_LIVE.DAT section lengths")
    return (bundle[12:12+players], bundle[12+players:12+players+teams] if need_teams else None,
            stats/"INPUT/ROSTER_LIVE.XLS")


def main():
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("game_folder", type=Path, nargs="?", default=ROOT/"Extracted/CUSA35521")
    cli.add_argument("output", type=Path, nargs="?", default=ROOT/"probes/roster-study/roster-records.decoded.json")
    cli.add_argument("--cards", type=Path)
    cli.add_argument("--compare", type=Path)
    cli.add_argument("--source", choices=["base", "live"], default="base",
                     help="base uses PLAYERS.BIN; live reads the embedded ROSTER_LIVE.DAT tables")
    args = cli.parse_args()
    folder = args.game_folder.resolve()
    outputs = [p.resolve() for p in (args.output, args.cards) if p]
    protected = {PROTECTED.resolve()}
    if args.compare:
        protected.add(args.compare.resolve())
    if len(set(outputs)) != len(outputs) or any(
            p in protected or p.is_relative_to(folder) or p.is_relative_to((ROOT/"Extracted").resolve())
            or p.is_relative_to((ROOT/"Prepared").resolve()) for p in outputs):
        cli.error("choose distinct output paths outside game inputs and the reference capture")
    stats = folder / "FRONTEND/STATS"
    players_data, team_data, sheet = source_tables(stats, args.source, args.cards is not None)
    rows = records(players_data)
    chosen = select_names(rows)
    cards = None
    if args.cards:
        teams, places = members(team_data, rows)
        cards = make_cards(rows, teams, places, sheet)
    if args.compare:
        compare(args.compare, rows, chosen)
    write_json(args.output, {name: {k: base64.b64encode(v).decode("ascii") for k,v in blocks.items()}
                             for name, blocks in chosen.items()})
    print(f"VERIFIED {sum(bool(row[0]) for row in rows)} named disc rows, {len(chosen)} name keys")
    if cards is not None:
        write_json(args.cards, cards)
        print(f"VERIFIED {len(cards)} affiliated cards, levels {dict(Counter(c['level'] for c in cards))}")


if __name__ == "__main__":
    main()

