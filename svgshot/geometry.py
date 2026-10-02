"""Conservative recovery of mixed MSAA coordinates in stored captures.

A native window boundary provides an independent physical rectangle. Only fit
an isotropic DPI transform when both dimensions agree with a standard scale.
The embedded snapshot is never mutated; these corrections are a rendering view.
"""
from copy import deepcopy


def _scale(a, b):
    if min(a[2:]) <= 0 or b[2] < 100 or b[3] < 12:
        return None
    for scale in (1.25, 1.5, 1.75, 2, 2.25, 2.5, 3):
        if abs(a[2]-b[2]*scale) <= 2 and abs(a[3]-b[3]*scale) <= 2:
            return scale
    return None


def normalize_msaa(root):
    root = deepcopy(root)

    def visit(node, parent=None, transform=(1, 0, 0)):
        raw = node['bounds']
        scale, dx, dy = transform
        box = [raw[0]*scale+dx, raw[1]*scale+dy, raw[2]*scale, raw[3]*scale]
        # Only native-window/client boundaries qualify, not arbitrary nested panels.
        if parent and parent.get('role') in {'window', 'pane'} and node.get('role') in {'pane', 'list', 'custom'}:
            target = parent['bounds']
            correction = _scale(target, box)
            if correction:
                scale *= correction
                dx, dy = target[0]-raw[0]*scale, target[1]-raw[1]*scale
                box = list(target)
        node['bounds'] = box
        if box != raw:
            node['geometry_recovered'] = True
        for child in node.get('children', []):
            visit(child, node, (scale, dx, dy))
        # Old common-control list proxies combine a physical client origin with
        # logical row offsets. A separately exposed header confirms the scale.
        if node.get('role') == 'list':
            factors = []
            for child in node.get('children', []):
                if child.get('role') == 'window':
                    for header in child.get('children', []):
                        if header.get('_dpi_scale'):
                            factors.append(header['_dpi_scale'])
            if factors and all(abs(f-factors[0]) < .01 for f in factors):
                factor = factors[0]
                x, y = box[:2]
                for child in node.get('children', []):
                    if child.get('role') == 'listitem':
                        a,b,w,h = child['bounds']
                        child['bounds'] = [x+(a-x)*factor, y+(b-y)*factor, w*factor, h*factor]
                        child['geometry_recovered'] = True
        if parent and parent.get('role') == 'window' and scale != transform[0]:
            node['_dpi_scale'] = scale/transform[0]
    visit(root)
    return root
