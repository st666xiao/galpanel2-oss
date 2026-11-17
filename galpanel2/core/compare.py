"""Compare typed legacy fields by copy path identity, without recommending a winner."""
import json
from .domain import CompareReport
from .store import open_library


def _projection(lib):
    with lib.connection() as c:
        fields = {}
        for r in c.execute("SELECT work_id,column_name,value_json FROM legacy_fields"):
            if r[1] not in ("id", "source_id"):
                fields.setdefault(r[0], {})[r[1]] = json.loads(r[2])
        return {r[0]: (r[1], fields.get(r[1], {}), r[2]) for r in c.execute("SELECT path_key,work_id,origin FROM copies")}


def compare_libraries(path_a: str, path_b: str) -> CompareReport:
    with open_library(path_a, readonly=True) as a, open_library(path_b, readonly=True) as b:
        pa, pb = _projection(a), _projection(b)
    common = set(pa) & set(pb)
    changed = {}
    for key in sorted(common):
        wa, fa, oa = pa[key]
        wb, fb, ob = pb[key]
        if oa == ob == "legacy":
            for field in set(fa) | set(fb):
                if fa.get(field) != fb.get(field):
                    changed.setdefault(field, set()).add((wa, wb))
    return CompareReport(tuple(sorted(set(pa) - set(pb))), tuple(sorted(set(pb) - set(pa))), len(common),
                         {k: len(v) for k, v in sorted(changed.items())})
