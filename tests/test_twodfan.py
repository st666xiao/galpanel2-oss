from pathlib import Path
import http.server
import io
import threading

import pytest
from galpanel2.core import twodfan as T
from galpanel2.core.saves import SaveError

FIX = Path(__file__).parent / 'fixtures/saves'


def html(name):
    return (FIX / name).read_text(encoding='utf-8')


def test_urls():
    assert T.search_url('年上彼女') == 'https://2dfan.com/subjects/search?keyword=%E5%B9%B4%E4%B8%8A%E5%BD%BC%E5%A5%B3'
    assert T.subject_saves_url(1).endswith('/subjects/1/downloads/cg_save')
    assert T.download_url(2).endswith('/downloads/2')


def test_search_cards():
    subjects = T.parse_search(html('2dfan_search.html'))
    assert [s.id for s in subjects] == [18977, 23484, 7955]
    assert subjects[0] == T.Subject(18977, '年上彼女', 'あざらしそふと', '2024-11-29', (30930,))
    assert subjects[1].save_ids == (44102,) and subjects[2].brand is None


def test_downloads_only_saves():
    items = T.parse_downloads(html('2dfan_subject_downloads.html'))
    assert items == [T.DownloadItem(30930, '年上彼女全CG存档', '存档', '18.6 KB', '来自誠也の部屋')]
    assert len(T.parse_downloads(html('2dfan_subject_downloads.html'), False)) == 3


def test_download_detail():
    detail = T.parse_download(html('2dfan_download_30930.html'))
    assert detail.title == '年上彼女全CG存档' and detail.kind == '存档'
    assert detail.direct_url.startswith('https://d1.acgxp.com/uploads/30930%2F')
    assert '&amp;' not in detail.direct_url and '来自誠也の部屋' in detail.description


def test_description_location():
    detail = T.parse_download(html('2dfan_download_40001.html'))
    assert 'LocalLow' in detail.description and '\n' in detail.description


def test_challenge():
    assert T.is_challenge(html('2dfan_cloudflare.html'))
    assert not T.is_challenge(html('2dfan_search.html'))


def test_pick_subject_exact_and_nfkc():
    subjects = T.parse_search(html('2dfan_search.html'))
    assert T.pick_subject(subjects, ['年上彼女']).id == 18977
    assert T.pick_subject(subjects, ['年上彼女2']).id == 23484
    assert T.pick_subject(subjects, ['年上彼女のあまやか 同棲']).id == 7955
    assert T.pick_subject(subjects[1:], ['年上彼女']) is None
    assert T.pick_subject(subjects, ['年上']) is None
    assert T.pick_subject(subjects, ['「年上彼女！」']).id == 18977


def test_changed_html_returns_empty():
    for text in ['', '<p>改版了</p>', '<div class="subject-card"><h3>']:
        assert T.parse_search(text) == [] and T.parse_downloads(text) == []
        assert T.parse_download(text).direct_url is None


class Response(io.BytesIO):
    def __init__(self, body, content_type='application/zip', length=None):
        super().__init__(body)
        self.headers = {'Content-Type': content_type}
        if length is not None:
            self.headers['Content-Length'] = str(length)


def test_fetch_file_headers_name_and_success(tmp_path, monkeypatch):
    seen = []
    def open_file(request, **kwargs):
        seen.append((dict(request.header_items()), kwargs))
        return Response(b'zip')
    monkeypatch.setattr(T, 'urlopen', open_file)
    path = T.fetch_file('https://example.test/uploads/123%2Fabc.zip?st=1', str(tmp_path))
    assert Path(path).name == 'abc.zip' and Path(path).read_bytes() == b'zip'
    headers = {k.lower(): v for k, v in seen[0][0].items()}
    assert headers['referer'] == 'https://2dfan.com/' and 'Mozilla' in headers['user-agent']
    assert not list(tmp_path.glob('*.partial'))


def test_fetch_file_size_limits_and_cleanup(tmp_path, monkeypatch):
    for length in (999, None):
        monkeypatch.setattr(T, 'urlopen', lambda *a, **kw: Response(b'large file', length=length))
        with pytest.raises(SaveError, match='大小上限'):
            T.fetch_file('https://example.test/a.zip', str(tmp_path), max_bytes=3)
        assert list(tmp_path.iterdir()) == []


def test_fetch_file_rejects_html_without_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(T, 'urlopen', lambda *a, **kw: Response(b'<html>challenge</html>', 'text/html; charset=utf-8'))
    with pytest.raises(SaveError, match='网页'):
        T.fetch_file('https://example.test/a.zip', str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_fetch_file_local_http_server(tmp_path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html>login</html>')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(SaveError, match='网页'):
            T.fetch_file(f'http://127.0.0.1:{server.server_port}/test.zip', str(tmp_path))
        assert list(tmp_path.iterdir()) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('content_type', ['text/plain', 'TEXT/CSS; charset=UTF-8', 'text/csv',
                                         'text/xml', 'application/json; charset=utf-8'])
def test_fetch_file_rejects_text_and_json_preserving_existing_file(tmp_path, monkeypatch, content_type):
    target = tmp_path / 'save.zip'
    target.write_bytes(b'previous download')
    monkeypatch.setattr(T, 'urlopen', lambda *a, **kw: Response(b'error response', content_type))
    with pytest.raises(SaveError, match='网页或文本'):
        T.fetch_file('https://origin.test/save.zip', str(tmp_path))
    assert target.read_bytes() == b'previous download'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['save.zip']


@pytest.mark.parametrize('content_type,body,limit,error', [
    ('application/zip', b'zip', 10, None),
    ('text/plain', b'error', 10, '网页或文本'),
    ('application/json', b'{}', 10, '网页或文本'),
    ('application/zip', b'large archive', 3, '大小上限'),
])
def test_fetch_cross_host_redirect_checks_final_response(tmp_path, monkeypatch, content_type, body, limit, error):
    from email.message import Message
    from urllib.request import HTTPHandler, build_opener
    from urllib.response import addinfourl
    seen = []
    class Handler(HTTPHandler):
        def http_open(self, request):
            seen.append(request.full_url)
            headers = Message()
            if len(seen) == 1:
                headers['Location'] = 'http://cdn.test/signed.zip?signature=secret'
                status, data = 302, b''
            else:
                headers['Content-Type'] = content_type
                status, data = 200, body
            response = addinfourl(io.BytesIO(data), headers, request.full_url, status)
            response.msg = 'Found' if status == 302 else 'OK'
            return response
    opener = build_opener(Handler())
    monkeypatch.setattr(T, 'urlopen', opener.open)
    url = 'http://origin.test/save.zip'
    if error:
        with pytest.raises(SaveError, match=error):
            T.fetch_file(url, str(tmp_path), max_bytes=limit)
        assert list(tmp_path.iterdir()) == []
    else:
        assert Path(T.fetch_file(url, str(tmp_path), max_bytes=limit)).read_bytes() == body
    assert seen == [url, 'http://cdn.test/signed.zip?signature=secret']
