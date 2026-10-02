"""Deterministic vector simplification of screenshot artwork."""
from .model import Node

def trace_artwork(image, box, *, opaque=False):
    """Simplify screenshot ink into flat vector contours, without font dependence."""
    import numpy as np
    x, y, w, h = box
    patch = image.crop((x, y, x+w, y+h)).convert("RGB")
    patch.thumbnail((256, 256))
    scale_x, scale_y = w/patch.width, h/patch.height
    w, h = patch.size
    if opaque:
        from PIL import Image, ImageFilter
        patch = patch.filter(ImageFilter.MedianFilter(3)).quantize(
            colors=16, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).convert("RGB")
    pixels = np.asarray(patch)
    background = np.median(np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1])), axis=0)
    foreground = np.ones((h, w), dtype=bool) if opaque else np.max(np.abs(pixels.astype(float)-background), axis=2) > 35
    # Fixed palette bins keep tracing deterministic and remove antialias texture.
    colors = pixels if opaque else np.minimum((pixels.astype("uint16")//48)*48+24, 255)
    paths = []
    for color in np.unique(colors[foreground], axis=0):
        mask = (foreground & np.all(colors == color, axis=2)).astype('uint8')
        # Trace pixel boundaries; omit collinear vertices to keep paths compact.
        edges = {}
        for row, column in np.argwhere(mask):
            a, b = int(column), int(row)
            for start, end, outside in (
                ((a,b),(a+1,b), b == 0 or not mask[b-1,a]),
                ((a+1,b),(a+1,b+1), a == w-1 or not mask[b,a+1]),
                ((a+1,b+1),(a,b+1), b == h-1 or not mask[b+1,a]),
                ((a,b+1),(a,b), a == 0 or not mask[b,a-1]),
            ):
                if outside:
                    edges.setdefault(start, []).append(end)
        segments = []
        while edges:
            start = next(iter(edges))
            points, current = [start], start
            while current in edges:
                following = edges[current].pop()
                if not edges[current]:
                    del edges[current]
                current = following
                if current == start:
                    break
                points.append(current)
            if current != start or len(points) < 4:
                continue
            simple = [point for i, point in enumerate(points)
                      if (point[0]-points[i-1][0], point[1]-points[i-1][1]) !=
                         (points[(i+1)%len(points)][0]-point[0], points[(i+1)%len(points)][1]-point[1])]
            if len(simple) >= 3:
                segments.append("M"+" L".join(f"{x+a*scale_x:g} {y+b*scale_y:g}" for a, b in simple)+" Z")
        if segments:
            paths.append({"d": " ".join(segments), "fill": "#%02x%02x%02x" % tuple(color)})
    return Node("capture-artwork", box, vector_data={"paths": paths, "opaque": opaque})


def photo_regions(image):
    """Find large, densely coloured rectangular pictures, excluding UI icons."""
    import numpy as np
    from scipy import ndimage
    pixels = np.asarray(image.convert('RGB'))
    mask = ndimage.binary_closing(pixels.min(axis=2) < 235, structure=np.ones((3, 3)))
    labels, _ = ndimage.label(mask)
    regions = []
    for label, slices in enumerate(ndimage.find_objects(labels), 1):
        if slices is None:
            continue
        ys, xs = slices
        w, h = xs.stop-xs.start, ys.stop-ys.start
        if w < 140 or h < 100 or w > image.width*.8:
            continue
        patch = pixels[ys, xs]
        if (labels[ys, xs] == label).mean() < .65:
            continue
        # Flat, bright panels have too few coarse colours to be photographs.
        chroma = patch.max(axis=2).astype(int)-patch.min(axis=2)
        if (chroma > 22).mean() < .2 or len(np.unique((patch[::4, ::4]//32).reshape(-1, 3), axis=0)) < 24:
            continue
        regions.append((xs.start, ys.start, w, h))
    return regions
