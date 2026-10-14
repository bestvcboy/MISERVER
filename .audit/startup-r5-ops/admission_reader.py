import hashlib
import json
import os
import re
import stat
import sys
import time
from enum import Enum
from pathlib import Path


ROOT = Path('/home/bestvcboy/dgx-thermal')
TOOLS = Path('/home/bestvcboy/dgx-thermal-release-tools')
ADMISSION = TOOLS / 'admission.json'
RELEASE = 'startup-repair-v14-r5'
STOPPED_BASELINE_RELEASE = 'cold-release-v14-r4'
MAX_JSON = 16384
MAX_FILES = 27
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
CHECK_SECONDS = 10
NAME = r'(?:core\.py|learning\.py|service\.py|qr_connector\.py|ui\.js|ui\.html|ui\.css|README\.md|test_[a-z0-9_]{1,80}\.py)'
DEPENDENCIES = {
    'qr_connector.py': '98dd6e5b74eaba733db12d24126655cbf802e3af5e3419d3a7fe85f1a91eb363',
    'ui.css': '3934c1146112c1ce18647a2d02a4357063914d727a066d9e1eb8aa86001e8d98',
}
REQUIRED = {'core.py', 'learning.py', 'service.py', 'ui.js', 'ui.html', 'README.md'} | set(DEPENDENCIES)


class AdmissionState(Enum):
    BLOCKED = 'blocked'
    CANDIDATE = 'candidate'
    ACCEPTED = 'accepted'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, 'Duplicate JSON key')
        result[name] = value
    return result


def safe_parents(path):
    require(path.is_absolute(), 'Expected absolute path')
    for parent in reversed(path.parents):
        require(not parent.is_symlink() and parent.is_dir(), 'Unsafe parent directory')
        require(parent.resolve() == parent, 'Unsafe resolved parent')


def read_regular(path, limit, deadline=None):
    safe_parents(path)
    require(not path.is_symlink(), 'Unsafe symlink')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'Unsafe or unbounded regular file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = bytearray()
            while True:
                if deadline is not None:
                    require(time.monotonic() < deadline, 'Admission validation budget reached')
                block = stream.read(min(65536, limit + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                require(len(data) <= limit, 'Unbounded regular file')
            require(len(data) == info.st_size, 'File changed during read')
            after = os.fstat(descriptor)
            require((info.st_size, info.st_mtime_ns) == (after.st_size, after.st_mtime_ns), 'File changed during read')
            return bytes(data)
    finally:
        os.close(descriptor)


def parse_record(data, *, stopped_upgrade=False):
    require(len(data) <= MAX_JSON, 'Unbounded admission record')
    record = json.loads(data.decode('utf-8'), object_pairs_hook=unique_object)
    require(type(record) is dict and set(record) == {'version', 'state', 'release', 'files'}, 'Invalid admission record')
    require(type(record['version']) is int and record['version'] == 1, 'Invalid admission version')
    state = AdmissionState(record['state'])
    require(record['release'] == RELEASE or (
        stopped_upgrade and record['release'] == STOPPED_BASELINE_RELEASE
        and state == AdmissionState.BLOCKED and record['files'] == {}), 'Invalid admission release')
    files = record['files']
    require(type(files) is dict and len(files) <= MAX_FILES, 'Invalid admission file count')
    for name, digest in files.items():
        require(type(name) is str and re.fullmatch(NAME, name), 'Invalid closure filename')
        require(type(digest) is str and re.fullmatch(r'[0-9a-f]{64}', digest), 'Invalid closure SHA256')
    if state != AdmissionState.BLOCKED:
        require(len(files) == 27, 'Incomplete runtime closure count')
        require(REQUIRED <= files.keys(), 'Incomplete runtime closure')
        require(all(files.get(name) == digest for name, digest in DEPENDENCIES.items()), 'Wrong unchanged dependency pin')
    return record


def validate_closure(root, files, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + CHECK_SECONDS
    safe_parents(root / 'core.py')
    require(not root.is_symlink() and root.is_dir(), 'Unsafe runtime directory')
    total = 0
    for name, digest in files.items():
        require(time.monotonic() < deadline, 'Admission validation budget reached')
        data = read_regular(root / name, MAX_FILE, deadline)
        total += len(data)
        require(total <= MAX_TOTAL, 'Unbounded runtime closure')
        require(hashlib.sha256(data).hexdigest() == digest, 'Runtime closure hash mismatch')
    require(time.monotonic() < deadline, 'Admission validation budget reached')


def check_admission(root=ROOT, admission=ADMISSION):
    deadline = time.monotonic() + CHECK_SECONDS
    safe_parents(admission)
    require(not admission.is_symlink(), 'Unsafe admission path')
    require(not permission_bits(admission.parent) & 0o022, 'Writable tools directory')
    data = read_regular(admission, MAX_JSON, deadline)
    require(not permission_bits(admission) & 0o077, 'Unsafe admission permissions')
    record = parse_record(data)
    require(AdmissionState(record['state']) != AdmissionState.BLOCKED, 'Start admission blocked')
    validate_closure(root, record['files'], deadline)
    return record


def permission_bits(path):
    return path.stat().st_mode & 0o777


def main():
    try:
        check_admission()
    except BaseException:
        sys.stderr.write('Forward start admission denied.\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
