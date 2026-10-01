import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from svgshot.schema import from_uia, render_view, validate
from svgshot.snapshot import embed_snapshot,read_snapshot
from svgshot.convert import main


def legacy():
    return {'version':2,'screen_bounds':[100,200,200,100],'image_size':[400,200],
      'capture_policy':{},'property_names':{'1':'IsEnabled','2':'HelpText'},'pattern_names':{},
      'root':{'id':'uia-a','control_type':50032,'bounds':[100,200,200,100],'name':'Window',
       'children':[{'id':'uia-b','control_type':50002,'bounds':[110,220,100,30],
       'name':'Keep','enabled':False,'properties':{'1':{'status':'error'},'2':{'status':'not_supported'}},'states':{'toggle':2},
       'text_ranges':[{'text':'nnn','rectangles':[[110,220,100,30]],'attributes':{'40008':{'status':'value','value':0x6a5fff}}}],
       'text_capture':{'status':'value'},'children':[]} ]}}


class UnifiedSchemaTests(unittest.TestCase):
    def test_normalization_preserves_native_and_statuses(self):
        old=legacy();s=from_uia(old);validate(s)
        self.assertEqual(s['native']['snapshot'],old)
        child=s['root']['children'][0]
        self.assertEqual(child['bounds'],[20,40,200,60])
        self.assertEqual(child['states']['checked'],'mixed')
        self.assertNotIn('enabled',child['states'])
        self.assertNotIn('help',child)
        self.assertEqual(child['field_status']['help']['status'],'not_supported')
        self.assertEqual(child['field_status']['states.enabled']['status'],'error')
        self.assertEqual(child['text']['lines'][0]['style']['foreground'],'#ff5f6a')
        self.assertEqual(render_view(s)['root']['children'][0]['states']['toggle'],2)

    def test_graph_and_geometry_validation(self):
        s=from_uia(legacy());s['root']['children'][0]['id']=s['root']['id']
        with self.assertRaises(ValueError):validate(s)
        s=from_uia(legacy());s['root']['relationships']={'labelled_by':['external']}
        with self.assertRaises(ValueError):validate(s)
        s=from_uia(legacy());s['root']['bounds'][2]=-1
        with self.assertRaises(ValueError):validate(s)

    def test_saved_and_memory_conversion_match_without_capture_files(self):
        s=from_uia(legacy())
        image=Image.new('RGB',(400,200),'white');stream=io.BytesIO();image.save(stream,format='PNG')
        png=embed_snapshot(stream.getvalue(),s)
        self.assertEqual(read_snapshot(io.BytesIO(png)),s)
        # Re-embedding replaces metadata, never accumulates multiple chunks.
        self.assertEqual(read_snapshot(io.BytesIO(embed_snapshot(png,s))),s)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);stored=root/'input.png';stored.write_bytes(png)
            with patch('svgshot.convert.capture_bytes',return_value=png) as capture:
                self.assertEqual(main([str(stored),str(root/'saved.svg'),'--no-ocr']),0)
                capture.assert_not_called()
                self.assertEqual(main(['--capture',str(root/'direct.svg'),'--no-ocr']),0)
                capture.assert_called_once()
            self.assertEqual((root/'saved.svg').read_bytes(),(root/'direct.svg').read_bytes())
            self.assertEqual(sorted(p.name for p in root.iterdir()),['direct.svg','input.png','saved.svg'])
            self.assertIn('svgshot.capture',(root/'direct.svg').read_text())
