"""Legacy MCP lifecycle over newline-delimited JSON-RPC 2.0 stdio."""
import argparse
from copy import deepcopy
import json
import sys

from galpanel2 import __version__
from galpanel2.agent import api
from galpanel2.agent.schema import SCHEMAS, DESCRIPTIONS

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')


class Server:
    def __init__(self, library=None, lock_library=False):
        self.library = library
        self.lock_library = lock_library
        self.initialized = False
        self.ready = False

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return self.error(message.get('id') if isinstance(message, dict) else None, -32600, 'Invalid Request')
        mid, method = message.get('id'), message['method']
        params = message.get('params', {})
        notification = 'id' not in message
        if not isinstance(params, dict):
            return None if notification else self.error(mid, -32602, 'Invalid params')
        if notification:
            if method == 'notifications/initialized' and self.initialized:
                self.ready = True
            return None
        if method == 'initialize':
            if not isinstance(params.get('protocolVersion'), str) or not isinstance(params.get('capabilities'), dict) or not isinstance(params.get('clientInfo'), dict):
                return self.error(mid, -32602, 'initialize requires protocolVersion, capabilities and clientInfo')
            self.initialized = True
            self.ready = False
            requested = params['protocolVersion']
            result = {'protocolVersion': requested if requested in VERSIONS else VERSIONS[0],
                      'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'galpanel2', 'version': __version__}}
        elif method == 'ping':
            result = {}
        elif not self.ready:
            return self.error(mid, -32002, 'Initialize and send notifications/initialized first')
        elif method == 'tools/list':
            tools = []
            for name, schema in SCHEMAS.items():
                spec = deepcopy(schema)
                if not self.lock_library:
                    spec['properties']['library'] = {'type': 'string', 'description': '可选库路径，覆盖启动参数或环境变量'}
                tools.append({'name': name, 'description': DESCRIPTIONS[name], 'inputSchema': spec,
                              'annotations': {'readOnlyHint': name not in api.WRITES,
                                              'destructiveHint': name in api.WRITES,
                                              'openWorldHint': name in ('search', 'fetch', 'bind-plan', 'bind', 'apply')}})
            result = {'tools': tools}
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            if not isinstance(name, str) or name not in SCHEMAS or not isinstance(args, dict):
                return self.error(mid, -32602, 'Unknown tool or invalid arguments')
            args = dict(args)
            if self.lock_library and 'library' in args:
                envelope, _ = api._failure(api.AgentError('library_override_forbidden', '服务已锁定库路径，tool 不允许传入 library'))
                return {'jsonrpc': '2.0', 'id': mid, 'result': {
                    'content': [{'type': 'text', 'text': json.dumps(envelope, ensure_ascii=False)}], 'isError': True}}
            library = args.pop('library', self.library)
            if library is not None and not isinstance(library, str):
                return self.error(mid, -32602, 'library must be text')
            envelope, _ = api.execute(name, library=library, **args)
            result = {'content': [{'type': 'text', 'text': json.dumps(envelope, ensure_ascii=False)}],
                      'isError': not envelope['ok']}
        else:
            return self.error(mid, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': mid, 'result': result}

    @staticmethod
    def error(mid, code, message):
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--library')
    parser.add_argument('--lock-library', action='store_true', help='禁止 tool 覆盖库路径')
    args = parser.parse_args(argv)
    server = Server(args.library, lock_library=args.lock_library)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
        sys.stdin.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = server.handle(message)
        except ValueError:
            response = server.error(None, -32700, 'Parse error')
        except Exception:
            print('MCP request failed', file=sys.stderr)
            response = server.error(None, -32603, 'Internal error')
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
