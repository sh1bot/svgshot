"""Persistent source and editable trace reuse across placements and algorithms."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sqlite3
from html.parser import HTMLParser

from PIL import Image, ImageDraw
import numpy as np

from svgshot.model import Node
from svgshot.svg import to_svg
from svgshot.tracing import trace, TraceCache, _bitmap_match, _fingerprint, _fingerprint_might_match
from svgshot.tracing_core import _target, _icon_palette, _smooth_quantize
from svgshot.validate import render


def icon(x=4, fill=(20, 80, 220)):
    image=Image.new('RGB',(24,24),'white')
    ImageDraw.Draw(image).rectangle((x,4,x+11,19),fill=fill)
    return image


class TraceCacheTests(TestCase):
    def test_review_pages_follow_all_variants_names_and_algorithms(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(),'#ffffff',cache_dir=folder,name='Blue <gear>')
            self.assertTrue(first.review_path.is_file())
            shifted=trace(icon(5),'#ffffff',cache_dir=folder,name='Settings & Tools')
            alternate=trace(icon(),'#ffffff',cache_dir=folder,
                            algorithm='smooth-palette',name='Another name')
            for result in (first,alternate):
                page=result.review_path.read_text()
                self.assertEqual(result.review_path, result.svg_path.with_suffix('.html'))
                self.assertIn('Blue &lt;gear&gt;',page)
                self.assertIn('Settings &amp; Tools',page)
                self.assertIn('Another name',page)
                self.assertIn(f'../bitmaps/{first.source_hash}.png',page)
                self.assertIn(f'../bitmaps/{shifted.source_hash}.png',page)
                self.assertIn('width="384" height="384"',page)
                self.assertIn('image-rendering:pixelated',page)
                self.assertIn('<dt>Offset</dt><dd>(1, 0) pixels</dd>',page)
                for svg in (first.svg_path,alternate.svg_path):
                    self.assertIn(f'src="{svg.name}"',page)
                self.assertIn(f'<code>{first.source_hash}</code>',page)
                self.assertIn('<dt>Match</dt><dd>bitmap</dd>',page)
                self.assertIn('<dt>Silhouette IoU</dt>',page)
                self.assertIn('<dt>Palette colours</dt>',page)
                self.assertIn('<h2>Algorithm results</h2>',page)
                self.assertIn(f'<article class="selected" data-algorithm="{result.svg_path.stem}">',page)
                self.assertNotIn('<gear>',page)
                class Images(HTMLParser):
                    def __init__(self):
                        super().__init__();self.images=[]
                    def handle_starttag(self,tag,attrs):
                        if tag=='img':self.images.append(dict(attrs))
                parsed=Images();parsed.feed(page)
                self.assertGreaterEqual(len(parsed.images),10)
                self.assertEqual(len([item for item in parsed.images
                                      if item['class']=='source-preview']),2)
                self.assertEqual(len([item for item in parsed.images
                                      if item['class']=='vector-preview']),2)
                self.assertGreaterEqual(len([item for item in parsed.images
                                             if item['class']=='stage-preview']),6)
            with sqlite3.connect(Path(folder)/'index.sqlite3') as db:
                self.assertEqual(db.execute('SELECT name FROM names WHERE hash=? ORDER BY name',
                                            (first.source_hash,)).fetchall(),
                                 [('Another name',),('Blue <gear>',)])
                self.assertEqual(db.execute('SELECT name FROM names WHERE hash=?',
                                            (shifted.source_hash,)).fetchall(),
                                 [('Settings & Tools',)])

    def test_review_pages_are_backfilled_for_an_existing_cache(self):
        with TemporaryDirectory() as folder:
            result=trace(icon(),'#ffffff',cache_dir=folder,name='Existing icon')
            result.review_path.unlink()
            (Path(folder)/'.review-pages-v4').unlink()
            with TraceCache(folder):
                pass
            self.assertIn('Existing icon',result.review_path.read_text())

    def test_saved_intermediates_show_exact_palette_input_and_contour_masks(self):
        with TemporaryDirectory() as folder:
            source=icon()
            ordinary=trace(source,'#ffffff',cache_dir=folder)
            smooth=trace(source,'#ffffff',algorithm='smooth-palette',cache_dir=folder)
            for result,scale in ((ordinary,1),(smooth,4)):
                base=result.svg_path.with_suffix('')
                stage_paths=[base.with_name(base.name+f'.{stage}.png')
                             for stage in ('target','quantized','islands','layer-00')]
                self.assertTrue(all(path.is_file() for path in stage_paths))
                with Image.open(stage_paths[0]) as target_image:
                    expected,_=_target(source,(0,0,24,24),background=(255,255,255))
                    np.testing.assert_array_equal(np.asarray(target_image),np.asarray(expected))
                with Image.open(stage_paths[1]) as quantized, Image.open(stage_paths[2]) as islands, \
                     Image.open(stage_paths[3]) as layer:
                    self.assertEqual(quantized.size,(24*scale,24*scale))
                    self.assertEqual(islands.size,quantized.size)
                    np.testing.assert_array_equal(np.asarray(islands)[:,:,:3],
                                                  np.asarray(quantized)[:,:,:3])
                    self.assertEqual(set(np.unique(np.asarray(islands)[:,:,3])),{0,255})
                    self.assertEqual(set(np.unique(np.asarray(layer)[:,:,3])),{0,255})
                    if scale==4:
                        palette=_icon_palette(expected,1)
                        np.testing.assert_array_equal(np.asarray(quantized),
                            np.asarray(_smooth_quantize(expected,palette,.5)))
                self.assertIn(f'{base.name}.islands.png',result.review_path.read_text())
                self.assertIn(f'{base.name}.layer-00.png',result.review_path.read_text())

    def test_old_svg_backfill_keeps_an_edited_trace(self):
        with TemporaryDirectory() as folder:
            result=trace(icon(),'#ffffff',cache_dir=folder)
            result.svg_path.write_text(result.svg.replace('#1450dc','#ff0000'))
            preview=result.svg_path.with_name(result.svg_path.stem+'.target.png')
            preview.unlink()
            (Path(folder)/'.review-pages-v4').unlink()
            with TraceCache(folder):
                pass
            self.assertTrue(preview.is_file())
            self.assertIn('#ff0000',result.svg_path.read_text())
            self.assertIn('SVG edited since tracing',result.review_path.read_text())

    def test_review_uses_the_intended_background_for_transparent_images(self):
        with TemporaryDirectory() as folder:
            image=Image.new('RGBA',(24,24))
            ImageDraw.Draw(image).rectangle((4,4,19,19),fill=(255,255,255,255))
            result=trace(image,'#101820',cache_dir=folder)
            self.assertIsNotNone(result.review_path)
            page=result.review_path.read_text()
            self.assertIn('--icon-background:#101820',page)
            self.assertIn('background-image:repeating-conic-gradient(from 45deg',page)
            self.assertIn('id="captured-background"',page)
            self.assertIn('#captured-background:checked ~ main img{background:var(--icon-background)}',page)
            self.assertIn('.source-preview{image-rendering:pixelated}',page)

    def test_review_warns_when_original_metrics_describe_an_edited_svg(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(),'#ffffff',cache_dir=folder)
            other=trace(icon(),'#ffffff',cache_dir=folder,algorithm='smooth-palette')
            first.svg_path.write_text(first.svg.replace('#1450dc','#ff0000'))
            trace(icon(),'#ffffff',cache_dir=folder)
            for page_path in (first.review_path,other.review_path):
                page=page_path.read_text()
                self.assertIn('SVG edited since tracing',page)
                self.assertIn('initial quality metrics',page)
                self.assertIn(f'src="{first.svg_path.name}"',page)
                self.assertIn(f'src="{other.svg_path.name}"',page)

    def test_old_cache_schema_migrates_and_rebuilds_review_pages(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(),'#ffffff',cache_dir=folder)
            second=trace(icon(),'#ffffff',cache_dir=folder,algorithm='smooth-palette')
            # Rebuild an index with the columns used before the review update.
            with sqlite3.connect(Path(folder)/'index.sqlite3') as db:
                db.execute('ALTER TABLE bitmaps DROP COLUMN match_kind')
                db.execute('ALTER TABLE traces DROP COLUMN generated_hash')
            (Path(folder)/'.review-pages-v4').unlink()
            first.review_path.unlink()
            second.review_path.unlink()
            with TraceCache(folder):
                pass
            page=first.review_path.read_text()
            self.assertIn('<dt>Match</dt><dd>unrecorded</dd>',page)
            self.assertIn(f'src="{second.svg_path.name}"',page)

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

    def test_search_uses_dimension_index_and_skips_dissimilar_bitmaps(self):
        with TemporaryDirectory() as folder, TraceCache(folder) as cache:
            red=cache.trace(icon(fill=(240,0,0)),'#ffffff')
            blue=cache.trace(icon(fill=(0,0,240)),'#ffffff')
            self.assertNotEqual(red.group_id,blue.group_id)
            plan=cache.db.execute('''EXPLAIN QUERY PLAN SELECT hash FROM bitmaps
                WHERE background=? AND width BETWEEN ? AND ?
                AND height BETWEEN ? AND ?''',('#ffffff',23,25,23,25)).fetchall()
            self.assertTrue(any('bitmap_search (background=? AND width>?' in row[3]
                                for row in plan),plan)
            with patch.object(cache,'_bitmap',wraps=cache._bitmap) as read:
                shifted=cache.trace(icon(5,fill=(240,0,0)),'#ffffff')
            self.assertEqual(shifted.group_id,red.group_id)
            self.assertNotIn(blue.source_hash,[call.args[0] for call in read.call_args_list])

    def test_fingerprint_allows_valid_one_pixel_shift_with_transparency(self):
        first=Image.new('RGBA',(24,24),(0,0,0,0))
        second=Image.new('RGBA',(25,25),(0,0,0,0))
        ImageDraw.Draw(first).rectangle((3,3,20,20),fill=(12,60,200,255))
        ImageDraw.Draw(second).rectangle((4,4,21,21),fill=(12,60,200,255))
        background=(248,248,248)
        self.assertIsNotNone(_bitmap_match(second,first,background))
        self.assertTrue(_fingerprint_might_match(_fingerprint(second,background),
                        _fingerprint(first,background),second.size,first.size))

    def test_old_cache_fingerprints_are_backfilled_when_searched(self):
        with TemporaryDirectory() as folder:
            red=trace(icon(fill=(240,0,0)),'#ffffff',cache_dir=folder)
            with sqlite3.connect(Path(folder)/'index.sqlite3') as db:
                db.execute('ALTER TABLE bitmaps DROP COLUMN fingerprint')
            with TraceCache(folder) as cache:
                shifted=cache.trace(icon(5,fill=(240,0,0)),'#ffffff')
                self.assertEqual(shifted.group_id,red.group_id)
                fingerprint,=cache.db.execute('SELECT fingerprint FROM bitmaps WHERE hash=?',
                                             (red.source_hash,)).fetchone()
                self.assertEqual(len(fingerprint),3)

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
