#!/usr/bin/env python3
"""Cinema's authenticated, persistent download queue. Python stdlib + curl."""
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit, unquote
import zipfile
from media import MediaGateway

ROOT = Path(os.environ.get("CINEMA_MOVIES", str(Path.home() / "Movies")))
STATE = Path(os.environ.get("CINEMA_STATE", str(Path.home() / ".local/state/cinema-downloads")))
TOKEN = os.environ.get("CINEMA_DOWNLOAD_TOKEN", "")
WAKE = threading.Event()


def db():
    connection = sqlite3.connect(str(STATE / "jobs.sqlite"), timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def initialize():
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT, status TEXT, message TEXT, folder TEXT, created REAL)")
        con.execute("UPDATE jobs SET status='queued', message='Resuming after restart' WHERE status='downloading'")


def public_address(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTP or HTTPS download links are supported")
    if len(url) > 8192 or any(ord(c) < 32 for c in url):
        raise ValueError("Invalid download link")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {result[4][0] for result in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Download links must point to public internet addresses")
    return parsed, port, sorted(addresses, key=lambda ip: ":" in ip)[0]


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise ValueError("A movie title is required (maximum 300 characters)")
    for key in ("videoUrl", "subtitleUrl"):
        if not isinstance(payload.get(key), str):
            raise ValueError("Both a video and an English subtitle link are required")
        public_address(payload[key])
    delay = payload.get("subtitleDelay", 0)
    if not isinstance(delay, (float, int)) or abs(delay) > 3600000:
        raise ValueError("Invalid subtitle delay")
    return {"title": title, "videoUrl": payload["videoUrl"], "subtitleUrl": payload["subtitleUrl"],
            "subtitleDelay": delay, "filename": str(payload.get("filename", ""))[:500]}


def update(job_id, status, message):
    with db() as con:
        con.execute("UPDATE jobs SET status=?, message=? WHERE id=?", (status, message, job_id))


def enqueue(payload):
    payload = validate(payload)
    identity = json.dumps([payload[k] for k in ("title", "videoUrl", "subtitleUrl", "subtitleDelay")], ensure_ascii=False)
    job_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
    title = re.sub(r'[^\w .()\[\]-]', "_", payload["title"], flags=re.UNICODE).strip(" .")[:140] or "Movie"
    folder = title + " [" + job_id[:8] + "]"
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] in ("queued", "downloading", "complete"):
            return job_id
        if con.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','downloading')").fetchone()[0] >= 20:
            raise ValueError("The download queue is full; wait for a download to finish")
        con.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)", (
            job_id, json.dumps(payload), "queued", "Queued on compute-alt", folder, time.time()))
    WAKE.set()
    return job_id


def status(job_id):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    folder = ROOT / row["folder"]
    size = sum(p.stat().st_size for p in folder.glob("*") if p.is_file()) if folder.exists() else 0
    message = row["message"]
    if row["status"] == "downloading" and size:
        message += " (%.1f MB saved)" % (size / 1000000)
    return {"id": job_id, "status": row["status"], "message": message,
            "folder": str(folder), "bytes": size}


def transfer(url, destination, max_size):
    """Resolve and pin each redirect to a public IP; curl never follows redirects itself."""
    for _ in range(8):
        parsed, port, address = public_address(url)
        headers = destination.with_suffix(".headers")
        resolve = "%s:%s:%s" % (parsed.hostname, port, "[" + address + "]" if ":" in address else address)
        command = ["curl", "--silent", "--show-error", "--fail", "--noproxy", "*", "--proto", "=http,https",
                   "--resolve", resolve, "--connect-timeout", "20", "--max-time", "14400",
                   "--speed-limit", "1024", "--speed-time", "120", "--max-filesize", str(max_size),
                   "--dump-header", str(headers), "--output", str(destination), "--write-out", "%{http_code}"]
        # Resume partial files, including after a service restart.
        if destination.exists() and destination.stat().st_size:
            command += ["--continue-at", "-"]
        result = subprocess.run(command + [url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first_code = int(result.stdout.decode()[-3:] or "0")
        if (result.returncode == 33 or first_code == 416) and "--continue-at" in command:
            # The source cannot resume, or the whole file was already saved
            # before a restart. A fresh request also verifies its current bytes.
            destination.unlink(missing_ok=True)
            result = subprocess.run(command[:command.index("--continue-at")] + [url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        text = headers.read_text(errors="replace") if headers.exists() else ""
        headers.unlink(missing_ok=True)
        code = int(result.stdout.decode()[-3:] or "0")
        if 300 <= code < 400:
            location = re.findall(r"(?im)^location:\s*(.*?)\r?$", text)
            if not location:
                raise ValueError("Download source returned a redirect without a location")
            destination.unlink(missing_ok=True)
            url = urljoin(url, location[-1])
            continue
        if result.returncode or code not in (200, 206):
            # Do not put signed URLs or source credentials into API errors/logs.
            raise ValueError("Source download failed (HTTP %s, curl %s); retry the save" % (code, result.returncode))
        if not destination.exists() or not destination.stat().st_size:
            raise ValueError("Source returned an empty file")
        if destination.stat().st_size > max_size:
            raise ValueError("Download exceeded the file size limit")
        return text
    raise ValueError("Download source redirected too many times")


def subtitle_file(data, delay):
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
            data = archive.read(10 * 1024 * 1024 + 1)
    if data.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [i for i in archive.infolist() if not i.is_dir() and re.search(r"\.(srt|vtt|ass|ssa)$", i.filename, re.I)]
            if len(files) != 1 or files[0].file_size > 10 * 1024 * 1024:
                raise ValueError("Subtitle archive must contain a single subtitle file")
            data = archive.read(files[0])
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("Subtitle file is too large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "cp1252")
    extension = "vtt" if text.lstrip().startswith("WEBVTT") else "ass" if "[Script Info]" in text else "srt"
    timestamp = r"(?:\d{1,3}:)?\d{2}:\d{2}(?:[.,]\d{1,3})?"
    timing = re.compile(r"(?m)^\s*(" + timestamp + r")\s*-->\s*(" + timestamp + r")([^\r\n]*)$")
    if extension == "srt" and not timing.search(text):
        raise ValueError("The English subtitle source did not return a supported subtitle file")
    if delay and extension == "ass":
        raise ValueError("ASS subtitles with a playback delay are not supported; set the delay to zero and retry")
    if extension in ("srt", "vtt"):
        def shift(value):
            parts = re.split(r"[.,]", value)
            seconds = 0
            for component in parts[0].split(":"):
                seconds = seconds * 60 + int(component)
            milliseconds = max(0, seconds * 1000 + int((parts[1] if len(parts) > 1 else "0").ljust(3, "0")) + int(delay))
            hours, milliseconds = divmod(milliseconds, 3600000)
            minutes, milliseconds = divmod(milliseconds, 60000)
            seconds, milliseconds = divmod(milliseconds, 1000)
            return "%02d:%02d:%02d%s%03d" % (hours, minutes, seconds, "," if extension == "srt" else ".", milliseconds)
        text = timing.sub(lambda match: shift(match[1]) + " --> " + shift(match[2]) + match[3], text)
    return extension, text.encode("utf-8")


def run_job(row):
    job_id = row["id"]
    payload = json.loads(row["payload"])
    folder = ROOT / row["folder"]
    folder.mkdir(exist_ok=True)
    basename = row["folder"].rsplit(" [", 1)[0]
    try:
        update(job_id, "downloading", "Saving English subtitles")
        part = folder / "subtitle.part"
        transfer(payload["subtitleUrl"], part, 10 * 1024 * 1024)
        extension, data = subtitle_file(part.read_bytes(), payload["subtitleDelay"])
        (folder / (basename + ".en." + extension)).write_bytes(data)
        part.unlink()
        update(job_id, "downloading", "Saving video to compute-alt")
        part = folder / "video.part"
        free = shutil.disk_usage(ROOT).free + (part.stat().st_size if part.exists() else 0) - 1024 ** 3
        if free < 100 * 1024 * 1024:
            raise ValueError("Not enough free disk space on compute-alt")
        headers = transfer(payload["videoUrl"], part, free)
        with part.open("rb") as video:
            magic = video.read(32)
        if magic.lstrip().startswith((b"#EXTM3U", b"<", b"{")):
            raise ValueError("This source is a playlist or web page; choose a direct video stream")
        filename = payload["filename"] or unquote(urlsplit(payload["videoUrl"]).path)
        ext = Path(filename).suffix.lower()
        if magic.startswith(b"\x1aE\xdf\xa3"):
            ext = ".webm" if ext == ".webm" else ".mkv"
        elif magic[4:8] == b"ftyp":
            ext = ".mp4"
        elif ext not in (".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".ts", ".m2ts"):
            ext = ".mp4" if "video/mp4" in headers.lower() else ".mkv"
        part.replace(folder / (basename + ext))
        update(job_id, "complete", "Saved video and English subtitles in Movies/" + row["folder"])
    except Exception as error:
        update(job_id, "failed", str(error) if isinstance(error, ValueError) else "Download interrupted; retry the save")


def worker():
    while True:
        with db() as con:
            row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if row:
            run_job(row)
        else:
            WAKE.wait(2)
            WAKE.clear()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass  # Avoid logging signed source URLs or bearer tokens.

    def send_json(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def authorized(self):
        if not TOKEN or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN):
            self.send_json(401, {"error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self.path.startswith(("/downloads", "/health", "/resolve")):
            try:
                return MediaGateway(ROOT, db, public_address).handle(self)
            except (BrokenPipeError, ConnectionResetError):
                return
        if not self.authorized():
            return
        if self.path == "/health":
            return self.send_json(200, {"status": "ok", "target": "compute-alt", "folder": str(ROOT)})
        if self.path.startswith("/resolve/"):
            from media import rank_titles
            from urllib.parse import unquote
            ranked = rank_titles(unquote(self.path[len("/resolve/"):]), MediaGateway(ROOT, db, public_address).library())
            return self.send_json(200, {"matches": [{k: row[k] for k in ("id", "title", "score", "status")} for row in ranked[:5]]})
        if re.fullmatch(r"/downloads/[a-f0-9]{32}", self.path):
            value = status(self.path.rsplit("/", 1)[1])
            return self.send_json(200 if value else 404, value or {"error": "Download not found"})
        self.send_json(404, {"error": "Not found"})

    def do_HEAD(self):
        if self.path.startswith(("/downloads", "/health", "/resolve")):
            return self.do_GET()
        try:
            MediaGateway(ROOT, db, public_address).handle(self, head=True)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/downloads":
            return self.send_json(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 32768:
                raise ValueError("Invalid request size")
            job_id = enqueue(json.loads(self.rfile.read(length)))
            self.send_json(202, status(job_id))
        except (ValueError, OSError) as error:
            self.send_json(400, {"error": str(error) if isinstance(error, ValueError) else "Cannot resolve the download source"})


if __name__ == "__main__":
    if len(TOKEN) < 32:
        raise SystemExit("Set CINEMA_DOWNLOAD_TOKEN to at least 32 characters")
    initialize()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8766"))), Handler).serve_forever()
