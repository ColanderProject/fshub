# fshub - File System Hub

fshub is a Python package for managing files across multiple devices. It provides a web UI for exploring file systems, scanning for duplicates, organizing files into groups, and backing up files.

## Features

- Web-based file explorer
- Device management
- File scanning and hashing
- Group management
- Backup functionality
- Cross-platform support (Windows and Linux)
- Recursive directory size and file count display
- Filename truncation with popup for long names
- Unix timestamp support for file metadata
- Enhanced OS information display for devices

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

## New Features & Updates

### Explorer Tab Enhancements
- **Recursive Directory Size & File Counts**: Directories now display total size and total file count including all subdirectories
- **Unix Timestamp Support**: All file timestamps (created, modified, accessed) are stored as Unix timestamps instead of ISO strings
- **Timestamp Formatting**: Timestamps are displayed to the second without fractional parts

### Device Management Improvements
- **Automatic Device Detection**: System automatically checks if the current device is registered in the known devices list
- **OS Information Display**: Added OS name and version to device information display
- **Onboarding Flow**: Unregistered devices are automatically redirected to the device management tab with a registration form

### UI/UX Improvements
- **Long Filename Handling**: Long filenames are now truncated with ellipsis in table cells and can be viewed fully in a popup modal
- **Enhanced Device Information**: Added OS name, OS version, and OS release fields to device display

### Backend Changes
- **Unix Timestamp Adoption**: All timestamp values (including scan start/finish times) are now stored as Unix timestamps
- **Recursive Calculation**: Added recursive calculation functions for directory sizes and file counts
- **System Information**: Enhanced system information collection with OS details

## License

MIT License