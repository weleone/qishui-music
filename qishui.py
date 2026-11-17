"""汽水音乐抓取核心 (Playwright headless).

流程 (基于 ethanliaa/qishui-music-downloader 原理):
1. 打开歌单/歌曲分享链接, 落地到网页版
2. 歌曲页: 拦截 resource_type == "media" 的网络响应, 拿到音频直链
3. 歌单页: 解析 SSR 接口拿到 track 列表, 逐首抓直链
"""
import re
import time
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

# 分享参数: 从歌曲分享页 URL 里解析
SHARE_PARAMS = ("sec_sharer_id", "from_group_id", "from_url")

# 歌曲: douyin.com/qishui/song/<id> 或 music.douyin.com/qishui/share/track?track_id=<id>
SONG_RE = re.compile(r'qishui/song/(\d+)|qishui/share/track[^\s]*track_id=(\d+)')
# 歌单: douyin.com/qishui/playlist/<id> 或 music.douyin.com/qishui/share/playlist
PLAYLIST_RE = re.compile(r'qishui/playlist/(\d+)|qishui/share/(?:playlist|album)')


def _new_browser(p):
    browser = p.chromium.launch(headless=True, args=[
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ])
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
    )
    return browser, ctx


def get_song_audio(song_url, timeout=60):
    """单首歌曲: 返回 {title, artist, audio_url} 或抛异常."""
    with sync_playwright() as p:
        browser, ctx = _new_browser(p)
        try:
            page = ctx.new_page()
            media_urls = []

            image_urls = []
            def on_response(resp):
                try:
                    # 音频直链: resource_type 为 media
                    if resp.request.resource_type == "media":
                        url = resp.url
                        if url and url not in media_urls:
                            media_urls.append(url)
                    # 封面: 图片响应, 取内容最大的那张 (专辑图通常最大)
                    elif resp.request.resource_type == "image":
                        try:
                            size = int(resp.headers.get("content-length") or 0)
                        except Exception:
                            size = 0
                        image_urls.append((size, resp.url))
                except Exception:
                    pass

            page.on("response", on_response)
            page.goto(song_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            # 等待播放器发起媒体请求
            page.wait_for_timeout(8000)
            # 尝试点击播放按钮触发加载
            try:
                page.keyboard.press("Space")
                page.wait_for_timeout(5000)
            except Exception:
                pass

            if not media_urls:
                raise RuntimeError("未捕获到音频地址 (可能需要登录或接口变化)")

            # 标题要带歌手 (如 午夜调频): 等标题加载完, 再从页面内嵌数据里取歌手名补上
            title = page.title().replace(" - 汽水音乐", "").strip() or "unknown"
            try:
                page.wait_for_timeout(3000)
                full = page.title().replace(" - 汽水音乐", "").strip()
                if full:
                    title = full
            except Exception:
                pass
            # 如果标题里还没有歌手, 从页面数据里找 artist / singer 补上
            if title.count("-") < 1:
                try:
                    artist = page.evaluate("""() => {
                        const html = document.documentElement.innerHTML;
                        const m1 = html.match(/"artistName"\s*:\s*"([^"]+)"/);
                        if (m1) return m1[1];
                        const m2 = html.match(/"artists"\s*:\s*\[\s*\{[^}]*"name"\s*:\s*"([^"]+)"/);
                        if (m2) return m2[1];
                        const m3 = html.match(/"artist"\s*:\s*"([^"]+)"/) || html.match(/"singer"\s*:\s*"([^"]+)"/) || html.match(/"author"\s*:\s*"([^"]+)"/);
                        if (m3) return m3[1];
                        const el = document.querySelector('.track-artist .artist-name-max');
                        return el ? el.textContent.trim() : '';
                    }""") or ""
                    if artist and artist not in title:
                        title = title + "-" + artist
                except Exception:
                    pass
            # 封面: 优先用拦截到的最大图片响应 (最可靠), 再用 og:image / 内嵌 JSON / 页面 img
            cover_url = ""
            try:
                if image_urls:
                    image_urls.sort(key=lambda x: x[0], reverse=True)
                    # 过滤掉小图标 / 占位图, 只留 >10KB 的
                    big = [u for s, u in image_urls if s > 10240]
                    if big:
                        cover_url = big[0]
                    else:
                        # 没拿到 content-length 的图 (size=0) 也可能是封面: 优先 douyinpic 的封面图
                        cand = [u for s, u in image_urls if "douyinpic" in u and "~c5_" in u]
                        if cand:
                            cover_url = cand[0]
            except Exception:
                pass
            if not cover_url:
              try:
                cover_url = page.evaluate("""() => {
                    const html = document.documentElement.innerHTML;
                    // 1) 页面里现成的 375x375 封面直链 (最准)
                    const m0 = html.match(/https:\/\/p\d+-luna\.douyinpic\.com\/img\/[^" ]+?~c5_375x375\.jpg/);
                    if (m0) return m0[0];
                    // 2) album.url_cover 对象: urls[0] + uri + ~c5_375x375.jpg
                    const m1 = html.match(/"url_cover"\s*:\s*\{\s*"uri"\s*:\s*"([^"]+)"[^}]*"urls"\s*:\s*\[\s*"([^"]+)"/);
                    if (m1) return (m1[2] + m1[1] + '~c5_375x375.jpg').replace(/\\\\u002F/g, '/');
                    // 3) og:image, 但 favicon 不算封面
                    const m = document.querySelector('meta[property="og:image"]') || document.querySelector('meta[name="og:image"]');
                    if (m && m.content && !m.content.includes('favicon')) return m.content;
                    // 4) 旧的三路字符串字段
                    const mm = html.match(/"url_cover"\s*:\s*"([^"]+)"/) || html.match(/"cover"\s*:\s*"([^"]+)"/) || html.match(/"album"[^}]*"url_cover"\s*:\s*"([^"]+)"/);
                    if (mm) return mm[1].replace(/\\\\u002F/g, '/');
                    // 5) 页面上最大的 img
                    let best = '', bestW = 0;
                    for (const im of document.querySelectorAll('img')) {
                        const w = im.naturalWidth || 0;
                        if (w > bestW && (im.currentSrc || im.src)) { bestW = w; best = im.currentSrc || im.src; }
                    }
                    return best || '';
                }""") or ""
              except Exception:
                cover_url = ""
            return {
                "title": title,
                "artist": "",
                "audio_url": media_urls[0],
                "cover_url": cover_url,
                "all_urls": media_urls,
            }
        finally:
            browser.close()


def get_playlist_tracks(playlist_url, timeout=90, on_track=None):
    """歌单: 返回 [{song_id, song_url, title}] 列表."""
    tracks = []
    with sync_playwright() as p:
        browser, ctx = _new_browser(p)
        try:
            page = ctx.new_page()
            api_urls = []

            def on_response(resp):
                url = resp.url
                # 捕获歌单 SSR 接口
                if "playlist" in url and ("api" in url or "ssr" in url.lower()):
                    if url not in api_urls:
                        api_urls.append(url)

            page.on("response", on_response)
            page.goto(playlist_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(6000)

            # 从页面链接提取歌曲 ID (SEO 页直接有 /qishui/song/<id> 链接)
            html = page.content()
            song_ids = list(dict.fromkeys(SONG_RE.findall(html)))
            for sid in song_ids:
                tracks.append({
                    "song_id": sid,
                    "song_url": f"https://www.douyin.com/qishui/song/{sid}",
                    "title": "",
                })
                if on_track:
                    on_track(len(tracks))
            return tracks
        finally:
            browser.close()


def get_share_params(song_url, timeout=60):
    """从歌曲分享页 URL 解析 sec_sharer_id / from_group_id / from_url."""
    with sync_playwright() as p:
        browser, ctx = _new_browser(p)
        try:
            page = ctx.new_page()
            page.goto(song_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(4000)
            final_url = page.url
            qs = parse_qs(urlparse(final_url).query)
            return {k: qs.get(k, [""])[0] for k in SHARE_PARAMS}
        finally:
            browser.close()


def detect_type(url):
    if PLAYLIST_RE.search(url):
        return "playlist"
    if SONG_RE.search(url):
        return "song"
    return "unknown"


def resolve_share_url(raw):
    """从分享文本提取 URL, 并跟随短链跳转拿到最终地址."""
    import requests as _rq
    # 1. 从文本中提取第一个 http(s) 链接
    m = re.search(r'https?://[^\s\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+', raw or '')
    url = m.group(0).rstrip('.,;!?)\'"」』】') if m else (raw or '').strip()
    # 2. 短链跟随跳转 (v.douyin.com 等)
    if url and 'qishui/' not in url:
        try:
            r = _rq.get(url, allow_redirects=True, timeout=20, stream=True,
                        headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                "Chrome/120.0.0.0 Safari/537.36")})
            url = r.url
            r.close()
        except Exception:
            pass
    return url
