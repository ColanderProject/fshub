"""Web server module for fshub"""

from flask import Flask, render_template, request, jsonify, session
from flask_swagger_ui import get_swaggerui_blueprint
import os
from .config import Config
from .utils import get_system_info


def create_app():
    app = Flask(__name__)
    app.secret_key = os.urandom(24)
    
    # Load configuration
    config = Config()
    
    # Swagger UI setup
    SWAGGER_URL = '/api/docs'
    API_URL = '/api/spec'
    swaggerui_blueprint = get_swaggerui_blueprint(
        SWAGGER_URL,
        API_URL,
        config={'app_name': "fshub API"}
    )
    app.register_blueprint(swaggerui_blueprint, url_prefix=SWAGGER_URL)
    
    @app.route('/')
    def index():
        return render_template('index.html')
    
    @app.route('/api/spec')
    def api_spec():
        # Proper OpenAPI 3.0 specification
        return jsonify({
            "openapi": "3.0.0",
            "info": {
                "title": "fshub API",
                "description": "File System Hub API for managing files across devices",
                "version": "1.0.0"
            },
            "servers": [
                {
                    "url": "http://localhost:7303",
                    "description": "Development server"
                }
            ],
            "paths": {
                "/api/v1/login": {
                    "post": {
                        "summary": "Login to the system",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "password": {
                                                "type": "string",
                                                "description": "Login password"
                                            }
                                        },
                                        "required": ["password"]
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "Login successful",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "success": {"type": "boolean"}
                                            }
                                        }
                                    }
                                }
                            },
                            "401": {
                                "description": "Invalid credentials"
                            }
                        }
                    }
                },
                "/api/v1/devices": {
                    "get": {
                        "summary": "Get all known devices",
                        "responses": {
                            "200": {
                                "description": "List of devices",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "devices": {
                                                    "type": "array",
                                                    "items": {"$ref": "#/components/schemas/Device"}
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "post": {
                        "summary": "Add a new device",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Device"}
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "Device added successfully"
                            }
                        }
                    }
                },
                "/api/v1/scan": {
                    "post": {
                        "summary": "Start scanning a directory",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "path": {
                                                "type": "string",
                                                "description": "Path to scan"
                                            }
                                        },
                                        "required": ["path"]
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "Scan started",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "scan_id": {"type": "string"},
                                                "status": {"type": "string"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "Device": {
                        "type": "object",
                        "properties": {
                            "device_name": {"type": "string"},
                            "device_type": {"type": "string"},
                            "host_name": {"type": "string"},
                            "cpu_model": {"type": "string"},
                            "memory_size": {"type": "integer"},
                            "storage_space": {"type": "integer"},
                            "ip_addr": {"type": "string"},
                            "mac_addr": {"type": "string"},
                            "thumbprint": {"type": "string"}
                        }
                    }
                }
            }
        })
    
    @app.route('/api/v1/login', methods=['POST'])
    def login():
        if not config.require_password:
            session['logged_in'] = True
            return jsonify({'success': True})
        
        data = request.get_json()
        if data and data.get('password') == config.login_password:
            session['logged_in'] = True
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Invalid password'}), 401
    
    def require_auth(f):
        def decorated_function(*args, **kwargs):
            if config.require_password and not session.get('logged_in'):
                return jsonify({'error': 'Authentication required'}), 401
            return f(*args, **kwargs)
        decorated_function.__name__ = f.__name__
        return decorated_function
    
    # Import and register API routes
    from .api.devices import device_bp
    from .api.scans import scan_bp
    from .api.groups import group_bp
    from .api.search import search_bp
    from .api.backup import backup_bp
    from .api.explorer import explorer_bp
    from .api.hashes import hash_bp

    app.register_blueprint(device_bp)
    app.register_blueprint(scan_bp)
    app.register_blueprint(group_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(explorer_bp)
    app.register_blueprint(hash_bp)
    
    return app, config


def start_web_server(host=None, port=None):
    app, config = create_app()
    
    # Use provided host/port or config defaults
    host = host or config.listen_ip
    port = port or config.listen_port
    
    print(f"Starting fshub server on {host}:{port}")
    print(f"Data path: {config.data_path}")
    
    # Ensure data directory exists
    os.makedirs(config.data_path, exist_ok=True)
    os.makedirs(os.path.join(config.data_path, 'devices'), exist_ok=True)
    os.makedirs(os.path.join(config.data_path, 'snapshots'), exist_ok=True)
    
    app.run(host=host, port=port, debug=False)