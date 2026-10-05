"""Persistent source and editable trace reuse across placements and algorithms."""
from pathlib import Path
import json
import re
from xml.etree import ElementTree as ET
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import sqlite3
from html.parser import HTMLParser

from PIL import Image, ImageDraw
import numpy as np

from svgshot.model import Node
from svgshot.svg import to_svg
from svgshot.tracing import (trace, TraceCache, main as trace_main, _bitmap_match, _fingerprint,
                             _fingerprint_might_match, _sample_fingerprint,
                             _sample_might_match, _filename)
from svgshot.tracing_core import _target, _icon_palette, _smooth_quantize
from svgshot.validate import render


def diagnostic(result, filename):
    return result.review_path.parent/f'{result.svg_path.stem}.{filename}'


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
            exact=trace(icon(),'#ffffff',cache_dir=folder,algorithm='pixel-boundary')
            self.assertEqual({r.review_path for r in (first,shifted,alternate,exact)},
                             {first.review_path})
            self.assertEqual(list(first.review_path.parent.rglob('*.html')),
                             [first.review_path])
            for result in (first,alternate,exact):
                page=result.review_path.read_text()
                self.assertEqual(result.review_path.parent.name,result.group_id)
                self.assertEqual(result.review_path.parent.parent.name,'summaries')
                self.assertIn('Blue &lt;gear&gt;',page)
                self.assertIn('Settings &amp; Tools',page)
                self.assertIn('Another name',page)
                self.assertIn(f'bitmaps/{first.source_hash}.png',page)
                self.assertIn(f'bitmaps/{shifted.source_hash}.png',page)
                self.assertIn('width="384" height="384"',page)
                self.assertIn('image-rendering:pixelated',page)
                self.assertIn('<dt>Offset</dt><dd>(1, 0) pixels</dd>',page)
                for svg in (first.svg_path,alternate.svg_path,exact.svg_path):
                    self.assertIn(f'src="../../groups/{result.group_id}/algorithms/{svg.name}"',page)
                self.assertIn(f'<code>{first.source_hash}</code>',page)
                self.assertIn('<dt>Match</dt><dd>bitmap</dd>',page)
                self.assertIn('<dt>Silhouette IoU</dt>',page)
                self.assertIn('<dt>Palette colours</dt>',page)
                self.assertIn('<h2>Algorithm results</h2>',page)
                self.assertEqual(page.count('<h4>Selected palette</h4>'),3)
                self.assertIn(f'<article data-algorithm="{result.svg_path.stem}">',page)
                self.assertNotIn('<gear>',page)
                class Images(HTMLParser):
                    def __init__(self):
                        super().__init__();self.images=[]
                    def handle_starttag(self,tag,attrs):
                        if tag=='img':self.images.append(dict(attrs))
                parsed=Images();parsed.feed(page)
                self.assertGreaterEqual(len(parsed.images),15)
                self.assertEqual(len([item for item in parsed.images
                                      if item['class']=='source-preview']),2)
                self.assertEqual(len([item for item in parsed.images
                                      if item['class']=='vector-preview']),3)
                self.assertGreaterEqual(len([item for item in parsed.images
                                             if item['class']=='stage-preview']),9)
                for item in parsed.images:
                    self.assertTrue((result.review_path.parent/item['src']).is_file(),item['src'])
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
            with TraceCache(folder) as cache:
                cache.trace(icon(),'#ffffff')
            self.assertIn('Existing icon',result.review_path.read_text())

    def test_review_can_be_rebuilt_from_summary_after_database_closes(self):
        with TemporaryDirectory() as folder:
            cache=TraceCache(folder)
            result=cache.trace(icon(),'#ffffff',name='Source icon')
            self.assertTrue((result.review_path.parent/'group.json').is_file())
            self.assertTrue((result.review_path.parent/
                             f'{result.source_hash}.source.json').is_file())
            self.assertTrue((result.review_path.parent/
                             f'{result.svg_path.stem}.trace.json').is_file())
            cache.close()
            result.review_path.unlink()
            cache._refresh_review_pages(result.group_id)
            self.assertIn('Source icon',result.review_path.read_text())
            self.assertIn(f'../../groups/{result.group_id}/algorithms/',
                          result.review_path.read_text())

    def test_saved_intermediates_show_exact_palette_input_and_contour_masks(self):
        with TemporaryDirectory() as folder:
            source=icon()
            ordinary=trace(source,'#ffffff',cache_dir=folder,estimate_alpha=True)
            smooth=trace(source,'#ffffff',algorithm='smooth-palette',cache_dir=folder,
                         estimate_alpha=True)
            for result,scale in ((ordinary,1),(smooth,4)):
                base=result.svg_path.with_suffix('')
                stage_paths=[diagnostic(result,f'{stage}.png')
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

    def test_selected_palette_swatches_match_visible_quantized_pixels(self):
        with TemporaryDirectory() as folder:
            image=Image.new('RGBA',(24,24))
            draw=ImageDraw.Draw(image)
            draw.rectangle((3,3,11,20),fill=(220,20,20,255))
            draw.rectangle((12,3,20,20),fill=(20,30,220,255))
            result=trace(image,'#ffffff',cache_dir=folder,
                         algorithm='pixel-boundary',palette_size=2,estimate_alpha=True)
            palette_path=diagnostic(result,'palette.json')
            palette=json.loads(palette_path.read_text())
            self.assertEqual(len(palette),2)
            self.assertEqual(sum(entry['pixels'] for entry in palette),18*18)
            page=result.review_path.read_text()
            for entry in palette:
                self.assertIn(f'<code>{entry["rgb"]}</code> · {entry["pixels"]} grid pixels',page)
                self.assertIn(f'background-color:{entry["rgb"]}',page)

    def test_pixel_boundary_trace_follows_quantized_mask_without_diagonal_lines(self):
        with TemporaryDirectory() as folder:
            image=Image.new('RGBA',(24,24))
            draw=ImageDraw.Draw(image)
            draw.rectangle((3,3,19,19),fill=(20,80,220,255))
            draw.rectangle((8,8,14,14),fill=(0,0,0,0))
            draw.rectangle((3,16,22,19),fill=(20,80,220,255))
            result=trace(image,'#ffffff',cache_dir=folder,algorithm='pixel-boundary',
                         estimate_alpha=True)
            self.assertIsNotNone(result.svg_path)
            self.assertIn('pixel-boundary',result.svg_path.name)
            paths=ET.parse(result.svg_path).findall('.//{http://www.w3.org/2000/svg}path')
            self.assertTrue(paths)
            for path in paths:
                self.assertRegex(path.attrib['d'],
                    r'^M\d+,\d+(?:[HV]\d+)+Z(?: M\d+,\d+(?:[HV]\d+)+Z)*$')
            islands=diagnostic(result,'islands.png')
            with Image.open(islands) as source:
                expected=np.asarray(source.getchannel('A'))>0
            rendered=np.asarray(render(str(result.svg_path),24))
            np.testing.assert_array_equal((rendered[:,:,:3]!=255).any(axis=2),expected)
            again=trace(image,'#ffffff',cache_dir=folder,algorithm='pixel-boundary',
                        estimate_alpha=True)
            self.assertEqual(again.match,'exact')
            self.assertEqual(again.svg,result.svg)

    def test_no_estimated_alpha_uses_background_as_reference_without_exporting_it(self):
        with TemporaryDirectory() as folder:
            image=icon()
            inferred=trace(image,'#ffffff',cache_dir=folder,palette_size=2,
                           estimate_alpha=True)
            for algorithm in ('palette','smooth-palette','pixel-boundary'):
                with self.subTest(algorithm=algorithm):
                    result=trace(image,'#ffffff',cache_dir=folder,
                                 algorithm=algorithm,palette_size=2)
                    self.assertIsNotNone(result.svg_path)
                    self.assertNotEqual(result.svg_path,inferred.svg_path)
                    self.assertIn(inferred.svg_path.name,result.review_path.read_text())
                    paths=ET.parse(result.svg_path).findall('.//{http://www.w3.org/2000/svg}path')
                    self.assertEqual(len(paths),1)
                    self.assertEqual(paths[0].get('fill'),'#1450dc')
                    self.assertNotIn('fill="#ffffff"',result.svg)
                    self.assertTrue(result.node.vector_data['no_estimated_alpha'])
                    self.assertIn('Background reference (not exported)',
                                  result.review_path.read_text())
                    palette=json.loads(diagnostic(result,'palette.json').read_text())
                    self.assertEqual(palette[0]['rgb'],'#ffffff')
                    with Image.open(diagnostic(result,'quantized.png')) as quantized:
                        self.assertEqual(quantized.getchannel('A').getextrema(),(255,255))
                    with Image.open(diagnostic(result,'layer-00.png')) as layer:
                        self.assertEqual(layer.getchannel('A').getextrema(),(255,255))
                    rendered=np.asarray(render(str(result.svg_path),24))
                    self.assertTrue(np.all(rendered[0,0]==255))
                    self.assertTrue(np.all(rendered[10,10]==(20,80,220)))

    def test_no_estimated_alpha_flattens_real_alpha_without_inferring_new_alpha(self):
        with TemporaryDirectory() as folder:
            image=Image.new('RGBA',(24,24))
            ImageDraw.Draw(image).rectangle((5,5,18,18),fill=(20,80,220,128))
            result=trace(image,'#101820',cache_dir=folder,
                         algorithm='pixel-boundary',palette_size=2)
            target=diagnostic(result,'target.png')
            with Image.open(target) as flattened:
                self.assertEqual(flattened.getchannel('A').getextrema(),(255,255))
                self.assertEqual(flattened.getpixel((0,0))[:3],(16,24,32))
                self.assertNotEqual(flattened.getpixel((8,8))[:3],(20,80,220))
            self.assertNotIn('fill="#101820"',result.svg)
            self.assertNotIn('M0,0H24V24H0Z',result.svg)

    def test_trace_cli_defaults_to_no_estimation_and_accepts_estimate_alpha(self):
        with TemporaryDirectory() as folder:
            source=Path(folder)/'icon.png'
            icon().save(source)
            self.assertEqual(trace_main([str(source),'#ffffff','--algorithm','pixel-boundary',
                                         '--palette-size','2',
                                         '--cache-dir',folder]),0)
            paths=list((Path(folder)/'groups').glob('*/algorithms/pixel-boundary-*.svg'))
            self.assertEqual(len(paths),1)
            self.assertNotIn('d="M0,0H24V24H0Z"',paths[0].read_text())
            self.assertEqual(trace_main([str(source),'#ffffff','--algorithm','pixel-boundary',
                                         '--estimate-alpha','--palette-size','2',
                                         '--cache-dir',folder]),0)
            self.assertEqual(len(list((Path(folder)/'groups').glob(
                '*/algorithms/pixel-boundary-*.svg'))),2)

    def test_old_svg_backfill_keeps_an_edited_trace(self):
        with TemporaryDirectory() as folder:
            result=trace(icon(),'#ffffff',cache_dir=folder)
            result.svg_path.write_text(result.svg.replace('#1450dc','#ff0000'))
            preview=diagnostic(result,'target.png')
            preview.unlink()
            result.review_path.unlink()
            with TraceCache(folder) as cache:
                cache.trace(icon(),'#ffffff')
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
                self.assertIn(f'src="../../groups/{first.group_id}/algorithms/{first.svg_path.name}"',page)
                self.assertIn(f'src="../../groups/{other.group_id}/algorithms/{other.svg_path.name}"',page)

    def test_old_cache_schema_migrates_and_rebuilds_review_pages(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(),'#ffffff',cache_dir=folder)
            second=trace(icon(),'#ffffff',cache_dir=folder,algorithm='smooth-palette')
            # Model the previous layout: each algorithm had its own HTML and
            # its diagnostic files were mixed with editable SVGs.
            for result in (first,second):
                old_html=result.svg_path.with_suffix('.html')
                old_html.write_text('Former review page')
                for source in result.review_path.parent.glob(result.svg_path.stem+'.*'):
                    if source.name.endswith(('.png','.json')) and not source.name.endswith('.trace.json'):
                        source.replace(result.svg_path.with_name(source.name))
            # Rebuild an index with the columns used before the review update.
            with sqlite3.connect(Path(folder)/'index.sqlite3') as db:
                db.execute('ALTER TABLE bitmaps DROP COLUMN match_kind')
                db.execute('ALTER TABLE traces DROP COLUMN generated_hash')
            first.review_path.unlink()
            with TraceCache(folder) as cache:
                cache.trace(icon(),'#ffffff')
            page=first.review_path.read_text()
            self.assertIn('<dt>Match</dt><dd>unrecorded</dd>',page)
            self.assertIn(f'src="../../groups/{second.group_id}/algorithms/{second.svg_path.name}"',page)
            self.assertEqual(first.review_path,second.review_path)
            self.assertEqual(list(first.review_path.parent.rglob('*.html')),[first.review_path])
            for result in (first,second):
                self.assertTrue(diagnostic(result,'target.png').is_file())
                self.assertTrue(diagnostic(result,'palette.json').is_file())
                self.assertFalse(result.svg_path.with_suffix('.html').exists())
                self.assertEqual(list(result.svg_path.parent.glob(result.svg_path.stem+'.*')),
                                 [result.svg_path])

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

    def test_sample_filter_rejects_unrelated_icons_and_preserves_offsets(self):
        background=(255,255,255)
        known=icon()
        different=icon(fill=(220,20,20))
        sample=_sample_fingerprint(known,background)
        def flattened(image):
            return np.asarray(image.convert('RGB'),dtype=np.float32)
        self.assertFalse(_sample_might_match(flattened(different),sample,
                                            different.size,known.size))
        shifted=icon(5)
        self.assertTrue(_sample_might_match(flattened(shifted),sample,
                                           shifted.size,known.size))
        self.assertTrue(_sample_might_match(flattened(known),sample,
                                           known.size,known.size))

    def test_exact_hits_leave_review_page_untouched_until_an_svg_is_edited(self):
        with TemporaryDirectory() as folder, TraceCache(folder) as cache:
            result=cache.trace(icon(),'#ffffff')
            with patch.object(cache,'_refresh_review_pages',wraps=cache._refresh_review_pages) as refresh, \
                 patch('svgshot.tracing._bitmap_match') as fuzzy, \
                 patch('svgshot.tracing.simplify_icon') as retrace:
                cache.trace(icon(),'#ffffff')
                self.assertEqual(refresh.call_count,0)
                fuzzy.assert_not_called()
                retrace.assert_not_called()
                result.svg_path.write_text(result.svg.replace('#1450dc','#ff0000'))
                cache.trace(icon(),'#ffffff')
                self.assertEqual(refresh.call_count,1)

    def test_switching_mode_retraces_known_bitmap_once(self):
        with TemporaryDirectory() as folder, TraceCache(folder) as cache:
            source=icon()
            default=cache.trace(source,'#ffffff')
            from svgshot.tracing_core import simplify_icon as actual_simplify
            with patch('svgshot.tracing._bitmap_match') as fuzzy, \
                 patch('svgshot.tracing.simplify_icon',wraps=actual_simplify) as retrace:
                inferred=cache.trace(source,'#ffffff',estimate_alpha=True)
                again=cache.trace(source,'#ffffff',estimate_alpha=True)
            self.assertEqual(retrace.call_count,1)
            fuzzy.assert_not_called()
            self.assertEqual(inferred.match,'exact')
            self.assertEqual(again.match,'exact')
            self.assertNotEqual(default.svg_path,inferred.svg_path)

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

    def test_new_palette_version_preserves_old_hand_edited_svg(self):
        with TemporaryDirectory() as folder:
            first=trace(icon(),'#ffffff',cache_dir=folder,estimate_alpha=True)
            old_key=_filename('palette',None,.5,version=1,estimate_alpha=True)
            old_path=first.svg_path.with_name(old_key+'.svg')
            first.svg_path.rename(old_path)
            old_path.write_text(old_path.read_text().replace('#1450dc','#ff0000'))
            with sqlite3.connect(Path(folder)/'index.sqlite3') as db:
                db.execute('UPDATE traces SET algorithm=? WHERE group_id=? AND algorithm=?',
                           (old_key,first.group_id,first.svg_path.stem))
            inherited=trace(icon(),'#ffffff',cache_dir=folder,estimate_alpha=True)
            self.assertIn('#ff0000',inherited.svg)
            self.assertTrue(old_path.is_file())
            self.assertNotEqual(inherited.svg_path,old_path)
            self.assertIn('SVG edited since tracing',inherited.review_path.read_text())

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
