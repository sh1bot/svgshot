"""Persistent source and editable trace reuse across placements and algorithms."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sqlite3

from PIL import Image, ImageDraw
import numpy as np

from svgshot.model import Node
from svgshot.svg import to_svg
from svgshot.tracing import trace
from svgshot.validate import render


def icon(x=4, fill=(20, 80, 220)):
    image=Image.new('RGB',(24,24),'white')
    ImageDraw.Draw(image).rectangle((x,4,x+11,19),fill=fill)
    return image


class TraceCacheTests(TestCase):
    def test_exact_fuzzy_and_offsets_retain_each_bitmap(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(), '#ffffff', cache_dir=folder)
            again=trace(icon(), '#ffffff', cache_dir=folder)
            shifted=trace(icon(5), '#ffffff', cache_dir=folder)
            self.assertEqual([first.match,again.match,shifted.match],['new','exact','bitmap'])
            self.assertEqual(first.group_id,shifted.group_id)
            self.assertEqual(shifted.offset,(1.,0.))
            self.assertNotEqual(first.source_hash,shifted.source_hash)
            bitmaps=list((Path(folder)/'groups'/first.group_id/'bitmaps').glob('*.png'))
            self.assertEqual(len(bitmaps),2)
            with sqlite3.connect(Path(folder)/'index.sqlite3') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM groups').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT dx,dy FROM bitmaps WHERE hash=?',
                                            (shifted.source_hash,)).fetchone(),(1.,0.))
            svg=to_svg(Node('window',(0,0,24,24),color='#ffffff',children=[shifted.node]))
            path=Path(folder)/'composite.svg';path.write_text(svg)
            result=np.asarray(render(str(path),24))
            self.assertGreater(result[12,5,2],150)
            self.assertTrue((result[12,4,:3]>240).all())

    def test_algorithms_have_separate_editable_svgs_and_shared_bitmap_index(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(),'#ffffff',cache_dir=folder)
            second=trace(icon(),'#ffffff',algorithm='smooth-palette',cache_dir=folder)
            self.assertEqual(first.group_id,second.group_id)
            path=next((Path(folder)/'groups'/first.group_id/'algorithms').glob('palette-*.svg'))
            self.assertEqual(len(list(path.parent.glob('*.svg'))),2)
            self.assertIn('viewBox=',path.read_text())
            path.write_text(path.read_text().replace('#1450dc','#ff0000'))
            edited=trace(icon(),'#ffffff',cache_dir=folder)
            self.assertEqual(edited.match,'exact')
            self.assertIn('#ff0000',edited.svg)
            self.assertEqual(edited.node.vector_data['paths'][0]['fill'],'#ff0000')
            self.assertNotIn('#ff0000',second.svg)

    def test_svg_similarity_links_distinct_sources_after_bitmap_check(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(),'#ffffff',cache_dir=folder)
            altered=icon(fill=(21,80,220))
            with patch('svgshot.tracing._bitmap_match',return_value=None):
                second=trace(altered,'#ffffff',cache_dir=folder)
            self.assertEqual(second.match,'svg')
            self.assertEqual(first.group_id,second.group_id)
            self.assertEqual(len(list((Path(folder)/'groups'/first.group_id/'bitmaps').glob('*.png'))),2)
            self.assertEqual(len(list((Path(folder)/'groups'/first.group_id/'algorithms').glob('*.svg'))),1)

    def test_background_and_settings_are_part_of_cache_identity(self):
        with TemporaryDirectory() as folder:
            image=icon()
            first=trace(image,'#ffffff',cache_dir=folder,palette_size=2)
            second=trace(image,'#f0f0f0',cache_dir=folder,palette_size=2)
            trace(image,'#ffffff',cache_dir=folder,palette_size=4)
            self.assertNotEqual(first.source_hash,second.source_hash)
            self.assertNotEqual(first.group_id,second.group_id)
            self.assertEqual(len(list((Path(folder)/'groups'/first.group_id/'algorithms').glob('*.svg'))),2)

    def test_shifted_variant_uses_canonical_bitmap_for_new_algorithm(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(4),'#ffffff',cache_dir=folder)
            shifted=trace(icon(5),'#ffffff',cache_dir=folder,algorithm='smooth-palette')
            self.assertEqual(shifted.match,'bitmap')
            self.assertEqual(shifted.group_id,first.group_id)
            self.assertEqual(shifted.offset,(1.,0.))
            # The new algorithm traces the first image, then places it at the
            # recorded offset for this source variant.
            self.assertEqual(len(list((Path(folder)/'groups'/first.group_id/'algorithms').glob('*.svg'))),2)
            different=trace(icon(fill=(220,20,20)),'#ffffff',cache_dir=folder)
            self.assertNotEqual(different.group_id,first.group_id)

    def test_hand_edited_svg_rect_is_inlined_without_path_conversion(self):
        with TemporaryDirectory() as folder:
            initial=trace(icon(),'#ffffff',cache_dir=folder)
            path=next((Path(folder)/'groups'/initial.group_id/'algorithms').glob('*.svg'))
            path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
                            'width="24" height="24"><rect x="3" y="4" width="12" '
                            'height="16" fill="red"/></svg>')
            result=trace(icon(),'#ffffff',cache_dir=folder)
            self.assertEqual(result.node.vector_data['paths'],[])
            svg=to_svg(Node('window',(0,0,24,24),color='#ffffff',children=[result.node]))
            output=Path(folder)/'render.svg';output.write_text(svg)
            pixels=np.asarray(render(str(output),24))
            np.testing.assert_array_equal(pixels[10,10,:3],[255,0,0])
