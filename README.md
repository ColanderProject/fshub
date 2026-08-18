# fshub - File System Hub

fshub is a Python package for managing files across multiple devices. It provides a web UI for exploring file systems, scanning for duplicates, organizing files into groups, and backing up files.

## Features

- Web-based file explorer with recursive directory size / file counts
- Device management
- File scanning and hashing (including duplicate detection)
- Group management (include/exclude filters)
- Backup to a folder or to split zip archives
- Cross-platform support (Windows and Linux)

## Installation

```bash
pip install fshub
```

## Usage

```bash
# Start the web server
fshub web

# Generate a default configuration
fshub config gen

# Scan a directory from the command line
fshub scan /home/me --skip-path /home/me/.cache
```

`fshub web` is the localhost convenience runner. For a long running
deployment use any WSGI server:

```bash
gunicorn -b 127.0.0.1:7303 'fshub.web:create_app()'
```

## Configuration

The configuration file is read from `fshub.yaml` in the current directory, or
`~/.config/fshub.yaml`. See [`fshub.yaml.example`](fshub.yaml.example).

```yaml
data_path: ~/.fshub/
listen_ip: localhost
listen_port: 7303
```

## Security

**fshub performs no authentication of its own.** Every API endpoint is open to
anyone who can reach the port, and the API exposes file metadata as well as
scan and backup operations that write to disk.

- By default fshub binds to `localhost` only. Keep it that way.
- If you need remote access, put it behind a reverse proxy that handles
  authentication, e.g. nginx with HTTP basic auth:

```nginx
server {
    listen 443 ssl;
    server_name fshub.example.com;

    location / {
        auth_basic           "fshub";
        auth_basic_user_file /etc/nginx/.htpasswd;

        proxy_pass         http://127.0.0.1:7303;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Never expose fshub directly to an untrusted network.

## Data layout

Everything lives under `data_path`:

```
~/.fshub/
├── snapshots/   # snapshot_<ts>_<count>_<uuid>.jsonl.gz and their *_groups.jl logs
├── devices/     # devices_<host>.jl, media_<host>.jl
├── scan_logs/   # one append-only JSONL status/error log per scan run
└── backups/     # one JSONL log per backup run
```

Both backup types write *into* a target directory. Zip backups produce
`<backup_name>_<timestamp>_<uuid>_NNN.zip`, and archives are opened with mode
`x`, so running a backup twice into the same directory adds a new set rather
than overwriting the previous one — even for two runs started within the same
second. A run that could not copy every selected file finishes as
`completed_with_errors` and reports `failed_files`/`errors`; it never claims
success.

Web scans are asynchronous. The Scanner tab polls live progress and lists
historical runs. `GET /api/v1/scan-tasks` lists them,
`GET /api/v1/scan/<scan_id>` returns one status, and
`GET /api/v1/scan/<scan_id>/log` returns its lifecycle/progress/error records.
Scan logs are append-only files under `scan_logs/`; access errors are flushed
as they occur, and status remains queryable after a restart. A partial scan
finishes as `completed_with_errors`; a non-terminal log left by a killed
process is reported as `interrupted`.

Hashing is also asynchronous so a large snapshot does not occupy a web
worker. `POST /api/v1/hash/calculate` and `POST /api/v1/hash/duplicates`
return HTTP 202 with a `task_id`; poll `/api/v1/hash/status/<task_id>` until
its status is `completed` or `error`. Successful output is in `result`.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest
```

See [DEVELOPER.md](DEVELOPER.md) for architecture notes.

## License

MIT License
