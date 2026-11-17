"""Save pack discovery, placement and reversible file operations (no Qt)."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import zipfile


class SaveError(Exception):
    pass


@dataclass(frozen=True)
class SaveEnv:
    home: str
    appdata: str
    localappdata: str
    documents: str

    @classmethod
    def current(cls):
        home = os.environ.get('USERPROFILE', os.path.expanduser('~')) if os.name == 'nt' else os.path.expanduser('~')
        documents = os.path.join(home, 'Documents')
        if os.name == 'nt':
            # CSIDL_PERSONAL honours redirected Documents folders, including OneDrive.
            try:
                import ctypes
                buf = ctypes.create_unicode_buffer(32768)
                if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                    documents = buf.value
            except (AttributeError, OSError):
                pass
        return cls(home, os.environ.get('APPDATA', os.path.join(home, 'AppData', 'Roaming')),
                   os.environ.get('LOCALAPPDATA', os.path.join(home, 'AppData', 'Local')), documents)


def _encoding(data):
    if data.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return 'utf-16'
    try:
        data.decode('utf-8')
        return 'utf-8'
    except UnicodeError:
        pass
    decoded = {}
    for enc in ('gbk', 'cp932'):
        try:
            decoded[enc] = data.decode(enc)
        except UnicodeError:
            pass
    if len(decoded) == 1:
        return next(iter(decoded))
    if 'cp932' in decoded and sum('\u3040' <= c <= '\u30ff' for c in decoded['cp932']) >= 3:
        return 'cp932'
    return 'gbk'


def decode_text(data: bytes) -> str:
    return data.decode(_encoding(data), errors='replace')


@dataclass(frozen=True)
class Location:
    path: str
    filename: str | None
    line: str
    generic: bool


def _key(path):
    return os.path.normcase(os.path.normpath(path))


def _native(path):
    return os.path.normpath(path.replace('\\', os.sep).replace('/', os.sep))


def _ci_path(path):
    """Resolve existing components case insensitively, also on test hosts."""
    p = Path(path)
    if p.exists():
        return str(p)
    if p.parent == p:
        return str(p)
    parent = Path(_ci_path(str(p.parent)))
    if parent.is_dir():
        try:
            found = next((x for x in parent.iterdir() if x.name.casefold() == p.name.casefold()), None)
        except OSError:
            found = None
        if found is not None:
            return str(found)
    return str(parent / p.name)


def parse_locations(text: str, env: SaveEnv, game_dir: str | None) -> list[Location]:
    roots = [env.home, env.appdata, env.localappdata, env.documents, os.path.join(env.home, 'AppData', 'LocalLow')]
    result, seen = [], set()
    start_re = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]|%(?:appdata|localappdata|userprofile)%', re.I)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('//'):
            continue
        matches = list(start_re.finditer(line))
        # Relative paths can contain spaces; strip the explanatory prefix only.
        starts = [m.start() for m in matches]
        if not starts:
            posix = re.search(r'(?:^|[：:\s\"\'])/(?!/)[^/]', line)
            if posix:
                starts = [line.index('/', posix.start())]
        if not starts and re.search(r'https?://', line, re.I):
            continue
        if not starts and game_dir:
            starts = [max(line.rfind('：'), line.rfind(':')) + 1]
        for i, start in enumerate(starts):
            part = line[start: starts[i + 1] if i + 1 < len(starts) else None].strip()
            # Quotes and prose separated by whitespace terminate a path. Spaces in
            # directory names remain intact; known suffix instructions do not.
            part = re.split(r'["“”\n]|\s+(?:に|へ|を|覆盖|复制|上書き|即可|放入|然后|（|\((?!x86\)(?:[\\/]|$)))', part, maxsplit=1, flags=re.I)[0]
            part = part.strip().rstrip("'，,；;。 ")
            pieces = [x for x in re.split(r'[\\/]+', part) if x]
            pieces = [re.sub(r'[（(][^()（）]*[)）]$', '', x) if not re.fullmatch(r'\([^)]*\)', x) and x.casefold() != 'program files (x86)' else x for x in pieces]
            if not pieces or any(re.fullmatch(r'\[.*\]|［.*］', x) or '游戏名' in x for x in pieces):
                continue
            path = None
            if len(pieces) >= 3 and re.fullmatch('[A-Za-z]:', pieces[0]) and pieces[1].casefold() == 'users':
                tail = pieces[3:]
                root = env.home
                if tail and tail[0].casefold() in ('documents', 'my documents'):
                    root, tail = env.documents, tail[1:]
                elif len(tail) >= 2 and tail[0].casefold() == 'appdata':
                    which = tail[1].casefold()
                    root = {'roaming': env.appdata, 'local': env.localappdata,
                            'locallow': os.path.join(env.home, 'AppData', 'LocalLow')}.get(which)
                    if root:
                        tail = tail[2:]
                    else:
                        root = env.home
                path = os.path.join(root, *tail)
            elif re.fullmatch(r'%(appdata|localappdata|userprofile)%', pieces[0], re.I):
                root = {'%appdata%': env.appdata, '%localappdata%': env.localappdata, '%userprofile%': env.home}[pieces[0].lower()]
                path = os.path.join(root, *pieces[1:])
            elif re.match(r'^[A-Za-z]:[\\/]', part) or part.startswith('/'):
                path = _native('/'.join(pieces) if not part.startswith('/') else '/' + '/'.join(pieces))
            elif game_dir and len(pieces) >= 2:
                first = _ci_path(os.path.join(game_dir, pieces[0]))
                if os.path.exists(first):
                    path = os.path.join(first, *pieces[1:])
            if path is None:
                continue
            path = _native(path)
            filename = None
            if re.search(r'\.[A-Za-z0-9]{1,6}$', os.path.basename(path)):
                path, filename = os.path.dirname(path), os.path.basename(path)
            key = (_key(path), os.path.normcase(filename or ''))
            if key not in seen:
                result.append(Location(path, filename, line, _key(path) in {_key(x) for x in roots}))
                seen.add(key)
    return result


@dataclass(frozen=True)
class SavePack:
    path: str
    kind: str
    label: str


_EXCLUDED = {'.txt', '.url', '.lnk', '.reg', '.bat', '.cmd', '.exe', '.html', '.htm', '.md', '.nfo'}
_PACK_NAME = re.compile(r'存档|存檔|全cg|全回想|cg包|セーブ', re.I)


def _archive(name):
    low = name.lower()
    if re.search(r'\.part\d+\.rar$', low):
        return bool(re.search(r'\.part0*1\.rar$', low))
    return low.endswith(('.zip', '.7z', '.rar', '.7z.001'))


def _walk(root, max_depth=None):
    for here, dirs, files in os.walk(root, followlinks=False):
        depth = len(Path(here).relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if d != '.git' and not os.path.islink(os.path.join(here, d)))
        files = sorted(f for f in files if not os.path.islink(os.path.join(here, f)))
        yield Path(here), dirs[:], files
        if max_depth is not None and depth >= max_depth:
            dirs[:] = []


def find_packs(game_dir: str) -> list[SavePack]:
    found = []
    for root, depth, prefix in ((Path(game_dir), 3, ''), (Path(game_dir).parent / '_补丁', 4, '补丁/')):
        folders, archives = [], []
        for here, dirs, files in _walk(root, depth):
            level = len(here.relative_to(root).parts)
            if level and _PACK_NAME.search(here.name):
                folders.append(here)
            if level < depth:
                archives.extend(here / f for f in files if _PACK_NAME.search(f) and _archive(f))
        leaves = [p for p in folders if not any(p != q and p in q.parents for q in folders)]
        for p in leaves + archives:
            found.append(SavePack(str(p), 'folder' if p in leaves else 'archive', prefix + p.relative_to(root).as_posix()))
    return sorted(found, key=lambda p: p.path)


@dataclass(frozen=True)
class LoadedPack:
    pack: SavePack
    root: str
    files: tuple[str, ...]
    hints: tuple[Location, ...]
    reg_files: tuple[str, ...]


def _payload_file(name):
    return Path(name).suffix.lower() not in _EXCLUDED


def load_pack(pack: SavePack, game_dir: str, env: SaveEnv, extract_dir: str, extra_text: str = '') -> LoadedPack:
    base = Path(pack.path)
    if pack.kind == 'archive':
        base = Path(extract_dir) / hashlib.sha1(os.path.abspath(pack.path).encode()).hexdigest()[:12]
        if base.exists():
            shutil.rmtree(base)
        extract_archive(pack.path, str(base))
    if not base.is_dir() or base.is_symlink():
        raise SaveError('存档包目录不存在或是符号链接')
    hints = parse_locations(extra_text, env, game_dir)
    regs = []
    for here, _, files in _walk(base):
        for f in files:
            if Path(f).suffix.lower() == '.txt':
                hints.extend(parse_locations(decode_text((here / f).read_bytes()), env, game_dir))
            elif Path(f).suffix.lower() == '.reg':
                regs.append(str(here / f))
    root = base
    while True:
        _, dirs, files = next(_walk(root))
        if len(dirs) != 1 or any(_payload_file(f) for f in files):
            break
        if os.path.exists(_ci_path(os.path.join(game_dir, dirs[0]))):
            break
        root /= dirs[0]
    files = tuple(sorted((here / f).relative_to(root).as_posix() for here, _, names in _walk(root) for f in names if _payload_file(f)))
    if not files:
        raise SaveError('包里没有存档文件')
    unique = {}
    for h in hints:
        unique.setdefault((_key(h.path), os.path.normcase(h.filename or '')), h)
    return LoadedPack(pack, str(root), files, tuple(unique.values()), tuple(regs))


@dataclass(frozen=True)
class Destination:
    path: str
    reason: str
    score: int


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _game_file_index(game_dir, loaded):
    """Collect bounded, case-insensitive filename evidence in a single walk."""
    excluded = {_key(p.path) for p in find_packs(game_dir) if p.kind == 'folder'}
    excluded.add(_key(loaded.root))
    ignored = {'.git', '_补丁', '_extract', '_downloads', 'save-backups'}
    index, count = {}, 0
    for here, dirs, files in os.walk(game_dir, followlinks=False):
        if _key(here) in excluded:
            dirs[:] = []
            continue
        relative = Path(here).relative_to(game_dir).parts
        dirs[:] = sorted(d for d in dirs if d.casefold() not in ignored
                         and _key(os.path.join(here, d)) not in excluded
                         and not os.path.islink(os.path.join(here, d))) if len(relative) < 8 else []
        for name in sorted(files):
            if os.path.islink(os.path.join(here, name)):
                continue
            index.setdefault(name.casefold(), []).append(relative + (name,))
            count += 1
            if count >= 50000:
                return index
    return index


def rank_destinations(loaded: LoadedPack, game_dir: str, env: SaveEnv) -> list[Destination]:
    candidates = [(h.path, '存档说明里写的', True) for h in loaded.hints]
    tops = sorted(p.name for p in Path(loaded.root).iterdir() if p.is_dir() and not p.is_symlink())
    if any(os.path.exists(_ci_path(os.path.join(game_dir, t))) for t in tops):
        candidates.append((game_dir, '游戏目录里有同名文件夹', False))
    index = _game_file_index(game_dir, loaded)
    matching_dirs = set()
    for f in loaded.files:
        parts = tuple(p.casefold() for p in f.split('/'))
        for existing in index.get(parts[-1], ()):
            if len(existing) >= len(parts) and tuple(p.casefold() for p in existing[-len(parts):]) == parts:
                directory = existing[:-len(parts)]
                # Keep the original limit on D, even for a nested payload.
                if len(directory) <= 5:
                    matching_dirs.add(directory)
    # Tuple ordering preserves the original sorted walk's directory order.
    matches = {_key(os.path.join(game_dir, *d)) for d in matching_dirs}
    for directory in sorted(matching_dirs):
        candidates.append((os.path.join(game_dir, *directory), '游戏目录里找到同名存档', False))
    if not candidates:
        ext = {Path(f).suffix.lower() for f in loaded.files}
        guess = None
        if '.rpgsave' in ext:
            guess = 'www/save' if os.path.isdir(_ci_path(os.path.join(game_dir, 'www', 'save'))) else 'save'
        elif '.rmmzsave' in ext:
            guess = 'save'
        elif '.save' in ext:
            guess = 'game/saves'
        if guess:
            candidates.append((os.path.join(game_dir, *guess.split('/')), '按引擎猜的', False))
    result = {}
    for path, reason, hint in candidates:
        path = _ci_path(path)
        score = (3 if _key(path) in matches or any(os.path.isfile(_ci_path(os.path.join(path, *f.split('/')))) for f in loaded.files) else 2) if os.path.isdir(path) else (1 if hint else 0)
        item = Destination(path, reason, score)
        if _key(path) not in result or result[_key(path)].score < score:
            result[_key(path)] = item
    return sorted(result.values(), key=lambda x: -x.score)


@dataclass(frozen=True)
class ApplyRecord:
    work_id: int
    backup_dir: str
    dest: str
    source: str
    created: str
    overwritten: tuple[str, ...]
    added: tuple[str, ...]
    restored: bool


def backup_root_for(library_path: str) -> str:
    return os.path.join(os.path.dirname(library_path), 'save-backups')


def _sha1(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha1').hexdigest()


def _manifest(directory, data):
    path = Path(directory) / 'manifest.json'
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def _record(data):
    return ApplyRecord(**{k: tuple(data[k]) if k in ('overwritten', 'added') else data[k] for k in ApplyRecord.__dataclass_fields__})


def _empty_dirs(paths):
    for p in reversed(paths):
        try:
            Path(p).rmdir()
        except (FileNotFoundError, OSError):
            pass


def _mkdirs(path, created):
    absent = []
    p = Path(path)
    while not p.exists():
        absent.append(p)
        p = p.parent
    for p in reversed(absent):
        p.mkdir()
        created.append(str(p))


def _safe_relative(f):
    return bool(f) and not ntpath.splitdrive(f)[0] and not f.startswith(('/', '\\')) and '..' not in re.split(r'[\\/]', f)


def _without_extended_prefix(path):
    if path.startswith('\\\\?\\'):
        path = path[4:]
        if path[:4].casefold() == 'unc\\':
            path = '\\\\' + path[4:]
    return path


def _long_windows_path(path):
    """Expand the existing ancestor, retaining any not-yet-created suffix."""
    if os.name != 'nt':
        return path
    import ctypes
    ancestor, tail = path, []
    while not os.path.exists(ancestor):
        parent, name = ntpath.split(ancestor)
        if not name or parent == ancestor:
            return path
        tail.append(name)
        ancestor = parent
    get_long = ctypes.windll.kernel32.GetLongPathNameW
    get_long.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    get_long.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    size = get_long(ancestor, buffer, len(buffer))
    if size >= len(buffer):
        buffer = ctypes.create_unicode_buffer(size + 1)
        size = get_long(ancestor, buffer, len(buffer))
    return ntpath.join(buffer.value, *reversed(tail)) if size else path


def _dest_forms(path):
    path = _without_extended_prefix(path)
    forms = [ntpath.normpath(path), os.path.realpath(path)]
    if os.name == 'nt':
        forms += [_long_windows_path(p) for p in forms[:]]
    return [ntpath.normpath(_without_extended_prefix(p)).casefold() for p in forms]


def _validate_dest(loaded, dest):
    p = Path(dest)
    if p == p.parent or re.fullmatch(r'[A-Za-z]:[\\/]*', _without_extended_prefix(dest)) or _inside(dest, loaded.root):
        raise SaveError('不能把存档应用到根目录或存档包内部')
    forms = _dest_forms(dest)
    protected = [r'c:\windows', r'c:\program files', r'c:\program files (x86)']
    protected += [os.environ.get(k, '') for k in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)')]
    for root in filter(None, protected):
        for root_form in _dest_forms(root):
            if any(win == root_form or win.startswith(root_form.rstrip('\\') + '\\') for win in forms):
                raise SaveError('不能把存档应用到系统目录')
    if os.path.lexists(dest) and not p.is_dir():
        raise SaveError('目标存在但不是文件夹')
    for f in loaded.files:
        if not _safe_relative(f) or not _inside(str(p / f), dest):
            raise SaveError('存档文件路径不安全')
        src = Path(loaded.root) / f
        if not src.is_file() or src.is_symlink() or not _inside(str(src), loaded.root):
            raise SaveError('存档源文件不存在或路径不安全')


def apply_pack(loaded: LoadedPack, dest: str, backup_root: str, work_id: int, source: str) -> ApplyRecord:
    _validate_dest(loaded, dest)
    dest = os.path.abspath(dest)
    _validate_dest(loaded, dest)
    # Preflight every target before creating the backup or touching live files.
    targets, overwritten, added = {}, [], []
    for f in loaded.files:
        target = Path(_ci_path(os.path.join(dest, *f.split('/'))))
        if target.exists() and not target.is_file():
            raise SaveError(f'目标文件被目录占用：{target}')
        if not _inside(str(target), dest):
            raise SaveError('目标文件经过了目录外的符号链接')
        targets[f] = target
        (overwritten if target.exists() else added).append(f)
    now = datetime.now()
    directory = Path(backup_root) / f'{work_id}-{now:%Y%m%dT%H%M%S}'
    original = directory
    n = 2
    while directory.exists():
        directory = Path(str(original) + f'-{n}')
        n += 1
    rec = ApplyRecord(work_id, str(directory), dest, source, now.isoformat(timespec='microseconds'), tuple(overwritten), tuple(added), False)
    data = asdict(rec) | {'files_sha1': {}, 'created_dirs': [], 'target_paths': {f: str(p) for f, p in targets.items()}}
    touched = []
    try:
        (directory / 'files').mkdir(parents=True)
        # Complete all backups before the first live write.
        for f in overwritten:
            backup = directory / 'files' / f
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(targets[f], backup)
        data['files_sha1'] = {f: _sha1(Path(loaded.root) / f) for f in loaded.files}
        _manifest(directory, data)
        for f, target in targets.items():
            _mkdirs(target.parent, data['created_dirs'])
            touched.append(f)  # include a partially written copy in rollback
            shutil.copy2(Path(loaded.root) / f, target)
        _manifest(directory, data)
    except Exception as error:
        rollback_errors = []
        for f in reversed(touched):
            try:
                if f in overwritten:
                    # Avoid the failing copy2 operation when restoring bytes.
                    shutil.copyfile(directory / 'files' / f, targets[f])
                else:
                    targets[f].unlink(missing_ok=True)
            except OSError as e:
                rollback_errors.append(str(e))
        _empty_dirs(data['created_dirs'])
        data.update(failed=True, error=str(error), rollback_errors=rollback_errors)
        if directory.exists():
            try:
                _manifest(directory, data)
            except OSError:
                pass
        suffix = '；回滚未完成，原文件仍在备份中：' + '；'.join(rollback_errors) if rollback_errors else '（已回滚）'
        raise SaveError(f'应用存档失败：{error}{suffix}') from error
    return rec


def list_applied(backup_root: str, work_id: int) -> list[ApplyRecord]:
    records = []
    for p in Path(backup_root).glob(f'{work_id}-*/manifest.json'):
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            if data.get('work_id') == work_id and not data.get('failed'):
                records.append(_record(data))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(records, key=lambda r: r.created, reverse=True)


def restore(record: ApplyRecord) -> ApplyRecord:
    directory = Path(record.backup_dir)
    try:
        data = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if data.get('restored') or data.get('failed'):
            raise SaveError('这条记录已经还原或应用失败')
        # Read the persisted record so stale objects cannot restore twice.
        rec = _record(data)
        kept = []
        for f in rec.overwritten + rec.added:
            if not _safe_relative(f):
                raise SaveError('备份记录里的路径不安全')
            target = Path(data.get('target_paths', {}).get(f, str(Path(rec.dest) / f)))
            if not _inside(str(target), rec.dest):
                raise SaveError('还原目标经过了目录外的符号链接')
            if f in rec.overwritten:
                backup = directory / 'files' / f
                if not backup.is_file():
                    raise SaveError(f'原文件备份丢失：{backup}')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
            elif target.exists():
                if target.is_file() and _sha1(target) == data['files_sha1'].get(f):
                    target.unlink()
                else:
                    kept.append(f)
        _empty_dirs(data.get('created_dirs', []))
        data.update(restored=True, kept_changed=kept)
        _manifest(directory, data)
        return replace(rec, restored=True)
    except SaveError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SaveError(f'还原失败，备份仍保留：{error}') from error


def extract_archive(archive: str, out_dir: str) -> str:
    out = Path(out_dir)
    def reset_output():
        if out.is_symlink():
            out.unlink()
        elif out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
    try:
        if archive.lower().endswith('.zip'):
            reset_output()
            with zipfile.ZipFile(archive) as zf:
                evidence = []
                for item in zf.infolist():
                    if not item.flag_bits & 0x800:
                        evidence.append(item.filename.encode('cp437'))
                    if item.filename.lower().endswith('.txt'):
                        evidence.append(zf.read(item))
                encoding = _encoding(b'\n'.join(evidence))
                for item in zf.infolist():
                    name = item.filename if item.flag_bits & 0x800 else item.filename.encode('cp437').decode(encoding, errors='replace')
                    name = name.replace('\\', '/')
                    mode = item.external_attr >> 16
                    if not _safe_relative(name) or stat.S_ISLNK(mode) or not _inside(str(out / name), str(out)):
                        continue
                    target = out / name
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(item) as src, target.open('wb') as dst:
                            shutil.copyfileobj(src, dst)
        else:
            errors = []
            commands = [['tar', '-xf', archive, '-C', out_dir]]
            bz = Path(r'C:\Program Files\Bandizip\bz.exe')
            if bz.is_file():
                commands.append([str(bz), 'x', f'-o:{out_dir}', '-y', archive])
            for command in commands:
                reset_output()
                try:
                    result = subprocess.run(command, capture_output=True, timeout=1800)
                    if result.returncode == 0:
                        return out_dir
                    errors.append(result.stderr.decode(errors='replace')[:400])
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append(str(error))
            raise SaveError('解压失败：' + '；'.join(errors))
    except SaveError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        raise SaveError(f'解压失败：{error}') from error
    return out_dir
