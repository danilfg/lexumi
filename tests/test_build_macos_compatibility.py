"""Regression checks for the minimum supported macOS version.

These tests run on Linux CI too; the real Mach-O audit runs on the macOS
release runner after PyInstaller has assembled the application bundle.
"""
from pathlib import Path
import plistlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from build_macos import (
    MINIMUM_MACOS_VERSION,
    _macos_minimum_versions_from_otool,
    configure_info_plist,
    verify_macos_compatibility,
)


def load_command(minimum):
    return (
        "Load command 3\n"
        "      cmd LC_BUILD_VERSION\n"
        "  cmdsize 32\n"
        " platform MACOS\n"
        f"    minos {minimum}\n"
        "      sdk 15.0\n"
    )


class MacOSCompatibilityTests(unittest.TestCase):
    def test_target_is_ventura(self):
        self.assertEqual(MINIMUM_MACOS_VERSION, '13.0')

    def test_info_plist_declares_macos_13(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'Lexumi.app'
            contents = app / 'Contents'
            contents.mkdir(parents=True)
            info = contents / 'Info.plist'
            with info.open('wb') as output:
                plistlib.dump({'CFBundleIdentifier': 'tech.itroadmaps.lexumi'}, output)
            configure_info_plist(app)
            with info.open('rb') as source:
                data = plistlib.load(source)
            self.assertEqual(data['LSMinimumSystemVersion'], '13.0')

    def test_parser_reads_modern_load_command(self):
        self.assertEqual(
            _macos_minimum_versions_from_otool(load_command('13.0.1')),
            [(13, 0, 1)],
        )

    def test_parser_reads_legacy_load_command(self):
        commands = (
            "Load command 2\n"
            "      cmd LC_VERSION_MIN_MACOSX\n"
            "  cmdsize 16\n"
            "  version 10.15\n"
            "      sdk 11.0\n"
        )
        self.assertEqual(_macos_minimum_versions_from_otool(commands), [(10, 15)])

    def test_parser_ignores_unrelated_sdk_version(self):
        self.assertEqual(_macos_minimum_versions_from_otool("Load command 0\n  cmd LC_ID_DYLIB\n  version 25.0\n"), [])

    def test_compatibility_audit_accepts_macos_13_binary(self):
        self._audit_binary('13.0', should_pass=True)

    def test_compatibility_audit_rejects_macos_14_binary(self):
        self._audit_binary('14.0', should_pass=False)

    def _audit_binary(self, minimum, should_pass):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            (app / 'test.dylib').write_bytes(b'test')
            def fake_run(command, **_kwargs):
                if command[0] == '/usr/bin/file':
                    return SimpleNamespace(stdout='Mach-O 64-bit arm64', stderr='', returncode=0)
                return SimpleNamespace(stdout=load_command(minimum), stderr='', returncode=0)
            with patch('build_macos.subprocess.run', side_effect=fake_run):
                if should_pass:
                    verify_macos_compatibility(app)
                else:
                    with self.assertRaisesRegex(SystemExit, 'requires macOS 14.0'):
                        verify_macos_compatibility(app)


if __name__ == '__main__':
    unittest.main()
