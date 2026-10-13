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