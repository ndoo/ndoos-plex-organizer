# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions before 1.0.0 may change
behaviour between minor releases.

## [Unreleased]

## [0.2.0] - 2026-10-14

### Added

- `accept_title_changes` config option to let a reviewed title change through.

### Changed

- `compose.example.yml` puts the organizer services in a profile so `docker compose up` does not start them.

## [0.1.0] - 2026-10-13

First public release. Written for one specific setup (see the README).

### Added

- Plex-driven renaming to Plex naming conventions, with torrent-owned files changed through Deluge
  RPC (`plan`, `apply`, `undo`, `watch`).
- `audit` for suspicious Plex matches.
- `clean` for leftovers and Blu-ray/DVD folders, with torrent-owned leftovers deselected in Deluge
  first.
- `move` for relocating files and folders, through Deluge when a torrent owns them.
- `unpack` for leftover rar sets (`unrar` when installed, otherwise 7z), including the hidden
  VobSub files in scene "Subs" archives.
- `ingest` for videos Plex does not track: odd episode numbering, extras folders, OVAs and specials,
  zero-byte stubs, and subtitles left behind by an earlier rename.
- Subtitles in a `Subs` folder follow their video.
- Plex and Deluge reached by container name, falling back to `host.docker.internal`, the Docker
  gateway and localhost.
- Release workflow: tests, sdist and wheel, GHCR image, GitHub release from this changelog.
