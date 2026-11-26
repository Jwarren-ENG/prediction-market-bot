import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import notices


class ReleaseNoticesTests(unittest.TestCase):
    def test_isolated_scripts_and_locked_package_inputs(self):
        for command in (["package.py", "--help"], ["notices.py"]):
            with self.subTest(command=command):
                subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(notices.ROOT / "scripts" / command[0]),
                        *command[1:],
                    ],
                    cwd=tempfile.gettempdir(),
                    env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )

    def test_installed_microsoft_terms_and_compiler_notices(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            compiler = root / "COPYRIGHT-library.html"
            compiler.write_text("Test Rust toolchain notices", encoding="utf-8")
            for platform in ("mac", "windows"):
                destination = root / platform / "notices"
                notices.install(destination, compiler, windows=platform == "windows")
                self.assertEqual(
                    compiler.read_bytes(),
                    (destination / "RUST-STANDARD-LIBRARY.html").read_bytes(),
                )
                self.assertEqual(
                    notices.DOCUMENT.read_bytes(),
                    (destination / "THIRD_PARTY.md").read_bytes(),
                )
                terms = destination / "WINDOWS-COMPONENT-TERMS.txt"
                self.assertEqual(terms.is_file(), platform == "windows")
            terms = terms.read_text(encoding="utf-16")
            for text in [
                "MICROSOFT VISUAL C++",
                "MICROSOFT EDGE WEBVIEW2 RUNTIME",
                "Additional Conditions for this Windows binary build",
                "Microsoft Defender SmartScreen",
                "/ACCEPTCOMPONENTTERMS",
            ]:
                self.assertIn(text, terms)
