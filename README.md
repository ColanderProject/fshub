# fshub - File System Hub

fshub is a Python package for managing files across multiple devices. It provides a web UI for exploring file systems, scanning for duplicates, organizing files into groups, and backing up files.

## Features

- Web-based file explorer
- Device management
- File scanning and hashing
- Group management
- Backup functionality
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
```

## Configuration

The configuration file is stored as `fshub.yaml` in the current directory or `~/.config/fshub.yaml`.

## License

MIT License