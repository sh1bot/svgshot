import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from svgshot.schema import render_view, validate
from svgshot.snapshot import embed_snapshot,read_snapshot
from svgshot.convert import main


def semantic_capture():
    return {'format':'svgshot.capture','version':3,
      'source':{'platform':'windows','provider':'windows-uia'},
      'image':{'size':[400,200],'coordinate_space':'image-pixels',
       'source_bounds':[100,200,200,100],'source_to_image':[2,0,0,2,-200,-400]},
      'capture_policy':{},'warnings':[],
      'root':{'id':'n1','role':'window','bounds':[0,0,400,200],'label':'Window',
       'states':{},'relationships':{},'text':{'status':'not_captured','lines':[]},
       'value':{'status':'not_captured'},'children':[
        {'id':'n2','role':'checkbox','bounds':[20,40,200,60],'label':'Keep',
         'states':{'enabled':False,'checked':'mixed'},'relationships':{},
         'text':{'status':'value','lines':[{'content':'nnn','rectangles':[[20,40,200,60]],'style':{'foreground':'#ff5f6a'}}]},
         'value':{'status':'not_captured'},'children':[]}]},
      'native':{'provider':'windows-uia','snapshot':{}}}


class UnifiedSchemaTests(unittest.TestCase):
    def test_schema_is_v3_and_renderer_view_is_derived(self):
        s=semantic_capture();validate(s)
        view=render_view(s)
        self.assertEqual(view['root']['children'][0]['states']['toggle'],2)
        self.assertEqual(view['root']['children'][0]['bounds'],[20,40,200,60])

    def test_graph_and_geometry_validation(self):
        s=semantic_capture();s['root']['children'][0]['id']=s['root']['id']
        with self.assertRaises(ValueError):validate(s)
        s=semantic_capture();s['root']['relationships']={'labelled_by':['external']}
        with self.assertRaises(ValueError):validate(s)
        s=semantic_capture();s['root']['bounds'][2]=-1
        with self.assertRaises(ValueError):validate(s)
        with self.assertRaises(ValueError):validate({'version':2})

    def test_saved_and_memory_conversion_match_without_capture_files(self):
        s=semantic_capture()
        image=Image.new('RGB',(400,200),'white');stream=io.BytesIO();image.save(stream,format='PNG')
        png=embed_snapshot(stream.getvalue(),s)
        self.assertEqual(read_snapshot(io.BytesIO(png)),s)
        # Re-embedding replaces metadata, never accumulates multiple chunks.
        self.assertEqual(read_snapshot(io.BytesIO(embed_snapshot(png,s))),s)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);stored=root/'input.png';stored.write_bytes(png)
            with patch('svgshot.convert.capture_bytes',return_value=png) as capture_helper:
                self.assertEqual(main([str(stored),str(root/'saved.svg'),'--no-ocr']),0)
                capture_helper.assert_not_called()
                self.assertEqual(main(['--capture',str(root/'direct.svg'),'--no-ocr']),0)
                capture_helper.assert_called_once()
            self.assertEqual((root/'saved.svg').read_bytes(),(root/'direct.svg').read_bytes())
            self.assertEqual(sorted(p.name for p in root.iterdir()),['direct.svg','input.png','saved.svg'])
            self.assertIn('svgshot.capture',(root/'direct.svg').read_text())
