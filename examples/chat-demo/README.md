# chat-demo · 「一起听」宿主端示范

README 实拍图里那层聊天页和进度药丸,浓缩成这一页**零依赖的最小实现**——接上就能用,照着改就能长进你家的聊天系统。

它演示了宿主端的全部四件套:

- **♪ 抽屉** — 播放器以 iframe 嵌进来,从底部滑出(播放器检测到自己在 iframe 里会自动切成抽屉版式)
- **进度药丸** — 顶栏的小胶囊:封面、歌名、进度条、时间,全靠播放器每秒推的 `music:tick` 喂,不自己起计时器
- **歌曲卡** — 聊天消息里的「一起听 ▶」,点了让对方的播放器直接开播
- **歌词卡** — 带时间戳,点「跳进这句去听」播放器落针到那一句;播放器歌词页「递过去」的直聊消息也会落回聊天流

## 跑起来

**前提:demo 必须和播放器同源**(播放器 `postMessage` 的目标是 `location.origin`,跨域消息一律静默丢弃)。用任意反代把两者挂在同一个域名下即可,例如 Caddy:

```caddy
handle /music/* {
    reverse_proxy 127.0.0.1:9090 { header_up X-Music-Gateway music-gateway }
}
handle /chat-demo/* {
    root * /path/to/music-mcp-netease/examples
    file_server
}
```

打开 `https://你的域名/chat-demo/`。播放器路径默认 `/music/`,不一样就用 `?player=/别的路径/` 覆盖。

## 协议速查（全部走 `postMessage`）

播放器 → 宿主:

| 消息 | 载荷 | 什么时候来 |
|---|---|---|
| `music:song` | `{song:{songId,name,artist,album,cover,playing}}` | 换歌时、以及应答 `music:ask` |
| `music:tick` | `{at, duration, playing}` | 播放中每秒一次 |
| `music:lyric` | `{hasLyric, at, prev, line, next, song}` | 应答 `music:lyric-now`(当前句带前后句和翻译) |
| `music:lyric-send` | `{song, at, prev, line, next, note}` | 播放器内直聊:对方在歌词页写完直接递出 |

宿主 → 播放器:

| 消息 | 载荷 | 干什么 |
|---|---|---|
| `music:ask` | `{}` | 要一份当前播放状态(开抽屉时喊一声) |
| `music:play` | `{song:{songId,name,artist,album,cover}, at?}` | 播这首;带 `at`(秒)则落针到那个时间点 |
| `music:lyric-now` | `{}` | 要当前唱到的那句(做「聊这句」功能) |

`prev` / `line` / `next` 的形状都是 `{time, text, trans?}`。

## 接进你家

demo 里的 `send()` 只往本地消息流里塞气泡——真实接入时把它换成你家的消息通道,再把 `music:lyric-send` 的处理接到同一条通道上,双向就通了。AI 那一侧的点歌/歌词卡由 [`mcp/`](../../mcp/) 的十一把工具负责,两边合起来就是 README 实拍图里的完整体。
