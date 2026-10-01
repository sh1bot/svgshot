from contextlib import redirect_stdout
from datetime import datetime
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

from svgshot.output import capture_path, desktop_dir
from svgshot.grab import main
from svgshot.snapshot import embed_snapshot, read_snapshot
from tests.test_schema import semantic_capture


class CaptureOutputTests(unittest.TestCase):
    def test_names_are_valid_and_collisions_preserve_existing_captures(self):
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026, 10, 1, 14, 0, 0, 123000)
            path = capture_path('New user: <test>/?', directory=folder, now=now)
            self.assertEqual(path.name, '2026-10-01_14-00-00-123 - New user_ _test___.png')
            path.write_bytes(b'existing')
            other = capture_path('New user: <test>/?', directory=folder, now=now)
            self.assertNotEqual(path, other)
            self.assertEqual(path.read_bytes(), b'existing')
            unicode_path = capture_path('🙂' * 200, directory=folder, now=now)
            self.assertLess(len(unicode_path.name.encode('utf-8')), 255)

    def test_linux_uses_configured_desktop_without_executing_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)
            (config / 'user-dirs.dirs').write_text('XDG_DESKTOP_DIR="$HOME/Desk top"\n')
            with patch('svgshot.output.sys.platform', 'linux'), patch.dict(os.environ, {'XDG_CONFIG_HOME': folder}):
                self.assertEqual(desktop_dir(), Path.home() / 'Desk top')

    def test_frontend_saves_automatic_and_explicit_png_filenames(self):
        stream = io.BytesIO()
        Image.new('RGB', (400, 200), 'white').save(stream, format='PNG')
        png = embed_snapshot(stream.getvalue(), semantic_capture())
        with tempfile.TemporaryDirectory() as folder, \
             patch('svgshot.grab.capture_bytes', return_value=png), \
             patch('svgshot.output.desktop_dir', return_value=Path(folder)), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 0)
            generated = list(Path(folder).glob('*.png'))
            self.assertEqual(len(generated), 1)
            self.assertTrue(generated[0].name.endswith(' - Window.png'))
            self.assertEqual(read_snapshot(generated[0])['root']['label'], 'Window')
            explicit = Path(folder) / 'chosen.png'
            self.assertEqual(main(['--out', str(explicit)]), 0)
            self.assertEqual(explicit.read_bytes(), png)
