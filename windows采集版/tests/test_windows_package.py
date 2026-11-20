import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
from xhs_research import runtime

from xhs_research.runtime import process_lock
from xhs_research.windows_env import chrome_path


class WindowsPackageTests(unittest.TestCase):
    def test_lock_blocks_other_process_and_releases_after_exception(self):
        with tempfile.TemporaryDirectory(prefix='采集 space ') as folder:
            directory = Path(folder)
            script = ('from pathlib import Path; import sys; '
                      'from xhs_research.runtime import process_lock; '
                      'lock=process_lock(Path(sys.argv[1])); lock.__enter__(); lock.__exit__(None,None,None)')
            def probe():
                return subprocess.run([sys.executable, '-c', script, folder], capture_output=True, text=True)
            with self.assertRaisesRegex(RuntimeError, 'exit'):
                with process_lock(directory):
                    self.assertNotEqual(probe().returncode, 0)
                    raise RuntimeError('exit')
            self.assertEqual(probe().returncode, 0)

    def test_chrome_detection_accepts_unicode_space_path(self):
        with tempfile.TemporaryDirectory(prefix='中文 space ') as folder:
            path = Path(folder) / 'Google/Chrome/Application/chrome.exe'
            path.parent.mkdir(parents=True)
            path.touch()
            with patch.dict(os.environ, {'LOCALAPPDATA': folder}, clear=True):
                self.assertEqual(chrome_path(), path)

    def test_command_scripts_use_local_environment_and_missing_setup_hint(self):
        root = Path(__file__).resolve().parents[1]
        for path in sorted(root.glob('0[2-8]_*.cmd')):
            script = path.read_text(encoding='utf-8')
            self.assertIn('cd /d "%~dp0"', script)
            self.assertIn('if not exist ".venv\\Scripts\\python.exe"', script)
            self.assertIn('01_install.cmd', script)
            self.assertNotIn('/Users/', script)

    def test_windows_lock_unlocks_same_byte_and_rejects_contention(self):
        import errno
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fake = Mock(LK_NBLCK=1, LK_UNLCK=2)
            proxy = SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if name != 'name'}, name='nt')
            with patch.object(runtime, 'os', proxy), patch.object(runtime, 'msvcrt', fake, create=True):
                with process_lock(directory):
                    self.assertEqual(fake.locking.call_args.args[1:], (1, 1))
                self.assertEqual(fake.locking.call_args.args[1:], (2, 1))
                self.assertEqual((directory / 'worker.lock').stat().st_size, 1)
                fake.locking.reset_mock()
                fake.locking.side_effect = OSError(errno.EACCES, 'busy')
                from xhs_research.errors import StopRun
                with self.assertRaises(StopRun):
                    with process_lock(directory):
                        self.fail('must not acquire busy lock')
                self.assertEqual(fake.locking.call_count, 1)
