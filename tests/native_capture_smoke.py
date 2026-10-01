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
nodes = list(walk(snapshot['root']))
assert any(node['password'] for node in nodes), 'No password control captured'
assert all('properties' in n and 'patterns' in n and 'text_selection' in n for n in nodes), 'Missing broad fields'
assert any(r.get('attributes') and r.get('format_runs') for n in nodes for r in n['text_ranges']), 'No text formatting captured'
assert any(p.get('status') == 'value' and p.get('value') == 1
           for n in nodes if n['name'] == 'Preserve semantics'
           for key, p in n['properties'].items()
           if 'ToggleState' in snapshot['property_names'][key]), 'Checked state missing from typed properties'
print(f"Validated {len(nodes)} elements, embedded JSON, typed state, formatting, and password redaction")
