import hashlib
import subprocess
import sys
from pathlib import Path


def verify_staged(root, names):
    for name in names:
        result = subprocess.run(['git', 'show', ':' + name], cwd=root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode or hashlib.sha256(result.stdout).digest() != hashlib.sha256((root / name).read_bytes()).digest():
            raise ValueError('Staged source differs from checked workspace: ' + name)


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import check
    verify_staged(root, check.source_paths(root) + ['release_index.json'])
    result = subprocess.run([sys.executable, '-B', str(root / 'check.py')], cwd=root)
    verify_staged(root, check.source_paths(root) + ['release_index.json'])
    return result.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
