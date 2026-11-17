from pathlib import Path
import json
import os
import shutil
import zipfile

import pytest
from galpanel2.core import saves as S

FIX = Path(__file__).parent / 'fixtures' / 'saves'


@pytest.fixture
def env(tmp_path):
    home = tmp_path / 'profile'
    return S.SaveEnv(str(home), str(home / 'AppData' / 'Roaming'), str(home / 'AppData' / 'Local'), str(home / 'Documents'))


@pytest.fixture
def game(tmp_path):
    game = tmp_path / 'game'
    (game / 'savedata').mkdir(parents=True)
    (game / 'savedata' / 'saveg.dat').write_bytes(b'old')
    pack = game / '全CG存档'
    (pack / 'savedata').mkdir(parents=True)
    (pack / 'savedata' / 'saveg.dat').write_bytes(b'new')
    (pack / 'savedata' / 'new.dat').write_bytes(b'added')
    (pack / '说明.txt').write_text('%AppData%\\Studio\\Game', encoding='utf-8')
    (pack / 'unlock.reg').write_bytes(b'reg')
    return game


def load(game, env, tmp_path):
    return S.load_pack(S.SavePack(str(game / '全CG存档'), 'folder', '全CG存档'), str(game), env, str(tmp_path / 'extract'))


def sample(name, env, game=None):
    return S.parse_locations(S.decode_text((FIX / 'packs' / name).read_bytes()), env, str(game) if game else None)


def test_decode_utf8_and_boms():
    text = '存档位置'
    assert S.decode_text(text.encode()) == text
    assert S.decode_text(text.encode('utf-8-sig')) == text
    assert S.decode_text(text.encode('utf-16')) == text


def test_decode_legacy_encodings():
    for text, encoding in [('存档位置：用户名', 'gbk'), ('説明書 あざらしそふと', 'cp932')]:
        assert S.decode_text(text.encode(encoding)) == text
    assert '\ufffd' in S.decode_text(b'\x81')


def test_parse_ntraholic(env):
    loc = sample('ntraholic_utf8.txt', env)
    assert len(loc) == 1 and loc[0].path == str(Path(env.home) / 'AppData/LocalLow/TiramisuLovesNtr/Ntraholic')


def test_parse_admin(env):
    assert sample('kansoku_admin.txt', env) == [S.Location(str(Path(env.home) / 'AppData/LocalLow/TiramisuLovesNtr/Ntraholic'), None,
        'C:\\Users\\Administrator\\AppData\\LocalLow\\TiramisuLovesNtr\\Ntraholic', False)]


def test_parse_goodbye(env):
    loc = sample('goodbye_gbk.txt', env)
    assert len(loc) == 1 and loc[0].path == str(Path(env.appdata) / 'Godot/app_userdata/Goodbye Eternity/saves')


def test_parse_demonqueen(env):
    loc = sample('demonqueen_file.txt', env)
    assert len(loc) == 1 and loc[0].path == str(Path(env.appdata) / 'Godot/app_userdata/Seduction Of The Demon Queen')
    assert loc[0].filename == 'autosave_v0.5.0.0_2026-07-05_23-37-49.sav'


def test_parse_documents(env):
    loc = sample('yukionna_documents.txt', env)
    assert len(loc) == 1 and loc[0].path == str(Path(env.documents) / '美少女万華鏡異聞_雪おんな/SaveData')


def test_parse_generic(env):
    loc = sample('lifeguard_username_brace.txt', env)
    assert len(loc) == 1 and loc[0].generic and loc[0].path == str(Path(env.home) / 'AppData/LocalLow')


def test_parse_multiple(env):
    loc = sample('oppai_multi.txt', env)
    suffix = 'みるくふぁくとりー/もっと！孕ませ！炎のおっぱい超エロ▼アプリ学園！'
    assert [l.path for l in loc] == [str(Path(env.documents) / suffix), str(Path(env.appdata) / suffix)]
    assert [l.filename for l in loc] == ['datasu.ksd'] * 2


def test_parse_reject_guides_and_comments(env):
    assert sample('rpgmaker_guide.txt', env) == []
    assert S.parse_locations('メーカーＨＰ【http://example.test/】', env, str(FIX)) == []
    assert S.parse_locations('//C:\\GameSaveData\\\n// <APPDATA>', env, None) == []
    assert S.parse_locations('%AppData%\\［Studio］\\Title', env, None) == []


def test_parse_relative(env, tmp_path):
    (tmp_path / 'Dojo NTR_Data').mkdir()
    loc = sample('dojo_relative.txt', env, tmp_path)
    assert len(loc) == 1 and loc[0].path == str(tmp_path / 'Dojo NTR_Data/game_data')
    assert sample('dojo_relative.txt', env) == []
    assert sample('yakuza_garbled.txt', env, tmp_path) == []


def test_parse_annotations_normalize_and_deduplicate(env):
    text = '存档位置：%AppData%\\..\\LocalLow\\A\\B\n存档位置：%appdata%\\..\\LocalLow\\A\\B'
    loc = S.parse_locations(text, env, None)
    assert len(loc) == 1 and loc[0].path == str(Path(env.home) / 'AppData/LocalLow/A/B')
    loc = S.parse_locations('D:/My Saves/Game/*.sav', env, None)
    assert loc[0].filename == '*.sav' and loc[0].path == os.path.normpath('D:/My Saves/Game')
    loc = S.parse_locations('C:/Users/示例用户（你的用户名）/AppData（隐藏文件夹）/Roaming/A', env, None)
    assert loc[0].path == str(Path(env.appdata) / 'A')


def test_find_leaf_patch_archives_and_depth(tmp_path):
    game = tmp_path / 'game'
    for rel in ['A786存档/全CG存档', 'A786存档/存档', '.git/存档', 'a/b/c/存档']:
        (game / rel).mkdir(parents=True)
    (tmp_path / '_补丁/汉化/存档').mkdir(parents=True)
    for name in ['全CG.zip', '存档.part1.rar', '存档.part2.rar', '存档.7z.001', '存档.txt']:
        (game / name).write_bytes(b'x')
    packs = S.find_packs(str(game))
    assert {p.label for p in packs} == {'A786存档/全CG存档', 'A786存档/存档', '补丁/汉化/存档', '全CG.zip', '存档.part1.rar', '存档.7z.001'}
    assert [p.path for p in packs] == sorted(p.path for p in packs)


def test_find_does_not_follow_symlinks(tmp_path, monkeypatch):
    (tmp_path / 'real/存档').mkdir(parents=True)
    (tmp_path / 'game').mkdir()
    link = tmp_path / 'game/全CG存档'
    link.mkdir()
    real_islink = S.os.path.islink
    monkeypatch.setattr(S.os.path, 'islink', lambda p: Path(p) == link or real_islink(p))
    assert S.find_packs(str(tmp_path / 'game')) == []


def test_load_mirror_exclusions_and_reg(game, env, tmp_path):
    pack = game / '全CG存档'
    for name in ['open.url', 'open.lnk', 'x.BAT', 'x.cmd', 'x.exe', 'x.html', 'x.htm', 'x.md', 'x.nfo']:
        (pack / name).write_bytes(b'ignored')
    loaded = load(game, env, tmp_path)
    assert loaded.root == str(pack)
    assert loaded.files == ('savedata/new.dat', 'savedata/saveg.dat')
    assert loaded.reg_files == (str(pack / 'unlock.reg'),)
    assert loaded.hints[0].path == str(Path(env.appdata) / 'Studio/Game')


def test_load_drills_single_wrapper(env, tmp_path):
    pack = tmp_path / '存档/wrapper/save'
    pack.mkdir(parents=True)
    (pack / 'a.dat').write_bytes(b'a')
    loaded = S.load_pack(S.SavePack(str(pack.parent.parent), 'folder', '存档'), str(tmp_path / 'game'), env, str(tmp_path / 'x'))
    assert loaded.root == str(pack) and loaded.files == ('a.dat',)


def test_load_empty_error(env, tmp_path):
    (tmp_path / '说明.txt').write_text('x')
    with pytest.raises(S.SaveError, match='包里没有存档文件'):
        S.load_pack(S.SavePack(str(tmp_path), 'folder', 'x'), str(tmp_path / 'game'), env, str(tmp_path / 'x'))


def test_real_zip_cp932_and_hint_order(game, env, tmp_path):
    loaded = S.load_pack(S.SavePack(str(FIX / 'toshiuekanojo_cg_save.zip'), 'archive', 'x'), str(game), env, str(tmp_path / 'x'), '%LocalAppData%\\Extra')
    assert Path(loaded.root).name == 'toshiuekanojo' and loaded.files == ('saveg.dat',)
    assert (Path(loaded.root) / '説明書.txt').exists()
    assert loaded.hints[0].path == str(Path(env.localappdata) / 'Extra')
    assert loaded.hints[1].filename == 'saveg.dat'
    assert loaded.hints[1].path == str(Path(env.appdata) / 'あざらしそふと/年上彼女')


def test_rank_live_save_wins_hint(game, env, tmp_path):
    loaded = S.load_pack(S.SavePack(str(FIX / 'toshiuekanojo_cg_save.zip'), 'archive', 'x'), str(game), env, str(tmp_path / 'x'))
    ranked = S.rank_destinations(loaded, str(game), env)
    assert ranked[0] == S.Destination(str(game / 'savedata'), '游戏目录里找到同名存档', 3)
    assert any(d.score == 1 and 'あざらしそふと' in d.path for d in ranked)


def test_rank_mirror_no_double_savedata(game, env, tmp_path):
    ranked = S.rank_destinations(load(game, env, tmp_path), str(game), env)
    assert ranked[0].path == str(game) and ranked[0].score == 3
    assert all(d.path != str(game / 'savedata') for d in ranked)


def test_rank_engine_fallback(env, tmp_path):
    game = tmp_path / 'game'
    (game / 'www/save').mkdir(parents=True)
    pack = tmp_path / 'pack'
    pack.mkdir()
    for ext, target in [('rpgsave', 'www/save'), ('rmmzsave', 'save'), ('save', 'game/saves')]:
        file = pack / ('a.' + ext)
        file.write_bytes(b'a')
        loaded = S.LoadedPack(S.SavePack(str(pack), 'folder', 'pack'), str(pack), (file.name,), (), ())
        ranked = S.rank_destinations(loaded, str(game), env)
        assert ranked[0].path == str(game / target) and ranked[0].reason == '按引擎猜的'
        file.unlink()


def test_apply_backup_manifest_and_restore(game, env, tmp_path):
    rec = S.apply_pack(load(game, env, tmp_path), str(game), str(tmp_path / 'backup'), 7, 'bundled:全CG存档')
    assert rec.overwritten == ('savedata/saveg.dat',) and rec.added == ('savedata/new.dat',)
    assert (game / 'savedata/saveg.dat').read_bytes() == b'new'
    assert (Path(rec.backup_dir) / 'files/savedata/saveg.dat').read_bytes() == b'old'
    data = json.loads((Path(rec.backup_dir) / 'manifest.json').read_text())
    assert len(data['files_sha1']) == 2 and data['source'] == 'bundled:全CG存档'
    assert S.list_applied(str(tmp_path / 'backup'), 7) == [rec]
    assert S.list_applied(str(tmp_path / 'backup'), 8) == []
    restored = S.restore(rec)
    assert restored.restored and (game / 'savedata/saveg.dat').read_bytes() == b'old'
    assert not (game / 'savedata/new.dat').exists()
    with pytest.raises(S.SaveError):
        S.restore(rec)


def test_restore_keeps_changed_and_cleans_only_created_dirs(game, env, tmp_path):
    dest = tmp_path / 'newdest'
    rec = S.apply_pack(load(game, env, tmp_path), str(dest), str(tmp_path / 'backup'), 7, 'x')
    (dest / 'savedata/new.dat').write_bytes(b'played')
    S.restore(rec)
    assert (dest / 'savedata/new.dat').read_bytes() == b'played'
    assert not (dest / 'savedata/saveg.dat').exists()
    data = json.loads((Path(rec.backup_dir) / 'manifest.json').read_text())
    assert data['kept_changed'] == ['savedata/new.dat']
    dest2 = tmp_path / 'clean/a'
    rec2 = S.apply_pack(load(game, env, tmp_path), str(dest2), str(tmp_path / 'backup'), 7, 'x')
    S.restore(rec2)
    assert not (tmp_path / 'clean').exists()


def test_apply_refuses_unsafe_destinations(game, env, tmp_path):
    loaded = load(game, env, tmp_path)
    file = tmp_path / 'file'
    file.write_bytes(b'x')
    for dest in [Path(tmp_path.anchor), Path(loaded.root), Path(loaded.root) / 'savedata', file, 'C:/', r'C:\Windows\Temp', r'C:\Program Files\Game']:
        with pytest.raises(S.SaveError):
            S.apply_pack(loaded, str(dest), str(tmp_path / 'backup'), 1, 'x')
    assert not (tmp_path / 'backup').exists()


def test_apply_mid_copy_failure_rolls_back(game, env, tmp_path, monkeypatch):
    loaded = load(game, env, tmp_path)
    original_copy = shutil.copy2
    calls = []
    def fail_later(src, dst, *args, **kwargs):
        if Path(dst) == game / 'savedata/saveg.dat':
            Path(dst).write_bytes(b'partial')
            calls.append(dst)
            raise OSError('simulated write failure')
        return original_copy(src, dst, *args, **kwargs)
    monkeypatch.setattr(S.shutil, 'copy2', fail_later)
    with pytest.raises(S.SaveError, match='已回滚'):
        S.apply_pack(loaded, str(game), str(tmp_path / 'backup'), 1, 'x')
    assert calls and (game / 'savedata/saveg.dat').read_bytes() == b'old'
    assert not (game / 'savedata/new.dat').exists()
    assert S.list_applied(str(tmp_path / 'backup'), 1) == []
    manifest = next((tmp_path / 'backup').glob('*/manifest.json'))
    assert json.loads(manifest.read_text())['failed'] is True


def test_apply_rollback_removes_created_dirs(game, env, tmp_path, monkeypatch):
    original = shutil.copy2
    dest = tmp_path / 'new/sub'
    def fail(src, dst, *a, **kw):
        if Path(dst).name == 'saveg.dat':
            Path(dst).write_bytes(b'partial')
            raise OSError('write failed')
        return original(src, dst, *a, **kw)
    monkeypatch.setattr(S.shutil, 'copy2', fail)
    with pytest.raises(S.SaveError):
        S.apply_pack(load(game, env, tmp_path), str(dest), str(tmp_path / 'backup'), 1, 'x')
    assert not (tmp_path / 'new').exists()


def test_zip_slip_absolute_and_symlink(tmp_path):
    zip_path = tmp_path / 'bad.zip'
    with zipfile.ZipFile(zip_path, 'w') as z:
        for name in ['../escape.sav', '/absolute.sav', 'C:/escape.sav', '..\\escape2.sav']:
            z.writestr(name, b'bad')
        item = zipfile.ZipInfo('link')
        item.create_system = 3
        item.external_attr = 0o120777 << 16
        z.writestr(item, '../escape.sav')
        z.writestr('safe/a.sav', b'ok')
    S.extract_archive(str(zip_path), str(tmp_path / 'out'))
    assert [p.relative_to(tmp_path / 'out').as_posix() for p in (tmp_path / 'out').rglob('*') if p.is_file()] == ['safe/a.sav']
    assert not (tmp_path / 'escape.sav').exists()


def test_backup_root_and_environment(tmp_path):
    assert S.backup_root_for(str(tmp_path / 'library.galdb')) == str(tmp_path / 'save-backups')
    current = S.SaveEnv.current()
    assert all(isinstance(p, str) and p for p in (current.home, current.appdata, current.localappdata, current.documents))


def test_rank_depth_limit_applies_to_candidate_directory(env, tmp_path):
    game = tmp_path / 'game'
    target = game / 'one/two/three/four/five'
    (target / 'savedata').mkdir(parents=True)
    (target / 'savedata/a.dat').write_bytes(b'old')
    root = tmp_path / 'pack'
    (root / 'savedata').mkdir(parents=True)
    (root / 'savedata/a.dat').write_bytes(b'new')
    loaded = S.LoadedPack(S.SavePack(str(root), 'folder', 'pack'), str(root), ('savedata/a.dat',), (), ())
    ranked = S.rank_destinations(loaded, str(game), env)
    assert ranked[0].path == str(target) and ranked[0].score == 3
    assert all(d.path != str(target / 'savedata') for d in ranked)


def test_rank_large_game_uses_single_index_under_five_seconds(env, tmp_path):
    import time
    game, pack = tmp_path / 'game', tmp_path / 'pack'
    game.mkdir()
    pack.mkdir()
    for number in range(1500):
        (game / f'dir{number:04}').mkdir()
    files = tuple(f'save{number:04}.dat' for number in range(300))
    for name in files:
        (pack / name).write_bytes(b'new')
    (game / 'dir1499' / files[-1].upper()).write_bytes(b'old')
    loaded = S.LoadedPack(S.SavePack(str(pack), 'folder', 'pack'), str(pack), files, (), ())
    started = time.perf_counter()
    ranked = S.rank_destinations(loaded, str(game), env)
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f'rank_destinations took {elapsed:.3f}s'
    assert ranked == [S.Destination(str(game / 'dir1499'), '游戏目录里找到同名存档', 3)]
    print(f'1500 directories / 300 payload files: {elapsed:.3f}s')


def test_rank_excludes_other_packs_and_cache_directories(env, tmp_path):
    game = tmp_path / 'game'
    for directory in ('存档', '全CG存档', 'savedata', '_补丁/nested', '_extract/nested',
                      '_downloads/nested', 'save-backups/nested'):
        target = game / directory
        target.mkdir(parents=True)
        (target / 'save0001.dat').write_bytes(b'save')
    loaded = S.load_pack(S.SavePack(str(game / '全CG存档'), 'folder', '全CG存档'),
                         str(game), env, str(tmp_path / 'extract'))
    assert S.rank_destinations(loaded, str(game), env) == [
        S.Destination(str(game / 'savedata'), '游戏目录里找到同名存档', 3)]


def test_rank_index_matches_path_segments_case_insensitively_in_walk_order(env, tmp_path):
    game, pack = tmp_path / 'game', tmp_path / 'pack'
    pack.mkdir()
    for directory in ('b/SaveData', 'a/SaveData', 'a/deeper/SaveData', 'bad/notSaveData'):
        target = game / directory
        target.mkdir(parents=True)
        (target / 'SAVEG.DAT').write_bytes(b'old')
    loaded = S.LoadedPack(S.SavePack(str(pack), 'folder', 'pack'), str(pack), ('savedata/saveg.dat',), (), ())
    ranked = S.rank_destinations(loaded, str(game), env)
    assert [d.path for d in ranked] == [str(game / d) for d in ('a', 'a/deeper', 'b')]
    assert all(d.score == 3 for d in ranked)


def test_rank_index_depth_eight_and_file_limit(env, tmp_path, monkeypatch):
    game, pack = tmp_path / 'game', tmp_path / 'pack'
    pack.mkdir()
    target = game / 'a/b/c/d/e/f/g/h'
    target.mkdir(parents=True)
    (target / 'in.dat').write_bytes(b'in')
    (target / 'i').mkdir()
    (target / 'i/out.dat').write_bytes(b'out')
    loaded = S.LoadedPack(S.SavePack(str(pack), 'folder', 'pack'), str(pack), (), (), ())
    assert set(S._game_file_index(str(game), loaded)) == {'in.dat'}
    monkeypatch.setattr(S, 'find_packs', lambda path: [])
    def walk(path, **kwargs):
        yield str(game), [], [f'{n:05}.dat' for n in range(50001)]
        raise AssertionError('walk must stop once the file budget is exhausted')
    monkeypatch.setattr(S.os, 'walk', walk)
    index = S._game_file_index(str(game), loaded)
    assert len(index) == 50000 and '49999.dat' in index and '50000.dat' not in index


def test_apply_refuses_extended_system_path(game, env, tmp_path):
    with pytest.raises(S.SaveError, match='系统目录'):
        S.apply_pack(load(game, env, tmp_path), r'\\?\C:\Windows\Temp\x', str(tmp_path / 'backup'), 1, 'x')
    assert not (tmp_path / 'backup').exists()


def test_apply_refuses_realpath_system_ancestor_with_missing_tail(game, env, tmp_path, monkeypatch):
    protected = tmp_path / 'Windows'
    protected.mkdir()
    link = tmp_path / 'alias'
    if os.name == 'nt':
        return  # The junction-specific test below covers Windows without symlink privileges.
    link.symlink_to(protected, target_is_directory=True)
    monkeypatch.setenv('SystemRoot', str(protected))
    with pytest.raises(S.SaveError, match='系统目录'):
        S.apply_pack(load(game, env, tmp_path), str(link / 'missing/x'), str(tmp_path / 'backup'), 1, 'x')
    assert not (protected / 'missing').exists() and not (tmp_path / 'backup').exists()


def test_apply_refuses_windows_short_system_path(game, env, tmp_path):
    if os.name != 'nt':
        return
    with pytest.raises(S.SaveError, match='系统目录'):
        S.apply_pack(load(game, env, tmp_path), r'C:\PROGRA~1\x', str(tmp_path / 'backup'), 1, 'x')
    assert not (tmp_path / 'backup').exists()


def test_apply_refuses_windows_system_junction(game, env, tmp_path):
    if os.name != 'nt':
        return
    import subprocess
    link = tmp_path / 'windows-junction'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), os.environ.get('SystemRoot', r'C:\Windows')],
                   check=True, capture_output=True)
    try:
        with pytest.raises(S.SaveError, match='系统目录'):
            S.apply_pack(load(game, env, tmp_path), str(link / 'Temp/missing/x'), str(tmp_path / 'backup'), 1, 'x')
        assert not (tmp_path / 'backup').exists()
    finally:
        os.rmdir(link)


@pytest.mark.parametrize('failure', ['exit', 'timeout'])
def test_extract_retry_clears_only_output_and_uses_1800_seconds(tmp_path, monkeypatch, failure):
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'stale.dat').write_bytes(b'stale')
    sentinel = tmp_path / 'keep.dat'
    sentinel.write_bytes(b'keep')
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda path: True if str(path) == r'C:\Program Files\Bandizip\bz.exe' else original_is_file(path))
    calls = []
    def run(command, *, capture_output, timeout):
        assert capture_output and timeout == 1800
        assert list(out.iterdir()) == []
        calls.append(command)
        if len(calls) == 1:
            (out / 'partial.dat').write_bytes(b'incomplete')
            if failure == 'timeout':
                raise S.subprocess.TimeoutExpired(command, timeout)
            return S.subprocess.CompletedProcess(command, 1, stderr=b'tar failed')
        (out / 'good.dat').write_bytes(b'complete')
        return S.subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(S.subprocess, 'run', run)
    assert S.extract_archive(str(tmp_path / 'archive.rar'), str(out)) == str(out)
    assert len(calls) == 2 and calls[0][0] == 'tar' and calls[1][1] == 'x'
    assert [p.name for p in out.iterdir()] == ['good.dat'] and sentinel.read_bytes() == b'keep'


def test_extract_zip_clears_previous_output(tmp_path):
    archive, out = tmp_path / 'archive.zip', tmp_path / 'out'
    out.mkdir()
    (out / 'stale.dat').write_bytes(b'stale')
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('new.dat', b'new')
    S.extract_archive(str(archive), str(out))
    assert [p.name for p in out.iterdir()] == ['new.dat']


def test_parse_program_files_x86_keeps_complete_path(env):
    text = r'C:\Program Files (x86)\Steam\userdata\1\saves'
    locations = S.parse_locations(text, env, None)
    assert locations == [S.Location(S._native(text), None, text, False)]
    annotated = r'C:\Program Files (x86)\Steam\saves (复制到这里)'
    assert S.parse_locations(annotated, env, None)[0].path == S._native(r'C:\Program Files (x86)\Steam\saves')


def test_extract_retry_does_not_follow_output_symlink(tmp_path):
    if os.name == 'nt':
        return
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'keep.dat').write_bytes(b'keep')
    out = tmp_path / 'out'
    out.symlink_to(external, target_is_directory=True)
    archive = tmp_path / 'archive.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('new.dat', b'new')
    S.extract_archive(str(archive), str(out))
    assert not out.is_symlink() and (out / 'new.dat').read_bytes() == b'new'
    assert [p.name for p in external.iterdir()] == ['keep.dat']
