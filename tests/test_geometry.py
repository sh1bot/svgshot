"""Regression cases for mixed physical/logical MSAA rectangles at 150% DPI."""
import copy
import unittest
from svgshot.geometry import normalize_msaa


def node(role, box, children=()):
    return dict(role=role, bounds=box, children=list(children), name='', framework_id='MSAA')


class GeometryTests(unittest.TestCase):
    def test_header_and_mixed_origin_rows_at_150_percent(self):
        header = node('window', [516, 171, 942, 36], [
            node('list', [300, 84, 628, 24], [node('headeritem', [300, 84, 180, 24])])])
        root = node('list', [516, 171, 942, 735], [
            node('listitem', [516, 195, 504, 19]),
            node('listitem', [516, 214, 504, 19]), header])
        original = copy.deepcopy(root)
        fixed = normalize_msaa(root)
        self.assertEqual(root, original, 'Do not overwrite captured evidence')
        self.assertEqual(fixed['bounds'], original['bounds'])
        self.assertEqual(fixed['children'][0]['bounds'], [516, 207, 756, 28.5])
        self.assertEqual(fixed['children'][1]['bounds'], [516, 235.5, 756, 28.5])
        self.assertEqual(fixed['children'][2]['children'][0]['children'][0]['bounds'], [516,171,270,36])

    def test_correct_physical_bounds_are_not_scaled_twice(self):
        root = node('window', [-800, 100, 942, 36], [
            node('list', [-800,100,942,36], [node('headeritem', [-800,100,270,36])])])
        self.assertEqual(normalize_msaa(root), root)

    def test_arbitrary_nested_control_sizes_are_not_dpi_evidence(self):
        root = node('group', [0,0,600,300], [node('list',[0,0,400,200])])
        self.assertEqual(normalize_msaa(root), root)
