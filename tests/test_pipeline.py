import re
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from svgshot.diagnostic import make_diagnostic
from svgshot.fixture import generate
from svgshot.model import Node, flatten
from svgshot.recognize import (Options, _blue_window_surfaces,
                               _decorative_background, _large_beveled_buttons,
                               _embedded_panels, _focused_controls, _disabled_buttons,
                               _footer_surface,
                               _multicolor_marks, _window_close_controls,
                               _clipped_outline,
                               _window_shell, _refine_ambiguous_text,
                               _covered_small_fragment,
                               reconstruct)
from svgshot.svg import to_svg
from svgshot.validate import (add_fidelity_regions, compare, ground_truth,
                              render, semantic_scorecard)


class PipelineTest(unittest.TestCase):
    def test_partial_border_at_capture_edge_is_not_a_panel(self):
        clipped = Node("outline", (182, 574, 577, 68))
        complete = Node("outline", (100, 100, 200, 80))
        self.assertTrue(_clipped_outline(clipped, 766, 643))
        self.assertFalse(_clipped_outline(complete, 766, 643))

    def test_ambiguous_ocr_is_retried_as_a_local_label(self):
        source = Image.new("RGB", (100, 60), "white")
        text = Node("text", (20, 20, 40, 9), text="see.", confidence=.55)
        local = Node("text", (8, 4, 40, 9), text="Select...", confidence=.92)
        with patch("svgshot.recognize._ocr_ui", return_value=[local]):
            _refine_ambiguous_text(source, [text], "eng")
        self.assertEqual(text.text, "Select...")
        self.assertEqual(text.box, (20, 20, 40, 9))
        self.assertEqual(text.confidence, .92)

    def test_selected_row_ocr_suppresses_overlapping_fragment(self):
        fragment = Node("text", (66, 135, 10, 6), text="ns", confidence=.81)
        selected = Node("text", (14, 132, 62, 9), text="Permissions", confidence=.96)
        self.assertTrue(_covered_small_fragment(fragment, [selected]))

    def test_thresholded_ocr_recovers_small_dialog_labels(self):
        image=Image.new("RGB",(457,251),"#f0f0f0")
        draw=ImageDraw.Draw(image)
        draw.rectangle((1,1,455,30),fill="white")
        draw.rectangle((13,106,348,125),fill="#e1e1e1",outline="#adadad")
        draw.rectangle((13,156,348,210),fill="white",outline="#707070")
        draw.rectangle((357,106,443,125),fill="#e1e1e1",outline="#adadad")
        font=ImageFont.truetype("DejaVuSans.ttf",10)
        draw.text((16,109),"ERIC-DESKTOP",font=font,fill="#222222")
        draw.text((15,158),"administrators",font=font,fill="#222222")
        draw.text((371,110),"Locations...",font=font,fill="#222222")
        scene=reconstruct(image,Options())
        labels={n.text for n in flatten(scene) if n.kind=="text"}
        self.assertIn("administrators",labels)
        self.assertIn("Locations...",labels)

    def test_semantic_field_selection_and_footer(self):
        image=Image.new("RGB",(400,206),"white")
        draw=ImageDraw.Draw(image)
        draw.rectangle((1,144,398,204),fill="#f0f0f0")
        draw.rectangle((64,100,383,122),outline="#0078d7")
        draw.rectangle((70,103,128,117),fill="#0078d7")
        pixels=np.asarray(image)
        controls,_=_focused_controls(pixels,image,"eng",False)
        footer=_footer_surface(pixels)
        self.assertEqual([n.kind for n in controls],["input-field","text-selection"])
        self.assertIsNotNone(footer)
        scene=Node("window",(0,0,400,206),"#ffffff",children=[footer,*controls])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"field.svg"
            path.write_text(to_svg(scene),encoding="utf-8")
            drawn=render(str(path),400)
        baseline=semantic_scorecard(image,Image.new("RGB",image.size,"white"),
                                    Node("window",(0,0,400,206),"#ffffff"),
                                    {"edge_recall_4px":.5,"edge_precision_4px":1,
                                     "blurred_color_error":.1},"eng",False)
        improved=semantic_scorecard(image,drawn,scene,
                                    {"edge_recall_4px":1,"edge_precision_4px":1,
                                     "blurred_color_error":0},"eng",False)
        self.assertEqual({f["kind"] for f in baseline["findings"]},
                         {"input-field","text-selection","footer-panel"})
        self.assertEqual(improved["findings"],[])

    def test_semantic_list_pane_dividers_and_disabled_buttons(self):
        image=Image.new("RGB",(600,440),"#f0f0f0")
        draw=ImageDraw.Draw(image)
        draw.rectangle((1,150,141,167),fill="#0078d7")
        draw.rectangle((150,220,400,350),fill="white",outline="#828790")
        for x in (300,350):
            draw.line((x,222,x,245),fill="#e5e5e5")
        for y in (250,282):
            draw.rectangle((450,y,550,y+24),fill="#cccccc",outline="#bfbfbf")
        pixels=np.asarray(image)
        panels=_embedded_panels(pixels)
        buttons=_disabled_buttons(pixels)
        controls,_=_focused_controls(pixels,image,"eng",False)
        self.assertEqual(len(panels),1)
        self.assertEqual(len(panels[0].children),2)
        self.assertEqual(len(buttons),2)
        self.assertEqual([n.kind for n in controls],["selected-row"])
        scene=Node("window",(0,0,600,440),"#f0f0f0",children=[*panels,*buttons,*controls])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"pane.svg"
            path.write_text(to_svg(scene),encoding="utf-8")
            drawn=render(str(path),600)
        score=semantic_scorecard(image,drawn,scene,
                                 {"edge_recall_4px":1,"edge_precision_4px":1,
                                  "blurred_color_error":0},"eng",False)
        self.assertFalse(score["findings"])

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
        self.assertFalse(any(n.kind == "dropdown" for n in nodes))
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

    def test_close_icon_remains_visible_over_header_surface(self):
        scene = Node("window", (0,0,200,80), "#f0f0f0", children=[
            Node("close-icon", (180,10,10,10), "#111111"),
            Node("rect", (1,1,198,30), "#ffffff")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"close.svg"
            path.write_text(to_svg(scene),encoding="utf-8")
            result = render(str(path),200)
            self.assertLess(max(result.getpixel((180,10))),80)
            self.assertEqual(result.getpixel((185,5)),(255,255,255))

    def test_close_controls_on_both_sides_of_offset_windows(self):
        image = Image.new("RGB",(420,300),"white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((100,110,358,139),fill="#1745bb")
        draw.rectangle((333,113,352,135),fill="#eeeeee")
        draw.line((337,117,347,127),fill="#111111",width=2)
        draw.line((347,117,337,127),fill="#111111",width=2)
        draw.rectangle((30,40,300,73),fill="#eeeeee")
        draw.ellipse((42,49,55,62),fill="#ed5d55")
        draw.rectangle((30,76,300,104),fill="#eeeeee")
        draw.line((42,83,52,93),fill="#111111")
        draw.line((52,83,42,93),fill="#111111")
        titles = [Node("text",(110,115,60,13),text="Message"),
                  Node("text",(108,48,95,13),text="Preferences"),
                  Node("text",(150,82,80,13),text="Settings")]
        icons = _window_close_controls(np.asarray(image),titles)
        self.assertTrue(any(n.kind == "close-icon" and 335 <= n.box[0] <= 340
                            for n in icons))
        self.assertTrue(any(n.kind == "close-dot" and n.box[0] == 42 for n in icons))
        self.assertTrue(any(n.kind == "close-icon" and n.box[0] == 42 for n in icons))

    def test_fidelity_repair_reports_raster_coverage(self):
        image = Image.new("RGB",(288,288),"white")
        ImageDraw.Draw(image).rectangle((18,18,170,105),fill="#2449b5")
        scene = Node("window",(0,0,288,288),"#ffffff")
        info = add_fidelity_regions(image,scene,to_svg(scene))
        self.assertIsNotNone(info)
        self.assertGreater(info["raster_coverage"],.1)
        self.assertLess(info["raster_coverage"],1)
        with tempfile.TemporaryDirectory() as directory:
            svg = Path(directory)/"repaired.svg"
            svg.write_text(to_svg(scene),encoding="utf-8")
            self.assertEqual(render(str(svg),288).getpixel((50,50)),(36,73,181))

    def test_wide_buttons_and_single_title_gradient_stay_separate(self):
        self.assertFalse(Options().fidelity_fallback)
        image = Image.new("RGB",(700,500),"#cecece")
        draw = ImageDraw.Draw(image)
        for x in range(50,650):
            t = (x-50)/600
            draw.line((x,50,x,99),fill=(int(10+50*t),int(65+80*t),int(170+40*t)))
        font = ImageFont.truetype("DejaVuSans.ttf",28)
        for x,label in ((100,"OK"),(370,"Cancel")):
            draw.rectangle((x,390,x+229,454),fill="#cecece")
            draw.rectangle((x,390,x+225,397),fill="#f4f4f4")
            draw.line((x+227,390,x+227,454),fill="#202020",width=4)
            draw.line((x,452,x+229,452),fill="#202020",width=4)
            draw.text((x+75,408),label,fill="black",font=font)
        pixels = np.asarray(image)
        bars = _blue_window_surfaces(pixels)
        buttons = _large_beveled_buttons(pixels,image,"eng")
        self.assertEqual(len([n for n in bars if n.kind == "gradient-title"]),1)
        self.assertEqual([n.children[0].text for n in buttons],["OK","Cancel"])
        scene = Node("window",(0,0,700,500),"#cecece",children=bars+buttons)
        svg = to_svg(scene)
        self.assertEqual(svg.count('data-kind="gradient-title"'),1)
        self.assertEqual(svg.count('data-kind="beveled-button"'),2)
        self.assertNotIn('data-kind="fidelity-raster"',svg)

    def test_semantic_imagery_scoring_finds_missing_vector_regions(self):
        image = Image.new("RGB",(400,400),"#f1eef3")
        draw = ImageDraw.Draw(image)
        draw.rectangle((1,1,398,29),fill="white")
        draw.text((10,9),"Preferences",fill="black",
                  font=ImageFont.truetype("DejaVuSans.ttf",12))
        draw.rectangle((1,30,398,398),fill="#dceef9")
        draw.rectangle((250,30,398,398),fill="#f2ecf3")
        draw.polygon(((1,210),(200,320),(170,398),(1,398)),fill="#f8fdfd")
        draw.rectangle((70,100,329,299),fill="white")
        for x,y,color in ((100,140,"#f25022"),(113,140,"#7fba00"),
                          (100,153,"#00a4ef"),(113,153,"#ffb900")):
            draw.rectangle((x,y,x+11,y+11),fill=color)
        draw.text((100,184),"someone@example.com",fill="#555555",
                  font=ImageFont.truetype("DejaVuSans.ttf",12))
        draw.line((100,206,305,206),fill="#666666")
        pixels = np.asarray(image)
        mark = _multicolor_marks(pixels)
        backdrop = _decorative_background(pixels)
        self.assertEqual(len(mark),1)
        self.assertIsNotNone(backdrop)
        shell,title = _window_shell(pixels,image,"eng",True)
        self.assertEqual([n.kind for n in shell],["window-header","dialog-panel"])
        self.assertEqual(title.text,"Preferences")
        from svgshot.recognize import _input_underlines, _ocr
        rule = _input_underlines(pixels,_ocr(image,"eng"),shell[-1].box)
        self.assertEqual(len(rule),1)
        scene = Node("window",(0,0,400,400),"#ffffff",
                     children=[backdrop,*shell,*mark,title,*rule])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"vector.svg"
            path.write_text(to_svg(scene),encoding="utf-8")
            drawn = render(str(path),400)
        baseline = semantic_scorecard(image,Image.new("RGB",image.size,"white"),
                                      Node("window",(0,0,400,400),"#ffffff"),
                                      {"edge_recall_4px":.5},"eng",True)
        improved = semantic_scorecard(image,drawn,scene,
                                      {"edge_recall_4px":.5},"eng",True)
        self.assertEqual({f["kind"] for f in baseline["findings"]},
                         {"dialog-panel","window-title","input-underline",
                          "missing-vector-icon","decorative-region"})
        self.assertGreater(improved["imagery"],baseline["imagery"])
        self.assertIn("input-underline",{r["kind"] for r in improved["layout_regions"]})
        self.assertFalse(any(n.kind == "raster" for n in scene.children))

    def test_dialog_tabs_checkbox_dropdown_and_footer(self):
        image = Image.new("RGB",(361,321),"#f0f0f0")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("DejaVuSans.ttf",10)
        draw.rectangle((0,0,360,320),outline="#707070")
        draw.rectangle((1,1,359,30),fill="white")
        draw.rectangle((7,38,82,59),fill="white",outline="#d9d9d9")
        draw.rectangle((82,40,139,58),fill="#f0f0f0",outline="#d9d9d9")
        draw.line((140,58,350,58),fill="#d9d9d9")
        draw.line((8,59,350,59),fill="white")
        draw.text((15,43),"Enforcement",font=font,fill="black")
        draw.text((87,43),"Advanced",font=font,fill="black")
        draw.rectangle((23,100,335,196),outline="#dcdcdc")
        draw.text((32,96),"Executable rules:",font=font,fill="black")
        draw.rectangle((40,115,52,127),fill="white",outline="#333333")
        draw.line((43,121,46,124,50,118),fill="#333333")
        draw.text((58,115),"Configured",font=font,fill="black")
        draw.rectangle((37,140,319,160),fill="#e1e1e1",outline="#adadad")
        draw.text((41,144),"Enforce rules",font=font,fill="black")
        draw.line((307,148,311,152,315,148),fill="#666666")
        for x,caption in ((199,"Cancel"),(280,"Apply")):
            draw.rectangle((x,291,x+72,311),fill="#e1e1e1",outline="#adadad")
            draw.text((x+16,296),caption,font=font,fill="black")
        nodes = list(flatten(reconstruct(image,Options())))
        self.assertEqual(sum(n.kind == "tab-active" for n in nodes),1)
        self.assertEqual(sum(n.kind == "tab" for n in nodes),1)
        self.assertEqual([n.box for n in nodes if n.kind == "checkbox-selected"],
                         [(40,115,13,13)])
        self.assertEqual([n.box for n in nodes if n.kind == "dropdown"],
                         [(37,140,283,21)])
        self.assertIn("Cancel",[n.text for n in nodes if n.kind == "text"])

    def test_validation_checks_input_text_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            png, manifest, svg = (Path(directory)/name for name in
                                  ("input.png","truth.json","output.svg"))
            generate(png,manifest)
            source = Image.open(png)
            scene = reconstruct(source,Options())
            for node in flatten(scene):
                if node.kind == "text" and node.text == "Apply":
                    node.text = "Banana"
            svg.write_text(to_svg(scene),encoding="utf-8")
            report = compare(source,str(svg),scene)
            self.assertEqual(report["text"]["recall"],1.0)
            self.assertLess(report["source_text"]["recall"],1.0)
            self.assertIn("Apply",report["source_text"]["missing"])

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
