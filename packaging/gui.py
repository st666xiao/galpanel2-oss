"""PyInstaller GUI entry; absolute imports work outside the package."""
from galpanel2.__main__ import main

if __name__ == '__main__':
    raise SystemExit(main())
