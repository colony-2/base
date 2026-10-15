"""Serve unchanged signed cache files, optionally pause one NAR mid-transfer."""
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory='/fixture/binary', **kwargs)

    def log_message(self, *_):
        pass

    def copyfile(self, source, output):
        pause = Path('/coord/pause-url')
        if pause.exists() and self.path.lstrip('/') == pause.read_text().strip():
            chunk = source.read(1024)
            output.write(chunk)
            output.flush()
            Path('/coord/paused').touch()
            deadline = time.monotonic() + 40
            while not Path('/coord/release').exists():
                if time.monotonic() > deadline:
                    return
                time.sleep(0.02)
        try:
            super().copyfile(source, output)
        except (BrokenPipeError, ConnectionResetError):
            pass


ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
