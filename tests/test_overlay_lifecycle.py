import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Requires WinForms and native HWND state")
class OverlayLifecycleTests(unittest.TestCase):
    def test_background_overlay_recovery(self):
        script = Path(__file__).with_suffix(".ps1")
        with tempfile.TemporaryDirectory(prefix="q-console-overlay-test-") as home:
            result = subprocess.run(
                ["powershell.exe", "-STA", "-NoProfile", "-ExecutionPolicy",
                 "Bypass", "-File", str(script), "-AppHome", home],
                capture_output=True, text=True, errors="replace", timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)
