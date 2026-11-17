import sys
from galpanel2.agent.__main__ import main

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    raise SystemExit(main())
