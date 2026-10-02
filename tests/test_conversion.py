"""Both PNG input kinds must use the same recognition and export options."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from svgshot.cli import main
from svgshot.model import Node
from svgshot.snapshot import embed_snapshot


class ConversionTests(unittest.TestCase):
    def test_shared_options_reports_and_missing_metadata_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            raw, semantic = d/'raw.png', d/'semantic.png'
            Image.new('RGB',(120,80),'white').save(raw)
            snapshot = {'format':'svgshot.capture','version':3,
                'source':{'platform':'windows','provider':'windows-uia'},
                'image':{'size':[120,80],'coordinate_space':'image-pixels'},
                'capture_policy':{},'warnings':['Saved capture note'],
                'root':{'id':'n1','role':'window','bounds':[0,0,120,80],'children':[]}}
            semantic.write_bytes(embed_snapshot(raw.read_bytes(),snapshot))
            seen = []
            def recognize(image, options):
                seen.append(vars(options).copy())
                return Node('window',(0,0,120,80),color='#ffffff')
            errors = io.StringIO()
            with patch('svgshot.cli.reconstruct', side_effect=recognize), \
                 patch('svgshot.cli.compare', return_value={'warnings':[]}), \
                 redirect_stderr(errors):
                for source in (raw,semantic):
                    self.assertEqual(main([str(source),str(source.with_suffix('.svg')), '--no-ocr',
                        '--no-raster','--report',str(source.with_suffix('.json'))]),0)
            self.assertEqual(seen[0],seen[1])
            self.assertFalse(seen[0]['ocr'])
            self.assertFalse(seen[0]['raster_fallback'])
            self.assertEqual(errors.getvalue().count('no embedded semantic data'),1)
            self.assertIn('Recorded capture warning: Saved capture note',errors.getvalue())
