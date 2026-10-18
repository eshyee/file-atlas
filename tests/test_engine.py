"""Safety regressions use disposable files, never a user's real documents."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fileatlas.engine import Engine, JournalError, QUARANTINE, SafetyError


class SimulatedCrash(BaseException):
    pass


class EngineSafetyTests(unittest.TestCase):
    def setUp(self):
        # Windows' default temp folder is inside protected AppData.  Fixtures
        # live in a unique user-owned directory instead, like real Documents.
        temp_parent = None
        if os.name == 'nt':
            temp_parent = Path.home() / '.fileatlas-tests'
            temp_parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='fileatlas-test-', dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.destination = self.base / 'output'
        self.state = self.base / 'state'
        self.engine = Engine(self.state)

    def file(self, relative='a.txt', contents=b'important original data', root=None):
        path = (root or self.source) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def scan(self, mode='merge', operation='copy', sources=None, **extra):
        return self.engine.scan({'mode': mode, 'operation': operation,
                                 'sources': [str(p) for p in (sources or [self.source])],
                                 'destination': str(self.destination), **extra})

    def tree(self, root):
        return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in root.rglob('*') if p.is_file()}

    def execute_one(self, operation='copy'):
        original = self.file()
        plan = self.scan(operation=operation)
        target = Path(plan['items'][0]['destination'])
        result = self.engine.execute(plan['id'])
        self.assertEqual('completed', result['status'], result)
        return original, target, result

    def test_scan_is_read_only_and_preserves_archive_tree(self):
        self.file('nested/a.zip', b'not extracted or rewritten')
        self.file('photo.jpg', b'photo')
        before = self.tree(self.source)
        plan = self.scan()
        self.assertEqual(before, self.tree(self.source))
        self.assertFalse(self.destination.exists())
        self.assertEqual(2, plan['summary']['planned'])
        self.assertTrue(all(item['sha256'] for item in plan['items']))
        self.assertTrue(any(Path(item['destination']).parts[-3:] == ('source', 'nested', 'a.zip') for item in plan['items']))

    def test_copy_and_undo_keep_original(self):
        original, target, result = self.execute_one()
        self.assertEqual(original.read_bytes(), target.read_bytes())
        self.assertEqual(original.stat().st_mtime_ns, target.stat().st_mtime_ns)
        restored = self.engine.restore(result['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertTrue(original.exists())
        self.assertFalse(target.exists())
        self.assertFalse(restored['restorable'])

    def test_move_and_restore_after_restart(self):
        original, target, result = self.execute_one('move')
        self.assertFalse(original.exists())
        self.assertTrue(target.exists())
        reopened = Engine(self.state)
        self.assertTrue(reopened.history()[0]['restorable'])
        restored = reopened.restore(result['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertEqual(b'important original data', original.read_bytes())
        self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows creation-time regression')
    def test_windows_creation_time_survives_copy_and_restore(self):
        original = self.file()
        creation = getattr(original.stat(), 'st_birthtime_ns', original.stat().st_ctime_ns)
        plan = self.scan(operation='move')
        result = self.engine.execute(plan['id'])
        target = Path(plan['items'][0]['destination'])
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(creation, getattr(target.stat(), 'st_birthtime_ns', target.stat().st_ctime_ns))
        restored = self.engine.restore(result['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertEqual(creation, getattr(original.stat(), 'st_birthtime_ns', original.stat().st_ctime_ns))

    def test_dedup_keeps_source_order_and_quarantines_reversibly(self):
        preferred = self.base / 'z-preferred'
        secondary = self.base / 'a-secondary'
        keeper = self.file('z.txt', b'same content', preferred)
        redundant = self.file('a.txt', b'same content', secondary)
        plan = self.scan('dedup', sources=[preferred, secondary])
        self.assertEqual(str(keeper), plan['duplicates'][0]['keeper'])
        self.assertEqual(1, plan['summary']['duplicate_files'])
        self.assertEqual(2, len(plan['duplicates'][0]['files']))
        target = Path(plan['items'][0]['destination'])
        self.assertTrue(target.is_relative_to(secondary / QUARANTINE / plan['id']))
        result = self.engine.execute(plan['id'])
        self.assertEqual('completed', result['status'], result)
        self.assertTrue(keeper.exists())
        self.assertFalse(redundant.exists())
        self.assertEqual(b'same content', target.read_bytes())
        self.assertEqual('restored', self.engine.restore(result['id'])['status'])
        self.assertEqual(b'same content', redundant.read_bytes())
        self.assertTrue(keeper.exists())

    def test_zero_byte_files_are_never_removed(self):
        a = self.file('a.txt', b'')
        b = self.file('b.txt', b'')
        for mode in ('dedup', 'merge', 'classify'):
            with self.subTest(mode=mode):
                plan = self.scan(mode, operation='move')
                self.assertEqual([], plan['items'])
                self.assertTrue(a.exists())
                self.assertTrue(b.exists())

    def test_hash_collision_is_not_a_duplicate(self):
        a = self.file('a.bin', b'AAA')
        b = self.file('b.bin', b'BBB')
        with patch('fileatlas.engine._hash_file', return_value='a' * 64):
            plan = self.scan('dedup')
        self.assertEqual([], plan['items'])
        self.assertEqual(b'AAA', a.read_bytes())
        self.assertEqual(b'BBB', b.read_bytes())
        self.assertTrue(any('逐字节' in row['reason'] for row in plan['skipped']))

    def test_hardlinks_are_kept_as_same_physical_file(self):
        original = self.file('a.txt')
        linked = self.source / 'b.txt'
        try:
            os.link(original, linked)
        except OSError as exc:
            self.skipTest(str(exc))
        plan = self.scan('dedup')
        self.assertEqual([], plan['items'])
        self.assertTrue(linked.exists())
        self.assertTrue(any('硬链接' in row['reason'] for row in plan['skipped']))

    def test_changed_keeper_refuses_to_remove_redundant_file(self):
        keeper = self.file('a.txt', b'content')
        redundant = self.file('b.txt', b'content')
        plan = self.scan('dedup')
        keeper.write_bytes(b'changed')
        result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertEqual(0, result['completed'])
        self.assertEqual(b'content', redundant.read_bytes())
        self.assertFalse(Path(plan['items'][0]['destination']).exists())

    def test_keeper_changed_after_copy_still_preserves_original(self):
        keeper = self.file('a.txt', b'content')
        redundant = self.file('b.txt', b'content')
        plan = self.scan('dedup')
        publish = self.engine._publish_copy

        def change_keeper(*args, **kwargs):
            result = publish(*args, **kwargs)
            keeper.write_bytes(b'CHANGED')
            return result

        with patch.object(self.engine, '_publish_copy', side_effect=change_keeper):
            result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertEqual(b'content', redundant.read_bytes())
        target = Path(plan['items'][0]['destination'])
        self.assertEqual(b'content', target.read_bytes())
        self.assertEqual('restored', self.engine.restore(result['id'])['status'])
        self.assertFalse(target.exists())

    def test_changed_source_before_execute_is_preserved(self):
        original = self.file()
        plan = self.scan(operation='move')
        original.write_bytes(b'new user content')
        result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertEqual(b'new user content', original.read_bytes())
        self.assertFalse(Path(plan['items'][0]['destination']).exists())

    def test_same_size_and_timestamp_content_change_is_detected(self):
        original = self.file(contents=b'AAAA')
        plan = self.scan(operation='move')
        before = original.stat()
        original.write_bytes(b'BBBB')
        os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertEqual(b'BBBB', original.read_bytes())
        self.assertFalse(Path(plan['items'][0]['destination']).exists())

    def test_collision_in_preview_gets_a_separate_name(self):
        self.file()
        old_target = self.destination / self.source.name / 'a.txt'
        old_target.parent.mkdir(parents=True)
        old_target.write_bytes(b'preexisting')
        plan = self.scan()
        self.assertNotEqual(str(old_target), plan['items'][0]['destination'])
        result = self.engine.execute(plan['id'])
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(b'preexisting', old_target.read_bytes())

    def test_target_appearing_after_preview_is_never_overwritten(self):
        original = self.file()
        plan = self.scan(operation='move')
        target = Path(plan['items'][0]['destination'])
        target.parent.mkdir(parents=True)
        target.write_bytes(b'new user file')
        result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertEqual(b'new user file', target.read_bytes())
        self.assertTrue(original.exists())

    def test_destination_inside_source_is_excluded(self):
        self.file('a.txt')
        self.destination = self.source / 'output'
        self.file('existing.txt', b'already organized', self.destination)
        plan = self.scan()
        self.assertEqual(1, len(plan['items']))
        self.assertEqual('a.txt', Path(plan['items'][0]['source']).name)

    def test_destination_same_or_parent_of_source_is_refused(self):
        self.file()
        for target in (self.source, self.base):
            with self.subTest(target=target), self.assertRaises(SafetyError):
                self.destination = target
                self.scan()

    def test_overlapping_sources_are_not_counted_twice(self):
        self.file('nested/a.txt')
        plan = self.scan(sources=[self.source, self.source / 'nested'])
        self.assertEqual(1, plan['summary']['files'])
        self.assertEqual(1, len(plan['items']))

    def test_duplicate_root_names_keep_separate_merge_trees(self):
        first = self.base / 'left' / 'photos'
        second = self.base / 'right' / 'photos'
        self.file('a.txt', b'left', first)
        self.file('a.txt', b'right', second)
        plan = self.scan(sources=[first, second])
        targets = [Path(item['destination']).relative_to(self.destination).parts[0] for item in plan['items']]
        self.assertEqual(['photos', 'photos (2)'], targets)

    def test_source_root_and_ancestor_projects_are_refused(self):
        self.file('package.json', b'{}')
        child = self.source / 'documents'
        child.mkdir()
        for root in (self.source, child):
            with self.subTest(root=root), self.assertRaises(SafetyError):
                self.scan(sources=[root])

    def test_nested_project_is_skipped_entirely(self):
        kept = self.file('a.txt')
        project = self.source / 'project'
        self.file('pyproject.toml', b'[project]', project)
        code = self.file('private.txt', b'leave project alone', project)
        plan = self.scan(operation='move')
        self.assertEqual([str(kept)], [i['source'] for i in plan['items']])
        result = self.engine.execute(plan['id'])
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(b'leave project alone', code.read_bytes())

    def test_logs_dependencies_quarantine_and_state_cannot_be_sources(self):
        for name in (QUARANTINE, 'node_modules', '.venv', 'media_classify_logs', 'cross_drive_dedup_logs'):
            root = self.source / name
            self.file('a.txt', b'preserve', root)
            with self.subTest(name=name), self.assertRaises(SafetyError):
                self.scan(sources=[root])
        with self.assertRaises(SafetyError):
            self.scan(sources=[self.state])
        plan = self.scan()
        self.assertEqual([], plan['items'])

    def test_reserved_destinations_are_refused(self):
        self.file()
        for name in (QUARANTINE, 'node_modules', 'organizer_logs'):
            self.destination = self.base / name
            with self.subTest(name=name), self.assertRaises(SafetyError):
                self.scan()

    def test_application_data_folders_are_protected_but_state_is_allowed(self):
        for name in ('AppData', '.cache', '.config', '.local'):
            root = self.source / name
            self.file('private.txt', b'application state', root)
            with self.subTest(name=name), self.assertRaises(SafetyError):
                self.scan(sources=[root])
            # Journal storage is intentionally allowed in AppData, unlike sources.
            self.assertEqual(root / 'state', Engine(root / 'state').data_dir)
        self.assertEqual([], self.scan()['items'])

    @unittest.skipUnless(os.name == 'nt', 'NTFS alternate data streams')
    def test_ads_files_are_kept_with_visible_skip_reason(self):
        original = self.file('download.txt', b'file content')
        stream = Path(str(original) + ':Zone.Identifier')
        try:
            stream.write_bytes(b'[ZoneTransfer]\r\nZoneId=3\r\n')
        except OSError as exc:
            self.skipTest(f'Fixture filesystem does not support ADS: {exc}')
        plan = self.scan(operation='move')
        self.assertEqual([], plan['items'])
        self.assertTrue(any('附加数据流' in row['reason'] for row in plan['skipped']))
        self.assertEqual(b'[ZoneTransfer]\r\nZoneId=3\r\n', stream.read_bytes())
        self.assertEqual(b'file content', original.read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'NTFS alternate data streams')
    def test_ads_added_after_preview_prevents_move(self):
        original = self.file()
        plan = self.scan(operation='move')
        stream = Path(str(original) + ':user-data')
        stream.write_bytes(b'added user data')
        result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertEqual(b'added user data', stream.read_bytes())
        self.assertTrue(original.exists())
        self.assertFalse(Path(plan['items'][0]['destination']).exists())

    @unittest.skipUnless(os.name == 'nt', 'NTFS alternate data streams')
    def test_ads_added_to_destination_prevents_destructive_undo(self):
        original, target, result = self.execute_one()
        stream = Path(str(target) + ':user-data')
        stream.write_bytes(b'added target data')
        restored = self.engine.restore(result['id'])
        self.assertEqual('restore_partial', restored['status'], restored)
        self.assertTrue(original.exists())
        self.assertTrue(target.exists())
        self.assertEqual(b'added target data', stream.read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'NTFS alternate data streams')
    def test_ads_added_between_copy_and_unlink_preserves_original(self):
        original = self.file()
        plan = self.scan(operation='move')
        publish = self.engine._publish_copy
        stream = Path(str(original) + ':user-data')

        def add_stream(*args, **kwargs):
            result = publish(*args, **kwargs)
            stream.write_bytes(b'late data')
            return result

        with patch.object(self.engine, '_publish_copy', side_effect=add_stream):
            result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertTrue(original.exists())
        self.assertTrue(Path(plan['items'][0]['destination']).exists())
        self.assertEqual(b'late data', stream.read_bytes())

    def test_state_inside_source_is_excluded(self):
        self.engine = Engine(self.source / 'application-state')
        self.file('private.log', b'journal', self.engine.data_dir)
        user_file = self.file()
        plan = self.scan()
        self.assertEqual([str(user_file)], [i['source'] for i in plan['items']])

    def create_directory_link(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            if os.name == 'nt':
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
            else:
                raise

    def test_linked_source_target_and_state_ancestors_are_refused(self):
        real = self.base / 'real'
        real.mkdir()
        self.file('a.txt', b'data', real)
        linked = self.base / 'linked'
        try:
            self.create_directory_link(real, linked)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(SafetyError):
            self.scan(sources=[linked])
        self.destination = linked / 'target'
        with self.assertRaises(SafetyError):
            self.scan()
        with self.assertRaises(SafetyError):
            Engine(linked / 'state')

    def test_nested_directory_link_is_not_traversed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        kept = self.file('private.txt', b'outside', outside)
        try:
            self.create_directory_link(outside, self.source / 'linked')
        except OSError as exc:
            self.skipTest(str(exc))
        plan = self.scan()
        self.assertEqual([], plan['items'])
        self.assertTrue(kept.exists())
        self.assertTrue(any('重解析点' in row['reason'] for row in plan['skipped']))

    def test_symlink_replacement_after_preview_is_refused(self):
        original = self.file()
        outside = self.file('outside.txt', b'outside', self.base)
        plan = self.scan(operation='move')
        original.unlink()
        try:
            os.symlink(outside, original)
        except OSError as exc:
            self.skipTest(str(exc))
        result = self.engine.execute(plan['id'])
        self.assertEqual('partial', result['status'], result)
        self.assertEqual(b'outside', outside.read_bytes())
        self.assertTrue(original.is_symlink())

    def test_cancelled_scan_writes_nothing_and_cannot_execute(self):
        self.file()
        cancel = threading.Event()
        cancel.set()
        plan = self.engine.scan({'sources': [str(self.source)], 'mode': 'merge', 'destination': str(self.destination)}, cancel=cancel)
        self.assertTrue(plan['cancelled'])
        self.assertFalse(self.destination.exists())
        with self.assertRaises(SafetyError):
            self.engine.execute(plan['id'])

    def test_cancel_after_first_move_leaves_rest_recoverable(self):
        first = self.file('a.txt', b'first')
        second = self.file('b.txt', b'second')
        plan = self.scan(operation='move')
        cancel = threading.Event()

        def progress(value):
            if value['phase'] == 'execute' and value['processed'] == 1:
                cancel.set()

        result = self.engine.execute(plan['id'], cancel=cancel, progress=progress)
        self.assertEqual('cancelled', result['status'], result)
        self.assertEqual(1, result['completed'])
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual('restored', self.engine.restore(result['id'])['status'])
        self.assertEqual(b'first', first.read_bytes())
        self.assertEqual(b'second', second.read_bytes())

    def test_undo_refuses_changed_target(self):
        original, target, result = self.execute_one()
        target.write_bytes(b'user edited target')
        restored = self.engine.restore(result['id'])
        self.assertEqual('restore_partial', restored['status'], restored)
        self.assertTrue(original.exists())
        self.assertEqual(b'user edited target', target.read_bytes())

    def test_undo_refuses_changed_original(self):
        original, target, result = self.execute_one()
        original.write_bytes(b'user edited original')
        restored = self.engine.restore(result['id'])
        self.assertEqual('restore_partial', restored['status'], restored)
        self.assertEqual(b'user edited original', original.read_bytes())
        self.assertTrue(target.exists())

    def test_undo_preserves_only_remaining_copy(self):
        original, target, result = self.execute_one()
        original.unlink()
        restored = self.engine.restore(result['id'])
        self.assertEqual('restore_partial', restored['status'], restored)
        self.assertEqual(b'important original data', target.read_bytes())

    def test_restore_move_refuses_to_overwrite_new_original(self):
        original, target, result = self.execute_one('move')
        original.write_bytes(b'new user file')
        restored = self.engine.restore(result['id'])
        self.assertEqual('restore_partial', restored['status'], restored)
        self.assertEqual(b'new user file', original.read_bytes())
        self.assertTrue(target.exists())

    def crash_at(self, event, callback):
        append = self.engine._append

        def fail(journal, kind, **fields):
            if kind == event:
                raise SimulatedCrash(kind)
            return append(journal, kind, **fields)

        with patch.object(self.engine, '_append', side_effect=fail), self.assertRaises(SimulatedCrash):
            callback()

    def test_crash_after_publish_before_record_can_undo(self):
        original = self.file()
        plan = self.scan(operation='move')
        self.crash_at('published', lambda: self.engine.execute(plan['id']))
        target = Path(plan['items'][0]['destination'])
        self.assertTrue(original.exists())
        self.assertTrue(target.exists())
        reopened = Engine(self.state)
        self.assertEqual('recoverable', reopened.history()[0]['status'])
        restored = reopened.restore(plan['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertTrue(original.exists())
        self.assertFalse(target.exists())

    def crash_during_streaming(self, plan):
        append = self.engine._append
        active = {'streaming': False, 'checks': 0}

        def track(journal, event, **fields):
            result = append(journal, event, **fields)
            if event == 'stage_created':
                active['streaming'] = True
            return result

        def fail_during_second_chunk(cancel):
            if active['streaming']:
                active['checks'] += 1
                if active['checks'] == 2:
                    raise SimulatedCrash('partial streaming copy')

        with patch.object(self.engine, '_append', side_effect=track), \
                patch('fileatlas.engine._check_cancel', side_effect=fail_during_second_chunk), \
                self.assertRaises(SimulatedCrash):
            self.engine.execute(plan['id'])

    def test_crash_with_partial_stage_recovers_and_cleans_verified_prefix(self):
        original = self.file(contents=b'x' * (3 * 1024 * 1024))
        plan = self.scan(operation='move')
        self.crash_during_streaming(plan)
        stages = list(self.destination.rglob('*.tmp'))
        self.assertEqual(1, len(stages))
        self.assertGreater(stages[0].stat().st_size, 0)
        self.assertLess(stages[0].stat().st_size, original.stat().st_size)
        self.assertTrue(original.exists())
        restored = Engine(self.state).restore(plan['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertFalse(stages[0].exists())
        self.assertEqual(b'x' * (3 * 1024 * 1024), original.read_bytes())

    def test_changed_partial_stage_is_preserved_for_manual_inspection(self):
        original = self.file(contents=b'x' * (3 * 1024 * 1024))
        plan = self.scan(operation='move')
        self.crash_during_streaming(plan)
        stage = next(self.destination.rglob('*.tmp'))
        stage.write_bytes(b'user content inside stage')
        restored = Engine(self.state).restore(plan['id'])
        self.assertEqual('restore_partial', restored['status'], restored)
        self.assertTrue(original.exists())
        self.assertEqual(b'user content inside stage', stage.read_bytes())

    def test_crash_after_original_removed_can_restore(self):
        original = self.file()
        plan = self.scan(operation='move')
        self.crash_at('committed', lambda: self.engine.execute(plan['id']))
        self.assertFalse(original.exists())
        reopened = Engine(self.state)
        restored = reopened.restore(plan['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertEqual(b'important original data', original.read_bytes())

    def test_crash_during_restore_publication_can_resume(self):
        original, target, result = self.execute_one('move')
        self.crash_at('restore_published', lambda: self.engine.restore(result['id']))
        self.assertTrue(original.exists())
        self.assertTrue(target.exists())
        reopened = Engine(self.state)
        restored = reopened.restore(result['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertEqual(b'important original data', original.read_bytes())
        self.assertFalse(target.exists())

    def test_crash_after_restore_removal_can_resume(self):
        original, target, result = self.execute_one('move')
        self.crash_at('restored', lambda: self.engine.restore(result['id']))
        self.assertTrue(original.exists())
        self.assertFalse(target.exists())
        restored = Engine(self.state).restore(result['id'])
        self.assertEqual('restored', restored['status'], restored)

    def test_torn_final_journal_record_is_repaired_before_recovery(self):
        original, target, result = self.execute_one('move')
        journal = self.state / f"{result['id']}.jsonl"
        with journal.open('ab') as output:
            output.write(b'{"event":"interrupted')
        reopened = Engine(self.state)
        restored = reopened.restore(result['id'])
        self.assertEqual('restored', restored['status'], restored)
        self.assertTrue(original.exists())
        self.assertFalse(target.exists())
        for line in journal.read_text(encoding='utf-8').splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_journal_short_writes_are_completed_before_mutation(self):
        original_fdopen = os.fdopen

        class ShortWriter:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

            def write(self, data):
                return self.wrapped.write(data[:max(1, len(data) // 2)])

            def fileno(self):
                return self.wrapped.fileno()

        def wrap(descriptor, mode, **kwargs):
            opened = original_fdopen(descriptor, mode, **kwargs)
            return ShortWriter(opened) if mode == 'ab' else opened

        self.file()
        plan = self.scan(operation='move')
        with patch('fileatlas.engine.os.fdopen', side_effect=wrap):
            result = self.engine.execute(plan['id'])
        self.assertEqual('completed', result['status'], result)
        self.assertEqual('restored', Engine(self.state).restore(result['id'])['status'])

    def test_zero_byte_journal_write_aborts_before_original_removal(self):
        original = self.file()
        plan = self.scan(operation='move')
        append = self.engine._append
        original_fdopen = os.fdopen

        class FailedWriter:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

            def write(self, data):
                self.wrapped.write(data[:3])
                return 0

            def fileno(self):
                return self.wrapped.fileno()

        def wrap(descriptor, mode, **kwargs):
            return FailedWriter(original_fdopen(descriptor, mode, **kwargs))

        def fail_removal_record(journal, event, **fields):
            if event == 'remove_intent':
                with patch('fileatlas.engine.os.fdopen', side_effect=wrap):
                    return append(journal, event, **fields)
            return append(journal, event, **fields)

        with patch.object(self.engine, '_append', side_effect=fail_removal_record), self.assertRaises(JournalError):
            self.engine.execute(plan['id'])
        self.assertTrue(original.exists())
        self.assertTrue(Path(plan['items'][0]['destination']).exists())
        reopened = Engine(self.state)
        self.assertEqual('recoverable', reopened.history()[0]['status'])
        self.assertEqual('restored', reopened.restore(plan['id'])['status'])
        self.assertEqual(b'important original data', original.read_bytes())

    def test_selected_ids_and_repeated_execution_are_checked(self):
        self.file('a.txt', b'first')
        self.file('b.txt', b'second')
        plan = self.scan()
        with self.assertRaises(SafetyError):
            self.engine.execute(plan['id'], ['not-in-plan'])
        result = self.engine.execute(plan['id'], [plan['items'][0]['id']])
        self.assertEqual(1, result['total'])
        self.assertTrue(Path(plan['items'][0]['destination']).exists())
        self.assertFalse(Path(plan['items'][1]['destination']).exists())
        with self.assertRaises(SafetyError):
            self.engine.execute(plan['id'])

    def test_classification_respects_category_filter_and_keeps_archives(self):
        document = self.file('a.txt', b'document')
        archive = self.file('archive.zip', b'archive')
        plan = self.scan('classify', categories=['document'])
        self.assertEqual([str(document)], [row['source'] for row in plan['items']])
        self.assertEqual('document', plan['items'][0]['category'])
        self.assertTrue(archive.exists())
        self.assertIn('document_classify', plan['items'][0]['destination'])


if __name__ == '__main__':
    unittest.main()
