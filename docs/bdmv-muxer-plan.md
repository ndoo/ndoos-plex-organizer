# Plan: BDMV watcher that builds Plex-ready MKVs

Handoff for a future agent. Not implemented yet.

## Background

`plex-organizer clean` moves Blu-ray / DVD folders (any folder containing `BDMV`, `VIDEO_TS`, `AUDIO_TS` or `CERTIFICATE`) out of the Plex libraries into a top-level `BDMVs/` folder next to the library folders (config: `clean.disc_dir`). Plex cannot read these structures. Torrent-owned discs are moved with Deluge `move_storage`, so they keep seeding from `BDMVs/<name>/`. The muxer must therefore treat everything under `BDMVs/` as read-only source material and write its output elsewhere.

## Goal

A daemon (separate container, same repo family) that watches `BDMVs/`, and for every new disc folder produces one or more `.mkv` files in the right Plex library, named per Plex conventions, without disturbing Deluge.

## Requirements

1. **Detect** new disc folders under `BDMVs/` (inotify, with a periodic rescan fallback). Wait until the folder is stable (no size change for N minutes) and, if Deluge owns it, the torrent is complete (not Downloading/Checking/Moving).
2. **Classify** the disc: Blu-ray (`BDMV/index.bdmv`, `PLAYLIST/*.mpls`, `STREAM/*.m2ts`) or DVD (`VIDEO_TS/*.IFO`). Skip and report anything else.
3. **Choose titles** to extract:
   - Read playlists with `mkvmerge --identify` on `.mpls` files (mkvtoolnix reads MPLS directly), or `ffprobe`/`bd_list_titles` (libbluray) as a cross-check.
   - Main feature: longest playlist, ignoring duplicates (same clip list, "play all" and seamless-branching playlists with repeated clips). Use clip-list deduplication, not duration alone.
   - TV/multi-episode discs: playlists of similar length (+/- 10%) in the 18-70 minute range are episodes; order by playlist number, then confirm against Plex episode runtimes.
   - Extras: playlists under ~20 minutes go to `Extras` / `Featurettes` folders (Plex extras naming) only if configured; default is to skip.
4. **Identify** the title with the Plex-first rule used elsewhere: look the disc up in Plex by folder name (`tmdb`/`imdb` hints via `{tmdb-123}`, year in name); if there is no match, write the job to a review queue (JSON + CLI) instead of guessing.
5. **Mux** with mkvtoolnix (`mkvmerge -o out.mkv playlist.mpls`) so that video, all lossless audio (TrueHD, DTS-HD MA, FLAC/LPCM) and PGS/SRT subtitles are copied without re-encoding; keep chapters from the playlist. Drop nothing silently; configurable language filter (default keep all). For DVD use `mkvmerge` on the concatenated VOBs per title (title sets from `lsdvd` / `dvdbackup` or `HandBrake --scan` listing; `makemkvcon` is an alternative if licensing allows) and keep MPEG-2 as is. Encrypted discs are out of scope: detect (`AACS`/`CSS` markers, mux failure) and report.
6. **Name and place** output per Plex rules, reusing `plex_organizer.naming` (`movie_names`, `episode_names`): `Movies/Title (Year)/Title (Year).mkv`, with edition/version suffix when another version exists. Write to a temp name (`.tmp.mkv` in the destination folder) and rename on success so Plex never sees partial files. Never overwrite an existing file.
7. **Deluge safety**: the source stays in `BDMVs/` and keeps seeding; the output is a new, non-torrent file. Do not rename, move or delete anything under `BDMVs/`. After success, append a record to a state file (source folder, playlists used, output path, tool versions) so work is not repeated.
8. **Plex**: trigger a partial section scan on the destination folder after each output.
9. **Idempotence and recovery**: state file keyed by disc folder + content fingerprint (hash of `index.bdmv` + clip sizes). Crash-safe: leftover `.tmp.mkv` files are cleaned on startup. Dry-run mode prints the playlists it would extract and the target names.

## Suggested design

- Python 3.12 package `bdmv-muxer`, same config conventions as plex-organizer (YAML, env/`*_FILE` secrets, `path_maps`, excludes by folder glob).
- Modules: `discscan` (classify, parse playlists via `mkvmerge --identify --identification-format json`), `titles` (selection heuristics, pure functions, unit-tested against fixture JSON), `mux` (subprocess wrapper, progress, temp file handling), `plexmatch` (reuse `plex_organizer.plexsrc`/naming), `watch` (inotify + stability check + Deluge state via `deluge-client`), `queue` (SQLite or JSONL review queue), `cli` (`scan`, `plan`, `run`, `review`).
- Container: `python:3.12-slim` plus `mkvtoolnix`, `ffmpeg`, `libbluray-bin`; run as PUID/PGID; mount `BDMVs` read-only and the libraries read-write.
- Resource limits: one mux at a time by default; optional `nice`/`ionice`; check free space against 1.1x the source size before starting.

## Testing

- Unit tests with recorded `mkvmerge --identify` JSON for: single-feature disc, disc with play-all playlist, multi-episode TV disc, 3D/dup-angle disc, DVD.
- Integration test with a tiny synthetic BDMV folder (generate with `ffmpeg` + `tsMuxeR`/`mkvmerge`), run in Docker.
- On the server: dry-run across `BDMVs/`, review the plan, then mux one disc and confirm in Plex (match, playback, subtitles, chapters) before enabling the watcher.

## Open questions for the owner

- Keep the disc folder after a successful mux, or delete once the MKV is verified? (Default: keep.)
- Extras policy (skip / `Extras` folder / `Featurettes`).
- Subtitle/audio language filter and whether to keep commentary tracks.
- MakeMKV licensing/use for encrypted discs, or only already-decrypted rips.
