"""Store a semantic capture, or return it in memory for direct conversion."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .schema import validate
from .snapshot import read_snapshot, embed_snapshot
from .output import capture_path


def helper(platform, explicit=None):
    if explicit:
        return str(Path(explicit).resolve())
    override = os.environ.get('SVGSHOT_CAPTURE_HELPER')
    if override:
        return override
    name = ('svgshot-capture-win' if platform == 'win32' else
            'svgshot-capture-linux' if platform.startswith('linux') else 'svgshot-capture-macos')
    installed = shutil.which(name)
    if installed:
        return installed
    root = Path(__file__).resolve().parents[1]
    candidates = ([root/'build/capture/Release'/f'{name}.exe', root/'build/capture'/f'{name}.exe']
                  if platform == 'win32' else [root/'build/capture'/name])
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError(f'Build {name}, use --helper, or set SVGSHOT_CAPTURE_HELPER')


def capture_bytes(*, native_helper=None, hwnd=None, window=None, foreground=False, delay=0,
                  include_hidden_content=False, debug_unredacted=False,
                  accessibility_api="auto", bitmap=None, window_bounds=None):
    if not 0 <= delay <= 60:
        raise ValueError('Delay must be 0–60 seconds')
    if sys.platform == 'win32':
        if window is not None or bitmap or window_bounds:
            raise ValueError('Linux/macOS window options are not applicable on Windows')
        command = [helper(sys.platform, native_helper), '--stdout']
        if hwnd:
            command += ['--hwnd', str(hwnd)]
        if foreground:
            command += ['--foreground']
        if delay:
            command += ['--delay', str(delay)]
        if include_hidden_content:
            command += ['--include-hidden-content']
        if debug_unredacted:
            command += ['--debug-unredacted']
        if accessibility_api != 'auto':
            command += ['--accessibility-api', accessibility_api]
        result = subprocess.run(command, stdout=subprocess.PIPE, check=True, timeout=delay+60)
        png = result.stdout
        snapshot = read_snapshot(io.BytesIO(png))
    elif sys.platform == 'darwin':
        if hwnd or bitmap or window_bounds:
            raise ValueError('Windows/Linux options are not applicable on macOS')
        if include_hidden_content and not debug_unredacted:
            raise ValueError('macOS currently supports visible-content capture only; use --debug-unredacted to bypass redactions')
        command = [helper(sys.platform, native_helper), '--framed']
        if window is not None:
            command += ['--window', str(window)]
        if foreground:
            command += ['--foreground']
        if delay:
            command += ['--delay', str(delay)]
        if debug_unredacted:
            command += ['--debug-unredacted']
        if accessibility_api != 'auto':
            raise ValueError('--accessibility-api is currently available only on Windows')
        result = subprocess.run(command, stdout=subprocess.PIPE, check=True, timeout=delay+90)
        # Native Swift worker returns JSON followed by PNG, prefixed by JSON length.
        import struct
        if len(result.stdout) < 4:
            raise RuntimeError('macOS capture returned no data')
        length = struct.unpack('>I', result.stdout[:4])[0]
        if length > 64*1024*1024 or length+4 >= len(result.stdout):
            raise RuntimeError('Invalid macOS capture framing')
        snapshot = json.loads(result.stdout[4:4+length])
        png = result.stdout[4+length:]
    elif sys.platform.startswith('linux'):
        if hwnd:
            raise ValueError('Windows handle options are not applicable on Linux')
        command = [helper(sys.platform, native_helper), '--stdout']
        if window is not None:
            command += ['--window', str(window)]
        if foreground:
            command += ['--foreground']
        if delay:
            command += ['--delay', str(delay)]
        if include_hidden_content:
            command += ['--include-hidden-content']
        if debug_unredacted:
            command += ['--debug-unredacted']
        if accessibility_api != 'auto':
            raise ValueError('--accessibility-api is currently available only on Windows')
        if bitmap:
            command += ['--bitmap', str(bitmap)]
        if window_bounds:
            command += ['--window-bounds', *map(str, window_bounds)]
        png = subprocess.run(command, stdout=subprocess.PIPE, check=True, timeout=delay+60).stdout
        snapshot = read_snapshot(io.BytesIO(png))
    else:
        raise RuntimeError('Live capture supports Windows, macOS, and Linux')
    validate(snapshot)
    # Check IHDR without requiring Pillow in the capture frontend on Windows/macOS.
    import struct
    if png[:8] != b'\x89PNG\r\n\x1a\n' or png[12:16] != b'IHDR' or len(png) < 24:
        raise ValueError('Capture did not produce a PNG')
    if list(struct.unpack('>II', png[16:24])) != snapshot['image']['size']:
        raise ValueError('Capture bitmap dimensions do not match semantics')
    return embed_snapshot(png, snapshot)


def add_options(parser):
    parser.add_argument('--helper', type=Path, help='Explicit native capture helper')
    parser.add_argument('--hwnd', help='Windows window handle')
    parser.add_argument('--window', help='Linux capture-local window ID or macOS CGWindowID')
    parser.add_argument('--foreground', action='store_true')
    parser.add_argument('--delay', type=int, default=0)
    parser.add_argument('--include-hidden-content', action='store_true')
    parser.add_argument('--debug-unredacted', action='store_true',
                        help='Debug only: include content normally redacted, including passwords; may capture sensitive information')
    parser.add_argument('--accessibility-api', choices=('auto', 'uia', 'msaa'), default='auto',
                        help='Windows accessibility source (default: choose the richer UIA/MSAA tree)')
    parser.add_argument('--bitmap', type=Path, help='Linux: explicitly pair a window bitmap with the selected AT-SPI window')
    parser.add_argument('--window-bounds', type=float, nargs=4, metavar=('X','Y','W','H'),
                        help='Linux: source bounds represented by --bitmap')


def options(args):
    return dict(native_helper=args.helper, hwnd=args.hwnd, window=args.window,
                foreground=args.foreground, delay=args.delay,
                include_hidden_content=args.include_hidden_content,
                debug_unredacted=args.debug_unredacted,
                accessibility_api=args.accessibility_api,
                bitmap=args.bitmap, window_bounds=args.window_bounds)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Capture a window into a self-contained semantic PNG')
    parser.add_argument('output', nargs='?', type=Path, help='PNG filename (default: dated capture on Desktop)')
    parser.add_argument('--out', type=Path, help='PNG filename, as an alternative to the positional filename')
    parser.add_argument('--stdout', action='store_true', help='Write binary semantic PNG to stdout')
    parser.add_argument('--json', type=Path, help='Also export the unified snapshot as JSON')
    parser.add_argument('--list-windows', action='store_true')
    add_options(parser)
    args = parser.parse_args(argv)
    try:
        if args.list_windows:
            if sys.platform == 'darwin' or sys.platform.startswith('linux'):
                return subprocess.run([helper(sys.platform, args.helper), '--list-windows'], check=False).returncode
            raise ValueError('--list-windows is available on Linux/macOS')
        if args.output and args.out:
            raise ValueError('Choose a filename or --out, not both')
        args.output = args.output or args.out
        automatic = not args.output and not args.stdout
        if args.output and args.stdout:
            raise ValueError('Output PNG cannot be combined with --stdout')
        if args.output and args.output.suffix.lower() != '.png':
            raise ValueError('Capture output must have a .png extension')
        if args.json and args.output and args.json.resolve() == args.output.resolve():
            raise ValueError('JSON must not overwrite the PNG')
        png = capture_bytes(**options(args))
        if automatic:
            snapshot = read_snapshot(io.BytesIO(png))
            args.output = capture_path(snapshot['root'].get('label', ''))
        if args.json and args.output and args.json.resolve() == args.output.resolve():
            raise ValueError('JSON must not overwrite the PNG')
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a failed capture does not overwrite the previous corpus entry.
            import tempfile
            with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as f:
                temporary = Path(f.name)
                f.write(png)
            try:
                if automatic:
                    os.link(temporary, args.output)  # Never replace another capture on a naming collision.
                else:
                    os.replace(temporary, args.output)
            finally:
                temporary.unlink(missing_ok=True)
            print(args.output)
        else:
            sys.stdout.buffer.write(png)
        if args.json:
            args.json.write_text(json.dumps(read_snapshot(io.BytesIO(png)), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f'svgshot grab: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
