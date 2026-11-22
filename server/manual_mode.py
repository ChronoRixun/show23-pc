"""Prepare and play an unpacked install without the launcher."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


TITLE = 'CUSA35521'
# Executable references shared with the launcher's input_checks.hpp.
EXECUTABLES = {
    '1.00': (38785273, '78858c91ee74e69056c8000cb54d3451b2d738b9b9d3d89c006b704be3ec3aa1'),
    '1.21': (39247689, 'ceb28e9b8e96e582214e785682f2d4f27dea0746edb4b4c6a36df62dc2bd9bec'),
}
DIRECTORIES = ('PS4', 'CROSS', 'FLUX', 'FRONTEND')
RESOLUTION_WIDTH = {720: 1280, 1080: 1920}


def directory_bytes(directory):
    total = 0
    with os.scandir(directory) as children:
        for entry in children:
            info = entry.stat(follow_symlinks=False)
            if getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f'Linked entry in game folder: {entry.path}')
            if stat.S_ISDIR(info.st_mode):
                total += directory_bytes(entry.path)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
            else:
                raise ValueError(f'Unsupported entry in game folder: {entry.path}')
    return total


def game_folder(folder, version, label):
    boot = folder / 'eboot.bin'
    if not boot.is_file():
        raise ValueError(f'{label} folder is missing eboot.bin: {folder}')
    expected = EXECUTABLES[version]
    size = boot.stat().st_size
    if size not in {reference[0] for reference in EXECUTABLES.values()}:
        raise ValueError(f'{label} eboot.bin has the wrong size for version {version}.')
    digest = hashlib.sha256()
    with boot.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    identity = size, digest.hexdigest()
    if identity != expected:
        actual = next((name for name, reference in EXECUTABLES.items() if reference == identity), None)
        if actual is not None:
            raise ValueError(f'{label} folder is version {actual}; expected {version}.')
        raise ValueError(f'{label} eboot.bin failed SHA-256 verification for version {version}.')
    totals = {}
    for name in DIRECTORIES:
        directory = folder / name
        if not directory.is_dir() or directory.resolve() != directory:
            raise ValueError(f'{label} folder needs an unpacked {name} directory: {folder}')
        totals[name] = directory_bytes(directory)
    return {'path': str(folder), 'version': version,
            'executable': {'sha256': identity[1], 'size': size}, 'directory_bytes': totals}


def write_metadata(path, content):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_profile_json(path):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('duplicate keys')
        return result

    def invalid_constant(value):
        raise ValueError(f'invalid JSON constant: {value}')

    try:
        return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)
    except FileNotFoundError:
        return {}
    except (ValueError, RecursionError):
        raise ValueError(f'{path.name} is corrupt. Repair it before playing.') from None


def new_users(name):
    return {'Users': {'commit_hash': '', 'user': [
        {
            'np_age': 30, 'np_country': 'us', 'np_date_of_birth': '1994-01-01',
            'np_language': 'en', 'player_index': slot, 'shadnet_email': '',
            'shadnet_enabled': False, 'shadnet_npid': '', 'shadnet_password': '',
            'shadnet_token': '', 'user_color': slot, 'user_id': 999 + slot,
            'user_name': name if slot == 1 else f'Player {slot}',
        }
        for slot in range(1, 5)
    ]}}


def update_user_name(path, name):
    name = name.strip(' ')
    if not re.fullmatch(r'[A-Za-z0-9 _-]{3,16}', name):
        raise ValueError('Player name must be 3–16 letters, digits, spaces, hyphens or underscores.')
    if not path.exists():
        return new_users(name)
    data = read_profile_json(path)
    group = data.get('Users') if isinstance(data, dict) else None
    users = group.get('user') if isinstance(group, dict) else None
    error = 'users.json has an invalid player profile. Repair it before playing.'
    if not isinstance(users, list) or any(not isinstance(row, dict) or type(row.get('user_id')) is not int
                                          for row in users):
        raise ValueError(error)
    primary = [row for row in users if row['user_id'] == 1000]
    if len(primary) != 1:
        raise ValueError(error)
    row, = primary
    if type(row.get('player_index')) is not int or row['player_index'] != 1 or not isinstance(row.get('user_name'), str):
        raise ValueError(error)
    if row['user_name'] == name:
        return None
    row['user_name'] = name
    return data


def write_play_settings(user, resolution, name):
    config_path = user / 'config.json'
    config = read_profile_json(config_path)
    groups = ('General', 'Network', 'Log', 'Debug', 'Input', 'Audio', 'GPU', 'Vulkan')
    if not isinstance(config, dict) or any(key in config and not isinstance(config[key], dict) for key in groups):
        raise ValueError('config.json is corrupt. Repair it before playing.')
    width = RESOLUTION_WIDTH[resolution]
    config.setdefault('GPU', {}).update(internal_screen_width=width, internal_screen_height=resolution,
                                       window_width=width, window_height=resolution)
    user_path = user / 'users.json'
    users = update_user_name(user_path, name) if name is not None else None
    # Validate both files before replacing either.
    updates = [(config_path, config)]
    if users is not None:
        updates.append((user_path, users))
    encoded = [(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8'))
               for path, value in updates]
    for path, content in encoded:
        write_metadata(path, content)


def setup_install(install, base, update):
    install = Path(install).resolve()
    sources = [('base', '1.00', base)]
    if update is not None:
        sources.append(('update', '1.21', update))
    record = {'schema': 1, 'title_id': TITLE, 'mode': 'in_place'}
    for kind, version, source in sources:
        folder = source.resolve()
        if not folder.is_dir():
            raise ValueError(f'{kind.capitalize()} must be an unpacked game folder, not a package: {source}')
        if install.is_relative_to(folder) or folder.is_relative_to(install):
            raise ValueError('The install folder and the game folders must be separate.')
        record[kind] = game_folder(folder, version, kind.capitalize())
    record['created_utc'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    game = install / 'data/games' / TITLE
    manifests = install / 'manifests'
    directories = [game, manifests, *(install / relative for relative in
                   ('data/profiles/default/user', 'data/profiles/default/server', 'data/logs'))]
    if any(directory.resolve() != directory for directory in directories):
        raise ValueError('The install data folders must not contain links.')
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
    write_metadata(game / 'game.json', body.encode('utf-8'))
    write_metadata(manifests / 'game-ready.txt', (TITLE + '\n').encode('ascii'))
    return install


def setup_cli(install, args):
    parser = argparse.ArgumentParser(prog='olserver.py setup', allow_abbrev=False)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--update', type=Path)
    options = parser.parse_args(args)
    if install is None:
        parser.error('give an install folder with --install')
    try:
        path = setup_install(install, options.base, options.update)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'setup: {error}', file=sys.stderr)
        return 1
    print(f'Setup complete: {path}')
    return 0


def require_free_ports(ports):
    for port in ports:
        with socket.socket() as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as error:
                raise ValueError(f'Local server port {port} is already in use or unavailable.') from error


def start_child(args, directory, log, env=None):
    group = (dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt'
             else dict(start_new_session=True))
    with log.open('xb') as output:
        return subprocess.Popen(args, cwd=directory, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, env=env, **group)


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    if os.name == 'nt':
        try:
            child.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            child.terminate()
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            child.kill()
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait()


def wait_for_server(child, log, prefix, timeout=60):
    # The private child log receives this line only after both sockets bind.
    deadline = time.monotonic() + timeout
    with log.open('rb') as output:
        while child.poll() is None and time.monotonic() < deadline:
            offset = output.tell()
            line = output.readline()
            if not line.endswith(b'\n'):
                output.seek(offset)
                time.sleep(0.1)
            elif prefix in line:
                return
    if child.poll() is not None:
        raise ValueError(f'The server exited during startup ({child.returncode}). See {log}.')
    raise ValueError(f'The server did not start within {timeout} seconds. See {log}.')


def play_install(layout, script, emulator, fullscreen, no_update_art, ports, resolution=1080, name=None):
    emulator = emulator.resolve()
    boot = Path(layout.base) / 'eboot.bin'
    if not emulator.is_file():
        raise ValueError(f'Emulator executable not found: {emulator}')
    if not boot.is_file():
        raise ValueError(f'The base game is missing eboot.bin: {boot.parent}')
    require_free_ports(ports)
    profile = Path(layout.work).parent
    user = profile / 'user'
    user.mkdir(parents=True, exist_ok=True)
    write_play_settings(user, resolution, name)
    logs = Path(layout.logs)
    logs.mkdir(parents=True, exist_ok=True)
    ca = Path(layout.work) / 'ca.pem'
    suffix = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8]
    server_log = logs / f'server-console-{suffix}.log'
    emulator_log = logs / f'emulator-{suffix}.log'
    env = dict(os.environ)
    if no_update_art:
        env['SHOW23_NO_UPDATE_ART'] = '1'
    server = game = None
    try:
        server = start_child([sys.executable, '-B', str(script), 'run', '--install', layout.install,
                              f'--log=server-{suffix}.log'], script.parent, server_log, env)
        wait_for_server(server, server_log, f'listening on 127.0.0.1:{ports[0]} for '.encode('ascii'))
        if not ca.is_file() or not ca.stat().st_size:
            raise ValueError(f'The server did not provide its certificate. See {server_log}.')
        args = [os.fspath(emulator), '-g', str(boot), '-i', '-f', str(fullscreen).lower(),
                '--show23-local-ca', str(ca)]
        game = start_child(args, profile, emulator_log)
        while game.poll() is None:
            if server.poll() is not None:
                raise ValueError(f'The server exited while playing ({server.returncode}). See {server_log}.')
            time.sleep(0.1)
        if game.returncode:
            print(f'play: emulator exited with status {game.returncode}. See {emulator_log}.', file=sys.stderr)
        return game.returncode if game.returncode >= 0 else 128 - game.returncode
    finally:
        try:
            stop_child(game)
        finally:
            stop_child(server)


def play_cli(layout, script, args, ports):
    parser = argparse.ArgumentParser(prog='olserver.py play', allow_abbrev=False)
    parser.add_argument('--emulator', type=Path, required=True)
    parser.add_argument('--fullscreen', action='store_true')
    parser.add_argument('--no-update-art', action='store_true')
    parser.add_argument('--resolution', type=int, choices=RESOLUTION_WIDTH, default=1080)
    parser.add_argument('--name', help='player name for the primary local profile')
    options = parser.parse_args(args)
    if layout.install is None:
        parser.error('give an install folder with --install')
    try:
        return play_install(layout, Path(script).resolve(), options.emulator, options.fullscreen,
                            options.no_update_art, ports, resolution=options.resolution, name=options.name)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(f'play: {error}', file=sys.stderr)
        return 1
