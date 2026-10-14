import subprocess
import unittest
from pathlib import Path


class ObserverUiTests(unittest.TestCase):
    def test_real_ui_script_with_offline_dom_and_fetch(self):
        subprocess.run(['node', str(Path(__file__).with_name('test_ui.cjs'))], check=True, timeout=30)
