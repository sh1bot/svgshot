"""Compact icon approximation shared by semantic tracing and raster fallback.

Filled contours work for both glyphs and colour artwork. Simplification is
accepted only after a supersampled render checks silhouette and colour error;
PNG remains the fallback for textures or shapes outside that error budget.
Photographs and explicit fidelity overlays do not enter this path.

Developer metrics: accept silhouette IoU >= .82 and mean premultiplied RGB/
alpha error <= .16 over foreground pixels, with at most 500 polygon vertices
or 500 straight/curved spans in the optional smoothing experiment. Compare
at the original icon size after 4x supersampling; transparent RGB is ignored.
These bounds preserve recognisability rather than reproducing every pixel.
"""
import base64
import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .model import Node


def _contours(mask):
    h, w = mask.shape
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
    contours = []
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
        if current == start and len(points) >= 4:
            contours.append(np.asarray(points, dtype=float))
    return contours


def _rdp(points, tolerance):
    if len(points) <= 2:
        return points
    delta = points[-1]-points[0]
    length = float(np.dot(delta,delta))
    t = np.clip((points-points[0]) @ delta / max(length,1e-9),0,1)
    distance = np.linalg.norm(points-(points[0]+t[:,None]*delta),axis=1)
    index = int(distance.argmax())
    if distance[index] <= tolerance:
        return points[[0,-1]]
    return np.concatenate((_rdp(points[:index+1],tolerance)[:-1],_rdp(points[index:],tolerance)))


def _simplify(points, tolerance):
    # Split a closed contour at distant vertices; a closed zero-length baseline
    # would otherwise collapse a loop or create an arbitrary corner.
    pivot = int(np.linalg.norm(points-points[0],axis=1).argmax())
    first = _rdp(points[:pivot+1],tolerance)
    last = _rdp(np.concatenate((points[pivot:],points[:1])),tolerance)
    return np.concatenate((first[:-1],last[:-1]))


def _render(layers, size):
    w,h = size
    output = Image.new('RGBA',(w*4,h*4))
    for color, contours in layers:
        mask = np.zeros((h*4,w*4),dtype=bool)
        for points in contours:
            part = Image.new('1',(w*4,h*4))
            ImageDraw.Draw(part).polygon([(round(x*4),round(y*4)) for x,y in points],fill=1)
            mask ^= np.asarray(part,dtype=bool)  # even-odd holes remain transparent
        layer = Image.new('RGBA',output.size,tuple(color))
        layer.putalpha(Image.fromarray(mask.astype('uint8')*int(color[3])))
        output.alpha_composite(layer)
    return output.resize(size,Image.Resampling.LANCZOS)


def _quality(target, rendered):
    a,b = np.asarray(target).astype(float)/255,np.asarray(rendered).astype(float)/255
    am = a[:,:,3] > max(.03,float(a[:,:,3].max())*.35)
    bm = b[:,:,3] > max(.03,float(b[:,:,3].max())*.35)
    union = am|bm
    if not union.any():
        return 0.,1.
    iou = (am&bm).sum()/union.sum()
    # Premultiplied colour ignores RGB in fully transparent pixels.
    error = np.abs(a[:,:,:3]*a[:,:,3:] - b[:,:,:3]*b[:,:,3:]).mean(axis=2)
    alpha_error = np.abs(a[:,:,3]-b[:,:,3])
    return float(iou),float((error[union].mean()+alpha_error[union].mean())/2)


def _background_color(image, box, *, prefer_dominant=False):
    x, y, w, h = box
    pad = np.asarray(image.crop((max(0,x-2),max(0,y-2),min(image.width,x+w+2),
                                 min(image.height,y+h+2))).convert('RGB'))
    border = np.concatenate((pad[0],pad[-1],pad[:,0],pad[:,-1]))
    colors, counts = np.unique(border, axis=0, return_counts=True)
    # A channel-wise median can invent a colour halfway between a selection
    # background and a border. Prefer an actual repeated background colour.
    index = int(counts.argmax())
    if prefer_dominant and counts[index] >= max(3, len(border)*.05):
        return colors[index].astype(float)
    return np.median(border, axis=0)


def _target(image, box, *, force_colour=False, background=None):
    x,y,w,h = box
    patch = image.crop((x,y,x+w,y+h)).convert('RGBA')
    pixels = np.asarray(patch).astype(float)
    rgb, alpha = pixels[:,:,:3],pixels[:,:,3]/255
    has_alpha = bool((alpha < .99).any())
    if not has_alpha:
        # A screenshot is already composited. Its surrounding pixels usually
        # provide a better background estimate than a tightly cropped icon edge.
        background = _background_color(image, box) if background is None else background
        distance = np.linalg.norm(rgb-background,axis=2)
        foreground = distance > 40
    else:
        foreground = alpha > max(.03,float(alpha.max())*.35)
        background = np.zeros(3)
        distance = np.linalg.norm(rgb-background,axis=2)
    if foreground.sum() < 3:
        return None,False
    strong = foreground & (alpha > .8) if has_alpha else foreground & (distance >= np.percentile(distance[foreground],65))
    color = np.median(rgb[strong if strong.any() else foreground],axis=0)
    if has_alpha:
        monochrome = np.percentile(np.linalg.norm(rgb[foreground]-color,axis=1),90) < 30
    else:
        direction = color-background
        coverage = np.clip((rgb-background) @ direction/max(1,np.dot(direction,direction)),0,1)
        residual = np.linalg.norm(rgb-(background+coverage[:,:,None]*direction),axis=2)
        monochrome = np.percentile(residual[foreground],90) < 24
        if monochrome:
            alpha = coverage
    monochrome = monochrome and not force_colour
    if monochrome:
        rgb = np.broadcast_to(color,rgb.shape).copy()
    elif not has_alpha:
        alpha = foreground.astype(float)
    rgba = np.concatenate((rgb,alpha[:,:,None]*255),axis=2).clip(0,255).astype('uint8')
    return Image.fromarray(rgba),monochrome


def _distinct_palette(patch, count):
    """Greedy max-min RGB separation, seeded with the most frequent source colour.

    This approximates a maximally distinct set; every entry is an actual source
    RGB triple. Hidden RGB in transparent pixels does not consume palette slots.
    Ties use lexicographic order for reproducible captures.
    """
    pixels = np.asarray(patch.convert('RGBA'))
    visible = pixels[:, :, 3] > 8
    if not visible.any():
        return np.empty((0, 3), dtype=np.uint8)
    colors, frequencies = np.unique(pixels[:, :, :3][visible], axis=0, return_counts=True)
    values = colors.astype(float)
    selected = [int(frequencies.argmax())]
    distances = ((values - values[selected[0]]) ** 2).sum(axis=1)
    for _ in range(min(count, len(colors)) - 1):
        index = int(distances.argmax())
        selected.append(index)
        distances = np.minimum(distances, ((values - values[index]) ** 2).sum(axis=1))
    return colors[selected]


def _smooth_quantize(patch, palette, blur):
    """Blur first, bicubic 4x second, then nearest source-palette RGB, no dither.

    Filter premultiplied RGBA so invisible RGB never bleeds into the boundary.
    Alpha stays separate from the RGB palette and is used for silhouette tracing.
    """
    rgba = np.asarray(patch.convert('RGBA')).astype(float)
    rgba[:, :, :3] *= rgba[:, :, 3:] / 255
    # RGBa tells Pillow the channels are already premultiplied during resize.
    premultiplied = Image.fromarray(np.rint(rgba).astype('uint8'), 'RGBa')
    filtered = premultiplied.filter(ImageFilter.GaussianBlur(blur)).resize(
        (patch.width * 4, patch.height * 4), Image.Resampling.BICUBIC)
    pixels = np.asarray(filtered).astype(float)
    rgb = np.clip(pixels[:, :, :3] * 255 / np.maximum(pixels[:, :, 3:], 1), 0, 255)
    # Bound temporary memory for the largest eligible icons and palettes.
    samples = rgb.reshape(-1, 3)
    indices = np.empty(len(samples), dtype=np.uint8)
    for start in range(0, len(samples), 4096):
        distances = ((samples[start:start+4096, None] - palette.astype(float)[None]) ** 2).sum(axis=2)
        indices[start:start+4096] = distances.argmin(axis=1)
    pixels[:, :, :3] = palette[indices].reshape(rgb.shape)
    return Image.fromarray(np.rint(pixels).astype('uint8'))


def _bezier(control, t):
    t = np.asarray(t)[:, None]
    return ((1-t)**3 * control[0] + 3*(1-t)**2*t * control[1]
            + 3*(1-t)*t**2 * control[2] + t**3 * control[3])


def _fit_curve(points, left, right, tolerance, *, prefer_lines=True):
    """Prefer a line within tolerance; otherwise fit and split cubic segments.

    Lines have two endpoints; cubics have four control points. Line error is
    nearest distance to the finite segment, in original-image pixels, including
    endpoint overshoot. Rendering and SVG export use the same chosen geometry.
    """
    delta = points[-1] - points[0]
    length = float(np.dot(delta, delta))
    position = np.clip((points-points[0]) @ delta / max(length, 1e-9), 0, 1)
    line_error = np.linalg.norm(points-(points[0]+position[:, None]*delta), axis=1)
    if prefer_lines and float(line_error.max()) <= tolerance:
        return [points[[0, -1]]]
    def unit(vector):
        return vector / max(float(np.linalg.norm(vector)), 1e-9)
    left, right = unit(left), unit(right)
    chord = np.linalg.norm(np.diff(points, axis=0), axis=1)
    distance = float(chord.sum())
    if len(points) == 2 or distance < 1e-9:
        span = np.linalg.norm(points[-1] - points[0]) / 3
        return [np.array([points[0], points[0]+left*span, points[-1]+right*span, points[-1]])]
    t = np.concatenate(([0.], np.cumsum(chord))) / distance
    def fit(parameters):
        b0, b1, b2, b3 = ((1-parameters)**3, 3*parameters*(1-parameters)**2,
                          3*parameters**2*(1-parameters), parameters**3)
        residual = points - (b0+b1)[:, None]*points[0] - (b2+b3)[:, None]*points[-1]
        matrix = np.stack((b1[:, None]*left, b2[:, None]*right), axis=2).reshape(-1, 2)
        lengths = np.linalg.lstsq(matrix, residual.reshape(-1), rcond=None)[0]
        if (lengths <= 1e-6).any() or (lengths > distance).any():
            lengths[:] = np.linalg.norm(points[-1]-points[0]) / 3
        return np.array([points[0], points[0]+left*lengths[0],
                         points[-1]+right*lengths[1], points[-1]])
    control = fit(t)
    error = np.linalg.norm(_bezier(control, t)-points, axis=1)
    # Newton projection updates the correspondence along the curve before
    # splitting. Preserve point order; this is local reparameterisation, not
    # a claim of an exact global nearest-point solution for arbitrary cubics.
    for _ in range(4):
        if float(error.max()) <= tolerance:
            break
        u = t[:, None]
        first = 3*((1-u)**2*(control[1]-control[0]) +
                   2*(1-u)*u*(control[2]-control[1]) + u**2*(control[3]-control[2]))
        second = 6*((1-u)*(control[2]-2*control[1]+control[0]) +
                    u*(control[3]-2*control[2]+control[1]))
        residual = _bezier(control, t)-points
        denominator = (first*first).sum(axis=1)+(residual*second).sum(axis=1)
        step = np.divide((residual*first).sum(axis=1), denominator,
                         out=np.zeros_like(t), where=np.abs(denominator)>1e-9)
        revised = np.clip(t-step, 0, 1)
        revised[0], revised[-1] = 0, 1
        if (np.diff(revised) <= 0).any():
            break
        candidate = fit(revised)
        candidate_error = np.linalg.norm(_bezier(candidate, revised)-points, axis=1)
        if float(candidate_error.max()) >= float(error.max()):
            break
        t, control, error = revised, candidate, candidate_error
    split = int(error[1:-1].argmax()) + 1
    if error[split] <= tolerance:
        return [control]
    tangent = unit(points[split+1]-points[split-1])
    return (_fit_curve(points[:split+1], left, -tangent, tolerance, prefer_lines=prefer_lines)
            + _fit_curve(points[split:], tangent, right, tolerance, prefer_lines=prefer_lines))


def _sample_segment(segment, t):
    if len(segment) == 2:
        t = np.asarray(t)[:, None]
        return (1-t)*segment[0] + t*segment[1]
    return _bezier(segment, t)


def _curve_contour(points, tolerance, *, prefer_lines=True):
    # Measure turns across a source-pixel neighbourhood, rather than preserving
    # every right angle on the 4x quantized staircase as an intentional corner.
    dots, incoming, outgoing = [], [], []
    for i, point in enumerate(points):
        neighbours = []
        for direction in (-1, 1):
            for offset in range(1, len(points)):
                neighbour = points[(i+direction*offset) % len(points)]
                if np.linalg.norm(neighbour-point) >= 1.5:
                    break
            neighbours.append(neighbour)
        a, b = point-neighbours[0], neighbours[1]-point
        incoming.append(a/max(np.linalg.norm(a), 1e-9))
        outgoing.append(b/max(np.linalg.norm(b), 1e-9))
        dots.append(float(np.dot(a, b) / max(np.linalg.norm(a)*np.linalg.norm(b), 1e-9)))
    corners = []
    for i in np.argsort(dots):
        if dots[i] < .5 and all(np.linalg.norm(points[i]-points[j]) >= 1.5 for j in corners):
            corners.append(int(i))
    anchors = sorted(corners) if len(corners) >= 2 else sorted(set(
        corners + [0, int(np.linalg.norm(points-points[0], axis=1).argmax())]))
    curves = []
    for i, start in enumerate(anchors):
        end = anchors[(i+1) % len(anchors)]
        span = points[start:end+1] if end > start else np.concatenate((points[start:], points[:end+1]))
        def end_direction(sequence):
            for point in sequence[1:]:
                direction = point-sequence[0]
                if np.linalg.norm(direction) >= 1.5:
                    return direction
            return sequence[-1]-sequence[0]
        left = end_direction(span) if start in corners else incoming[start]+outgoing[start]
        right = end_direction(span[::-1]) if end in corners else -incoming[end]-outgoing[end]
        curves.extend(_fit_curve(span, left, right, tolerance, prefer_lines=prefer_lines))
    return curves


def _smooth_candidate(image, box, palette_size, blur):
    x, y, w, h = box
    patch = image.crop((x, y, x+w, y+h)).convert('RGBA')
    palette = _distinct_palette(patch, palette_size)
    if not len(palette):
        return None
    filtered = _smooth_quantize(patch, palette, blur)
    background = _background_color(image, box, prefer_dominant=True)
    target, _ = _target(image, box, force_colour=True, background=background)
    # Background removal happens after quantization, so screenshot backgrounds
    # participate in both palette selection and edge smoothing.
    prepared, _ = _target(filtered, (0, 0, w*4, h*4), force_colour=True,
                          background=background)
    if target is None or prepared is None:
        return None
    pixels = np.asarray(prepared)
    visible = pixels[:, :, 3] > max(8, float(pixels[:, :, 3].max())*.35)
    raw = []
    for color in palette:
        mask = visible & np.all(pixels[:, :, :3] == color, axis=2)
        if mask.any():
            alpha = int(np.percentile(pixels[:, :, 3][mask], 90))
            contours = []
            for contour in _contours(mask):
                p = contour / 4
                area = abs(float((p[:, 0]*np.roll(p[:, 1], 1)
                                  - p[:, 1]*np.roll(p[:, 0], 1)).sum())) / 2
                # Ignore quantization specks below a quarter source pixel;
                # the original-image quality check still bounds lost detail.
                if area >= .25:
                    contours.append(_simplify(p, .12))
            raw.append((tuple(int(c) for c in color)+(alpha,), [p for p in contours if len(p) >= 3]))
    best = None
    # Try lines first, retaining curve-only fits when replacing curves with
    # lines fails the overall silhouette/colour checks. Select by path bytes.
    candidates = [(lines, t) for lines in (True, False) for t in (.75, .4, .2)]
    for prefer_lines, tolerance in candidates:
        fitted = [(color, [_curve_contour(p, tolerance, prefer_lines=prefer_lines)
                           for p in contours]) for color, contours in raw]
        # Count both straight and curved spans against the complexity budget.
        complexity = sum(len(curves) for _, contours in fitted for curves in contours)
        if complexity > 500:
            continue
        # Sample fitted curves for the existing original-size quality check.
        layers = [(color, [np.concatenate([_sample_segment(c, np.linspace(0, 1, max(8,
            int(np.linalg.norm(np.diff(c, axis=0), axis=1).sum()*8)+1)))[:-1] for c in curves])
            for curves in contours]) for color, contours in fitted]
        iou, error = _quality(target, _render(layers, (w, h)))
        if iou < .82 or error > .16:
            continue
        paths = []
        def number(value):
            return f'{value:.3f}'.rstrip('0').rstrip('.') or '0'
        for color, contours in fitted:
            parts = []
            for curves in contours:
                parts.append('M' + ','.join(number(v) for v in curves[0][0]))
                for curve in curves:
                    command = 'L' if len(curve) == 2 else 'C'
                    parts.append(command+' '.join(','.join(number(v) for v in p) for p in curve[1:]))
                parts.append('Z')
            if parts:
                paths.append({'d':''.join(parts), 'fill':'#%02x%02x%02x'%color[:3],
                              **({'opacity':round(color[3]/255, 3)} if color[3] != 255 else {})})
        size = sum(len(path['d']) for path in paths)
        if best is None or size < best[0]:
            best = (size, Node('capture-artwork', box, vector_data={'paths':paths, 'local':True,
                'icon_size':[w, h], 'approximation':'smooth-palette', 'palette_size':len(palette),
                'blur_radius':blur, 'silhouette_iou':round(iou, 3), 'colour_error':round(error, 3)}))
    return best


def _vector_candidate(target, monochrome, box):
    x,y,w,h = box
    pixels = np.asarray(target)
    visible = pixels[:,:,3] > max(8,float(pixels[:,:,3].max())*.35)
    best = None
    for count in ((1,) if monochrome else (4,8,12)):
        # Quantize foreground samples only: transparent background colours must
        # neither consume the palette nor bleed into visible icon edges.
        sample = Image.fromarray(pixels[:,:,:3][visible].reshape(1,-1,3))
        quantized = sample.quantize(colors=count,method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE)
        palette = np.asarray(quantized.convert('RGB')).reshape(-1,3)
        colors = np.unique(palette,axis=0)
        distances = ((pixels[:,:,:3].astype(float)[:,:,None,:]-colors[None,None,:,:])**2).sum(axis=3)
        indices = distances.argmin(axis=2)
        raw = [(tuple(int(v) for v in color)+(int(np.percentile(pixels[:,:,3][visible & (indices==i)],90)),),
                _contours(visible & (indices==i))) for i,color in enumerate(colors)
               if (visible & (indices==i)).any()]
        for tolerance in (1.,.65,.35):
            layers = [(color,[_simplify(p,tolerance) for p in paths]) for color,paths in raw]
            layers = [(color,[p for p in paths if len(p)>=3]) for color,paths in layers]
            rendered = _render(layers,(w,h))
            iou,error = _quality(target,rendered)
            vertices = sum(len(p) for _,paths in layers for p in paths)
            if iou < .82 or error > .16 or vertices > 500:
                continue
            paths = []
            for color,contours in layers:
                d = ' '.join('M'+' '.join(f'{a:g},{b:g}' for a,b in contour)+'Z' for contour in contours)
                if d:
                    paths.append({'d':d,'fill':'#%02x%02x%02x'%color[:3],
                                  **({'opacity':round(color[3]/255,3)} if color[3] != 255 else {})})
            size = sum(len(p['d']) for p in paths)
            if best is None or size < best[0]:
                best = (size,Node('capture-artwork',box,vector_data={'paths':paths,'local':True,
                    'icon_size':[w,h],'approximation':'monochrome' if monochrome else 'colour',
                    'silhouette_iou':round(iou,3),'colour_error':round(error,3)}))
    return best


def simplify_icon(image, box, *, allow_raster=True, smooth=False, palette_size=16, blur=.5):
    """Prefer a bounded vector approximation; retain PNG when it loses detail."""
    x,y,w,h = box
    if (not isinstance(palette_size, int) or not 2 <= palette_size <= 32
            or not np.isfinite(blur) or not 0 <= blur <= 4):
        raise ValueError('icon palette size must be 2–32 and blur radius 0–4 source pixels')
    target,monochrome = _target(image,box)
    if target is None:
        return None
    best = (_smooth_candidate(image, box, palette_size, blur) if smooth
            else _vector_candidate(target,monochrome,box))
    if best is None and monochrome and not smooth:
        # A shaded colour icon can lie close to one colour axis. Failure of a
        # glyph approximation is a reason to try colour, not immediately PNG.
        target,_ = _target(image,box,force_colour=True)
        best = _vector_candidate(target,False,box)
    if best:
        return best[1]
    if not allow_raster:
        return None
    buffer = io.BytesIO()
    # Preserve the source crop when approximation failed; do not invent alpha
    # around textures merely because their edge colour happened to be common.
    image.crop((x,y,x+w,y+h)).save(buffer,format='PNG',optimize=True)
    return Node('raster',box,image_data=base64.b64encode(buffer.getvalue()).decode('ascii'),
                vector_data={'icon_fallback':True})


def simplify_scene_artwork(scene, image, *, allow_raster=True, smooth=False, palette_size=16, blur=.5):
    """Both recognition routes converge here, after control/text heuristics."""
    cache = {}
    def visit(parent):
        retained = []
        for node in parent.children:
            visit(node)
            if (node.children or node.kind not in ('raster','capture-artwork')
                    or node.vector_data.get('opaque') or node.vector_data.get('approximation')
                    or node.vector_data.get('icon_fallback') or max(node.box[2:]) > 96):
                retained.append(node)
                continue
            x, y, w, h = node.box
            # Include surroundings: screenshot background estimation uses them.
            key = (w, h, image.crop((x-2, y-2, x+w+2, y+h+2)).tobytes())
            if key not in cache:
                cache[key] = simplify_icon(image,node.box,allow_raster=allow_raster,
                                           smooth=smooth, palette_size=palette_size, blur=blur)
            template = cache[key]
            replacement = (Node(template.kind, node.box, image_data=template.image_data,
                                vector_data=template.vector_data.copy()) if template is not None else None)
            if replacement is not None:
                replacement.confidence = node.confidence
                retained.append(replacement)
            elif node.kind != 'raster' or allow_raster:
                retained.append(node)
        parent.children = retained
    visit(scene)
    return scene
