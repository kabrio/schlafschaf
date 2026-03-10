"""
Schlafschaf – HTTP Web-Server
Stellt Schlafdaten als JSON-API bereit, damit die iOS App
die Daten direkt über WLAN herunterladen kann.

Verwendung:
  python3 web_server.py [--port 8080] [--host 0.0.0.0]
  Browser: http://<uno-q-ip>:8080/sessions
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from database import Database
from export import build_export
from analyzer import analyze_session

EXPORT_DIR = Path(__file__).parent.parent / 'data' / 'exports'
CHART_DIR = Path(__file__).parent.parent / 'data' / 'charts'


def make_json_response(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')


class SleepHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"[{ts}] {self.address_string()} {format % args}")

    def send_json(self, data, status=200):
        body = make_json_response(data)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_json({'error': 'Datei nicht gefunden'}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(data))
        self.send_header('Content-Disposition',
                         f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        db = Database()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        parts = [p for p in path.split('/') if p]

        try:
            # GET /  → Info
            if path in ('', '/'):
                self.send_json({
                    'name': 'Schlafschaf API',
                    'version': '1.0',
                    'device': 'Arduino Uno Q',
                    'endpoints': [
                        'GET /sessions',
                        'GET /sessions/<uuid>',
                        'GET /sessions/<uuid>/export',
                        'GET /sessions/<uuid>/chart',
                        'POST /sessions/<uuid>/analyze',
                    ]
                })

            # GET /sessions
            elif path == '/sessions':
                sessions = db.list_sessions()
                result = []
                for s in sessions:
                    epoch_count = len(db.get_epochs_for_session(s['id']))
                    result.append({
                        'id': s['id'],
                        'start_time': datetime.fromtimestamp(
                            s['start_time']).isoformat(),
                        'end_time': datetime.fromtimestamp(
                            s['end_time']).isoformat() if s['end_time'] else None,
                        'analyzed': epoch_count > 0,
                        'epoch_count': epoch_count,
                    })
                self.send_json(result)

            # GET /sessions/<uuid>
            elif len(parts) == 2 and parts[0] == 'sessions':
                session = db.get_session(parts[1])
                if not session:
                    self.send_json({'error': 'Session nicht gefunden'}, 404)
                    return
                epochs = db.get_epochs_for_session(parts[1])
                export = build_export(session, epochs) if epochs else None
                self.send_json({
                    'session': session,
                    'export': export,
                })

            # GET /sessions/<uuid>/export
            elif len(parts) == 3 and parts[0] == 'sessions' and parts[2] == 'export':
                session = db.get_session(parts[1])
                if not session:
                    self.send_json({'error': 'Session nicht gefunden'}, 404)
                    return
                epochs = db.get_epochs_for_session(parts[1])
                if not epochs:
                    self.send_json({'error': 'Keine Epochen. Erst /analyze aufrufen.'}, 400)
                    return
                export = build_export(session, epochs)
                # Als Datei-Download
                json_bytes = make_json_response(export)
                filename = f"schlafschaf_{parts[1][:8]}.json"
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', len(json_bytes))
                self.send_header('Content-Disposition',
                                 f'attachment; filename="{filename}"')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json_bytes)

            # GET /sessions/<uuid>/chart
            elif len(parts) == 3 and parts[0] == 'sessions' and parts[2] == 'chart':
                CHART_DIR.mkdir(parents=True, exist_ok=True)
                chart_path = CHART_DIR / f"{parts[1]}.png"
                if not chart_path.exists():
                    # Chart on-demand generieren
                    db.close()
                    subprocess.run([
                        sys.executable, 'visualize.py',
                        '--session', parts[1],
                        '--output', str(chart_path)
                    ], cwd=os.path.dirname(__file__))
                    db = Database()
                self.send_file(chart_path, 'image/png')

            else:
                self.send_json({'error': 'Endpoint nicht gefunden'}, 404)

        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            try:
                db.close()
            except Exception:
                pass

    def do_POST(self):
        db = Database()
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.strip('/').split('/') if p]

        try:
            # POST /sessions/<uuid>/analyze
            if len(parts) == 3 and parts[0] == 'sessions' and parts[2] == 'analyze':
                session = db.get_session(parts[1])
                if not session:
                    self.send_json({'error': 'Session nicht gefunden'}, 404)
                    return
                epochs = analyze_session(parts[1], db)
                self.send_json({
                    'session_id': parts[1],
                    'epochs_analyzed': len(epochs),
                })
            else:
                self.send_json({'error': 'Endpoint nicht gefunden'}, 404)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            try:
                db.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description='Schlafschaf Web-Server')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--db', default=None)
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), SleepHandler)
    print(f"Schlafschaf Web-Server läuft auf http://{args.host}:{args.port}")
    print(f"iOS App: http://<uno-q-ip>:{args.port}/sessions")
    print("Ctrl+C zum Beenden\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")


if __name__ == '__main__':
    main()
