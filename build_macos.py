"""Build a standalone Apple Silicon Lexumi.app and optional branded DMG release artifact."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

from lexumi import __version__ as VERSION

BUNDLE_ID = 'tech.itroadmaps.lexumi'
MINIMUM_MACOS_VERSION = '13.0'
DJVU_TOOLS = ('ddjvu', 'djvused', 'djvutxt', 'djvudump')


def run(*args, **kwargs):
    print('+', ' '.join(map(str, args)))
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def make_icon(folder):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter, QColor, QPen, QPainterPath

    iconset = folder / 'Lexumi.iconset'
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            n = size * scale
            image = QImage(n, n, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            p = QPainter(image)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.scale(n / 1024, n / 1024)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor('#2d694f'))
            p.drawRoundedRect(QRectF(32, 32, 960, 960), 210, 210)
            p.setBrush(QColor('#fff7df'))
            left = QPainterPath()
            left.moveTo(220, 290); left.cubicTo(330, 255, 420, 280, 496, 337)
            left.lineTo(496, 763); left.cubicTo(410, 706, 320, 680, 220, 713); left.closeSubpath()
            p.drawPath(left)
            right = QPainterPath()
            right.moveTo(528, 337); right.cubicTo(615, 280, 705, 255, 804, 290)
            right.lineTo(804, 713); right.cubicTo(697, 680, 610, 706, 528, 763); right.closeSubpath()
            p.drawPath(right)
            p.setPen(QPen(QColor('#b6c7ae'), 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for y in (420, 485, 550):
                p.drawLine(285, y, 425, y + 24)
                p.drawLine(590, y + 24, 738, y)
            p.end()
            suffix = '@2x' if scale == 2 else ''
            image.save(str(iconset / f'icon_{size}x{size}{suffix}.png'))
    icon = folder / 'Lexumi.icns'
    run('/usr/bin/iconutil', '-c', 'icns', iconset, '-o', icon)
    return icon


def locate_djvu_tool(name):
    candidates = [Path('/opt/homebrew/bin') / name, Path('/usr/local/bin') / name]
    portable_dir = os.environ.get('LEXUMI_DJVU_BIN_DIR', '').strip()
    if portable_dir:
        candidates.insert(0, Path(portable_dir) / name)
    which = shutil.which(name)
    if which:
        candidates.insert(0, Path(which))
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise SystemExit(f'Missing {name}. Install DjVuLibre first: brew install djvulibre')


def pyinstaller_command(root, temp, icon):
    cmd = [
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
        '--name', 'Lexumi', '--target-architecture', 'arm64',
        '--osx-bundle-identifier', BUNDLE_ID, '--icon', str(icon),
        '--distpath', str(temp / 'dist'), '--workpath', str(temp / 'work'),
        '--specpath', str(temp), '--paths', str(root),
        '--add-data', f'{root / "README.md"}:documentation',
        '--add-data', f'{root / "LICENSE"}:documentation',
        '--add-data', f'{root / "THIRD_PARTY.md"}:documentation',
    ]
    # PyInstaller treats these as real binaries and performs dependency analysis,
    # so Homebrew dylibs used by DjVuLibre are copied into the app bundle as well.
    for tool in DJVU_TOOLS:
        cmd += ['--add-binary', f'{locate_djvu_tool(tool)}:djvu/bin']
    cmd.append(str(root / 'main.py'))
    return cmd


def configure_info_plist(app):
    info = app / 'Contents/Info.plist'
    with info.open('rb') as f:
        data = plistlib.load(f)
    extensions = [
        'pdf', 'djvu', 'djv', 'epub', 'fb2', 'mobi', 'prc', 'xps', 'oxps', 'cbz', 'cbr',
        'txt', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'gif', 'svg'
    ]
    data.update(
        CFBundleDisplayName='Lexumi',
        CFBundleName='Lexumi',
        CFBundleShortVersionString=VERSION,
        CFBundleVersion='2',
        NSHighResolutionCapable=True,
        LSMinimumSystemVersion=MINIMUM_MACOS_VERSION,
        CFBundleDocumentTypes=[dict(
            CFBundleTypeName='Ebook or document',
            CFBundleTypeRole='Viewer',
            LSHandlerRank='Alternate',
            CFBundleTypeExtensions=extensions,
        )],
    )
    with info.open('wb') as f:
        plistlib.dump(data, f)


def verify_standalone(app):
    """Fail the release build if any Mach-O still points into a Homebrew prefix."""
    bad = []
    disallowed_prefixes = ['/opt/homebrew/', '/usr/local/']
    portable_dir = os.environ.get('LEXUMI_DJVU_BIN_DIR', '').strip()
    if portable_dir:
        disallowed_prefixes.append(str(Path(portable_dir).resolve().parent) + '/')
    for path in app.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        probe = subprocess.run(['/usr/bin/file', str(path)], capture_output=True, text=True)
        if 'Mach-O' not in probe.stdout:
            continue
        deps = subprocess.run(['/usr/bin/otool', '-L', str(path)], capture_output=True, text=True, check=False)
        for line in deps.stdout.splitlines()[1:]:
            dep = line.strip().split(' ', 1)[0]
            if any(dep.startswith(prefix) for prefix in disallowed_prefixes):
                bad.append((path.relative_to(app), dep))
    if bad:
        lines = '\n'.join(f'  {binary}: {dep}' for binary, dep in bad[:40])
        raise SystemExit('The app is not standalone; unresolved Homebrew dependencies remain:\n' + lines)


def _macos_minimum_versions_from_otool(load_commands):
    """Parse deployment targets from arm64 Mach-O load commands."""
    versions = []
    current_command = None
    for raw_line in load_commands.splitlines():
        line = raw_line.strip()
        if line.startswith('Load command '):
            current_command = None
        elif line.startswith('cmd '):
            current_command = line[4:]
        elif current_command in ('LC_BUILD_VERSION', 'LC_VERSION_MIN_MACOSX'):
            field = 'minos' if current_command == 'LC_BUILD_VERSION' else 'version'
            match = re.fullmatch(rf'{field} (\d+(?:\.\d+){{1,2}})', line)
            if match:
                versions.append(tuple(int(part) for part in match.group(1).split('.')))
    return versions


def verify_macos_compatibility(app):
    """Reject bundles with arm64 binaries requiring a newer macOS than advertised.

    Info.plist alone is not enough: PyInstaller, Qt, Python and DjVuLibre
    ship compiled libraries that may have their own newer deployment targets.
    """
    target = tuple(int(part) for part in MINIMUM_MACOS_VERSION.split('.'))
    incompatible = []
    checked = 0
    for path in app.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        probe = subprocess.run(['/usr/bin/file', str(path)], capture_output=True, text=True)
        if 'Mach-O' not in probe.stdout:
            continue
        checked += 1
        commands = subprocess.run(
            ['/usr/bin/otool', '-arch', 'arm64', '-l', str(path)],
            capture_output=True, text=True,
        )
        relative_path = path.relative_to(app)
        if commands.returncode != 0:
            incompatible.append(f'{relative_path}: cannot inspect arm64 slice: {commands.stderr.strip()}')
            continue
        versions = _macos_minimum_versions_from_otool(commands.stdout)
        if not versions:
            incompatible.append(f'{relative_path}: missing macOS deployment target')
        elif max(versions) > target:
            version = '.'.join(map(str, max(versions)))
            incompatible.append(f'{relative_path}: requires macOS {version}')
    if checked == 0:
        raise SystemExit('No Mach-O binaries found in application bundle')
    if incompatible:
        details = '\n'.join(f'  {line}' for line in incompatible[:40])
        raise SystemExit(
            f'Cannot advertise macOS {MINIMUM_MACOS_VERSION}: incompatible bundled binaries:\n'
            + details
            + ('\n  ...' if len(incompatible) > 40 else '')
        )
    print(f'Verified {checked} Mach-O binaries for macOS {MINIMUM_MACOS_VERSION} on arm64')


def sign_app(app):
    identity = os.environ.get('LEXUMI_CODESIGN_IDENTITY', '').strip()
    if identity:
        run('/usr/bin/codesign', '--force', '--deep', '--options', 'runtime', '--timestamp', '--sign', identity, app)
    else:
        # Ad-hoc signing keeps local builds runnable, but does not replace Developer ID + notarization.
        run('/usr/bin/codesign', '--force', '--deep', '--sign', '-', app)
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', '--verbose=2', app)


def create_dmg(app, output_dir, volume_icon):
    """Create a Finder-friendly drag-to-Applications DMG with Lexumi branding."""
    dmg = output_dir / 'Lexumi-macOS-arm64.dmg'
    dmg.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix='lexumi-dmg-') as temp_name:
        temp = Path(temp_name)
        settings = temp / 'dmg_settings.py'
        settings.write_text(
            '\n'.join([
                f'application = {str(app)!r}',
                f'volume_icon = {str(volume_icon)!r}',
                "files = [application]",
                "symlinks = {'Applications': '/Applications'}",
                "icon = volume_icon",
                "icon_locations = {'Lexumi.app': (150, 175), 'Applications': (490, 175)}",
                "background = 'builtin-arrow'",
                "window_rect = ((120, 120), (640, 360))",
                "default_view = 'icon-view'",
                "show_status_bar = False",
                "show_tab_view = False",
                "show_toolbar = False",
                "show_pathbar = False",
                "show_sidebar = False",
                "show_icon_preview = False",
                "include_icon_view_settings = True",
                "arrange_by = None",
                "label_pos = 'bottom'",
                "text_size = 14",
                "icon_size = 112",
                "format = 'UDZO'",
            ]) + '\n',
            encoding='utf-8',
        )
        run(
            sys.executable, '-m', 'dmgbuild',
            '-s', settings,
            'Lexumi',
            dmg,
        )
    return dmg


def notarize_if_configured(dmg):
    apple_id = os.environ.get('APPLE_ID')
    team_id = os.environ.get('APPLE_TEAM_ID')
    password = os.environ.get('APPLE_APP_PASSWORD')
    if not all((apple_id, team_id, password)):
        return False
    run('/usr/bin/xcrun', 'notarytool', 'submit', dmg, '--apple-id', apple_id,
        '--team-id', team_id, '--password', password, '--wait')
    run('/usr/bin/xcrun', 'stapler', 'staple', dmg)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='dist', help='Output directory (default: dist)')
    parser.add_argument('--dmg', action='store_true', help='Also create a drag-to-Applications DMG')
    args = parser.parse_args()

    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Build Lexumi on an Apple Silicon macOS runner without Rosetta.')

    # Propagate the target to build tools that compile or relink native code.
    os.environ['MACOSX_DEPLOYMENT_TARGET'] = MINIMUM_MACOS_VERSION

    root = Path(__file__).resolve().parent
    output_dir = (root / args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix='lexumi-build-') as tmp:
        temp = Path(tmp)
        icon = make_icon(temp)
        run(*pyinstaller_command(root, temp, icon), cwd=root)
        app = temp / 'dist/Lexumi.app'
        configure_info_plist(app)
        verify_standalone(app)
        verify_macos_compatibility(app)
        sign_app(app)

        target = output_dir / 'Lexumi.app'
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(app, target, symlinks=True)
        print('Created:', target)

        if args.dmg:
            dmg = create_dmg(target, output_dir, icon)
            notarized = notarize_if_configured(dmg)
            print('Created:', dmg)
            print('Notarized:' if notarized else 'Not notarized (Developer ID secrets were not configured):', dmg)


if __name__ == '__main__':
    main()
