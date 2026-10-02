"""Validate the real Windows fixture and its independently decoded PNG payload."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from svgshot.snapshot import read_snapshot


def walk(node):
    yield node
    for child in node.get('children', []):
        if child:
            yield from walk(child)


snapshot = read_snapshot('build/live.png')
assert snapshot == json.loads(Path('build/live.uia.json').read_text(encoding='utf-8')), 'PNG/JSON mismatch'
assert snapshot['version'] == 3, 'Wrong schema version'
from svgshot.schema import validate
validate(snapshot)
snapshot = snapshot['native']['snapshot']
assert snapshot['property_names'], 'No property registry'
assert snapshot['pattern_names'], 'No pattern registry'
assert snapshot['capture_policy']['actions_invoked'] is False, 'Unexpected action policy'
assert 'PasswordHiddenSentinel' not in json.dumps(snapshot), 'Password leaked'
assert 'HiddenTextSentinel' not in json.dumps(snapshot), 'Hidden formatted text leaked'
assert 'HiddenValueSentinel' not in json.dumps(snapshot), 'Hidden control leaked'
assert 'OutsideValueSentinel' not in json.dumps(snapshot), 'Out-of-window control leaked'
assert snapshot['capture_policy']['include_hidden_content'] is False, 'Hidden capture unexpectedly enabled'
assert all('value' not in n['states'] for n in walk(snapshot['root'])), 'Full edit value leaked'
nodes = list(walk(snapshot['root']))
assert any(node['password'] for node in nodes), 'No password control captured'
assert all('properties' in n and 'patterns' in n and 'text_selection' in n for n in nodes), 'Missing broad fields'
assert any(n['text_capture']['status']=='value' for n in nodes), 'TextPattern not exercised'
assert any(p.get('status') == 'value' and p.get('value') == 1
           for n in nodes if n['name'] == 'Preserve semantics'
           for key, p in n['properties'].items()
           if 'ToggleState' in snapshot['property_names'][key]), 'Checked state missing from typed properties'
print(f"Validated {len(nodes)} elements, embedded JSON, typed state, formatting, and password redaction")

# Full capture is an explicit opt-in; passwords remain excluded even then.
broad = read_snapshot('build/broad.png')['native']['snapshot']
assert broad['capture_policy']['include_hidden_content'] is True
assert 'PasswordHiddenSentinel' not in json.dumps(broad), 'Password leaked in opt-in mode'
assert any('value' in n['states'] for n in walk(broad['root']) if not n['password']), 'Opt-in values missing'

assert any(r.get('attributes') and r.get('format_runs') for n in walk(broad['root']) for r in n['text_ranges']), 'No text formatting captured'

# Native stdout must contain a complete semantic PNG with no temporary capture path.
if len(sys.argv)>1:
    import subprocess,io
    result=subprocess.run(['build/capture/Release/svgshot-capture-win.exe','--hwnd',sys.argv[1],'--stdout'],stdout=subprocess.PIPE,check=True)
    streamed=read_snapshot(io.BytesIO(result.stdout))
    validate(streamed)
    assert streamed['root']['label']=='svgshot Capture Fixture'
    assert 'PasswordHiddenSentinel' not in json.dumps(streamed)
    print('Validated direct in-memory native PNG capture')
    debug = subprocess.run(['build/capture/Release/svgshot-capture-win.exe', '--hwnd', sys.argv[1],
        '--debug-unredacted', '--stdout'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True, timeout=30)
    unredacted = read_snapshot(io.BytesIO(debug.stdout))
    validate(unredacted)
    assert b'may include sensitive information' in debug.stderr
    assert unredacted['capture_policy']['debug_unredacted'] is True
    assert unredacted['capture_policy']['password_content'] == 'included'
    assert any('may include sensitive information' in warning for warning in unredacted['warnings'])
    for api in ('uia', 'msaa', 'auto'):
        selected = subprocess.run(['build/capture/Release/svgshot-capture-win.exe', '--hwnd', sys.argv[1],
            '--accessibility-api', api, '--stdout'], stdout=subprocess.PIPE,
            check=True, timeout=30)
        selected_snapshot = read_snapshot(io.BytesIO(selected.stdout))
        validate(selected_snapshot)
        assert selected_snapshot['source']['provider'] in ('windows-uia', 'windows-msaa')
        if api != 'auto':
            assert selected_snapshot['source']['provider'] == 'windows-' + api
        if api == 'msaa':
            assert not any('tree truncated' in warning for warning in selected_snapshot['warnings']), \
                'MSAA traversal repeated children until the node limit'
            assert any(n.get('label') == 'Accessible service row' and n['role'] == 'listitem'
                       for n in walk(selected_snapshot['root'])), 'MSAA missed nested list control contents'
        assert selected_snapshot['capture_policy']['collector_integrity_level'] >= 0
        assert selected_snapshot['capture_policy']['target_integrity_level'] == selected_snapshot['capture_policy']['collector_integrity_level']
    compatibility=subprocess.run(['build/capture/Release/svgshot-capture-win.exe','--hwnd',sys.argv[1],
        '--print-window','--stdout'],stdout=subprocess.PIPE,check=True,timeout=30)
    compatible=read_snapshot(io.BytesIO(compatibility.stdout))
    validate(compatible)
    assert compatible['root']['label']=='svgshot Capture Fixture'
    assert compatible['capture_policy']['bitmap_method']=='printwindow'
    assert compatible['warnings'], 'Missing compatibility capture warning'
    assert 'PasswordHiddenSentinel' not in json.dumps(compatible)
    Path('build/compatibility.png').write_bytes(compatibility.stdout)
    # Exercise the visible-screen fallback and ensure it refuses other windows'
    # pixels. Raise the fixture only in this test; the capture tool must not do so.
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    hwnd = int(sys.argv[1], 0)
    assert user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x13), 'Cannot raise fixture'
    screen = subprocess.run(['build/capture/Release/svgshot-capture-win.exe', '--hwnd', sys.argv[1],
        '--screen', '--stdout'], stdout=subprocess.PIPE, check=True, timeout=30)
    visible = read_snapshot(io.BytesIO(screen.stdout))
    validate(visible)
    assert visible['capture_policy']['bitmap_method'] == 'screen'
    assert visible['root']['label'] == 'svgshot Capture Fixture'
    assert 'PasswordHiddenSentinel' not in json.dumps(visible)
    Path('build/screen.png').write_bytes(screen.stdout)
    rectangle = wintypes.RECT()
    assert user32.GetWindowRect(hwnd, ctypes.byref(rectangle))
    overlay = user32.CreateWindowExW(0x88, 'STATIC', 'Occlusion sentinel', 0x90000000,
        rectangle.left + 20, rectangle.top + 40, 64, 64, None, None, None, None)
    assert overlay, 'Cannot create overlapping fixture'
    try:
        rejected = subprocess.run(['build/capture/Release/svgshot-capture-win.exe', '--hwnd', sys.argv[1],
            '--screen', '--stdout'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        assert rejected.returncode != 0, 'Obscured window captured'
        assert not rejected.stdout, 'Failed capture emitted image data'
        assert b'unobscured window' in rejected.stderr, rejected.stderr
    finally:
        user32.DestroyWindow(overlay)
    # An omitted output path must resolve to the actual Desktop folder and use
    # a dated, titled PNG filename. Do not leave CI test captures on the Desktop.
    result=subprocess.run(['build/capture/Release/svgshot-capture-win.exe','--hwnd',sys.argv[1]],
        stdout=subprocess.PIPE,check=True,text=True,encoding='utf-8',timeout=30)
    generated=Path(result.stdout.strip())
    try:
        import re
        assert re.match(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{3} - svgshot Capture Fixture',generated.name)
        assert generated.suffix=='.png'
        assert generated.is_file()
        validate(read_snapshot(generated))
    finally:
        generated.unlink(missing_ok=True)
    print('Validated compatibility and screen capture, occlusion refusal, and automatic Desktop filename')
