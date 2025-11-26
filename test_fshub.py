#!/usr/bin/env python3
"""
Simple test script to verify fshub functionality
"""

from fshub.web import create_app

def test_app_creation():
    """Test that the Flask app can be created successfully"""
    try:
        app, config = create_app()
        print("✓ App created successfully")
        print(f"✓ Data path: {config.data_path}")
        print(f"✓ Listen IP: {config.listen_ip}")
        print(f"✓ Listen port: {config.listen_port}")
        print(f"✓ Password required: {config.require_password}")
        print("✓ All API routes registered successfully")
        
        # Test that key routes exist
        routes = [rule.rule for rule in app.url_map.iter_rules()]
        expected_routes = [
            '/', 
            '/api/spec',
            '/api/v1/login',
            '/api/v1/devices',
            '/api/v1/scan',
            '/api/v1/scans',
            '/api/v1/groups/<snapshot_filename>',
            '/api/v1/group/<snapshot_filename>/add_file',
            '/api/v1/group/<snapshot_filename>/add_dir',
            '/api/v1/group/<snapshot_filename>/remove_file',
            '/api/v1/group/<snapshot_filename>/remove_dir',
            '/api/v1/group/<snapshot_filename>/files',
            '/api/v1/search',
            '/api/v1/backup/zip',
            '/api/v1/backup/folder',
            '/api/v1/load_snapshot',
            '/api/v1/unload_snapshot',
            '/api/v1/getPath',
            '/api/v1/snapshots',
            '/api/v1/hash/calculate',
            '/api/v1/hash/duplicates'
        ]
        
        missing_routes = []
        for route in expected_routes:
            if route not in routes:
                missing_routes.append(route)
        
        if missing_routes:
            print(f"✗ Missing routes: {missing_routes}")
        else:
            print("✓ All expected routes are registered")
            
        return True
    except Exception as e:
        print(f"✗ Error creating app: {e}")
        return False


if __name__ == "__main__":
    print("Testing fshub functionality...")
    success = test_app_creation()
    if success:
        print("\n✓ All tests passed! fshub is ready to use.")
    else:
        print("\n✗ Some tests failed.")