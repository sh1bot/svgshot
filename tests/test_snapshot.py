import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops

from svgshot.capture import main as render_capture
from svgshot.snapshot import CHUNK, HEADER, MAGIC, MAX_JSON, decode_snapshot, encode_snapshot, read_snapshot, main


def chunk(data):
    return struct.pack('>I', len(data))+CHUNK+data+struct.pack('>I', zlib.crc32(CHUNK+data)&0xffffffff)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.png = self.directory/'capture.png'
        Image.new('RGB', (120, 80), '#abcdee').save(self.png)
        self.original = self.png.read_bytes()
        self.snapshot = dict(format='svgshot.capture', version=3,
                             source={'platform':'windows','provider':'windows-uia'},
                             image={'size':[120,80],'coordinate_space':'image-pixels',
                                    'source_bounds':[20,30,120,80],
                                    'source_to_image':[1,0,0,1,-20,-30]},
                             capture_policy={}, warnings=[],
                             root={'id':'n1','role':'window','label':'Window Ω <&>',
                                   'bounds':[0,0,120,80],'states':{},'relationships':{},
                                   'text':{'status':'not_captured','lines':[]},
                                   'value':{'status':'not_captured'},'children':[]},
                             native={'provider':'windows-uia','snapshot':{}})

    def embed(self, data=None):
        payload = encode_snapshot(self.snapshot) if data is None else data
        self.png.write_bytes(self.original[:-12]+chunk(payload)+self.original[-12:])

    def test_round_trip_and_pixels_unchanged(self):
        with Image.open(self.png) as image:
            original_pixels = image.copy()
        self.embed()
        self.assertEqual(read_snapshot(self.png), self.snapshot)
        with Image.open(self.png) as image:
            self.assertIsNone(ImageChops.difference(original_pixels, image).getbbox())
        self.assertTrue(self.png.read_bytes().startswith(self.original[:-12]))
        self.assertEqual(decode_snapshot(encode_snapshot(self.snapshot)), self.snapshot)

    def test_png_only_replay_preserves_extended_metadata_and_export(self):
        self.embed()
        svg, exported = self.directory/'capture.svg', self.directory/'export.json'
        self.assertEqual(render_capture([str(svg), '--image', str(self.png), '--no-ocr']), 0)
        metadata = ET.parse(svg).find('.//{http://www.w3.org/2000/svg}metadata')
        self.assertEqual(json.loads(metadata.text), self.snapshot)
        self.assertEqual(main([str(self.png), '--json', str(exported)]), 0)
        self.assertEqual(json.loads(exported.read_text()), self.snapshot)

    def test_old_chunk_name_is_not_read(self):
        payload = encode_snapshot(self.snapshot)
        old_chunk = struct.pack('>I', len(payload))+b'suIA'+payload
        old_chunk += struct.pack('>I', zlib.crc32(b'suIA'+payload)&0xffffffff)
        self.png.write_bytes(self.original[:-12]+old_chunk+self.original[-12:])
        with self.assertRaisesRegex(ValueError, 'no embedded seMA'):
            read_snapshot(self.png)

    def test_missing_corrupt_duplicate_and_truncated_chunks(self):
        with self.assertRaisesRegex(ValueError, 'no embedded seMA'):
            read_snapshot(self.png)
        payload = encode_snapshot(self.snapshot)
        self.embed()
        corrupt = bytearray(self.png.read_bytes())
        corrupt[-13] ^= 1
        self.png.write_bytes(corrupt)
        with self.assertRaisesRegex(ValueError, 'CRC'):
            read_snapshot(self.png)
        self.png.write_bytes(self.original[:-12]+chunk(payload)*2+self.original[-12:])
        with self.assertRaisesRegex(ValueError, 'multiple'):
            read_snapshot(self.png)
        self.embed()
        self.png.write_bytes(self.png.read_bytes()[:-15])
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            read_snapshot(self.png)

    def test_unknown_format_and_decompression_limits(self):
        payload = bytearray(encode_snapshot(self.snapshot))
        for offset in (8, 9, 10, 11):
            mutated = bytearray(payload)
            mutated[offset] = 9
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                decode_snapshot(mutated)
        for declared_length, raw in ((1, b'x'*1000000), (100, b'{}')):
            data = HEADER.pack(MAGIC, 1, 1, 1, 0, declared_length)+zlib.compress(raw)
            with self.assertRaisesRegex(ValueError, 'length or stream'):
                decode_snapshot(data)
        with self.assertRaisesRegex(ValueError, '64 MiB'):
            decode_snapshot(HEADER.pack(MAGIC, 1, 1, 1, 0, MAX_JSON+1))
        with self.assertRaisesRegex(ValueError, 'stream'):
            decode_snapshot(bytes(payload)+b'trailing')
        with self.assertRaises(ValueError):
            decode_snapshot(bytes(payload[:-2]))

    def test_old_schema_is_rejected_even_with_the_current_chunk_name(self):
        raw = json.dumps({'version':2,'image_size':[120,80],'screen_bounds':[0,0,120,80],
                          'root':{'id':'old','children':[]}}).encode()
        data = HEADER.pack(MAGIC, 1, 1, 1, 0, len(raw))+zlib.compress(raw)
        with self.assertRaisesRegex(ValueError, 'Unsupported unified capture schema'):
            decode_snapshot(data)
        with self.assertRaisesRegex(ValueError, 'Unsupported unified capture schema'):
            encode_snapshot({'version':2})

    def test_embedded_dimensions_must_match_bitmap(self):
        self.snapshot['image']['size'] = [119, 80]
        self.embed()
        self.assertEqual(render_capture([str(self.directory/'out.svg'), '--image', str(self.png), '--no-ocr']), 1)


if __name__ == '__main__':
    unittest.main()
