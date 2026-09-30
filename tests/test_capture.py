import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops
from svgshot.svg import to_svg
from svgshot.validate import render

from svgshot.capture import main, local_box, merge_uia, semantic_svg, accessible_html
from svgshot.model import Node, flatten


def element(role, name, box, **extra):
    return dict(control_type=role, name=name, bounds=box, children=[], enabled=True,
                control_element=True, states={}, **extra)


def snapshot():
    root = element(50032, 'Window <&>', [100, 200, 300, 180])
    root['children'] = [element(50000, 'Add…', [120, 225, 90, 30]),
                        element(50002, 'Keep semantics', [120, 280, 190, 22]),
                        element(50020, 'Exact UIA spelling', [120, 320, 180, 20])]
    root['children'][1]['states'] = {'toggle': 1}
    return dict(version=1, screen_bounds=[100, 200, 300, 180], image_size=[300, 180], root=root)


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
            image, uia, output = d/'capture.png', d/'capture.uia.json', d/'capture.svg'
            Image.new('RGB', (300, 180), 'white').save(image)
            uia.write_text(json.dumps(snapshot()))
            self.assertEqual(main([str(output), '--image', str(image), '--uia', str(uia), '--no-ocr', '--html', str(d/'capture.html')]), 0)
            svg = ET.parse(output)
            self.assertEqual(svg.getroot().get('role'), 'graphics-document group')
            self.assertNotIn('<image ', output.read_text())
            s = snapshot(); s['image_size'] = [5, 5]; uia.write_text(json.dumps(s))
            self.assertEqual(main([str(output), '--image', str(image), '--uia', str(uia), '--no-ocr']), 1)

    def test_password_ocr_is_not_emitted(self):
        s = snapshot()
        item = element(50004, 'Password', [120, 225, 90, 30], password=True)
        s['root']['children'] = [item]
        scene = Node('root', (0, 0, 300, 180), children=[Node('text', (25, 30, 60, 12), text='masked')])
        merge_uia(scene, Image.new('RGB', (300, 180)), s)
        self.assertFalse(any(n.kind == 'text' for n in flatten(scene)))


if __name__ == '__main__':
    unittest.main()
