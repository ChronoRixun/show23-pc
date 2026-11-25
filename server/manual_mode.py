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
from types import SimpleNamespace
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
                   ('data/profiles/default/server', 'data/logs'))]
    if any(directory.resolve() != directory for directory in directories):
        raise ValueError('The install data folders must not contain links.')
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
    write_metadata(game / 'game.json', body.encode('utf-8'))
    write_metadata(manifests / 'game-ready.txt', (TITLE + '\n').encode('ascii'))
    return install


def setup_cli(install, args):
    parser = argparse.ArgumentParser(prog='setup', allow_abbrev=False)
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
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as error:
                raise ValueError(f'Local server port {port} is already in use or unavailable.') from error


def start_child(args, directory, log, env=None):
    with log.open('xb') as output:
        return subprocess.Popen(args, cwd=directory, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, env=env,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    try:
        child.send_signal(signal.CTRL_BREAK_EVENT)
    except OSError:
        child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
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


def version_key(value):
    match = re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-beta\.(0|[1-9][0-9]*))?', value)
    if not match:
        raise ValueError('Invalid runtime or server version.')
    return (*map(int, match.groups()[:3]), match[4] is None, int(match[4] or 0))


def installed_engine(install, script):
    manifests = install / 'manifests'
    release = (manifests / 'runtime.txt').read_text(encoding='ascii').strip()
    version_key(release)
    root = install / 'runtime' / release
    if script != (root / 'server/olserver.py').resolve():
        raise ValueError('Play must use the local server from the active runtime.')
    server = json.loads((manifests / release / 'server.json').read_text(encoding='utf-8'))
    if server.get('part') != 'server' or server.get('version') != release:
        raise ValueError('The local server does not match the active runtime.')
    try:
        floor = (manifests / 'server-state-version.txt').read_text(encoding='ascii').strip()
    except FileNotFoundError:
        floor = release
    if version_key(release) < version_key(floor):
        raise ValueError('This install is reserved for a newer local server. Choose a fresh install folder.')
    return root / 'emulator'


def native_environment(profile, fullscreen, resolution, name, logs):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('BB_')}
    env.update({
        'BB_CONFIG': str(profile / 'bbport.ini'),
        'BB_INPUT_CONFIG': str(profile / 'input.ini'),
        'BB_GPU_USER_DIR': str(profile / 'user'),
        'BB_VBLANK_HZ': '0',
        'BB_FPS_LIMIT': '120',
        'BB_PRESENT_MODE': 'Mailbox',
        'BB_READBACK_SMALL_BUFFERS': '1',
        'BB_READBACK_SMALL_GATE': '1',
        'BB_READBACK_SMALL_MAX': '4096',
        'BB_READBACK_TARGETS': '256x512',
        'BB_UPNP': '0',
        'BB_LOG_BUFFER': '1',
        'BB_WINDOW_SIZE': f'{RESOLUTION_WIDTH[resolution]}x{resolution}',
        'BB_FULLSCREEN': '1' if fullscreen else '0',
        'BB_USER_NAME': name,
        'TEMP': str(logs),
        'TMP': str(logs),
    })
    return env


def play_install(layout, script, fullscreen, no_update_art, ports, resolution=1080, name='Player'):
    install = Path(layout.install).resolve()
    engine = installed_engine(install, script)
    require_free_ports(ports)
    server_data = Path(layout.work)
    profile = server_data.parent / 'engine'
    user = profile / 'user'
    logs = Path(layout.logs)
    if any(path.resolve() != path for path in (server_data, profile, user, logs)):
        raise ValueError('The install data folders must not contain links.')
    user.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    for config in ('bbport.ini', 'input.ini'):
        target = profile / config
        if not target.exists():
            write_metadata(target, (engine / config).read_bytes())
        if target.resolve() != target or not target.is_file() or not target.stat().st_size:
            raise ValueError(f'Invalid game engine settings: {target}')
    suffix = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8]
    result = logs / f'prepare-{suffix}.txt'
    prepare_log = result.with_suffix('.log')
    server_log = logs / f'server-console-{suffix}.log'
    engine_log = logs / f'engine-{suffix}.log'
    ca = server_data / 'ca.pem'
    trust = [server_data / name for name in ('ca.pem', 'ca.key', 'server.pem', 'server.key')]

    def ready_trust():
        return all(path.resolve() == path and path.is_file() and path.stat().st_size for path in trust)

    py = [sys.executable, '-B', '-X', 'utf8', '-u']
    env = os.environ.copy()
    if no_update_art:
        env['SHOW23_NO_UPDATE_ART'] = '1'
    helper = server = game = None
    try:
        print(f'Preparing game files. The first launch can take a few minutes. Logs: {logs}', flush=True)
        helper = start_child([*py, engine / 'prepare.py', install, '--launcher-result', result], engine, prepare_log)
        if helper.wait():
            raise ValueError(f'Game preparation failed. See {prepare_log}.')
        key = result.read_text(encoding='ascii')
        if not re.fullmatch(r'v[0-9]+-[0-9a-f]{64}-[0-9a-f]{16}', key):
            raise ValueError(f'Game preparation did not produce a usable cache. See {prepare_log}.')
        cache = install / 'data/engine-cache' / TITLE / key
        if not all((cache / leaf).is_file() for leaf in ('boot-linked.bin', 'content.bin')):
            raise ValueError(f'Game preparation did not produce a usable cache. See {prepare_log}.')
        if not ready_trust():
            cert_log = logs / f'server-certs-{suffix}.log'
            helper = start_child([*py, script, 'gen-certs', '--install', install], script.parent, cert_log, env)
            if helper.wait(timeout=60) or not ready_trust():
                raise ValueError(f'Local certificate generation failed. See {cert_log}.')
        print('Starting the local server.', flush=True)
        server = start_child([*py, script, 'run', '--install', install, f'--log=server-{suffix}.log'],
                             script.parent, server_log, env)
        wait_for_server(server, server_log, f'listening on 127.0.0.1:{ports[0]} for '.encode('ascii'))
        args = [engine / 'bb-probe.exe', cache / 'boot-linked.bin', '--content-profile', cache / 'content.bin',
                '--app0', layout.base, '--user', user, '--timeout', '0', '--show23-local-ca', ca]
        game = start_child(args, engine, engine_log, native_environment(profile, fullscreen, resolution, name, logs))
        while game.poll() is None:
            if server.poll() is not None and game.poll() is None:
                raise ValueError(f'The local server exited during play ({server.returncode}). See {server_log}.')
            time.sleep(0.1)
        if game.returncode:
            print(f'play: game engine exited with status {game.returncode}. See {engine_log}.', file=sys.stderr)
        return game.returncode
    finally:
        try:
            stop_child(game)
        finally:
            try:
                stop_child(server)
            finally:
                stop_child(helper)


def play_cli(layout, script, args, ports):
    parser = argparse.ArgumentParser(prog='play', allow_abbrev=False)
    parser.add_argument('--fullscreen', action='store_true')
    parser.add_argument('--no-update-art', action='store_true')
    parser.add_argument('--resolution', type=int, choices=RESOLUTION_WIDTH, default=1080)
    parser.add_argument('--name', default='Player', help='player name for the local profile')
    options = parser.parse_args(args)
    name = options.name.strip(' ')
    if not re.fullmatch(r'[A-Za-z0-9 _-]{3,16}', name):
        parser.error('Use 3 to 16 letters, digits, spaces, hyphens or underscores for the player name.')
    if layout.install is None:
        parser.error('choose an install folder')
    if os.name != 'nt':
        parser.error('This release requires Windows.')
    try:
        return play_install(layout, Path(script).resolve(), options.fullscreen,
                            options.no_update_art, ports, options.resolution, name)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'play: {error}', file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(usage='%(prog)s {setup,play} <install> [options]', allow_abbrev=False)
    parser.add_argument('action', choices=('setup', 'play'))
    parser.add_argument('install', type=Path)
    options, args = parser.parse_known_args()
    if os.name != 'nt':
        parser.error('This release requires Windows.')
    if options.action == 'setup':
        return setup_cli(options.install, args)
    try:
        install = options.install.resolve(strict=True)
        record = json.loads((install / 'data/games' / TITLE / 'game.json').read_text(encoding='utf-8'))
        layout = SimpleNamespace(install=install, base=record['base']['path'],
                                 work=install / 'data/profiles/default/server', logs=install / 'data/logs')
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'play: could not read the game record: {error}', file=sys.stderr)
        return 1
    return play_cli(layout, Path(__file__).with_name('olserver.py'), args, (10000, 10080))


if __name__ == '__main__':
    sys.exit(main())
