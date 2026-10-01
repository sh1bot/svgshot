"""Developer reference for ``--report`` metrics.

``geometry`` measures edge recall/precision within four pixels after masking
text boxes, plus blurred RGB error. ``text`` checks rendered OCR against the
reconstructed scene; ``source_text`` uses a separate OCR pass over the input.
Neither OCR pass is ground truth. ``scorecard`` checks selected layout, text and
image regions; ``ground_truth`` compares detected elements to an optional
hand-authored fixture manifest. Fidelity fields report the area covered by
source-image fallback regions.

The combined score is a heuristic for ranking iterations, not a calibrated
measure of correctness. Region checks cover only patterns they recognize, and
OCR can miss or misread text. Treat individual findings and known-fixture
results as more useful evidence than a passing global score.
"""
from __future__ import annotations

import difflib
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from .model import Node, flatten
from .recognize import (_ocr, _raster, _multicolor_marks, _input_underlines,
                        _embedded_panels, _focused_controls, _disabled_buttons, _footer_surface,
                        _header_illustration, _titlebar_icon,
                        _decorative_background, _white_card, _window_shell)
from .svg import to_svg


def render(svg_path: str, width: int) -> Image.Image:
    if not shutil.which("inkscape"):
        raise RuntimeError("Comparison needs Inkscape on PATH to render the SVG")
    process = subprocess.run(
        ["inkscape", svg_path, "--export-type=png", "--export-filename=-",
         f"--export-width={width}"], capture_output=True,
    )
    if process.returncode:
        raise RuntimeError("SVG rendering failed: " + process.stderr.decode(errors="replace"))
    return Image.open(io.BytesIO(process.stdout)).convert("RGB")


def add_fidelity_regions(source: Image.Image, scene: Node, svg: str,
                         font_family: str = "auto") -> dict | None:
    """Retain source crops where vector reconstruction visibly fails.

    These overlays are intentionally explicit scene nodes, so reports can
    distinguish visual fidelity from editable reconstruction.
    """
    if not shutil.which("inkscape"):
        return None
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/"draft.svg"
        path.write_text(svg, encoding="utf-8")
        candidate = render(str(path),source.width)
    original = np.asarray(source.convert("RGB"),dtype=np.float32)
    drawn = np.asarray(candidate.resize(source.size),dtype=np.float32)
    a = ndimage.gaussian_filter(original,(1.5,1.5,0))
    b = ndimage.gaussian_filter(drawn,(1.5,1.5,0))
    error = np.mean(np.abs(a-b),axis=2)
    initial = float(error.mean()/255)
    text_nodes = [n for n in flatten(scene) if n.kind == "text"]
    edge_recall = _geometry(source,candidate,text_nodes)["edge_recall_4px"]
    if initial < .025 and edge_recall >= .75:
        return {"initial_blurred_color_error":round(initial,3),
                "initial_edge_recall_4px":edge_recall,
                "raster_coverage":0.0,"regions":0}
    height,width = error.shape
    tile = 96
    rows,cols = (height+tile-1)//tile,(width+tile-1)//tile
    bad = np.zeros((rows,cols),dtype=bool)
    for row in range(rows):
        for col in range(cols):
            patch = error[row*tile:min(height,(row+1)*tile),
                          col*tile:min(width,(col+1)*tile)]
            bad[row,col] = float(patch.mean()) > (4 if initial < .025 else 6)
    regions = 0
    covered = 0
    # Merge only complete rectangular runs; bounding a ragged component would
    # erase vector work inside its good tiles.
    for row in range(rows):
        col = 0
        while col < cols:
            if not bad[row,col]:
                col += 1
                continue
            right = col
            while right < cols and bad[row,right]:
                right += 1
            bottom = row+1
            while bottom < rows and bad[bottom,col:right].all():
                bottom += 1
            bad[row:bottom,col:right] = False
            x,y = col*tile,row*tile
            w,h = min(width,right*tile)-x,min(height,bottom*tile)-y
            scene.children.append(Node("fidelity-raster",(x,y,w,h),
                                       confidence=0.0,
                                       image_data=_raster(source,(x,y,w,h))))
            regions += 1
            covered += w*h
            col = right
    return {"initial_blurred_color_error":round(initial,3),
            "initial_edge_recall_4px":edge_recall,
            "raster_coverage":round(covered/(width*height),3),
            "regions":regions}


def _edge(image: np.ndarray) -> np.ndarray:
    gray = image @ np.array([.2126, .7152, .0722])
    gray = ndimage.gaussian_filter(gray, .8)
    gradient = np.hypot(ndimage.sobel(gray, axis=0), ndimage.sobel(gray, axis=1))
    return gradient > max(40, np.percentile(gradient, 88))


def _geometry(source: Image.Image, rendered: Image.Image, texts: list[Node]) -> dict:
    src = np.asarray(source.convert("RGB"), dtype=np.float32)
    dst = np.asarray(rendered.resize(source.size).convert("RGB"), dtype=np.float32)
    a, b = _edge(src), _edge(dst)
    # Text metrics are separate: mask both source and rendered ink areas with
    # room for different font metrics and subpixel placement.
    ignore = np.zeros(a.shape, dtype=bool)
    height, width = ignore.shape
    for text in texts:
        x, y, w, h = text.box
        ignore[max(0,y-8):min(height,y+h+9), max(0,x-8):min(width,x+w+18)] = True
    a &= ~ignore
    b &= ~ignore
    distances_a = ndimage.distance_transform_edt(~a)
    distances_b = ndimage.distance_transform_edt(~b)
    # Fraction of edges within 4 pixels of an edge in the other image.
    recall = float(np.mean(distances_b[a] <= 4)) if a.any() else 1.0
    precision = float(np.mean(distances_a[b] <= 4)) if b.any() else 1.0
    softened_a = ndimage.gaussian_filter(src, (3, 3, 0))
    softened_b = ndimage.gaussian_filter(dst, (3, 3, 0))
    color_error = float(np.mean(np.abs(softened_a-softened_b)) / 255)
    # A global score can hide a bad footer in a large, mostly empty dialog.
    regions = []
    for row in range(3):
        for col in range(2):
            x0, x1 = width*col//2, width*(col+1)//2
            y0, y1 = height*row//3, height*(row+1)//3
            source_edges = a[y0:y1,x0:x1]
            if source_edges.sum() < 12:
                continue
            near = distances_b[y0:y1,x0:x1][source_edges] <= 4
            regions.append({"box": [x0,y0,x1-x0,y1-y0],
                            "edge_recall_4px": round(float(near.mean()),3),
                            "source_edges": int(source_edges.sum())})
    return {"edge_recall_4px": round(recall, 3),
            "edge_precision_4px": round(precision, 3),
            "blurred_color_error": round(color_error, 3),
            "regions": regions}


def _text(source: list[Node], rendered: list[Node], image: Image.Image,
          language: str, threshold: float = .75) -> dict:
    found = set()
    matches = []
    for item in source:
        best = (0.0, None)
        for i, candidate in enumerate(rendered):
            if i in found:
                continue
            similarity = difflib.SequenceMatcher(None, item.text.casefold(), candidate.text.casefold()).ratio()
            dx = abs(item.box[0]-candidate.box[0])
            dy = abs(item.box[1]-candidate.box[1])
            if dx > max(20, item.box[2]*.3) or dy > max(12, item.box[3]):
                continue
            if similarity > best[0]:
                best = similarity, i
        if best[1] is not None and best[0] >= threshold:
            found.add(best[1])
            matches.append(item.text)
            continue
        # A footer or menu row may become several SVG text nodes. Compare the
        # joined reading when their baselines and combined span agree.
        row = sorted(((i,candidate) for i,candidate in enumerate(rendered)
                      if i not in found and abs(item.box[1]-candidate.box[1]) <= max(12,item.box[3]) and
                      item.box[0]-20 <= candidate.box[0] <= item.box[0]+item.box[2]+20),
                     key=lambda pair:pair[1].box[0])
        if len(row)>=2:
            joined = " ".join(candidate.text for _,candidate in row)
            import re
            normalize = lambda s:re.sub(r"[^\w\s]", "",s.casefold())
            if difflib.SequenceMatcher(None,normalize(item.text),normalize(joined)).ratio() >= threshold:
                found.update(i for i,_ in row)
                matches.append(item.text)
                continue
        x, y, w, h = item.box
        left, top = max(0,x-6), max(0,y-6)
        right, bottom = min(image.width,x+w+16), min(image.height,y+h+9)
        if right > left and bottom > top:
            local = _ocr(image.crop((left,top,right,bottom)), language, psm=7)
            if any(difflib.SequenceMatcher(None, item.text.casefold(),
                                          candidate.text.casefold()).ratio() >= threshold
                   for candidate in local):
                matches.append(item.text)
    return {"source_lines": len(source), "rendered_lines": len(rendered),
            "matched_lines": len(matches),
            "recall": round(len(matches)/len(source), 3) if source else None,
            "missing": [item.text for item in source if item.text not in matches]}


def _iou(a, b) -> float:
    x1, y1, w1, h1 = a
    x2, y2, w2, h2 = b
    overlap = max(0, min(x1+w1,x2+w2)-max(x1,x2))*max(0,min(y1+h1,y2+h2)-max(y1,y2))
    return overlap/(w1*h1+w2*h2-overlap) if overlap else 0.0


def ground_truth(scene: Node, manifest_path: str) -> dict:
    """Compare detected primitives with an independent fixture manifest."""
    with open(manifest_path, encoding="utf-8") as stream:
        expected = json.load(stream)["elements"]
    actual = list(flatten(scene))
    used = set()
    results = []
    aliases = {"radio": {"radio", "ring", "circle"},
               "radio-selected": {"radio-selected", "circle"},
               "button": {"button", "outlined-button", "rect", "outline"},
               "checkbox": {"checkbox", "outline", "rect"}}
    for element in expected:
        kind = element["kind"]
        possibilities = aliases.get(kind, {kind})
        scores = []
        for i, node in enumerate(actual):
            if i in used or node.kind not in possibilities:
                continue
            if kind == "text":
                similarity = difflib.SequenceMatcher(None, element["text"].casefold(),
                                                      node.text.casefold()).ratio()
                score = similarity * .7 + _iou(element["box"],node.box) * .3
            else:
                score = _iou(element["box"], node.box)
            scores.append((score, i))
        score, match = max(scores, default=(0, None))
        passed = score >= (.55 if kind == "text" else .5)
        if passed:
            used.add(match)
        results.append({"kind": kind, "text": element.get("text", ""),
                        "score": round(score, 3), "matched": passed})
    return {"matched": sum(e["matched"] for e in results),
            "expected": len(results), "elements": results}


def _clean_ocr(value: str) -> str:
    import re
    value = re.sub(r"^i(?=[A-Z][a-z])", "",value.strip(" ‘'|:;,."))
    return " ".join(value.casefold().split())


def _local_text_checks(source: Image.Image, rendered: Image.Image,
                       scene: Node, language: str) -> dict:
    checks = []
    for node in flatten(scene):
        if node.kind != "text" or len(node.text) < 8 or node.confidence < .7:
            continue
        x,y,w,h = node.box
        left,top = max(0,x-10),max(0,y-1)
        right,bottom = min(source.width,x+w+2),min(source.height,y+h+1)
        if right-left < 20 or bottom-top < 6:
            continue
        box = (left,top,right,bottom)
        expected = _ocr(source.crop(box),language,psm=7)
        observed = _ocr(rendered.crop(box),language,psm=7)
        if len(expected)!=1 or expected[0].confidence<.8:
            continue
        a = _clean_ocr(expected[0].text)
        b = _clean_ocr(observed[0].text) if len(observed)==1 else ""
        if len(a)<8:
            continue
        similarity = difflib.SequenceMatcher(None,a,b).ratio()
        first_a,first_b = a.split()[0],b.split()[0] if b else ""
        # A control or preceding label can enter the expanded crop in only
        # one rendering. The reference text is intact when it is a suffix.
        extra_left_context = b.endswith(a) and b!=a
        first_mismatch = (not extra_left_context and len(first_a)>=3 and
                          difflib.SequenceMatcher(None,first_a,first_b).ratio()<=.8)
        checks.append({"box":[left,top,right-left,bottom-top],"source":a,
                       "rendered":b,"similarity":round(similarity,3),
                       "fault":(similarity<.84 and not extra_left_context) or first_mismatch})
    return {"checked":len(checks),"faults":[c for c in checks if c["fault"]],
            "mean_similarity":round(float(np.mean([c["similarity"] for c in checks])),3)
            if checks else None}


def semantic_scorecard(source: Image.Image, rendered: Image.Image, scene: Node,
                       geometry: dict, language: str, check_text: bool) -> dict:
    """Score proposed semantic image regions separately from text and edges."""
    src = np.asarray(source.convert("RGB"))
    dst = np.asarray(rendered.resize(source.size).convert("RGB"))
    source_marks = _multicolor_marks(src)
    output_marks = _multicolor_marks(dst)
    scene_nodes = list(flatten(scene))
    findings = []
    regions = []
    layout = []
    title_icon=_titlebar_icon(src)
    if title_icon:
        box=title_icon.box
        vector=any(n.kind=="title-icon" and _iou(n.box,box)>.6
                   for n in scene_nodes)
        raster=any(n.kind in ("raster","fidelity-raster") and
                   _iou(n.box,box)>.2 for n in scene_nodes)
        score=float(vector and not raster)
        regions.append({"kind":"title-icon","box":list(box),"score":score,
                        "vector":bool(score)})
        if not score:
            findings.append({"kind":"title-icon","box":list(box),
                             "message":"Small window icon is missing or raster-backed"})
    icon=_header_illustration(src)
    if icon:
        box=icon.box
        vector=any(n.kind=="header-icon" and _iou(n.box,box)>.7
                   for n in scene_nodes)
        raster=any(n.kind in ("raster","fidelity-raster") and
                   _iou(n.box,box)>.08 for n in scene_nodes)
        x,y,w,h=box
        original=src[y:y+h,x:x+w].astype(float)
        visible=dst[y:y+h,x:x+w].astype(float)
        color_error=float(np.mean(np.abs(ndimage.gaussian_filter(original,(1,1,0))-
                                         ndimage.gaussian_filter(visible,(1,1,0))))/255)
        score=round(.6*int(vector and not raster)+.4*max(0,1-color_error/.22),3)
        regions.append({"kind":"header-icon","box":list(box),"score":score,
                        "vector":vector and not raster,
                        "blurred_color_error":round(color_error,3)})
        if not vector or raster or score<.68:
            findings.append({"kind":"header-icon","box":list(box),
                             "message":"Heading illustration is missing or raster-backed"})
    source_controls,control_labels = _focused_controls(src,source,language,check_text)
    output_controls,_ = _focused_controls(dst,rendered,language,False)
    footer=_footer_surface(src)
    for expected in _embedded_panels(src)+_disabled_buttons(src)+source_controls+([footer] if footer else []):
        kind,box=expected.kind,expected.box
        nodes = (_embedded_panels(dst) if kind=="content-panel" else
                 _disabled_buttons(dst) if kind=="disabled-button" else
                 [_footer_surface(dst)] if kind=="footer-panel" else output_controls)
        nodes=[n for n in nodes if n is not None]
        visible = any(n.kind==kind and _iou(n.box,box)>.75 for n in nodes)
        vector = any(n.kind==kind and _iou(n.box,box)>.75 for n in scene_nodes)
        score=.5*int(visible)+.5*int(vector)
        entry={"kind":kind,"box":list(box),"score":score,
               "vector":vector,"visible":visible}
        if kind=="input-field" or kind=="selected-row":
            labels=[label for label in control_labels if _iou(label.box,box)>0]
            if labels:
                label=labels[0]
                retained=any(n.kind=="text" and n.text.casefold()==label.text.casefold() and
                             _iou(n.box,label.box)>.45 for n in scene_nodes)
                crop_box=box
                if kind=="input-field":
                    crop_box=next((n.box for n in source_controls if n.kind=="text-selection"
                                   and _iou(n.box,box)>0),box)
                cx,cy,cw,ch=crop_box
                crop=rendered.crop((cx,cy,cx+cw,cy+ch))
                observed=_ocr(crop,language,psm=7)
                readable=any(difflib.SequenceMatcher(None,label.text.casefold(),
                             n.text.casefold()).ratio()>.8 for n in observed)
                entry.update({"source_text":label.text,"text_vector":retained,
                              "text_readable":readable})
                score=.3*int(visible)+.3*int(vector)+.4*int(retained and readable)
                entry["score"]=round(score,3)
        layout.append(entry)
        if entry["score"]<.85:
            findings.append({"kind":kind,"box":list(box),
                             "message":f"{kind} is missing, displaced, or has unreadable text"})
        if kind=="content-panel":
            visible_dividers=[child for panel in nodes for child in panel.children]
            for divider in expected.children:
                present=any(_iou(divider.box,n.box)>.8 for n in scene_nodes
                            if n.kind=="column-divider")
                visible_line=any(_iou(divider.box,n.box)>.8 for n in visible_dividers)
                value=.5*int(present)+.5*int(visible_line)
                layout.append({"kind":"column-divider","box":list(divider.box),
                               "score":value,"vector":present,"visible":visible_line})
                if value<1:
                    findings.append({"kind":"column-divider","box":list(divider.box),
                                     "message":"List header divider is missing"})
    card = _white_card(src)
    if card:
        output_card = _white_card(dst)
        card_iou = _iou(card,output_card) if output_card else 0.0
        vector = any(n.kind=="dialog-panel" and _iou(n.box,card)>.8
                     for n in scene_nodes)
        score = round(.5*int(vector)+.5*card_iou,3)
        layout.append({"kind":"dialog-panel","box":list(card),"score":score,
                       "vector":vector,"visible_iou":round(card_iou,3)})
        if score<.8:
            findings.append({"kind":"dialog-panel","box":list(card),
                             "message":"Inset white dialog panel is missing as a bounded surface"})
        shell,title = _window_shell(src,source,language,True) if check_text else ([],None)
        if title:
            title_node = next((n for n in scene_nodes if n.kind=="text" and
                               n.vector_data.get("role")=="window-title"),None)
            sx,sy,sw,sh = title.box
            crop = (max(0,sx-5),max(0,sy-4),
                    min(source.width,sx+sw+15),min(source.height,sy+sh+6))
            local = _ocr(rendered.crop(crop),language,psm=7)
            matched = max((difflib.SequenceMatcher(None,_clean_ocr(title.text),
                       _clean_ocr(t.text)).ratio() for t in local),default=0.0)
            position = _iou(title_node.box,title.box) if title_node else 0.0
            header = any(n.kind=="window-header" for n in scene_nodes)
            score = round(.3*int(title_node is not None and header)+.5*matched+
                          .2*position,3)
            layout.append({"kind":"window-title","box":list(title.box),
                           "text":title.text,"score":score,
                           "rendered_similarity":round(matched,3),
                           "bound_to_header":title_node is not None and header})
            if score<.8:
                findings.append({"kind":"window-title","box":list(title.box),
                                 "message":"Outer window title is missing, misread, or unbound from its title strip"})
        source_labels = _ocr(source,language) if check_text else []
        for rule in _input_underlines(src,source_labels,card):
            visible = any(_iou(rule.box,n.box)>.8 for n in
                          _input_underlines(dst,source_labels,_white_card(dst)))
            vector = any(n.kind=="input-underline" and _iou(n.box,rule.box)>.8
                         for n in scene_nodes)
            score = .5*int(visible)+.5*int(vector)
            layout.append({"kind":"input-underline","box":list(rule.box),
                           "score":score,"vector":vector,"visible":visible})
            if score<1:
                findings.append({"kind":"input-underline","box":list(rule.box),
                                 "message":"Email field underline is missing or displaced"})
        for label in source_labels:
            x,y,w,h = label.box
            if not (card[0]<x and card[1]<y and x+w<card[0]+card[2] and
                    y+h<card[1]+card[3] and w>=35):
                continue
            if any(_iou(label.box,mark.box)>.05 for mark in source_marks):
                continue
            area = (slice(y,min(source.height,y+h)),slice(x,min(source.width,x+w)))
            original = src[area].astype(int)
            blue = (original[:,:,2]-original[:,:,0]>55)&(original[:,:,2]-original[:,:,1]>10)
            fraction = float(blue.mean())
            if not .02<fraction<.45:
                continue
            output = dst[area].astype(int)
            output_blue = (output[:,:,2]-output[:,:,0]>55)&(output[:,:,2]-output[:,:,1]>10)
            visible = float(output_blue.mean()) >= fraction*.35
            vector = any(n.kind=="text" and _iou(n.box,label.box)>.45
                         for n in scene_nodes)
            score = .5*int(visible)+.5*int(vector)
            layout.append({"kind":"link-ink","box":list(label.box),
                           "score":score,"vector":vector,"visible":visible})
            if score<1:
                findings.append({"kind":"link-ink","box":list(label.box),
                                 "message":"Link text lost its blue color or vector label"})
    for mark in source_marks:
        box = mark.box
        recognized = any(n.kind=="multicolor-mark" and _iou(n.box,box)>.6
                         for n in scene_nodes)
        visible = any(_iou(n.box,box)>.6 for n in output_marks)
        raster = any(n.kind in ("raster","fidelity-raster") and _iou(n.box,box)>.3
                     for n in scene_nodes)
        score = 1.0 if recognized and visible and not raster else 0.0
        regions.append({"kind":"multicolor-mark","box":list(box),"score":score,
                        "vector":recognized and not raster,"visible":visible})
        if score<1:
            findings.append({"kind":"missing-vector-icon","box":list(box),
                             "message":"Multicolor mark is missing or raster-backed"})
    decoration = _decorative_background(src)
    if decoration:
        x,y,w,h = decoration.box
        mask = np.zeros((source.height,source.width),bool)
        mask[y:y+h,x:x+w] = True
        if card:
            cx,cy,cw,ch = card
            mask[max(0,cy-3):min(source.height,cy+ch+3),
                 max(0,cx-3):min(source.width,cx+cw+3)] = False
        original_blur = ndimage.gaussian_filter(src.astype(float),(12,12,0))
        rendered_blur = ndimage.gaussian_filter(dst.astype(float),(12,12,0))
        color_error = float(np.mean(np.abs(original_blur-rendered_blur)[mask])/255)
        wave_mask = mask.copy()
        wave_mask[:int(source.height*.7)] = False
        wave_mask[:,int(source.width*.72):] = False
        a = (src[:,:,0]>=246)&(src[:,:,1]>=249)&(src[:,:,2]>=250)
        b = (dst[:,:,0]>=246)&(dst[:,:,1]>=249)&(dst[:,:,2]>=250)
        boundary_agreement = float(np.mean(a[wave_mask]==b[wave_mask])) if wave_mask.any() else 1.0
        vector = any(n.kind=="decorative-background" and _iou(n.box,decoration.box)>.7
                     for n in scene_nodes)
        score = round(.25*int(vector)+.35*max(0,1-color_error/.06)+
                      .4*boundary_agreement,3)
        regions.append({"kind":"decorative-background","box":list(decoration.box),
                        "score":score,"vector":vector,
                        "blurred_color_error":round(color_error,3),
                        "shape_agreement":round(boundary_agreement,3)})
        if not vector or score<.75:
            findings.append({"kind":"decorative-region","box":list(decoration.box),
                             "message":"Backdrop is missing as vector art or differs substantially"})
    local = _local_text_checks(source,rendered,scene,language) if check_text else None
    if local:
        for fault in local["faults"]:
            findings.append({"kind":"local-text","box":fault["box"],
                             "message":f'Text differs: {fault["source"]!r} vs {fault["rendered"]!r}'})
    imagery = float(np.mean([r["score"] for r in regions])) if regions else None
    layout_score = float(np.mean([r["score"] for r in layout])) if layout else None
    text_score = local["mean_similarity"] if local and local["checked"] else None
    appearance=max(0,1-geometry.get("blurred_color_error",0)/.08)
    parts = [(geometry["edge_recall_4px"],.25),(appearance,.15),
             (geometry.get("edge_precision_4px",1),.1)]
    if imagery is not None:
        parts.append((imagery,.25))
    if text_score is not None:
        parts.append((text_score,.25))
    if layout_score is not None:
        parts.append((layout_score,.2))
    total = round(sum(value*weight for value,weight in parts)/sum(weight for _,weight in parts),3)
    return {"score":total,"passes":not findings,"fault_count":len(findings),
            "geometry":geometry["edge_recall_4px"],"appearance":round(appearance,3),
            "imagery":round(imagery,3) if imagery is not None else None,
            "layout":round(layout_score,3) if layout_score is not None else None,
            "local_text":local,"regions":regions,"layout_regions":layout,
            "findings":findings}


def compare(source: Image.Image, svg_path: str, scene: Node,
            manifest: str | None = None, ocr: bool = True, language: str = "eng") -> dict:
    rendered = render(svg_path, source.width)
    original_text = [n for n in flatten(scene) if n.kind == "text"]
    report = {"geometry": _geometry(source, rendered, original_text),
              "scene": {"objects": len(list(flatten(scene))) - 1,
                        "text_lines": len(original_text),
                        "raster_islands": sum(n.kind == "raster" for n in flatten(scene)),
                        "fidelity_regions": sum(n.kind == "fidelity-raster" for n in flatten(scene)),
                        "fidelity_raster_coverage": round(sum(n.box[2]*n.box[3]
                            for n in flatten(scene) if n.kind == "fidelity-raster") /
                            (source.width*source.height),3)}}
    if ocr:
        detected = _ocr(rendered, language)
        # Sparse layout mode can omit text in an isolated button. Block mode
        # may catch it, even when sparse mode recognizes the rest correctly.
        for candidate in _ocr(rendered, language, psm=6):
            if not any(_iou(candidate.box, previous.box) > .35 and
                       difflib.SequenceMatcher(None, candidate.text.casefold(),
                                               previous.text.casefold()).ratio() > .7
                       for previous in detected):
                detected.append(candidate)
        report["text"] = _text(original_text, detected, rendered, language)
        # Segment the input independently from reconstruction. A self-check
        # against scene text alone rewards confident OCR hallucinations.
        reference = [item for item in _ocr(source,language,psm=6)
                     if item.confidence >= .75]
        report["source_text"] = _text(reference, detected, rendered, language,
                                      threshold=.86)
    report["scorecard"] = semantic_scorecard(source,rendered,scene,report["geometry"],
                                              language,ocr)
    if manifest:
        report["ground_truth"] = ground_truth(scene, manifest)
    report["warnings"] = []
    if report["geometry"]["edge_recall_4px"] < .65:
        report["warnings"].append("Many source edges are missing or displaced")
    if ocr and report["text"]["recall"] is not None and report["text"]["recall"] < .8:
        report["warnings"].append("Rendered text is not reliably readable by OCR")
    if ocr and report["source_text"]["recall"] is not None and report["source_text"]["recall"] < .85:
        report["warnings"].append("Text visible in the input is missing or changed in the SVG")
    if manifest and report["ground_truth"]["matched"] < report["ground_truth"]["expected"]:
        report["warnings"].append("Known fixture elements were missed")
    if report["scene"]["fidelity_raster_coverage"] > .25:
        report["warnings"].append("Large regions use source-image fallback; editability is limited")
    for finding in report["scorecard"]["findings"]:
        report["warnings"].append(finding["message"])
    report["scorecard"]["passes"] = not report["warnings"]
    return report
