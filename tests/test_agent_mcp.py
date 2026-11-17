import json
import subprocess
import sys

from galpanel2.mcp.__main__ import Server
from test_agent_cli import agent_library


def request(server, mid, method, **params):
    return server.handle({'jsonrpc': '2.0', 'id': mid, 'method': method, 'params': params})


def handshake(server):
    result = request(server, 1, 'initialize', protocolVersion='2025-06-18', capabilities={}, clientInfo={'name': 'test', 'version': '1'})
    assert result['result']['protocolVersion'] == '2025-06-18'
    assert server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None


def test_tools_and_lifecycle(agent_library):
    server = Server(agent_library)
    assert request(server, 0, 'tools/list')['error']['code'] == -32002
    assert request(server, 0, 'ping')['result'] == {}
    handshake(server)
    tools = request(server, 2, 'tools/list')['result']['tools']
    assert len(tools) == 12
    for tool in tools:
        assert 'library' in tool['inputSchema']['properties']
        if tool['name'] in ('bind', 'unbind', 'set', 'summary', 'apply'):
            assert tool['inputSchema']['properties']['apply']['default'] is False
    reply = request(server, 3, 'tools/call', name='list', arguments={'unbound': True})['result']
    assert not reply['isError'] and json.loads(reply['content'][0]['text'])['data']['total'] == 2
    reply = request(server, 4, 'tools/call', name='set', arguments={'work_id': 1, 'category': '日系'})['result']
    assert not json.loads(reply['content'][0]['text'])['data']['applied']
    reply = request(server, 5, 'tools/call', name='bind', arguments={'work_id': 1, 'ref': 'v17'})['result']
    assert reply['isError']
    assert request(server, 6, 'unknown')['error']['code'] == -32601
    assert request(server, 7, 'tools/call', name='unknown')['error']['code'] == -32602
    assert server.handle([])['error']['code'] == -32600


def test_mcp_subprocess_only_protocol_stdout(agent_library):
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'show', 'arguments': {'work_id': 1}}},
    ]
    proc = subprocess.run([sys.executable, '-m', 'galpanel2.mcp', '--library', agent_library],
                          input='\n \t\n' + '\n\n'.join(json.dumps(m) for m in messages) + '\n{broken\n', capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0 and not proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == 4
    assert replies[2]['id'] == 3 and not replies[2]['result']['isError']
    assert replies[3]['error']['code'] == -32700


def test_mcp_write_apply_and_revision_error(agent_library):
    server = Server()
    handshake(server)
    arguments = {'library': agent_library, 'work_id': 1, 'revision': 0, 'category': '日系', 'apply': True}
    result = request(server, 1, 'tools/call', name='set', arguments=arguments)['result']
    assert not result['isError']
    assert json.loads(result['content'][0]['text'])['data']['revision'] == 1
    result = request(server, 2, 'tools/call', name='set', arguments=arguments)['result']
    assert result['isError']
    assert json.loads(result['content'][0]['text'])['error']['code'] == 'revision_conflict'


def test_locked_library_rejects_override(agent_library, monkeypatch):
    from galpanel2.agent import api
    server = Server(agent_library, lock_library=True)
    handshake(server)
    tools = request(server, 2, 'tools/list')['result']['tools']
    assert all('library' not in tool['inputSchema']['properties'] for tool in tools)
    assert not request(server, 3, 'tools/call', name='list')['result']['isError']
    def forbidden(*a, **kw):
        raise AssertionError('rejected library must not reach API')
    monkeypatch.setattr(api, 'execute', forbidden)
    for library in (agent_library, '/other.galdb', None):
        result = request(server, 4, 'tools/call', name='list', arguments={'library': library})['result']
        assert result['isError']
        assert json.loads(result['content'][0]['text'])['error']['code'] == 'library_override_forbidden'


def test_locked_library_startup_argument(agent_library):
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list', 'arguments': {'library': agent_library}}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'list'}},
    ]
    proc = subprocess.run([sys.executable, '-m', 'galpanel2.mcp', '--library', agent_library, '--lock-library'],
                          input=('\n'.join(json.dumps(m) for m in messages) + '\n').encode(), capture_output=True)
    assert proc.returncode == 0 and not proc.stderr
    assert b'\r\n' not in proc.stdout
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies) == 3
    assert replies[1]['result']['isError'] and not replies[2]['result']['isError']


def test_stdio_ignores_blank_lines_and_forces_lf(monkeypatch):
    import io
    from galpanel2.mcp.__main__ import main
    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding='utf-8', newline='\r\n')
    stdin = io.TextIOWrapper(io.BytesIO(b'\n \t\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n\n'))
    monkeypatch.setattr(sys, 'stdout', stdout)
    monkeypatch.setattr(sys, 'stdin', stdin)
    assert main([]) == 0
    stdout.flush()
    raw = buffer.getvalue()
    assert b'\r' not in raw and len(raw.splitlines()) == 1
    assert json.loads(raw)['result'] == {}
