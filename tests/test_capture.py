import io
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops
from svgshot.svg import to_svg
from svgshot.validate import render

from svgshot.capture import main, local_box, merge_uia, semantic_svg, accessible_html
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


if __name__ == '__main__':
    unittest.main()
