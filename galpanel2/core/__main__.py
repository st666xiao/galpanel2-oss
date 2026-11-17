"""JSON-only CLI. Importing this module does not perform any operations."""
import argparse
from dataclasses import asdict, is_dataclass
import json
import sys

from .legacy import inspect_legacy, import_legacy, verify_import
from .compare import compare_libraries
from .store import upgrade_library, open_library
from .play import playtime, list_playtimes
from . import meta, records, sites, binding, tidy
from .repository import get_work


def _id_list(text):
    return [int(part) for part in str(text).split(",") if part.strip()]


def _revisions(lib, ids):
    with lib.connection() as c:
        found = {}
        for wid in ids:
            row = c.execute("SELECT revision FROM works WHERE id=?", (wid,)).fetchone()
            if row is None:
                raise KeyError(wid)
            found[wid] = row[0]
        return found


def _tidy(args):
    if args.tidy_command == "report":
        with open_library(args.library, readonly=True) as lib:
            with lib.connection() as c:
                works = c.execute("SELECT COUNT(*) FROM works").fetchone()[0]
            return {"schema_version": lib.schema_version, "works": works,
                    "missing_groups": tidy.missing_groups(lib), "suspects": tidy.suspect_report(lib),
                    "duplicates": tidy.duplicate_report(lib), "titles": tidy.title_report(lib),
                    "legacy": tidy.legacy_report(lib), "ignored": tidy.list_ignored(lib)}, 0
    with open_library(args.library) as lib:
        if args.tidy_command == "remove":
            ids = _id_list(args.ids)
            return tidy.remove_works(lib, _revisions(lib, ids), ignore=not args.no_ignore), 0
        if args.tidy_command == "remove-missing":
            try:
                return tidy.remove_missing_group(lib, args.directory), 0
            except KeyError:
                return {"error": "no such missing group"}, 2
            except ValueError as exc:
                if str(exc).startswith("drive offline"):
                    return {"error": "drive offline"}, 2
                raise
        if args.tidy_command == "merge":
            others = _revisions(lib, _id_list(args.others))
            keep = _revisions(lib, [args.keep])[args.keep]
            return tidy.merge_works(lib, args.keep, keep, others), 0
        if args.tidy_command == "retitle":
            if args.suggested is not None:
                report = tidy.title_report(lib)
                if args.suggested != "all":
                    wanted = set(_id_list(args.suggested))
                    report = [row for row in report if row["work_id"] in wanted]
                titles = {row["work_id"]: (row["revision"], row["suggested"]) for row in report}
            else:
                pairs = []
                for pair in args.pairs:
                    wid, title = pair.split("=", 1)
                    pairs.append((int(wid), title))
                revs = _revisions(lib, [wid for wid, _ in pairs])
                titles = {wid: (revs[wid], title) for wid, title in pairs}
            return {"changed": tidy.retitle(lib, titles)}, 0
        if args.tidy_command == "ignore":
            if args.ignore_command == "add":
                tidy.add_ignored(lib, args.path, "tree" if args.tree else "exact", "user")
                return None, 0
            if args.ignore_command == "remove":
                return tidy.remove_ignored(lib, args.path), 0
            return tidy.list_ignored(lib), 0
        if args.tidy_command == "legacy-summary":
            applied, skipped = [], []
            for wid in _id_list(args.ids):
                (applied if tidy.use_legacy_summary(lib, wid) else skipped).append(wid)
            return {"applied": applied, "skipped": skipped}, 0
        return tidy.list_log(lib), 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect").add_argument("old")
    for name in ("import", "verify"):
        p = sub.add_parser(name)
        p.add_argument("old")
        p.add_argument("target")
    p = sub.add_parser("compare")
    p.add_argument("a")
    p.add_argument("b")
    sub.add_parser("upgrade").add_argument("library")
    sub.add_parser("meta").add_argument("library")
    p = sub.add_parser("info")
    p.add_argument("library")
    p.add_argument("work_id", type=int)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--sites", default=",".join(records.SITES))
    sub.add_parser("fetch").add_argument("ref")
    p = sub.add_parser("bind")
    p.add_argument("library")
    p.add_argument("work_id", type=int)
    p.add_argument("ref")
    p.add_argument("--title", choices=("original", "translated"))
    p.add_argument("--no-cover", action="store_true")
    p = sub.add_parser("playtime")
    p.add_argument("library")
    p.add_argument("--work", type=int)
    tidy_p = sub.add_parser("tidy")
    tidy_p.add_argument("library")
    tidy_sub = tidy_p.add_subparsers(dest="tidy_command", required=True)
    tidy_sub.add_parser("report")
    p = tidy_sub.add_parser("remove")
    p.add_argument("ids")
    p.add_argument("--no-ignore", action="store_true")
    p = tidy_sub.add_parser("remove-missing")
    p.add_argument("directory")
    p = tidy_sub.add_parser("merge")
    p.add_argument("keep", type=int)
    p.add_argument("others")
    p = tidy_sub.add_parser("retitle")
    p.add_argument("pairs", nargs="*")
    p.add_argument("--suggested")
    ignore = tidy_sub.add_parser("ignore")
    ignore_sub = ignore.add_subparsers(dest="ignore_command", required=True)
    p = ignore_sub.add_parser("add")
    p.add_argument("path")
    p.add_argument("--tree", action="store_true")
    p = ignore_sub.add_parser("remove")
    p.add_argument("path")
    ignore_sub.add_parser("list")
    p = tidy_sub.add_parser("legacy-summary")
    p.add_argument("ids")
    tidy_sub.add_parser("log")
    args = ap.parse_args(argv)
    try:
        if args.command == "info":
            with open_library(args.library, readonly=True) as lib:
                result, code = binding.get_info(lib, args.work_id), 0
        elif args.command in ("search", "fetch", "bind"):
            client = sites.SiteClient(sites.default_transport(), token=sites.read_token())
            if args.command == "search":
                selected = tuple(args.sites.split(","))
                if any(s not in records.SITES for s in selected):
                    raise ValueError("Unknown site")
                candidates, errors = client.search_all(args.query, selected)
                result = {"candidates": [dict(site=c.record.site, site_id=c.record.site_id,
                          title=c.record.title, translated_title=c.record.translated_title,
                          brand=c.record.brands[0].name if c.record.brands else "",
                          date=c.record.release_date, grade=c.grade, score=c.score,
                          complete=c.record.complete) for c in candidates], "errors": errors}
            else:
                ref = records.parse_ref(args.ref)
                if ref is None:
                    raise ValueError("Unrecognized reference")
                if args.command == "fetch":
                    result = asdict(client.fetch(ref))
                    result.pop("payload")
                else:
                    with open_library(args.library) as lib:
                        record = client.fetch(ref)
                        work = get_work(lib, args.work_id)
                        plan = binding.plan_bind(lib, args.work_id, record)
                        title = None
                        if args.title:
                            title = record.title if args.title == "original" else record.translated_title
                            if title not in plan.title_options:
                                raise ValueError("Title is not an option")
                        cover, cover_error = None, None
                        if plan.cover == "set" and not args.no_cover:
                            cover, cover_error = binding.download_cover(client, record)
                        revision = binding.apply_bind(lib, args.work_id, work.revision, record, title, cover)
                        result = dict(work_id=args.work_id, site=record.site, site_id=record.site_id,
                                      revision=revision, plan=asdict(plan), cover_error=cover_error)
            code = 0
        elif args.command == "inspect":
            result, code = inspect_legacy(args.old), 0
        elif args.command == "import":
            result = import_legacy(args.old, args.target)
            code = 1 if result.rejected or result.conservation_errors else 0
        elif args.command == "verify":
            result = verify_import(args.old, args.target)
            code = int(bool(result))
        elif args.command == "upgrade":
            result, code = upgrade_library(args.library), 0
        elif args.command == "meta":
            with open_library(args.library, readonly=True) as lib:
                result = {"schema_version": lib.schema_version,
                          "groups": [asdict(g) for g in meta.list_tag_groups(lib)],
                          "brands": [asdict(b) for b in meta.list_brands(lib)],
                          "tags": [asdict(t) for t in meta.list_tags(lib)],
                          "works": [asdict(m) for _, m in sorted(meta.list_work_meta(lib).items())]}
                code = 0
        elif args.command == "playtime":
            with open_library(args.library, readonly=True) as lib:
                times = [playtime(lib, args.work)] if args.work is not None else list(list_playtimes(lib).values())
                result, code = [asdict(t) for t in times], 0
        elif args.command == "tidy":
            result, code = _tidy(args)
        else:
            result, code = compare_libraries(args.a, args.b), 0
        print(json.dumps(asdict(result) if is_dataclass(result) else result, ensure_ascii=False))
        return code
    except Exception as e:
        print(json.dumps({"error": type(e).__name__, "message": str(e)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
