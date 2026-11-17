"""CLI / MCP shared parameter contract."""
from galpanel2.core import shelf, records


def field(typ, description, **extra):
    return {'type': typ, 'description': description, **extra}


WID = field('integer', '作品 ID，从 list 获取', minimum=1)
REV = field('integer', 'show 或 dry-run 返回的 revision，写入前检测冲突', minimum=0)
REF = field('string', '资料引用，如 vndb:v17、dlsite:RJ123456 或公开条目 URL')
SITE = field('string', '资料站点', enum=list(records.SITES))
APPLY = field('boolean', '默认 false 仅预览；true 备份并写库，需关闭 GUI', default=False)
SCHEMAS = {}
DESCRIPTIONS = {
    'status': '先了解库版本、作品和拷贝数、目录、分类和状态计数；只读，不联网。',
    'list': '列出及筛选作品，先用 unbound 找待绑定作品；只读，不联网。',
    'show': '检查完整资料、拷贝存在状态和 revision；只读，不联网。',
    'hints': '读取拷贝目录的文件名、exe、readme 和编号；只读文件，不联网，不执行文件。',
    'search': '显式联网搜索资料候选；按 work 使用标题和本地线索，候选不能自动视为正确绑定。',
    'fetch': '显式联网获取 ref 的详细资料，检查品牌、日期和作品版本。',
    'bind-plan': '显式联网取资料并预览绑定变化及当前 revision；不下载封面、不写库。',
    'bind': '先 dry-run 检查变化，apply=true 才备份、下载封面并绑定；必须提供 revision。',
    'unbind': '先预览解除站点绑定；apply=true 备份并解绑，不删除游戏或已有其他元数据。',
    'set': '先预览分类或状态变化；apply=true 备份并写入，null 清空。',
    'summary': '先预览自定义简介；apply=true 备份并写入，用户简介阻止后续站点覆盖。',
    'apply': '操作数组先全部校验再逐项执行；apply=true 一次备份，逐项事务，失败项不阻碍其他项。',
}


def add(name, properties=None, required=()):
    SCHEMAS[name] = {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False}


add('status')
add('list', {'unbound': field('boolean', '只列没有站点绑定的作品'),
             'category': field(['string', 'null'], '分类筛选，null 表示未分类', enum=[*shelf.CATEGORIES, None]),
             'status': field(['string', 'null'], '状态筛选，null 表示未设置', enum=[*shelf.STATUSES, None]),
             'folder': field('string', '拷贝路径包含的文字'), 'query': field('string', '标题、品牌模糊查询'),
             'limit': field('integer', '最多返回数，默认 100', minimum=0),
             'offset': field('integer', '跳过数，默认 0', minimum=0)})
for name in ('show', 'hints'):
    add(name, {'work_id': WID}, ('work_id',))
add('search', {'site': SITE, 'query': field('string', '搜索文本'), 'work': WID})
add('fetch', {'ref': REF}, ('ref',))
add('bind-plan', {'work_id': WID, 'ref': REF}, ('work_id', 'ref'))
add('bind', {'work_id': WID, 'ref': REF, 'revision': REV, 'apply': APPLY}, ('work_id', 'ref', 'revision'))
add('unbind', {'work_id': WID, 'site': SITE, 'revision': REV, 'apply': APPLY}, ('work_id', 'site'))
add('set', {'work_id': WID, 'category': field(['string', 'null'], '分类，null 清空', enum=[*shelf.CATEGORIES, None]),
            'status': field(['string', 'null'], '游玩状态，null 清空', enum=[*shelf.STATUSES, None]),
            'revision': REV, 'apply': APPLY}, ('work_id',))
add('summary', {'work_id': WID, 'text': field('string', '简介正文，空字符串清空'), 'revision': REV, 'apply': APPLY}, ('work_id', 'text'))
add('apply', {'file': field('string', '本地 JSON 操作数组路径，和 operations 二选一'),
              'operations': field('array', '操作数组，每项 command 为 bind/unbind/set/summary，参数同对应工具'),
              'apply': APPLY})

# Batch schemas expose the same validated operation contract to MCP clients.
from copy import deepcopy
_variants = []
for _name in ('bind', 'unbind', 'set', 'summary'):
    _spec = deepcopy(SCHEMAS[_name])
    _spec['properties'].pop('apply')
    _spec['properties']['command'] = {'type': 'string', 'const': _name}
    _spec['required'].append('command')
    _variants.append(_spec)
SCHEMAS['apply']['properties']['operations']['items'] = {'oneOf': _variants}
