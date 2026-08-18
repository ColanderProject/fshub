# Developer Documentation for fshub

## Overview

fshub is a file system hub application that provides a web-based interface for
managing files across multiple devices: exploring snapshots of file systems,
grouping files, hashing them, and backing them up.

## Project Structure

```
fshub/
├── fshub/
│   ├── main.py           # click CLI (web / scan / config gen)
│   ├── web.py            # Flask app factory
│   ├── scanning.py       # shared scan logic for CLI + API
│   ├── api/
│   │   ├── explorer.py   # snapshot loading, path listing, group filters
│   │   ├── devices.py    # device registry
│   │   ├── scans.py      # background scans
│   │   ├── groups.py     # group membership (append-only action log)
│   │   ├── search.py     # name search across loaded snapshots
│   │   ├── backup.py     # folder / zip backups
│   │   └── hashes.py     # hashing and duplicate detection
│   ├── config/           # Config object + get_config() singleton
│   ├── templates/index.html
│   └── utils/            # system info, path helpers, formatting
├── tests/                # pytest suite
├── fshub.yaml.example
└── setup.py
```

## Security model

fshub has **no authentication**. It is designed to run on localhost, or behind
a reverse proxy that authenticates (nginx HTTP basic auth). Do not add
half-measures inside the app; keep the trust boundary at the proxy.

What the application *is* responsible for:

- **Never escaping its own data directory.** Any user-supplied name that ends
  up in a file path goes through `utils.sanitize_name` / `utils.safe_join`
  (`UnsafePathError` on violation). This covers snapshot names and group logs.
  Values that users do not choose freely (host names may contain spaces,
  quotes or non-ASCII text) are instead percent-encoded with
  `utils.encode_name_component`, which is injective and always produces a
  name that `sanitize_name` accepts — rejecting them outright would make the
  device registry unusable on perfectly normal machines.
- **Never writing outside a backup target.** Snapshot paths are converted with
  `utils.snapshot_relative_path`, which strips leading separators, Windows
  drive letters and `..` segments; `utils.ensure_within` double-checks the
  final destination.

## Configuration

`fshub.config.get_config()` returns a process-wide singleton. Never construct
`Config()` per request — the file is parsed once at startup. Derived paths are
properties: `snapshot_dir`, `devices_dir`, `backup_log_dir`, `scan_log_dir`.

## Snapshot format

A snapshot is a gzipped JSONL file, one record per directory:

| key | meaning                                   |
|-----|-------------------------------------------|
| `p` | absolute directory path                    |
| `f` | file names                                 |
| `s` | file sizes, parallel to `f`                |
| `t` | file `[ctime, mtime, atime]`, parallel to `f` |
| `d` | subdirectory names                         |
| `T` | subdirectory `[ctime, mtime, atime]`, parallel to `d` |

All timestamps are Unix integers. The first record additionally carries the
device info of the machine that produced it (`device_name`, `os_name`,
`thumbprint`, `start_scan_time`, ...).

`fshub scan --use-index` writes the same data split into
`<base>_index.jsonl.gz` (paths only) plus `<base>.bin.gz` (everything else);
`explorer._read_snapshot_records` transparently loads either layout.

### Computed fields

On load, `S` (total size) and `C` (total file count) are computed for every
directory, *including subdirectories*. This is done iteratively in
`_compute_recursive_totals`: an explicit child→parent map plus a memoised
depth sort. Do not turn this back into a recursive walk — real trees exceed
Python's recursion limit, and the Windows "This PC" root (`/` → `C:\`) has no
string-derivable parent.

The same rule applies to `filter_on_snapshot`, which walks the tree with an
explicit stack. Anything that traverses a snapshot must stay iterative.

## Cross-platform paths

A snapshot taken on Windows can be browsed from a Linux server, so **never use
`os.path` on snapshot paths**. Use the helpers in `fshub.utils`:

- `join_snapshot_path(base, *parts, snapshot_os=...)`
- `snapshot_dirname(path, snapshot_os=...)` — always returns a path that
  can appear in the snapshot index, so a root keeps its separator: the
  parent of `C:\Users` is `C:\`, not `C:`, and the parent of `/home` is
  `/`. Returning `C:` broke every `filter_in` selection under a Windows
  drive, because the ancestor chain never matched the indexed drive root.
- `snapshot_relative_path(path, snapshot_os)` — for backup destinations.
  Note it only treats `\` as a separator for Windows snapshots: POSIX allows
  backslashes and colons inside file names, and normalising them would make
  two distinct sources collide on one destination. `join_snapshot_path`
  follows the same rule — on POSIX it neither rewrites `\` in the base path
  nor strips it from a component, otherwise a directory named `a\b` silently
  merges with a real `a/b`.
- `explorer.to_web_path` / `explorer.from_web_path` — the UI only ever sees
  the normalized form (`C:\Users` ⇄ `/C:/Users`)

## Groups

Group membership is stored as an append-only action log next to the snapshot
(`<base>_groups.jl`), one JSON array per line:
`[path, "f"|"d", group_name, "add"|"del", timestamp]`. It is replayed into
`{group: {'f': set(), 'd': set()}}` on load. The log write and the in-memory
update happen together under `snapshots_lock`, log first, so concurrent
requests cannot persist in a different order than they were applied. Paths
arrive from the UI in web form and are converted with `from_web_path` before
they are stored, so a group entry always matches the snapshot index.

Filtering semantics (`explorer.filter_on_snapshot`):

- `filter_out` always wins; a directory in `filter_out` prunes its whole subtree.
- `filter_in` on a **directory** selects that whole subtree.
- `filter_in` on a **file** selects just that file; ancestors are kept
  traversable via `dirinFilterSet` so deep selections are still reachable.
  `dirinFilterSet` is only built for backup/hash selection
  (`get_filtered_files`); filtered *browsing* (`filter_path_content`) shows a
  directory only when it is itself in `filter_in`, so a browsed listing can
  hide an ancestor whose children a backup with the same filter still copies.

## Concurrency

Flask serves requests from multiple threads. Shared mutable state is guarded:

- `explorer.snapshots_lock` for `loaded_snapshots`
- `scans.scan_lock` for `running_scans`
- `backup.backup_lock` for `backup_tasks`

Both scan and backup task registries prune finished entries so they cannot
grow without bound. Worker threads catch exceptions and report them through
the task's `status`/`error` fields — never leave a task stuck in `running`.

Every scan also has an append-only `scan_logs/<scan_id>.jsonl` log. Lifecycle,
throttled progress, and each access error are flushed to it. The scan status
API reconstructs completed/failed tasks from these logs after process restart;
a log without a terminal event is presented as `interrupted`. Keep scan IDs as
server-generated canonical UUIDs because they are used as log filenames.

A backup worker only moves `started` → `running` when the task is still
`started` (compare-and-set), so a stop request that arrives before the thread
is scheduled is not overwritten. Per-file results are counted separately:
`completed_files` are copies that succeeded, `failed_files`/`errors` the ones
that did not, and a run with any failure finishes as `completed_with_errors`.

## Testing

```bash
pip install -r requirements.txt pytest
python -m pytest
```

The suite scans real temporary directories rather than mocking the filesystem.
Fixtures live in `tests/conftest.py`; `config` redirects `data_path` into a
tmpdir so tests never touch `~/.fshub`.

When adding a feature, cover at minimum:

1. the happy path through the HTTP API,
2. rejection of malformed input (missing JSON body, bad filter types),
3. rejection of path traversal if any user input reaches the filesystem.
