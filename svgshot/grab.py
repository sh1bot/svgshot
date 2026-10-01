"""Store a semantic capture, or return it in memory for direct conversion."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .schema import from_uia, validate
from .snapshot import read_snapshot, embed_snapshot


def helper(platform, explicit=None):
    if explicit:
        return str(Path(explicit).resolve())
    override = os.environ.get('SVGSHOT_CAPTURE_HELPER')
    if override:
        return override
    name = 'svgshot-capture-win' if platform == 'win32' else 'svgshot-capture-macos'
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
                  include_hidden_content=False, bitmap=None, window_bounds=None):
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
        result = subprocess.run(command, stdout=subprocess.PIPE, check=True, timeout=delay+60)
        png = result.stdout
        snapshot = from_uia(read_snapshot(io.BytesIO(png)))
    elif sys.platform == 'darwin':
        if hwnd or bitmap or window_bounds:
            raise ValueError('Windows/Linux options are not applicable on macOS')
        if include_hidden_content:
            raise ValueError('macOS currently supports visible-content capture only')
        command = [helper(sys.platform, native_helper), '--framed']
        if window is not None:
            command += ['--window', str(window)]
        if foreground:
            command += ['--foreground']
        if delay:
            command += ['--delay', str(delay)]
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
        if hwnd or native_helper:
            raise ValueError('Windows helper options are not applicable on Linux')
        from .linux_capture import capture
        if delay:
            time.sleep(delay)
        png, snapshot = capture(window=window, foreground=foreground,
                                include_hidden=include_hidden_content, bitmap=bitmap,
                                window_bounds=window_bounds)
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
    parser.add_argument('--helper', type=Path, help='Explicit native Windows/macOS helper')
    parser.add_argument('--hwnd', help='Windows window handle')
    parser.add_argument('--window', help='Linux capture-local window ID or macOS CGWindowID')
    parser.add_argument('--foreground', action='store_true')
    parser.add_argument('--delay', type=int, default=0)
    parser.add_argument('--include-hidden-content', action='store_true')
    parser.add_argument('--bitmap', type=Path, help='Linux: explicitly pair a window bitmap with the selected AT-SPI window')
    parser.add_argument('--window-bounds', type=float, nargs=4, metavar=('X','Y','W','H'),
                        help='Linux: source bounds represented by --bitmap')


def options(args):
    return dict(native_helper=args.helper, hwnd=args.hwnd, window=args.window,
                foreground=args.foreground, delay=args.delay,
                include_hidden_content=args.include_hidden_content,
                bitmap=args.bitmap, window_bounds=args.window_bounds)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Capture a window into a self-contained semantic PNG')
    parser.add_argument('output', nargs='?', type=Path, help='PNG capture to store')
    parser.add_argument('--stdout', action='store_true', help='Write binary semantic PNG to stdout')
    parser.add_argument('--json', type=Path, help='Also export the unified snapshot as JSON')
    parser.add_argument('--list-windows', action='store_true')
    add_options(parser)
    args = parser.parse_args(argv)
    try:
        if args.list_windows:
            if sys.platform == 'darwin':
                return subprocess.run([helper(sys.platform, args.helper), '--list-windows'], check=False).returncode
            if not sys.platform.startswith('linux'):
                raise ValueError('--list-windows is available on Linux/macOS')
            from .linux_capture import list_windows
            print(json.dumps(list_windows(), ensure_ascii=False, indent=2))
            return 0
        if bool(args.output) == bool(args.stdout):
            raise ValueError('Choose an output PNG or --stdout')
        if args.output and args.output.suffix.lower() != '.png':
            raise ValueError('Capture output must have a .png extension')
        if args.json and args.output and args.json.resolve() == args.output.resolve():
            raise ValueError('JSON must not overwrite the PNG')
        png = capture_bytes(**options(args))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a failed capture does not overwrite the previous corpus entry.
            import tempfile
            with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as f:
                temporary = Path(f.name)
                f.write(png)
            try:
                os.replace(temporary, args.output)
            finally:
                temporary.unlink(missing_ok=True)
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
