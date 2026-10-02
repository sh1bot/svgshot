"""Platform-neutral, static capture schema."""
import math

FORMAT = 'svgshot.capture'
VERSION = 3
ROLES = ('button calendar checkbox combobox edit hyperlink image listitem list menu menubar '
         'menuitem progressbar radiobutton scrollbar slider spinner statusbar tab tabitem text '
         'toolbar tooltip tree treeitem custom group thumb datagrid dataitem document splitbutton '
         'window pane header headeritem table titlebar separator semanticzoom appbar').split()


def observation(status='value', value=None, **details):
    result = {'status': status, **details}
    if status == 'value':
        result['value'] = value
    return result


def validate(snapshot):
    if snapshot.get('format') != FORMAT or snapshot.get('version') != VERSION:
        raise ValueError('Unsupported unified capture schema')
    image = snapshot.get('image', {})
    size = image.get('size', [])
    if len(size) != 2 or any(type(v) is not int or v <= 0 for v in size) or image.get('coordinate_space') != 'image-pixels':
        raise ValueError('Invalid capture image description')
    ids, refs = set(), []
    def node(n, depth=0):
        if depth > 64 or len(ids) >= 5000:
            raise ValueError('Capture tree exceeds limits')
        if not isinstance(n, dict) or not isinstance(n.get('id'), str) or n['id'] in ids:
            raise ValueError('Invalid or duplicate capture node ID')
        ids.add(n['id'])
        if not isinstance(n.get('role'), str):
            raise ValueError('Capture node needs a named role')
        for b in [n.get('bounds', [])] + [r for l in n.get('text', {}).get('lines', []) for r in l.get('rectangles', [])]:
            if len(b) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in b) or min(b[2:]) < 0:
                raise ValueError('Invalid capture rectangle')
        for targets in n.get('relationships', {}).values():
            refs.extend(targets)
        for c in n.get('children', []):
            node(c, depth+1)
    node(snapshot.get('root'))
    if any(ref not in ids for ref in refs):
        raise ValueError('Unresolved capture relationship')


def render_view(snapshot):
    """Build the renderer's internal view from the current common schema."""
    if snapshot.get('_renderer_view') is True:
        return snapshot
    validate(snapshot)
    size = snapshot['image']['size']
    debug_unredacted = snapshot.get('capture_policy', {}).get('debug_unredacted', False)
    def line(t):
        return {'text': t.get('content', ''), 'rectangles': t.get('rectangles', []),
                'style': t.get('style', {}), 'format_runs': {'ranges': [line(r) for r in t.get('runs', [])]}}
    def node(n):
        states = n.get('states', {})
        s = {k: v for k, v in states.items() if k in ('selected', 'read_only')}
        if states.get('checked') in ('unchecked', 'checked', 'mixed'):
            s['toggle'] = {'unchecked': 0, 'checked': 1, 'mixed': 2}[states['checked']]
        if states.get('expansion') in ('collapsed', 'expanded', 'partial', 'leaf'):
            s['expand_collapse'] = {'collapsed': 0, 'expanded': 1, 'partial': 2, 'leaf': 3}[states['expansion']]
        v = n.get('value', {})
        if v.get('status') == 'value':
            s['range_value' if type(v['value']) in (int, float) else 'value'] = v['value']
        return {'id': n['id'], 'role': n['role'], 'name': n.get('label', ''), 'bounds': n['bounds'],
                'localized_control_type': n.get('role_description', ''), 'help_text': n.get('help', ''),
                'description': n.get('description', ''), 'states': s, 'password': states.get('protected', False),
                'offscreen': states.get('offscreen', False), 'enabled': states.get('enabled', True),
                'keyboard_focus': states.get('focused', False), 'focusable': states.get('focusable', False),
                'control_element': True, 'content_element': True,
                'class_name': n.get('hints', {}).get('class', ''), 'framework_id': n.get('hints', {}).get('toolkit', ''),
                'text_ranges': [line(t) for t in n.get('text', {}).get('lines', [])],
                'debug_unredacted': debug_unredacted,
                **n.get('shortcuts', {}), 'children': [node(c) for c in n.get('children', [])]}
    root = node(snapshot['root'])
    if snapshot.get('source', {}).get('provider') == 'windows-msaa':
        from .geometry import normalize_msaa
        root = normalize_msaa(root)
    return {'_renderer_view': True, 'image_size': size, 'screen_bounds': [0, 0, *size], 'root': root,
            'warnings': snapshot.get('warnings', [])}
