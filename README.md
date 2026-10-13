# ndoo's Plex Organizer

> **Read this first.** This project was vibe coded with an AI assistant and is highly specific to one person's media server (one Plex, one Deluge, one ZFS pool, one set of library folders and a lot of old release-style filenames). It is published in case it is useful to anyone with this very specific problem, not as a general-purpose tool. Expect rough edges, read the code before running it, and run the dry runs first. Several commands rename, move and delete files.

## The problem it solves

I have a Plex library full of release-style filenames (`Show.S01E01.1080p.WEB-DL-GRP.mkv`, loose movies, `Season 01` folders at the wrong level), and most of those files are still being seeded by Deluge. Renaming them by hand, or with a generic renamer, breaks the torrents: Deluge loses the files and starts rechecking or errors out.

So I wrote this to rename and sort my files to Plex's naming scheme, using Plex's own matches as the source of truth, and to change anything a torrent owns through Deluge's RPC so the torrent keeps seeding without a recheck.

Before and after:

    Movies/Twelve Monkeys (1995) (1080p).mkv
    Movies/Snake.Eyes.G.I.Joe.Origins.2021.1080p.BluRay.REMUX.AVC.Atmos-TRiToN.mkv
    TV/The Good Place/The.Good.Place.S01.Extended.1080p.BluRay.REMUX-GRP/The.Good.Place.S01E02.Flying.1080p....mkv

    Movies/12 Monkeys (1995)/12 Monkeys (1995).mkv
    Movies/Snake Eyes (2021)/Snake Eyes (2021) - 1080p.mkv
    TV/The Good Place (2016)/Season 01/The Good Place (2016) - s01e02 - Flying.mkv

## Is this for you?

Probably only if you have the same problem. I wrote it for one setup: Plex and Deluge 2.x on a machine I control, media on a single filesystem where renames are instant, and me being comfortable with Docker, reading a CSV, and fixing wrong matches in Plex myself. If you use Sonarr/Radarr to manage the library from the start, you do not need this.

## What it will and will not do to the files

- Renames and moves video files (plus their subtitles/NFOs that share the filename) inside your movie and TV libraries, and creates the folders needed.
- Torrent-owned files are changed only through Deluge. Their Deluge save path ends up at the library root.
- It never renames anything in a Blu-ray/DVD folder (`BDMV`, `VIDEO_TS`).
- It skips anything it is unsure about and tells you why (see `plan.csv`).
- Only the `clean`, `unpack` and `ingest` commands delete files, and they are dry runs unless you pass `--yes`. Deleted files cannot be restored; their paths are written to `deleted.log`.
- Renames and moves are journaled (`journal.jsonl`) and can be reversed with `undo`.

## Quick start

You need: Plex with your libraries scanned, Deluge 2.x with its daemon reachable (default port 58846), Docker with Compose on the machine that holds the media, and a backup of Deluge's `state/` folder (plus a filesystem snapshot if you can take one).

**1. Get the image**

    docker pull ghcr.io/ndoo/ndoos-plex-organizer:latest   # newest release; :edge follows main

or build it from a checkout: `git clone https://github.com/ndoo/ndoos-plex-organizer.git && docker build -t ndoos-plex-organizer ndoos-plex-organizer`.

**2. Write the config**

    mkdir organizer-config
    curl -o organizer-config/config.yml https://raw.githubusercontent.com/ndoo/ndoos-plex-organizer/main/config.example.yml

Set the Plex and Deluge addresses and, most importantly, `path_maps`: the same folder as Plex sees it, as this container sees it, and as Deluge sees it. If Plex and this container mount the media at `/data` and Deluge mounts it at `/downloads`:

    path_maps:
      - plex: /data
        local: /data
        deluge: /downloads

If this is wrong, torrent-owned files look like ordinary files and would be renamed behind Deluge's back. The first dry run shows you (step 3).

**Networking.** Put the organizer on the same Docker network as Plex and Deluge and use their container (service) names, with Docker's internal DNS doing the rest:

    plex:   {url: "http://plex:32400"}
    deluge: {host: deluge}

If Plex or Deluge runs with `network_mode: host`, those names do not resolve from a bridge network. The tool then tries `host.docker.internal` (add `extra_hosts: ["host.docker.internal:host-gateway"]`, as in the compose example), then the container's default gateway, then `127.0.0.1`, and uses the first one that answers; it logs which one it picked. You can also run the organizer itself with host networking and leave the addresses as `localhost`. Deluge's daemon must accept remote connections (`allow_remote`) when it is reached over a network.

**3. Dry run**

    docker run --rm --network <network-shared-with-plex-and-deluge> --user 1000:1000 \
      --add-host host.docker.internal:host-gateway \
      -v ./organizer-config:/config -v /path/to/media:/data \
      -v "/path/to/plex/Preferences.xml:/plex/Preferences.xml:ro" \
      -v /path/to/deluge/auth:/deluge/auth:ro \
      ghcr.io/ndoo/ndoos-plex-organizer:latest plan

Or with Compose: add the `plex-organizer` service from `compose.example.yml` to your stack, fill in the paths, and run `docker compose run --rm plex-organizer plan`.

`plan` prints a summary and writes `organizer-config/plan.csv`. Open it. Check that torrent-owned files show a torrent id in the `owner` column and that the targets look right. Nothing has changed yet.

**4. Try it on a few files**

    ... ndoos-plex-organizer apply --yes --limit 3 --library "Movies"

(same options as above; `/data` must be mounted read-write). Confirm that Deluge still shows the torrents as seeding with no recheck, and that Plex keeps the items and their watch state.

**5. Do the rest**

Run `apply --yes` library by library. Switch off Plex's "scan my library automatically" setting while it runs, because every rename triggers a rescan. After that, work through the clean-up commands in the next section. Run `plan` again at the end: when it reports nothing left to move, you are done.

## Typical workflow

1. `audit`: lists Plex matches that look wrong (a show matched to the wrong series, episode numbers that do not agree with the filenames). Fix those in Plex first, because Plex's matches decide the names.
2. `plan`, then `apply` as above.
3. `ingest`: handles videos Plex does not recognise (for example `[S1.Ep6] Show - Episode 6.mkv`), extras and OVAs.
4. `unpack`: extracts leftover `.rar` sets that have no extracted copy and deletes the archives.
5. `clean`: moves Blu-ray/DVD folders to `clean.disc_dir` and deletes leftovers such as `.nfo`, `.sfv`, samples.
6. Repeat `plan` / `apply` until it is idempotent, then optionally run the `watch` service for new downloads.

## Command reference

Every command that changes anything is a dry run unless given `--yes`. `plan` and `audit` never change anything. Most commands accept `--library NAME` and `--only GLOB`.

| Command | What it does |
| --- | --- |
| `plan` | Writes `plan.csv`: target path per file, Deluge owner, and a verdict (`move`, `ok`, `skip` + reason). |
| `apply --yes [--limit N]` | Applies the plan. Free files are renamed on disk; torrent-owned files go through Deluge (storage moved to the library root, then files renamed). Rescans Plex and removes emptied folders. |
| `audit` | Compares filenames with Plex's show, season/episode and episode title and reports mismatches. |
| `move --to DIR PATH...` | Moves files or folders to another library folder, through Deluge when a torrent owns them. |
| `ingest --yes` | Maps untracked videos to a Plex show from the folder name, parses odd episode numbering, routes extras to Plex extras folders and OVAs/specials to Season 00, deletes zero-byte stubs, reports the rest. |
| `unpack --yes` | Deletes a rar set when a same-size video already exists; otherwise extracts it in place with 7z and deletes the volumes after the sizes check out. Torrent-owned sets are skipped. |
| `clean --yes` | Moves disc folders to `clean.disc_dir`; deletes untracked non-video leftovers (torrent-owned ones are deselected in Deluge first). Discs, disc images, subtitles and Plex artwork are kept; temp files, unmatched archives and untracked videos are only reported. |
| `undo [--match REGEX]` | Reverses `journal.jsonl` records, optionally only those whose source path matches. |
| `watch` | Repeats `apply` every `watch_interval` seconds, skipping files that are still changing or downloading. |

Naming: `Title (Year)/Title (Year).ext`; several versions `Title (Year) - 1080p.ext`, `- 2160p HDR.ext`; editions `{edition-X}`; split files `- partN` (list them under `naming.part_paths`); `Show (Year)/Season 01/Show (Year) - s01e01 - Title.ext`, `s01e01-e02` for multi-episode files.

## Why a file was skipped

`plan.csv` gives a reason for every skip: the torrent holds a video Plex has not matched, spans libraries, is checking/moving/errored, or is excluded; the movie has no year in Plex (unmatched); the Plex title changed since the file was last normalised (check the match, then list the file under `accept_title_changes` to let it through); two files map to the same name; the file sits inside a disc folder. Exclude things with `exclude` / `include` in the config (library names, path globs, title regexes, torrent names, Deluge labels).

## Troubleshooting

- **Torrent-owned files show as `free`**: `path_maps` does not match how Deluge sees the paths. Fix it before running `apply`.
- **"No deluge section configured"**: add one, or set `deluge.disabled: true` if nothing in the libraries is seeded.
- **A run stops with a Plex "not found" error**: Plex was rescanning. Run it again; the tool waits for running scans before reading the libraries.
- **`unpack` reports "Unsupported Method"**: that RAR variant needs the non-free `unrar`, which the image installs from Debian's non-free section at build time (if you build without it, `unpack` falls back to 7z). Archives that cannot be extracted are left untouched.
- **Names look wrong**: they come from Plex. Use "Fix match" in Plex (or correct the files Plex mismatched), rescan, and plan again.

## Releases

Versions follow [Semantic Versioning](https://semver.org); changes are listed in `CHANGELOG.md`. Pushing a tag such as `v0.1.0` runs the release workflow: it checks the tag against `pyproject.toml`, runs the tests, builds the sdist and wheel, publishes the image to GHCR as `X.Y.Z`, `X.Y` and `latest`, and creates the GitHub release from the matching CHANGELOG section. Pushes to `main` publish `edge`.

## Development

    pip install -e .[test]
    pytest

`docs/bdmv-muxer-plan.md` describes a separate, not yet written, daemon that would mux the moved disc folders into MKV files.

## License

MIT, see `LICENSE`.
