import asyncio
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from xhs_research import cli, runtime
from xhs_research.config import Config
from xhs_research.demo import DemoAdapter, FakeClock
from xhs_research.errors import NetworkFailure, ProtocolError, StopRun
from xhs_research.store import Store


@unittest.skipUnless(os.name == 'nt', 'Windows API and cmd.exe')
class WindowsOvernightTests(unittest.TestCase):
    def test_power_request_restored_on_normal_error_and_interrupt(self):
        for error in (None, RuntimeError, KeyboardInterrupt):
            setter = Mock(side_effect=[0x80000002, 0x80000001])
            with patch.object(ctypes.windll.kernel32, 'SetThreadExecutionState', setter):
                try:
                    with runtime.keep_awake(lambda *_: None) as active:
                        self.assertTrue(active)
                        if error:
                            raise error()
                except (RuntimeError, KeyboardInterrupt):
                    pass
            self.assertEqual([c.args[0] for c in setter.call_args_list], [0x80000001, 0x80000002])

    def test_power_request_failure_warns_without_attempting_release(self):
        setter = Mock(return_value=0)
        messages = []
        with patch.object(ctypes.windll.kernel32, 'SetThreadExecutionState', setter):
            with runtime.keep_awake(messages.append) as active:
                self.assertFalse(active)
        self.assertEqual(setter.call_count, 1)
        self.assertIn('未能启用', messages[0])

    def test_real_cmd_launchers_success_failure_and_missing_environment(self):
        root = Path(__file__).resolve().parents[1]
        for source in sorted(root.glob('0[2-8]_*.cmd')):
            data = source.read_bytes()
            self.assertTrue(data.isascii())
            self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
            for result in (0, 7, None):
                with self.subTest(script=source.name, result=result), tempfile.TemporaryDirectory(prefix='采集 space ') as folder:
                    directory = Path(folder)
                    (directory / 'scripts').mkdir()
                    (directory / 'scripts/launcher_message.py').write_bytes((root / 'scripts/launcher_message.py').read_bytes())
                    text = data.decode('ascii')
                    if result is not None:
                        exe = directory / '.venv/Scripts/python.exe'
                        exe.parent.mkdir(parents=True)
                        exe.touch()
                        # Replace only the collector invocation: never make network calls.
                        lines = text.splitlines()
                        for index, line in enumerate(lines):
                            if line.startswith('".venv\\Scripts\\python.exe" -m '):
                                lines[index] = '"%TEST_PYTHON%" -c "import sys; sys.exit(' + str(result) + ')"'
                            elif line.startswith('".venv\\Scripts\\python.exe" scripts'):
                                lines[index] = line.replace('".venv\\Scripts\\python.exe"', '"%TEST_PYTHON%"')
                        text = '\r\n'.join(lines) + '\r\n'
                    target = directory / source.name
                    target.write_bytes(text.encode('ascii'))
                    run = subprocess.run(['cmd.exe', '/d', '/c', str(target)], input=b'\r\n',
                                         capture_output=True, timeout=15,
                                         env={**os.environ, 'TEST_PYTHON': sys.executable})
                    self.assertEqual(run.returncode, 2 if result is None else result, run.stderr)
                    self.assertEqual(run.stderr, b'')
                    if result is not None:
                        self.assertIn('请按任意键关闭此窗口。', run.stdout.decode('utf-8'))
                    else:
                        self.assertIn(b'01_install.cmd', run.stdout)

    def test_install_script_exit_codes_without_installing(self):
        root = Path(__file__).resolve().parents[1]
        data = (root / '01_install.cmd').read_bytes()
        self.assertTrue(data.isascii())
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        for branch in ('py', 'python', 'missing'):
            for result in (0, 7):
                with self.subTest(branch=branch, result=result), tempfile.TemporaryDirectory(prefix='安装 space ') as folder:
                    directory = Path(folder)
                    (directory / 'scripts').mkdir()
                    (directory / 'scripts/launcher_message.py').write_bytes((root / 'scripts/launcher_message.py').read_bytes())
                    text = data.decode('ascii')
                    text = text.replace('where py >nul 2>nul', 'cmd /d /c exit ' + ('0' if branch == 'py' else '1'))
                    text = text.replace('where python >nul 2>nul', 'cmd /d /c exit ' + ('0' if branch == 'python' else '1'))
                    for executable in ('py -3', 'python'):
                        text = text.replace(executable + ' scripts\\setup_live.py --menu',
                                            '"%TEST_PYTHON%" -c "import sys; sys.exit(' + str(result) + ')"')
                        text = text.replace(executable + ' scripts\\launcher_message.py',
                                            '"%TEST_PYTHON%" scripts\\launcher_message.py')
                    target = directory / '01_install.cmd'
                    target.write_bytes(text.encode('ascii'))
                    run = subprocess.run(['cmd.exe', '/d', '/c', str(target)], input=b'\r\n',
                                         capture_output=True, timeout=15,
                                         env={**os.environ, 'TEST_PYTHON': sys.executable})
                    self.assertEqual(run.returncode, 2 if branch == 'missing' else result, run.stderr)
                    self.assertEqual(run.stderr, b'')


class RunDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_records_stop_network_protocol_error_and_cancellation(self):
        cases = [(StopRun('candidates_exhausted'), 'candidates_exhausted'),
                 (NetworkFailure('offline'), 'network_failure'),
                 (ProtocolError('bad'), 'protocol_error'),
                 (asyncio.CancelledError(), 'user_interrupted'),
                 (RuntimeError('SECRET'), 'unexpected_error:RuntimeError')]
        for error, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                config = Config(root=Path(folder))
                store = Store(config.state_dir)
                clock = FakeClock()
                adapter = DemoAdapter(clock)
                async def fail(*args, **kwargs):
                    raise error
                try:
                    with patch.object(cli, 'Clock', return_value=clock), patch.object(cli, 'MediaCrawlerAdapter', return_value=adapter), patch.object(cli.Engine, 'run', fail):
                        try:
                            await cli.collect(config, store, 'run')
                        except (asyncio.CancelledError, RuntimeError):
                            pass
                    run = store.status('2026-11-13')['last_run']
                    self.assertEqual(run['reason'], reason)
                    self.assertEqual(run['state'], 'stopped')
                    self.assertIsNotNone(run['ended_at'])
                    self.assertNotIn('SECRET', str(run))
                finally:
                    store.close()

    def test_unclean_exit_recognized_on_next_locked_start(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.start_run(100, 'run')
            store.touch_run(140)
            store.close()
            store = Store(folder)
            try:
                store.start_run(200, 'run')
                previous = store.status('2026-11-13')['previous_run']
                self.assertEqual(previous['state'], 'interrupted')
                self.assertEqual(previous['reason'], 'unclean_exit')
                self.assertEqual(previous['last_activity_at'], 140)
                self.assertIsNone(previous['ended_at'])
            finally:
                store.close()
