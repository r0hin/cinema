"""Title resolution and HTTP byte-range serving for local and downloading media."""
import http.client
import mimetypes
from pathlib import Path
import re
import socket
import ssl
import unicodedata
from urllib.parse import quote, unquote, urljoin, urlsplit

from rapidfuzz import fuzz
from rapidfuzz.distance import DamerauLevenshtein

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".ts", ".m2ts"}
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
NUMBERS = dict(ROMAN, one="1", two="2", three="3", four="4", five="5", six="6", seven="7", eight="8", nine="9", ten="10")


def normalize(text):
    text = unquote(text).casefold().replace("&", " and ")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    # Only a trailing release year is metadata. Keep numeric titles (1917,
    # 2001: A Space Odyssey) and the title number in Blade Runner 2049.
    match = re.search(r"(?:\s|\()((?:19|20)\d{2})\)?\s*$", text)
    year = None
    if match and text[:match.start()].strip() and ("(" in match[0] or int(match[1]) <= 2030):
        year = int(match[1])
        text = text[:match.start()]
    words = re.findall(r"[^\W_]+", text, re.UNICODE)
    if words and words[0] in ("the", "a", "an"):
        words = words[1:]
    return " ".join(NUMBERS.get(word, word) for word in words), year


def title_score(query, title):
    q, qyear = normalize(query)
    candidate, year = normalize(title)
    if not q or not candidate:
        return 0.0
    if q == candidate:
        score = 100.0
    else:
        if len(q) == 1:
            return 0.0
        # A transposition counts as one edit; combine whole-title similarity
        # with token order tolerance, without token_set's subset=100 behavior.
        edit = 100 * DamerauLevenshtein.normalized_similarity(q, candidate)
        whole = max(edit, fuzz.ratio(q, candidate), fuzz.token_sort_ratio(q, candidate))
        coverage = min(len(q), len(candidate)) / max(len(q), len(candidate))
        partial = fuzz.partial_ratio(q, candidate) * (0.72 + 0.18 * coverage)
        score = max(whole, partial)
        # Extra words matter: Odyssey should beat Odyssey of Life, including
        # when the short title has a typo but the longer title contains it.
        qwords, cwords = q.split(), candidate.split()
        if len(cwords) > len(qwords):
            score -= min(18, 4 * (len(cwords) - len(qwords)))
        qnumbers = {w for w in qwords if w.isdigit()}
        cnumbers = {w for w in cwords if w.isdigit()}
        if qnumbers != cnumbers:
            score -= 28
    if qyear:
        score += 8 if year == qyear else -min(30, 12 + abs((year or qyear - 30) - qyear))
    return round(score, 3)


def rank_titles(query, rows):
    ranked = []
    for row in rows:
        score = title_score(query, row["title"])
        _, year = normalize(row["title"])
        ranked.append(dict(row, score=score, year=year or 0))
    # Complete files win only exact title/year ties; a newer remake is the
    # default if no year was requested. A query's explicit year dominates.
    return sorted(ranked, key=lambda row: (row["score"], row["year"], row.get("status") == "complete", row.get("created", 0), row["id"]), reverse=True)


def byte_range(value, total):
    if not value:
        return 0, total - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()) or total <= 0:
        raise ValueError("Unsatisfiable byte range")
    first, last = match.groups()
    if not first:
        length = int(last)
        if not length:
            raise ValueError("Unsatisfiable byte range")
        start, end = max(0, total - length), total - 1
    else:
        start, end = int(first), min(int(last), total - 1) if last else total - 1
    if start >= total or start > end:
        raise ValueError("Unsatisfiable byte range")
    return start, end, True


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, address):
        super().__init__(host, port, timeout=30, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def open_source(url, method, headers, public_address):
    for _ in range(8):
        parsed, port, address = public_address(url)
        if parsed.scheme == "https":
            connection = PinnedHTTPS(parsed.hostname, port, address)
        else:
            connection = http.client.HTTPConnection(address, port, timeout=30)
        authority = parsed.netloc
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request(method, path, headers=dict(headers, Host=authority, **{"Accept-Encoding": "identity"}))
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            location = response.getheader("Location")
            response.close()
            connection.close()
            if not location:
                raise ValueError("Source returned an invalid redirect")
            url = urljoin(url, location)
            continue
        return connection, response
    raise ValueError("Too many source redirects")


class MediaGateway:
    def __init__(self, root, db, public_address):
        self.root, self.db, self.public_address = root, db, public_address

    def library(self):
        import hashlib
        import json
        with self.db() as con:
            rows = con.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()
        library = [dict(row, title=json.loads(row["payload"])["title"]) for row in rows]
        known = {row["folder"] for row in library}
        for file in self.root.rglob("*"):
            if not file.is_file() or file.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if file.relative_to(self.root).parts[0] in known:
                continue
            # Never publish a symlink pointing outside Movies.
            if self.root.resolve() not in file.resolve().parents:
                continue
            relative = str(file.relative_to(self.root))
            title = file.stem.replace(".", " ").replace("_", " ")
            year = re.search(r"\b((?:19|20)\d{2})\b", title)
            if year and title[:year.start()].strip():
                title = title[:year.start()].strip(" ()[]-") + " (" + year[1] + ")"
            else:
                title = re.split(r"\b(?:2160p|1080p|720p|WEB-DL|BluRay|x264|x265)\b", title, flags=re.I)[0].strip()
            library.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:32], "title": title,
                            "status": "complete", "created": file.stat().st_mtime, "file": file})
        return library

    def handle(self, handler, head=False):
        path = urlsplit(handler.path).path
        if path in ("/", "/favicon.ico", "/robots.txt"):
            handler.send_json(404, {"error": "Use /movie-title to play a saved or downloading movie"})
            return
        canonical = re.fullmatch(r"/media/([a-f0-9]{32})/(video|subtitles)", path)
        library = self.library()
        if canonical:
            row = next((row for row in library if row["id"] == canonical[1]), None)
            if not row:
                return handler.send_json(404, {"error": "Movie not found"})
            subtitle = canonical[2] == "subtitles"
        else:
            query = unquote(path.strip("/"))
            subtitle = query.endswith("/subtitles")
            if subtitle:
                query = query[:-10]
            if len(query) > 300 or not query or "/" in query or query in ("health", "downloads"):
                return handler.send_json(404, {"error": "Movie not found"})
            ranked = rank_titles(query, library)
            if not ranked or ranked[0]["score"] < 58:
                return handler.send_json(404, {"error": "No close movie title found"})
            row = ranked[0]
            # Freeze the match for all subsequent seek requests, even if a new
            # similarly titled movie enters the library during playback.
            handler.send_response(307)
            handler.send_header("Location", "/media/%s/%s" % (row["id"], "subtitles" if subtitle else "video"))
            handler.send_header("X-Cinema-Match", quote(row["title"]))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        if "file" in row:
            if subtitle:
                files = list(row["file"].parent.glob(row["file"].stem + ".en.*"))
                if not files:
                    return handler.send_json(404, {"error": "English subtitle file not found"})
                return self.local(handler, files[0], row, head)
            return self.local(handler, row["file"], row, head)
        folder = self.root / row["folder"]
        files = sorted(folder.glob("*.en.*")) if subtitle else sorted(p for p in folder.glob("*") if p.suffix.lower() in VIDEO_EXTENSIONS)
        if files:
            return self.local(handler, files[0], row, head)
        if subtitle:
            handler.send_response(503)
            handler.send_header("Retry-After", "5")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        # The original Stremio endpoint supports seeking before completion.
        # Forward its status, ranges and body through this stable media URL.
        import json
        source = json.loads(row["payload"])["videoUrl"]
        return self.proxy(handler, source, row, head)

    def local(self, handler, filename, row, head):
        stat = filename.stat()
        total = stat.st_size
        etag = '"%s-%s-%s"' % (row["id"], total, stat.st_mtime_ns)
        value = handler.headers.get("Range")
        if handler.headers.get("If-Range") not in (None, etag):
            value = None
        try:
            start, end, partial = byte_range(value, total)
        except ValueError:
            handler.send_response(416)
            handler.send_header("Content-Range", "bytes */%s" % total)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        handler.send_response(206 if partial else 200)
        handler.send_header("Content-Type", {".mkv": "video/x-matroska", ".srt": "application/x-subrip", ".vtt": "text/vtt; charset=utf-8", ".ass": "text/x-ssa"}.get(filename.suffix, mimetypes.guess_type(str(filename))[0] or "application/octet-stream"))
        handler.send_header("Content-Length", str(end - start + 1))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", "private, no-cache")
        handler.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(filename.name))
        handler.send_header("Link", '</media/%s/subtitles>; rel="alternate"; title="English subtitles"' % row["id"])
        if partial:
            handler.send_header("Content-Range", "bytes %s-%s/%s" % (start, end, total))
        handler.end_headers()
        if head:
            return
        with filename.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)

    def proxy(self, handler, source, row, head):
        forward = {name: handler.headers[name] for name in ("Range", "If-Range") if handler.headers.get(name)}
        connection = response = None
        started = False
        try:
            connection, response = open_source(source, "HEAD" if head else "GET", forward, self.public_address)
            if response.status not in (200, 206, 416):
                return handler.send_json(502, {"error": "Original stream is unavailable; retry from Cinema or wait for the download"})
            handler.send_response(response.status)
            for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                value = response.getheader(name)
                if value is not None:
                    handler.send_header(name, value)
            # Never relay origin cookies, Location, auth headers or a signed URL.
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Link", '</media/%s/subtitles>; rel="alternate"; title="English subtitles"' % row["id"])
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            started = True
            if not head:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except (OSError, ValueError, http.client.HTTPException):
            if not started:
                handler.send_json(502, {"error": "Cannot reach original stream"})
            handler.close_connection = True
        finally:
            if response:
                response.close()
            if connection:
                connection.close()
