#!/usr/bin/env python3
"""Offline Unity inventory/export. Stdlib controller; optional UnityPy worker."""
import argparse
import collections
import ctypes
import hashlib
import http.client
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
import zipfile

PIN = 'f7873b92063ba0551b9a9513cfa1554ea8b9bf8eb49e218076bc3a6d794864f5'
SETTINGS = {'ShaderExportMode': 'Yaml', 'ScriptExportMode': 'DllExportWithoutRenaming',
            'ScriptContentLevel': 'Level0', 'ExportUnreadableAssets': 'true'}
HEADERS = (b'UnityFS\0', b'UnityRaw\0', b'UnityWeb\0')


class GuardError(ValueError):
    pass


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()) or (
        path.exists() and bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400))


def guard_path(value):
    text = str(value)
    if '\0' in text or text.startswith(('\\\\', '//')):
        raise GuardError('NUL/UNC paths are forbidden')
    # Reject alternate data streams, device names and ambiguous Windows components on all OSes.
    parts = PureWindowsPath(text).parts
    for part in parts[1:] if PureWindowsPath(text).drive else parts:
        if ':' in part or part.endswith((' ', '.')) or re.match(
                r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part, re.I):
            raise GuardError('unsafe path component: ' + part)
    p = Path(value).absolute()
    for node in (p, *p.parents):
        if linked(node):
            raise GuardError('linked path/ancestor: ' + str(node))
    return p.resolve()


def within(path, root):
    return path == root or root in path.parents


def destination(root, relative):
    p = guard_path(root / relative)
    if not within(p, root):
        raise GuardError('write outside output')
    return p


class Budget:
    def __init__(self, args):
        self.end = time.monotonic() + args.timeout
        self.files = 0
        self.bytes = 0
        self.args = args

    def check(self):
        if time.monotonic() >= self.end:
            raise GuardError('wall-time budget exceeded')

    def add(self, size):
        self.check()
        self.files += 1
        self.bytes += size
        if self.files > self.args.max_files or self.bytes > self.args.max_bytes:
            raise GuardError('file/byte budget exceeded')


def digest(path, budget=None):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            if budget:
                budget.check()
            block = stream.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def walk(root):
    if root.is_file():
        yield root
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort(key=str.casefold)
        files.sort(key=str.casefold)
        for name in dirs + files:
            guard_path(Path(directory) / name)
        for name in files:
            yield Path(directory) / name


def write_json(path, obj):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(obj, stream, ensure_ascii=False, indent=2)


def scan_files(root, budget=None, skip=()):
    """Stream directory entries; never materialize a directory before checking."""
    if budget:
        budget.check()
    guard_path(root)
    if not root.exists():
        return
    with os.scandir(root) as entries:
        for entry in entries:
            if budget:
                budget.check()
            if entry.name in skip:
                continue
            path = guard_path(Path(entry.path))
            if entry.is_dir(follow_symlinks=False):
                yield from scan_files(path, budget)
            elif entry.is_file(follow_symlinks=False):
                yield path


class OwnedProcess:
    """Stable Popen handle; Windows kill-on-close Job, POSIX owned process group.

    The trusted Python worker waits for a start byte until assignment succeeds.
    No claim is made that this contains hostile OS escape attempts.
    """
    def __init__(self, argv, cwd, stdout, stderr):
        self.job = None
        self.proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
            stdout=stdout, stderr=stderr, start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            if os.name == 'nt':
                from ctypes import wintypes as w
                class BASIC(ctypes.Structure):
                    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                        ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', w.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', w.DWORD), ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
                class IO(ctypes.Structure):
                    _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
                class EXT(ctypes.Structure):
                    _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
                k = ctypes.WinDLL('kernel32', use_last_error=True)
                k.CreateJobObjectW.restype = w.HANDLE
                k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
                k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
                k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
                k.CloseHandle.argtypes = [w.HANDLE]
                self.kernel = k
                self.job = k.CreateJobObjectW(None, None)
                limits = EXT()
                limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
                if not self.job or not k.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                    raise OSError(ctypes.get_last_error(), 'cannot configure owned Job')
                if not k.AssignProcessToJobObject(self.job, int(self.proc._handle)):
                    raise OSError(ctypes.get_last_error(), 'cannot assign owned worker to Job')
            self.proc.stdin.write(b'1')
            self.proc.stdin.close()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt':
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


def run_worker(request, directory, budget, output_root=None):
    directory.mkdir()
    req = directory / 'request.json'
    result = directory / 'result.json'
    write_json(req, request)
    stdout = directory / 'stdout.log'
    stderr = directory / 'stderr.log'
    owned = None
    def check_outputs():
        budget.check()
        log_bytes = 0
        for path in scan_files(directory, budget):
            if path != req:
                log_bytes += path.stat().st_size
                if log_bytes > budget.args.max_log_bytes:
                    raise GuardError('worker log budget exceeded')
        root = output_root or (Path(request['export']) if request.get('export') else None)
        if root is not None:
            count = total = 0
            for path in scan_files(root, budget, skip=('_work', 'manifest.json')):
                count += 1
                total += path.stat().st_size
                if count > budget.args.max_files or total > budget.args.max_output_bytes:
                    raise GuardError('worker output file/byte budget exceeded')
    try:
        with stdout.open('xb') as out, stderr.open('xb') as err:
            owned = OwnedProcess([sys.executable, '-B', str(Path(__file__).resolve()),
                '--worker', str(req), str(result)], directory, out, err)
            while owned.proc.poll() is None:
                check_outputs()
                time.sleep(0.05)
            check_outputs()
            if owned.proc.returncode:
                raise GuardError('worker exit ' + str(owned.proc.returncode))
        if not result.exists() or result.stat().st_size > budget.args.max_log_bytes:
            raise GuardError('missing/oversized worker result')
        data = json.loads(result.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('type_counts'), dict) or not isinstance(data.get('errors'), list):
            raise GuardError('malformed worker result')
        if any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in data['type_counts'].items()):
            raise GuardError('malformed type counts')
        return data
    finally:
        if owned:
            owned.close()


def api(port, route, form=None, deadline=None):
    base = 'http://127.0.0.1:' + str(port)
    for hop in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('backend deadline exceeded')
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=min(remaining, 30))
        try:
            body = urllib.parse.urlencode(form) if form is not None else None
            conn.request('POST' if form is not None else 'GET', route, body,
                         {'Content-Type': 'application/x-www-form-urlencoded'} if form is not None else {})
            response = conn.getresponse()
            if len(response.read(1024 * 1024 + 1)) > 1024 * 1024:
                raise GuardError('backend response budget exceeded')
            if response.status == 200:
                return
            # Ready helper follows HTTP redirects. Only same owned endpoint is allowed.
            if response.status in (301, 302, 303, 307, 308) and hop < 5:
                location = response.getheader('Location')
                if not location:
                    raise GuardError('backend redirect without Location')
                url = urllib.parse.urlsplit(urllib.parse.urljoin(base + route, location))
                if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.port != port
                        or url.username or url.password):
                    raise GuardError('backend redirect outside owned loopback forbidden')
                route = urllib.parse.urlunsplit(('', '', url.path or '/', url.query, ''))
                if response.status in (301, 302, 303):
                    form = None
                continue
            raise GuardError('backend HTTP ' + str(response.status) + ' at ' + route)
        finally:
            conn.close()
    raise GuardError('backend redirect limit exceeded')


def worker(req, result):
    if sys.stdin.buffer.read(1) != b'1':
        return 2
    request = json.loads(Path(req).read_text(encoding='utf-8'))
    data = {'type_counts': {}, 'errors': [], 'dependencies': [], 'parser': None,
            'exported': False, 'coverage_proven': False, 'shader_source_restored': False}
    source = Path(request['input'])
    try:
        import UnityPy
        env = UnityPy.load(str(source))
        data['parser'] = {'name': 'UnityPy', 'version': UnityPy.__version__}
        data['type_counts'] = dict(collections.Counter(o.type.name for o in env.objects))
        for asset in env.files.values():
            for child in getattr(asset, 'files', {}).values():
                for ext in getattr(child, 'externals', []):
                    data['dependencies'].append(str(getattr(ext, 'path', ext)))
        if not data['type_counts']:
            data['errors'].append('parser returned no objects')
        if request['backend'] == 'unitypy' and not request['inventory_only']:
            target = Path(request['export'])
            target.mkdir()
            data['object_exports'] = []
            for ordinal, obj in enumerate(env.objects):
                if obj.type.name == 'Texture2D':
                    texture = obj.read()
                    name = str(ordinal) + '_' + str(int(obj.path_id)) + '.png'
                    with (target / name).open('xb') as stream:
                        texture.image.save(stream, format='PNG')
                    data['object_exports'].append({'path': name, 'object_ordinal': ordinal,
                        'path_id': int(obj.path_id), 'type': obj.type.name})
            data['exported'] = any(target.iterdir())
            data['coverage_proven'] = bool(data['type_counts'].get('Texture2D')) and set(data['type_counts']) <= {'Texture2D', 'AssetBundle'}
            if not data['coverage_proven']:
                data['errors'].append('UnityPy export supports Texture2D only; other types require AssetRipper')
    except ImportError:
        data['errors'].append('UnityPy unavailable in selected Python')
    except Exception as exc:
        data['errors'].append('UnityPy: ' + type(exc).__name__ + ': ' + str(exc)[:400])
    if request['backend'] == 'assetripper' and not request['inventory_only']:
        exe = Path(request['exe'])
        proc = None
        try:
            if digest(exe) != request['exe_sha256']:
                raise GuardError('backend SHA changed before launch')
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0))
                port = reserve.getsockname()[1]
            env_vars = dict(os.environ, SOURCE_DATE_EPOCH='1700000000')
            proc = subprocess.Popen([str(exe), '--headless', '--port', str(port),
                '--log-path', str(Path(result).parent / 'assetripper.log')], cwd=exe.parent,
                env=env_vars, stdout=sys.stdout, stderr=sys.stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            deadline = time.monotonic() + request['seconds']
            while True:
                if proc.poll() is not None:
                    raise GuardError('AssetRipper exited before ready')
                try:
                    api(port, '/', deadline=deadline)
                    break
                except (OSError, http.client.HTTPException):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('AssetRipper readiness timeout')
                    time.sleep(0.1)
            for key, value in SETTINGS.items():
                api(port, '/Settings/Update', {key: value}, deadline)
            api(port, '/LoadFile', {'path': str(source)}, deadline)
            api(port, '/Export/UnityProject', {'path': request['export']}, deadline)
            target = Path(request['export'])
            data['exported'] = target.exists() and any(p.is_file() for p in target.rglob('*'))
            if not data['exported']:
                data['errors'].append('backend produced zero files')
            # HTTP 200 and file existence do not prove all source objects were exported.
            data['errors'].append('AssetRipper object/dependency coverage not proven')
        except Exception as exc:
            data['errors'].append('AssetRipper: ' + type(exc).__name__ + ': ' + str(exc)[:400])
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
    write_json(Path(result), data)
    return 0


def archive_entries(path, target, budget):
    entries = []
    with zipfile.ZipFile(path) as z:
        seen = set()
        # Validate the entire central directory before the first extraction write.
        for info in z.infolist():
            raw = info.orig_filename
            name = raw.replace('\\', '/')
            parts = name.rstrip('/').split('/')
            if ('\0' in raw or name.startswith('/') or PureWindowsPath(name).drive or
                    any(p in ('', '.', '..') or ':' in p or p.endswith((' ', '.')) or
                        re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I) for p in parts)):
                raise GuardError('unsafe archive entry: ' + repr(raw))
            key = name.rstrip('/').casefold()
            if key in seen or stat.S_ISLNK(info.external_attr >> 16):
                raise GuardError('archive duplicate/case collision/link: ' + name)
            seen.add(key)
            if info.flag_bits & 1:
                raise GuardError('encrypted ZIP unsupported')
            if info.file_size / max(info.compress_size, 1) > budget.args.max_zip_ratio:
                raise GuardError('ZIP compressed ratio exceeded')
            if not info.is_dir():
                budget.add(info.file_size)
                entries.append(info)
        # Detect file/directory aliases (a file named a and another entry a/b).
        file_keys = {i.filename.replace('\\', '/').casefold() for i in entries}
        for key in seen:
            components = key.split('/')
            if any('/'.join(components[:n]) in file_keys for n in range(1, len(components))):
                raise GuardError('archive file/directory collision')
        target.mkdir()
        paths = []
        for info in entries:
            out = destination(target, info.filename.replace('\\', '/'))
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, out.open('xb') as dst:
                count = 0
                while True:
                    budget.check()
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > info.file_size:
                        raise GuardError('ZIP entry size changed')
                    dst.write(block)
                if count != info.file_size:
                    raise GuardError('truncated ZIP entry')
            paths.append(out)
        return paths


def kind(path):
    with path.open('rb') as stream:
        head = stream.read(64)
    if head.startswith(HEADERS):
        return 'bundle'
    if path.suffix.lower() in ('.zip', '.zipmod') or head.startswith(b'PK\x03\x04'):
        return 'zip'
    if path.name.lower().endswith('.bin'):
        return 'binary_catalog' if 'catalog' in path.name.lower() else 'unknown'
    if path.suffix.lower() == '.json':
        return 'json'
    if path.suffix.lower() in ('.assets', '.resS'.lower(), '.resource'):
        return 'serialized_or_companion'
    return 'unknown'


def catalogs(path, record, roots, budget):
    if path.stat().st_size > budget.args.max_log_bytes:
        raise GuardError('catalog JSON size budget exceeded')
    obj = json.loads(path.read_text(encoding='utf-8-sig'))
    ids = obj.get('m_InternalIds') if isinstance(obj, dict) else None
    if not isinstance(ids, list) or not all(isinstance(v, str) for v in ids):
        record['errors'].append('unsupported JSON catalog schema')
        return
    record['kind'] = 'addressables_json'
    record['internal_ids'] = []
    for value in ids:
        budget.check()
        resolved = None
        issue = None
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', value) and not value.startswith('file://'):
            issue = 'remote ID; no HTTP fetch'
        elif '{' in value or '}' in value:
            issue = 'runtime placeholder unsupported'
        else:
            try:
                local = urllib.parse.unquote(urllib.parse.urlparse(value).path) if value.startswith('file://') else value
                if value.startswith('file://') and urllib.parse.urlparse(value).netloc:
                    raise GuardError('file URL host forbidden')
                if os.name == 'nt' and re.match(r'^/[A-Za-z]:/', local):
                    local = local[1:]
                candidate = guard_path(Path(local) if Path(local).is_absolute() else path.parent / local)
                if not any(within(candidate, r) for r in roots):
                    issue = 'dependency outside explicit input roots'
                elif not candidate.is_file():
                    issue = 'missing local dependency'
                else:
                    resolved = str(candidate)
            except (ValueError, OSError) as exc:
                issue = str(exc)
        item = {'id': value, 'resolved': resolved, 'issue': issue}
        record['internal_ids'].append(item)
        # Catalog topology/address table decode is not inferred from path strings.
        if issue:
            record['unresolved_dependencies'].append(item)
    record['errors'].append('catalog addresses/dependency topology not decoded; internal IDs only')


def output_inventory(root, budget, final=False):
    rows = []
    total = 0
    for path in scan_files(root, None if final else budget, skip=('_work', 'manifest.json')):
        if not final:
            budget.check()
        relative = path.relative_to(root).as_posix()
        if relative == 'manifest.json' or relative.startswith('_work/'):
            continue
        size = path.stat().st_size
        total += size
        if len(rows) >= budget.args.max_files or total > budget.args.max_output_bytes:
            raise GuardError('output file/byte budget exceeded')
        native = None
        with path.open('rb') as stream:
            head = stream.read(8192)
        match = re.search(rb'--- !u!(\d+) &', head)
        if match:
            native = int(match.group(1))
        rows.append({'path': relative, 'bytes': size, 'sha256': digest(path, None if final else budget),
                     'native_class_id': native, 'format': path.suffix.lower()})
    return rows


def execute(args):
    budget = Budget(args)
    roots = [guard_path(v) for v in args.input]
    if len(set(str(p).casefold() for p in roots)) != len(roots):
        raise GuardError('duplicate/case input paths')
    if any(not p.exists() for p in roots):
        raise GuardError('input missing')
    out = guard_path(args.output)
    if out.exists():
        raise GuardError('existing output forbidden')
    if any(within(out, p) or within(p, out) for p in roots):
        raise GuardError('input/output overlap')
    exe = None
    backend_sha = None
    if args.backend == 'assetripper' and not args.inventory_only:
        if not args.assetripper:
            raise GuardError('--assetripper required')
        exe = guard_path(args.assetripper)
        backend_sha = digest(exe, budget)
        if backend_sha != args.backend_sha256.lower():
            raise GuardError('backend SHA mismatch; executable not launched')
    # Validate all source path ancestors before any write.
    originals = {}
    seen = set()
    initial_errors = []
    for root in roots:
        try:
            for p in walk(root):
                key = str(p).casefold()
                if key in seen:
                    if p in originals:
                        continue
                    raise GuardError('case-colliding input files')
                seen.add(key)
                budget.add(p.stat().st_size)
                originals[p] = digest(p, budget)
        except GuardError as exc:
            initial_errors.append(str(exc))
    # Expired preflight must not start output or a final unbudgeted source scan.
    budget.check()
    work = destination(out, '_work')
    report = {'schema': 1, 'operation': 'inventory' if args.inventory_only else 'export',
        'status': 'failed', 'inputs': [], 'outputs': [], 'errors': initial_errors,
        'skips': [], 'unresolved_dependencies': [], 'type_counts': {}, 'output_native_class_counts': {},
        'provenance': {'tool': 'unity-asset-extraction', 'python': sys.version.split()[0],
            'backend': args.backend, 'backend_executable': str(exe) if exe else None,
            'backend_sha256': backend_sha,
            'backend_contract_version': 'AssetRipper GUI 2.0.0.0 pinned build' if exe and args.backend_sha256.lower() == PIN else None,
            'settings': SETTINGS if args.backend == 'assetripper' else {},
            'network': 'owned loopback only; remote catalog IDs never fetched',
            'shader_source_restored': False},
        'budgets': {k: getattr(args, k) for k in ('timeout', 'max_files', 'max_bytes', 'max_output_bytes', 'max_zip_ratio', 'max_log_bytes')}}
    successes = 0
    queue = [(p, str(p), sha) for p, sha in originals.items()]
    processed = set()
    output_created = False
    try:
        out.mkdir(parents=True)
        output_created = True
        work.mkdir()
        index = 0
        while index < len(queue):
            p, origin, sha = queue[index]
            index += 1
            record = {'source': origin, 'sha256_before': sha, 'bytes': p.stat().st_size,
                'kind': kind(p), 'errors': [], 'unresolved_dependencies': [], 'type_counts': {}}
            report['inputs'].append(record)
            try:
                budget.check()
                namespace = sha  # Full digest: no truncation/collision assumptions.
                record['namespace'] = namespace
                if record['kind'] == 'zip':
                    stage = work / ('zip-' + sha)
                    if sha not in processed:
                        processed.add(sha)
                        for entry in archive_entries(p, stage, budget):
                            queue.append((entry, origin + '!' + entry.relative_to(stage).as_posix(), digest(entry, budget)))
                    record['archive_validated'] = True
                elif record['kind'] == 'json':
                    allowed = [r if r.is_dir() else r.parent for r in roots]
                    if within(p, work):
                        allowed.append(work / p.relative_to(work).parts[0])
                    catalogs(p, record, allowed, budget)
                    if record['kind'] == 'addressables_json':
                        successes += 1
                elif record['kind'] in ('bundle', 'serialized_or_companion'):
                    if sha in processed:
                        record['deduplicated'] = True
                        continue
                    processed.add(sha)
                    export = destination(out, namespace)
                    job = work / ('job-' + sha)
                    data = run_worker({'input': str(p), 'backend': args.backend,
                        'exe': str(exe) if exe else None, 'exe_sha256': args.backend_sha256.lower(),
                        'inventory_only': args.inventory_only, 'export': str(export),
                        'seconds': max(0.01, budget.end - time.monotonic())}, job, budget, out)
                    record.update(data)
                    for dep in data.get('dependencies', []):
                        record['unresolved_dependencies'].append({'id': dep, 'issue': 'parser external reference; resolution/coverage not proven'})
                    if data['type_counts'] or data.get('exported'):
                        successes += 1
                    if not args.inventory_only and not data.get('exported'):
                        record['errors'].append('zero export output')
                else:
                    record['errors'].append('unsupported ' + record['kind'])
                    report['skips'].append({'source': origin, 'reason': record['errors'][-1]})
            except (Exception, KeyboardInterrupt) as exc:
                record['errors'].append(type(exc).__name__ + ': ' + str(exc)[:500])
                if isinstance(exc, KeyboardInterrupt):
                    report['errors'].append('interrupted')
                    break
        report['outputs'] = output_inventory(out, budget)
    except (Exception, KeyboardInterrupt) as exc:
        report['errors'].append(type(exc).__name__ + ': ' + str(exc)[:500])
    finally:
        if not output_created:
            raise GuardError('output creation failed')
        if not report['outputs']:
            # Recover a bounded artifact inventory after timeout/failure, never promote it
            # to a successful export. This shares the final source-hash safety phase.
            try:
                report['outputs'] = output_inventory(out, budget, final=True)
            except (OSError, ValueError) as exc:
                report['errors'].append('final output inventory: ' + str(exc)[:400])
        # Archive entries remain read-only staging inputs, with their own after-hash.
        for p, origin, before in queue:
            if p not in originals:
                try:
                    after = digest(p)
                except OSError:
                    after = None
                for rec in report['inputs']:
                    if rec['source'] == origin:
                        rec['sha256_after'] = after
                        rec['source_unchanged'] = after == before
                if after != before:
                    report['errors'].append('archive entry changed: ' + origin)
        # Always rehash originals, even after timeout: immutability is a final gate.
        for p, before in originals.items():
            try:
                after = digest(p)
            except OSError:
                after = None
            for rec in report['inputs']:
                if rec['source'] == str(p):
                    rec['sha256_after'] = after
                    rec['source_unchanged'] = before == after
            if after != before:
                report['errors'].append('source changed: ' + str(p))
        # Cleanup only controller-owned staging; revalidate every link before removal.
        try:
            cleanup_end = time.monotonic() + 1
            while True:
                guard_path(work)
                for unused in scan_files(work):
                    pass
                try:
                    if work.exists():
                        shutil.rmtree(work)
                    break
                except PermissionError:
                    # Job termination can briefly leave descendant file handles closing.
                    if time.monotonic() >= cleanup_end:
                        raise
                    time.sleep(0.05)
        except (OSError, ValueError) as exc:
            report['errors'].append('staging cleanup refused: ' + str(exc)[:400])
    counts = collections.Counter()
    for rec in report['inputs']:
        counts.update(rec['type_counts'])
        report['unresolved_dependencies'].extend(rec['unresolved_dependencies'])
    report['type_counts'] = dict(counts)
    report['output_native_class_counts'] = dict(collections.Counter(str(r['native_class_id']) for r in report['outputs'] if r['native_class_id'] is not None))
    issues = report['errors'] or report['skips'] or report['unresolved_dependencies'] or any(r['errors'] for r in report['inputs'])
    if not args.inventory_only and not report['outputs']:
        report['errors'].append('zero output; export cannot be complete')
        issues = True
    report['status'] = ('partial' if issues else 'complete') if successes else 'failed'
    write_json(destination(out, 'manifest.json'), report)
    print(json.dumps({'status': report['status'], 'manifest': str(out / 'manifest.json'),
                      'inputs': len(report['inputs']), 'outputs': len(report['outputs'])}))
    return {'complete': 0, 'partial': 2, 'failed': 1}[report['status']]


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', action='append', required=True, help='repeatable file/directory/ZIP/catalog input')
    p.add_argument('--output', required=True, help='new local directory; existing paths never overwritten')
    p.add_argument('--inventory-only', action='store_true')
    p.add_argument('--backend', choices=('unitypy', 'assetripper'), default='unitypy')
    p.add_argument('--assetripper', help='explicit trusted AssetRipper.GUI.Free.exe')
    p.add_argument('--backend-sha256', default=PIN, help='approved executable SHA256; default pinned GUI v2')
    p.add_argument('--timeout', type=float, default=120)
    p.add_argument('--max-files', type=int, default=10000)
    p.add_argument('--max-bytes', type=int, default=2 * 1024**3)
    p.add_argument('--max-output-bytes', type=int, default=4 * 1024**3)
    p.add_argument('--max-zip-ratio', type=float, default=200)
    p.add_argument('--max-log-bytes', type=int, default=8 * 1024**2)
    return p


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        return worker(sys.argv[2], sys.argv[3])
    args = parser().parse_args()
    if (any(not math.isfinite(getattr(args, n)) for n in ('timeout', 'max_zip_ratio')) or
            any(getattr(args, n) <= 0 for n in ('timeout', 'max_files', 'max_bytes', 'max_output_bytes', 'max_zip_ratio', 'max_log_bytes'))):
        parser().error('all budgets must be finite and positive')
    try:
        return execute(args)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)[:500]}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
