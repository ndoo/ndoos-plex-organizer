import os
import posixpath

import pytest

from plex_organizer import naming
from plex_organizer.apply import Journal, apply_plan, undo
from plex_organizer.config import Config, DelugeConfig, PathMap
from plex_organizer.delugeidx import DelugeIndex, Torrent
from plex_organizer.model import MediaFile, PlexItem
from plex_organizer.pathmap import PathMapper
from plex_organizer.planner import build_plan
from plex_organizer.plexsrc import merge_episodes


def test_sanitize_and_names():
    assert naming.sanitize('Star Wars: Ep. I?') == "Star Wars - Ep. I"
    folder, fname = naming.movie_names("À bout de souffle", 1960, ".mkv")
    assert (folder, fname) == ("À bout de souffle (1960)", "À bout de souffle (1960).mkv")
    _, _, f = naming.episode_names("Show", 2017, 1, [(1, "A"), (2, "B")], ".mkv")
    assert f == "Show (2017) - s01e01-e02 - A & B.mkv"
    assert len(naming.movie_names("x" * 400, 2000, ".mkv")[1].encode()) <= 255


def test_media_label_and_edition():
    assert naming.media_label("4k", "HDR") == "2160p HDR"
    _, f = naming.movie_names("Avatar", 2009, ".mkv", edition="Director's Cut", label="1080p")
    assert f == "Avatar (2009) {edition-Director's Cut} - 1080p.mkv"


class FakeDeluge:
    def __init__(self, torrents, mapper):
        self.t = {x.id: x for x in torrents}
        self.mapper = mapper

    def torrents_status(self):
        return {t.id: self._st(t) for t in self.t.values()}

    def _st(self, t):
        return {"name": t.name, "save_path": t.save_path, "state": t.state, "progress": t.progress,
                "label": t.label, "files": [{"index": i, "path": p} for i, p in t.files]}

    def torrent_status(self, tid):
        return self._st(self.t[tid])

    def move_storage(self, tid, dest):
        t = self.t[tid]
        for _, rel in t.files:
            s = self.mapper.deluge_to_local(posixpath.join(t.save_path, rel))
            d = self.mapper.deluge_to_local(posixpath.join(dest, rel))
            os.makedirs(posixpath.dirname(d), exist_ok=True)
            os.rename(s, d)
        t.save_path = dest

    def deselect_files(self, tid, indices):
        self.t[tid].deselected = getattr(self.t[tid], "deselected", []) + list(indices)

    def rename_files(self, tid, renames):
        t = self.t[tid]
        files = dict(t.files)
        for i, new in renames:
            s = self.mapper.deluge_to_local(posixpath.join(t.save_path, files[i]))
            d = self.mapper.deluge_to_local(posixpath.join(t.save_path, new))
            os.makedirs(posixpath.dirname(d), exist_ok=True)
            os.rename(s, d)
            files[i] = new
        t.files = sorted(files.items())


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("x")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "data"
    maps = [PathMap(plex="/data", local=str(root), deluge="/dl")]
    cfg = Config(state_dir=str(tmp_path), deluge=DelugeConfig())
    return root, cfg, PathMapper(maps)


def movie(title, year, path, **kw):
    return PlexItem("Movies", "movie", "/data/Movies", title, year, files=[MediaFile(path, **kw)])


def test_free_file_moved_and_sidecar_follows(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/Foo.2012.1080p-GRP.mkv")
    touch(f"{root}/Movies/Foo.2012.1080p-GRP.en.srt")
    ops = build_plan(cfg, [movie("Foo", 2012, "/data/Movies/Foo.2012.1080p-GRP.mkv")], mapper, None)
    res = apply_plan(cfg, ops, mapper, None, None, Journal(str(root / "j.jsonl")))
    assert res.moved == 2 and res.failed == 0
    assert (root / "Movies/Foo (2012)/Foo (2012).mkv").exists()
    assert (root / "Movies/Foo (2012)/Foo (2012).en.srt").exists()
    assert build_plan(cfg, [movie("Foo", 2012, "/data/Movies/Foo (2012)/Foo (2012).mkv")], mapper, None)[0].action == "ok"
    assert undo(Journal(str(root / "j.jsonl")), None) == 2
    assert (root / "Movies/Foo.2012.1080p-GRP.mkv").exists()


def test_torrent_owned_goes_through_deluge(env):
    root, cfg, mapper = env
    touch(f"{root}/TV/Show/Show.S01.1080p/Show.S01E01.mkv")
    touch(f"{root}/TV/Show/Show.S01.1080p/sample.mkv")
    t = Torrent("abc", "Show.S01.1080p", "/dl/TV/Show", "Seeding", 100.0, "",
                [(0, "Show.S01.1080p/Show.S01E01.mkv"), (1, "Show.S01.1080p/sample.mkv")])
    fake = FakeDeluge([t], mapper)
    index = DelugeIndex.from_api(fake)
    item = PlexItem("TV", "episode", "/data/TV", "Pilot", show_title="Show", show_year=2017, season=1,
                    episodes=[(1, "Pilot")], files=[MediaFile("/data/TV/Show/Show.S01.1080p/Show.S01E01.mkv")])
    ops = build_plan(cfg, [item], mapper, index)
    assert [o.owner for o in ops] == ["abc"]
    res = apply_plan(cfg, ops, mapper, index, fake, Journal(str(root / "j.jsonl")))
    assert res.torrents == 1 and res.failed == 0
    assert t.save_path == "/dl/TV"
    assert (root / "TV/Show (2017)/Season 01/Show (2017) - s01e01 - Pilot.mkv").exists()
    assert (root / "TV/.leftovers/Show.S01.1080p/sample.mkv").exists()
    assert not (root / "TV/Show").exists()
    undo(Journal(str(root / "j.jsonl")), fake)
    assert t.save_path == "/dl/TV/Show"
    assert (root / "TV/Show/Show.S01.1080p/Show.S01E01.mkv").exists()


def test_partial_torrent_is_blocked(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/pack/A.mkv")
    touch(f"{root}/Movies/pack/B.mkv")
    t = Torrent("p", "pack", "/dl/Movies", "Seeding", 100.0, "", [(0, "pack/A.mkv"), (1, "pack/B.mkv")])
    index = DelugeIndex([t])
    ops = build_plan(cfg, [movie("A", 2000, "/data/Movies/pack/A.mkv")], mapper, index)
    assert ops[0].action == "skip" and "unmatched video" in ops[0].reason


def test_excludes_and_busy(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/A.mkv")
    cfg.exclude.paths = ["/data/Movies/A.*"]
    assert build_plan(cfg, [movie("A", 2000, "/data/Movies/A.mkv")], mapper, None)[0].reason == "excluded path"
    cfg.exclude.paths = []
    t = Torrent("t", "A.mkv", "/dl/Movies", "Checking", 100.0, "", [(0, "A.mkv")])
    assert build_plan(cfg, [movie("A", 2000, "/data/Movies/A.mkv")], mapper, DelugeIndex([t]))[0].reason == "torrent busy"
    t.state = "Seeding"
    cfg.exclude.torrents = ["A*"]
    assert build_plan(cfg, [movie("A", 2000, "/data/Movies/A.mkv")], mapper, DelugeIndex([t]))[0].reason == "excluded torrent"


def test_versions_and_multi_episode(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/a.mkv")
    touch(f"{root}/Movies/b.mkv")
    item = PlexItem("Movies", "movie", "/data/Movies", "Apollo 11", 2019, files=[
        MediaFile("/data/Movies/a.mkv", media_index=0, resolution="1080"),
        MediaFile("/data/Movies/b.mkv", media_index=1, resolution="4k", hdr="HDR")])
    dsts = [o.dst for o in build_plan(cfg, [item], mapper, None)]
    assert dsts == ["/data/Movies/Apollo 11 (2019)/Apollo 11 (2019) - 1080p.mkv",
                    "/data/Movies/Apollo 11 (2019)/Apollo 11 (2019) - 2160p HDR.mkv"]
    e1 = PlexItem("TV", "episode", "/data/TV", "A", show_title="S", season=1, episodes=[(1, "A")], files=[MediaFile("/x.mkv")])
    e2 = PlexItem("TV", "episode", "/data/TV", "B", show_title="S", season=1, episodes=[(2, "B")], files=[MediaFile("/x.mkv")])
    merged = merge_episodes([e1, e2])
    assert len(merged) == 1 and merged[0].episodes == [(1, "A"), (2, "B")]


def test_credentials_from_files(tmp_path, monkeypatch):
    from plex_organizer import config as c

    prefs = tmp_path / "Preferences.xml"
    prefs.write_text('<Preferences PlexOnlineToken="tok123" Other="x"/>')
    auth = tmp_path / "auth"
    auth.write_text("alice:pw1:10\nlocalclient:secret:10\n")
    cfgf = tmp_path / "c.yml"
    cfgf.write_text(f"plex: {{preferences_file: {prefs}}}\ndeluge: {{auth_file: {auth}}}\n")
    for v in ("PLEX_TOKEN", "PLEX_TOKEN_FILE", "DELUGE_PASSWORD", "DELUGE_PASSWORD_FILE"):
        monkeypatch.delenv(v, raising=False)
    cfg = c.load(cfgf)
    assert cfg.plex_token == "tok123" and cfg.deluge.password == "secret"


def test_audit_flags_wrong_series_and_numbers():
    from plex_organizer.audit import audit, parse_filename

    p = parse_filename("Game.of.Thrones.S01E01.Winter.Is.Coming.2160p.UHD.mkv")
    assert (p.series, p.season, p.episodes, p.title) == ("Game of Thrones", 1, [1], "Winter Is Coming")

    def ep(path, n, title):
        return PlexItem("TV", "episode", "/data/TV", title, show_title="Rick and Morty", season=1,
                        episodes=[(n, title)], files=[MediaFile(path)])

    items = [ep("/data/TV/R/Game.of.Thrones.S01E01.Winter.Is.Coming.1080p.mkv", 1, "Pilot"),
             ep("/data/TV/R/Rick.and.Morty.S01E02.Lawnmower.Dog.1080p.mkv", 3, "Lawnmower Dog")]
    kinds = {f.kind for f in audit(items)}
    assert {"different series in filename", "season/episode number differs"} <= kinds


def test_title_already_carrying_year():
    assert naming.with_year("The Tom and Jerry Show (2014)", 2011) == "The Tom and Jerry Show (2014)"


def test_incomplete_torrent_allowed_unless_settling(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/A.mkv")
    t = Torrent("t", "A.mkv", "/dl/Movies", "Downloading", 99.5, "", [(0, "A.mkv")])
    item = movie("A", 2000, "/data/Movies/A.mkv")
    assert build_plan(cfg, [item], mapper, DelugeIndex([t]))[0].action == "move"
    assert build_plan(cfg, [item], mapper, DelugeIndex([t]), settle=True)[0].reason == "torrent incomplete"


def test_clean_deselects_torrent_files_then_deletes(env):
    from plex_organizer import clean

    root, cfg, mapper = env
    touch(f"{root}/Movies/Foo (2012)/Foo (2012).mkv")
    touch(f"{root}/Movies/Foo (2012)/Foo (2012).en.srt")
    touch(f"{root}/Movies/Foo (2012)/Foo.2012.release.nfo")
    touch(f"{root}/Movies/Foo (2012)/Foo.2012.release.sfv")
    touch(f"{root}/Movies/Foo (2012)/poster.jpg")
    touch(f"{root}/Movies/Disc (2000)/BDMV/STREAM/00001.m2ts")
    t = Torrent("a", "Foo", "/dl/Movies", "Seeding", 100.0, "",
                [(0, "Foo (2012)/Foo (2012).mkv"), (1, "Foo (2012)/Foo.2012.release.nfo")])
    fake = FakeDeluge([t], mapper)
    index = DelugeIndex.from_api(fake)
    items = [movie("Foo", 2012, "/data/Movies/Foo (2012)/Foo (2012).mkv")]
    found = clean.find(cfg, items, mapper, index)
    cats = {os.path.basename(s.path): s.category for s in found}
    assert cats == {"Foo.2012.release.nfo": "delete", "Foo.2012.release.sfv": "delete", "poster.jpg": "keep-asset", "00001.m2ts": "keep-disc"}
    removed, skipped = clean.apply_clean(found, mapper, index, fake, str(root / "deleted.log"))
    assert removed == 2 and not skipped and t.deselected == [1]
    assert not (root / "Movies/Foo (2012)/Foo.2012.release.nfo").exists()
    assert (root / "Movies/Foo (2012)/Foo (2012).en.srt").exists()


def test_disc_folders_moved_to_bdmv_dir(env):
    from plex_organizer import clean

    root, cfg, mapper = env
    cfg.disc_dir = "/data/BDMVs"
    touch(f"{root}/Movies/Foo (2012)/Foo (2012).mkv")
    touch(f"{root}/Movies/FreeDisc/BDMV/STREAM/00001.m2ts")
    touch(f"{root}/Movies/SeedDisc/BDMV/STREAM/00001.m2ts")
    t = Torrent("d", "SeedDisc", "/dl/Movies", "Seeding", 100.0, "", [(0, "SeedDisc/BDMV/STREAM/00001.m2ts")])
    fake = FakeDeluge([t], mapper)
    index = DelugeIndex.from_api(fake)
    items = [movie("Foo", 2012, "/data/Movies/Foo (2012)/Foo (2012).mkv")]
    found = clean.find(cfg, items, mapper, index)
    moves = clean.plan_discs(cfg, items, found, mapper, index)
    assert {(m.root, m.owner, m.reason) for m in moves} == {
        ("/data/Movies/FreeDisc", "free", ""), ("/data/Movies/SeedDisc", "d", "")}
    assert clean.apply_discs(moves, mapper, index, fake, Journal(str(root / "j.jsonl"))) == 2
    assert (root / "BDMVs/FreeDisc/BDMV/STREAM/00001.m2ts").exists()
    assert t.save_path == "/dl/BDMVs" and (root / "BDMVs/SeedDisc/BDMV/STREAM/00001.m2ts").exists()


def test_planner_skips_disc_structures_and_names_generic_discs(env):
    from plex_organizer import clean

    root, cfg, mapper = env
    touch(f"{root}/TV/Show/Season 01/VIDEO_TS/VTS_01_1.VOB")
    item = PlexItem("TV", "episode", "/data/TV", "Pilot", show_title="Show", season=1, episodes=[(1, "Pilot")],
                    files=[MediaFile("/data/TV/Show/Season 01/VIDEO_TS/VTS_01_1.VOB")])
    assert build_plan(cfg, [item], mapper, None)[0].reason == "inside disc structure"
    cfg.disc_dir = "/data/BDMVs"
    touch(f"{root}/TV/Show/Season 01/VIDEO_TS/VTS_01_0.IFO")
    items = [PlexItem("TV", "episode", "/data/TV", "x", files=[MediaFile("/data/TV/Other/x.mkv")])]
    touch(f"{root}/TV/Other/x.mkv")
    moves = clean.plan_discs(cfg, items, clean.find(cfg, items, mapper, None), mapper, None)
    assert [(m.root, m.dest) for m in moves] == [("/data/TV/Show/Season 01", "/data/BDMVs/Show - Season 01")]


def test_prune_empty_dirs_keeps_root_and_nonempty(env):
    from plex_organizer.apply import prune_empty_dirs

    root, cfg, mapper = env
    touch(f"{root}/Movies/keep/a.mkv")
    touch(f"{root}/Movies/junk/sub/.DS_Store")
    os.makedirs(root / "Movies/empty/deeper")
    assert prune_empty_dirs(["/data/Movies"], mapper) == 4
    assert (root / "Movies/keep/a.mkv").exists() and (root / "Movies").is_dir()
    assert not (root / "Movies/empty").exists() and not (root / "Movies/junk").exists()


def test_part_paths_name_split_movies_as_parts(env):
    root, cfg, mapper = env
    cfg.naming.part_paths = ("/data/Movies/Out 1*",)
    touch(f"{root}/Movies/Out 1 a.mkv")
    touch(f"{root}/Movies/Out 1 b.mkv")
    item = PlexItem("Movies", "movie", "/data/Movies", "Out 1", 1971, files=[
        MediaFile("/data/Movies/Out 1 a.mkv", media_index=0, resolution="1080"),
        MediaFile("/data/Movies/Out 1 b.mkv", media_index=1, resolution="1080")])
    assert [o.dst for o in build_plan(cfg, [item], mapper, None)] == [
        "/data/Movies/Out 1 (1971)/Out 1 (1971) - part1.mkv", "/data/Movies/Out 1 (1971)/Out 1 (1971) - part2.mkv"]


def test_move_torrent_and_free_dirs(env):
    from plex_organizer import relocate

    root, cfg, mapper = env
    touch(f"{root}/Movies/Series.S01/e1.mkv")
    touch(f"{root}/Movies/Plain/e1.mkv")
    t = Torrent("s", "Series.S01", "/dl/Movies", "Seeding", 100.0, "", [(0, "Series.S01/e1.mkv")])
    fake = FakeDeluge([t], mapper)
    index = DelugeIndex.from_api(fake)
    moves = relocate.plan_moves(["/data/Movies/Series.S01", "/data/Movies/Plain"], "/data/TV/Show", mapper, index)
    assert [m.reason for m in moves] == ["", ""]
    assert relocate.apply_moves(moves, mapper, index, fake, Journal(str(root / "j.jsonl"))) == 2
    assert t.save_path == "/dl/TV/Show" and (root / "TV/Show/Series.S01/e1.mkv").exists()
    assert (root / "TV/Show/Plain/e1.mkv").exists()


def test_duplicate_quality_numbering_is_stable_and_unmatched_skipped(env):
    root, cfg, mapper = env
    for n, size in (("a", 1), ("b", 5)):
        touch(f"{root}/Movies/{n}.mkv")
        (root / f"Movies/{n}.mkv").write_text("x" * size)
    mk = lambda x, y: PlexItem("Movies", "movie", "/data/Movies", "Dup", 2000, files=[
        MediaFile(f"/data/Movies/{x}.mkv", media_index=0, resolution="1080"),
        MediaFile(f"/data/Movies/{y}.mkv", media_index=1, resolution="1080")])
    one = [o.dst for o in build_plan(cfg, [mk("a", "b")], mapper, None)]
    two = [o.dst for o in build_plan(cfg, [mk("b", "a")], mapper, None)]
    assert one == ["/data/Movies/Dup (2000)/Dup (2000) - 1080p #1.mkv", "/data/Movies/Dup (2000)/Dup (2000) - 1080p #2.mkv"]
    assert sorted(two) == sorted(one)
    item = PlexItem("Movies", "movie", "/data/Movies", "Gentlemen", None, files=[MediaFile("/data/Movies/a.mkv")])
    assert build_plan(cfg, [item], mapper, None)[0].reason == "no year (unmatched in Plex)"
    touch(f"{root}/Movies/Panic (1947)/Panic (1947).mkv")
    item = PlexItem("Movies", "movie", "/data/Movies", "Hard Boiled Mahoney", 1947,
                    files=[MediaFile("/data/Movies/Panic (1947)/Panic (1947).mkv")])
    assert build_plan(cfg, [item], mapper, None)[0].reason == "Plex match changed; review"


def test_existing_counter_suffix_is_left_alone(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/Dup (2000)/Dup (2000) - 1080p #3.mkv")
    touch(f"{root}/Movies/Dup (2000)/Dup (2000) - 1080p #4.mkv")
    item = PlexItem("Movies", "movie", "/data/Movies", "Dup", 2000, files=[
        MediaFile("/data/Movies/Dup (2000)/Dup (2000) - 1080p #3.mkv", media_index=0, resolution="1080"),
        MediaFile("/data/Movies/Dup (2000)/Dup (2000) - 1080p #4.mkv", media_index=1, resolution="1080")])
    assert {o.action for o in build_plan(cfg, [item], mapper, None)} == {"ok"}


def test_year_range_in_title_collapses_to_single_year():
    assert naming.with_year("Tom and Jerry (1940-1967)", 1940) == "Tom and Jerry (1940)"


def test_clean_protects_disc_roots_images_and_temp_files(env):
    from plex_organizer import clean

    root, cfg, mapper = env
    touch(f"{root}/Movies/Foo (2012)/Foo (2012).mkv")
    touch(f"{root}/Movies/Disc/AACS/Unit_Key_RO.inf")
    touch(f"{root}/Movies/Disc/BDMV/index.bdmv")
    touch(f"{root}/Movies/Image/movie.iso")
    touch(f"{root}/Movies/.part.AbC123")
    touch(f"{root}/TV/Show/Show.S01E01.rar")
    items = [movie("Foo", 2012, "/data/Movies/Foo (2012)/Foo (2012).mkv"),
             PlexItem("TV", "episode", "/data/TV", "Pilot", show_title="Show", season=1, episodes=[(1, "Pilot")],
                      files=[MediaFile("/data/TV/Show (2000)/Season 01/Show (2000) - s01e01 - Pilot.mkv")])]
    cats = {os.path.basename(s.path): s.category for s in clean.find(cfg, items, mapper, None)}
    assert cats["Unit_Key_RO.inf"] == "keep-disc" and cats["index.bdmv"] == "keep-disc"
    assert cats["movie.iso"] == "keep-image" and cats[".part.AbC123"] == "review-temp"
    assert cats["Show.S01E01.rar"] == "delete"


def test_movie_archive_deleted_only_when_title_year_tracked(env):
    from plex_organizer import clean

    root, cfg, mapper = env
    touch(f"{root}/Movies/The.Hitch-Hiker.1953.1080p-GRP/the.hitch-hiker.1953.1080p-grp.r00")
    touch(f"{root}/Movies/Other.1960.1080p-GRP/other.1960.1080p-grp.r00")
    items = [movie("The Hitch-Hiker", 1953, "/data/Movies/The Hitch-Hiker (1953)/The Hitch-Hiker (1953).mkv")]
    cats = {os.path.basename(s.path): s.category for s in clean.find(cfg, items, mapper, None)}
    assert cats == {"the.hitch-hiker.1953.1080p-grp.r00": "delete", "other.1960.1080p-grp.r00": "review-archive"}


def test_unpack_plan_and_apply(env):
    from plex_organizer import unpack

    root, cfg, mapper = env
    d = root / "Movies/Rel.1953-GRP"
    for n in ("x.rar", "x.r00", "x.r01", "x.sfv", "y.rar", "y.part1.rar", "y.part2.rar"):
        touch(str(d / n))
    big = 5_000_000
    touch(f"{root}/Movies/Existing (1953)/Existing (1953).mkv")
    (root / "Movies/Existing (1953)/Existing (1953).mkv").write_bytes(b"0" * big)
    items = [movie("Existing", 1953, "/data/Movies/Existing (1953)/Existing (1953).mkv")]

    def lister(path):
        return [("x.mkv", big)] if path.endswith("x.rar") else [("y.mkv", big + 1)]

    sets = {s.main: s for s in unpack.plan(cfg, items, mapper, None, lister=lister)}
    assert set(sets) == {"x.rar", "y.rar", "y.part1.rar"}
    assert sets["x.rar"].action == "delete-redundant" and sets["x.rar"].files == ["x.r00", "x.r01", "x.rar", "x.sfv"]

    def fake_extract(archive, dest):
        (root / "Movies/Rel.1953-GRP/y.mkv").write_bytes(b"0" * (big + 1))

    ex, dele, problems = unpack.apply([sets["x.rar"], sets["y.rar"]], extract=fake_extract)
    assert (ex, problems) == (1, []) and (d / "y.mkv").exists() and not (d / "x.rar").exists()
    assert not (d / "y.rar").exists()


def test_ingest_maps_unrecognised_names_to_plex_show(env):
    from plex_organizer import ingest

    root, cfg, mapper = env
    touch(f"{root}/TV/Little Britain/Season 01/[S1.Ep6] Little Britain - Episode 6.mkv")
    (root / "TV/Little Britain/Season 01/[S1.Ep6] Little Britain - Episode 6.mkv").write_text("x")
    touch(f"{root}/TV/Little Britain/Season 01/Extras/Behind the Scenes.mkv")
    touch(f"{root}/TV/Little Britain/Stub.mkv")
    (root / "TV/Little Britain/Stub.mkv").write_text("")
    touch(f"{root}/TV/Little Britain/Mystery.mkv")
    (root / "TV/Little Britain/Mystery.mkv").write_text("x")
    (root / "TV/Little Britain/Season 01/Extras/Behind the Scenes.mkv").write_text("x")
    tracked = PlexItem("TV", "episode", "/data/TV", "Pilot", show_title="Little Britain", show_year=2003, season=1,
                       episodes=[(1, "Pilot")], files=[MediaFile("/data/TV/Little Britain (2003)/Season 01/x.mkv")])
    extra, stubs, left = ingest.plan_ingest(cfg, [tracked], mapper)
    dsts = sorted(next(iter(i.dst_override.values())) for i in extra)
    assert dsts == ["/data/TV/Little Britain (2003)/Season 01/Behind The Scenes/Behind the Scenes.mkv",
                    "/data/TV/Little Britain (2003)/Season 01/Little Britain (2003) - s01e06.mkv"]
    assert stubs == ["/data/TV/Little Britain/Stub.mkv"] and [u.path for u in left] == ["/data/TV/Little Britain/Mystery.mkv"]
    ops = build_plan(cfg, extra, mapper, None)
    assert {o.action for o in ops} == {"move"}


def test_pick_host_prefers_configured_name_then_docker_host_then_localhost():
    from plex_organizer import net

    calls = []

    def only(ok):
        def connect(h, p):
            calls.append(h)
            return h == ok
        return connect

    assert net.pick_host("plex", 32400, connect=only("plex"), gateway="172.17.0.1") == "plex"
    calls.clear()
    assert net.pick_host("plex", 32400, connect=only("host.docker.internal"), gateway="172.17.0.1") == "host.docker.internal"
    assert calls == ["plex", "host.docker.internal"]
    assert net.pick_host("plex", 32400, connect=only("172.17.0.1"), gateway="172.17.0.1") == "172.17.0.1"
    assert net.pick_host("plex", 32400, connect=only("127.0.0.1"), gateway="") == "127.0.0.1"
    assert net.pick_host("plex", 32400, connect=lambda h, p: False, gateway="172.17.0.1") == "plex"


def test_gateway_ip_parses_proc_net_route(tmp_path):
    from plex_organizer import net

    f = tmp_path / "route"
    f.write_text("Iface\tDestination\tGateway\tFlags\neth0\t00000000\t0100A8C0\t0003\neth0\t0000A8C0\t00000000\t0001\n")
    assert net.gateway_ip(str(f)) == "192.168.0.1"


def test_ingest_uses_journal_aliases_and_recognises_ova_and_extras_folders(env):
    from plex_organizer import ingest

    root, cfg, mapper = env
    for rel in ("TV/Star Trek TNG/Season 01/Extras/Energized.Taking.mkv", "TV/Peep Show/Season 05/Peep.Show.S05.Extras.DVDRip/peep.show.s05.deleted.scenes.avi",
                "Anime/Lucky Star/[EnA]_Lucky_Star_OVA_(DVD)_[5F74AE07].mkv"):
        touch(str(root / rel))
        (root / rel).write_text("x")
    tng = PlexItem("TV", "episode", "/data/TV", "Pilot", show_title="Star Trek: The Next Generation", show_year=1987, season=1,
                   episodes=[(1, "Pilot")], files=[MediaFile("/data/TV/Star Trek - The Next Generation (1987)/Season 01/a.mkv")])
    peep = PlexItem("TV", "episode", "/data/TV", "x", show_title="Peep Show", show_year=2003, season=5, episodes=[(1, "x")],
                    files=[MediaFile("/data/TV/Peep Show (2003)/Season 05/a.mkv")])
    star = PlexItem("Anime", "episode", "/data/Anime", "x", show_title="Lucky Star", show_year=2007, season=1, episodes=[(1, "x")],
                    files=[MediaFile("/data/Anime/Lucky Star (2007)/Season 01/a.mkv")])
    records = [{"type": "fs", "src": f"{root}/TV/Star Trek TNG/Season 01/a.mkv",
                "dst": f"{root}/TV/Star Trek - The Next Generation (1987)/Season 01/a.mkv"}]
    aliases = ingest.load_aliases(records, ["/data/TV"], mapper)
    assert aliases == {"startrektng": "startrekthenextgeneration"}
    extra, _, left = ingest.plan_ingest(cfg, [tng, peep, star], mapper, aliases)
    dsts = sorted(next(iter(i.dst_override.values())) for i in extra)
    assert dsts == [
        "/data/Anime/Lucky Star (2007)/Season 00/Lucky Star (2007) - s00e01 - Lucky Star OVA.mkv",
        "/data/TV/Peep Show (2003)/Season 05/Deleted Scenes/peep.show.s05.deleted.scenes.avi",
        "/data/TV/Star Trek - The Next Generation (1987)/Season 01/Featurettes/Energized.Taking.mkv"]
    assert left == []


def test_unpack_restores_vobsub_name_and_cleans_failed_outputs(env):
    from plex_organizer import unpack

    root, cfg, mapper = env
    d = root / "TV/Show/Subs"
    d.mkdir(parents=True)
    (d / "e.idx").write_bytes(b"idx")
    (d / "e.rar").write_bytes(b"\x00\x00\x01\xba sub data")
    s = unpack.ArchiveSet(str(d), "e.subs.rar", ["e.subs.rar"], [("e.idx", 3), ("e.rar", 14)])
    unpack.fix_vobsub(s)
    assert (d / "e.sub").exists() and not (d / "e.rar").exists()
    (d / "z.idx").write_bytes(b"")
    unpack._remove_empty_outputs(unpack.ArchiveSet(str(d), "z.rar", [], [("z.idx", 5)]))
    assert not (d / "z.idx").exists() and (d / "e.idx").exists()


def test_subtitles_in_subs_folder_follow_their_video(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/Rel.2000-GRP/rel.2000-grp.mkv")
    touch(f"{root}/Movies/Rel.2000-GRP/Subs/rel.2000-grp.idx")
    touch(f"{root}/Movies/Rel.2000-GRP/Subs/rel.2000-grp.sub")
    item = movie("Rel", 2000, "/data/Movies/Rel.2000-GRP/rel.2000-grp.mkv")
    dsts = sorted(o.dst for o in build_plan(cfg, [item], mapper, None))
    assert dsts == ["/data/Movies/Rel (2000)/Rel (2000).idx", "/data/Movies/Rel (2000)/Rel (2000).mkv",
                    "/data/Movies/Rel (2000)/Rel (2000).sub"]


def test_subtitle_rescue_uses_the_journal(env):
    from plex_organizer import ingest

    root, cfg, mapper = env
    touch(f"{root}/Movies/Rel (2000)/Rel (2000).mkv")
    touch(f"{root}/Movies/Rel.2000-GRP/Subs/rel.2000-grp.idx")
    touch(f"{root}/Movies/Rel.2000-GRP/Subs/rel.2000-grp.sub")
    records = [{"type": "fs", "src": f"{root}/Movies/Rel.2000-GRP/rel.2000-grp.mkv",
                "dst": f"{root}/Movies/Rel (2000)/Rel (2000).mkv"}]
    items = [movie("Rel", 2000, "/data/Movies/Rel (2000)/Rel (2000).mkv")]
    out = ingest.plan_subtitle_rescue(cfg, items, mapper, records)
    assert sorted(next(iter(i.dst_override.values())) for i in out) == [
        "/data/Movies/Rel (2000)/Rel (2000).idx", "/data/Movies/Rel (2000)/Rel (2000).sub"]
    ops = build_plan(cfg, out, mapper, None)
    assert [o.action for o in ops] == ["move", "move"] and len(ops) == 2


def test_accept_title_changes_lets_a_reviewed_rename_through(env):
    root, cfg, mapper = env
    touch(f"{root}/Movies/Old Title (1982).mkv")
    item = movie("New Title", 1982, "/data/Movies/Old Title (1982).mkv")
    assert build_plan(cfg, [item], mapper, None)[0].reason == "Plex match changed; review"
    cfg.accept_title_changes = ["/data/Movies/Old Title*"]
    assert build_plan(cfg, [item], mapper, None)[0].action == "move"
