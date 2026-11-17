"""库操作共用 API：所有数据访问均经 core，不执行 SQL。"""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from galpanel2.core import binding, domain, meta, records, repository, shelf, sites, store
from galpanel2.identify import hints as hint_reader

WRITES = ('bind', 'unbind', 'set', 'summary', 'apply')


class AgentError(Exception):
    def __init__(self, code, message, exit_code=1):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def _ref(text):
    if not isinstance(text, str):
        raise ValueError('ref 必须是字符串')
    m = re.fullmatch(r'(vndb|dlsite|bangumi|steam|f95):(.+)', text)
    if m:
        site, sid = m.groups()
        pattern = {'vndb': r'v\d+', 'dlsite': r'(?:RJ|VJ|BJ|RE)\d{6,8}',
                   'bangumi': r'\d+', 'steam': r'\d+', 'f95': r'\d+'}[site]
        if re.fullmatch(pattern, sid):
            return records.Ref(site, sid)
    result = records.parse_ref(text)
    if result is None:
        raise ValueError('无法识别 ref，请用 vndb:v17、dlsite:RJ123456、bangumi:123、steam:123 或 f95:123')
    return result


def _record(record):
    result = asdict(record)
    result.pop('payload', None)
    payload = json.loads(record.payload)
    raw_platforms = payload.get('platforms', []) if isinstance(payload, dict) else []
    platforms = ([k for k, supported in raw_platforms.items() if supported] if isinstance(raw_platforms, dict)
                 else [p for p in raw_platforms if isinstance(p, str)] if isinstance(raw_platforms, list) else [])
    result.update(ref=f'{record.site}:{record.site_id}', platforms=platforms,
                  platform_note='站点未提供平台，需人工核验' if not platforms else '来自站点资料快照')
    return result


def _catalog(lib):
    return (shelf.list_shelf(lib), meta.list_work_meta(lib), meta.list_brands(lib), meta.list_tags(lib))


def _show(lib, work_id, catalog=None):
    work = repository.get_work(lib, work_id)
    result = asdict(work)
    result['info'] = asdict(binding.get_info(lib, work_id))
    result['sites'] = result['info']['links']
    shelves, metas, brands, tags = catalog if catalog is not None else _catalog(lib)
    result.update(asdict(shelves.get(work_id, shelf.Shelf(None, None))))
    wm = metas.get(work_id)
    result['brands'] = [asdict(b) for b in brands if wm and b.id in wm.brand_ids]
    result['tags'] = [asdict(t) for t in tags if wm and t.id in wm.tag_ids]
    for cp in result['copies']:
        cp['exists'] = os.path.exists(cp['path'])
    return result


def _list(lib, **args):
    rows = []
    catalog = _catalog(lib)
    for work in repository.list_works(lib, include_hidden=True):
        row = _show(lib, work.id, catalog)
        if args.get('unbound') and row['sites']:
            continue
        if any(k in args and row[k] != args[k] for k in ('category', 'status')):
            continue
        folder = args.get('folder')
        if folder and not any(folder.casefold() in cp['path'].casefold() for cp in row['copies']):
            continue
        query = args.get('query', '').casefold()
        titles = [row['title'], *(t[0] for t in row['info']['titles'])]
        if query and query not in ' '.join([*titles, row['developer'], *(b['name'] for b in row['brands'])]).casefold():
            continue
        rows.append({k: row[k] for k in ('id', 'revision', 'title', 'developer', 'brands', 'category', 'status', 'sites', 'copies')})
    offset, limit = args.get('offset', 0), args.get('limit', 100)
    return {'total': len(rows), 'items': rows[offset:offset + limit]}


def _hints(lib, work_id):
    return {'work_id': work_id, 'copies': [hint_reader.collect_folder(str(cp.id), cp.path)
            for cp in repository.get_work(lib, work_id).copies]}


def _search(lib, args):
    client = sites.SiteClient()
    queries, refs = [], []
    selected = [args['site']] if args.get('site') else list(records.SITE_ORDER)
    if args.get('work') is not None:
        work = _show(lib, args['work'])
        queries = [records.search_text(work['title'])]
        for item in _hints(lib, args['work'])['copies']:
            refs.extend(c for c in item.get('codes', []) if c.split(':')[0] in selected)
            name = item.get('folder_name') or item.get('name')
            if name:
                queries.append(records.search_text(name))
    if args.get('query'):
        queries.append(args['query'])
    candidates, errors, seen = [], [], set()
    for ref in dict.fromkeys(refs):
        try:
            r = client.fetch(_ref(ref))
            candidates.append(_record(r)); seen.add((r.site, r.site_id))
        except domain.SiteError as exc:
            errors.append({'ref': ref, 'message': str(exc)})
    for query in dict.fromkeys(queries):
        for site in selected:
            try:
                for r in client.search(site, query):
                    if (r.site, r.site_id) not in seen:
                        candidates.append(_record(r)); seen.add((r.site, r.site_id))
            except domain.SiteError as exc:
                errors.append({'site': site, 'query': query, 'message': str(exc)})
    return {'queries': queries, 'candidates': candidates, 'errors': errors}


def _prepare(lib, op):
    wid = op['work_id']
    current = _show(lib, wid)
    if op.get('revision') is not None and current['revision'] != op['revision']:
        raise AgentError('revision_conflict', f'作品 {wid} 已变化，请重新 show 和 dry-run')
    command = op['command']
    plan = {'command': command, 'work_id': wid, 'revision': current['revision']}
    record = None
    if command == 'bind':
        record = sites.SiteClient().fetch(_ref(op['ref']))
        plan['changes'] = asdict(binding.plan_bind(lib, wid, record))
        if plan['changes']['taken_by'] is not None:
            raise AgentError('duplicate_link', '该资料已绑定到其他作品')
    elif command == 'unbind':
        before = current['sites'].get(op['site'])
        plan['changes'] = {'site': op['site'], 'before': before, 'after': None}
        plan['changed'] = before is not None
    elif command == 'set':
        plan['changes'] = {k: {'before': current[k], 'after': op[k]} for k in ('category', 'status') if k in op}
    else:
        plan['changes'] = {'summary': {'before': current['info']['summary'], 'after': op['text'].strip() or None}}
    if command in ('set', 'summary'):
        plan['changed'] = any(v['before'] != v['after'] for v in plan['changes'].values())
    return plan, record


def _write(lib, op, prepared):
    plan, record = prepared
    wid, rev = op['work_id'], plan['revision']
    if not plan.get('changed', True):
        return {**plan, 'applied': False, 'cover_error': None}
    cover_error = None
    cover = None
    if op['command'] == 'bind' and plan['changes']['cover'] == 'set':
        cover, cover_error = binding.download_cover(sites.SiteClient(), record)
    with lib.transaction():
        if repository.get_work(lib, wid).revision != rev:
            raise domain.WriteConflict('Work changed')
        if op['command'] == 'bind':
            new_revision = binding.apply_bind(lib, wid, rev, record, cover_bytes=cover)
        else:
            if op['command'] == 'unbind':
                binding.unbind(lib, wid, op['site'])
            elif op['command'] == 'summary':
                binding.set_summary(lib, wid, op['text'])
            else:
                if 'category' in op:
                    shelf.set_category(lib, wid, op['category'])
                if 'status' in op:
                    shelf.set_status(lib, wid, op['status'])
            # 这些 core 方法不递增 revision，用 core CAS 在同一事务补齐。
            new_revision = repository.edit_work(lib, wid, rev).revision
    return {**plan, 'applied': True, 'revision': new_revision, 'cover_error': cover_error}


def _failure(exc):
    if isinstance(exc, AgentError):
        return {'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}, exc.exit_code
    mapping = ((domain.LibraryLocked, 'library_locked', '库正由 GUI 或其他写者使用，请关闭 GUI 后重试', 1),
               (domain.WriteConflict, 'revision_conflict', '作品已变化，请重新读取 revision', 1),
               (domain.DuplicateLink, 'duplicate_link', '资料已绑定到其他作品', 1),
               (domain.NeedsUpgrade, 'needs_upgrade', '请先在 GUI 升级库', 2),
               (domain.NotALibrary, 'invalid_library', '库文件不存在或不是有效的 GalPanel2 库', 2),
               (domain.SiteError, 'site_error', '资料请求失败：' + str(exc), 1),
               (KeyError, 'not_found', '作品或绑定不存在', 1),
               (ValueError, 'invalid_arguments', str(exc), 2),
               (TypeError, 'invalid_arguments', '参数类型不正确', 2),
               (OSError, 'environment_error', '文件读取、备份或访问失败', 2))
    for cls, code, message, status in mapping:
        if isinstance(exc, cls):
            return {'ok': False, 'error': {'code': code, 'message': message}}, status
    return {'ok': False, 'error': {'code': 'internal_error', 'message': '内部错误，请检查库与运行环境'}}, 2


def validate(command, args):
    # CLI 和 MCP 共用的 schema 和严格参数校验。
    from .schema import SCHEMAS
    if command not in SCHEMAS:
        raise ValueError('未知命令')
    schema = SCHEMAS[command]
    unknown = set(args) - set(schema['properties'])
    if unknown:
        raise ValueError('未知参数：' + ', '.join(sorted(unknown)))
    for key in schema.get('required', []):
        if key not in args:
            raise ValueError('缺少参数：' + key)
    for key, value in args.items():
        spec = schema['properties'][key]
        typ = spec['type']
        allowed_types = typ if isinstance(typ, list) else [typ]
        valid = any((t == 'null' and value is None) or (t == 'string' and isinstance(value, str)) or
                    (t == 'integer' and type(value) is int) or (t == 'boolean' and type(value) is bool) or
                    (t == 'array' and isinstance(value, list)) for t in allowed_types)
        if not valid or ('enum' in spec and value not in spec['enum']):
            raise ValueError('参数取值或类型不正确：' + key)
        if type(value) is int and 'minimum' in spec and value < spec['minimum']:
            raise ValueError('参数太小：' + key)
    if command == 'search' and not (args.get('work') or (args.get('site') and args.get('query'))):
        raise ValueError('search 需要 --work 或 --site 与 --query')
    if command == 'apply' and (('file' in args) == ('operations' in args)):
        raise ValueError('apply 需要 file 或 operations，且只能选一个')
    if command == 'set' and not any(k in args for k in ('category', 'status')):
        raise ValueError('set 需要 category 或 status')
    if command in ('bind', 'bind-plan', 'fetch'):
        _ref(args['ref'])


def execute(command, *, library=None, **args):
    """返回 (JSON envelope, exit code)，不打印、不启动 GUI、不移动游戏。"""
    try:
        validate(command, args)
        path = library or os.getenv('GALPANEL2_LIBRARY')
        if command == 'fetch':
            return {'ok': True, 'data': _record(sites.SiteClient().fetch(_ref(args['ref'])))}, 0
        if command == 'search' and not args.get('work'):
            return {'ok': True, 'data': _search(None, args)}, 0
        if not path:
            raise AgentError('library_required', '请指定 --library 或 GALPANEL2_LIBRARY', 2)
        applying = command in WRITES and args.get('apply', False)
        # 禁止 writable open 的游戏会话恢复副作用；备份必须早于任何写入。
        with store.open_library(path, readonly=not applying, recover=False) as lib:
            backup = None
            if command == 'status':
                rows = _list(lib, limit=2**63-1)['items']
                data = {'schema_version': lib.schema_version, 'works': len(rows),
                        'copies': sum(len(r['copies']) for r in rows),
                        'sources': [asdict(s) for s in repository.list_sources(lib)],
                        'categories': dict(Counter(r['category'] or 'none' for r in rows)),
                        'statuses': dict(Counter(r['status'] or 'none' for r in rows)),
                        'unbound': sum(not r['sites'] for r in rows)}
            elif command == 'list':
                data = _list(lib, **args)
            elif command == 'show':
                data = _show(lib, args['work_id'])
            elif command == 'hints':
                data = _hints(lib, args['work_id'])
            elif command == 'search':
                data = _search(lib, args)
            elif command == 'bind-plan':
                data, _ = _prepare(lib, {'command': 'bind', **args})
            else:
                if command == 'apply':
                    operations = args.get('operations')
                    if operations is None:
                        operations = json.loads(Path(args['file']).read_text(encoding='utf-8'))
                    if not isinstance(operations, list):
                        raise ValueError('批量计划必须是操作数组')
                else:
                    operations = [{'command': command, **{k: v for k, v in args.items() if k != 'apply'}}]
                for op in operations:
                    if not isinstance(op, dict) or op.get('command') not in WRITES[:-1]:
                        raise ValueError('批量操作必须为 bind/unbind/set/summary')
                    validate(op['command'], {k: v for k, v in op.items() if k != 'command'})
                    if 'apply' in op:
                        raise ValueError('批量条目不允许 apply；只使用会话级 apply')
                prepared = []
                for op in operations:
                    try:
                        prepared.append((_prepare(lib, op), None))
                    except (AgentError, KeyError, domain.SiteError) as exc:
                        prepared.append((None, _failure(exc)[0]))
                # 所有操作先校验和取资料，再备份和执行；逐项事务，允许独立成功。
                results = []
                for op, (plan, error) in zip(operations, prepared):
                    if error:
                        results.append(error); continue
                    try:
                        if applying:
                            # 前面的批量条目可能已改变同一作品；重新检查 revision 和实际差异。
                            if op['command'] != 'bind':
                                plan = _prepare(lib, op)
                            if plan[0].get('changed', True) and backup is None:
                                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                                backup = lib.path + '.agent-' + stamp + '.bak'
                                lib.backup(backup)
                            value = _write(lib, op, plan)
                        else:
                            value = {**plan[0], 'applied': False}
                        results.append({'ok': True, 'data': value})
                    except Exception as exc:
                        if command != 'apply':
                            return _failure(exc)
                        results.append(_failure(exc)[0])
                if command != 'apply':
                    if not results[0]['ok']:
                        return results[0], 1
                    data = {**results[0]['data'], 'backup': backup}
                else:
                    data = {'results': results, 'backup': backup, 'applied': applying}
                    if any(not r['ok'] for r in results):
                        return {'ok': False, 'data': data, 'error': {'code': 'partial_failure', 'message': '部分操作失败，见 results'}}, 1
            return {'ok': True, 'data': data}, 0
    except Exception as exc:
        return _failure(exc)
