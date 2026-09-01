#!/usr/bin/env python3
"""Stage, preflight, activate, inspect, or roll back the final Methodenbot."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid


HOST = os.environ.get('METHODENBOT_DEPLOY_HOST', '').strip()
ADMIN = os.environ.get('METHODENBOT_DEPLOY_ADMIN', '').strip()
SERVICE_USER = 'methodenbot'
SERVICE = 'methodenbot.service'
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'MANIFEST.sha256'
INSTALL_ROOT = Path('/srv/methodenbot-final')
RELEASES = INSTALL_ROOT / 'releases'
STAGED = INSTALL_ROOT / 'staged'
CURRENT = INSTALL_ROOT / 'current'
STATUS = INSTALL_ROOT / 'stage-status.json'
ETC = Path('/etc/methodenbot')
RUNTIME_ENV = ETC / 'runtime.env'
LOCAL_TOKEN = ETC / 'gwdg-local-token'
GATEWAY_CLIENT = Path('/etc/posit-gwdg-gateway/client.env')
STATE = Path('/var/lib/methodenbot')
OLD = Path('/home/methodenbot/methodenbot')
VENV_PYTHON = OLD / 'venv/bin/python'
UNIT = Path('/etc/systemd/system/methodenbot.service')
DROPIN_DIR = Path('/etc/systemd/system/methodenbot.service.d')
DROPIN = DROPIN_DIR / '20-final.conf'
BACKUPS = Path('/var/backups/methodenbot-final')
MAX_ADDITIONAL_CONTROL_ROOMS = 8
MATRIX_USER_ID = re.compile(r'@[^\s:]+:[^\s]+')
MATRIX_ROOM_ID = re.compile(r'![^\s:]+:[^\s]+')


class DeployError(RuntimeError):
    pass


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def manifest_entries():
    if not MANIFEST.is_file() or MANIFEST.is_symlink():
        raise DeployError('manifest_missing')
    expected = {}
    for line in MANIFEST.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.ÄÖÜäöüß/-]+)', line)
        if not match:
            raise DeployError('manifest_invalid')
        relative = PurePosixPath(match.group(2))
        if relative.is_absolute() or '..' in relative.parts or str(relative) in expected:
            raise DeployError('manifest_path_invalid')
        expected[str(relative)] = match.group(1)
    return expected


def verify_bundle():
    expected = manifest_entries()
    actual = set()
    for path in ROOT.rglob('*'):
        if path.name == 'MANIFEST.sha256':
            continue
        relative = path.relative_to(ROOT)
        if '__pycache__' in relative.parts or path.suffix == '.pyc':
            continue
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                raise DeployError('bundle_symlink_forbidden')
            continue
        actual.add(relative.as_posix())
    if actual != set(expected):
        raise DeployError('bundle_members_differ_from_manifest')
    for relative, expected_hash in expected.items():
        path = ROOT / relative
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2_000_000 or sha256(path) != expected_hash:
            raise DeployError('bundle_hash_mismatch')
    return hashlib.sha256(MANIFEST.read_bytes()).hexdigest()


def check_host():
    if not HOST:
        raise DeployError('deploy_host_not_configured')
    if socket.gethostname().split('.')[0] != HOST:
        raise DeployError('wrong_host')


def require_root():
    check_host()
    if not ADMIN:
        raise DeployError('deploy_admin_not_configured')
    if os.geteuid() != 0 or os.environ.get('SUDO_USER') != ADMIN:
        raise DeployError('run_with_sudo_as_configured_admin')


def service_identity():
    try:
        return pwd.getpwnam(SERVICE_USER)
    except KeyError:
        raise DeployError('service_user_missing') from None


def run(command, *, check=True, capture=False, cwd=None, env=None):
    return subprocess.run(command, check=check, cwd=cwd, env=env, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def atomic_write(path, data, mode, uid=0, gid=0):
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = path.parent / ('.' + path.name + '.new.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, uid, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def env_values(lines):
    values = {}
    pattern = re.compile(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$')
    for line in lines:
        match = pattern.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"\'')
    return values


def additional_control_rooms(raw, *, control_user, control_room, production_room):
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result

    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 16_384:
        raise DeployError('matrix_additional_control_rooms_invalid')
    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (TypeError, ValueError, RecursionError):
        raise DeployError('matrix_additional_control_rooms_invalid') from None
    if (not isinstance(value, dict) or len(value) > MAX_ADDITIONAL_CONTROL_ROOMS
            or any(not isinstance(user, str) or not MATRIX_USER_ID.fullmatch(user)
                   or not isinstance(room, str) or not MATRIX_ROOM_ID.fullmatch(room)
                   for user, room in value.items())
            or len(set(value.values())) != len(value)
            or control_user in value
            or control_room in value.values()
            or production_room in value.values()):
        raise DeployError('matrix_additional_control_rooms_invalid')
    return value


def control_bindings_hash(control_user, control_room, additional):
    bindings = [(control_user, control_room), *sorted(additional.items())]
    raw = json.dumps(bindings, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def final_runtime_env(source):
    try:
        lines = source.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        raise DeployError('production_env_unreadable') from None
    source_values = env_values(lines)
    control_user = source_values.get('MATRIX_CONTROL_USER', '')
    if not MATRIX_USER_ID.fullmatch(control_user):
        raise DeployError('matrix_control_user_invalid')
    control_room = source_values.get('MATRIX_CONSOLE_ROOM_ID', '')
    production_room = source_values.get('MATRIX_ROOM_ID', '')
    if not MATRIX_ROOM_ID.fullmatch(control_room) or not MATRIX_ROOM_ID.fullmatch(production_room):
        raise DeployError('matrix_room_id_invalid')
    additional = additional_control_rooms(
        source_values.get('MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON', '{}'),
        control_user=control_user,
        control_room=control_room, production_room=production_room)
    managed = {
        'MATRIX_CONTROL_USER': control_user,
        'MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON': json.dumps(
            additional, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        'MATRIX_DEVICE_ID': 'METHODENBOT_FINAL_2026_08',
        'MATRIX_ALLOW_UNENCRYPTED_CONTROL_DM': 'true',
        'METHODENBOT_AI_ENABLED': 'true',
        'METHODENBOT_AI_DEFAULT_ENABLED': 'false',
        'GWDG_DATA_TRANSFER_APPROVED': 'true',
        'GWDG_MODEL': 'qwen3-30b-a3b-instruct-2507',
    }
    removed = set(managed) | {
        'GWDG_API_KEY', 'GWDG_API_KEY_FILE', 'METHODENBOT_EXPERIMENT_LIVE',
        'MATRIX_ALLOW_UNENCRYPTED_TEST_DM', 'METHODENBOT_STATE_DIR',
        'METHODENBOT_ENV_FILE', 'MATRIX_TOKEN_FILE'}
    key_pattern = re.compile(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=')
    kept = [line for line in lines
            if not ((match := key_pattern.match(line)) and match.group(1) in removed)]
    kept.extend(['', '# Methodenbot final: zentral verwaltete Werte',
                 *(key + '=' + value for key, value in managed.items())])
    values = env_values(kept)
    required = ('UK_NUMMER', 'EMAIL_ADDRESS', 'EMAIL_PASSWORD', 'EWS_ENDPOINT',
                'MATRIX_SERVER', 'MATRIX_USER', 'MATRIX_PASSWORD', 'MATRIX_ROOM_ID',
                'MATRIX_CONSOLE_ROOM_ID', 'MATRIX_CONTROL_USER', 'GOOGLE_FORM_LINK')
    if any(not values.get(key) for key in required):
        raise DeployError('required_runtime_value_missing')
    if values['MATRIX_ROOM_ID'] == values['MATRIX_CONSOLE_ROOM_ID']:
        raise DeployError('control_and_production_room_equal')
    return ('\n'.join(kept).rstrip() + '\n').encode('utf-8')


def gateway_token():
    try:
        lines = GATEWAY_CLIENT.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        raise DeployError('gateway_client_unreadable') from None
    values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        match = re.fullmatch(r'(OPENAI_COMPATIBLE_BASE_URL|OPENAI_COMPATIBLE_API_KEY)=([^\s]+)', stripped)
        if not match or match.group(1) in values:
            raise DeployError('gateway_client_invalid')
        values[match.group(1)] = match.group(2)
    if (set(values) != {'OPENAI_COMPATIBLE_BASE_URL', 'OPENAI_COMPATIBLE_API_KEY'}
            or values['OPENAI_COMPATIBLE_BASE_URL'] != 'http://127.0.0.1:18765/v1'
            or not values['OPENAI_COMPATIBLE_API_KEY']):
        raise DeployError('gateway_client_invalid')
    return (values['OPENAI_COMPATIBLE_API_KEY'] + '\n').encode('utf-8')


def replace_symlink(path, target):
    temporary = path.parent / ('.' + path.name + '.new.' + uuid.uuid4().hex)
    temporary.symlink_to(target)
    os.replace(temporary, path)


def remove_symlink(path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISLNK(metadata.st_mode):
        raise DeployError('expected_symlink')
    path.unlink()


def installed_final_release():
    """Return the configured final release, or None for the untouched legacy unit.

    A half-present current link/drop-in is not treated as a first installation:
    continuing from such a state could mix legacy CSV files with final state.
    """
    try:
        current_metadata = os.lstat(CURRENT)
        current_present = True
    except FileNotFoundError:
        current_metadata = None
        current_present = False
    try:
        dropin_metadata = os.lstat(DROPIN)
        dropin_present = True
    except FileNotFoundError:
        dropin_metadata = None
        dropin_present = False
    if not current_present and not dropin_present:
        return None
    if (not current_present or not dropin_present
            or not stat.S_ISLNK(current_metadata.st_mode)
            or not stat.S_ISREG(dropin_metadata.st_mode)
            or DROPIN.is_symlink() or DROPIN.read_bytes() != dropin_text()):
        raise DeployError('inconsistent_final_installation')
    try:
        release = CURRENT.resolve(strict=True)
        metadata = os.lstat(release)
    except (OSError, RuntimeError):
        raise DeployError('inconsistent_final_installation') from None
    if (release.parent != RELEASES or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)):
        raise DeployError('inconsistent_final_installation')
    return release


def validate_protected_file(path, *, mode, uid, gid):
    try:
        metadata = os.lstat(path)
    except OSError:
        raise DeployError('protected_runtime_file_missing') from None
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_uid != uid or metadata.st_gid != gid):
        raise DeployError('protected_runtime_file_invalid')


def validate_existing_runtime(identity):
    validate_protected_file(RUNTIME_ENV, mode=0o640, uid=0, gid=identity.pw_gid)
    validate_protected_file(LOCAL_TOKEN, mode=0o600, uid=0, gid=0)
    # Validate without rewriting the canonical files of a running final release.
    final_runtime_env(RUNTIME_ENV)
    try:
        values = env_values(RUNTIME_ENV.read_text(encoding='utf-8').splitlines())
    except (OSError, UnicodeError):
        raise DeployError('production_env_unreadable') from None
    expected = {
        'MATRIX_DEVICE_ID': 'METHODENBOT_FINAL_2026_08',
        'MATRIX_ALLOW_UNENCRYPTED_CONTROL_DM': 'true',
        'METHODENBOT_AI_ENABLED': 'true',
        'METHODENBOT_AI_DEFAULT_ENABLED': 'false',
        'GWDG_DATA_TRANSFER_APPROVED': 'true',
        'GWDG_MODEL': 'qwen3-30b-a3b-instruct-2507',
    }
    forbidden = {'GWDG_API_KEY', 'GWDG_API_KEY_FILE', 'METHODENBOT_EXPERIMENT_LIVE',
                 'MATRIX_ALLOW_UNENCRYPTED_TEST_DM', 'METHODENBOT_STATE_DIR',
                 'METHODENBOT_ENV_FILE', 'MATRIX_TOKEN_FILE'}
    control_user = values.get('MATRIX_CONTROL_USER', '')
    if (not MATRIX_USER_ID.fullmatch(control_user)
            or any(values.get(key) != value for key, value in expected.items())
            or forbidden & set(values)):
        raise DeployError('canonical_runtime_configuration_invalid')
    try:
        token = LOCAL_TOKEN.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise DeployError('local_token_unreadable') from None
    stripped = token.strip()
    if (token != stripped + '\n' or not stripped or len(stripped) > 8192
            or any(character.isspace() for character in stripped)):
        raise DeployError('local_token_invalid')


def prepare_runtime_configuration(identity):
    """Initialize secrets once; follow-up releases preserve canonical values."""
    existing = installed_final_release()
    ETC.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(ETC, 0, identity.pw_gid)
    os.chmod(ETC, 0o750)
    if existing is not None:
        validate_existing_runtime(identity)
        return existing
    atomic_write(RUNTIME_ENV, final_runtime_env(OLD / '.env'), 0o640, 0, identity.pw_gid)
    atomic_write(LOCAL_TOKEN, gateway_token(), 0o600, 0, 0)
    return None


def write_status(release, live):
    atomic_write(STATUS, (json.dumps({'version': 1, 'release': release.name,
                                     'live_preflight': bool(live)}, sort_keys=True) + '\n').encode(),
                 0o644)


def read_status():
    try:
        data = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        raise DeployError('stage_status_missing') from None
    if (not isinstance(data, dict) or data.get('version') != 1
            or not isinstance(data.get('release'), str) or type(data.get('live_preflight')) is not bool):
        raise DeployError('stage_status_invalid')
    release = RELEASES / data['release']
    if release.parent != RELEASES or not release.is_dir() or STAGED.resolve() != release.resolve():
        raise DeployError('staged_release_mismatch')
    return release, data


def as_service(command, *, cwd):
    identity = service_identity()
    environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   'HOME': str(STATE), 'PYTHONDONTWRITEBYTECODE': '1',
                   'METHODENBOT_ENV_FILE': str(RUNTIME_ENV),
                   'METHODENBOT_STATE_DIR': str(STATE)}
    return run(['/usr/sbin/runuser', '-u', identity.pw_name, '--', *command], cwd=cwd, env=environment)


def plan():
    check_host()
    manifest_hash = verify_bundle()
    print('Bundle verifiziert; keine Aenderung ausgefuehrt.')
    print('manifest_sha256=' + manifest_hash)
    print('Geplant: /srv/methodenbot-final, /etc/methodenbot, /var/lib/methodenbot')
    print('Aktivierung stoppt/startet methodenbot.service erst nach separater Bestaetigung.')


def stage(confirm):
    require_root()
    if not confirm:
        raise DeployError('data_transfer_confirmation_required')
    verify_bundle()
    identity = service_identity()
    if not VENV_PYTHON.is_file() or not UNIT.is_file() or not (OLD / '.env').is_file():
        raise DeployError('production_baseline_missing')
    if not re.fullmatch(r'methodenbot-final-[A-Za-z0-9T-]+', ROOT.name):
        raise DeployError('unexpected_bundle_directory_name')
    RELEASES.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = RELEASES / ROOT.name
    if target.exists():
        raise DeployError('release_already_staged')
    target.mkdir(mode=0o755)
    for relative in [*manifest_entries(), 'MANIFEST.sha256']:
        source, destination = ROOT / relative, target / relative
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for path in [target, *target.rglob('*')]:
        if path.is_symlink():
            raise DeployError('installed_release_contains_symlink')
        os.chown(path, 0, 0)
        os.chmod(path, 0o755 if path.is_dir() or path.parent.name == 'deployment' else 0o644)
    INSTALL_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    replace_symlink(STAGED, target)

    prepare_runtime_configuration(identity)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(STATE, identity.pw_uid, identity.pw_gid)
    os.chmod(STATE, 0o700)

    as_service([str(VENV_PYTHON), '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=target)
    as_service([str(VENV_PYTHON), '-m', 'pip', 'check'], cwd=target)
    as_service([str(VENV_PYTHON), '-B', 'deployment/runtime_preflight.py', '--offline'], cwd=target)
    write_status(target, False)
    print('Release gestaged; Produktivdienst unveraendert.')
    print('release=' + target.name)
    print('Naechster Schritt: live-preflight')


def live_preflight(confirm):
    require_root()
    if not confirm:
        raise DeployError('data_transfer_confirmation_required')
    release, _data = read_status()
    unit_name = 'methodenbot-final-preflight-' + str(os.getpid())
    command = [
        '/usr/bin/systemd-run', '--quiet', '--wait', '--pipe', '--collect',
        '--unit=' + unit_name, '--uid=' + SERVICE_USER, '--gid=' + SERVICE_USER,
        '--property=UMask=0077', '--property=NoNewPrivileges=yes',
        '--property=LoadCredential=gwdg-local-token:' + str(LOCAL_TOKEN),
        '--setenv=METHODENBOT_ENV_FILE=' + str(RUNTIME_ENV),
        '--setenv=METHODENBOT_STATE_DIR=' + str(STATE),
        '--working-directory=' + str(release),
        str(VENV_PYTHON), '-B', str(release / 'deployment/runtime_preflight.py'), '--live']
    run(command)
    write_status(release, True)
    print('Live-Preflight erfolgreich; keine Mail und keine Matrix-Nachricht versendet.')


def copy_runtime_file(source, target, identity):
    if not source.is_file():
        raise DeployError('runtime_csv_missing')
    if target.exists() and sha256(target) != sha256(source):
        raise DeployError('runtime_csv_conflict')
    if not target.exists():
        shutil.copyfile(source, target)
    os.chown(target, identity.pw_uid, identity.pw_gid)
    os.chmod(target, 0o600)


def replace_runtime_file(source, target, identity):
    if not source.is_file():
        raise DeployError('runtime_csv_missing')
    temporary = target.parent / ('.' + target.name + '.rollback.' + uuid.uuid4().hex)
    shutil.copyfile(source, temporary)
    os.chown(temporary, identity.pw_uid, identity.pw_gid)
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def validated_release_path(value, *, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise DeployError('backup_metadata_invalid')
    release = Path(value)
    try:
        metadata = os.lstat(release)
        resolved = release.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DeployError('backup_release_missing') from None
    if (not release.is_absolute() or resolved != release or release.parent != RELEASES
            or not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise DeployError('backup_release_invalid')
    return release


def backup_metadata(backup):
    try:
        data = json.loads((backup / 'metadata.json').read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise DeployError('backup_metadata_invalid') from None
    if (not isinstance(data, dict)
            or set(data) != {'version', 'prior_current', 'activated_release'}
            or data.get('version') != 2):
        raise DeployError('backup_metadata_invalid')
    return {
        'prior_current': validated_release_path(data.get('prior_current'), allow_none=True),
        'activated_release': validated_release_path(data.get('activated_release')),
    }


def backup_production(release):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BACKUPS / ('before-final-' + stamp)
    backup.mkdir(mode=0o700, parents=True)
    os.chmod(BACKUPS, 0o700)
    if UNIT.exists():
        shutil.copy2(UNIT, backup / 'methodenbot.service')
    if DROPIN_DIR.exists():
        shutil.copytree(DROPIN_DIR, backup / 'dropins', symlinks=True)
    shutil.copytree(OLD, backup / 'old-tree', symlinks=True,
                    ignore=shutil.ignore_patterns('venv', '__pycache__', '*.pyc'))
    prior_release = installed_final_release()
    data = {'version': 2,
            'prior_current': str(prior_release) if prior_release is not None else None,
            'activated_release': str(release)}
    atomic_write(backup / 'metadata.json', (json.dumps(data, sort_keys=True) + '\n').encode(),
                 0o600)
    return backup


def dropin_text():
    return f'''[Service]
ExecStart=
ExecStart={VENV_PYTHON} -B {CURRENT}/main.py
WorkingDirectory={CURRENT}
Environment=METHODENBOT_ENV_FILE={RUNTIME_ENV}
Environment=METHODENBOT_STATE_DIR={STATE}
StateDirectory=methodenbot
StateDirectoryMode=0700
UMask=0077
LoadCredential=gwdg-local-token:{LOCAL_TOKEN}
'''.encode('utf-8')


def service_active():
    return run(['/usr/bin/systemctl', 'is-active', '--quiet', SERVICE], check=False).returncode == 0


def service_snapshot(expected_release):
    configured_release = installed_final_release()
    if expected_release is None:
        if configured_release is not None:
            raise DeployError('unexpected_final_configuration')
        expected_main = str(OLD / 'main.py')
    else:
        expected_release = Path(expected_release)
        if configured_release != expected_release:
            raise DeployError('unexpected_final_release')
        expected_main = str(CURRENT / 'main.py')
    if not service_active():
        raise DeployError('service_not_active')
    result = run(['/usr/bin/systemctl', 'show', SERVICE, '--property=MainPID', '--value'],
                 capture=True)
    pid_text = result.stdout.strip()
    if not pid_text.isdigit() or int(pid_text) <= 0:
        raise DeployError('service_main_pid_missing')
    try:
        arguments = (Path('/proc') / pid_text / 'cmdline').read_bytes().split(b'\0')
        arguments = [argument.decode('utf-8', errors='strict') for argument in arguments if argument]
    except (OSError, UnicodeError):
        raise DeployError('service_command_unreadable') from None
    if expected_main not in arguments:
        raise DeployError('unexpected_service_process')
    return int(pid_text)


def verify_service(expected_release, *, attempts=20, stable_seconds=5, sleep=time.sleep):
    """Require the expected process to remain unchanged across a stability window."""
    first_pid = None
    for attempt in range(attempts):
        try:
            first_pid = service_snapshot(expected_release)
            break
        except (DeployError, OSError, subprocess.SubprocessError):
            if attempt + 1 == attempts:
                raise DeployError('service_verification_failed') from None
            sleep(1)
    sleep(stable_seconds)
    try:
        second_pid = service_snapshot(expected_release)
    except (DeployError, OSError, subprocess.SubprocessError):
        raise DeployError('service_not_stable') from None
    if second_pid != first_pid:
        raise DeployError('service_not_stable')
    return first_pid


def wait_control_ready(expected_release, expected_pid, identity, *, attempts=90, sleep=time.sleep):
    """Wait for the synchronous Matrix bootstrap without accepting a PID restart."""
    control_file = STATE / 'control/state.json'
    ready_file = STATE / 'control/ready.json'
    try:
        runtime_values = env_values(RUNTIME_ENV.read_text(encoding='utf-8').splitlines())
        control_user = runtime_values.get('MATRIX_CONTROL_USER', '')
        control_room = runtime_values.get('MATRIX_CONSOLE_ROOM_ID', '')
        production_room = runtime_values.get('MATRIX_ROOM_ID', '')
        additional = additional_control_rooms(
            runtime_values.get('MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON', '{}'),
            control_user=control_user, control_room=control_room,
            production_room=production_room)
        expected_hash = control_bindings_hash(control_user, control_room, additional)
    except (OSError, UnicodeError):
        raise DeployError('production_env_unreadable') from None
    for attempt in range(attempts):
        if service_snapshot(expected_release) != expected_pid:
            raise DeployError('service_not_stable')
        try:
            validate_protected_file(control_file, mode=0o600,
                                    uid=identity.pw_uid, gid=identity.pw_gid)
            validate_protected_file(ready_file, mode=0o600,
                                    uid=identity.pw_uid, gid=identity.pw_gid)
        except DeployError as exc:
            if str(exc) != 'protected_runtime_file_missing':
                raise
        else:
            try:
                if control_file.stat().st_size > 2_000_000 or ready_file.stat().st_size > 4096:
                    raise DeployError('control_state_invalid')
                control = json.loads(control_file.read_text(encoding='utf-8'))
                ready = json.loads(ready_file.read_text(encoding='utf-8'))
            except DeployError:
                raise
            except (OSError, UnicodeError, ValueError, RecursionError):
                raise DeployError('control_state_invalid') from None
            if (not isinstance(control, dict) or type(control.get('ai_enabled')) is not bool
                    or control.get('since') is not None and not isinstance(control.get('since'), str)
                    or not isinstance(ready, dict)
                    or set(ready) != {'version', 'pid', 'controllers_sha256'}
                    or ready.get('version') != 1 or ready.get('pid') != expected_pid
                    or ready.get('controllers_sha256') != expected_hash):
                raise DeployError('control_state_invalid')
            if isinstance(control.get('since'), str) and control['since']:
                return control
        if attempt + 1 < attempts:
            sleep(1)
    raise DeployError('control_state_not_ready')


def set_deployment_configuration(dropin, release):
    if dropin is None:
        try:
            DROPIN.unlink()
        except FileNotFoundError:
            pass
    else:
        DROPIN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic_write(DROPIN, dropin, 0o644)
    if release is None:
        remove_symlink(CURRENT)
    else:
        replace_symlink(CURRENT, release)
    run(['/usr/bin/systemctl', 'daemon-reload'])


def restore_and_verify(expected_release, restore, error_code):
    """Restore one known configuration; any doubt leaves the service stopped."""
    try:
        run(['/usr/bin/systemctl', 'stop', SERVICE], check=False)
        restore()
        run(['/usr/bin/systemctl', 'start', SERVICE])
        verify_service(expected_release)
    except Exception as exc:
        run(['/usr/bin/systemctl', 'stop', SERVICE], check=False)
        raise DeployError(error_code) from exc


def activate(confirm):
    require_root()
    if not confirm:
        raise DeployError('restart_confirmation_required')
    release, data = read_status()
    if not data['live_preflight']:
        raise DeployError('live_preflight_required')
    identity = service_identity()
    backup = backup_production(release)
    metadata = backup_metadata(backup)
    prior_release = metadata['prior_current']
    prior_was_final = prior_release is not None
    changed = False
    try:
        run(['/usr/bin/systemctl', 'stop', SERVICE])
        if service_active():
            raise DeployError('service_did_not_stop')
        if prior_was_final:
            if not (STATE / 'processed_emails.csv').is_file() or not (STATE / 'stats.csv').is_file():
                raise DeployError('final_runtime_csv_missing')
        else:
            copy_runtime_file(OLD / 'processed_emails.csv', STATE / 'processed_emails.csv', identity)
            copy_runtime_file(OLD / 'stats.csv', STATE / 'stats.csv', identity)
        replace_symlink(CURRENT, release)
        changed = True
        DROPIN_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic_write(DROPIN, dropin_text(), 0o644)
        run(['/usr/bin/systemctl', 'daemon-reload'])
        run(['/usr/bin/systemd-analyze', 'verify', str(UNIT)])
        run(['/usr/bin/systemctl', 'start', SERVICE])
        pid = verify_service(release)
        control = wait_control_ready(release, pid, identity)
        if not prior_was_final and control.get('ai_enabled') is not False:
            raise DeployError('initial_ai_state_not_off')
    except Exception as activation_error:
        def restore_previous():
            if changed and not prior_was_final:
                replace_runtime_file(STATE / 'processed_emails.csv', OLD / 'processed_emails.csv', identity)
                replace_runtime_file(STATE / 'stats.csv', OLD / 'stats.csv', identity)
            prior_dropin = backup / 'dropins/20-final.conf'
            set_deployment_configuration(
                prior_dropin.read_bytes() if prior_dropin.is_file() else None,
                prior_release)
        try:
            restore_and_verify(prior_release, restore_previous,
                               'automatic_restore_failed_service_left_stopped')
        except DeployError:
            raise
        raise activation_error
    print('Finaler Dienst aktiv; noch kein Testbefehl automatisch gesendet.')
    print('backup=' + backup.name)


def rollback(name, confirm):
    require_root()
    if not confirm:
        raise DeployError('restart_confirmation_required')
    if not re.fullmatch(r'before-final-[0-9]{8}T[0-9]{6}Z', name or ''):
        raise DeployError('invalid_backup_name')
    backup = BACKUPS / name
    metadata_path = backup / 'metadata.json'
    if not backup.is_dir() or not metadata_path.is_file():
        raise DeployError('backup_missing')
    identity = service_identity()
    metadata = backup_metadata(backup)
    activated_release = metadata['activated_release']
    prior_release = metadata['prior_current']
    current_release = installed_final_release()
    if current_release != activated_release:
        raise DeployError('rollback_lineage_mismatch')
    # Refuse before stopping or touching CSV data unless this exact final release
    # is the stable process currently serving production.
    verify_service(activated_release)
    current_dropin = DROPIN.read_bytes()
    try:
        run(['/usr/bin/systemctl', 'stop', SERVICE])
        if prior_release is None:
            replace_runtime_file(STATE / 'processed_emails.csv', OLD / 'processed_emails.csv', identity)
            replace_runtime_file(STATE / 'stats.csv', OLD / 'stats.csv', identity)
        prior_dropin = backup / 'dropins/20-final.conf'
        set_deployment_configuration(
            prior_dropin.read_bytes() if prior_dropin.is_file() else None,
            prior_release)
        run(['/usr/bin/systemctl', 'start', SERVICE])
        verify_service(prior_release)
    except Exception as rollback_error:
        def restore_current():
            set_deployment_configuration(current_dropin, activated_release)
        try:
            restore_and_verify(activated_release, restore_current,
                               'rollback_recovery_failed_service_left_stopped')
        except DeployError:
            raise
        raise rollback_error
    print('Rollback aktiv; Matrix-Nachrichten wurden nicht entfernt.')


def status():
    check_host()
    print('service_active=' + str(service_active()).lower())
    if STATUS.is_file():
        data = json.loads(STATUS.read_text())
        print('staged_release=' + str(data.get('release')))
        print('live_preflight=' + str(data.get('live_preflight')).lower())
    print('current=' + (str(CURRENT.resolve()) if CURRENT.exists() else 'not-set'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('plan')
    staged = sub.add_parser('stage')
    staged.add_argument('--confirm-data-transfer', action='store_true')
    live = sub.add_parser('live-preflight')
    live.add_argument('--confirm-data-transfer', action='store_true')
    active = sub.add_parser('activate')
    active.add_argument('--confirm-restart', action='store_true')
    sub.add_parser('status')
    rolled = sub.add_parser('rollback')
    rolled.add_argument('--backup', required=True)
    rolled.add_argument('--confirm-restart', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            plan()
        elif args.command == 'stage':
            stage(args.confirm_data_transfer)
        elif args.command == 'live-preflight':
            live_preflight(args.confirm_data_transfer)
        elif args.command == 'activate':
            activate(args.confirm_restart)
        elif args.command == 'rollback':
            rollback(args.backup, args.confirm_restart)
        else:
            status()
    except (DeployError, OSError, subprocess.SubprocessError) as exc:
        print('Abbruch: ' + (str(exc) if isinstance(exc, DeployError) else type(exc).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
