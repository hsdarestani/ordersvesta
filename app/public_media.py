import mimetypes
import re
from urllib.parse import unquote, urlparse

from app import main as m

MEDIA_DIR = m.DATA / 'bridge_media'
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
NAME_RE = re.compile(r'^[a-f0-9]{48}\.(?:jpg|jpeg|png|webp)$', re.I)


class BridgeHTTP(m.Health):
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path.startswith('/media/'):
            name = path.rsplit('/', 1)[-1]
            if not NAME_RE.fullmatch(name):
                self.send_error(404)
                return
            file_path = MEDIA_DIR / name
            if not file_path.is_file():
                self.send_error(404)
                return
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'private, max-age=60')
            self.send_header('X-Robots-Tag', 'noindex, nofollow')
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()
