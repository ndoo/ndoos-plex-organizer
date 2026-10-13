# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versions before 1.0.0 may change
behaviour between minor releases.

## [Unreleased]

### Added

- Plex-driven renaming to Plex naming conventions, with torrent-owned files changed through Deluge
  RPC (`plan`, `apply`, `undo`, `watch`).
- `audit` for suspicious Plex matches.
- `clean` for leftovers and Blu-ray/DVD folders.
- `move` for relocating files and folders, through Deluge when a torrent owns them.
- `unpack` for leftover rar sets.
- `ingest` for videos Plex does not track.
- Plex and Deluge reached by container name, with fallback to the Docker host.
