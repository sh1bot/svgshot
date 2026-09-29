"""Conservative GUI geometry and text recognition from a PNG."""
from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .model import Node


@dataclass
class Options:
    colors: int = 10
    min_area: int = 18
    max_raster: int = 128
    raster_fallback: bool = True
    ocr: bool = True
    language: str = "eng"

    @classmethod
    def from_file(cls, path: str | None) -> "Options":
        if not path:
            return cls()
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        result = cls(**data)
        if not 2 <= result.colors <= 32 or result.min_area < 1 or result.max_raster < 0:
            raise ValueError("colors must be 2–32, min_area positive, max_raster nonnegative")
        return result


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(x) for x in rgb[:3])


def _luma(rgb) -> float:
    return float(np.dot(rgb[:3], [0.2126, 0.7152, 0.0722]))


def _ocr(image: Image.Image, language: str, psm: int = 11) -> list[Node]:
    if not shutil.which("tesseract"):
        raise RuntimeError("Tesseract OCR is required (install it or pass --no-ocr)")
    data = io.BytesIO()
    image.convert("RGB").save(data, format="PNG")
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", language, "--psm", str(psm), "tsv"],
        input=data.getvalue(), capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("Tesseract failed: " + result.stderr.decode(errors="replace").strip())
    import csv
    lines = defaultdict(list)
    reader = csv.DictReader(io.StringIO(result.stdout.decode("utf-8-sig")), delimiter="\t")
    for row in reader:
        word = row["text"].strip()
        try:
            confidence = float(row["conf"])
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
        except (ValueError, KeyError):
            continue
        if not word or confidence < 35 or w <= 0 or h <= 0:
            continue
        key = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
        lines[key].append((x, y, w, h, word, confidence))
    nodes = []
    for words in lines.values():
        words.sort(key=lambda w: w[0])
        # OCR frequently mistakes a radio ring beside a label for © or ®.
        # Let the geometry stage see the original pixels in that box.
        if len(words) > 1 and words[0][4] in ("©", "®", "○", "◉", "O"):
            first, second = words[:2]
            if 7 <= first[2] <= 28 and 0 <= second[0]-(first[0]+first[2]) <= 28:
                words = words[1:]
        x = min(w[0] for w in words)
        y = min(w[1] for w in words)
        right = max(w[0] + w[2] for w in words)
        bottom = max(w[1] + w[3] for w in words)
        nodes.append(Node("text", (x, y, right-x, bottom-y),
                          text=" ".join(w[4] for w in words),
                          confidence=sum(w[5] for w in words) / (100 * len(words))))
    return sorted(nodes, key=lambda n: (n.box[1], n.box[0]))


def _pixel_color(image: np.ndarray, box, pad=2):
    x, y, w, h = box
    height, width = image.shape[:2]
    left, top = max(0, x-pad), max(0, y-pad)
    right, bottom = min(width, x+w+pad), min(height, y+h+pad)
    crop = image[top:bottom, left:right].reshape(-1, 3)
    if len(crop) == 0:
        return np.array([0, 0, 0])
    # Prefer high-contrast pixels relative to the perimeter, for both themes.
    lum = crop @ np.array([.2126, .7152, .0722])
    perimeter = np.concatenate((image[top, left:right], image[bottom-1, left:right],
                                image[top:bottom, left], image[top:bottom, right-1]))
    local = _luma(np.median(perimeter, axis=0))
    return crop[int(np.argmax(np.abs(lum-local)))]


def _background(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    border = np.concatenate((image[0], image[-1], image[:, 0], image[:, -1]))
    quant = ((border.astype(np.uint16) + 8) // 16).clip(0, 15)
    key = quant[:, 0] * 256 + quant[:, 1] * 16 + quant[:, 2]
    common = Counter(key.tolist()).most_common(1)[0][0]
    return np.median(border[key == common], axis=0).astype(np.uint8)


def _classify(component: np.ndarray, width: int, height: int) -> tuple[str, float]:
    area = int(component.sum())
    ratio = area / (width * height)
    if min(width, height) <= 2:
        return "line", .85
    if .78 <= width / height <= 1.28 and 7 <= width <= 60:
        yy, xx = np.nonzero(component)
        radius = np.hypot((xx - (width-1)/2) / (width/2),
                          (yy - (height-1)/2) / (height/2))
        # A ring or disk must reach all four sides, with few occupied corners.
        corners = np.r_[component[:2, :2].ravel(), component[:2, -2:].ravel(),
                        component[-2:, :2].ravel(), component[-2:, -2:].ravel()]
        if float(corners.mean()) < .25 and (ratio > .65 or
                (ratio > .12 and np.mean(np.abs(radius-1) < .23) > .72)):
            return ("circle" if ratio > .65 else "ring"), .87
    if ratio >= .78:
        return "rect", min(.98, ratio)
    border = np.r_[component[:2].ravel(), component[-2:].ravel(),
                   component[:, :2].ravel(), component[:, -2:].ravel()]
    if width >= 8 and height >= 6 and border.mean() >= .68 and ratio < .65:
        return "outline", .72
    return "unknown", .25


def _raster(image: Image.Image, box) -> str:
    x, y, w, h = box
    buffer = io.BytesIO()
    image.crop((x, y, x+w, y+h)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _contains(parent: Node, child: Node, tolerance=3) -> bool:
    x, y, w, h = parent.box
    a, b, c, d = child.box
    return (x-tolerance <= a and y-tolerance <= b and
            a+c <= x+w+tolerance and b+d <= y+h+tolerance)


def reconstruct(image: Image.Image, options: Options) -> Node:
    if image.width * image.height > 25_000_000:
        raise ValueError("PNG exceeds the 25-megapixel limit")
    rgb_image = image.convert("RGB")
    pixels = np.asarray(rgb_image)
    h, w = pixels.shape[:2]
    background = _background(pixels)
    root = Node("window", (0, 0, w, h), _hex(background))
    texts = _ocr(rgb_image, options.language) if options.ocr else []
    reserved = np.zeros((h, w), dtype=bool)
    for node in texts:
        x, y, tw, th = node.box
        reserved[max(0,y-2):min(h,y+th+3), max(0,x-2):min(w,x+tw+3)] = True
        node.color = _hex(_pixel_color(pixels, node.box))

    # Quantization removes antialiasing variants and decorative micro-shades.
    small = rgb_image.copy()
    small.thumbnail((1200, 1200))
    palette_image = small.quantize(colors=options.colors, method=Image.Quantize.MEDIANCUT)
    palette = np.array(palette_image.getpalette()[:options.colors*3], dtype=np.uint8).reshape(-1, 3)
    if len(palette) == 0:
        palette = background[None, :]
    # Pixel assignment in stripes bounds memory on large screenshots.
    indices = np.empty((h, w), dtype=np.uint8)
    for y in range(0, h, 128):
        stripe = pixels[y:y+128].astype(np.int32)
        distances = ((stripe[:, :, None, :] - palette[None, None, :, :].astype(np.int32)) ** 2).sum(axis=3)
        indices[y:y+128] = np.argmin(distances, axis=2)
    bg_index = int(np.argmin(((palette.astype(int)-background.astype(int))**2).sum(axis=1)))
    candidates = []
    for index, color in enumerate(palette):
        if index == bg_index:
            continue
        mask = (indices == index) & ~reserved
        labels, count = ndimage.label(mask)
        slices = ndimage.find_objects(labels)
        for label, region in enumerate(slices, start=1):
            if region is None:
                continue
            ys, xs = region
            bw, bh = xs.stop-xs.start, ys.stop-ys.start
            area = int(np.count_nonzero(labels[region] == label))
            if area < options.min_area or bw < 2 or bh < 2 or bw*bh > w*h*.85:
                continue
            box = (xs.start, ys.start, bw, bh)
            kind, confidence = _classify(labels[region] == label, bw, bh)
            if kind == "unknown":
                if not options.raster_fallback or max(bw, bh) > options.max_raster or area < 35:
                    continue
                node = Node("raster", box, confidence=confidence, image_data=_raster(rgb_image, box))
            else:
                node = Node(kind, box, _hex(color), confidence=confidence)
            candidates.append(node)

    # Keep a shallow semantic hierarchy; preserve paint order by drawing large
    # surfaces before controls, and text last.
    candidates.sort(key=lambda n: n.box[2]*n.box[3], reverse=True)
    for node in candidates:
        if node.kind in ("circle", "ring") and node.box[2] <= 28:
            right = node.box[0] + node.box[2]
            nearby = [t for t in texts if 0 <= t.box[0]-right <= 28 and
                      abs((t.box[1]+t.box[3]/2)-(node.box[1]+node.box[3]/2)) < 10]
            if nearby:
                node.kind = "radio" if node.kind == "ring" else "radio-selected"
        elif node.kind in ("rect", "outline") and node.box[2] <= 28 and node.box[3] <= 28:
            right = node.box[0] + node.box[2]
            if any(0 <= t.box[0]-right <= 28 and
                   abs((t.box[1]+t.box[3]/2)-(node.box[1]+node.box[3]/2)) < 10 for t in texts):
                node.kind = "checkbox"
    # A selected radio's central dot may be a separate color component.
    consumed = set()
    for radio in candidates:
        if radio.kind != "radio":
            continue
        x, y, rw, rh = radio.box
        for i, dot in enumerate(candidates):
            if dot is radio or i in consumed:
                continue
            dx, dy, dw, dh = dot.box
            if (x+3 <= dx and y+3 <= dy and dx+dw <= x+rw-3 and
                    dy+dh <= y+rh-3 and max(dw,dh) <= 11):
                radio.kind = "radio-selected"
                consumed.add(i)
                break
    candidates = [n for i,n in enumerate(candidates) if i not in consumed]
    if options.ocr:
        examined = 0
        for control in candidates:
            x, y, cw, ch = control.box
            if control.kind not in ("rect", "outline") or not (55 <= cw <= 300 and 24 <= ch <= 65):
                continue
            if any(_contains(control, t) for t in texts):
                continue
            examined += 1
            if examined > 30:
                break
            for local in _ocr(rgb_image.crop((x,y,x+cw,y+ch)), options.language, psm=7):
                if local.confidence < .65 or sum(c.isalpha() for c in local.text) < 2:
                    continue
                lx, ly, lw, lh = local.box
                local.box = (x+lx, y+ly, lw, lh)
                local.color = _hex(_pixel_color(pixels, local.box))
                if not any(_contains(t, local, 5) for t in texts):
                    texts.append(local)
    root.children = candidates
    for text in texts:
        parents = [n for n in candidates if n.kind in ("rect", "outline") and
                   n.box[2] > text.box[2]+8 and _contains(n, text)]
        if parents:
            parent = min(parents, key=lambda n: n.box[2]*n.box[3])
            if parent.box[3] <= 65 and parent.box[2] <= 300 and parent.box[0] > 0:
                parent.kind = "button" if parent.kind == "rect" else "outlined-button"
                parent.children.append(text)
                continue
        root.children.append(text)
    # Pair controls with their captions, then group repeated aligned radios.
    for control in candidates:
        if control.kind not in ("radio", "radio-selected", "checkbox"):
            continue
        right = control.box[0]+control.box[2]
        matches = [t for t in root.children if t.kind == "text" and
                   0 <= t.box[0]-right <= 28 and
                   abs((t.box[1]+t.box[3]/2)-(control.box[1]+control.box[3]/2)) < 10]
        if matches:
            caption = min(matches, key=lambda t: t.box[0]-right)
            root.children.remove(caption)
            control.children.append(caption)
    radios = [n for n in candidates if n.kind in ("radio", "radio-selected")]
    clusters = defaultdict(list)
    for radio in radios:
        clusters[round(radio.box[0]/4)].append(radio)
    for row in clusters.values():
        row.sort(key=lambda n:n.box[1])
        if len(row) < 2:
            continue
        for control in row:
            root.children.remove(control)
        all_nodes = [n for control in row for n in (control, *control.children)]
        x = min(n.box[0] for n in all_nodes)
        y = min(n.box[1] for n in all_nodes)
        right = max(n.box[0]+n.box[2] for n in all_nodes)
        bottom = max(n.box[1]+n.box[3] for n in all_nodes)
        root.children.append(Node("radio-group", (x,y,right-x,bottom-y), children=row))
    return root
