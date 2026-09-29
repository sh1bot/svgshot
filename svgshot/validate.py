"""Perceptual and semantic checks, intentionally not a single pixel-difference score."""
from __future__ import annotations

import difflib
import io
import json
import shutil
import subprocess

import numpy as np
from PIL import Image
from scipy import ndimage

from .model import Node, flatten
from .recognize import _ocr


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


def compare(source: Image.Image, svg_path: str, scene: Node,
            manifest: str | None = None, ocr: bool = True, language: str = "eng") -> dict:
    rendered = render(svg_path, source.width)
    original_text = [n for n in flatten(scene) if n.kind == "text"]
    report = {"geometry": _geometry(source, rendered, original_text),
              "scene": {"objects": len(list(flatten(scene))) - 1,
                        "text_lines": len(original_text),
                        "raster_islands": sum(n.kind == "raster" for n in flatten(scene))}}
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
    return report
