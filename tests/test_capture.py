import io
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops, ImageDraw
from svgshot.svg import to_svg
from svgshot.validate import render

from svgshot.capture import main, local_box, merge_uia, semantic_icon_boxes, semantic_svg, accessible_html
from svgshot.model import Node, flatten
from svgshot.snapshot import embed_snapshot


def element(role, name, box, **extra):
    return dict(control_type=role, name=name, bounds=box, children=[], enabled=True,
                control_element=True, states={}, **extra)


def snapshot():
    root = element(50032, 'Window <&>', [100, 200, 300, 180])
    root['children'] = [element(50000, 'Add…', [120, 225, 90, 30]),
                        element(50002, 'Keep semantics', [120, 280, 190, 22]),
                        element(50020, 'Exact UIA spelling', [120, 320, 180, 20])]
    root['children'][1]['states'] = {'toggle': 1}
    return dict(_renderer_view=True, screen_bounds=[100, 200, 300, 180], image_size=[300, 180], root=root)


def semantic_snapshot():
    root = {'id':'n1','role':'window','label':'Window <&>','bounds':[0,0,300,180],
            'states':{},'relationships':{},'text':{'status':'not_captured','lines':[]},
            'value':{'status':'not_captured'},'children':[
                {'id':'n2','role':'button','label':'Add…','bounds':[20,25,90,30],
                 'states':{},'relationships':{},'text':{'status':'not_captured','lines':[]},
                 'value':{'status':'not_captured'},'children':[]},
                {'id':'n3','role':'checkbox','label':'Keep semantics','bounds':[20,80,190,22],
                 'states':{'checked':'checked'},'relationships':{},
                 'text':{'status':'not_captured','lines':[]},
                 'value':{'status':'not_captured'},'children':[]} ]}
    return {'format':'svgshot.capture','version':3,
            'source':{'platform':'windows','provider':'windows-uia'},
            'image':{'size':[300,180],'coordinate_space':'image-pixels',
                     'source_bounds':[100,200,300,180],
                     'source_to_image':[1,0,0,1,-100,-200]},
            'capture_policy':{},'warnings':[],'root':root}


class CaptureTests(unittest.TestCase):
    def test_named_image_and_icon_button_keep_accessible_names_for_cache(self):
        from svgshot.icons import simplify_scene_artwork
        s=snapshot()
        s['root']['children'] = [element(50006,'Service icon',[120,225,24,24]),
                                  element(50000,'Open menu',[180,225,24,24])]
        image=Image.new('RGB',(300,180),'white')
        ImageDraw.Draw(image).rectangle((21,26,42,47),fill='#1450dc')
        ImageDraw.Draw(image).rectangle((81,26,102,47),fill='#dc3c14')
        scene=Node('root',(0,0,300,180))
        merge_uia(scene,image,s,ocr_enabled=False)
        names={n.vector_data.get('accessible_name') for n in flatten(scene)
               if n.kind=='capture-artwork'}
        self.assertTrue({'Service icon','Open menu'} <= names)
        with tempfile.TemporaryDirectory() as folder:
            simplify_scene_artwork(scene,image,cache_dir=folder)
            import sqlite3
            with sqlite3.connect(Path(folder)/'index.sqlite3') as db:
                stored={row[0] for row in db.execute('SELECT name FROM names')}
            self.assertTrue({'Service icon','Open menu'} <= stored)

    def test_image_inside_protected_control_is_not_named_in_cache(self):
        s=snapshot()
        secret=element(50004,'Password',[120,225,40,30])
        secret['password']=True
        secret['children']=[element(50006,'Private detail',[124,229,24,24])]
        s['root']['children']=[secret]
        image=Image.new('RGB',(300,180),'white')
        ImageDraw.Draw(image).rectangle((25,30,42,47),fill='#1450dc')
        scene=Node('root',(0,0,300,180))
        merge_uia(scene,image,s,ocr_enabled=False)
        self.assertFalse(any(n.vector_data.get('accessible_name') for n in flatten(scene)
                             if n.kind=='capture-artwork'))

    def test_msaa_sloping_tabs_replace_false_buttons_and_border_ink(self):
        s = snapshot()
        s['root']['children'] = [element(50019,'Extended',[120,325,100,28],
                                       framework_id='MSAA')]
        s['root']['children'][0]['states'] = {'selected':True}
        image = Image.new('RGB',(300,180),'#f0f0f0')
        ImageDraw.Draw(image).polygon(((20,125),(119,125),(108,152),(31,152)),
                                     fill='white',outline='#707070')
        scene = Node('root',(0,0,300,180),children=[
            Node('outlined-button',(28,130,80,32)),
            Node('text',(35,132,72,17),text='Extended /')])
        with patch('svgshot.capture._ocr_ui',return_value=[Node('text',(1,3,69,14),text='Extended')]):
            merge_uia(scene,image,s)
        nodes = list(flatten(scene))
        tab = next(n for n in nodes if n.kind == 'tab-active')
        self.assertIn('tab_corners',tab.vector_data)
        self.assertFalse(any(n.kind == 'outlined-button' for n in nodes))
        label = next(n for n in nodes if n.kind == 'text')
        self.assertEqual(label.text,'Extended')
        self.assertEqual(label.box[3],14)
        svg = ET.fromstring(to_svg(scene))
        self.assertIsNotNone(svg.find('.//{http://www.w3.org/2000/svg}path[@data-kind="tab-active"]'))

    def test_tab_border_ocr_does_not_become_caption_punctuation(self):
        s = snapshot()
        s['root']['children'] = [element(50019,'Standard',[120,225,100,28],framework_id='MSAA')]
        scene = Node('root',(0,0,300,180),children=[Node('text',(30,30,75,18),text='Standard /')])
        with patch('svgshot.capture._ocr_ui',return_value=[Node('text',(2,5,75,17),text='Standard ;')]):
            merge_uia(scene,Image.new('RGB',(300,180),'white'),s)
        self.assertEqual([n.text for n in flatten(scene) if n.kind=='text'],['Standard'])

    def test_header_sort_mark_is_excluded_without_masking_its_caption(self):
        s = snapshot()
        s['root']['children'] = [element(50035,'Name',[120,225,120,36],framework_id='MSAA')]
        image = Image.new('RGB',(300,180),'white')
        draw = ImageDraw.Draw(image)
        draw.polygon(((70,27),(65,32),(75,32)),fill='#777777')
        draw.text((25,40),'Name',fill='black')
        boxes = semantic_icon_boxes(image,s)
        self.assertEqual(len(boxes),1)
        self.assertLess(boxes[0][1]+boxes[0][3],40)

    def test_leading_row_icon_is_artwork_and_does_not_consume_the_caption(self):
        s = snapshot()
        s['root']['children'] = [element(50007,'Known service',[120,225,250,25],framework_id='MSAA')]
        image = Image.new('RGB',(300,180),'white')
        ImageDraw.Draw(image).rectangle((23,29,38,44),fill='#427ba1')
        boxes = semantic_icon_boxes(image,s)
        self.assertEqual(len(boxes),1)
        scene = Node('root',(0,0,300,180),children=[
            Node('text',(24,30,10,13),text='Ch'),
            Node('text',(45,30,95,14),text='Known service')])
        merge_uia(scene,image,s)
        self.assertEqual([n.text for n in flatten(scene) if n.kind=='text'],['Known service'])
        self.assertTrue(any(n.kind=='capture-artwork' for n in flatten(scene)))
        self.assertFalse(any(n.vector_data.get('accessible_name') for n in flatten(scene)
                             if n.kind=='capture-artwork'),
                         'A list row caption was mistaken for its generic icon name')

    def test_msaa_caption_retry_recovers_suffix_missing_from_page_ocr(self):
        s = snapshot()
        s['root']['children'] = [element(50007,'Installer (suffix)',[120,225,250,25],framework_id='MSAA')]
        scene = Node('root',(0,0,300,180),children=[Node('text',(45,30,55,14),text='Installer')])
        with patch('svgshot.capture._ocr_ui',return_value=[Node('text',(25,5,115,14),text='Installer (suffix)')]):
            merge_uia(scene,Image.new('RGB',(300,180),'white'),s)
        self.assertEqual([n.text for n in flatten(scene) if n.kind=='text'],['Installer (suffix)'])

    def test_msaa_row_does_not_erase_other_columns_or_invent_offscreen_names(self):
        s = snapshot()
        s['root']['children'] = [element(50007, 'Known service', [120,225,250,25], framework_id='MSAA'),
                                  element(50007, 'Hidden row', [120,250,250,25], framework_id='MSAA')]
        scene = Node('root', (0,0,300,180), children=[
            Node('text', (25,30,95,14), text='Known service'),
            Node('text', (170,30,65,14), text='Running')])
        merge_uia(scene, Image.new('RGB',(300,180),'white'), s)
        texts = [n.text for n in flatten(scene) if n.kind == 'text']
        self.assertEqual(texts.count('Known service'), 1)
        self.assertIn('Running', texts)
        self.assertNotIn('Hidden row', texts)

    def test_field_ocr_does_not_duplicate_nested_breadcrumb_captions(self):
        s = snapshot()
        field = element(50004, 'Address bar', [110,220,280,30])
        field['children'] = [element(50000,name,[115+index*85,225,80,20])
                             for index,name in enumerate(('Network','files','scratch'))]
        s['root']['children'] = [field]
        whole = 'Network > files > scratch'
        scene = Node('root',(0,0,300,180),children=[Node('text',(15,24,230,14),text=whole)])
        with patch('svgshot.capture._ocr_ui', return_value=[Node('text',(0,2,230,14),text=whole,confidence=.95)]):
            merge_uia(scene,Image.new('RGB',(300,180),'white'),s)
        text = [n.text for n in flatten(scene) if n.kind == 'text']
        self.assertNotIn(whole,text)
        self.assertEqual(sorted(text),['Network','files','scratch'])

    def test_square_icon_control_keeps_artwork_instead_of_ocr_letters(self):
        s = snapshot()
        s['root']['children'] = [element(50013,'Details',[120,225,30,30],framework_id='DirectUI')]
        scene = Node('root',(0,0,300,180),children=[Node('text',(23,29,20,15),text='Ba')])
        merge_uia(scene,Image.new('RGB',(300,180),'white'),s)
        self.assertFalse(any(n.kind == 'text' for n in flatten(scene)))
        self.assertTrue(any(n.kind == 'capture-artwork' for n in flatten(scene)))

    def test_misplaced_icon_bounds_cannot_consume_a_visible_caption(self):
        s = snapshot()
        s['root']['children'] = [element(50000,'Close',[120,225,30,30],framework_id='MSAA')]
        scene = Node('root',(0,0,300,180),children=[Node('text',(23,29,45,15),text='Action')])
        merge_uia(scene,Image.new('RGB',(300,180),'white'),s)
        self.assertIn('Action',[n.text for n in flatten(scene) if n.kind == 'text'])

    def test_coordinate_origin_scaling_and_clipping(self):
        s = snapshot()
        self.assertEqual(local_box([120, 225, 90, 30], s), (20, 25, 90, 30))
        self.assertEqual(local_box([90, 190, 30, 30], s), (0, 0, 20, 20))
        self.assertIsNone(local_box([0, 0, 10, 10], s))
        s['image_size'] = [600, 360]
        self.assertEqual(local_box([120, 225, 90, 30], s), (40, 50, 180, 60))

    def test_uia_corrects_ocr_and_keeps_unknown_labels(self):
        s = snapshot()
        scene = Node('root', (0, 0, 300, 180), color='#ffffff', children=[
            Node('text', (23, 122, 160, 12), text='Exact UlA spelling'),
            Node('text', (220, 155, 50, 12), text='Unknown'),
        ])
        merge_uia(scene, Image.new('RGB', (300, 180), 'white'), s)
        text = [n.text for n in flatten(scene) if n.kind == 'text']
        self.assertIn('Exact UIA spelling', text)
        self.assertNotIn('Exact UlA spelling', text)
        self.assertIn('Unknown', text)
        self.assertIn('Add…', text)
        self.assertTrue(any(n.kind == 'checkbox-selected' for n in flatten(scene)))

    def test_structured_svg_escaping_states_and_offscreen(self):
        s = snapshot()
        s['root']['children'].append(element(50020, 'Not visible', [120, 260, 20, 20], offscreen=True))
        scene = merge_uia(Node('root', (0, 0, 300, 180), color='#ffffff'), Image.new('RGB', (300, 180)), s)
        svg = semantic_svg(scene, s)
        doc = ET.fromstring(svg)
        self.assertNotEqual(doc.get('role'), 'img')
        ns = {'s': 'http://www.w3.org/2000/svg'}
        labels = [n.get('aria-label', '') for n in doc.findall('.//s:g', ns)]
        self.assertTrue(any('Keep semantics' in n and 'checked' in n for n in labels))
        self.assertFalse(any('Not visible' in n for n in labels))
        self.assertTrue(any('Window <&>' in label for label in labels))
        self.assertIn('Captured interface information', accessible_html(svg, s))

    def test_accessibility_overlay_preserves_visual_pixels(self):
        s = snapshot()
        scene = Node('root', (0, 0, 300, 180), color='#ffffff', children=[
            Node('rect', (20, 30, 60, 20), color='#0078d7'),
            Node('text', (100, 80, 90, 14), text='Visible text'),
        ])
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            a, b = d/'visual.svg', d/'semantic.svg'
            a.write_text(to_svg(scene))
            b.write_text(semantic_svg(scene, s))
            self.assertIsNone(ImageChops.difference(render(str(a), 300), render(str(b), 300)).getbbox())

    def test_titlebar_button_names_are_not_visible_captions(self):
        s = snapshot()
        titlebar = element(50037, '', [100, 200, 300, 25])
        titlebar['children'] = [element(50000, 'Minimize', [350, 200, 25, 25])]
        s['root']['children'].append(titlebar)
        scene = merge_uia(Node('root', (0, 0, 300, 180), color='#ffffff'), Image.new('RGB', (300, 180)), s)
        texts = [n.text for n in flatten(scene) if n.kind == 'text']
        self.assertNotIn('Minimize', texts)
        self.assertIn('Window <&>', texts)
        self.assertIn('Minimize', semantic_svg(scene, s))

    def test_close_caption_keeps_its_icon_without_a_button_outline(self):
        s = snapshot()
        s['root']['children'] = [element(50000, 'Close', [350, 201, 46, 32],
                                         class_name='Button', framework_id='Win32')]
        scene = merge_uia(Node('root', (0, 0, 300, 180), color='#ffffff'),
                          Image.new('RGB', (300, 180), 'white'), s)
        self.assertFalse(any(n.kind == 'outlined-button' for n in flatten(scene)))
        self.assertIn('Close', semantic_svg(scene, s))

    def test_replay_cli_and_dimension_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            image, output = d/'capture.png', d/'capture.svg'
            s = semantic_snapshot()
            stream = io.BytesIO()
            Image.new('RGB', (300, 180), 'white').save(stream, format='PNG')
            original_png = stream.getvalue()
            image.write_bytes(embed_snapshot(original_png, s))
            self.assertEqual(main([str(output), '--image', str(image), '--no-ocr', '--html', str(d/'capture.html')]), 0)
            svg = ET.parse(output)
            self.assertEqual(svg.getroot().get('role'), 'graphics-document group')
            self.assertNotIn('<image ', output.read_text())
            s['image']['size'] = [5, 5]
            image.write_bytes(embed_snapshot(original_png, s))
            self.assertEqual(main([str(output), '--image', str(image), '--no-ocr']), 1)

    def test_table_cells_and_icon_buttons_are_not_painted_as_form_controls(self):
        s = snapshot()
        row = element(50007, 'report.PNG', [110, 250, 270, 30], framework_id='DirectUI')
        cell = element(50004, 'Name', [140, 250, 180, 30], framework_id='DirectUI')
        cell['states'] = {'value': 'report.PNG'}
        row['children'] = [cell]
        button = element(50000, 'Back', [110, 205, 30, 30], framework_id='XAML', class_name='Button')
        glyph = element(50020, '\ue72b', [115, 210, 18, 18], framework_id='XAML', class_name='Button')
        glyph['text_ranges'] = [{'text': '\ue72b', 'rectangles': [[115, 210, 18, 18]]}]
        button['children'] = [glyph]
        s['root']['children'] = [row, button]
        scene = Node('root', (0, 0, 300, 180), color='#ffffff')
        merge_uia(scene, Image.new('RGB', (300, 180), 'white'), s)
        nodes = list(flatten(scene))
        self.assertEqual([n.text for n in nodes if n.kind == 'text'], ['report.PNG'])
        self.assertFalse(any(n.kind in {'outline', 'outlined-button'} for n in nodes))
        self.assertTrue(any(n.kind == 'capture-artwork' for n in nodes))
        self.assertIn('Back', semantic_svg(scene, s))

    def test_ocr_line_spanning_icons_and_caption_is_replaced(self):
        s = snapshot()
        s['root']['children'] = [element(50020, 'Details', [330, 220, 55, 24])]
        scene = Node('root', (0, 0, 300, 180), children=[
            Node('text', (100, 25, 180, 14), text='eee C3 Details')])
        merge_uia(scene, Image.new('RGB', (300, 180), 'white'), s)
        self.assertEqual([n.text for n in flatten(scene) if n.kind == 'text'], ['Details'])

    def test_bright_artwork_keeps_color_and_transparent_holes(self):
        from svgshot.capture import trace_artwork
        image = Image.new('RGB', (30, 30), '#191919')
        image.paste('#ffff00', (3, 3, 27, 27))
        image.paste('#191919', (10, 10, 20, 20))
        artwork = trace_artwork(image, (0, 0, 30, 30))
        self.assertTrue(any(p['fill'] == '#ffff18' for p in artwork.vector_data['paths']))
        scene = Node('root', (0, 0, 30, 30), color='#191919', children=[artwork])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'artwork.svg'
            path.write_text(semantic_svg(scene, snapshot()))
            rendered = render(str(path), 30).convert('RGB')
            self.assertEqual(rendered.getpixel((15,15)), (25,25,25))
            self.assertGreater(rendered.getpixel((5,5))[0], 240)
            self.assertLess(rendered.getpixel((5,5))[2], 50)

    def test_terminal_grid_spacing_colours_and_blank_lines(self):
        from PIL import ImageDraw
        s = snapshot()
        terminal = element(50020, 'Terminal', [110, 240, 240, 100], class_name='TermControl', framework_id='XAML')
        terminal['text_ranges'] = [
            {'text': 'AB  CD' + ' '*14, 'rectangles': [[110, 240, 240, 30]]},
            {'text': ' '*20, 'rectangles': [[110, 270, 240, 30]]},
            {'text': 'E' + ' '*19, 'rectangles': [[110, 300, 240, 30]]},
        ]
        s['root']['children'] = [terminal]
        image = Image.new('RGB', (300, 180), '#002050')
        draw = ImageDraw.Draw(image)
        for x in (10, 22):
            draw.rectangle((x+3, 45, x+8, 60), fill='#00ff00')
        for x in (58, 70):
            draw.rectangle((x+3, 45, x+8, 60), fill='#ffff00')
        scene = Node('root', (0, 0, 300, 180), children=[Node('text', (10, 40, 200, 16), text='OCR duplicate')])
        merge_uia(scene, image, s)
        texts = [n for n in flatten(scene) if n.kind == 'text']
        self.assertEqual(''.join(n.text for n in texts), 'AB  CDE')
        self.assertEqual([n.box[0] for n in texts], [10, 58, 10])
        self.assertEqual([n.box[1] for n in texts], [40, 40, 100])
        self.assertEqual([n.box[2] for n in texts], [48, 24, 12])
        self.assertEqual([n.color for n in texts[:2]], ['#00ff00', '#ffff00'])
        svg = semantic_svg(scene, s)
        doc = ET.fromstring(svg)
        visible = doc.findall('.//{http://www.w3.org/2000/svg}text')
        self.assertTrue(all(n.get('font-size') == '24' for n in visible))
        self.assertTrue(all(n.get('{http://www.w3.org/XML/1998/namespace}space') == 'preserve' for n in visible))
        self.assertTrue(all('Mono' in n.get('font-family') or 'Consolas' in n.get('font-family') for n in visible))
        self.assertIn('AB  CD' + ' '*14, svg)

    def test_terminal_semantic_colours_override_letter_pixel_colours(self):
        from svgshot.capture import terminal_text
        from PIL import ImageDraw
        s = snapshot()
        terminal = element(50020, 'Terminal', [110, 240, 120, 30], class_name='TermControl')
        value = lambda color: {'40008': {'status': 'value', 'value': color}}
        line = {'text': 'enn W xyz ', 'rectangles': [[110, 240, 120, 30]],
                'attributes': value(0xcccccc),
                'format_runs': {'status': 'value', 'ranges': [
                    {'text': 'enn W ', 'attributes': value(0xcccccc)},
                    {'text': 'xyz ', 'attributes': value(0x6a5fff)},
                ]}}
        terminal['text_ranges'] = [line]
        image = Image.new('RGB', (300, 180), '#012456')
        draw = ImageDraw.Draw(image)
        for index, color in enumerate(['#cccccc', '#b096bc', '#b096bc', '#cccccc', '#c8c3b3']):
            draw.rectangle((13+index*12, 45, 18+index*12, 60), fill=color)
        texts = terminal_text(image, terminal, s)
        self.assertEqual([(n.text, n.color, n.box[0], n.box[2]) for n in texts],
                         [('enn W ', '#cccccc', 10, 72), ('xyz', '#ff5f6a', 82, 36)])
        # Mixed line colours must use the individual formatting ranges.
        line['attributes'] = {'40008': {'status': 'mixed'}}
        self.assertEqual([n.color for n in terminal_text(image, terminal, s)],
                         ['#cccccc', '#ff5f6a'])
        # A line colour also works without any formatting ranges.
        line.pop('format_runs')
        line['attributes'] = value(0xcccccc)
        self.assertEqual([(n.text, n.color) for n in terminal_text(image, terminal, s)],
                         [('enn W xyz', '#cccccc')])

    def test_clipped_xaml_caption_keeps_full_semantic_name(self):
        from PIL import ImageDraw
        s = snapshot()
        name = 'A very long tab label that is clipped'
        s['root']['children'] = [element(50020, name, [120, 220, 70, 24], framework_id='XAML')]
        image = Image.new('RGB', (300, 180), 'white')
        ImageDraw.Draw(image).rectangle((20, 25, 89, 38), fill='black')
        scene = merge_uia(Node('root', (0, 0, 300, 180)), image, s)
        caption = next(n.text for n in flatten(scene) if n.kind == 'text')
        self.assertTrue(caption.endswith('…'))
        self.assertLess(len(caption), len(name))
        self.assertIn(name, semantic_svg(scene, s))

    def test_password_ocr_is_not_emitted(self):
        s = snapshot()
        item = element(50004, 'Password', [120, 225, 90, 30], password=True)
        s['root']['children'] = [item]
        scene = Node('root', (0, 0, 300, 180), children=[Node('text', (25, 30, 60, 12), text='masked')])
        merge_uia(scene, Image.new('RGB', (300, 180)), s)
        self.assertFalse(any(n.kind == 'text' for n in flatten(scene)))

    def test_debug_snapshot_can_render_value_that_is_normally_redacted(self):
        from svgshot.schema import render_view
        s = semantic_snapshot()
        s['capture_policy'] = {'debug_unredacted': True}
        s['root']['children'].append({
            'id':'n4','role':'edit','label':'Password','bounds':[20,110,180,24],
            'states':{'protected':True},'relationships':{},
            'text':{'status':'not_supported','lines':[]},
            'value':{'status':'value','value':'debug-secret'},'children':[]})
        view = render_view(s)
        scene = merge_uia(Node('root',(0,0,300,180)),Image.new('RGB',(300,180)),view)
        self.assertIn('debug-secret',[n.text for n in flatten(scene) if n.kind=='text'])

    def test_visible_edit_text_is_recovered_from_its_own_bounds(self):
        s = snapshot()
        s['root']['children'] = [element(50004, 'Open:', [180, 220, 100, 22])]
        scene = Node('root', (0, 0, 300, 180), color='#ffffff', children=[
            Node('text', (5, 20, 116, 12), text='Open: | secpol.msc', color='#222222')])
        detected = Node('text', (0, 2, 59, 12), text='secpol.msc', confidence=.89)
        with patch('svgshot.capture._ocr_ui', return_value=[detected]):
            merge_uia(scene, Image.new('RGB', (300, 180)), s)
        values = [n for n in flatten(scene) if n.kind == 'text']
        self.assertEqual([n.text for n in values], ['secpol.msc'])
        self.assertEqual(values[0].vector_data['source'], 'ocr')

    def test_overlapping_document_and_control_text_is_drawn_once(self):
        s = snapshot()
        s['root']['text_ranges'] = [{'text': 'Add…', 'rectangles': [[120, 225, 90, 30]]}]
        scene = Node('root', (0, 0, 300, 180), color='#ffffff')
        merged = merge_uia(scene, Image.new('RGB', (300, 180), 'white'), s)
        self.assertEqual([n.text for n in flatten(merged) if n.kind == 'text'].count('Add…'), 1)
        svg = semantic_svg(merged, s)
        self.assertEqual(svg.count('aria-label="Add…, button"'), 1)
        outline = accessible_html(svg, s).split('Captured interface information', 1)[1]
        self.assertEqual(outline.count('<li>Add…, button</li>'), 1)


if __name__ == '__main__':
    unittest.main()
