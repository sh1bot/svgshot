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
assert snapshot['version'] == 2, 'Wrong schema version'
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
broad = read_snapshot('build/broad.png')
assert broad['capture_policy']['include_hidden_content'] is True
assert 'PasswordHiddenSentinel' not in json.dumps(broad), 'Password leaked in opt-in mode'
assert any('value' in n['states'] for n in walk(broad['root']) if not n['password']), 'Opt-in values missing'

assert any(r.get('attributes') and r.get('format_runs') for n in walk(broad['root']) for r in n['text_ranges']), 'No text formatting captured'
