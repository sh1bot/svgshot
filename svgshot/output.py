"""Desktop capture names, using each platform's configured Desktop location."""
from datetime import datetime
import os
from pathlib import Path
import shlex
import sys


def desktop_dir():
    if sys.platform == 'win32':
        import ctypes
        import uuid
        folder = ctypes.c_void_p()
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(
            uuid.UUID('B4BFCC3A-DB2C-424C-B029-7FE99A87C641').bytes_le)
        get = ctypes.windll.shell32.SHGetKnownFolderPath
        get.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p)]
        get.restype = ctypes.c_long
        hr = get(ctypes.byref(guid), 0x8000, None, ctypes.byref(folder))
        if hr < 0:
            raise OSError(f'Cannot locate Desktop folder (0x{hr & 0xffffffff:08X})')
        try:
            return Path(ctypes.wstring_at(folder.value))
        finally:
            free = ctypes.windll.ole32.CoTaskMemFree
            free.argtypes = [ctypes.c_void_p]
            free(folder)
    home = Path.home()
    if sys.platform.startswith('linux'):
        config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'user-dirs.dirs'
        try:
            for line in config.read_text(encoding='utf-8').splitlines():
                key, separator, value = line.strip().partition('=')
                if separator and key.strip() == 'XDG_DESKTOP_DIR':
                    parsed = shlex.split(value, comments=True)
                    if len(parsed) == 1:
                        path = Path(parsed[0].replace('${HOME}', str(home)).replace('$HOME', str(home)))
                        if path.is_absolute():
                            return path
        except (FileNotFoundError, ValueError):
            pass
    return home / 'Desktop'


def capture_path(title, *, directory=None, now=None):
    desktop = Path(directory) if directory is not None else desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    title = str(title or '')[:100]
    title = ''.join('_' if ord(c) < 32 or c in '<>:"/\\|?*' else c for c in title)
    title = title.encode('utf-8')[:180].decode('utf-8', errors='ignore').rstrip('. ') or 'Window'
    stamp = (now or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S-%f')[:-3]
    stem = f'{stamp} - {title}'
    output = desktop / f'{stem}.png'
    suffix = 2
    while output.exists():
        output = desktop / f'{stem} ({suffix}).png'
        suffix += 1
    return output
