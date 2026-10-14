import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parents[1]
    directory = (root / sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(directory))
    if os.name == 'nt' and directory == root:
        import test_service
        name = 'test_bound_token_restores_in_fresh_app_without_qr_or_network'
        method = getattr(test_service.HTTPTests, name)
        setattr(test_service.HTTPTests, name, unittest.skip(
            'Native Linux motor budget file permissions required for restart restoration')(method))
    suite = unittest.defaultTestLoader.discover(str(directory), pattern=sys.argv[2])
    adaptations = []
    if os.name == 'nt' and directory == root:
        import service
        adaptations.append('Application directory fsync simulated on Windows; Linux durability tests remain skipped')
        with patch.object(service, 'fsync_directory', return_value=None):
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    else:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  skipped=[dict(id=test.id(), test=str(test), reason=reason) for test, reason in result.skipped],
                  successful=result.wasSuccessful(), adaptations=adaptations)
    Path(sys.argv[3]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
