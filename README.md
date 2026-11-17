# qishui-music (self-hosted)

汽水音乐歌曲/歌单抓取服务，Docker 一键部署。
原理: Playwright 无头浏览器打开分享链接，拦截音频直链下载。

## 构建（多平台）

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t weleone/qishui-music:1.0.0 -t weleone/qishui-music:latest --push .
```

> 镜像较大 (~2GB, 含 Chromium)。arm64 构建前确认 QEMU 已注册:
> `docker run --rm --privileged tonistiigi/binfmt --install all`

## 运行

```bash
docker compose up -d
# 浏览器打开 http://<host>:8091
```

## 使用

1. 汽水音乐 App 内分享歌曲/歌单，复制链接
2. 粘贴到页面，点"开始抓取"
3. 单首直接下载；歌单逐首抓取，可单独下载或打包 zip

## API

- `POST /api/fetch` `{"url":"..."}` → `{"job_id":"..."}`
- `GET /api/status/<job_id>` → `{"status","progress","tracks","files","error"}`
- `GET /api/file/<job_id>/<index>` → 单个 MP3
- `GET /api/zip/<job_id>` → 打包下载
- `GET /api/health`

## 已知限制

- VIP/付费歌曲可能下载不完整
- 依赖网页版接口，改版后需更新 `qishui.py`
- 频繁抓取可能触发风控
- 仅供个人学习使用
