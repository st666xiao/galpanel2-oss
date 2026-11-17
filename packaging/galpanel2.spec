# Build with: python -m PyInstaller --clean --noconfirm packaging/galpanel2.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
# Preserve the package-relative layout used by theme.py, store.py and records.py.
data = collect_data_files('galpanel2') + collect_data_files('assets')
analyses = [Analysis([str(root / 'packaging' / entry)], pathex=[str(root)],
                     binaries=[], datas=data, hiddenimports=[], hookspath=[],
                     runtime_hooks=[], excludes=[], noarchive=False)
            for entry in ('gui.py', 'agent.py', 'mcp.py')]
executables = []
for analysis, name, console in zip(analyses,
        ('GalPanel2', 'galpanel2-agent', 'galpanel2-mcp'), (False, True, True)):
    pyz = PYZ(analysis.pure)
    executables.append(EXE(pyz, analysis.scripts, [], exclude_binaries=True,
                           name=name, debug=False, bootloader_ignore_signals=False,
                           strip=False, upx=False, console=console))
coll = COLLECT(*executables,
               *(a.binaries for a in analyses), *(a.datas for a in analyses),
               strip=False, upx=False, name='GalPanel2')
