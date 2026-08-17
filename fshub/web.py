"""Web server module for fshub.

fshub performs no authentication of its own. It is meant to be exposed
through a reverse proxy (for example nginx with HTTP basic auth) whenever it
is reachable from anything but localhost.
"""

from flask import Flask, render_template, jsonify

from .config import get_config


def create_app():
    """Build the WSGI application. Usable directly by e.g. gunicorn."""
    app = Flask(__name__)

    get_config().ensure_dirs()

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/api/v1/health')
    def health():
        return jsonify({'status': 'ok'})

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

    return app


def start_web_server(host=None, port=None):
    app = create_app()
    config = get_config()

    # Use provided host/port or config defaults
    host = host or config.listen_ip
    port = int(port or config.listen_port)

    print(f"Starting fshub server on {host}:{port}")
    print(f"Data path: {config.data_path}")
    if host not in ('localhost', '127.0.0.1', '::1'):
        print("WARNING: fshub has no built-in authentication. Put it behind a "
              "reverse proxy (e.g. nginx HTTP basic auth) before exposing it.")

    app.run(host=host, port=port, debug=False)
