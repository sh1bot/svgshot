"""Icon fidelity, transparency, shared recognition routes, and SVG reuse."""
import tempfile
from pathlib import Path
import unittest
from xml.etree import ElementTree as ET
from PIL import Image, ImageDraw
import numpy as np
from svgshot.artwork import trace_artwork
from svgshot.icons import simplify_icon, simplify_scene_artwork
from svgshot.model import Node
from svgshot.svg import to_svg
from svgshot.validate import render


def render_svg(svg, width):
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'icon.svg'
        path.write_text(svg)
        return np.asarray(render(str(path),width))


class IconTests(unittest.TestCase):
    def test_semitransparent_colour_keeps_opacity(self):
        image=Image.new('RGBA',(24,24),(20,80,220,0))
        ImageDraw.Draw(image).rectangle((5,5,18,18),fill=(20,80,220,128))
        icon=simplify_icon(image,(0,0,24,24))
        self.assertEqual(icon.kind,'capture-artwork')
        self.assertAlmostEqual(icon.vector_data['paths'][0]['opacity'],.502,places=3)
        result=render_svg(to_svg(Node('window',(0,0,24,24),color='#ffffff',children=[icon])),24)
        np.testing.assert_allclose(result[12,12,:3],[137,167,237],atol=3)

    def test_no_raster_still_attempts_vectors_and_discards_failed_candidates(self):
        image=Image.new('RGB',(32,32),'white')
        ImageDraw.Draw(image).polygon(((4,4),(25,16),(4,28)),fill='black')
        scene=Node('window',(0,0,32,32),children=[Node('raster',(0,0,32,32))])
        simplify_scene_artwork(scene,image,allow_raster=False)
        self.assertEqual(scene.children[0].kind,'capture-artwork')
        texture=Image.fromarray(np.random.default_rng(8).integers(0,256,(32,32,3),dtype=np.uint8))
        scene=Node('window',(0,0,32,32),children=[Node('raster',(0,0,32,32))])
        simplify_scene_artwork(scene,texture,allow_raster=False)
        self.assertEqual(scene.children,[])

    def test_monochrome_diagonal_is_compact_and_keeps_its_hole(self):
        image=Image.new('RGB',(40,40),'white')
        draw=ImageDraw.Draw(image)
        draw.ellipse((7,7,29,29),outline='#222222',width=3)
        icon=simplify_icon(image,(3,3,34,34))
        self.assertEqual(icon.kind,'capture-artwork')
        self.assertEqual(icon.vector_data['approximation'],'monochrome')
        svg=to_svg(Node('window',(0,0,40,40),color='#ffffff',children=[icon]))
        self.assertLess(len(svg),1800)
        result=render_svg(svg,40)
        self.assertTrue((result[18,18,:3]>240).all(),'ring hole was filled')
        self.assertTrue((result[7:10,15:22,:3]<100).any(),'ring disappeared')

    def test_rgba_colour_icon_ignores_hidden_rgb_and_retains_transparency(self):
        image=Image.new('RGBA',(32,32),(255,0,255,0))
        draw=ImageDraw.Draw(image)
        draw.rectangle((5,5,15,25),fill=(20,80,220,255))
        draw.rectangle((16,5,26,25),fill=(220,60,20,255))
        icon=simplify_icon(image,(0,0,32,32))
        self.assertEqual(icon.kind,'capture-artwork')
        self.assertEqual(icon.vector_data['approximation'],'colour')
        self.assertNotIn('#ff00ff',str(icon.vector_data['paths']))
        output=render_svg(to_svg(Node('window',(0,0,32,32),color='#00ff00',children=[icon])),32)
        self.assertGreater(output[1,1,1],240)
        self.assertGreater(output[10,10,2],150)
        self.assertGreater(output[10,22,0],150)

    def test_repeated_icons_share_definition_and_distinct_icons_do_not(self):
        image=Image.new('RGB',(32,32),'white')
        ImageDraw.Draw(image).polygon(((5,5),(25,16),(5,27)),fill='black')
        a=simplify_icon(image,(0,0,32,32))
        b=Node(a.kind,(40,0,32,32),vector_data=a.vector_data.copy())
        image=image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        c=simplify_icon(image,(0,0,32,32));c.box=(80,0,32,32)
        svg=to_svg(Node('window',(0,0,120,32),color='#ffffff',children=[a,b,c]))
        root=ET.fromstring(svg);ns={'s':'http://www.w3.org/2000/svg'}
        self.assertEqual(len(root.findall('.//s:use',ns)),2)
        self.assertEqual(len(root.findall('./s:defs/s:g',ns)),1)
        self.assertLess(len(svg),1100)
        result=render_svg(svg,120)
        self.assertTrue((result[16,8,:3]<100).all())
        self.assertTrue((result[16,48,:3]<100).all())
        self.assertTrue((result[16,103,:3]<100).all())

    def test_pixel_and_semantic_artwork_use_the_same_approximation(self):
        image=Image.new('RGB',(32,32),'white')
        ImageDraw.Draw(image).polygon(((4,4),(25,16),(4,28)),fill='black')
        traced=trace_artwork(image,(0,0,32,32))
        raster=Node('raster',(0,0,32,32),image_data='unused')
        scene=Node('window',(0,0,32,32),children=[traced,raster])
        simplify_scene_artwork(scene,image)
        self.assertEqual(scene.children[0].vector_data,scene.children[1].vector_data)
        self.assertTrue(all(n.kind=='capture-artwork' for n in scene.children))

    def test_texture_falls_back_and_no_raster_respects_existing_vector(self):
        image=Image.fromarray(np.random.default_rng(8).integers(0,256,(32,32,3),dtype=np.uint8))
        self.assertEqual(simplify_icon(image,(0,0,32,32)).kind,'raster')
        self.assertIsNone(simplify_icon(image,(0,0,32,32),allow_raster=False))
        vector=trace_artwork(image,(0,0,32,32))
        scene=Node('window',(0,0,32,32),children=[vector])
        simplify_scene_artwork(scene,image,allow_raster=False)
        self.assertIs(scene.children[0],vector)
