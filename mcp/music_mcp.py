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


# ── 歌与歌词 ────────────────────────────────────────────────

def search_songs(q, limit=6):
    d = music_get("/music/search", q=q)
    return (d.get("songs") or [])[:max(1, min(int(limit or 6), 10))]


def resolve_song(args):
    """query 搜第一首；或直接给 song_id(+name/artist/cover)。返回统一 dict。"""
    sid = str(args.get("song_id") or "").strip()
    query = str(args.get("query") or "").strip()
    if sid:
        return {"id": sid, "name": args.get("name") or "", "artist": args.get("artist") or "",
                "album": args.get("album") or "", "cover": args.get("cover") or ""}
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
        done.append(f"✎ 批注已记「{memo}」")
    if mode in ("queue", "now"):
        s = {"songId": song["id"], "name": song["name"], "artist": song["artist"],
             "album": song.get("album", ""), "cover": song.get("cover", "")}
        if mode == "now":
            s["mode"] = "now"
        music_post("/music/remote", {"song": s})
        done.append(("▶ 已递到播放器,立刻开播" if mode == "now" else "⏯ 已插进「接下来播」(播放器开着 5s 内接走)")
                    + f"：{song['name']} — {song['artist']}")
    else:
        res = send_card("song", str(args.get("note") or ""), {"song": {
            "songId": str(song["id"]), "name": song["name"], "artist": song["artist"],
            "album": song.get("album", ""), "cover": song.get("cover", "")}})
        if res is None:
            done.append(f"♪ 歌曲卡（未配 CARD_WEBHOOK_URL，请直接转述）：{song['name']} — {song['artist']}"
                        + (f" · {song['album']}" if song.get("album") else "") + f"  (song_id {song['id']})")
        else:
            done.append(f"♪ 歌曲卡已发：{song['name']} — {song['artist']}")
    return "\n".join(done)


def t_lyric_share(args):
    song = resolve_song(args)
    d = music_get("/music/lyric", id=song["id"])
    lines = parse_lrc(d.get("lrc") or "")
    if not lines:
        return f"❌ 「{song['name']}」没有歌词，分享不了句子（可以改用 song_share 点整首）"
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
            return f"❌ 歌词里找不到「{match}」。可以先不带 match_text 调一次看整词，或换 at_seconds 定位"
    elif at is not None:
        at = float(at)
        idx = 0
        for i, l in enumerate(lines):
            if l["time"] <= at:
                idx = i
            else:
                break
    else:
        return "❌ 要么给 at_seconds（秒），要么给 match_text（那句里的几个字）"

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
    res = send_card("lyric", str(args.get("note") or ""), {
        "lyric": lyric,
        "song": {"songId": str(song["id"]), "name": song["name"], "artist": song["artist"],
                 "album": song.get("album", ""), "cover": song.get("cover", "")}})
    out = []
    if res is None:
        out.append(f"🎧 歌词卡（未配 CARD_WEBHOOK_URL，请直接转述）：{song['name']} {mmss(cur['time'])}「{cur['text']}」"
                   + (f"（{cur['trans']}）" if cur.get("trans") else ""))
    else:
        out.append(f"🎧 歌词卡已发：{song['name']} {mmss(cur['time'])}「{cur['text']}」")
        out.append("对方一点卡片就会跳进这句去听。")
    # 分享过的句子自动收进批注本的「喜欢的句子」＝这首歌的共同回忆
    _fav(song, cur["text"] + (f"（{cur['trans']}）" if cur.get("trans") else ""))
    out.append("✧ 这句已收进批注本的「喜欢的句子」。")
    memo = str(args.get("memo") or "").strip()
    if memo:
        _memo(song, memo)
        out.append(f"✎ 批注已记「{memo}」")
    return "\n".join(out)


def t_her_likes(args):
    limit = min(50, max(1, int(args.get("limit") or 15)))
    pls = music_get("/music/netease/playlists")
    if not pls.get("ok"):
        return "❌ 歌单列表拉不到（要配网易云 cookie 才有账号歌单）"
    liked_pl = next((p for p in pls["playlists"] if p.get("mine") and "喜欢的音乐" in p.get("name", "")), None)
    if not liked_pl:
        return "❌ 找不到「喜欢的音乐」歌单"
    d = music_get("/music/netease/playlist", id=liked_pl["id"], limit=limit)
    if not d.get("ok"):
        return "❌ 红心单内容拉取失败"
    out = [f"红心单共 {liked_pl.get('count', '?')} 首,最近 {len(d['songs'])} 首:"]
    for i, s in enumerate(d["songs"], 1):
        out.append(f"{i}. {s['name']} — {s['artist']}" + (f" · {s['album']}" if s.get("album") else "") + f"  (song_id {s['songId']})")
    return "\n".join(out)


def t_song_memo(args):
    song = resolve_song(args)
    memo = str(args.get("memo") or "").strip()
    if not memo:
        return "❌ memo 不能是空的"
    _memo(song, memo)
    return f"✎ 已记进「{song['name']}」的批注本：{memo}"


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
            out.append("✎ 批注：")
            out += ["  " + l for l in m["notes"].strip().split("\n")]
        if m.get("feeling"):
            out.append(f"♡ 感受：{m['feeling']}")
        if m.get("favoriteLines"):
            out.append("✧ 喜欢的句子：")
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
            bits.append("✎" + (first[:24] + "…" if len(first) > 24 else first))
        if m.get("favoriteLines"):
            bits.append(f"✧句子×{len(m['favoriteLines'])}")
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
        return "❌ 评论暂时取不到"

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
        return f"❌ 没找到歌单「{key}」。现有：{'、'.join(p['name'] for p in pls) or '（空）'}"
    songs = music_get("/music/playlists/songs", id=hit["id"]).get("songs") or []
    out = [f"歌单「{hit['name']}」共 {len(songs)} 首："]
    for s in songs[:50]:
        by = f"（{s['addedBy']} 收的）" if s.get("addedBy") and s["addedBy"] != "user" else ""
        out.append(f"· {s.get('name','?')} — {s.get('artist','')}{by}  (song_id {s.get('songId')})")
    return "\n".join(out)


def t_playlist_add(args):
    key = str(args.get("playlist") or "").strip()
    if not key:
        return "❌ 要给 playlist（歌单名或 id）。先用 playlists 看看架子上有什么"
    hit, pls = _find_playlist(key)
    if not hit:
        return f"❌ 没找到歌单「{key}」。现有：{'、'.join(p['name'] for p in pls) or '（空）'}"
    song = resolve_song(args)
    r = music_post("/music/playlists/add-song", {"playlistId": hit["id"], "by": SIGN_AS, "song": {
        "songId": str(song["id"]), "name": song["name"], "artist": song["artist"],
        "album": song.get("album", ""), "cover": song.get("cover", "")}})
    if r.get("duplicate"):
        return f"「{song['name']}」本来就在「{hit['name']}」里，没重复收。"
    if not r.get("ok"):
        return f"❌ 收不进去：{r}"
    return f"➕ 已把「{song['name']} — {song['artist']}」收进歌单「{hit['name']}」（署名 {SIGN_AS}）。"


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
    {"name": "her_likes",
     "description": "看对方最近红心了哪些歌（网易云「喜欢的音乐」，只读）。挑歌回赠、感知最近的口味时用。",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "看最近几首，默认 15，最多 50"}}}},
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
    {"name": "song_comments",
     "description": "刷一首歌的网易云评论区（热评＋最新）。想引热评聊歌、看大家怎么说时用。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "歌名 歌手。与 song_id 二选一"},
         "song_id": {"type": "string"},
         "limit": {"type": "integer", "description": "热评条数，默认 10，最多 30"},
         "offset": {"type": "integer", "description": "最新评论翻页用，默认 0"}}}},
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
            "her_likes": t_her_likes, "memo_read": t_memo_read,
            "her_recent": t_her_recent, "song_comments": t_song_comments,
            "playlists": t_playlists, "playlist_add": t_playlist_add}


def call(name, args):
    fn = HANDLERS.get(name)
    if not fn:
        return f"❌ 没有这把工具: {name}"
    try:
        return fn(args or {})
    except ValueError as e:
        return f"❌ {e}"


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
            r = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                 "serverInfo": {"name": "music", "version": "1.1.0"},
                 "instructions": (
                     "点歌台。song_share mode=now 会打断对方正在听的，仅在明确要求立刻听时用；"
                     "默认发卡片或排队。lyric_share 的卡片可点击跳进歌曲对应段落。"
                     "批注 append-only，不覆盖对方手写的内容。")}
        elif method == "tools/list":
            r = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text = call(p.get("name"), p.get("arguments") or {})
            except Exception as e:
                text = f"❌ 执行出错: {e}"
            r = {"content": [{"type": "text", "text": text}]}
            if text.startswith("❌"):
                r["isError"] = True
        elif method == "ping":
            r = {}
        else:
            return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                    "error": {"code": -32601, "message": f"method not found: {method}"}})
        self._send(200, {"jsonrpc": "2.0", "id": mid, "result": r})


if __name__ == "__main__":
    log(f"music-mcp 起在 {HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
