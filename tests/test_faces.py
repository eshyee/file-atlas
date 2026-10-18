"""Offline index tests use synthetic images and a deterministic fake recognizer.

These tests verify indexing, privacy and safety behavior, not model accuracy.
No face model, network access or optional inference dependency is required.
"""

import io
import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image, UnidentifiedImageError

from fileatlas.faces import (CapabilityError, DETECTOR_MODEL, FaceIndex,
                             RECOGNIZER_MODEL, SafetyError, _decode)


class Crash(BaseException):
    pass


def detection(embedding, bbox=None):
    return {'embedding': embedding, 'bbox': bbox or [.2, .15, .7, .8], 'score': .99}


class FakeBackend:
    def __init__(self, table):
        self.table = table
        self.calls = 0

    def analyze(self, image):
        self.calls += 1
        value = self.table.get(image.getpixel((0, 0)), [])
        if isinstance(value, BaseException):
            raise value
        return value


class FaceIndexTests(unittest.TestCase):
    def setUp(self):
        temp_parent = None
        if os.name == 'nt':
            temp_parent = Path.home() / '.fileatlas-tests'
            temp_parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='faces-test-', dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.photos = self.base / 'photos'
        self.photos.mkdir()
        self.models = self.base / 'models'
        self.models.mkdir()
        for name in (DETECTOR_MODEL, RECOGNIZER_MODEL):
            (self.models / name).write_bytes(b'synthetic test model, not real ONNX\0' * 60)
        self.backend = FakeBackend({(255, 0, 0): [detection([1, 0, 0])],
                                    (0, 255, 0): [detection([0, 1, 0])],
                                    (0, 0, 255): [detection([.99, .05, 0])]})
        self.index = FaceIndex(self.base / 'state', backend_factory=lambda *args: self.backend)

    def photo(self, name='a.png', color=(255, 0, 0), root=None, size=(96, 64), exif=None):
        path = (root or self.photos) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new('RGB', size, color)
        options = {'exif': exif} if exif else {}
        image.save(path, **options)
        return path

    def config(self, **options):
        return {'sources': [str(self.photos)], 'model_dir': str(self.models), **options}

    def build(self, **options):
        with patch.object(self.index, 'capabilities', return_value={'ready': True}):
            return self.index.build(self.config(**options))

    def test_optional_capability_does_not_load_models_or_network(self):
        original_import = __import__('importlib').import_module

        def missing(name):
            if name in ('cv2', 'numpy'):
                raise ImportError('synthetic unavailable optional package')
            return original_import(name)

        with patch('fileatlas.faces.importlib.import_module', side_effect=missing):
            capability = self.index.capabilities(self.config())
            self.assertFalse(capability['ready'])
            self.assertFalse(capability['dependencies']['cv2']['available'])
            self.assertTrue(capability['models']['detector']['available'])
            self.assertTrue(capability['models']['recognizer']['available'])
            self.assertIn('opencv-python', capability['message'])
            self.assertTrue(capability['offline'])
            self.assertEqual({'MIT', 'Apache-2.0'}, {item['license'] for item in capability['licenses']})
            with self.assertRaises(CapabilityError) as error:
                self.index.build(self.config())
            self.assertFalse(error.exception.capability['ready'])
        self.assertEqual(0, self.backend.calls)

    def test_git_lfs_pointer_is_not_treated_as_model(self):
        pointer = self.models / DETECTOR_MODEL
        pointer.write_bytes(b'version https://git-lfs.github.com/spec/v1\n' + b' ' * 2000)
        capability = self.index.capabilities(self.config())
        self.assertFalse(capability['models']['detector']['available'])
        self.assertIn('Git LFS', capability['models']['detector']['error'])

    def test_missing_models_are_distinct_and_readable(self):
        (self.models / DETECTOR_MODEL).unlink()
        capability = self.index.capabilities(self.config())
        self.assertFalse(capability['models']['detector']['available'])
        self.assertTrue(capability['models']['recognizer']['available'])
        self.assertIn(DETECTOR_MODEL, capability['message'])

    def test_build_originals_unchanged_and_same_person_grouped(self):
        first = self.photo('a.png')
        second = self.photo('b.png', (0, 0, 255))
        third = self.photo('c.png', (0, 255, 0))
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.photos.rglob('*') if path.is_file()}
        result = self.build()
        self.assertEqual({'scanned': 3, 'indexed': 3, 'faces': 3, 'people': 2, 'errors': 0, 'cancelled': False},
                         {key: result[key] for key in ('scanned', 'indexed', 'faces', 'people', 'errors', 'cancelled')})
        self.assertEqual(before, {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in self.photos.rglob('*') if path.is_file()})
        groups = self.index.people()['rows']
        self.assertEqual([2, 1], [group['photo_count'] for group in groups])
        photos = self.index.photos(groups[0]['id'])
        self.assertEqual({str(first), str(second)}, {row['path'] for row in photos['rows']})
        self.assertNotIn(str(third), {row['path'] for row in photos['rows']})
        self.assertTrue(all(len(row['id']) == 32 for row in groups))
        self.assertEqual([], list(self.photos.glob('*.sqlite3')))

    def test_pages_are_bounded_and_do_not_export_embeddings(self):
        for index in range(3):
            self.photo(f'{index}.png')
        self.build()
        group = self.index.people()['rows'][0]
        page = self.index.photos(group['id'], offset=1, limit=1)
        self.assertEqual(3, page['total'])
        self.assertEqual(1, len(page['rows']))
        self.assertNotIn('embedding', json.dumps(page))
        self.assertIn('root', page['rows'][0])
        self.assertIn('bbox', page['rows'][0]['faces'][0])
        self.assertNotIn('url', json.dumps(page))
        for offset, limit in ((-1, 10), (0, 201), (0, 0), (False, 10)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(SafetyError):
                self.index.photos(offset=offset, limit=limit)
        with self.assertRaises(SafetyError):
            self.index.photos('../../outside')

    def test_unchanged_build_reuses_features_and_keeps_opaque_ids(self):
        self.photo()
        first = self.build()
        photo_before = self.index.photos()['rows'][0]
        person_before = self.index.people()['rows'][0]
        self.assertEqual(1, self.backend.calls)
        second = self.build()
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(1, second['unchanged'])
        self.assertEqual(1, self.backend.calls)
        self.assertEqual(photo_before['id'], self.index.photos()['rows'][0]['id'])
        self.assertEqual(photo_before['faces'][0]['id'], self.index.photos()['rows'][0]['faces'][0]['id'])
        self.assertEqual(person_before['id'], self.index.people()['rows'][0]['id'])

    def test_model_hash_change_even_same_size_mtime_forces_rebuild(self):
        self.photo()
        self.build()
        previous_id = self.index.status()['model_id']
        model = self.models / RECOGNIZER_MODEL
        previous = model.stat()
        raw = bytearray(model.read_bytes())
        raw[-1] ^= 1
        model.write_bytes(raw)
        os.utime(model, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        result = self.build()
        self.assertEqual(0, result['unchanged'])
        self.assertEqual(2, self.backend.calls)
        self.assertNotEqual(previous_id, self.index.status()['model_id'])
        with closing(sqlite3.connect(self.index.db_path)) as connection:
            models = json.loads(connection.execute("SELECT value FROM meta WHERE key='models'").fetchone()[0])
        self.assertTrue(all('sha256' in model and 'mtime_ns' in model for model in models))

    def test_inference_setting_change_does_not_mix_cached_embeddings(self):
        self.photo()
        self.build()
        result = self.build(max_dim=2000)
        self.assertEqual(0, result['unchanged'])
        self.assertEqual(2, self.backend.calls)

    def test_completed_build_remembers_local_model_directory(self):
        self.photo()
        self.build()
        reopened = FaceIndex(self.index.data_dir)
        self.assertEqual(str(self.models), reopened.status()['model_dir'])
        self.assertEqual(str(self.models), reopened.capabilities()['model_dir'])
        self.assertFalse(reopened.status()['bundled_models'])
        self.assertFalse(reopened.capabilities()['bundled_models'])

    def test_new_pyinstaller_extraction_replaces_missing_remembered_models(self):
        self.photo()
        self.build()
        moved = self.base / 'old-models-for-fixture'
        self.assertTrue(self.models.resolve().is_relative_to(self.base.resolve()))
        self.assertTrue(moved.resolve().is_relative_to(self.base.resolve()))
        self.models.rename(moved)
        extraction = self.base / 'current-extraction'
        bundled = extraction / 'models'
        bundled.mkdir(parents=True)
        for model in moved.iterdir():
            (bundled / model.name).write_bytes(model.read_bytes())
        reopened = FaceIndex(self.index.data_dir)
        with patch.object(sys, '_MEIPASS', str(extraction), create=True):
            capability = reopened.capabilities()
        self.assertEqual('', capability['model_dir'])
        self.assertTrue(capability['bundled_models'])
        self.assertNotIn(str(extraction), json.dumps(capability))
        self.assertTrue(capability['models']['detector']['available'])
        self.assertTrue(capability['models']['recognizer']['available'])

    def test_bundled_paths_are_not_exposed_or_saved_and_restart_reuses_features(self):
        self.photo()
        first_extraction = self.base / '_MEIfirst'
        second_extraction = self.base / '_MEIsecond'
        for extraction in (first_extraction, second_extraction):
            bundled = extraction / 'models'
            bundled.mkdir(parents=True)
            for model in self.models.iterdir():
                (bundled / model.name).write_bytes(model.read_bytes())
        with patch.object(sys, '_MEIPASS', str(first_extraction), create=True):
            capability = self.index.capabilities({'model_dir': str(first_extraction / 'models')})
            self.assertTrue(capability['bundled_models'])
            self.assertEqual('', capability['model_dir'])
            self.assertNotIn(str(first_extraction), json.dumps(capability))
            first = self.build(model_dir=None)
            old_model_id = self.index.status()['model_id']
            self.assertEqual('', self.index.status()['model_dir'])
            self.assertTrue(self.index.status()['bundled_models'])
        with closing(sqlite3.connect(self.index.db_path)) as connection:
            saved = connection.execute('SELECT config FROM runs WHERE id=?', (first['run_id'],)).fetchone()[0]
            audit = connection.execute("SELECT value FROM meta WHERE key='models'").fetchone()[0]
        self.assertEqual('', json.loads(saved)['model_dir'])
        self.assertTrue(json.loads(saved)['bundled_models'])
        self.assertNotIn(str(first_extraction), saved)
        self.assertNotIn(str(first_extraction), audit)
        # The former extraction intentionally still exists: a stable bundled
        # marker must prefer this launch's models even before old cleanup.
        with patch.object(sys, '_MEIPASS', str(second_extraction), create=True):
            reopened = FaceIndex(self.index.data_dir, backend_factory=lambda *args: self.backend)
            capability = reopened.capabilities()
            self.assertTrue(capability['bundled_models'])
            self.assertEqual('', capability['model_dir'])
            self.assertNotIn('_MEI', json.dumps(capability))
            selected, _, _ = reopened._model_paths()
            self.assertEqual(second_extraction / 'models', selected)
            with patch.object(reopened, 'capabilities', return_value={'ready': True}):
                second = reopened.build(self.config(model_dir=None))
            self.assertEqual(old_model_id, reopened.status()['model_id'])
        self.assertEqual(1, second['unchanged'])
        self.assertEqual(1, self.backend.calls)

    def test_legacy_saved_extraction_is_migrated_even_when_old_files_exist(self):
        self.photo()
        first = self.build()
        old = self.base / '_MEIlegacy' / 'models'
        current = self.base / '_MEIcurrent' / 'models'
        for directory in (old, current):
            directory.mkdir(parents=True)
            for model in self.models.iterdir():
                (directory / model.name).write_bytes(model.read_bytes())
        with self.index._connect() as connection:
            row = connection.execute('SELECT config FROM runs WHERE id=?', (first['run_id'],)).fetchone()
            settings = json.loads(row['config'])
            settings.pop('bundled_models', None)
            settings['model_dir'] = str(old)
            connection.execute('UPDATE runs SET config=? WHERE id=?', (json.dumps(settings), first['run_id']))
        with patch.object(sys, '_MEIPASS', str(current.parent), create=True):
            capability = self.index.capabilities()
            self.assertTrue(capability['bundled_models'])
            self.assertEqual('', capability['model_dir'])
            self.assertEqual('', self.index.status()['model_dir'])
            self.assertEqual(current, self.index._model_paths()[0])
            self.assertNotIn('_MEI', json.dumps(capability))

    def test_identical_models_in_new_extraction_reuse_features(self):
        self.photo()
        first = self.build()
        old_model_id = self.index.status()['model_id']
        extraction = self.base / 'new-extraction'
        bundled = extraction / 'models'
        bundled.mkdir(parents=True)
        for model in self.models.iterdir():
            (bundled / model.name).write_bytes(model.read_bytes())
        second = self.build(model_dir=str(bundled))
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertEqual(old_model_id, self.index.status()['model_id'])
        self.assertEqual(1, second['unchanged'])
        self.assertEqual(1, self.backend.calls)

    def test_complete_link_prevents_similarity_chain_merge(self):
        self.backend.table = {
            (255, 0, 0): [detection([1, 0])],
            (0, 255, 0): [detection([math.cos(math.radians(35)), math.sin(math.radians(35))])],
            (0, 0, 255): [detection([math.cos(math.radians(70)), math.sin(math.radians(70))])],
        }
        self.photo('a.png', (255, 0, 0))
        self.photo('b.png', (0, 255, 0))
        self.photo('c.png', (0, 0, 255))
        result = self.build(similarity=.8)
        self.assertEqual(2, result['people'])
        self.assertEqual([2, 1], [row['photo_count'] for row in self.index.people()['rows']])

    def test_distinct_faces_in_one_photo_cannot_be_same_person(self):
        self.backend.table[(255, 0, 0)] = [detection([1, 0]), detection([1, 0], [.72, .15, .95, .7])]
        self.photo()
        result = self.build()
        self.assertEqual(2, result['faces'])
        self.assertEqual(2, result['people'])
        self.assertEqual([1, 1], [row['photo_count'] for row in self.index.people()['rows']])

    def test_cancel_new_generation_preserves_previous_visible_index(self):
        self.photo('a.png')
        first = self.build()
        self.photo('b.png', (0, 255, 0))
        cancel = threading.Event()

        def progress(value):
            if value['phase'] == 'faces_index':
                cancel.set()

        with patch.object(self.index, 'capabilities', return_value={'ready': True}):
            result = self.index.build(self.config(), cancel=cancel, progress=progress)
        self.assertTrue(result['cancelled'])
        self.assertEqual(first['run_id'], self.index.status()['run_id'])
        self.assertEqual(1, self.index.photos()['total'])
        self.assertEqual(1, FaceIndex(self.index.data_dir).people()['total'])

    def test_cancel_during_clustering_keeps_previous_generation(self):
        self.photo('a.png')
        first = self.build()
        self.photo('b.png', (0, 255, 0))
        cancel = threading.Event()

        def progress(value):
            if value['phase'] == 'faces_cluster':
                cancel.set()

        with patch.object(self.index, 'capabilities', return_value={'ready': True}):
            result = self.index.build(self.config(), cancel=cancel, progress=progress)
        self.assertTrue(result['cancelled'])
        self.assertEqual(first['run_id'], self.index.status()['run_id'])
        self.assertEqual(1, self.index.people()['total'])

    def test_crash_new_generation_keeps_previous_index_browsable(self):
        self.photo('a.png')
        first = self.build()
        self.photo('b.png', (0, 255, 0))
        self.backend.table[(0, 255, 0)] = Crash('simulated interrupted inference')
        with self.assertRaises(Crash):
            self.build()
        reopened = FaceIndex(self.index.data_dir)
        self.assertEqual(first['run_id'], reopened.status()['run_id'])
        self.assertEqual(1, reopened.photos()['total'])

    def test_thumbnail_is_memory_only_cropped_and_has_no_exif(self):
        exif = Image.Exif()
        exif[270] = 'private metadata'
        self.photo(exif=exif)
        self.build()
        person = self.index.people()['rows'][0]
        before = set(self.index.data_dir.rglob('*'))
        data, mime = self.index.thumbnail(person['representative_image_id'], person['representative_face_id'], max_size=64)
        self.assertEqual('image/jpeg', mime)
        with Image.open(io.BytesIO(data)) as image:
            self.assertLessEqual(max(image.size), 64)
            self.assertEqual({}, dict(image.getexif()))
        self.assertEqual(before, set(self.index.data_dir.rglob('*')))

    def test_exif_orientation_is_applied_without_changing_original(self):
        exif = Image.Exif()
        exif[274] = 6
        original = self.photo('rotated.jpg', size=(120, 80), exif=exif)
        before = original.read_bytes()
        result = self.build()
        self.assertEqual(1, result['indexed'])
        row = self.index.photos()['rows'][0]
        self.assertEqual((80, 120), (row['width'], row['height']))
        data, _ = self.index.thumbnail(row['id'])
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual((80, 120), image.size)
        self.assertEqual(before, original.read_bytes())

    def test_changed_original_cannot_be_served_as_old_thumbnail(self):
        original = self.photo()
        self.build()
        image_id = self.index.photos()['rows'][0]['id']
        previous = original.stat()
        raw = bytearray(original.read_bytes())
        raw[-1] ^= 1
        original.write_bytes(raw)
        os.utime(original, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with self.assertRaises(SafetyError):
            self.index.thumbnail(image_id)
        self.assertEqual(bytes(raw), original.read_bytes())

    def test_face_id_must_belong_to_requested_image(self):
        self.photo('a.png')
        self.photo('b.png', (0, 255, 0))
        self.build()
        rows = self.index.photos()['rows']
        with self.assertRaises(KeyError):
            self.index.thumbnail(rows[0]['id'], rows[1]['faces'][0]['id'])
        with self.assertRaises(KeyError):
            self.index.photo('../../arbitrary-file')

    def test_corrupt_raw_and_oversized_images_report_errors_not_data_loss(self):
        corrupt = self.photos / 'broken.jpg'
        corrupt.write_bytes(b'not a jpeg')
        raw = self.photos / 'camera.arw'
        raw.write_bytes(b'raw file retained')
        large = self.photo('large.png', size=(1100, 1000))
        result = self.build(max_pixels=1_000_000)
        self.assertEqual(3, result['scanned'])
        self.assertEqual(3, result['errors'])
        self.assertEqual(0, result['indexed'])
        self.assertEqual(b'not a jpeg', corrupt.read_bytes())
        self.assertEqual(b'raw file retained', raw.read_bytes())
        self.assertTrue(large.exists())
        self.assertEqual(3, self.index.photos()['total'])

    def test_disguised_eps_is_rejected_before_external_decoder(self):
        from PIL import EpsImagePlugin
        output = io.BytesIO()
        Image.new('RGB', (16, 16)).save(output, format='EPS')
        original = self.photos / 'disguised.jpg'
        original.write_bytes(output.getvalue())
        with patch.object(EpsImagePlugin, 'Ghostscript', side_effect=AssertionError('must never run')) as decoder:
            with self.assertRaises(UnidentifiedImageError):
                _decode(output.getvalue(), '.jpg', 1600, 48_000_000)
            result = self.build()
        decoder.assert_not_called()
        self.assertEqual(1, result['errors'])
        self.assertEqual(0, result['indexed'])
        self.assertEqual(output.getvalue(), original.read_bytes())

    def test_nonwhitelisted_pillow_formats_are_rejected_before_load(self):
        output = io.BytesIO()
        Image.new('RGB', (16, 16)).save(output, format='PPM')
        with self.assertRaises(UnidentifiedImageError):
            _decode(output.getvalue(), '.png', 1600, 48_000_000)

    def test_malformed_face_data_does_not_enter_index(self):
        self.photo()
        self.backend.table[(255, 0, 0)] = [detection([math.nan, 1])]
        result = self.build()
        self.assertEqual(1, result['errors'])
        self.assertEqual(0, result['faces'])
        self.assertEqual(0, self.index.people()['total'])

    def test_project_and_linked_subtrees_are_skipped(self):
        self.photo('safe.png')
        project = self.photos / 'project'
        self.photo('private.png', root=project)
        (project / 'pyproject.toml').write_text('[project]', encoding='utf-8')
        outside = self.base / 'outside'
        self.photo('private.png', root=outside)
        linked = self.photos / 'linked'
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except OSError:
            if os.name == 'nt':
                import _winapi
                _winapi.CreateJunction(str(outside), str(linked))
            else:
                self.skipTest('Directory links unavailable')
        result = self.build()
        self.assertEqual(1, result['scanned'])
        self.assertGreaterEqual(result['skipped'], 2)
        self.assertEqual(1, self.index.photos()['total'])

    def test_sources_cannot_overlap_index_state_or_linked_root(self):
        with self.assertRaises(SafetyError):
            self.index.build(self.config(sources=[str(self.base)]))
        with self.assertRaises(SafetyError):
            self.index.build(self.config(sources=[str(self.index.data_dir)]))

    def test_open_database_never_follows_sidecar_link(self):
        unrelated = self.base / 'unrelated.txt'
        unrelated.write_text('private content', encoding='utf-8')
        sidecar = Path(str(self.index.db_path) + '-journal')
        try:
            os.symlink(unrelated, sidecar)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(SafetyError):
            self.index.people()
        self.assertEqual('private content', unrelated.read_text(encoding='utf-8'))

    def test_index_database_hardlink_is_refused_before_writes(self):
        alias = self.base / 'unrelated-db-copy.sqlite3'
        try:
            os.link(self.index.db_path, alias)
        except OSError as exc:
            self.skipTest(str(exc))
        before = alias.read_bytes()
        with self.assertRaises(SafetyError):
            self.index.people()
        self.assertEqual(before, alias.read_bytes())

    def test_overlapping_sources_are_deduplicated_in_scan(self):
        nested = self.photos / 'nested'
        self.photo(root=nested)
        result = self.build(sources=[str(self.photos), str(nested)])
        self.assertEqual(1, result['scanned'])
        self.assertEqual(1, result['indexed'])

    def test_photo_with_ntfs_download_marker_is_read_only_and_supported(self):
        if os.name != 'nt':
            self.skipTest('NTFS alternate data streams')
        original = self.photo()
        stream = Path(str(original) + ':Zone.Identifier')
        stream.write_bytes(b'[ZoneTransfer]\r\nZoneId=3\r\n')
        result = self.build()
        self.assertEqual(1, result['indexed'])
        self.assertEqual(b'[ZoneTransfer]\r\nZoneId=3\r\n', stream.read_bytes())


if __name__ == '__main__':
    unittest.main()
