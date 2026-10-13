from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import sys
import time
from collections import Counter

from . import config as cfgmod
from .apply import Journal, apply_plan, prune_empty_dirs, undo
from .delugeidx import DelugeIndex, DelugeRpc
from .pathmap import PathMapper
from .planner import build_plan, write_csv
from .plexsrc import load_items

log = logging.getLogger("plex_organizer")


def _connect_deluge(cfg):
    d = cfg.deluge
    if d is None or d.disabled:
        return None
    return DelugeRpc(d.host, d.port, d.username, d.password)


def _check_deluge_policy(cfg) -> None:
    if cfg.deluge is None:
        sys.exit("No deluge section configured. Set deluge.disabled: true to run without Deluge protection.")


def _filter(items, args):
    if args.library:
        items = [i for i in items if i.library in args.library]
    if args.only:
        items = [i for i in items if any(fnmatch.fnmatch(f.path, args.only) for f in i.files)]
    return items


def _plan(cfg, args, *, settle=False):
    _check_deluge_policy(cfg)
    mapper = PathMapper(cfg.path_maps)
    api = _connect_deluge(cfg)
    index = DelugeIndex.from_api(api) if api else None
    items = _filter(load_items(cfg), args)
    ops = build_plan(cfg, items, mapper, index, settle=settle)
    return mapper, api, index, ops


def _summary(ops) -> str:
    c = Counter((o.action, o.reason, "torrent" if o.owner != "free" else "free") for o in ops)
    return "\n".join(f"{n:6d}  {a:5s} {r or '-':32s} {k}" for (a, r, k), n in sorted(c.items()))


def cmd_plan(cfg, args) -> int:
    _, _, _, ops = _plan(cfg, args)
    path = os.path.join(cfg.state_dir, "plan.csv")
    write_csv(ops, path)
    print(_summary(ops))
    print(f"plan written to {path}")
    return 0


def cmd_audit(cfg, args) -> int:
    from .audit import audit, render

    findings = audit(_filter(load_items(cfg), args))
    text = render(findings)
    with open(os.path.join(cfg.state_dir, "audit.txt"), "w") as fh:
        fh.write(text + "\n")
    print(text or "no suspicious matches")
    return 0


def cmd_clean(cfg, args) -> int:
    from . import clean

    _check_deluge_policy(cfg)
    mapper = PathMapper(cfg.path_maps)
    api = _connect_deluge(cfg)
    index = DelugeIndex.from_api(api) if api else None
    items = _filter(load_items(cfg), args)
    found = clean.find(cfg, items, mapper, index)
    print(clean.summarize(found))
    for s in found:
        if s.category == "review-video":
            print("review:", s.path)
    discs = clean.plan_discs(cfg, items, found, mapper, index)
    for m in discs:
        print(f"disc: {m.root} -> {m.dest} [{m.owner if m.owner != 'free' else 'free'}] {m.reason or 'ok'}")
    if not args.yes:
        print("dry run; pass --yes to move discs, deselect (Deluge) and delete the 'delete' rows")
        return 0
    moved = clean.apply_discs(discs, mapper, index, api, Journal(os.path.join(cfg.state_dir, "journal.jsonl")))
    print(f"discs moved={moved}")
    removed, skipped = clean.apply_clean(found, mapper, index, api, os.path.join(cfg.state_dir, "deleted.log"))
    pruned = prune_empty_dirs([it.root for it in items], mapper)
    print(f"deleted={removed} empty_dirs_removed={pruned}")
    for line in skipped:
        print("skipped:", line)
    return 0


def cmd_move(cfg, args) -> int:
    from . import relocate

    _check_deluge_policy(cfg)
    mapper = PathMapper(cfg.path_maps)
    api = _connect_deluge(cfg)
    index = DelugeIndex.from_api(api) if api else None
    moves = relocate.plan_moves(args.paths, args.to, mapper, index)
    for m in moves:
        kind = f"torrents={','.join(t[:8] for t in m.torrents)}" if m.torrents else "free"
        print(f"{m.src} -> {m.dest} [{kind}] {m.reason or 'ok'}")
    if not args.yes:
        print("dry run; pass --yes to move")
        return 0
    n = relocate.apply_moves(moves, mapper, index, api, Journal(os.path.join(cfg.state_dir, "journal.jsonl")))
    print(f"moved={n}")
    if cfg.refresh_plex and n:
        _refresh_plex(cfg)
    return 0


def cmd_unpack(cfg, args) -> int:
    from . import unpack

    _check_deluge_policy(cfg)
    mapper = PathMapper(cfg.path_maps)
    api = _connect_deluge(cfg)
    index = DelugeIndex.from_api(api) if api else None
    items = _filter(load_items(cfg), args)
    sets = unpack.plan(cfg, items, mapper, index)
    for s in sets:
        print(f"{s.action or 'skip':16s} {s.reason:26s} {os.path.join(s.directory, s.main)}")
    if not args.yes:
        print("dry run; pass --yes to extract and delete archive volumes")
        return 0
    extracted, deleted, problems = unpack.apply(sets)
    print(f"extracted={extracted} archive_files_deleted={deleted}")
    for line in problems:
        print("problem:", line)
    return 1 if problems else 0


def cmd_ingest(cfg, args) -> int:
    from . import ingest

    _check_deluge_policy(cfg)
    mapper = PathMapper(cfg.path_maps)
    api = _connect_deluge(cfg)
    index = DelugeIndex.from_api(api) if api else None
    items = _filter(load_items(cfg), args)
    extra, stubs, unhandled = ingest.plan_ingest(cfg, items, mapper)
    ops = build_plan(cfg, extra, mapper, index)
    for o in ops:
        print(f"{o.action:5s} {o.reason:26s} {o.src} -> {o.dst}")
    for s in stubs:
        print(f"stub  zero-byte file: {s}")
    for u in unhandled:
        print(f"left  {u.reason}: {u.path}")
    if not args.yes:
        print("dry run; pass --yes to rename these and delete the stubs")
        return 0
    res = apply_plan(cfg, ops, mapper, index, api, Journal(os.path.join(cfg.state_dir, "journal.jsonl")))
    for sp in stubs:
        os.remove(mapper.to_local(sp))
    pruned = prune_empty_dirs([o.root for o in ops], mapper)
    print(f"moved={res.moved} torrents={res.torrents} failed={res.failed} stubs_deleted={len(stubs)} empty_dirs_removed={pruned}")
    if cfg.refresh_plex and res.moved:
        _refresh_plex(cfg)
    return 1 if res.failed else 0


def cmd_apply(cfg, args) -> int:
    mapper, api, index, ops = _plan(cfg, args, settle=args.settle)
    print(_summary(ops))
    if not args.yes:
        print("dry run; pass --yes to apply")
        return 0
    res = apply_plan(cfg, ops, mapper, index, api, Journal(os.path.join(cfg.state_dir, "journal.jsonl")), limit=args.limit)
    pruned = prune_empty_dirs([o.root for o in ops], mapper)
    print(f"moved={res.moved} torrents={res.torrents} failed={res.failed} empty_dirs_removed={pruned}")
    if cfg.refresh_plex and res.moved:
        _refresh_plex(cfg)
    return 1 if res.failed else 0


def _refresh_plex(cfg) -> None:
    from plexapi.server import PlexServer

    server = PlexServer(cfg.plex_url, cfg.plex_token)
    for s in server.library.sections():
        if s.type in ("movie", "show"):
            s.update()


def cmd_undo(cfg, args) -> int:
    api = _connect_deluge(cfg)
    n = undo(Journal(args.journal or os.path.join(cfg.state_dir, "journal.jsonl")), api, match=args.match)
    print(f"reverted {n} operations")
    if cfg.refresh_plex and n:
        _refresh_plex(cfg)
    return 0


def cmd_watch(cfg, args) -> int:
    args.yes = True
    while True:
        try:
            cmd_apply(cfg, args)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("watch cycle failed")
        time.sleep(cfg.watch_interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="plex-organizer")
    p.add_argument("-c", "--config", default=os.environ.get("CONFIG", "/config/config.yml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "audit", "clean", "unpack", "ingest", "apply", "watch"):
        s = sub.add_parser(name)
        s.add_argument("--library", action="append", help="limit to a Plex library name (repeatable)")
        s.add_argument("--only", help="limit to files matching this glob (Plex paths)")
        if name != "plan":
            s.add_argument("--limit", type=int, help="stop after N torrents/files")
        if name in ("apply", "clean", "unpack", "ingest"):
            s.add_argument("--yes", action="store_true", help="perform the changes")
        if name == "apply":
            s.add_argument("--settle", action="store_true", help="skip recently modified files")
    mv = sub.add_parser("move")
    mv.add_argument("--to", required=True, help="destination directory (Plex path)")
    mv.add_argument("--yes", action="store_true")
    mv.add_argument("paths", nargs="+", help="files or directories (Plex paths) to move")
    u = sub.add_parser("undo")
    u.add_argument("--journal")
    u.add_argument("--match", help="only revert records whose source path matches this regex")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    cfg = cfgmod.load(args.config if os.path.exists(args.config) else None)
    os.makedirs(cfg.state_dir, exist_ok=True)
    if args.cmd == "watch":
        args.settle = True
    return {"plan": cmd_plan, "audit": cmd_audit, "clean": cmd_clean, "unpack": cmd_unpack, "ingest": cmd_ingest, "move": cmd_move, "apply": cmd_apply, "undo": cmd_undo, "watch": cmd_watch}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
