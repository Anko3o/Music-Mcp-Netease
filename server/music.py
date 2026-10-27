#!/usr/bin/env python3
"""music — standalone music server for Netease Cloud Music.

Zero external dependencies (Python stdlib only). Handles:
  - Song search, audio URL resolution with CDN fallback, audio streaming
  - Lyrics with translation caching (.lrc + .tlyric)
  - Playlist CRUD (single default + multi-playlist system)
  - Recent play history
  - Music profile (avatar, signature, background)
  - Daily recommendations (based on liked songs)
  - Song memory / notes system
  - Listening stats
  - Roam mode (random genre discovery)
  - Similar song discovery
  - Remote play (push a song to another client)
  - Background audio analysis (via analyze_song.py subprocess)
  - Listen-complete tracking (together count)
  - Static file serving for cached mp3s and frontend

Usage:
    python3 server/music.py                     # port 9090
    PORT=8080 python3 server/music.py           # custom port

Data layout:
    ./data/music_cache/    — cached mp3, lrc, tlyric, analysis files
    ./data/music_data.json — playlists, recent, profile
    ./data/music_memory.json — per-song memory (notes, listen counts)
    ./data/music_playlist.json — legacy flat playlist (synced with liked)
    ./data/music_remote.json — ephemeral remote-play payload
    ./.secret              — auto-generated auth token
    ./.netease_cred        — MUSIC_U=<cookie> (one line)
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import random
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.request
import urllib.error

HERE = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("music")


# ── Secret management ────────────────────────────────────────────────────────

def _load_or_create_secret() -> str:
    secret_file = HERE / ".secret"
    try:
        if secret_file.exists():
            s = secret_file.read_text().strip()
            if s:
                return s
        new_secret = secrets.token_hex(32)
        secret_file.write_text(new_secret)
        secret_file.chmod(0o600)
        logger.info("Auto-generated shared secret saved to %s", secret_file)
        return new_secret
    except Exception as e:
        logger.warning("Could not auto-generate secret: %s", e)
        return ""


# ── Request handler ──────────────────────────────────────────────────────────

class MusicHandler(BaseHTTPRequestHandler):
    state: "ServerState"

    server_version = "Music/1.1"

    def log_message(self, fmt, *args):
        # 访问日志不记查询串（搜索词、粘进来的分享链接都在里面），只留路径；状态码照记。
        line = fmt % args
        if "?" in line:
            head, _, tail = line.partition("?")
            rest = tail.split(" ", 1)
            line = head + "?…" + (" " + rest[1] if len(rest) > 1 else "")
        logger.info("%s %s", self.address_string(), line)

    # ── Helpers ──

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _check_auth(self) -> bool:
        # 反代部署：公网请求先经过同站 basic_auth，Caddy 再加固定的内部标记。
        # 后端只监听 loopback；同时校验来源地址和标记，避免浏览器保存第二枚 music token。
        if (
            self.client_address[0] in {"127.0.0.1", "::1"}
            and self.headers.get("X-Music-Gateway", "") == os.environ.get("MUSIC_GATEWAY_TOKEN", "music-gateway")
        ):
            return True
        if not self.state.shared_secret:
            return True
        token = self.headers.get("X-Auth-Token", "") or self.headers.get("X-Auth", "")
        if not token:
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get("token") or [""])[0]
        return token == self.state.shared_secret

    def _require_auth(self) -> bool:
        if self._check_auth():
            return True
        self._send_json(403, {"error": "auth required"})
        return False

    def _send_json(self, status: int, body: dict[str, Any]):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Auth")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, file_path: Path, content_type: str | None = None):
        """Serve a file with proper headers and Range support."""
        if not file_path.exists() or not file_path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        st = file_path.stat()
        size = st.st_size
        if content_type is None:
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        # 静态文件带 ETag：浏览器每次问一声，没变吃 304，变了立刻拿新的
        etag = f'"{st.st_mtime_ns:x}-{size:x}"'
        if self.headers.get("If-None-Match", "") == etag and not self.headers.get("Range"):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.end_headers()
            return

        # Range request support (needed for audio seeking)
        range_header = self.headers.get("Range")
        if range_header:
            try:
                range_spec = range_header.replace("bytes=", "")
                start_str, end_str = range_spec.split("-", 1)
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            except Exception:
                pass  # Fall through to full response

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Auth")
        self.end_headers()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, X-Auth, Range")
        self.end_headers()

    # ── Netease helpers ──

    # 登录探针：MUSIC_U 过期时海外 IP 什么都取不到，前端只会报「没取到播放源」。这里问一句「我是谁」（60 秒缓存一次），
    # 过期就明说。问不到网易就当没过期，别乱报。
    def _netease_logged_in(self, ttl: int = 60) -> bool:
        cache = getattr(self.state, "_login_probe", None)
        now = time.time()
        if cache and now - cache[1] < ttl:
            return cache[0]
        try:
            a = self._netease_request("https://music.163.com/api/nuser/account/get", timeout=6)
            ok = bool(a.get("account")) or bool(a.get("profile"))
        except Exception:
            ok = True
        try:
            self.state._login_probe = (ok, now)
        except Exception:
            pass
        return ok

    def _netease_cookie(self) -> str:
        cred = HERE / ".netease_cred"
        try:
            for line in cred.read_text().splitlines():
                if line.startswith("MUSIC_U="):
                    return f"MUSIC_U={line.split('=', 1)[1].strip()}; os=pc"
        except OSError:
            pass
        return ""

    def _netease_request(self, url: str, data: bytes | None = None,
                         extra_headers: dict[str, str] | None = None,
                         timeout: int = 10) -> Any:
        """Make an authenticated request to Netease API and return parsed JSON."""
        headers = {
            "Cookie": self._netease_cookie(),
            "Referer": "https://music.163.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if extra_headers:
            headers.update(extra_headers)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _ensure_cover(self, song_id, cover: str = "") -> str:
        if cover:
            return cover
        try:
            url = f"https://music.163.com/api/song/detail?ids=[{song_id}]"
            d = self._netease_request(url)
            return d.get("songs", [{}])[0].get("album", {}).get("picUrl", "")
        except Exception:
            return ""

    # ── Data helpers ──

    def _playlist_path(self) -> Path:
        return self.state.data_dir / "music_playlist.json"

    def _load_playlist(self) -> list:
        p = self._playlist_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return []

    def _save_playlist(self, songs: list):
        p = self._playlist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(songs, ensure_ascii=False))

    def _music_data_path(self) -> Path:
        return self.state.data_dir / "music_data.json"

    def _load_music_data(self) -> dict:
        p = self._music_data_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        # Bootstrap from legacy playlist
        old = self._load_playlist()
        data = {
            "playlists": [{"id": "liked", "name": "Liked", "songs": old}],
            "recent": [],
            "profile": {"avatar": "", "signature": "", "bg": ""},
        }
        self._save_music_data(data)
        return data

    def _save_music_data(self, data: dict):
        p = self._music_data_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1))

    def _song_memory_path(self) -> Path:
        return self.state.data_dir / "music_memory.json"

    def _load_song_memory(self) -> dict:
        p = self._song_memory_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return {}

    def _save_song_memory(self, mem: dict):
        p = self._song_memory_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(mem, ensure_ascii=False, indent=1))

    # ── Audio download with CDN fallback ──

    def _download_audio(self, audio_url: str, cache_file: Path):
        """Download audio to cache_file with CDN fallback for overseas servers."""
        def _dl(dl_url: str):
            areq = urllib.request.Request(dl_url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://music.163.com",
                "Cookie": self._netease_cookie(),
            })
            tmp = cache_file.with_suffix(".tmp")
            with urllib.request.urlopen(areq, timeout=120) as aresp:
                with open(tmp, "wb") as f:
                    while True:
                        chunk = aresp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
            tmp.rename(cache_file)

        try:
            _dl(audio_url)
        except urllib.error.HTTPError:
            # CDN fallback: m*.music.126.net -> m701.music.126.net
            fallback = re.sub(r'm\d+\.music\.126\.net', 'm701.music.126.net', audio_url)
            _dl(fallback)

    def _fetch_music_url(self, song_id) -> bool:
        """Ensure audio is cached, return True if available."""
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            return True
        try:
            url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=128000"
            raw = self._netease_request(url)
            audio_url = (raw.get("data") or [{}])[0].get("url")
            if not audio_url:
                return False
            self._download_audio(audio_url, cache_file)
            return cache_file.exists() and cache_file.stat().st_size > 1000
        except Exception:
            return False

    # ── GET routes ────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Health check (no auth)
        if path == "/health":
            self._send_json(200, {"ok": True, "version": "1.0", "service": "music"})
            return

        # Static: cached music files.
        # 这里照常要 token：网易云的 song ID 是公开的，谁都能枚举，免鉴权等于把缓存过的歌变成公开下载站。
        # <audio> 标签带不了自定义 header，所以前端走 ?token= 查询参数（_check_auth 本来就认）。
        if path.startswith("/music/file/"):
            if not self._require_auth():
                return
            self._serve_music_file(path)
            return

        # Static: frontend files from ../client/
        if path == "/" or not path.startswith("/music") and not path.startswith("/health"):
            # Serve frontend static files
            self._serve_static(path)
            return

        # 卡片图：免 token——内容全是网易公开数据（封面/歌名/一句歌词），
        # 且官端聊天窗的 <img> 带不了自定义 header。见 _handle_music_card。
        if path == "/music/card":
            self._handle_music_card()
            return

        # All /music/* endpoints below require auth
        if not self._require_auth():
            return

        if path == "/music/search":
            self._handle_music_search()
        elif path == "/music/avatar/fog":        # AI 默认头像：有 client/fog-avatar.png 就用它，没有画个音符占位
            f = HERE.parent / "client" / "fog-avatar.png"
            if f.exists():
                self._send_file(f, "image/png")
            else:
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><rect width="200" height="200" rx="100" fill="#f3e9ee"/>'
                       '<path d="M84 136V62l56-12v74" fill="none" stroke="#984e68" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>'
                       '<circle cx="70" cy="136" r="14" fill="#984e68"/><circle cx="126" cy="124" r="14" fill="#984e68"/></svg>')
                data = svg.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        elif path == "/music/url":
            self._handle_music_url()
        elif path == "/music/stream":
            self._handle_music_stream()
        elif path == "/music/lyric":
            self._handle_music_lyric()
        elif path == "/music/comments":
            self._handle_music_comments()
        elif path == "/music/cover":
            self._handle_music_cover()
        elif path == "/music/mv":
            self._handle_music_mv()
        elif path == "/music/netease/playlists":
            self._handle_netease_playlists()
        elif path == "/music/netease/playlist":
            self._handle_netease_playlist()
        elif path == "/music/netease/daily":
            self._handle_netease_daily()
        elif path == "/music/netease/likes":
            self._handle_netease_likes_get()
        elif path == "/music/netease/record":
            self._handle_netease_record()
        elif path == "/music/netease/profile":
            self._handle_netease_profile()
        elif path == "/music/netease/status":
            self._handle_netease_status()
        elif path == "/music/skin/bg":
            self._handle_skin_bg_get()
        elif path == "/music/netease/qr/new":
            self._handle_netease_qr_new()
        elif path == "/music/netease/qr/check":
            self._handle_netease_qr_check()
        elif path == "/music/playlist":
            self._handle_music_playlist_get()
        elif path == "/music/playlists":
            self._handle_music_playlists_list()
        elif path == "/music/playlists/songs":
            self._handle_music_playlists_songs()
        elif path == "/music/recent":
            self._handle_music_recent_get()
        elif path == "/music/profile":
            self._handle_music_profile_get()
        elif path == "/music/daily":
            self._handle_music_daily()
        elif path == "/music/memory":
            self._handle_music_memory_get()
        elif path == "/music/memory/noted":
            self._handle_music_memory_noted()
        elif path == "/music/stats":
            self._handle_music_stats()
        elif path == "/music/roam":
            self._handle_music_roam()
        elif path == "/music/similar":
            self._handle_music_similar()
        elif path == "/music/remote":
            self._handle_music_remote_get()
        elif path == "/music/remote/peek":
            self._handle_music_remote_peek()
        elif path == "/music/now":
            self._handle_music_now_get()
        elif path == "/music/analyze/status":
            self._handle_analyze_status()
        else:
            self._send_json(404, {"error": "not found"})

    # ── POST routes ───────────────────────────────────────────────────────────

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        if path == "/music/skin/bg":          # 裸图字节，不是 JSON，得在 _read_body 之前接住
            self._handle_skin_bg_post()
            return
        body = self._read_body()
        if path == "/music/clientlog":        # 前端把报错送回来写进服务日志
            logging.warning("[client] %s | %s | %s", str(body.get("msg", ""))[:300], str(body.get("stack", ""))[:600].replace("\n", " ⏎ "), str(body.get("ua", ""))[:120])
            self._send_json(200, {"ok": True})
            return

        if path == "/music/playlist/add":
            self._handle_music_playlist_add(body)
        elif path == "/music/playlist/remove":
            self._handle_music_playlist_remove(body)
        elif path == "/music/playlists/create":
            self._handle_music_playlists_create(body)
        elif path == "/music/playlists/rename":
            self._handle_music_playlists_rename(body)
        elif path == "/music/playlists/delete":
            self._handle_music_playlists_delete(body)
        elif path == "/music/playlists/add-song":
            self._handle_music_playlists_add_song(body)
        elif path == "/music/playlists/remove-song":
            self._handle_music_playlists_remove_song(body)
        elif path == "/music/recent/add":
            self._handle_music_recent_add(body)
        elif path == "/music/netease/like":
            self._handle_netease_like(body)
        elif path == "/music/netease/scrobble":
            self._handle_netease_scrobble(body)
        elif path == "/music/memory":
            self._handle_music_memory_save(body)
        elif path == "/music/analyze":
            self._handle_analyze_trigger(body)
        elif path == "/music/listen-together":
            self._handle_listen_together(body)
        elif path == "/music/listen-complete":
            self._handle_music_listen_complete(body)
        elif path == "/music/profile":
            self._handle_music_profile_update(body)
        elif path == "/music/remote":
            self._handle_music_remote_post(body)
        elif path == "/music/now":
            self._handle_music_now_post(body)
        else:
            self._send_json(404, {"error": "not found"})

    # ── Static file serving ───────────────────────────────────────────────────

    def _serve_music_file(self, path: str):
        """Serve cached mp3/png files from data/music_cache/."""
        filename = path[len("/music/file/"):]
        if not filename or ".." in filename or "/" in filename:
            self._send_json(400, {"error": "bad path"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        target = (cache_dir / filename).resolve()
        # Path traversal guard
        try:
            target.relative_to(cache_dir.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        self._send_file(target)

    def _serve_static(self, path: str):
        """Serve frontend static files from ../client/ directory."""
        client_dir = HERE.parent / "client"
        if not client_dir.is_dir():
            self._send_json(404, {"error": "frontend not found — place files in ../client/"})
            return
        rel = path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel:
            self._send_json(403, {"error": "forbidden"})
            return
        target = (client_dir / rel).resolve()
        try:
            target.relative_to(client_dir.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        # SPA fallback: if file not found, serve index.html
        if not target.exists() or not target.is_file():
            target = client_dir / "index.html"
            if not target.exists():
                self._send_json(404, {"error": "not found"})
                return
        self._send_file(target)

    # ── Music endpoint handlers ───────────────────────────────────────────────

    # 搜索框里粘网易云分享链接就直接认出那首歌。
    # 上游只会拿整串链接当歌名去搜，铁定搜不到。分享链接常见三种长相：
    #   y.music.163.com/m/song?id=123&userid=...   （App 分享出来的）
    #   music.163.com/#/song?id=123                （网页版）
    #   music.163.com/song/123/                    （路径式）
    NETEASE_ID_RE = re.compile(r"(?:song\?[^#\s]*\bid=|song/)(\d{4,})")

    def _handle_music_search(self):
        qs = parse_qs(urlparse(self.path).query)
        keyword = qs.get("q", [""])[0]
        if not keyword:
            self._send_json(400, {"error": "missing q"})
            return

        m = self.NETEASE_ID_RE.search(keyword)
        if m:
            song_id = m.group(1)
            try:
                detail = self._netease_request(
                    f"https://music.163.com/api/song/detail?ids=[{song_id}]")
                songs = []
                for ds in detail.get("songs", []) or []:
                    al = ds.get("album", {}) or {}
                    cover = al.get("picUrl", "") or ""
                    if cover and not cover.startswith("http"):
                        cover = "https:" + cover
                    songs.append({
                        "id": ds.get("id"),
                        "name": ds.get("name", ""),
                        "artist": ", ".join(a.get("name", "") for a in ds.get("artists", []) or []),
                        "album": al.get("name", ""),
                        "cover": cover,
                    })
                self._send_json(200, {"ok": True, "songs": songs, "from_link": True})
                return
            except Exception as e:
                # 链接解析失败就别硬撑，退回照常搜一遍,总比直接报错强
                logger.warning("link resolve failed for %s: %s", song_id, e)

        try:
            url = "https://music.163.com/api/search/get"
            post_data = urlencode({
                "s": keyword, "type": "1", "limit": "6", "offset": "0"
            }).encode()
            raw = self._netease_request(url, data=post_data)
            songs = []
            result = raw.get("result", {})
            if not isinstance(result, dict):
                self._send_json(200, {"ok": True, "songs": []})
                return
            raw_songs = result.get("songs", [])[:6]
            # Batch-fetch covers
            ids = [s.get("id") for s in raw_songs if s.get("id")]
            covers: dict[int, str] = {}
            if ids:
                try:
                    detail_url = f"https://music.163.com/api/song/detail?ids=[{','.join(str(i) for i in ids)}]"
                    detail = self._netease_request(detail_url)
                    for ds in detail.get("songs", []):
                        al = ds.get("album", {}) or {}
                        if al.get("picUrl"):
                            covers[ds.get("id")] = al["picUrl"]
                except Exception:
                    pass
            for s in raw_songs:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                album = s.get("album", {}) or {}
                cover = covers.get(s.get("id"), album.get("picUrl", "") or "")
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s.get("id"),
                    "name": s.get("name", ""),
                    "artist": artists,
                    "album": album.get("name", ""),
                    "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_music_url(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            self._send_json(200, {"ok": True, "url": f"/music/file/{song_id}.mp3", "cached": True})
            return
        try:
            url = f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=128000"
            raw = self._netease_request(url)
            data_list = raw.get("data", [])
            audio_url = data_list[0].get("url") if data_list else None
            if not audio_url:
                if not self._netease_logged_in():
                    self._send_json(200, {"ok": False, "error": "netease_login_expired",
                                          "message": "网易云登录过期了，要换一次 MUSIC_U（server/.netease_cred）"})
                    return
                self._send_json(200, {"ok": False, "error": "no url, may need VIP or song unavailable"})
                return
            # 拿到地址立刻把 CDN 直链交给客户端（国内直连最快），服务器在后台慢慢存缓存，下次再听走 /music/file。
            # 直链放不了（跨域/过期）时客户端会回来要缓存版。
            # 页面是 https，直链给 http 会被浏览器当混合内容拦下——网易 CDN 支持 https，硬升
            direct_url = re.sub(r"^http://", "https://", audio_url)
            self._send_json(200, {"ok": True, "url": direct_url, "direct": True, "cached": False,
                                  "cache_url": f"/music/file/{song_id}.mp3"})
            def _bg():
                try:
                    if not (cache_file.exists() and cache_file.stat().st_size > 0):
                        self._download_audio(audio_url, cache_file)
                except Exception as e:
                    logging.warning("bg cache %s failed: %s", song_id, e)
            threading.Thread(target=_bg, daemon=True, name=f"cache-{song_id}").start()
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_music_stream(self):
        """Stream audio directly — resolve URL, cache, and redirect to file."""
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.mp3"
        if not (cache_file.exists() and cache_file.stat().st_size > 0):
            # Try to fetch and cache the file
            if not self._fetch_music_url(song_id):
                self._send_json(404, {"ok": False, "error": "audio unavailable"})
                return
        self._send_file(cache_file, "audio/mpeg")

    def _handle_music_lyric(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{song_id}.lrc"
        cache_trans = cache_dir / f"{song_id}.tlyric"
        # Serve from cache if available
        if cache_file.exists():
            tlyric = cache_trans.read_text() if cache_trans.exists() else ""
            self._send_json(200, {"ok": True, "lrc": cache_file.read_text(), "tlyric": tlyric})
            return
        try:
            url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&tv=-1"
            raw = self._netease_request(url)
            lrc = raw.get("lrc", {}).get("lyric", "")
            tlyric = raw.get("tlyric", {}).get("lyric", "")
            # Cache BOTH .lrc AND .tlyric (critical: both must be saved)
            if lrc:
                cache_file.write_text(lrc)
            if tlyric:
                cache_trans.write_text(tlyric)
            self._send_json(200, {"ok": True, "lrc": lrc, "tlyric": tlyric})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    # ── Playlist (legacy flat) ──

    def _handle_music_playlist_get(self):
        self._send_json(200, {"ok": True, "songs": self._load_playlist()})

    def _handle_music_playlist_add(self, body: dict):
        song = body.get("song")
        if not song or not song.get("songId"):
            self._send_json(400, {"error": "missing song"})
            return
        song["cover"] = self._ensure_cover(song["songId"], song.get("cover", ""))
        song["addedBy"] = body.get("by", "unknown")
        playlist = self._load_playlist()
        if any(s.get("songId") == song["songId"] for s in playlist):
            self._send_json(200, {"ok": True, "duplicate": True, "songs": playlist})
            return
        playlist.append(song)
        self._save_playlist(playlist)
        # Also add to "liked" in multi-playlist system
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == "liked":
                if not any(s.get("songId") == song["songId"] for s in pl["songs"]):
                    pl["songs"].append(song)
                self._save_music_data(data)
                break
        self._send_json(200, {"ok": True, "songs": playlist})

    def _handle_music_playlist_remove(self, body: dict):
        song_id = body.get("songId")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        playlist = self._load_playlist()
        playlist = [s for s in playlist if s.get("songId") != song_id]
        self._save_playlist(playlist)
        self._send_json(200, {"ok": True, "songs": playlist})

    # ── Multi-playlist system ──

    def _handle_music_playlists_list(self):
        data = self._load_music_data()
        out = []
        for pl in data["playlists"]:
            cover = ""
            if pl["songs"]:
                cover = pl["songs"][0].get("cover", "")
            out.append({"id": pl["id"], "name": pl["name"], "count": len(pl["songs"]), "cover": cover, "owner": pl.get("owner", "")})
        self._send_json(200, {"ok": True, "playlists": out})

    def _handle_music_playlists_songs(self):
        qs = parse_qs(urlparse(self.path).query)
        pid = qs.get("id", [""])[0]
        if not pid:
            self._send_json(400, {"error": "missing id"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                self._send_json(200, {"ok": True, "songs": pl["songs"]})
                return
        self._send_json(404, {"error": "not found"})

    def _handle_music_playlists_create(self, body: dict):
        name = body.get("name", "").strip()
        if not name:
            self._send_json(400, {"error": "missing name"})
            return
        data = self._load_music_data()
        pl = {"id": uuid.uuid4().hex[:8], "name": name, "songs": []}
        if body.get("owner") in ("fog", "ai"):      # AI 的歌单挂 owner=fog，住在它自己的主页（MCP 署名 ai 也认）
            pl["owner"] = "fog"
        data["playlists"].append(pl)
        self._save_music_data(data)
        self._send_json(200, {"ok": True, "playlist": {"id": pl["id"], "name": pl["name"], "count": 0, "cover": "", "owner": pl.get("owner", "")}})

    def _handle_music_playlists_rename(self, body: dict):
        pid = body.get("id", "")
        name = body.get("name", "").strip()
        if not pid or not name:
            self._send_json(400, {"error": "missing id or name"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                pl["name"] = name
                self._save_music_data(data)
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "not found"})

    def _handle_music_playlists_delete(self, body: dict):
        pid = body.get("id", "")
        if not pid or pid == "liked":
            self._send_json(400, {"error": "cannot delete"})
            return
        data = self._load_music_data()
        data["playlists"] = [p for p in data["playlists"] if p["id"] != pid]
        self._save_music_data(data)
        self._send_json(200, {"ok": True})

    def _handle_music_playlists_add_song(self, body: dict):
        pid = body.get("playlistId", "")
        song = body.get("song")
        if not pid or not song or not song.get("songId"):
            self._send_json(400, {"error": "missing playlistId or song"})
            return
        song["cover"] = self._ensure_cover(song["songId"], song.get("cover", ""))
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                if any(str(s.get("songId")) == str(song["songId"]) for s in pl["songs"]):
                    self._send_json(200, {"ok": True, "duplicate": True})
                    return
                song["addedBy"] = body.get("by", "unknown")
                pl["songs"].append(song)
                self._save_music_data(data)
                if pid == "liked":
                    self._save_playlist(pl["songs"])
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "playlist not found"})

    def _handle_music_playlists_remove_song(self, body: dict):
        pid = body.get("playlistId", "")
        song_id = body.get("songId")
        if not pid or not song_id:
            self._send_json(400, {"error": "missing playlistId or songId"})
            return
        data = self._load_music_data()
        for pl in data["playlists"]:
            if pl["id"] == pid:
                pl["songs"] = [s for s in pl["songs"] if s.get("songId") != song_id]
                self._save_music_data(data)
                if pid == "liked":
                    self._save_playlist(pl["songs"])
                self._send_json(200, {"ok": True})
                return
        self._send_json(404, {"error": "playlist not found"})

    # ── Recent play history ──

    def _handle_music_recent_get(self):
        data = self._load_music_data()
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if (qs.get("by") or [""])[0] == "fog":      # AI 自己听的单独一列
            self._send_json(200, {"ok": True, "songs": data.get("fogRecent", [])[:30]})
            return
        self._send_json(200, {"ok": True, "songs": data.get("recent", [])[:30]})

    def _handle_music_recent_add(self, body: dict):
        song = body.get("song")
        if not song or not song.get("songId"):
            self._send_json(200, {"ok": True})
            return
        data = self._load_music_data()
        if body.get("by") in ("fog", "ai"):         # AI 听的（song_listen 真听了一遍才记），不混进用户的最近在听、不计听歌数
            song["cover"] = self._ensure_cover(song["songId"], song.get("cover", ""))
            song["playedAt"] = datetime.now(timezone.utc).isoformat()
            fr = [x for x in data.get("fogRecent", []) if str(x.get("songId")) != str(song["songId"])]
            fr.insert(0, song)
            data["fogRecent"] = fr[:50]
            self._save_music_data(data)
            self._send_json(200, {"ok": True})
            return
        recent = data.get("recent", [])
        recent = [s for s in recent if s.get("songId") != song["songId"]]
        song["playedAt"] = datetime.now(timezone.utc).isoformat()
        recent.insert(0, song)
        data["recent"] = recent[:50]
        self._save_music_data(data)
        # Auto-increment listen count in song memory
        mem = self._load_song_memory()
        sid = str(song["songId"])
        entry = mem.get(sid, {
            "songId": song["songId"],
            "name": song.get("name", ""),
            "artist": song.get("artist", ""),
            "listenCount": 0,
            "togetherCount": 0,
            "firstListened": None,
            "lastListened": None,
            "analyzed": False,
            "notes": "",
            "feeling": "",
            "favoriteLines": [],
            "tags": [],
        })
        entry["listenCount"] = entry.get("listenCount", 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        entry["lastListened"] = now
        if not entry.get("firstListened"):
            entry["firstListened"] = now
        entry["name"] = song.get("name", entry.get("name", ""))
        entry["artist"] = song.get("artist", entry.get("artist", ""))
        mem[sid] = entry
        self._save_song_memory(mem)
        self._send_json(200, {"ok": True})

    # ── 歌曲评论区：走 .netease_cred 里的账号 cookie，取网易云热评＋最新评论 ──
    #    首页(offset=0)带热评，翻页只给最新；字段裁剪到画卡要用的几项。

    def _handle_music_comments(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id.isdigit():
            self._send_json(400, {"error": "missing or bad id"})
            return
        try:
            offset = max(0, int(qs.get("offset", ["0"])[0] or 0))
        except ValueError:
            offset = 0
        try:
            limit = min(50, max(1, int(qs.get("limit", ["20"])[0] or 20)))
        except ValueError:
            limit = 20
        url = (f"https://music.163.com/api/v1/resource/comments/R_SO_4_{song_id}"
               f"?limit={limit}&offset={offset}")
        try:
            d = self._netease_request(url)
        except Exception:
            self._send_json(502, {"error": "评论暂时取不到"})
            return

        def trim(c):
            u = c.get("user") or {}
            return {
                "user": u.get("nickname", ""),
                "avatar": u.get("avatarUrl", ""),
                "content": c.get("content", ""),
                "liked": c.get("likedCount", 0),
                "time": c.get("time", 0),
            }

        self._send_json(200, {
            "ok": True,
            "total": d.get("total", 0),
            "more": bool(d.get("more")),
            "hot": [trim(c) for c in (d.get("hotComments") or [])[:15]] if offset == 0 else [],
            "comments": [trim(c) for c in (d.get("comments") or [])],
        })

    # ── 封面取色代理 ──
    #    网易 CDN 不给 CORS 头，前端 canvas 采样封面会被「污染画布」拦住；
    #    这里同源转一手（只放行网易音乐图床），舞台模式才能按封面生成配色。

    def _handle_music_cover(self):
        qs = parse_qs(urlparse(self.path).query)
        url = qs.get("url", [""])[0]
        if not re.match(r"^https?://p[0-9]\.music\.126\.net/", url):
            self._send_json(400, {"error": "只代理网易音乐图床"})
            return
        try:
            req = urllib.request.Request(url.replace("http://", "https://", 1) + ("?param=200y200" if "?" not in url else ""), headers={
                "Referer": "https://music.163.com",
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
        except Exception:
            self._send_json(502, {"error": "封面没取到"})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # ── 卡片图 ──
    #    GET /music/card?id=&line= → 服务端现画一张歌曲卡：有 PIL＋CJK 字体出 PNG，
    #    没有就退自包含 SVG（封面 base64 内嵌）。给官端（claude.ai / ChatGPT 连接器）
    #    当 markdown 图片贴——官端不渲染自定义组件，但渲染图片，卡片以图的形式回家。

    CARD_LINE_MAX = 60

    def _handle_music_card(self):
        import hashlib
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id.isdigit():
            self._send_json(400, {"error": "missing or bad id"})
            return
        line = qs.get("line", [""])[0]
        # 有人不做百分号编码直接把 UTF-8 塞 URL（curl 手打就会）：请求行按 latin-1
        # 解码后中文变乱码，这里试着还原一次，还原不动就保持原样。
        try:
            line = line.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        line = line.strip()[: self.CARD_LINE_MAX]
        cache_dir = self.state.data_dir / "cards"
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(f"{song_id}|{line}".encode()).hexdigest()[:10]
        for ext, ctype in (("png", "image/png"), ("svg", "image/svg+xml")):
            f = cache_dir / f"{song_id}-{key}.{ext}"
            if f.exists():
                self._send_card_bytes(f.read_bytes(), ctype)
                return
        try:
            d = self._netease_request(f"https://music.163.com/api/song/detail?ids=[{song_id}]")
            ds = (d.get("songs") or [{}])[0]
            name = ds.get("name", "") or f"song {song_id}"
            artist = ", ".join(a.get("name", "") for a in ds.get("artists", []) or [])
            cover_url = (ds.get("album", {}) or {}).get("picUrl", "") or ""
        except Exception:
            name, artist, cover_url = f"song {song_id}", "", ""
        cover = b""
        if cover_url:
            try:
                u = cover_url.replace("http://", "https://", 1)
                req = urllib.request.Request(
                    u + ("?param=300y300" if "?" not in u else ""),
                    headers={"Referer": "https://music.163.com", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    cover = resp.read()
            except Exception as e:
                logger.warning("card: cover fetch failed for %s: %r", song_id, e)
                cover = b""
        try:
            data, ext, ctype = self._draw_card_png(name, artist, line, cover), "png", "image/png"
        except Exception as e:
            logger.info("card: PNG unavailable (%s), falling back to SVG", e)
            data, ext, ctype = self._draw_card_svg(name, artist, line, cover), "svg", "image/svg+xml"
        # 封面本该有却没取到（网易偶发抽风）就别缓存，免得无封面版被端上桌 7 天
        if cover or not cover_url:
            try:
                (cache_dir / f"{song_id}-{key}.{ext}").write_bytes(data)
            except OSError:
                pass
        self._send_card_bytes(data, ctype)

    def _send_card_bytes(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _card_font(self, size: int):
        import os
        from PIL import ImageFont
        candidates = [
            os.environ.get("MUSIC_CARD_FONT", ""),
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyh.ttc",
        ]
        for path in candidates:
            if not path or not Path(path).exists():
                continue
            # .ttc 多面字体：优先找简体中文那一面（家族名带 SC）
            for idx in range(5):
                try:
                    f = ImageFont.truetype(path, size, index=idx)
                    if "SC" in "".join(f.getname()):
                        return f
                except Exception:
                    break
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        raise RuntimeError("no usable CJK font (set MUSIC_CARD_FONT)")

    def _draw_card_png(self, name, artist, line, cover_bytes):
        import io
        from PIL import Image, ImageDraw
        W, H, PAD, CV = 900, 300, 30, 240
        base = (46, 52, 64)
        cover = None
        if cover_bytes:
            try:
                cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB").resize((CV, CV))
                base = cover.resize((1, 1)).getpixel((0, 0))
            except Exception:
                cover = None
        img = Image.new("RGB", (W, H))
        top = tuple(int(c * 0.42) for c in base)
        bot = tuple(int(c * 0.16) for c in base)
        for y in range(H):
            t = y / (H - 1)
            img.paste(tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)), (0, y, W, y + 1))
        draw = ImageDraw.Draw(img)
        if cover is not None:
            mask = Image.new("L", (CV, CV), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, CV - 1, CV - 1), radius=18, fill=255)
            img.paste(cover, (PAD, PAD), mask)
        else:
            draw.rounded_rectangle((PAD, PAD, PAD + CV, PAD + CV), radius=18,
                                   fill=tuple(int(c * 0.6) for c in base))
            draw.text((PAD + CV // 2, PAD + CV // 2), "♪",
                      font=self._card_font(90), fill=(255, 255, 255), anchor="mm")
        tx = PAD + CV + 36
        maxw = W - tx - PAD

        def fit(text, font):
            if draw.textlength(text, font=font) <= maxw:
                return text
            while text and draw.textlength(text + "…", font=font) > maxw:
                text = text[:-1]
            return text + "…"

        f_name, f_artist, f_line, f_foot = (self._card_font(44), self._card_font(27),
                                            self._card_font(29), self._card_font(20))
        y = PAD + 14
        draw.text((tx, y), fit(name, f_name), font=f_name, fill=(255, 255, 255))
        y += 62
        if artist:
            draw.text((tx, y), fit(artist, f_artist), font=f_artist, fill=(197, 203, 214))
            y += 46
        if line:
            accent = tuple(min(255, int(c * 0.5 + 150)) for c in base)
            draw.text((tx, y + 10), fit(f"「{line}」", f_line), font=f_line, fill=accent)
        draw.text((W - PAD, H - 18), "♪ music-mcp", font=f_foot, fill=(139, 147, 162), anchor="rs")
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()

    def _draw_card_svg(self, name, artist, line, cover_bytes):
        import base64
        from xml.sax.saxutils import escape
        if cover_bytes:
            uri = "data:image/jpeg;base64," + base64.b64encode(cover_bytes).decode()
            cover_el = f'<image x="30" y="30" width="240" height="240" clip-path="url(#r)" href="{uri}"/>'
        else:
            cover_el = ('<rect x="30" y="30" width="240" height="240" rx="18" fill="#3a4150"/>'
                        '<text x="150" y="180" font-size="80" fill="#fff" text-anchor="middle">♪</text>')
        fam = "font-family=\"'Noto Sans SC','PingFang SC','Microsoft YaHei',sans-serif\""
        artist_el = f'<text x="306" y="152" font-size="27" fill="#c5cbd6" {fam}>{escape(artist)}</text>' if artist else ""
        line_el = f'<text x="306" y="212" font-size="29" fill="#9fd0c9" {fam}>「{escape(line)}」</text>' if line else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="300" viewBox="0 0 900 300">'
                f'<defs><clipPath id="r"><rect x="30" y="30" width="240" height="240" rx="18"/></clipPath>'
                f'<linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="0" stop-color="#2b3140"/><stop offset="1" stop-color="#12151c"/></linearGradient></defs>'
                f'<rect width="900" height="300" fill="url(#g)"/>{cover_el}'
                f'<text x="306" y="98" font-size="44" font-weight="700" fill="#ffffff" {fam}>{escape(name)}</text>'
                f'{artist_el}{line_el}'
                f'<text x="870" y="278" font-size="20" fill="#8b93a2" text-anchor="end">♪ music-mcp</text></svg>').encode("utf-8")

    # ── MV ──
    #    songId → song/detail 拿 mv id → mv/detail 拿各清晰度片源。
    #    片源是网易 CDN 的 mp4，https 化后 <video> 直接吃。

    def _handle_music_mv(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id.isdigit():
            self._send_json(400, {"error": "missing or bad id"})
            return
        try:
            d = self._netease_request(f"https://music.163.com/api/song/detail?ids=[{song_id}]")
            s0 = (d.get("songs") or [{}])[0]
            # 老 API 这里叫 mvid，新 API 才叫 mv——两个都认
            mv_id = s0.get("mvid") or s0.get("mv") or 0
        except Exception:
            self._send_json(502, {"error": "查不到这首歌"})
            return
        if not mv_id:
            self._send_json(200, {"ok": False, "msg": "这首没有MV"})
            return
        try:
            m = self._netease_request(f"https://music.163.com/api/mv/detail?id={mv_id}")
        except Exception:
            self._send_json(502, {"error": "MV 详情取不到"})
            return
        data = m.get("data") or {}
        urls = {k: v.replace("http://", "https://", 1)
                for k, v in (data.get("brs") or {}).items() if v}
        best = urls.get("1080") or urls.get("720") or urls.get("480") or urls.get("240")
        if not best:
            self._send_json(200, {"ok": False, "msg": "有MV但片源没拿到"})
            return
        self._send_json(200, {"ok": True, "mvId": mv_id, "name": data.get("name", ""),
                              "artist": data.get("artistName", ""),
                              "cover": data.get("cover", ""),
                              "duration": data.get("duration", 0),
                              "url": best, "urls": urls})

    # ── 网易云歌单同步：只读镜像，列歌单/取曲目，不改不删云端的任何东西 ──

    def _handle_netease_playlists(self):
        try:
            a = self._netease_request("https://music.163.com/api/nuser/account/get")
            uid = (a.get("account") or {}).get("id") or (a.get("profile") or {}).get("userId")
            nick = (a.get("profile") or {}).get("nickname") or ""
            if not uid:
                self._send_json(200, {"ok": False, "msg": "cookie 里认不出账号"})
                return
            pl = self._netease_request(f"https://music.163.com/api/user/playlist?uid={uid}&limit=50&offset=0")
        except Exception:
            self._send_json(502, {"error": "网易歌单拉取失败"})
            return
        out = []
        for p in (pl.get("playlist") or []):
            out.append({
                "id": p.get("id"), "name": p.get("name", ""),
                "count": p.get("trackCount", 0),
                "cover": str(p.get("coverImgUrl") or "").replace("http://", "https://", 1),
                "mine": (p.get("creator") or {}).get("userId") == uid,
            })
        self._send_json(200, {"ok": True, "uid": uid, "nickname": nick, "playlists": out})

    def _handle_netease_playlist(self):
        qs = parse_qs(urlparse(self.path).query)
        pid = qs.get("id", [""])[0]
        if not pid.isdigit():
            self._send_json(400, {"error": "missing or bad id"})
            return
        try:
            limit = min(1000, max(1, int(qs.get("limit", ["300"])[0] or 300)))
        except ValueError:
            limit = 300
        try:
            d = self._netease_request(f"https://music.163.com/api/v6/playlist/detail?id={pid}&n={limit}&s=0")
        except Exception:
            self._send_json(502, {"error": "歌单内容拉取失败"})
            return
        p = d.get("playlist") or {}
        songs = []
        for t in (p.get("tracks") or [])[:limit]:
            al = t.get("al") or {}
            songs.append({
                "songId": t.get("id"), "name": t.get("name", ""),
                "artist": ", ".join(a.get("name", "") for a in (t.get("ar") or []) if a.get("name")),
                "album": al.get("name", ""),
                "cover": str(al.get("picUrl") or "").replace("http://", "https://", 1),
            })
        self._send_json(200, {"ok": True, "id": p.get("id"), "name": p.get("name", ""),
                              "total": p.get("trackCount", 0), "songs": songs})

    # ── 网易云日推 ──
    # 日推和账号档案半小时内不会变，进程里记一份：日推 30 分钟、档案 10 分钟；前端那边还有自己的十分钟缓存。
    _TTL_CACHE: dict[str, tuple[float, Any]] = {}

    def _ttl_get(self, key: str, ttl: float):
        hit = self._TTL_CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        return None

    def _ttl_put(self, key: str, payload):
        self._TTL_CACHE[key] = (time.time(), payload)
        return payload

    def _handle_netease_daily(self):
        cached = self._ttl_get("ne_daily", 30 * 60)
        if cached is not None:
            self._send_json(200, cached)
            return
        try:
            d = self._netease_request("https://music.163.com/api/v3/discovery/recommend/songs")
        except Exception:
            self._send_json(502, {"error": "日推拉取失败"})
            return
        songs = []
        for t in ((d.get("data") or {}).get("dailySongs") or []):
            al = t.get("al") or {}
            songs.append({
                "songId": t.get("id"), "name": t.get("name", ""),
                "artist": ", ".join(a.get("name", "") for a in (t.get("ar") or []) if a.get("name")),
                "album": al.get("name", ""),
                "cover": str(al.get("picUrl") or "").replace("http://", "https://", 1),
                "reason": t.get("reason", ""),
            })
        self._send_json(200, self._ttl_put("ne_daily", {"ok": True, "songs": songs}))

    # ── 网易云红心同步 ──
    #    读：song/like/get 全量红心 id。写：song/like 老口（radio/like 会被风控 -460，别换）。写进的是登录账号的「喜欢的音乐」。

    def _handle_netease_likes_get(self):
        try:
            d = self._netease_request("https://music.163.com/api/song/like/get")
        except Exception:
            self._send_json(502, {"error": "红心列表拉取失败"})
            return
        ids = d.get("ids") or []
        self._send_json(200, {"ok": True, "count": len(ids), "ids": ids})

    def _handle_netease_like(self, body: dict):
        song_id = str(body.get("songId") or "")
        like = bool(body.get("like", True))
        if not song_id.isdigit():
            self._send_json(400, {"error": "missing songId"})
            return
        form = urlencode({"trackId": song_id, "like": "true" if like else "false",
                          "time": "3", "alg": "itembased"}).encode()
        try:
            r = self._netease_request("https://music.163.com/api/song/like", data=form)
        except Exception:
            self._send_json(502, {"error": "网易那边没应门"})
            return
        ok = r.get("code") == 200
        self._send_json(200, {"ok": ok, "code": r.get("code"),
                              "msg": "" if ok else str(r.get("message") or r.get("msg") or "")})


    def _handle_netease_scrobble(self, body: dict):
        """听歌记账上报：把一次播放写回网易云官方客户端同款的 weblog 口，听歌量/年度时长才吃得到这里的播放。
        end='playend' 自然听完,'ui' 中途切歌——都按实际播放秒数记。"""
        song_id = str(body.get("songId") or "")
        try:
            seconds = max(0, int(body.get("seconds") or 0))
        except (TypeError, ValueError):
            seconds = 0
        end = body.get("end") if body.get("end") in ("playend", "ui") else "playend"
        if not song_id.isdigit() or seconds <= 0:
            self._send_json(400, {"error": "missing songId/seconds"})
            return
        logs = json.dumps([{"action": "play", "json": {
            "download": 0, "end": end, "id": int(song_id),
            "sourceId": str(body.get("sourceId") or ""),
            "time": seconds, "type": "song", "wifi": 0, "source": "list",
            "mainsite": 1, "content": ""}}])
        form = urlencode({"logs": logs}).encode()
        try:
            r = self._netease_request("https://music.163.com/api/feedback/weblog", data=form)
        except Exception:
            self._send_json(502, {"error": "网易那边没应门"})
            return
        ok = r.get("code") == 200
        self._send_json(200, {"ok": ok, "code": r.get("code")})


    def _handle_netease_record(self):
        """听歌排行拉取：网易云的单曲累计播放榜，type=0 总榜 / 1 最近一周。只读。"""
        qs = parse_qs(urlparse(self.path).query)
        rtype = 1 if qs.get("type", ["0"])[0] == "1" else 0
        try:
            acc = self._netease_request("https://music.163.com/api/nuser/account/get")
            uid = (acc.get("profile") or {}).get("userId")
            if not uid:
                self._send_json(502, {"error": "拿不到账号 uid"})
                return
            d = self._netease_request(
                f"https://music.163.com/api/v1/play/record?uid={uid}&type={rtype}")
        except Exception:
            self._send_json(502, {"error": "听歌排行拉取失败"})
            return
        key = "weekData" if rtype == 1 else "allData"
        out = []
        for row in (d.get(key) or []):
            song = row.get("song") or {}
            al = song.get("al") or {}
            out.append({
                "songId": song.get("id"), "name": song.get("name", ""),
                "artist": ", ".join(a.get("name", "") for a in (song.get("ar") or []) if a.get("name")),
                "album": al.get("name", ""),
                "cover": str(al.get("picUrl") or "").replace("http://", "https://", 1),
                "playCount": row.get("playCount", 0), "score": row.get("score", 0),
            })
        self._send_json(200, {"ok": True, "type": rtype, "songs": out})


    def _handle_netease_profile(self):
        """账号档案：昵称/听歌量/等级/入网天数，只读。"""
        cached = self._ttl_get("ne_profile", 10 * 60)
        if cached is not None:
            self._send_json(200, cached)
            return
        try:
            acc = self._netease_request("https://music.163.com/api/nuser/account/get")
            uid = (acc.get("profile") or {}).get("userId")
            if not uid:
                self._send_json(502, {"error": "拿不到账号 uid"})
                return
            d = self._netease_request(f"https://music.163.com/api/v1/user/detail/{uid}")
        except Exception:
            self._send_json(502, {"error": "账号档案拉取失败"})
            return
        prof = d.get("profile") or {}
        self._send_json(200, self._ttl_put("ne_profile", {"ok": True, "profile": {
            "userId": uid, "nickname": prof.get("nickname", ""),
            "listenSongs": d.get("listenSongs", 0), "level": d.get("level", 0),
            "createDays": d.get("createDays", 0),
            "follows": prof.get("follows", 0), "followeds": prof.get("followeds", 0),
            "avatar": prof.get("avatarUrl", ""),
            "signature": prof.get("signature", ""),
        }}))

    # ── 网易云扫码登录 ──
    # 网易的裸 /api/ 接口不加密也给二维码钥匙和轮询结果：unikey → app 扫 music.163.com/login?codekey=KEY
    # → 轮询 client/login：801 等扫、802 扫了没确认、803 成功（Set-Cookie 带 MUSIC_U）、800 过期。
    # 成功就把 MUSIC_U 写回 .netease_cred（旧的留 .bak），登录探针缓存清掉，横幅自己灭。
    def _netease_plain_post(self, url: str, form: dict[str, Any], timeout: int = 8):
        """不带旧 cookie 的裸 POST，回 (json, Set-Cookie 列表)。扫码那几步要看响应头，走不了 _netease_request。"""
        req = urllib.request.Request(url, data=urlencode(form).encode(), headers={
            "Referer": "https://music.163.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), (resp.headers.get_all("Set-Cookie") or [])

    def _handle_netease_qr_new(self):
        key = None
        try:
            d, _ = self._netease_plain_post("https://music.163.com/api/login/qrcode/unikey", {"type": 3})
            key = d.get("unikey")
        except Exception:
            key = None
        if not key:
            self._send_json(502, {"error": "网易没给二维码钥匙"})
            return
        self._send_json(200, {"ok": True, "key": key, "url": f"https://music.163.com/login?codekey={key}"})

    def _handle_netease_qr_check(self):
        qs = parse_qs(urlparse(self.path).query)
        key = (qs.get("key") or [""])[0].strip()
        if not re.fullmatch(r"[0-9a-zA-Z-]{10,64}", key):
            self._send_json(400, {"error": "bad key"})
            return
        try:
            d, cookies = self._netease_plain_post(
                "https://music.163.com/api/login/qrcode/client/login", {"key": key, "type": 3})
        except Exception:
            self._send_json(502, {"error": "问不到网易"})
            return
        code = d.get("code")
        out: dict[str, Any] = {"ok": True, "code": code, "message": d.get("message", "")}
        if code == 803:
            music_u = ""
            for c in cookies:
                m = re.match(r"\s*MUSIC_U=([^;]+)", c)
                if m:
                    music_u = m.group(1).strip()
            if not music_u:
                self._send_json(502, {"error": "登录成功但响应里没有 MUSIC_U"})
                return
            self._save_music_u(music_u)
            try:
                a = self._netease_request("https://music.163.com/api/nuser/account/get", timeout=6)
                out["nickname"] = ((a.get("profile") or {}).get("nickname")) or ""
            except Exception:
                out["nickname"] = ""
        self._send_json(200, out)

    def _save_music_u(self, music_u: str):
        """MUSIC_U 写回 .netease_cred：旧文件留一份 .bak-时间，其它行原样保留，权限 600，原子替换。"""
        cred = HERE / ".netease_cred"
        lines: list[str] = []
        try:
            lines = cred.read_text().splitlines()
        except OSError:
            lines = []
        if lines:
            try:
                shutil.copy2(cred, HERE / f".netease_cred.bak-{time.strftime('%Y%m%d-%H%M')}")
            except Exception:
                pass
        kept = [ln for ln in lines if not ln.startswith("MUSIC_U=")]
        kept.insert(0, f"MUSIC_U={music_u}")
        tmp = cred.with_name(".netease_cred.tmp")
        tmp.write_text("\n".join(kept) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, cred)
        try:
            self.state._login_probe = None
        except Exception:
            pass

    # ── 自定义皮肤的底图：自己传一张图当整页的底 ──
    # 一个家一张图，存 data/skin-bg.img（旁边 skin-bg.json 记 mime），手机电脑抽屉共用。
    # POST 裸图字节（Content-Type image/*，8MB 封顶）；POST 空身子＝去掉。GET 原样吐出来给 CSS 当背景。
    def _skin_bg_paths(self):
        return self.state.data_dir / "skin-bg.img", self.state.data_dir / "skin-bg.json"

    def _handle_skin_bg_get(self):
        img, meta = self._skin_bg_paths()
        if not img.exists():
            self._send_json(404, {"error": "no skin bg"})
            return
        mime = "image/jpeg"
        try:
            mime = json.loads(meta.read_text()).get("mime") or mime
        except Exception:
            pass
        self._send_file(img, mime)

    def _handle_skin_bg_post(self):
        img, meta = self._skin_bg_paths()
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            for p in (img, meta):
                try:
                    p.unlink()
                except OSError:
                    pass
            self._send_json(200, {"ok": True, "cleared": True})
            return
        if length > 8 * 1024 * 1024:
            self._send_json(413, {"error": "图太大了，8MB 以内"})
            return
        mime = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            self._send_json(415, {"error": "只收 jpg / png / webp / gif"})
            return
        raw = self.rfile.read(length)
        self.state.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = img.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, img)
        meta.write_text(json.dumps({"mime": mime, "size": len(raw), "at": int(time.time())}))
        self._send_json(200, {"ok": True, "url": f"/music/skin/bg?v={int(time.time())}", "size": len(raw)})

    def _handle_netease_status(self):
        """主页「网易云」那行用：登没登录、叫什么。"""
        ok = self._netease_logged_in(ttl=0)
        nick = ""
        if ok:
            try:
                a = self._netease_request("https://music.163.com/api/nuser/account/get", timeout=6)
                nick = ((a.get("profile") or {}).get("nickname")) or ""
            except Exception:
                nick = ""
        self._send_json(200, {"ok": True, "logged_in": ok, "nickname": nick})

    # ── Song memory system ──

    def _handle_music_memory_get(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        mem = self._load_song_memory()
        if song_id:
            entry = mem.get(str(song_id))
            if entry and not entry.get("cover"):          # 批注本歌头卡要封面：借浮现那套进程内缓存补一张
                cache = getattr(self.state, "_noted_cover", None)
                if cache is None:
                    cache = {}
                    try:
                        self.state._noted_cover = cache
                    except Exception:
                        pass
                c = cache.get(str(song_id)) or self._ensure_cover(song_id, "")
                cache[str(song_id)] = c
                entry = dict(entry, cover=c)
            self._send_json(200, {"ok": True, "memory": entry})
        else:
            self._send_json(200, {"ok": True, "memories": mem})

    # ── 批注浮现 ──
    # 只挑写过字的（notes / feeling / favoriteLines 任一非空），补上封面（song/detail 取一次，进程内缓存）。
    def _handle_music_memory_noted(self):
        mem = self._load_song_memory()
        cache = getattr(self.state, "_noted_cover", None)
        if cache is None:
            cache = {}
            try:
                self.state._noted_cover = cache
            except Exception:
                pass
        out = []
        for sid, e in mem.items():
            if not (e.get("notes") or e.get("feeling") or e.get("favoriteLines")):
                continue
            cover = e.get("cover") or cache.get(str(sid)) or ""
            if not cover:
                cover = self._ensure_cover(sid, "")
                cache[str(sid)] = cover
            out.append({
                "songId": e.get("songId") or sid, "name": e.get("name", ""), "artist": e.get("artist", ""),
                "notes": e.get("notes", ""), "feeling": e.get("feeling", ""), "favoriteLines": e.get("favoriteLines") or [],
                "notedBy": e.get("notedBy", ""), "notedAt": e.get("notedAt", ""), "listenCount": e.get("listenCount", 0),
                "cover": cover,
            })
        out.sort(key=lambda x: x.get("notedAt") or "", reverse=True)
        self._send_json(200, {"ok": True, "songs": out})

    def _handle_music_memory_save(self, body: dict):
        song_id = str(body.get("songId", ""))
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        mem = self._load_song_memory()
        entry = mem.get(song_id, {
            "songId": int(song_id),
            "name": "",
            "artist": "",
            "listenCount": 0,
            "togetherCount": 0,
            "firstListened": None,
            "lastListened": None,
            "analyzed": False,
            "notes": "",
            "feeling": "",
            "favoriteLines": [],
            "tags": [],
        })
        now = datetime.now(timezone.utc).isoformat()
        action = body.get("action", "listen")
        if action == "listen":
            entry["listenCount"] = entry.get("listenCount", 0) + 1
            entry["lastListened"] = now
            if not entry.get("firstListened"):
                entry["firstListened"] = now
            entry["name"] = body.get("name", entry.get("name", ""))
            entry["artist"] = body.get("artist", entry.get("artist", ""))
        elif action == "together":
            entry["togetherCount"] = entry.get("togetherCount", 0) + 1
            entry["lastListened"] = now
        elif action == "analyze":
            entry["analyzed"] = True
            if body.get("notes"):
                entry["notes"] = body["notes"]
            if body.get("feeling"):
                entry["feeling"] = body["feeling"]
            if body.get("favoriteLines"):
                entry["favoriteLines"] = body["favoriteLines"]
            if body.get("tags"):
                entry["tags"] = body["tags"]
            if body.get("bpm"):
                entry["bpm"] = body["bpm"]
            if body.get("duration"):
                entry["duration"] = body["duration"]
        elif action == "note":
            # 批注是手写的，不设门槛。
            for key in ("notes", "feeling", "tags", "favoriteLines"):
                if key in body:
                    entry[key] = body[key]
            entry["name"] = body.get("name", entry.get("name", ""))
            entry["artist"] = body.get("artist", entry.get("artist", ""))
            entry["notedBy"] = body.get("by", "anko")
            entry["notedAt"] = now
        elif action == "like":
            entry["liked"] = True
            entry["name"] = body.get("name", entry.get("name", ""))
            entry["artist"] = body.get("artist", entry.get("artist", ""))
            cover = self._ensure_cover(song_id, body.get("cover", ""))
            song_obj = {
                "songId": int(song_id),
                "name": entry["name"],
                "artist": entry["artist"],
                "cover": cover,
                "addedBy": body.get("by", "user"),
            }
            data = self._load_music_data()
            # Add to a "Liked by User" playlist (auto-create if missing)
            liked_pl = None
            for pl in data.get("playlists", []):
                if pl.get("id") == "user_liked":
                    liked_pl = pl
                    break
            if not liked_pl:
                liked_pl = {"id": "user_liked", "name": "User Liked", "songs": []}
                data.setdefault("playlists", []).append(liked_pl)
            if not any(s.get("songId") == int(song_id) for s in liked_pl["songs"]):
                liked_pl["songs"].append(song_obj)
                self._save_music_data(data)
        elif action == "note":
            entry["notes"] = body.get("notes", entry.get("notes", ""))
            if body.get("feeling"):
                entry["feeling"] = body["feeling"]
            if body.get("favoriteLines"):
                entry["favoriteLines"] = body["favoriteLines"]
        mem[song_id] = entry
        self._save_song_memory(mem)
        self._send_json(200, {"ok": True, "memory": entry})

    # ── Listen together ──

    def _handle_listen_together(self, body: dict):
        """Record a 'listen together' event. In standalone mode this just logs
        the event; in the full CcCompanion it also injects into tmux."""
        song_id = body.get("songId")
        name = body.get("name", "")
        artist = body.get("artist", "")
        cover = self._ensure_cover(song_id, body.get("cover", ""))
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        is_roam = body.get("roam", False)
        # Record in song memory
        mem = self._load_song_memory()
        sid = str(song_id)
        entry = mem.get(sid, {
            "songId": song_id,
            "name": name,
            "artist": artist,
            "listenCount": 0,
            "togetherCount": 0,
            "firstListened": None,
            "lastListened": None,
            "analyzed": False,
            "notes": "",
            "feeling": "",
            "favoriteLines": [],
            "tags": [],
        })
        now = datetime.now(timezone.utc).isoformat()
        entry["listenCount"] = entry.get("listenCount", 0) + 1
        entry["lastListened"] = now
        if not entry.get("firstListened"):
            entry["firstListened"] = now
        entry["name"] = name or entry.get("name", "")
        entry["artist"] = artist or entry.get("artist", "")
        mem[sid] = entry
        self._save_song_memory(mem)
        logger.info("listen-together: %s — %s (roam=%s)", name, artist, is_roam)
        self._send_json(200, {"ok": True})

    def _handle_music_listen_complete(self, body: dict):
        """Called when a song finishes playing naturally (audio ended event)."""
        song_id = body.get("songId")
        source = body.get("source", "")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        if source != "together":
            self._send_json(200, {"ok": True, "counted": False})
            return
        sid = str(song_id)
        mem = self._load_song_memory()
        entry = mem.get(sid)
        if not entry:
            self._send_json(200, {"ok": True, "counted": False})
            return
        now = datetime.now(timezone.utc).isoformat()
        entry["togetherCount"] = entry.get("togetherCount", 0) + 1
        entry["lastListened"] = now
        if not entry.get("firstListened"):
            entry["firstListened"] = now
        mem[sid] = entry
        self._save_song_memory(mem)
        self._send_json(200, {"ok": True, "counted": True})

    # ── Background pre-analysis ──

    def _handle_analyze_trigger(self, body: dict):
        song_id = body.get("songId")
        song_name = body.get("name", "")
        song_artist = body.get("artist", "")
        if not song_id:
            self._send_json(400, {"error": "missing songId"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        result_file = cache_dir / f"{song_id}_preanalysis.json"
        marker_file = cache_dir / f"{song_id}.analyzing"
        if result_file.exists():
            self._send_json(200, {"ok": True, "status": "ready"})
            return
        if marker_file.exists():
            age = time.time() - marker_file.stat().st_mtime
            if age < 60:
                self._send_json(200, {"ok": True, "status": "running"})
                return
            marker_file.unlink(missing_ok=True)
        audio_file = cache_dir / f"{song_id}.mp3"
        if not audio_file.exists():
            if not self._fetch_music_url(song_id):
                self._send_json(400, {"error": "cannot fetch audio"})
                return
        marker_file.write_text(json.dumps({
            "songId": song_id, "name": song_name, "started": time.time()
        }))
        script = str(HERE / "analyze_song.py")
        analyze_py = os.environ.get("MUSIC_ANALYZE_PYTHON", "python3")
        subprocess.Popen(
            [analyze_py, script, str(song_id), song_name, song_artist, str(cache_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._send_json(200, {"ok": True, "status": "started"})

    def _handle_analyze_status(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        cache_dir = self.state.data_dir / "music_cache"
        result_file = cache_dir / f"{song_id}_preanalysis.json"
        if result_file.exists():
            result = json.loads(result_file.read_text())
            self._send_json(200, {"ok": True, "status": "ready", "analysis": result})
            return
        marker_file = cache_dir / f"{song_id}.analyzing"
        if marker_file.exists():
            age = time.time() - marker_file.stat().st_mtime
            if age < 60:
                self._send_json(200, {"ok": True, "status": "running"})
                return
            marker_file.unlink(missing_ok=True)
        err_file = cache_dir / f"{song_id}_analyze_error.txt"
        if err_file.exists():
            err = err_file.read_text()
            self._send_json(200, {"ok": True, "status": f"error: {err}"})
            return
        self._send_json(200, {"ok": True, "status": "none"})

    # ── Stats ──

    def _handle_music_stats(self):
        mem = self._load_song_memory()
        total_songs = len(mem)
        total_listens = sum(e.get("listenCount", 0) for e in mem.values())
        together_listens = sum(e.get("togetherCount", 0) for e in mem.values())
        analyzed = sum(1 for e in mem.values() if e.get("analyzed"))
        top = sorted(mem.values(), key=lambda e: e.get("listenCount", 0), reverse=True)[:10]
        top_list = [
            {"name": e.get("name", ""), "artist": e.get("artist", ""),
             "count": e.get("listenCount", 0), "songId": e.get("songId")}
            for e in top
        ]
        self._send_json(200, {"ok": True, "stats": {
            "totalSongs": total_songs,
            "totalListens": total_listens,
            "togetherListens": together_listens,
            "analyzedSongs": analyzed,
            "topSongs": top_list,
        }})

    # ── Profile ──

    def _handle_music_profile_get(self):
        data = self._load_music_data()
        self._send_json(200, {"ok": True, "profile": data.get("profile", {})})

    def _handle_music_profile_update(self, body: dict):
        data = self._load_music_data()
        profile = data.get("profile", {})
        for k in ("avatar", "signature", "name", "fogAvatar", "fogName", "fogNick", "fogSig"):   # fog*：AI 那颗头像/名字/签名
            if k in body:
                profile[k] = body[k]
        data["profile"] = profile
        self._save_music_data(data)
        self._send_json(200, {"ok": True, "profile": profile})

    # ── Daily recommendations ──

    def _handle_music_daily(self):
        data = self._load_music_data()
        liked = []
        for pl in data["playlists"]:
            if pl["id"] == "liked":
                liked = pl["songs"]
                break
        if not liked:
            self._send_json(200, {"ok": True, "songs": []})
            return
        seed_song = random.choice(liked)
        if not seed_song.get("songId"):
            self._send_json(200, {"ok": True, "songs": []})
            return
        try:
            url = f"https://music.163.com/api/discovery/simiSong?songid={seed_song['songId']}&offset=0&limit=6"
            raw = self._netease_request(url)
            songs = []
            for s in raw.get("songs", [])[:6]:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                al = s.get("album", {}) or {}
                cover = al.get("picUrl", "")
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s["id"], "name": s.get("name", ""), "artist": artists,
                    "album": al.get("name", ""), "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs, "seed": seed_song.get("name", "")})
        except Exception as e:
            self._send_json(200, {"ok": True, "songs": [], "error": str(e)})

    # ── Remote play ──

    # ── Now-playing 心跳 ──
    #    播放器每 5 秒 POST 一次当前播放状态，存内存不落盘；
    #    MCP 的 music://now 资源转手给官端 Apps 卡片画进度条＋跟唱高亮。

    def _handle_music_now_post(self, body: dict):
        self.state.now_playing = {
            "songId": str(body.get("songId") or ""),
            "name": body.get("name") or "",
            "artist": body.get("artist") or "",
            "position": float(body.get("position") or 0),
            "duration": float(body.get("duration") or 0),
            "playing": bool(body.get("playing")),
            "at": time.time(),
        }
        self._send_json(200, {"ok": True})

    def _handle_music_now_get(self):
        now = getattr(self.state, "now_playing", None)
        if not now or time.time() - now.get("at", 0) > 30:
            self._send_json(200, {"ok": False})
            return
        self._send_json(200, dict(now, ok=True, age=round(time.time() - now.get("at", 0), 1)))

    def _handle_music_remote_get(self):
        # 远程点播攒成列表一次全交，连插几首也不丢。兼容旧单曲格式（dict 就包成 [dict]）。
        f = self.state.data_dir / "music_remote.json"
        # 客户端本来就每秒问这条线，顺路把登录态捎回去（10 分钟才真问一次网易），前端的过期横幅跟着它亮灭。
        login = self._netease_logged_in(ttl=600)
        if f.exists():
            data = json.loads(f.read_text())
            f.unlink()
            songs = data if isinstance(data, list) else [data]
            self._send_json(200, {"ok": True, "songs": songs, "song": songs[0], "netease_login": login})
        else:
            self._send_json(200, {"ok": False, "netease_login": login})

    def _handle_music_remote_peek(self):
        # 只看不取：排错用。GET /music/remote 会把队列取走。
        f = self.state.data_dir / "music_remote.json"
        if f.exists():
            data = json.loads(f.read_text())
            songs = data if isinstance(data, list) else [data]
            self._send_json(200, {"ok": True, "pending": len(songs), "songs": songs})
        else:
            self._send_json(200, {"ok": True, "pending": 0, "songs": []})

    def _handle_music_remote_post(self, body: dict):
        song = body.get("song")
        if not song:
            self._send_json(400, {"error": "missing song"})
            return
        f = self.state.data_dir / "music_remote.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        queue = []
        if f.exists():
            try:
                old = json.loads(f.read_text())
                queue = old if isinstance(old, list) else [old]
            except Exception:
                queue = []
        queue.append(song)
        f.write_text(json.dumps(queue[-20:], ensure_ascii=False))
        self._send_json(200, {"ok": True, "queued": len(queue)})

    # ── Roam mode (random genre discovery) ──

    def _handle_music_roam(self):
        """Diverse random song discovery — rotates across genres/languages."""
        # Netease top song area IDs: 0=All, 7=Chinese, 96=Western, 8=Japanese, 16=Korean
        # Netease playlist IDs for genre diversity
        genre_playlists = [
            3779629,      # Chinese classics
            2884035,      # Western classics
            71384707,     # Japanese pop
            991319590,    # Korean pop
            60198,        # Hip-hop/Rap
            11640012,     # R&B
            5059642708,   # Electronic
            2529283982,   # Folk
            3136952023,   # Rock
        ]
        top_types = [0, 7, 96, 8, 16]
        strategy = random.choice(["top", "playlist"])
        try:
            songs = []
            if strategy == "top":
                t = random.choice(top_types)
                url = f"https://music.163.com/api/discovery/new/songs?areaId={t}&limit=50&total=true"
                raw = self._netease_request(url)
                for s in raw.get("data", []):
                    artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                    al = s.get("album", {}) or {}
                    cover = al.get("picUrl", "")
                    if cover and not cover.startswith("http"):
                        cover = "https:" + cover
                    songs.append({
                        "songId": s["id"], "name": s.get("name", ""), "artist": artists,
                        "album": al.get("name", ""), "cover": cover,
                    })
            else:
                pid = random.choice(genre_playlists)
                url = f"https://music.163.com/api/playlist/detail?id={pid}"
                raw = self._netease_request(url)
                result = raw.get("result", {})
                for s in result.get("tracks", []):
                    artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                    al = s.get("album", {}) or {}
                    cover = al.get("picUrl", "")
                    if cover and not cover.startswith("http"):
                        cover = "https:" + cover
                    songs.append({
                        "songId": s["id"], "name": s.get("name", ""), "artist": artists,
                        "album": al.get("name", ""), "cover": cover,
                    })
            if songs:
                pick = random.choice(songs)
                self._send_json(200, {"ok": True, "song": pick})
            else:
                self._send_json(200, {"ok": False, "error": "no songs found"})
        except Exception as e:
            self._send_json(200, {"ok": False, "error": str(e)})

    # ── Similar songs ──

    def _handle_music_similar(self):
        qs = parse_qs(urlparse(self.path).query)
        song_id = qs.get("id", [""])[0]
        if not song_id:
            self._send_json(400, {"error": "missing id"})
            return
        try:
            url = f"https://music.163.com/api/discovery/simiSong?songid={song_id}&offset=0&total=true&limit=6"
            raw = self._netease_request(url)
            raw_songs = raw.get("songs", [])[:6]
            songs = []
            for s in raw_songs:
                artists = ", ".join(a.get("name", "") for a in s.get("artists", []))
                album = s.get("album", {}) or {}
                cover = album.get("picUrl", "") or ""
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover
                songs.append({
                    "id": s.get("id"),
                    "name": s.get("name", ""),
                    "artist": artists,
                    "album": album.get("name", ""),
                    "cover": cover,
                })
            self._send_json(200, {"ok": True, "songs": songs})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


# ── Server state ─────────────────────────────────────────────────────────────

class ServerState:
    def __init__(self, port: int):
        self.host = os.environ.get("HOST", "127.0.0.1")
        self.port = port
        self.shared_secret = _load_or_create_secret()
        self.data_dir = HERE / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "music_cache").mkdir(parents=True, exist_ok=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    port = int(os.environ.get("PORT", "9090"))
    state = ServerState(port)
    MusicHandler.state = state

    server = ThreadingHTTPServer((state.host, state.port), MusicHandler)
    logger.info("music starting on %s:%d", state.host, state.port)
    logger.info("Data dir: %s", state.data_dir)
    if state.shared_secret:
        logger.info("Auth token: %s", state.shared_secret[:8] + "...")
        logger.info("(Full token in %s)", HERE / ".secret")
    else:
        logger.warning("No shared secret — all requests allowed!")
    logger.info("Netease cookie: %s", "configured" if (HERE / ".netease_cred").exists() else "NOT FOUND — create .netease_cred with MUSIC_U=<value>")
    logger.info("Frontend: %s", "found" if (HERE.parent / "client").is_dir() else "not found (place files in ../client/)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
