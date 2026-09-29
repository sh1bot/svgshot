import re
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFont

from svgshot.diagnostic import make_diagnostic
from svgshot.fixture import generate
from svgshot.model import flatten
from svgshot.recognize import Options, reconstruct
from svgshot.svg import to_svg
from svgshot.validate import compare, ground_truth


class PipelineTest(unittest.TestCase):
    def test_radio_state_and_white_panel_structure(self):
        image = Image.new("RGB", (420, 270), "#f0f0f0")
        draw = ImageDraw.Draw(image)
        draw.rectangle((1, 1, 418, 59), fill="white")
        draw.rectangle((109, 60, 110, 268), fill="white")
        draw.line((111, 227, 418, 227), fill="white")
        image_without_close = image.copy()
        draw.line((398, 10, 407, 19), fill="#222222")
        draw.line((407, 10, 398, 19), fill="#222222")
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
        for y, label, selected in ((95, "Publisher", False),
                                   (142, "Path", True),
                                   (189, "File hash", False)):
            draw.ellipse((130, y, 142, y+12), fill="white", outline="#555555")
            if selected:
                draw.ellipse((134, y+4, 138, y+8), fill="#555555")
            draw.text((151, y-1), label, font=font, fill="#222222")
        scene = reconstruct(image, Options())
        nodes = list(flatten(scene))
        radios = [n for n in nodes if n.kind in ("radio", "radio-selected")]
        self.assertEqual([n.kind for n in sorted(radios, key=lambda n:n.box[1])],
                         ["radio", "radio-selected", "radio"])
        self.assertEqual([n.box for n in radios],
                         [(130,95,13,13), (130,142,13,13), (130,189,13,13)])
        self.assertIn((1,1,418,59), [n.box for n in nodes if n.kind == "rect"])
        self.assertIn((109,60,2,208), [n.box for n in nodes if n.kind == "line"])
        self.assertIn((109,227,310,1), [n.box for n in nodes if n.kind == "line"])
        self.assertEqual([n.box for n in nodes if n.kind == "close-icon"], [(398,10,10,10)])
        self.assertFalse(any(n.kind == "close-icon" for n in
                             flatten(reconstruct(image_without_close, Options(ocr=False)))))
        svg = ElementTree.fromstring(to_svg(scene))
        self.assertEqual(sum(n.tag.endswith("circle") for n in svg.iter()), 4)
        self.assertEqual(len([n for n in svg.iter() if n.get("data-kind") == "close-icon"]), 1)

    def test_framed_selection_and_disabled_button(self):
        image = Image.new("RGB", (360, 210), "#f0f0f0")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 359, 209), outline="#707070")
        draw.rectangle((10, 46, 185, 65), fill="#0078d7")
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
        draw.text((18, 48), "Conditions", font=font, fill="white")
        draw.rectangle((220, 160, 315, 187), fill="#dddddd", outline="#999999")
        draw.text((240, 165), "Create", font=font, fill="#777777")
        scene = reconstruct(image, Options())
        self.assertEqual(scene.color, "#f0f0f0")
        self.assertEqual({n.text for n in flatten(scene) if n.kind == "text"},
                         {"Conditions", "Create"})
        svg = to_svg(scene)
        self.assertIn('fill="#ffffff"', svg)
        self.assertIn('>Create</text>', svg)
        self.assertIn('textLength="62"', svg)
        diagnostic = make_diagnostic(image, svg, scene)
        xml = ElementTree.fromstring(diagnostic)
        self.assertEqual(len([n for n in xml.iter() if n.tag.endswith("image") and
                              n.get("x") == "0" and n.get("y") == "28"]), 1)
        self.assertEqual(len([n for n in xml.iter() if n.tag.endswith("line")]), 2)
        self.assertEqual(len([n for n in xml.iter() if n.tag.endswith("title")]), 4)

    def test_known_light_and_dark_controls(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme), tempfile.TemporaryDirectory() as directory:
                png, manifest, svg = (Path(directory) / name for name in
                                      ("input.png", "truth.json", "output.svg"))
                generate(png, manifest, theme)
                source = Image.open(png)
                scene = reconstruct(source, Options())
                svg.write_text(to_svg(scene), encoding="utf-8")
                self.assertEqual(ground_truth(scene, str(manifest))["matched"], 10)
                self.assertIn("radio-group", [node.kind for node in flatten(scene)])
                self.assertIn("radio-selected", [node.kind for node in flatten(scene)])
                self.assertEqual(sum(node.kind == "raster" for node in flatten(scene)), 0)
                xml = ElementTree.parse(svg)
                texts = [element.text for element in xml.iter() if element.tag.endswith("text")]
                self.assertIn("Apply", texts)
                report = compare(source, str(svg), scene, str(manifest))
                self.assertEqual(report["text"]["recall"], 1.0)
                self.assertGreater(report["geometry"]["edge_recall_4px"], .9)
                self.assertEqual(report["warnings"], [])

    def test_validation_catches_missing_label_and_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            png, manifest, svg = (Path(directory) / name for name in
                                  ("input.png", "truth.json", "output.svg"))
            generate(png, manifest)
            source = Image.open(png)
            scene = reconstruct(source, Options())
            content = to_svg(scene)
            missing = re.sub(r"<text[^>]*>Apply</text>", "", content)
            self.assertNotEqual(missing, content)
            svg.write_text(missing, encoding="utf-8")
            report = compare(source, str(svg), scene)
            self.assertIn("Apply", report["text"]["missing"])
            blank = ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400">'
                     '<rect width="640" height="400" fill="#f7f7f7"/></svg>')
            svg.write_text(blank, encoding="utf-8")
            report = compare(source, str(svg), scene)
            self.assertLess(report["geometry"]["edge_recall_4px"], .65)
            self.assertTrue(report["warnings"])


if __name__ == "__main__":
    unittest.main()
