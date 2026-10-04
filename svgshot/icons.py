"""Place traced icon artwork into the recognized scene."""
from __future__ import annotations

from .model import Node
from .tracing import trace
from .tracing_core import (_background_color, _bezier, _fit_curve, _icon_palette,
                           _sample_segment, _smooth_quantize, _target,
                           _stacked_masks, simplify_icon as _uncached_simplify_icon)


def simplify_icon(image, box, *, allow_raster=True, smooth=False, palette_size=None,
                  blur=.5, cache_dir=False, name=None, pixel_boundaries=False):
    """Trace a crop, with optional disk cache; direct calls default to no cache."""
    if cache_dir is False:
        return _uncached_simplify_icon(image,box,allow_raster=allow_raster,
                                       smooth=smooth,palette_size=palette_size,blur=blur,
                                       pixel_boundaries=pixel_boundaries)
    x,y,w,h=box
    background=_background_color(image,box)
    result=trace(image.crop((x,y,x+w,y+h)),background,
                 cache_dir=cache_dir,algorithm=('pixel-boundary' if pixel_boundaries else
                                                'smooth-palette' if smooth else 'palette'),
                 palette_size=palette_size,blur=blur,allow_raster=allow_raster,name=name)
    if result.node is not None:
        result.node.box=box
    return result.node


def simplify_scene_artwork(scene, image, *, allow_raster=True, smooth=False,
                           palette_size=None, blur=.5, cache_dir=None,
                           pixel_boundaries=False):
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
            # The background participates in the cache key and tracing.
            background = tuple(_background_color(image,node.box).round().astype('uint8'))
            key = (w,h,background,image.crop((x,y,x+w,y+h)).tobytes())
            # Disk hits still read their SVG file, so an edit is visible even
            # when the same icon occurs more than once in a scene.
            if key not in cache or cache_dir is not False:
                cache[key] = simplify_icon(image,node.box,allow_raster=allow_raster,
                                           smooth=smooth,palette_size=palette_size,
                                           blur=blur,cache_dir=cache_dir,
                                           pixel_boundaries=pixel_boundaries,
                                           name=node.vector_data.get('accessible_name'))
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
