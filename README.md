# music-mcp (netease)

**A music player for two.** 一个为「两个人」设计的自部署网易云播放器——另一位可以是人，也可以是你的 AI 伴侣。

> Fork of [eryu（耳屿）](https://github.com/sebastianevan200-stack/eryu) by 沈妤（錯認水）— a self-hosted NetEase Cloud Music player built for listening together. 本仓库在其之上长出了一整个家的功能。

纯 Python 标准库 + 原生 JS，零依赖，一条命令跑起来。

---

## 它和普通播放器哪里不一样

普通播放器是「我听歌」。这个是「**我们听歌**」：

- 🎧 **点歌 MCP** — 你的 AI 伴侣可以搜歌、发歌曲卡、把歌直接插进你的播放队列（排队或立刻播）
- 📝 **歌词卡·可跳转** — AI 分享一句歌词给你，你点一下卡片，播放器打开、唱针直接落在那一句上
- 💬 **播放器内直聊** — 歌词页长按任意一句，就地写一句话递给对方，不用切走
- ✎ **批注本** — 每首歌可以记感受、打标签；双方分享过的歌词句自动收进「喜欢的句子」＝这首歌的共同回忆
- 🫶 **听歌计数** — 听过几次、一起听完几次，都记账

以及一个完整的播放器该有的一切：

- 🔍 搜歌 & 播放（网易云曲库，自带 VIP cookie 放无损）
- 📜 同步歌词 + 中文翻译，逐字点亮动效（文字PV 风，概念致敬 [folia-major](https://github.com/chthollyphile/folia-major)）
- 🎬 **MV 播放**（1080P，可切清晰度）
- ☁️ **账号同步** — 你自己的网易云歌单、每日推荐（带推荐理由）、红心列表全量镜像
- ♥ **红心双向同步** — 在这里点的心，同步回你的网易云
- 💬 **歌曲评论区** — 热评 + 最新，一页页刷
- 📋 歌单管理 / 加入歌单 / 「接下来播」优先队列（手动插入的歌永远先播，不被切歌单冲掉）
- 🔁 播放模式三档：顺序 / 单曲循环 / 随机
- 📱 移动端优先，可嵌 iframe 当聊天页的「一起听」抽屉

## 快速开始

```bash
git clone <this-repo>
cd music-mcp-netease

# 你的网易云 cookie（决定曲库权限与账号同步）
echo "MUSIC_U=your_cookie_here" > server/.netease_cred

python3 server/eryu.py          # 默认 :9090
```

打开 `http://localhost:9090`。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `9090` | 服务端口 |
| `ERYU_GATEWAY_TOKEN` | `eryu-gateway` | 反代内部标记：反代（如 Caddy/Nginx）注入 `X-Eryu-Gateway: <该值>` 头即免 token（推荐公网部署方式，由反代做鉴权，浏览器不存第二枚 token） |

不走反代时，首次启动会在 `server/.secret` 生成访问 token，前端用 `?token=` 携带。

### 反代示例（Caddy）

```caddy
redir /music /music/ 308
handle /music/ {
    basic_auth { you <bcrypt-hash> }
    rewrite * /
    reverse_proxy 127.0.0.1:9090 { header_up X-Eryu-Gateway eryu-gateway }
}
handle /music/* {
    basic_auth { you <bcrypt-hash> }
    reverse_proxy 127.0.0.1:9090 { header_up X-Eryu-Gateway eryu-gateway }
}
```

## API 速览

上游全部端点保留（search / url / stream / lyric / playlist(s) / recent / memory / roam …），本 fork 新增：

| 端点 | 说明 |
|---|---|
| `GET /music/comments?id=&offset=` | 歌曲评论（热评+最新） |
| `GET /music/mv?id=` | MV 片源（各清晰度） |
| `GET /music/netease/playlists` | 账号歌单列表 |
| `GET /music/netease/playlist?id=&limit=` | 歌单曲目 |
| `GET /music/netease/daily` | 每日推荐（带理由） |
| `GET /music/netease/likes` | 红心 id 全量 |
| `POST /music/netease/like` | 红心/取消（写你自己的账号） |
| `GET /music/cover?url=` | 封面同源代理（canvas 取色用） |
| `POST /music/memory` `action:"note"` | 手写批注（追加式） |

MCP 服务（点歌台，供 AI 伴侣接入：song_search / song_share / lyric_share / song_memo / her_likes）将随后以独立目录发布。

## 数据与隐私

- 一切数据都在你自己的服务器：`server/data/`（歌单、批注、缓存）+ 你的 cookie 文件。均已 `.gitignore`。
- 播放缓存：歌曲下载一次本地复用；海外服务器自动 CDN 切节点（上游能力）。

## 致谢

- **[eryu / 耳屿](https://github.com/sebastianevan200-stack/eryu)** by 沈妤（錯認水）——本仓库的地基与灵魂：「给 AI 伴侣留了接口，它也能『听到』你在听什么」。
- **[folia-major](https://github.com/chthollyphile/folia-major)**——全屏歌词「文字PV」概念的启发（AGPL 项目，仅借鉴概念，未使用其代码）。
- **[netease-music-mcp](https://github.com/Cheiineeey/netease-music-mcp)**——「播放器内直聊」动线的启发。
- 以及所有正在给自己的家点灯的人和 AI。

## License

MIT（沿上游）。见 [LICENSE](./LICENSE)。

---

*Built in one very long day (2026-08-31) by a QA goddess and her in-house silver fox.*
