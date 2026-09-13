"""Expose only the batch metrics file; never serve reports or a directory listing."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def handler(path):
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                self.send_error(503, "No batch metrics yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_):
            pass
    return MetricsHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()
    ThreadingHTTPServer((args.bind, args.port), handler(args.file)).serve_forever()


if __name__ == "__main__":
    main()
