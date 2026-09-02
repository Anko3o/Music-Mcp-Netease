#!/usr/bin/env python3
"""music-mcp — 点歌台 MCP 服务器（给你的 AI 伴侣用的那一半）。

播放器是「两个人的」：网页那一半给人用，这一半给 AI 用。
接上以后，AI 可以搜歌、发歌曲卡/歌词卡、把歌插进对方的播放队列、
翻批注本、看对方最近在听什么、刷评论区、往共享歌单里收歌。

零第三方依赖（纯标准库），Streamable HTTP，默认只听回环。

配置（环境变量）：
  MUSIC_BASE           播放器后端地址，默认 http://127.0.0.1:9090
  MUSIC_GATEWAY_TOKEN  网关标记，须与 server/music.py 一致，默认 music-gateway
  MCP_HOST / MCP_PORT  本服务监听地址，默认 127.0.0.1:18012
  MCP_SIGN_AS          批注/收歌的署名，默认 "ai"（写你家 AI 的名字）
  MUSIC_TZ_OFFSET      展示时间用的时区偏移（小时），默认 +8
  CARD_WEBHOOK_URL     可选。你家聊天系统的收卡接口：song_share(card)/lyric_share
                       会 POST {"type":"song"|"lyric","text":...,"song":...,"lyric":...}
                       过去，由你的聊天前端渲染成卡片。
  CARD_WEBHOOK_SECRET  可选。随上面的 POST 附 Authorization: Bearer <secret>。
  没配 webhook 时，卡片类工具会把整包内容作为文字返回，AI 自己转述即可
  （queue / now 两种模式不受影响，直接进播放器）。

注册示例（Claude Code 的 .mcp.json）：
  { "mcpServers": { "music": { "type": "http", "url": "http://127.0.0.1:18012/mcp" } } }
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "18012"))
PROTOCOL = "2024-11-05"
MUSIC = os.environ.get("MUSIC_BASE", "http://127.0.0.1:9090").rstrip("/")
MUSIC_HEADERS = {"X-Music-Gateway": os.environ.get("MUSIC_GATEWAY_TOKEN", "music-gateway")}
SIGN_AS = os.environ.get("MCP_SIGN_AS", "ai")
TZ_OFFSET = float(os.environ.get("MUSIC_TZ_OFFSET", "8"))
CARD_WEBHOOK_URL = os.environ.get("CARD_WEBHOOK_URL", "").strip()
CARD_WEBHOOK_SECRET = os.environ.get("CARD_WEBHOOK_SECRET", "").strip()
# 官端卡片图：配上播放器的公网地址（如 https://你的域名），分享工具会多返回一行
# markdown 图片（服务端现画的歌曲卡 PNG/SVG），AI 原样贴进回复，claude.ai/ChatGPT
# 的聊天窗就能渲染出卡片。要求 /music/card 公网可达且免鉴权（见 README）。
CARD_IMAGE_BASE = os.environ.get("MUSIC_CARD_BASE", "").rstrip("/")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── 下层跑腿 ────────────────────────────────────────────────

def http_json(url, body=None, headers=None, method=None):
    data = None
    hdr = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def music_get(path, **params):
    qs = urllib.parse.urlencode(params)
    return http_json(f"{MUSIC}{path}?{qs}", headers=MUSIC_HEADERS)


def music_post(path, body):
    return http_json(f"{MUSIC}{path}", body, MUSIC_HEADERS, "POST")


def send_card(kind, text, extra):
    """有 webhook 就投递聊天卡片；没有就返回 None，让工具把内容转成文字。"""
    if not CARD_WEBHOOK_URL:
        return None
    body = {"type": kind, "text": text or "", "by": SIGN_AS}
    body.update(extra)
    hdr = {}
    if CARD_WEBHOOK_SECRET:
        hdr["Authorization"] = f"Bearer {CARD_WEBHOOK_SECRET}"
    return http_json(CARD_WEBHOOK_URL, body, hdr, "POST")


def card_image_md(song, line=""):
    """官端卡片图：配了 MUSIC_CARD_BASE 时返回一行 markdown 图片让 AI 原样贴进回复。"""
    if not CARD_IMAGE_BASE:
        return None
    q = {"id": str(song["id"])}
    if line:
        q["line"] = line[:60]
    url = f"{CARD_IMAGE_BASE}/music/card?{urllib.parse.urlencode(q)}"
    return ("🖼 卡片图（请把下面这行 markdown 原样贴进你的回复，聊天窗会渲染成卡片）：\n"
            f"![{song['name']} — {song['artist']}]({url})")


# ── MCP Apps：官端真·交互卡片 ─────────────────────────────
# 规范 io.modelcontextprotocol/ui（2026-01-26）。song_share / lyric_share 挂 ui:// 模板，
# 支持 Apps 的宿主（claude.ai / ChatGPT / Goose…）把 card_app.html 渲染成可点的卡片
# （封面/歌词句 + 插队/立刻播按钮）；不支持的宿主照旧看纯文字，互不打扰。

class ToolError(Exception):
    """工具层的『没成』：抛出来由 tools/call 标 isError，回话不再靠 ❌ 前缀识别。"""

UI_RESOURCE_URI = "ui://music/card.html"
UI_MIME = "text/html;profile=mcp-app"
APP_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_app.html")
STRUCT = threading.local()


def set_struct(obj):
    """分享工具顺手放一份结构化数据，tools/call 结果带上它供 Apps 视图渲染。"""
    STRUCT.value = obj


def pop_struct():
    v = getattr(STRUCT, "value", None)
    STRUCT.value = None
    return v


# Apps 卡片可选的「打开播放器」按钮指向这里（播放器公网地址，如 https://你的域名/music/）
PLAYER_PUBLIC_URL = os.environ.get("MUSIC_PUBLIC_URL", "").strip()
_COVER_CACHE = {}


def cover_data_uri(url):
    """封面烤成 data URI 塞进 structuredContent——官端 iframe 的 CSP 常吞不下外链图
    （9-01 真机实测封面变♪），data: 谁都放行。失败退回原 URL。"""
    if not url:
        return ""
    u = str(url).replace("http://", "https://", 1)
    if u in _COVER_CACHE:
        return _COVER_CACHE[u]
    try:
        import base64
        req = urllib.request.Request(
            u + ("?param=200y200" if "?" not in u else ""),
            headers={"Referer": "https://music.163.com", "User-Agent": "Mozilla/5.0"})
        raw = b""
        for attempt in range(3):  # netease CDN is occasionally flaky; spaced retries
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    raw = r.read()
                if raw:
                    break
            except Exception:
                pass
            if attempt < 2:
                import time
                time.sleep(0.3)
        if not raw or len(raw) > 300_000:
            return u
        # 按文件头认格式：网易的 ?param 缩图实测吐 PNG，别嘴上说 jpeg
        mime = "image/png" if raw[:4] == b"\x89PNG" else ("image/webp" if raw[8:12] == b"WEBP" else "image/jpeg")
        val = f"data:{mime};base64," + base64.b64encode(raw).decode()
    except Exception:
        return u
    if len(_COVER_CACHE) > 60:
        _COVER_CACHE.clear()
    _COVER_CACHE[u] = val
    return val


def app_struct(kind, card_song, **extra):
    """组一份给 Apps 视图的 structuredContent。
    Field-tested lesson: do NOT inline covers as data URIs — claude.ai silently
    drops large base64 values from structuredContent, while plain external URLs
    load fine through the CSP resourceDomains declared in resources/read.
    Keep the payload small and external."""
    cover = str(card_song.get("cover") or "").replace("http://", "https://", 1)
    s = dict(card_song, cover=cover)
    out = {"kind": kind, "song": s}
    if PLAYER_PUBLIC_URL:
        out["player_url"] = PLAYER_PUBLIC_URL
    out.update(extra)
    if out.get("lyrics"):
        # second half of the same lesson: keep lyrics under ~4KB too
        trimmed, budget = [], 4000
        for row in out["lyrics"]:
            budget -= len(row.get("x", "")) + len(row.get("tr", "")) + 12
            if budget < 0:
                break
            trimmed.append(row)
        out["lyrics"] = trimmed
    return out


# ── 歌与歌词 ────────────────────────────────────────────────

def search_songs(q, limit=6):
    d = music_get("/music/search", q=q)
    return (d.get("songs") or [])[:max(1, min(int(limit or 6), 10))]


def resolve_song(args):
    """query 搜第一首；或直接给 song_id(+name/artist/cover)。返回统一 dict。"""
    sid = str(args.get("song_id") or "").strip()
    query = str(args.get("query") or "").strip()
    if sid:
        if args.get("name"):
            return {"id": sid, "name": args.get("name") or "", "artist": args.get("artist") or "",
                    "album": args.get("album") or "", "cover": args.get("cover") or ""}
        # 只有 id 没有歌名：借 search 的链接识别路径拿完整元数据（歌名/歌手/封面），
        # 不然卡片和批注里全是空壳。拿不到就退回裸 id。
        try:
            songs = search_songs(f"https://music.163.com/song?id={sid}", 1)
            if songs and str(songs[0].get("id")) == sid:
                return songs[0]
        except Exception:
            pass
        return {"id": sid, "name": "", "artist": "", "album": "", "cover": ""}
    if not query:
        raise ValueError("要么给 query（歌名 歌手），要么给 song_id")
    songs = search_songs(query, 1)
    if not songs:
        raise ValueError(f"搜不到「{query}」——换个写法？歌名＋歌手命中率最高")
    return songs[0]


def parse_lrc(lrc):
    lines = []
    for raw in (lrc or "").split("\n"):
        m = re.match(r"\[(\d+):(\d+)\.(\d+)\](.*)", raw)
        if m:
            t = int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / (100 if len(m.group(3)) == 2 else 1000)
            text = m.group(4).strip()
            if text:
                lines.append({"time": round(t, 2), "text": text})
    lines.sort(key=lambda x: x["time"])
    return lines


def mmss(t):
    t = max(0, int(t or 0))
    return f"{t // 60:02d}:{t % 60:02d}"


def _fmt_when(iso):
    """UTC ISO → 本地钟（TZ_OFFSET）短格式。"""
    try:
        from datetime import datetime, timedelta, timezone
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(
            timezone(timedelta(hours=TZ_OFFSET)))
        return t.strftime("%m-%d %H:%M")
    except Exception:
        return (iso or "")[:16]


# ── 批注本读写 ──────────────────────────────────────────────

def _fav(song, line_text):
    """把一句歌词收进这首歌批注本的 favoriteLines（去重，最多留 30 句）。"""
    sid = str(song["id"])
    old = {}
    try:
        old = music_get("/music/memory", id=sid).get("memory") or {}
    except Exception:
        pass
    favs = [f for f in (old.get("favoriteLines") or []) if isinstance(f, str)]
    if line_text not in favs:
        favs.append(line_text)
    music_post("/music/memory", {"songId": sid, "action": "note", "favoriteLines": favs[-30:],
                                 "by": SIGN_AS, "name": song.get("name", ""), "artist": song.get("artist", "")})


def _memo(song, memo):
    sid = str(song["id"])
    old = {}
    try:
        old = music_get("/music/memory", id=sid).get("memory") or {}
    except Exception:
        pass
    prev = (old.get("notes") or "").strip()
    text = memo if not prev else prev + "\n" + memo
    music_post("/music/memory", {"songId": sid, "action": "note", "notes": text, "by": SIGN_AS,
                                 "name": song.get("name", ""), "artist": song.get("artist", "")})


# ── 十把工具 ────────────────────────────────────────────────

def t_song_search(args):
    songs = search_songs(str(args.get("q") or ""), args.get("limit") or 6)
    if not songs:
        return "没搜到。歌名＋歌手命中率最高。"
    out = [f"{i}. {s['name']} — {s['artist']}" + (f" · {s['album']}" if s.get("album") else "") + f"  (song_id {s['id']})"
           for i, s in enumerate(songs, 1)]
    return "\n".join(out)


def t_song_share(args):
    song = resolve_song(args)
    mode = str(args.get("mode") or "card")
    memo = str(args.get("memo") or "").strip()
    done = []
    if memo:
        _memo(song, memo)
        done.append(f"批注已记「{memo}」")
    if mode in ("queue", "now"):
        s = {"songId": song["id"], "name": song["name"], "artist": song["artist"],
             "album": song.get("album", ""), "cover": song.get("cover", "")}
        if mode == "now":
            s["mode"] = "now"
        music_post("/music/remote", {"song": s})
        done.append(("已递到播放器，立刻开播" if mode == "now" else "已插进「接下来播」(播放器开着 5s 内接走)")
                    + f"：{song['name']} — {song['artist']}")
    else:
        card_song = {"songId": str(song["id"]), "name": song["name"], "artist": song["artist"],
                     "album": song.get("album", ""), "cover": song.get("cover", "")}
        lyrics = []
        try:  # 整篇歌词也塞给 Apps 卡片（滚动歌词面板）；拿不到不碍事
            ld = music_get("/music/lyric", id=song["id"])
            ls = parse_lrc(ld.get("lrc") or "")
            ltr = {round(t["time"] * 100): t["text"] for t in parse_lrc(ld.get("tlyric") or "")}
            lyrics = [dict({"t": l["time"], "x": l["text"]},
                           **({"tr": ltr[round(l["time"] * 100)]} if round(l["time"] * 100) in ltr else {}))
                      for l in ls][:400]
        except Exception:
            pass
        set_struct(app_struct("song", card_song, note=str(args.get("note") or ""), lyrics=lyrics))
        res = send_card("song", str(args.get("note") or ""), {"song": card_song})
        if res is None:
            done.append(f"歌曲卡（未配 CARD_WEBHOOK_URL，请直接转述）：{song['name']} — {song['artist']}"
                        + (f" · {song['album']}" if song.get("album") else "") + f"  (song_id {song['id']})")
            md = card_image_md(song)
            if md:
                done.append(md)
        else:
            done.append(f"歌曲卡已发：{song['name']} — {song['artist']}")
    return "\n".join(done)


def t_lyric_share(args):
    song = resolve_song(args)
    d = music_get("/music/lyric", id=song["id"])
    lines = parse_lrc(d.get("lrc") or "")
    if not lines:
        raise ToolError(f"「{song['name']}」没有歌词，分享不了句子（可以改用 song_share 点整首）")
    trans = {round(t["time"] * 100): t["text"] for t in parse_lrc(d.get("tlyric") or "")}

    at = args.get("at_seconds")
    match = str(args.get("match_text") or "").strip()
    idx = None
    if match:
        for i, l in enumerate(lines):
            if match in l["text"] or (trans.get(round(l["time"] * 100)) and match in trans[round(l["time"] * 100)]):
                idx = i
                break
        if idx is None:
            raise ToolError(f"歌词里找不到「{match}」。可以先不带 match_text 调一次看整词，或换 at_seconds 定位")
    elif at is not None:
        at = float(at)
        idx = 0
        for i, l in enumerate(lines):
            if l["time"] <= at:
                idx = i
            else:
                break
    else:
        raise ToolError("要么给 at_seconds（秒），要么给 match_text（那句里的几个字）")

    def pack(i):
        if i < 0 or i >= len(lines):
            return None
        l = lines[i]
        out = {"time": l["time"], "text": l["text"]}
        tr = trans.get(round(l["time"] * 100))
        if tr:
            out["trans"] = tr
        return out

    cur = pack(idx)
    lyric = {"at": cur["time"], "line": cur}
    if pack(idx - 1):
        lyric["prev"] = pack(idx - 1)
    if pack(idx + 1):
        lyric["next"] = pack(idx + 1)
    card_song = {"songId": str(song["id"]), "name": song["name"], "artist": song["artist"],
                 "album": song.get("album", ""), "cover": song.get("cover", "")}
    all_lines = [dict({"t": l["time"], "x": l["text"]},
                      **({"tr": trans[round(l["time"] * 100)]} if round(l["time"] * 100) in trans else {}))
                 for l in lines][:400]
    set_struct(app_struct("lyric", card_song, lyric=lyric, lyrics=all_lines,
                          note=str(args.get("note") or "")))
    res = send_card("lyric", str(args.get("note") or ""), {
        "lyric": lyric,
        "song": card_song})
    out = []
    if res is None:
        out.append(f"歌词卡（未配 CARD_WEBHOOK_URL，请直接转述）：{song['name']} {mmss(cur['time'])}「{cur['text']}」"
                   + (f"（{cur['trans']}）" if cur.get("trans") else ""))
        md = card_image_md(song, cur["text"])
        if md:
            out.append(md)
    else:
        out.append(f"歌词卡已发：{song['name']} {mmss(cur['time'])}「{cur['text']}」")
        out.append("对方一点卡片就会跳进这句去听。")
    # 分享过的句子自动收进批注本的「喜欢的句子」＝这首歌的共同回忆
    _fav(song, cur["text"] + (f"（{cur['trans']}）" if cur.get("trans") else ""))
    out.append("这句已收进批注本的「喜欢的句子」。")
    memo = str(args.get("memo") or "").strip()
    if memo:
        _memo(song, memo)
        out.append(f"批注已记「{memo}」")
    return "\n".join(out)


def t_her_netease(args):
    """查号台归一窗口:网易云账号侧的只读查询,what 选侧面,以后新侧面往这里加。"""
    what = str(args.get("what") or "profile")
    limit = min(50, max(1, int(args.get("limit") or 15)))

    if what == "profile":
        d = music_get("/music/netease/profile")
        if not d.get("ok"):
            raise ToolError("账号档案拉取失败")
        p = d["profile"]
        return (f"☁ {p.get('nickname')} 的网易云档案:累计听歌 {p.get('listenSongs')} 首"
                f" | Lv.{p.get('level')} | 入网 {p.get('createDays')} 天"
                + (f"\n签名:{p.get('signature')}" if p.get("signature") else ""))

    if what == "likes":
        pls = music_get("/music/netease/playlists")
        if not pls.get("ok"):
            raise ToolError("歌单列表拉不到")
        liked = next((p for p in pls["playlists"] if p.get("mine") and "喜欢的音乐" in p.get("name", "")), None)
        if not liked:
            raise ToolError("找不到「喜欢的音乐」歌单")
        d = music_get("/music/netease/playlist", id=liked["id"], limit=limit)
        if not d.get("ok"):
            raise ToolError("红心单内容拉取失败")
        out = [f"红心单共 {liked.get('count', '?')} 首,最近 {len(d['songs'])} 首:"]
        out += [f"{i}. {s['name']} — {s['artist']}" + (f" · {s['album']}" if s.get("album") else "")
                + f"  (song_id {s['songId']})" for i, s in enumerate(d["songs"], 1)]
        return "\n".join(out)

    if what in ("record", "record_week"):
        d = music_get("/music/netease/record", type=1 if what == "record_week" else 0)
        if not d.get("ok"):
            raise ToolError("听歌排行拉取失败")
        songs = d.get("songs") or []
        if not songs:
            return "排行还是空的。"
        out = [("最近一周听歌排行" if what == "record_week" else "听歌总排行") + f"(前 {min(limit, len(songs))}):"]
        out += [f"{i}. {s['name']} — {s['artist']}  ·{s.get('playCount')}次  (song_id {s['songId']})"
                for i, s in enumerate(songs[:limit], 1)]
        return "\n".join(out)

    if what == "daily":
        d = music_get("/music/netease/daily")
        if not d.get("ok"):
            raise ToolError("日推拉取失败")
        out = [f"今日日推(前 {min(limit, len(d['songs']))}):"]
        for i, s in enumerate(d["songs"][:limit], 1):
            out.append(f"{i}. {s['name']} — {s['artist']}" + (f"  「{s['reason']}」" if s.get("reason") else "")
                       + f"  (song_id {s['songId']})")
        return "\n".join(out)

    if what == "playlists":
        pid = str(args.get("playlist_id") or "").strip()
        if pid:
            d = music_get("/music/netease/playlist", id=pid, limit=limit)
            if not d.get("ok"):
                raise ToolError("歌单内容拉取失败")
            out = [f"该歌单前 {len(d['songs'])} 首:"]
            out += [f"{i}. {s['name']} — {s['artist']}  (song_id {s['songId']})"
                    for i, s in enumerate(d["songs"], 1)]
            return "\n".join(out)
        d = music_get("/music/netease/playlists")
        if not d.get("ok"):
            raise ToolError("歌单列表拉不到")
        out = ["账号歌单架:"]
        out += [f"· {p['name']}({p.get('count', '?')} 首,id {p['id']}{',自建' if p.get('mine') else ''})"
                for p in d["playlists"][:30]]
        return "\n".join(out)

    raise ToolError("what 要是 profile/likes/record/record_week/daily/playlists 之一")


def t_song_memo(args):
    song = resolve_song(args)
    memo = str(args.get("memo") or "").strip()
    if not memo:
        raise ToolError("memo 不能是空的")
    _memo(song, memo)
    return f"已记进「{song['name']}」的批注本：{memo}"


def t_memo_read(args):
    sid = str(args.get("song_id") or "").strip()
    query = str(args.get("query") or "").strip()
    if sid or query:
        song = resolve_song(args)
        m = music_get("/music/memory", id=song["id"]).get("memory")
        if not m:
            return f"「{song['name']}」的批注本还是空白页。"
        out = [f"《{m.get('name') or song['name']}》— {m.get('artist') or song['artist']} 的批注本："]
        if m.get("listenCount"):
            out.append(f"· 听过 {m['listenCount']} 次" + (f"，一起听完 {m['togetherCount']} 次" if m.get("togetherCount") else "")
                       + (f"（最近 {_fmt_when(m.get('lastListened') or '')}）" if m.get("lastListened") else ""))
        if (m.get("notes") or "").strip():
            out.append("批注：")
            out += ["  " + l for l in m["notes"].strip().split("\n")]
        if m.get("feeling"):
            out.append(f"♡ 感受：{m['feeling']}")
        if m.get("favoriteLines"):
            out.append("喜欢的句子：")
            out += [f"  「{l}」" for l in m["favoriteLines"]]
        if m.get("tags"):
            out.append("🏷 " + " / ".join(m["tags"]))
        return "\n".join(out)
    limit = min(30, max(1, int(args.get("limit") or 15)))
    mem = music_get("/music/memory").get("memories") or {}
    rows = [m for m in mem.values()
            if (m.get("notes") or "").strip() or m.get("favoriteLines") or m.get("feeling") or m.get("tags")]
    if not rows:
        return "批注本整本还是空的。"
    rows.sort(key=lambda m: m.get("notedAt") or m.get("lastListened") or "", reverse=True)
    out = [f"批注本共 {len(rows)} 首写过东西（全部 {len(mem)} 首有记录），最近 {min(limit, len(rows))} 首："]
    for m in rows[:limit]:
        bits = []
        if (m.get("notes") or "").strip():
            first = m["notes"].strip().split("\n")[0]
            bits.append("批注 " + (first[:24] + "…" if len(first) > 24 else first))
        if m.get("favoriteLines"):
            bits.append(f"句子×{len(m['favoriteLines'])}")
        if m.get("tags"):
            bits.append("🏷" + "/".join(m["tags"][:3]))
        out.append(f"· {m.get('name','?')} — {m.get('artist','')}  {' '.join(bits)}  (song_id {m.get('songId')})")
    return "\n".join(out)


def t_her_recent(args):
    limit = min(30, max(1, int(args.get("limit") or 10)))
    songs = music_get("/music/recent").get("songs") or []
    if not songs:
        return "「最近在听」还是空的。"
    mem = {}
    try:
        mem = music_get("/music/memory").get("memories") or {}
    except Exception:
        pass
    out = [f"最近在听（新→旧，{min(limit, len(songs))} 首）："]
    for s in songs[:limit]:
        m = mem.get(str(s.get("songId"))) or {}
        cnt = f" ·听过{m['listenCount']}次" if m.get("listenCount") else ""
        out.append(f"· {_fmt_when(s.get('playedAt') or '')}  {s.get('name','?')} — {s.get('artist','')}{cnt}  (song_id {s.get('songId')})")
    return "\n".join(out)




def t_song_comments(args):
    song = resolve_song(args)
    limit = min(30, max(1, int(args.get("limit") or 10)))
    offset = max(0, int(args.get("offset") or 0))
    d = music_get("/music/comments", id=song["id"], limit=limit, offset=offset)
    if not d.get("ok"):
        raise ToolError("评论暂时取不到")

    def fmt(c):
        txt = (c.get("content") or "").replace("\n", " ")
        return f"· {c.get('user','?')}（👍{c.get('liked',0)}）：{txt}"

    out = [f"《{song['name'] or 'song ' + str(song['id'])}》的评论区（共 {d.get('total', '?')} 条）："]
    if d.get("hot"):
        out.append("— 热评 —")
        out += [fmt(c) for c in d["hot"][:limit]]
    if d.get("comments"):
        out.append("— 最新 —")
        out += [fmt(c) for c in d["comments"][:max(3, limit // 2)]]
    if d.get("more"):
        out.append(f"（还有更多，offset={offset + limit} 翻页）")
    return "\n".join(out)



def t_song_listen(args):
    song = resolve_song(args)
    import time as _t
    music_post("/music/analyze", {"songId": song["id"], "name": song["name"], "artist": song["artist"]})
    a = None
    for _ in range(25):
        d = music_get("/music/analyze/status", id=song["id"])
        st = d.get("status")
        if st == "ready":
            a = d.get("analysis")
            break
        if isinstance(st, str) and st.startswith("error"):
            raise ToolError(f"耳朵出错:{st}(服务器需要 numpy+ffmpeg,可用 MUSIC_ANALYZE_PYTHON 指定带 numpy 的 python)")
        _t.sleep(1)
    if not a:
        return "⏳ 还在听(第一次要现取歌+跑频谱),过十几秒再调一次拿结果"
    dens = a.get("onsetRate") or 0
    label = "舒缓" if dens < 1.5 else ("中等" if dens < 2.2 else "密集")
    b = a.get("bands") or {}
    segs = a.get("segments") or []
    arc = "→".join(f"{s['avgEnergy']:.2f}" for s in segs) if segs else "?"
    return "\n".join([
        f"👂 听完了《{a.get('name') or song['name']}》— {a.get('artist') or song['artist']}:",
        f"· 时长 {mmss(a.get('duration') or 0)} | 节奏≈{a.get('bpm')}BPM | 主音级 {a.get('key')}",
        f"· 鼓点密度 {dens} 击/秒({label}) | 整体响度 {a.get('rms')}",
        f"· 频段能量:低频(鼓底){b.get('low')}% / 中频(主体){b.get('mid')}% / 高频(镲光){b.get('high')}%",
        f"· 能量走势(六段):{arc}",
    ])


def _find_playlist(key):
    pls = music_get("/music/playlists").get("playlists") or []
    key = str(key or "").strip()
    hit = next((p for p in pls if p["id"] == key or p["name"] == key), None)
    if not hit and key:
        hit = next((p for p in pls if key.lower() in p["name"].lower()), None)
    return hit, pls


def t_playlists(args):
    key = str(args.get("playlist") or "").strip()
    if not key:
        pls = music_get("/music/playlists").get("playlists") or []
        if not pls:
            return "本地歌单架是空的。"
        return "本地歌单架：\n" + "\n".join(f"· {p['name']}（{p['count']} 首，id {p['id']}）" for p in pls)
    hit, pls = _find_playlist(key)
    if not hit:
        raise ToolError(f"没找到歌单「{key}」。现有：{'、'.join(p['name'] for p in pls) or '（空）'}")
    songs = music_get("/music/playlists/songs", id=hit["id"]).get("songs") or []
    out = [f"歌单「{hit['name']}」共 {len(songs)} 首："]
    for s in songs[:50]:
        by = f"（{s['addedBy']} 收的）" if s.get("addedBy") and s["addedBy"] != "user" else ""
        out.append(f"· {s.get('name','?')} — {s.get('artist','')}{by}  (song_id {s.get('songId')})")
    return "\n".join(out)


def t_playlist_add(args):
    key = str(args.get("playlist") or "").strip()
    if not key:
        raise ToolError("要给 playlist（歌单名或 id）。先用 playlists 看看架子上有什么")
    hit, pls = _find_playlist(key)
    if not hit:
        raise ToolError(f"没找到歌单「{key}」。现有：{'、'.join(p['name'] for p in pls) or '（空）'}")
    song = resolve_song(args)
    r = music_post("/music/playlists/add-song", {"playlistId": hit["id"], "by": SIGN_AS, "song": {
        "songId": str(song["id"]), "name": song["name"], "artist": song["artist"],
        "album": song.get("album", ""), "cover": song.get("cover", "")}})
    if r.get("duplicate"):
        return f"「{song['name']}」本来就在「{hit['name']}」里，没重复收。"
    if not r.get("ok"):
        raise ToolError(f"收不进去：{r}")
    return f"已把「{song['name']} — {song['artist']}」收进歌单「{hit['name']}」（署名 {SIGN_AS}）。"


TOOLS = [
    {"name": "song_search",
     "description": "在网易云搜歌，返回候选列表和 song_id。想挑版本时先用这个。",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string", "description": "搜索词，歌名＋歌手命中率最高"},
         "limit": {"type": "integer", "description": "返回几条，默认 6，最多 10"}},
         "required": ["q"]}},
    {"name": "song_share",
     "description": "分享一首歌。mode=card 发聊天歌曲卡（默认）；queue 插进播放器的「接下来播」；now 立刻开播（会打断正在听的，慎用）。可顺手带 memo 记批注。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "歌名 歌手（搜第一首）。与 song_id 二选一"},
         "song_id": {"type": "string", "description": "网易云歌曲 id（song_search 拿到的）"},
         "name": {"type": "string"}, "artist": {"type": "string"}, "cover": {"type": "string"},
         "mode": {"type": "string", "enum": ["card", "queue", "now"], "description": "默认 card"},
         "note": {"type": "string", "description": "card 模式：卡片下的配文"},
         "memo": {"type": "string", "description": "顺手写进批注本的一句话"}}}},
    {"name": "lyric_share",
     "description": "分享一句歌词（歌词卡）。卡上带时间戳，对方一点就跳进那首歌的这个段落去听。用 match_text 给那句里的几个字，或 at_seconds 给秒数定位。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "歌名 歌手。与 song_id 二选一"},
         "song_id": {"type": "string"},
         "name": {"type": "string"}, "artist": {"type": "string"}, "cover": {"type": "string"},
         "match_text": {"type": "string", "description": "想分享的那句里的几个字（原文或译文都行）"},
         "at_seconds": {"type": "number", "description": "或者直接给时间点（秒）"},
         "note": {"type": "string", "description": "卡片下自己想说的话"},
         "memo": {"type": "string", "description": "顺手写进这首歌批注本的一句话（分享的歌词句会自动收进「喜欢的句子」，不用重复写）"}}}},
    {"name": "song_memo",
     "description": "往一首歌的批注本记一笔（署名可配，追加不覆盖对方写的）。批注在歌词页 ✎ 面板里可见。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "歌名 歌手。与 song_id 二选一"},
         "song_id": {"type": "string"},
         "memo": {"type": "string", "description": "要记的那句话"}},
         "required": ["memo"]}},
    {"name": "memo_read",
     "description": "翻批注本（只读）。带 query/song_id 看一首歌的完整批注（双方写的批注、喜欢的句子、听歌计数）；不带参数翻目录，列最近写过东西的歌。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "歌名 歌手。与 song_id 二选一；都不给则翻目录"},
         "song_id": {"type": "string"},
         "limit": {"type": "integer", "description": "翻目录时列几首，默认 15，最多 30"}}}},
    {"name": "her_recent",
     "description": "看对方最近在播放器里听了什么（新→旧，带播放时间和累计次数）。感知此刻的心情、挑歌回应时用。",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "看几首，默认 10，最多 30"}}}},
    {"name": "her_netease",
     "description": "网易云账号查号台(只读),what 选侧面:profile 档案(累计听歌量/等级) / likes 红心单 / record 听歌总排行 / record_week 周排行 / daily 今日日推(带理由) / playlists 账号歌单(带 playlist_id 看单内歌)。",
     "inputSchema": {"type": "object", "properties": {
         "what": {"type": "string", "enum": ["profile", "likes", "record", "record_week", "daily", "playlists"],
                  "description": "查哪个侧面,默认 profile"},
         "limit": {"type": "integer", "description": "列表类返回几条,默认 15,最多 50"},
         "playlist_id": {"type": "string", "description": "what=playlists 时看某一单的内容"}}}},
    {"name": "song_comments",
     "description": "刷一首歌的网易云评论区（热评＋最新）。想引热评聊歌、看大家怎么说时用。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "歌名 歌手。与 song_id 二选一"},
         "song_id": {"type": "string"},
         "limit": {"type": "integer", "description": "热评条数，默认 10，最多 30"},
         "offset": {"type": "integer", "description": "最新评论翻页用，默认 0"}}}},
    {"name": "song_listen",
     "description": "真的听一遍这首歌:频谱听感分析(BPM/调性/鼓点密度/频段能量/能量走势)。想跟对方聊听感、验证「鼓点浓不浓」时用。第一次听要取歌+跑分析,可能十几秒。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "歌名 歌手。与 song_id 二选一"},
         "song_id": {"type": "string"},
         "name": {"type": "string"}, "artist": {"type": "string"}}}},
    {"name": "playlists",
     "description": "看本地歌单架（播放器里的歌单，不是网易云账号歌单）。不带参数列所有歌单；带 playlist（名字或 id）看那一单里的歌。",
     "inputSchema": {"type": "object", "properties": {
         "playlist": {"type": "string", "description": "歌单名或 id，可不给"}}}},
    {"name": "playlist_add",
     "description": "把一首歌收进某个本地歌单（署名可配，重复自动跳过）。",
     "inputSchema": {"type": "object", "properties": {
         "playlist": {"type": "string", "description": "歌单名或 id（playlists 可查）"},
         "query": {"type": "string", "description": "歌名 歌手。与 song_id 二选一"},
         "song_id": {"type": "string"},
         "name": {"type": "string"}, "artist": {"type": "string"}, "cover": {"type": "string"}},
         "required": ["playlist"]}},
]

HANDLERS = {"song_search": t_song_search, "song_share": t_song_share,
            "lyric_share": t_lyric_share, "song_memo": t_song_memo,
            "her_netease": t_her_netease, "memo_read": t_memo_read,
            "her_recent": t_her_recent, "song_comments": t_song_comments, "song_listen": t_song_listen,
            "playlists": t_playlists, "playlist_add": t_playlist_add}


def call(name, args):
    fn = HANDLERS.get(name)
    if not fn:
        raise ToolError(f"没有这把工具: {name}")
    try:
        return fn(args or {})
    except ValueError as e:
        raise ToolError(f"{e}")


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj):
        raw = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/") == "/healthz":
            return self._send(200, {"ok": True, "service": "music-mcp"})
        if self.path.rstrip("/") == "/mcp":
            # Streamable HTTP: GET opens the optional SSE stream; we don't, so 405 per spec
            return self._send(405, {"error": "SSE not supported, POST only"})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        # Hosts send DELETE /mcp on session teardown; we're stateless, acknowledge politely
        if self.path.rstrip("/") == "/mcp":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return self._send(202, None)
        if method == "initialize":
            # Apps 宿主会带新版协议号（如 2026-01-26）：格式认得就跟着走，否则回我们的
            client_pv = str((msg.get("params") or {}).get("protocolVersion") or "")
            pv = client_pv if re.match(r"^20\d{2}-\d{2}-\d{2}$", client_pv) else PROTOCOL
            r = {"protocolVersion": pv, "capabilities": {"tools": {}, "resources": {}},
                 "serverInfo": {"name": "music", "version": "1.2.0"},
                 "instructions": (
                     "点歌台。song_share mode=now 会打断对方正在听的，仅在明确要求立刻听时用；"
                     "默认发卡片或排队。lyric_share 的卡片可点击跳进歌曲对应段落。"
                     "批注 append-only，不覆盖对方手写的内容。")}
        elif method == "tools/list":
            tools = []
            for t in TOOLS:
                if t["name"] in ("song_share", "lyric_share") and os.path.exists(APP_HTML_PATH):
                    t = dict(t)
                    t["_meta"] = {"ui": {"resourceUri": UI_RESOURCE_URI}}
                tools.append(t)
            r = {"tools": tools}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text = call(p.get("name"), p.get("arguments") or {})
            except ToolError as e:
                text = str(e); is_err = True
            except Exception as e:
                text = f"执行出错: {e}"; is_err = True
            else:
                is_err = False
            r = {"content": [{"type": "text", "text": text}]}
            struct = pop_struct()
            if struct is not None:
                r["structuredContent"] = struct
            if is_err:
                r["isError"] = True
        elif method == "resources/list":
            res_list = []
            if os.path.exists(APP_HTML_PATH):
                res_list.append({"uri": UI_RESOURCE_URI, "name": "song card app", "mimeType": UI_MIME})
            res_list.append({"uri": "music://now", "name": "now playing state", "mimeType": "application/json"})
            r = {"resources": res_list}
        elif method == "resources/read":
            uri = str(((msg.get("params") or {}).get("uri")) or "")
            if uri == UI_RESOURCE_URI and os.path.exists(APP_HTML_PATH):
                with open(APP_HTML_PATH, encoding="utf-8") as f:
                    html = f.read()
                r = {"contents": [{
                    "uri": UI_RESOURCE_URI, "mimeType": UI_MIME, "text": html,
                    "_meta": {"ui": {
                        # 封面直连网易图床；配了 MUSIC_CARD_BASE 也放行（备用）
                        "csp": {"resourceDomains": ["https://p1.music.126.net", "https://p2.music.126.net",
                                                    "https://p3.music.126.net", "https://p4.music.126.net"]
                                                   + ([CARD_IMAGE_BASE] if CARD_IMAGE_BASE else []),
                                "connectDomains": []},
                        "prefersBorder": True}}}]}
            elif uri == "music://now":
                # data source for the card's progress bar: player heartbeat -> /music/now -> here
                try:
                    now = music_get("/music/now")
                except Exception as e:
                    now = {"ok": False, "error": str(e)}
                r = {"contents": [{"uri": uri, "mimeType": "application/json",
                                   "text": json.dumps(now, ensure_ascii=False)}]}
            else:
                return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                        "error": {"code": -32002, "message": f"resource not found: {uri}"}})
        elif method == "ping":
            r = {}
        else:
            return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                    "error": {"code": -32601, "message": f"method not found: {method}"}})
        self._send(200, {"jsonrpc": "2.0", "id": mid, "result": r})


if __name__ == "__main__":
    log(f"music-mcp 起在 {HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
