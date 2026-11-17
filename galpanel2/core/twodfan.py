"""2DFan HTML parsing; only signed file downloads use HTTP here."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit, urljoin
from urllib.request import Request, urlopen

from .saves import SaveError

BASE = 'https://2dfan.com'


@dataclass(frozen=True)
class Subject:
    id: int
    title: str
    brand: str | None
    release: str | None
    save_ids: tuple[int, ...]


@dataclass(frozen=True)
class DownloadItem:
    id: int
    title: str
    kind: str
    size: str
    excerpt: str


@dataclass(frozen=True)
class DownloadDetail:
    title: str
    kind: str
    direct_url: str | None
    description: str


def search_url(keyword: str) -> str:
    return BASE + '/subjects/search?keyword=' + quote(keyword)


def subject_saves_url(subject_id: int) -> str:
    return BASE + f'/subjects/{subject_id}/downloads/cg_save'


def download_url(download_id: int) -> str:
    return BASE + f'/downloads/{download_id}'


def is_challenge(html: str) -> bool:
    low = html.casefold()
    return 'just a moment...' in low or 'performing security verification' in low or 'cf-chl-' in low


@dataclass
class _Node:
    tag: str = ''
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def all(self, cls=None, tag=None):
        for child in self.children:
            if isinstance(child, _Node):
                if (cls is None or cls in child.attrs.get('class', '').split()) and (tag is None or child.tag == tag):
                    yield child
                yield from child.all(cls, tag)

    def text(self):
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                value = child.text()
                parts.append(('\n' if child.tag in ('p', 'div', 'br', 'li') else '') + value)
        return ''.join(parts).strip()

    def first(self, cls=None, tag=None):
        return next(self.all(cls, tag), _Node())


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _tree(html):
    parser = _Parser()
    try:
        parser.feed(html)
    except (ValueError, AssertionError):
        pass
    return parser.root


def _id(node, kind):
    match = re.fullmatch(r'/' + kind + r'/(\d+)', node.attrs.get('href', ''))
    return int(match[1]) if match else None


def _flat(node):
    return ' '.join(node.text().split())


def parse_search(html: str) -> list[Subject]:
    result = []
    for card in _tree(html).all('subject-card'):
        title = card.first('subject-card-title').first(tag='a')
        ident = _id(title, 'subjects')
        if ident is None or not _flat(title):
            continue
        brand, release = None, None
        for item in card.all('subject-card-meta-item'):
            label = item.first('subject-card-meta-label').text()
            value = _flat(item.first('subject-card-meta-value'))
            if '品牌' in label:
                brand = value or None
            elif '发售' in label:
                release = value or None
        saves = tuple(dict.fromkeys(n for a in card.all(tag='a') if _flat(a) == '存档' and (n := _id(a, 'downloads')) is not None))
        result.append(Subject(ident, _flat(title), brand, release, saves))
    return result


def parse_downloads(html: str, only_saves: bool = True) -> list[DownloadItem]:
    result = []
    for card in _tree(html).all('download-card'):
        title = card.first('download-card-title').first(tag='a')
        ident = _id(title, 'downloads')
        kind = _flat(card.first('download-card-kind'))
        if ident is None or not _flat(title) or (only_saves and kind != '存档'):
            continue
        result.append(DownloadItem(ident, _flat(title), kind, _flat(card.first('download-card-size')), _flat(card.first('download-card-excerpt'))))
    return result


def parse_download(html: str) -> DownloadDetail:
    root = _tree(html)
    kind = ''
    for item in root.all('download-info-item'):
        if '分类' in item.first('download-info-label').text():
            kind = _flat(item.first('download-info-value'))
            break
    direct = None
    for node in root.all('download-btn', 'a'):
        href = node.attrs.get('href')
        if '直连下载' in node.text() and href:
            url = urljoin(BASE, href)
            if urlsplit(url).scheme in ('http', 'https'):
                direct = url
                break
    return DownloadDetail(_flat(root.first('download-title')), kind, direct, root.first('download-description-content').text())


def _normalized(title):
    return ''.join(c for c in unicodedata.normalize('NFKC', title).casefold() if not c.isspace() and not unicodedata.category(c).startswith(('P', 'Z')) and c not in '~～')


def pick_subject(subjects: list[Subject], titles: list[str]) -> Subject | None:
    wanted = {_normalized(t) for t in titles if _normalized(t)}
    return next((s for s in subjects if _normalized(s.title) in wanted), None)


def fetch_file(url: str, dest_path: str, *, max_bytes: int = 300 << 20, timeout: int = 60) -> str:
    name = unquote(urlsplit(url).path).rsplit('/', 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).rstrip(' .')
    if not name or name in ('.', '..'):
        name = 'download.bin'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        name = '_' + name
    dest = Path(dest_path) / name
    partial = dest.with_name(name + '.partial')
    request = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Referer': BASE + '/',
    })
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type.startswith('text/') or content_type in ('application/json', 'application/xhtml+xml'):
                raise SaveError('下载返回了网页或文本数据，可能需要登录或重新获取下载链接')
            length = response.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                raise SaveError('下载文件超过大小上限')
            total = 0
            with partial.open('wb') as f:
                while chunk := response.read(64 << 10):
                    total += len(chunk)
                    if total > max_bytes:
                        raise SaveError('下载文件超过大小上限')
                    f.write(chunk)
        os.replace(partial, dest)
        return str(dest)
    except Exception as error:
        partial.unlink(missing_ok=True)
        if isinstance(error, SaveError):
            raise
        raise SaveError(f'下载失败：{error}') from error
