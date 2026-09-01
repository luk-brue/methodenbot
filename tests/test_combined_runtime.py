from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import main


class FakeThread:
    instances = []

    def __init__(self, *, target, args=(), name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        self.joined = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.joined = timeout == 5


class CombinedRuntimeTests(unittest.TestCase):
    def test_multi_controller_worker_and_digest_are_one_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            bindings = (
                ('@primary:example.invalid', '!primary:example.invalid'),
                ('@second:example.invalid', '!second:example.invalid'),
            )
            config = SimpleNamespace(
                control_state_dir=str(base / 'control'),
                digest_state_dir=str(base / 'digest'),
                control_bindings=Mock(return_value=bindings),
            )
            primary = Mock()
            primary.snapshot.return_value = {'ai_enabled': True}
            primary.head.return_value = {'event_id': '$queued'}
            secondary = Mock()
            secondary.head.return_value = None
            digest_state = Mock()
            digest_service = Mock()
            worker = Mock()
            listeners = []
            listener_kwargs = []
            bootstrap_order = []
            digest_service.bootstrap.side_effect = lambda: bootstrap_order.append('digest')

            def make_listener(_bot, _config, state, _ai_service, **kwargs):
                listener = Mock()
                listener.state = state
                listener.bootstrap.side_effect = lambda: bootstrap_order.append('control')
                listeners.append(listener)
                listener_kwargs.append(kwargs)
                return listener

            FakeThread.instances = []
            with (patch.object(main, 'clear_control_ready') as clear_ready,
                  patch.object(main, 'write_control_ready',
                               side_effect=lambda *_args: bootstrap_order.append('ready')) as write_ready,
                  patch.object(main, 'ControlState', return_value=secondary) as control_state,
                  patch.object(main, 'DigestState', return_value=digest_state) as digest_state_type,
                  patch.object(main, 'MatrixCommandListener', side_effect=make_listener),
                  patch.object(main, 'DigestService', return_value=digest_service) as digest_type,
                  patch.object(main, 'MatrixCommandWorker', return_value=worker) as worker_type,
                  patch.object(main.threading, 'Thread', FakeThread)):
                runtime = main.BackgroundServices(config, primary, Mock(), Mock())
                runtime.start()
                runtime.stop()

            clear_ready.assert_called_once_with(config.control_state_dir)
            config.control_bindings.assert_called_once_with()
            control_state.assert_called_once_with(
                main.controller_state_directory(
                    config.control_state_dir, *bindings[1]),
                ai_default=True)
            digest_state_type.assert_called_once_with(config.digest_state_dir)
            digest_type.assert_called_once_with(runtime.bot, config, digest_state)
            self.assertEqual([listener.state for listener in listeners], [primary, secondary])
            self.assertEqual(
                [(values['control_user'], values['room_id']) for values in listener_kwargs],
                list(bindings))
            self.assertIs(listener_kwargs[0]['ai_state'], primary)
            self.assertIs(listener_kwargs[1]['ai_state'], primary)
            self.assertIs(listener_kwargs[0]['execution_lock'],
                          listener_kwargs[1]['execution_lock'])
            primary.acquire_process_lock.assert_called_once_with()
            secondary.acquire_process_lock.assert_called_once_with()
            for listener in listeners:
                listener.bootstrap.assert_called_once_with()
                listener.stop_event.set.assert_called_once_with()
            digest_state.acquire_process_lock.assert_called_once_with()
            digest_service.bootstrap.assert_called_once_with()
            self.assertEqual(bootstrap_order, ['control', 'control', 'digest', 'ready'])
            digest_service.stop_event.set.assert_called_once_with()
            write_ready.assert_called_once_with(config.control_state_dir, bindings)
            worker_type.assert_called_once_with(listeners, runtime.work_event)
            worker.stop_event.set.assert_called_once_with()
            self.assertTrue(runtime.work_event.is_set())
            self.assertEqual(
                [thread.name for thread in FakeThread.instances],
                ['matrix-control-1', 'matrix-control-2',
                 'matrix-control-worker', 'matrix-digest'])
            self.assertIs(FakeThread.instances[0].target, listeners[0].run_poll_forever)
            self.assertIs(FakeThread.instances[1].target, listeners[1].run_poll_forever)
            self.assertIs(FakeThread.instances[2].target, worker.run_forever)
            self.assertIs(FakeThread.instances[3].target, digest_service.run_forever)
            self.assertEqual(FakeThread.instances[0].args, (runtime.work_event,))
            self.assertEqual(FakeThread.instances[1].args, (runtime.work_event,))
            self.assertTrue(all(thread.daemon for thread in FakeThread.instances))
            self.assertTrue(all(thread.started for thread in FakeThread.instances))
            self.assertTrue(all(thread.joined for thread in FakeThread.instances))


if __name__ == '__main__':
    unittest.main()
