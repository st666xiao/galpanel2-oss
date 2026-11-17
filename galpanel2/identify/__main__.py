"""命令行入口：python -m galpanel2.identify {hints,run,report}。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import ai, hints, report, search
from .pipeline import run as run_pipeline
from .pipeline import _read_existing


def _cmd_hints(args) -> int:
    items: list[dict] = []
    if args.library:
        items = hints.collect_library(args.library, only_unbound=not args.all)
    elif args.folders:
        with open(args.folders, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                path = line.strip()
                if not path:
                    continue
                items.append(hints.collect_folder(str(lineno), path))
    else:
        print("hints 需要 --library 或 --folders", file=sys.stderr)
        return 2

    parent = os.path.dirname(os.path.abspath(args.out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"写入 {len(items)} 条线索：{args.out}", file=sys.stderr)
    return 0


def _load_hints(path: str) -> list[dict]:
    items: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                items.append(item)
    return items


def _cmd_run(args) -> int:
    items = _load_hints(args.hints)
    os.makedirs(args.work, exist_ok=True)
    config = ai.load_config(args.ai_config)
    searcher = search.Searcher(cache_dir=os.path.join(args.work, "search-cache"))

    def progress(done: int, total: int, name: str) -> None:
        print(f"[{done}/{total}] {name}", file=sys.stderr, flush=True)

    results = run_pipeline(items, args.work, config, searcher=searcher,
                           jobs=max(1, args.jobs), limit=args.limit, progress=progress)
    data = report.write_report(results, args.work)
    print(f"共 {len(results)} 部，identified {len(data['identified'])} 部；"
          f"结果在 {args.work}", file=sys.stderr)
    return 0


def _cmd_report(args) -> int:
    rows = _read_existing(os.path.join(args.work, "results.jsonl"))
    data = report.write_report(list(rows.values()), args.work)
    print(f"共 {len(rows)} 部，identified {len(data['identified'])} 部；"
          f"报告在 {args.work}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m galpanel2.identify",
                                     description="AI 识别流水线")
    sub = parser.add_subparsers(dest="cmd")

    p_hints = sub.add_parser("hints", help="收集线索并写成 JSONL")
    p_hints.add_argument("--library", help="现有 .galdb 路径（只读）")
    p_hints.add_argument("--all", action="store_true", help="包含已绑定的作品")
    p_hints.add_argument("--folders", help="文本文件，每行一个游戏文件夹 / 压缩包路径")
    p_hints.add_argument("--out", required=True, help="输出 hints.jsonl")
    p_hints.set_defaults(func=_cmd_hints)

    p_run = sub.add_parser("run", help="跑识别流水线并自动出报告")
    p_run.add_argument("--hints", required=True)
    p_run.add_argument("--work", required=True)
    p_run.add_argument("--ai-config")
    p_run.add_argument("--jobs", type=int, default=1)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.set_defaults(func=_cmd_run)

    p_report = sub.add_parser("report", help="根据 results.jsonl 重新生成报告")
    p_report.add_argument("--work", required=True)
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ai.AiError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
