import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest

from control_state import MAX_STATE_BYTES, ControlState, ControlStateError


def _try_process_lock(directory, connection):
    """Run in a fresh interpreter so no lock descriptor is inherited."""
    try:
        state = ControlState(directory)
        state.acquire_process_lock()
    except ControlStateError as exc:
        connection.send(('error', str(exc)))
    except Exception as exc:  # pragma: no cover - reported to the parent test
        connection.send(('unexpected', type(exc).__name__))
    else:
        connection.send(('acquired', None))
    finally:
        connection.close()


class ControlStatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'control'

    def test_planned_job_survives_real_object_reconstruction(self):
        first = ControlState(self.directory)
        first.bootstrap('sync-0')
        first.record_sync('sync-1', [('$command', 'Test')])
        parts = [{
            'msg': 'Test',
            'html_msg': '<p>Test</p>',
            'room_id': '!control:example.invalid',
            'thread_root_part': None,
            'transaction_id': 'stable-transaction',
            'event_id': None,
        }]
        first.plan_head('$command', False, parts)
        first.record_part('$command', 0, '$matrix-event')

        restarted = ControlState(self.directory)
        snapshot = restarted.snapshot()
        self.assertEqual(snapshot['since'], 'sync-1')
        self.assertFalse(snapshot['ai_enabled'])
        self.assertEqual(snapshot['queue'][0]['status'], 'planned')
        self.assertEqual(snapshot['queue'][0]['parts'][0]['transaction_id'],
                         'stable-transaction')
        self.assertEqual(snapshot['queue'][0]['parts'][0]['event_id'], '$matrix-event')

        restarted.complete_head('$command')
        after_second_restart = ControlState(self.directory).snapshot()
        self.assertEqual(after_second_restart['queue'], [])
        self.assertEqual(after_second_restart['completed'], ['$command'])

    def test_ai_toggle_persists_across_reconstruction(self):
        state = ControlState(self.directory)
        state.bootstrap('sync-0')
        state.record_sync('sync-1', [('$on', 'KI an')])
        self.assertTrue(state.set_ai('$on', True))
        state.complete_head('$on')

        restarted = ControlState(self.directory)
        self.assertTrue(restarted.snapshot()['ai_enabled'])
        restarted.record_sync('sync-2', [('$off', 'KI aus')])
        self.assertFalse(restarted.set_ai('$off', False))
        restarted.complete_head('$off')

        final = ControlState(self.directory).snapshot()
        self.assertFalse(final['ai_enabled'])
        self.assertEqual(final['since'], 'sync-2')
        self.assertEqual(final['completed'], ['$on', '$off'])

    def test_process_lock_excludes_fresh_process(self):
        state = ControlState(self.directory)
        state.acquire_process_lock()
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_try_process_lock,
                                  args=(str(self.directory), child))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10), 'child did not report its lock result')
            self.assertEqual(parent.recv(), ('error', 'control_listener_already_running'))
            process.join(10)
            self.assertFalse(process.is_alive(), 'child process did not exit')
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            parent.close()
            os.close(state._process_lock)
            state._process_lock = None

    def test_more_than_one_hundred_queued_jobs_is_atomic(self):
        state = ControlState(self.directory)
        state.bootstrap('sync-0')
        state.record_sync('sync-1', [('$existing', 'Test')])
        before = state.snapshot()
        jobs = [('$overflow-' + str(index), 'Test') for index in range(100)]

        with self.assertRaises(ControlStateError):
            state.record_sync('sync-must-not-stick', jobs)

        restarted = ControlState(self.directory)
        self.assertEqual(restarted.snapshot(), before)


class ControlStateSafetyTests(unittest.TestCase):
    def new_directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name) / 'control'

    def test_state_symlink_is_rejected(self):
        directory = self.new_directory()
        state = ControlState(directory)
        target = directory.parent / 'outside.json'
        target.write_text(json.dumps(state.snapshot()), encoding='utf-8')
        target.chmod(0o600)
        state.path.unlink()
        state.path.symlink_to(target)

        with self.assertRaises(ControlStateError):
            ControlState(directory)

    def test_wrong_state_file_permissions_are_rejected(self):
        directory = self.new_directory()
        state = ControlState(directory)
        state.path.chmod(0o644)

        with self.assertRaisesRegex(ControlStateError, 'unsafe_control_state_file'):
            ControlState(directory)

    def test_wrong_directory_permissions_are_rejected(self):
        directory = self.new_directory()
        directory.mkdir(mode=0o700)
        directory.chmod(0o755)

        with self.assertRaisesRegex(ControlStateError, 'unsafe_control_state_directory'):
            ControlState(directory)

    def test_corrupt_state_is_rejected(self):
        directory = self.new_directory()
        state = ControlState(directory)
        state.path.write_text('{not-json', encoding='utf-8')
        state.path.chmod(0o600)

        with self.assertRaisesRegex(ControlStateError, 'control_state_unreadable'):
            ControlState(directory)

    def test_oversized_state_is_rejected(self):
        directory = self.new_directory()
        state = ControlState(directory)
        with state.path.open('wb') as handle:
            handle.write(b'x' * (MAX_STATE_BYTES + 1))
        state.path.chmod(0o600)

        with self.assertRaises(ControlStateError):
            ControlState(directory)


if __name__ == '__main__':
    unittest.main()
