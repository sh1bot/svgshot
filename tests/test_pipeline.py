import re
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image

from svgshot.fixture import generate
from svgshot.model import flatten
from svgshot.recognize import Options, reconstruct
from svgshot.svg import to_svg
from svgshot.validate import compare, ground_truth


class PipelineTest(unittest.TestCase):
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
