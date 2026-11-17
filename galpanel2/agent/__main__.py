"""One JSON object on stdout for every invocation, including usage errors."""
import argparse
import json
import sys

from . import api
from .schema import SCHEMAS


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    try:
        parser = Parser(description='GalPanel2 JSON agent CLI', add_help=False)
        parser.add_argument('--library', default=argparse.SUPPRESS)
        sub = parser.add_subparsers(dest='command', required=True)
        for command, schema in SCHEMAS.items():
            p = sub.add_parser(command, add_help=False)
            # Accept --library both before and after the command.
            p.add_argument('--library', default=argparse.SUPPRESS)
            for key, spec in schema['properties'].items():
                if key == 'operations':
                    continue
                positional = key == 'work_id' or (key == 'ref' and command in ('fetch', 'bind', 'bind-plan'))
                flag = key if positional else '--' + key.replace('_', '-')
                options = {} if positional else {'default': argparse.SUPPRESS}
                if spec['type'] == 'boolean':
                    options['action'] = 'store_true'
                else:
                    options['type'] = int if spec['type'] == 'integer' else str
                    if not positional and key in schema.get('required', []):
                        options['required'] = True
                p.add_argument(flag, **options)
        args = vars(parser.parse_args(argv))
        command = args.pop('command')
        library = args.pop('library', None)
        for key in ('category', 'status'):
            if args.get(key) == 'none':
                args[key] = None
        result, code = api.execute(command, library=library, **args)
    except (ValueError, TypeError) as exc:
        result, code = api._failure(ValueError(str(exc)))
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    raise SystemExit(main())
