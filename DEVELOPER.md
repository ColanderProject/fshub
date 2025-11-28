# Developer Documentation for fshub

## Overview

fshub is a file system hub application that provides a web-based interface for managing files across multiple devices. The application allows users to explore file systems, scan for duplicates, organize files into groups, and perform backups.

## Project Structure

```
fshub/
├── fshub/
│   ├── __init__.py
│   ├── main.py           # Main application entry point
│   ├── web.py            # Web server implementation
│   ├── api/              # API endpoints
│   │   ├── __init__.py
│   │   ├── explorer.py   # File explorer API
│   │   ├── devices.py    # Device management API
│   │   ├── scans.py      # File scanning API
│   │   └── ...
│   ├── config/           # Configuration module
│   ├── static/           # Static files
│   ├── templates/        # HTML templates
│   │   └── index.html    # Main web interface
│   └── utils/            # Utility functions
│       └── __init__.py
├── README.md
├── setup.py
└── ...
```

## Key Features & Implementation Details

### Recursive Directory Size and File Count

#### Backend Implementation (`fshub/api/explorer.py`)
- The `calculate_sub_counts` function now uses recursion to calculate total size and file count for each directory
- Directories store their total size in the 'S' field and total file count in the 'C' field
- These values include all subdirectories recursively
- Calculations happen each time a snapshot is loaded

#### Frontend Implementation (`fshub/templates/index.html`)
- Directory table now shows "Total Size" and "Total Files" columns
- Uses `formatBytes` function for human-readable size display
- Displays both S (size) and C (count) values for each directory

### Timestamp Handling

#### Unix Timestamp Conversion
- All file timestamps (created, modified, accessed) are now stored as Unix timestamps (integers)
- Scan start/finish times are stored as Unix timestamps
- Device registration timestamps use Unix format
- Backend APIs in `scans.py` and `devices.py` now use Unix timestamps

#### Frontend Timestamp Formatting (`fshub/templates/index.html`)
- `formatTimestamp` function handles both ISO strings and Unix timestamps
- Automatically detects Unix timestamps and converts them to readable format
- Truncates fractional seconds for cleaner display

### Device Management Enhancement

#### Automatic Device Detection
- `checkLoginStatus` function checks if the current device is known
- If unknown, automatically redirects to the devices tab
- Shows registration form for unknown devices

#### OS Information Collection
- System information function now collects OS name, version, and release
- `get_system_info` function in both `utils/__init__.py` and `api/devices.py`
- Enhanced thumbprint calculation includes OS information for uniqueness

### Filename Truncation & Popup Display

#### CSS Implementation
- `.filename-cell` class limits width to 200px with ellipsis overflow
- Hover effects and cursor indicators for user experience

#### JavaScript Implementation
- `showFullFilenamePopup` function displays Bootstrap modal with full name
- Applied to all filename displays: directories, files, scan results, search results
- Maintains search functionality while truncating display

### API Endpoints

#### Explorer API (`fshub/api/explorer.py`)
- `/api/v1/load_snapshot` - Load snapshot and calculate recursive totals
- `/api/v1/getPath` - Get path content with S and C fields
- Updated to return recursive directory information

#### Device API (`fshub/api/devices.py`)
- `/api/v1/devices` (GET) - Get all devices with OS information
- `/api/v1/devices` (POST) - Add new device with OS data
- System info now includes OS fields

#### Scan API (`fshub/api/scans.py`)
- All timestamps now stored as Unix timestamps (created, modified, accessed, start/end times)
- Scan results timestamps use Unix format

## Frontend Changes

### Template Updates (`fshub/templates/index.html`)

#### Table Structure Changes
- Directory table now has Total Size and Total Files columns
- Added CSS classes for filename truncation
- Added popup modal element

#### JavaScript Enhancements
- Recursive directory size display functionality
- Filename truncation with popup display
- Enhanced device registration flow
- Unix timestamp formatting support

## Data Storage Format

### Snapshot Files
- Store file timestamps as Unix timestamps (integers)
- Directory objects include S (total size) and C (total file count) fields calculated at load time
- These fields are not stored in the file but computed on load

### Device Files
- Store OS information (os_name, os_version, os_release)
- Unix timestamps for device registration and update times
- Enhanced thumbprint calculation includes OS identifiers

## Development Workflow

### Adding New Features
1. Update backend APIs in `fshub/api/` to support new functionality
2. Modify frontend templates in `fshub/templates/index.html` to display new data
3. Add corresponding JavaScript functions for client-side functionality
4. Test with both fresh and existing snapshots

### Testing Considerations
- Verify timestamp display works with both old and new Unix timestamp format
- Ensure recursive calculations work correctly for nested directory structures
- Test device registration flow on new systems
- Confirm filename truncation works across different browsers

## Migration Notes

### From ISO Timestamps to Unix Timestamps
- Existing snapshots will continue to work as timestamp formatting is handled dynamically
- New snapshots will store timestamps as Unix timestamps
- The `formatTimestamp` function handles both formats seamlessly

### Recursive Directory Calculations
- Directory S and C values are calculated at runtime, not stored in snapshot files
- Performance impact is minimal due to depth-first recursive calculation
- Calculations are done only once per snapshot load

## Error Handling

- Timestamp formatting gracefully handles missing or invalid values
- Filename popup handles null or empty values safely
- Device detection includes fallback for missing system information
- Directory recursion includes safeguards against circular references (though not expected in file systems)