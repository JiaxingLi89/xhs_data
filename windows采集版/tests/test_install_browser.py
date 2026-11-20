import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import setup_live as setup
from xhs_research.browser_selection import browser_menu, switch_browser
from xhs_research.config import Config
from xhs_research.errors import SetupError, StopRun
from xhs_research.runtime import process_lock
from xhs_research.windows_env import browser_path


class InstallerTests(unittest.TestCase):
    def test_sources_and_offline_are_exclusive(self):
        root = Path('project space')
        for source in ('tuna', 'pypi'):
            cmd = setup.pip_command('python.exe', root, setup.parse_args(['--source', source]))
            self.assertEqual(cmd[-2:], ['--index-url', setup.SOURCES[source]])
            self.assertNotIn('--extra-index-url', cmd)
        self.assertEqual(setup.pip_command('py', root, setup.parse_args([]))[-1], setup.SOURCES['tuna'])
        cmd = setup.pip_command('py', root, setup.parse_args(['--wheels', 'wheel space']))
        self.assertIn('--no-index', cmd)
        self.assertNotIn('--index-url', cmd)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            setup.parse_args(['--wheels', '.', '--source', 'tuna'])

    def test_menu_default_official_and_cancel(self):
        for answer, expected in [('', 'tuna'), ('2', 'pypi'), ('0', None)]:
            with patch('builtins.input', return_value=answer), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.choose_source(), expected)

    def test_python_relaunch_preserves_source(self):
        from types import SimpleNamespace
        with patch.object(setup, 'os', SimpleNamespace(name='nt')), patch.object(setup.sys, 'getwindowsversion', return_value=SimpleNamespace(build=22631), create=True), patch.object(setup, 'find_python', return_value=['py', '-3.12']), patch.object(setup.subprocess, 'call', return_value=0) as call:
            self.assertEqual(setup.main(['--source', 'pypi']), 0)
            self.assertEqual(call.call_args.args[0][-3:], ['--selected', '--source', 'pypi'])

    def test_install_failure_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(setup.venv, 'EnvBuilder'), patch.object(setup.subprocess, 'call', return_value=1) as call, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup.install(Path(temp), setup.parse_args(['--source', 'pypi'])), 2)
            self.assertEqual(call.call_count, 1)
            self.assertIn('依赖安装失败', output.getvalue())

    def test_installed_but_doctor_failed_retains_resolved_versions(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(setup.venv, 'EnvBuilder'), patch.object(setup.subprocess, 'call', side_effect=[0, 2]), patch.object(setup.subprocess, 'check_output', return_value='example==1\n'), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(setup.install(Path(temp), setup.parse_args([])), 2)
            self.assertEqual((Path(temp) / 'requirements-resolved.txt').read_text(), 'example==1\n')
            self.assertIn('08_browser.cmd', output.getvalue())

    def test_offline_skips_menu(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(setup.venv, 'EnvBuilder'), patch.object(setup, 'choose_source', side_effect=AssertionError('must not prompt')), patch.object(setup.subprocess, 'call', return_value=1), contextlib.redirect_stdout(io.StringIO()):
            setup.install(Path(temp), setup.parse_args(['--wheels', temp, '--menu']))


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='浏览器 space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.json'
        self.data = {'keywords': ['保留关键词'], 'browser_channel': 'chrome', 'stage': 'pilot', 'daily_requests_pilot': 27, 'data_dir': 'data/custom', 'export_dir': 'exports/custom'}
        self.path.write_text(json.dumps(self.data), encoding='utf-8')

    def test_switch_preserves_fields_and_database_and_separates_profiles(self):
        config = Config.load(self.path)
        config.state_dir.mkdir(parents=True)
        db = config.state_dir / 'state.sqlite'
        db.write_bytes(b'unchanged database sentinel')
        config.browser_profile.mkdir()
        session = config.browser_profile / 'session-marker'
        session.write_text('existing Chrome session')
        with patch('xhs_research.browser_selection.browser_path', return_value=Path('edge.exe')), contextlib.redirect_stdout(io.StringIO()):
            switch_browser(self.path, 'msedge')
            self.assertEqual(json.loads(self.path.read_text()), dict(self.data, browser_channel='msedge'))
            edge_profile = Config.load(self.path).browser_profile
            self.assertEqual(edge_profile.name, 'browser-profile-msedge')
            self.assertFalse(edge_profile.exists())
            switch_browser(self.path, 'chrome')
        self.assertEqual(Config.load(self.path).browser_profile, config.browser_profile)
        self.assertEqual(session.read_text(), 'existing Chrome session')
        self.assertEqual(db.read_bytes(), b'unchanged database sentinel')
        self.assertEqual(list(self.root.glob('.browser-*.tmp')), [])

    def test_busy_missing_and_cancel_leave_config_unchanged(self):
        before = self.path.read_bytes()
        with process_lock(Config.load(self.path).state_dir), self.assertRaises(StopRun):
            switch_browser(self.path, 'msedge')
        with patch('xhs_research.browser_selection.browser_path', return_value=None), self.assertRaises(SetupError):
            switch_browser(self.path, 'msedge')
        with patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(browser_menu(self.path), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_atomic_replace_keeps_original(self):
        before = self.path.read_bytes()
        with patch('xhs_research.browser_selection.browser_path', return_value=Path('edge.exe')), patch.object(Path, 'replace', side_effect=PermissionError('locked')), self.assertRaises(PermissionError):
            switch_browser(self.path, 'msedge')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('.browser-*.tmp')), [])

    def test_detect_edge_in_standard_location(self):
        executable = self.root / 'Microsoft/Edge/Application/msedge.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch.dict('os.environ', {'PROGRAMFILES(X86)': str(self.root)}, clear=True):
            self.assertEqual(browser_path('msedge'), executable)
            self.assertIsNone(browser_path('chrome'))
