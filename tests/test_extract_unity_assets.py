"""Observable CLI, path, ZIP, worker fault and lifecycle tests; no Unity Editor."""
import ctypes
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'unity-asset-extraction/scripts/extract_unity_assets.py'
spec = importlib.util.spec_from_file_location('extract', SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
BASE = Path('D:/Temp/asset-extraction-tests') if os.name == 'nt' else Path(tempfile.gettempdir()) / 'asset-extraction-tests'


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        tool.guard_path(BASE)
        BASE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='owned-', dir=BASE)).resolve()
        self.out = self.root / 'out'
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')

    def tearDown(self):
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, BASE.resolve())
        self.assertTrue(resolved.name.startswith('owned-'))
        # Junction negatives explicitly remove the junction as a link in their finally.
        tool.guard_path(resolved)
        list(tool.walk(resolved))
        shutil.rmtree(resolved)

    def file(self, name, data=b'unknown'):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def fake_parser(self, mode='good'):
        directory = self.root / 'parser'
        directory.mkdir()
        source = '''
from pathlib import Path
import os, subprocess, sys, time
__version__ = 'fixture-only'
export_mode = EXPORT_MODE
source_path = None
class Type:
    name = 'Texture2D'
class Image:
    def __init__(self, content=b'fixture-png-not-production'):
        self.content = content
    def save(self, stream, format):
        assert format == 'PNG'
        stream.write(self.content)
        stream.flush()
        if export_mode == 'slow' and not Path(source_path).name.startswith('first'):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            Path(source_path + '.pid').write_text(str(child.pid))
            Path(source_path + '.workerpid').write_text(str(os.getpid()))
            for n in range(12):
                (Path(stream.name).parent / ('extra-' + str(n))).write_bytes(b'x' * 16)
                time.sleep(0.1)
            time.sleep(3)
            Path(source_path + '.sentinel').write_text('worker reached final sentinel')
class Object:
    type = Type()
    path_id = 42
    def __init__(self, asset='one.assets', content=b'fixture-png-not-production'):
        self.assets_file = type('Asset', (), {'name': asset})()
        self.content = content
    def read(self):
        return type('Texture', (), {'image': Image(self.content)})()
class Env:
    files = {}
    objects = [Object()]
def load(path):
    global source_path
    source_path = path
    MODE
    return Env()
'''
        behavior = {
            'good': 'pass',
            'slow': 'pass',
            'collision': "env = Env(); env.objects = [Object('one.assets', b'first-fixture'), Object('two.assets', b'second-fixture')]; return env",
            'error': "raise ValueError('controlled parse failure')",
            'timeout': "time.sleep(30)",
            'tree': "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); Path(path + '.pid').write_text(str(child.pid)); time.sleep(30)",
            'mixed': "\n    if Path(path).name.startswith('bad'): raise ValueError('controlled mixed failure')",
        }[mode]
        (directory / 'UnityPy.py').write_text(source.replace('EXPORT_MODE', repr(mode)).replace('MODE', behavior), encoding='utf-8')
        self.env['PYTHONPATH'] = str(directory)

    def cli(self, inputs, *extra, output=None):
        args = [sys.executable, '-B', str(SCRIPT), '--output', str(output or self.out)]
        for p in inputs:
            args += ['--input', str(p)]
        result = subprocess.run(args + list(extra), env=self.env, capture_output=True, text=True, timeout=20)
        manifest = (output or self.out) / 'manifest.json'
        return result, json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else None

    def test_header_extensionless_export_hashes_immutable_and_nooverwrite(self):
        self.fake_parser()
        p = self.file('cache/noextension', b'UnityFS\0fixture')
        before = hashlib.sha256(p.read_bytes()).hexdigest()
        result, report = self.cli([p])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['type_counts'], {'Texture2D': 1})
        self.assertEqual(report['inputs'][0]['kind'], 'bundle')
        self.assertTrue(report['inputs'][0]['source_unchanged'])
        self.assertEqual(before, hashlib.sha256(p.read_bytes()).hexdigest())
        row = report['outputs'][0]
        self.assertEqual(row['path'], before + '/0_42.png')
        self.assertEqual((self.out / row['path']).read_bytes(), b'fixture-png-not-production')
        self.assertEqual(row['sha256'], hashlib.sha256((self.out / row['path']).read_bytes()).hexdigest())
        snapshot = (self.out / 'manifest.json').read_bytes()
        retry, unused = self.cli([p])
        self.assertEqual(retry.returncode, 1)
        self.assertEqual(snapshot, (self.out / 'manifest.json').read_bytes())
        self.assertFalse((self.out / '_work').exists())

    def test_same_path_id_in_two_serialized_files_cli_preserves_both(self):
        self.fake_parser('collision')
        p = self.file('collision.bundle', b'UnityFS\0two serialized files')
        before = hashlib.sha256(p.read_bytes()).hexdigest()
        result, report = self.cli([p])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['type_counts'], {'Texture2D': 2})
        self.assertEqual(len(report['outputs']), 2)
        contents = {row['path']: (self.out / row['path']).read_bytes() for row in report['outputs']}
        self.assertEqual(contents, {before + '/0_42.png': b'first-fixture',
                                    before + '/1_42.png': b'second-fixture'})
        mapping = report['inputs'][0]['object_exports']
        self.assertEqual([(r['object_ordinal'], r['path_id'], r['path']) for r in mapping],
                         [(0, 42, '0_42.png'), (1, 42, '1_42.png')])
        for row in report['outputs']:
            self.assertEqual(row['sha256'], hashlib.sha256(contents[row['path']]).hexdigest())
        self.assertEqual(report['inputs'][0]['sha256_before'], before)
        self.assertEqual(report['inputs'][0]['sha256_after'], before)
        self.assertTrue(report['inputs'][0]['source_unchanged'])

    def assert_process_stopped(self, pid):
        if os.name == 'nt':
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            k.OpenProcess.restype = ctypes.c_void_p
            k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = k.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    self.assertEqual(k.WaitForSingleObject(handle, 3000), 0, 'owned process survived')
                finally:
                    k.CloseHandle(handle)
        else:
            proc = Path('/proc') / str(pid) / 'stat'
            if proc.exists():
                self.assertEqual(proc.read_text().split()[2], 'Z')

    def test_live_export_caps_cancel_before_sentinel_including_prior_outputs(self):
        self.fake_parser('slow')
        first = self.file('first.bundle', b'UnityFS\0first')
        slow = self.file('slow.bundle', b'UnityFS\0slow')
        for n, (inputs, cap) in enumerate([
                ([slow], ('--max-files', '4')),
                ([slow], ('--max-output-bytes', '70')),
                ([first, slow], ('--max-files', '4')),
                ([first, slow], ('--max-output-bytes', '70'))]):
            with self.subTest(inputs=len(inputs), cap=cap):
                output = self.root / ('live-' + str(n))
                for suffix in ('.pid', '.workerpid', '.sentinel'):
                    Path(str(slow) + suffix).unlink(missing_ok=True)
                start = time.monotonic()
                result, report = self.cli(inputs, *cap, output=output)
                self.assertLess(time.monotonic() - start, 3)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(report['status'], 'complete')
                self.assertTrue(any('worker output file/byte budget' in e
                                    for r in report['inputs'] for e in r['errors']))
                self.assertFalse(Path(str(slow) + '.sentinel').exists())
                self.assertFalse((output / '_work').exists())
                for suffix in ('.pid', '.workerpid'):
                    marker = Path(str(slow) + suffix)
                    self.assertTrue(marker.exists())
                    self.assert_process_stopped(int(marker.read_text()))
                emitted = list((output / hashlib.sha256(slow.read_bytes()).hexdigest()).iterdir())
                if len(inputs) == 2:
                    # Prior namespace consumes capacity even while active output is below cap.
                    self.assertLessEqual(len(emitted), 4)
                    if cap[0] == '--max-output-bytes':
                        self.assertLessEqual(sum(p.stat().st_size for p in emitted), 70)

    def test_output_enumeration_stops_at_cap_without_materializing_directory(self):
        self.out.mkdir()
        for n in range(20):
            (self.out / str(n)).write_bytes(b'x')
        args = tool.parser().parse_args(['--input', str(self.out), '--output', 'unused', '--max-files', '1'])
        actual = os.scandir
        visited = []
        class StreamingDirectory:
            def __enter__(inner):
                inner.entries = actual(self.out)
                return inner
            def __exit__(inner, *unused):
                inner.entries.close()
            def __iter__(inner):
                for entry in inner.entries:
                    visited.append(entry.name)
                    if len(visited) > 2:
                        raise AssertionError('directory enumerated past first cap violation')
                    yield entry
        with patch.object(tool.os, 'scandir', return_value=StreamingDirectory()):
            with self.assertRaisesRegex(tool.GuardError, 'output file/byte budget'):
                tool.output_inventory(self.out, tool.Budget(args))
        self.assertEqual(len(visited), 2)

    def test_nonfinite_python_cli_budgets_reject_before_output(self):
        p = self.file('finite.bundle', b'UnityFS\0fixture')
        for flag in ('--timeout', '--max-zip-ratio'):
            for value in ('nan', 'inf', '-inf', '0', '-1'):
                with self.subTest(flag=flag, value=value):
                    output = self.root / (flag[2:] + value)
                    result, report = self.cli([p], flag + '=' + value, output=output)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('finite and positive', result.stderr)
                    self.assertIsNone(report)
                    self.assertFalse(output.exists())

    def test_backend_provenance_reuses_preflight_hash_and_expiry_is_prewrite(self):
        exe = self.file('trusted.exe', b'fixture exe never launched')
        original = self.file('large.original', b'x' * (2 * 1024 * 1024))
        args = tool.parser().parse_args(['--input', str(original), '--output', str(self.out),
            '--backend', 'assetripper', '--assetripper', str(exe),
            '--backend-sha256', hashlib.sha256(exe.read_bytes()).hexdigest()])
        actual = tool.digest
        calls = []
        def expire_after_scan(path, budget=None):
            calls.append(path)
            value = actual(path, budget)
            if path == original and budget:
                budget.end = 0
            return value
        with patch.object(tool, 'digest', expire_after_scan):
            with self.assertRaisesRegex(tool.GuardError, 'wall-time budget'):
                tool.execute(args)
        self.assertEqual(calls, [exe, original])
        self.assertFalse(self.out.exists())
        calls.clear()
        def counted(path, budget=None):
            calls.append(path)
            return actual(path, budget)
        with patch.object(tool, 'digest', counted):
            self.assertEqual(tool.execute(args), 1)
        report = json.loads((self.out / 'manifest.json').read_text())
        self.assertEqual(calls.count(exe), 1)
        self.assertEqual(report['provenance']['backend_sha256'], args.backend_sha256)
        self.assertEqual(report['status'], 'failed')
        self.assertFalse((self.out / '_work').exists())

    def test_output_created_setup_failure_and_deadline_have_manifest_cleanup(self):
        p = self.file('unknown.original')
        actual = Path.mkdir
        for n, fail in enumerate((True, False)):
            output = self.root / ('setup-' + str(n))
            args = tool.parser().parse_args(['--input', str(p), '--output', str(output)])
            budget = tool.Budget(args)
            def controlled_mkdir(path, *a, **kw):
                if path == output / '_work':
                    if fail:
                        raise OSError('controlled staging setup failure')
                    value = actual(path, *a, **kw)
                    budget.end = 0
                    return value
                return actual(path, *a, **kw)
            with patch.object(tool, 'Budget', return_value=budget), patch.object(Path, 'mkdir', controlled_mkdir):
                self.assertEqual(tool.execute(args), 1)
            report = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(report['status'], 'failed')
            self.assertFalse((output / '_work').exists())
            self.assertTrue(report['errors'] or any(r['errors'] for r in report['inputs']))

    def test_staging_cleanup_retries_transient_permission_error(self):
        p = self.file('unknown.original')
        args = tool.parser().parse_args(['--input', str(p), '--output', str(self.out)])
        actual = shutil.rmtree
        attempts = []
        def delayed_release(path):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError('controlled descendant handle still closing')
            return actual(path)
        with patch.object(tool.shutil, 'rmtree', delayed_release):
            self.assertEqual(tool.execute(args), 1)
        report = json.loads((self.out / 'manifest.json').read_text())
        self.assertEqual(attempts, [self.out / '_work'] * 2)
        self.assertFalse((self.out / '_work').exists())
        self.assertFalse(any('staging cleanup' in e for e in report['errors']))

    def test_multiple_inputs_same_basename_and_content_dedup(self):
        self.fake_parser()
        a = self.file('a/cache', b'UnityFS\0one')
        b = self.file('b/cache', b'UnityFS\0two')
        c = self.file('c/cache', a.read_bytes())
        result, report = self.cli([a, b, c])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(report['outputs']), 2)
        self.assertEqual(len({r['path'] for r in report['outputs']}), 2)
        self.assertTrue(report['inputs'][2]['deduplicated'])
        self.assertEqual(report['type_counts'], {'Texture2D': 2})

    def test_input_output_overlap_and_case_duplicates(self):
        p = self.file('data/a.bin')
        for inputs, output in [([p], p), ([p], p.parent), ([p.parent], p.parent / 'child'), ([p, p], self.out)]:
            result, report = self.cli(inputs, output=output)
            self.assertEqual(result.returncode, 1)
            self.assertIsNone(report)
        self.assertEqual(p.read_bytes(), b'unknown')

    def test_zip_entries_and_archive_hashes(self):
        self.fake_parser()
        p = self.root / 'mod.zipmod'
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('abdata/cache', b'UnityFS\0zip')
        result, report = self.cli([p])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['inputs'][1]['source'], str(p) + '!abdata/cache')
        self.assertEqual(report['inputs'][1]['sha256_before'], hashlib.sha256(b'UnityFS\0zip').hexdigest())
        self.assertTrue(report['inputs'][0]['source_unchanged'])
        self.assertTrue(report['inputs'][1]['source_unchanged'])
        self.assertFalse((self.out / '_work').exists())

    def test_zip_slip_drive_unc_duplicate_case_and_file_directory(self):
        entries = [('../escape',), ('C:/escape',), ('//host/share',), ('A', 'a'), ('a', 'a/b'), ('NUL.txt',)]
        for n, names in enumerate(entries):
            p = self.root / ('unsafe' + str(n) + '.zip')
            with zipfile.ZipFile(p, 'w') as z:
                for name in names:
                    z.writestr(name, b'data')
            output = self.root / ('out' + str(n))
            result, report = self.cli([p], output=output)
            self.assertEqual(result.returncode, 1, names)
            self.assertEqual(report['status'], 'failed')
            self.assertFalse((output / '_work').exists())
            self.assertEqual(report['outputs'], [])
        self.assertFalse((self.root / 'escape').exists())

    def test_zip_symlink_and_ratio_limits(self):
        for label, link in [('link', True), ('ratio', False)]:
            p = self.root / (label + '.zip')
            with zipfile.ZipFile(p, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                info = zipfile.ZipInfo('link')
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
                if link:
                    z.writestr(info, '../target')
                else:
                    z.writestr('huge', b'0' * 10000)
            result, report = self.cli([p], '--max-zip-ratio', '2', output=self.root / label)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(report['inputs'][0]['errors'])

    def test_file_byte_output_and_time_limits(self):
        self.fake_parser()
        a = self.file('data/a', b'UnityFS\0data')
        b = self.file('data/b', b'UnityFS\0data2')
        for n, args in enumerate([('--max-files', '1'), ('--max-bytes', '2'), ('--max-output-bytes', '8'), ('--timeout', '0.01')]):
            result, report = self.cli([a.parent], *args, output=self.root / ('budget' + str(n)))
            self.assertNotEqual(result.returncode, 0, args)
            if report:
                self.assertNotEqual(report['status'], 'complete')
                self.assertFalse((self.root / ('budget' + str(n)) / '_work').exists(), (args, report['errors']))

    def test_remote_missing_local_and_unsupported_catalog(self):
        self.fake_parser()
        p = self.file('data/cache', b'UnityFS\0fixture')
        catalog = self.file('data/catalog.json', json.dumps({'m_InternalIds': ['cache', 'missing', 'https://invalid.example/no-fetch', '{RuntimePath}/x']}).encode())
        binary = self.file('data/catalog.bin', b'unknown-binary-catalog')
        result, report = self.cli([p.parent], '--inventory-only')
        self.assertEqual(result.returncode, 2, result.stderr)
        ids = next(r for r in report['inputs'] if r['kind'] == 'addressables_json')['internal_ids']
        self.assertEqual(ids[0]['resolved'], str(p))
        self.assertEqual(len(report['unresolved_dependencies']), 3)
        self.assertTrue(any(r['kind'] == 'binary_catalog' for r in report['inputs']))
        self.assertTrue(any('remote' in r['issue'] for r in report['unresolved_dependencies']))
        self.assertEqual(binary.read_bytes(), b'unknown-binary-catalog')

    def test_empty_unknown_and_malformed_json_are_failed(self):
        empty_zip = self.root / 'empty.zip'
        with zipfile.ZipFile(empty_zip, 'w'):
            pass
        for n, paths in enumerate([[], [self.file('unknown')], [self.file('bad.json', b'{broken')], [empty_zip]]):
            if not paths:
                empty = self.root / 'empty'
                empty.mkdir()
                paths = [empty]
            result, report = self.cli(paths, '--inventory-only', output=self.root / ('empty' + str(n)))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['outputs'], [])

    def test_mixed_failure_partial(self):
        self.fake_parser('mixed')
        good = self.file('good', b'UnityFS\0one')
        bad = self.file('bad', b'UnityFS\0two')
        result, report = self.cli([good, bad])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(len(report['outputs']), 1)
        self.assertTrue(report['inputs'][1]['errors'])

    def test_backend_timeout_cleanup_and_owned_child(self):
        self.fake_parser('tree')
        p = self.file('slow', b'UnityFS\0timeout')
        start = time.monotonic()
        result, report = self.cli([p], '--timeout', '1.5')
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any('budget' in e for r in report['inputs'] for e in r['errors']))
        self.assertFalse((self.out / '_work').exists())
        pid_file = Path(str(p) + '.pid')
        self.assertTrue(pid_file.exists(), result.stderr)
        pid = int(pid_file.read_text())
        if os.name == 'nt':
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            k.OpenProcess.restype = ctypes.c_void_p
            k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = k.OpenProcess(0x100000, False, pid)
            if handle:
                self.assertEqual(k.WaitForSingleObject(handle, 3000), 0, 'owned child survived Job cleanup')
                k.CloseHandle(handle)
        else:
            proc = Path('/proc') / str(pid) / 'stat'
            if proc.exists():
                self.assertEqual(proc.read_text().split()[2], 'Z')

    def test_malformed_worker_report_rejected(self):
        fake = self.file('bad_worker.py', b"import sys, pathlib\nsys.stdin.buffer.read(1)\npathlib.Path(sys.argv[-1]).write_text('{\"type_counts\": [], \"errors\": []}')\n")
        args = tool.parser().parse_args(['--input', str(fake), '--output', str(self.out)])
        budget = tool.Budget(args)
        actual = tool.OwnedProcess
        def replaced(argv, cwd, stdout, stderr):
            return actual([sys.executable, '-B', str(fake), argv[-1]], cwd, stdout, stderr)
        with patch.object(tool, 'OwnedProcess', replaced):
            with self.assertRaisesRegex(tool.GuardError, 'malformed worker result'):
                tool.run_worker({}, self.root / 'job', budget)

    def test_api_redirect_owned_only(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/remote':
                    self.send_response(302)
                    self.send_header('Location', 'http://example.invalid/forbidden')
                else:
                    self.send_response(200)
                self.end_headers()
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(302)
                self.send_header('Location', '/')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.server_address[1]
            tool.api(port, '/settings', {'value': 'Yaml'}, time.monotonic() + 3)
            with self.assertRaisesRegex(tool.GuardError, 'outside owned loopback'):
                tool.api(port, '/remote', deadline=time.monotonic() + 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_symlink_or_junction_writable_ancestor_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        alias = self.root / 'alias'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)], capture_output=True)
            if result.returncode:
                self.skipTest('junction creation unavailable')
        else:
            alias.symlink_to(target, target_is_directory=True)
        try:
            p = self.file('source')
            result, report = self.cli([p], output=alias / 'out')
            self.assertEqual(result.returncode, 1)
            self.assertIsNone(report)
            self.assertEqual(list(target.iterdir()), [])
        finally:
            if os.name == 'nt':
                os.rmdir(alias)  # Junction itself only.
            else:
                alias.unlink()

    def test_untrusted_backend_not_launched(self):
        p = self.file('bundle', b'UnityFS\0a')
        exe = self.file('unknown.exe', b'not executable')
        result, report = self.cli([p], '--backend', 'assetripper', '--assetripper', str(exe))
        self.assertEqual(result.returncode, 1)
        self.assertIn('SHA mismatch', result.stderr)
        self.assertFalse(self.out.exists())

    def test_powershell_wrapper_real_invocation(self):
        if os.name != 'nt':
            self.skipTest('Windows wrapper')
        p = self.file('has spaces/catalog.bin', b'unsupported')
        wrapper = SCRIPT.with_name('ExportUnityAssets.ps1')
        result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(wrapper),
            '-InputPath', str(p), '-OutputPath', str(self.out), '-PythonPath', sys.executable, '-InventoryOnly'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads((self.out / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(report['inputs'][0]['kind'], 'binary_catalog')


if __name__ == '__main__':
    unittest.main()
