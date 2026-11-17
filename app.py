import hmac
import os
import re
import time
import uuid
import shutil
import zipfile
import threading
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_file, session, redirect

from qishui import get_song_audio, get_playlist_tracks, detect_type, resolve_share_url

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32).hex()
# 登录开关: AUTH_USER 和 AUTH_PASSWORD 都设了才要求登录, 不设就跟以前一样不用登录
AUTH_USER = os.environ.get('AUTH_USER', '')
AUTH_PASSWORD = os.environ.get('AUTH_PASSWORD', '')
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)

LOGIN_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 - 汽水抓取</title>
<style>body{background:#0e0e11;color:#eee;font-family:sans-serif;margin:0;
position:fixed;inset:0;display:flex;align-items:center;justify-content:center;}
.card{width:min(320px,86vw);text-align:center;}
h1{font-size:22px;margin:0 0 8px;}p.sub{color:#999;font-size:14px;margin:0 0 24px;}
input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #333;border-radius:8px;
background:#1a1a1e;color:#eee;font-size:16px;margin-bottom:12px;outline:none;}
input:focus{border-color:#1f9d55;}
button{width:100%;padding:12px;border:none;border-radius:8px;background:#1f9d55;
color:#fff;font-size:16px;cursor:pointer;}
.err{color:#ff6666;font-size:14px;margin:0 0 12px;}</style></head>
<body><div class="card"><h1>汽水抓取</h1><p class="sub">请登录后使用</p>
<!--ERR-->
<form method="post">
<input id="username" name="username" placeholder="用户名" autocomplete="username" required>
<input id="password" name="password" type="password" placeholder="密码" autocomplete="current-password" required>
<button type="submit">登录</button>
</form></div></body></html>"""

@app.before_request
def require_login():
    if not AUTH_ENABLED:
        return None
    if request.endpoint in ('login', 'api_health', 'static'):
        return None
    if request.path == '/login' or request.path == '/logout':
        return None
    if session.get('authed'):
        return None
    if request.path.startswith('/api/'):
        return jsonify({"error": "请先登录"}), 401
    return redirect('/login?next=' + request.path)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not AUTH_ENABLED:
        return redirect('/')
    if request.method == 'POST':
        u = request.form.get('username') or ''
        p = request.form.get('password') or ''
        if hmac.compare_digest(u, AUTH_USER) and hmac.compare_digest(p, AUTH_PASSWORD):
            session['authed'] = True
            nxt = request.args.get('next') or '/'
            return redirect(nxt if nxt.startswith('/') else '/')
        return LOGIN_HTML.replace('<!--ERR-->', '<p class="err">用户名或密码不对</p>'), 401
    return LOGIN_HTML

@app.route('/logout')
def logout():
    session.pop('authed', None)
    return redirect('/login')
WORK_DIR = Path(os.environ.get('WORK_DIR', '/tmp/qishui'))
WORK_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get('PORT', '8080'))
CLEANUP_AFTER = int(os.environ.get('CLEANUP_AFTER', '3600'))
# 页面上显示的保存路径(宿主机上的目录), 只做展示用, 容器里实际写 WORK_DIR.
# 宿主机挂载目录改了就同步改这个值, 否则气泡和已下载列表还显示旧路径.
HOST_SAVE_PATH = os.environ.get('HOST_SAVE_PATH', '/mnt/sdc1/music/mp3/qishui').rstrip('/') or '/mnt/sdc1/music/mp3/qishui'

jobs = {}
jobs_lock = threading.Lock()

def sanitize(name):
    name = (name or 'audio')
    # 只去掉歌名两侧的《》, 标题里任何位置的 @汽水音乐/-汽水音乐都去掉, 最后统一加一次 -汽水音乐
    name = name.replace('《', '').replace('》', '')
    name = re.sub(r'\s*[@\-－—]?\s*汽水音乐', '', name)
    name = re.sub(r'[-－—]{2,}', '-', name)
    name = name.strip().strip(' -@－—').strip()
    if not name:
        name = 'audio'
    name = name + '-汽水音乐'
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()[:80] or 'audio'
    return name

def download_file(url, dest):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
               "Referer": "https://www.douyin.com/"}
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

def sniff_ext(path):
    """按文件头判断实际格式: MP4/AAC -> .m4a, MP3 -> .mp3, 否则 .m4a (汽水音频是 AAC)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(12)
        if head[:3] == b'ID3' or head[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
            return '.mp3'
        if len(head) >= 8 and head[4:8] == b'ftyp':
            return '.m4a'
    except Exception:
        pass
    return '.m4a'

def embed_cover(mp3_path, cover_url, job_dir):
    """下载封面并嵌进 MP3 (ID3 APIC), 成功返回 True, 失败返回 False (保留原文件)."""
    import subprocess
    if not cover_url:
        print("[cover] no cover_url", flush=True)
        return False
    try:
        cover_path = job_dir / "cover.jpg"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                   "Referer": "https://music.douyin.com/",
                   "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
        with requests.get(cover_url, headers=headers, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(cover_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        print(f"[cover] downloaded {cover_path.stat().st_size} bytes from {cover_url}", flush=True)
        if cover_path.stat().st_size < 1000:
            print("[cover] too small, skip", flush=True)
            return False
        out_path = job_dir / (mp3_path.stem + "_cover.mp3")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3_path), "-i", str(cover_path),
                            "-map", "0:a", "-map", "1", "-c:a", "copy", "-c:v", "mjpeg",
                            "-id3v2_version", "3",
                            "-metadata:s:v", "title=Album cover",
                            "-metadata:s:v", "comment=Cover (front)", str(out_path)],
                           capture_output=True, timeout=120)
        cover_path.unlink(missing_ok=True)
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            out_path.replace(mp3_path)
            print("[cover] embedded OK", flush=True)
            return True
        print(f"[cover] ffmpeg failed rc={r.returncode} err={r.stderr.decode(errors='ignore')[:300]}", flush=True)
        out_path.unlink(missing_ok=True)
        return False
    except Exception:
        return False

def convert_to_mp3(src, dst):
    """ffmpeg 转码为 MP3 (libmp3lame, VBR q2 约 190k), 失败返回 False."""
    import subprocess
    try:
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                            "-codec:a", "libmp3lame", "-q:a", "2", str(dst)],
                           capture_output=True, timeout=300)
        return r.returncode == 0 and Path(dst).exists() and Path(dst).stat().st_size > 0
    except Exception:
        return False

def run_job(job_id, url):
    with jobs_lock:
        job = jobs[job_id]
    try:
        url = resolve_share_url(url)
        job["resolved_url"] = url
        kind = detect_type(url)
        if kind == "unknown":
            raise RuntimeError("仅支持汽水音乐歌曲/歌单分享链接")

        job_dir = WORK_DIR
        job["status"] = "fetching"
        job["progress"] = 5

        if kind == "song":
            info = get_song_audio(url)
            tracks = [{"title": info["title"], "audio_url": info["audio_url"], "cover_url": info.get("cover_url", "")}]
        else:
            raw_tracks = get_playlist_tracks(
                url, on_track=lambda n: job.update({"progress": 5 + min(n, 20)}))
            if not raw_tracks:
                raise RuntimeError("歌单为空或解析失败")
            tracks = []
            for i, t in enumerate(raw_tracks):
                job["status"] = f"解析第 {i+1}/{len(raw_tracks)} 首"
                job["progress"] = 20 + int(50 * i / len(raw_tracks))
                try:
                    info = get_song_audio(t["song_url"])
                    tracks.append({"title": info["title"] or t["song_id"],
                                   "audio_url": info["audio_url"],
                                   "cover_url": info.get("cover_url", "")})
                except Exception as e:
                    tracks.append({"title": t["song_id"], "error": str(e)[:100]})
                job["tracks"] = [{"title": x["title"],
                                  "ok": "audio_url" in x} for x in tracks]

        # 下载
        ok_files = []
        for i, t in enumerate(tracks):
            if "audio_url" not in t:
                continue
            job["status"] = f"下载第 {i+1}/{len(tracks)} 首"
            job["progress"] = 70 + int(25 * i / max(len(tracks), 1))
            fname = sanitize(t["title"])
            tmp_dest = job_dir / f"{fname}.bin"
            try:
                download_file(t["audio_url"], tmp_dest)
                ext = sniff_ext(tmp_dest)
                raw_dest = job_dir / f"{fname}{ext}"
                tmp_dest.rename(raw_dest)
                # 转成真 MP3, 转码失败则保留原文件
                mp3_dest = job_dir / f"{fname}.mp3"
                if ext == '.mp3':
                    dest, fname = raw_dest, f"{fname}.mp3"
                elif convert_to_mp3(raw_dest, mp3_dest):
                    raw_dest.unlink(missing_ok=True)
                    dest, fname = mp3_dest, f"{fname}.mp3"
                else:
                    dest, fname = raw_dest, f"{fname}{ext}"
                if fname.endswith(".mp3") and t.get("cover_url"):
                    embed_cover(Path(dest), t["cover_url"], job_dir)
                ok_files.append({"file": str(dest), "name": fname})
            except Exception as e:
                t["error"] = str(e)[:100]

        # 打包 zip (多首时, 直接放保存目录, 文件名带任务号防重名)
        if len(ok_files) > 1:
            zip_path = job_dir / f"qishui_playlist_{job_id}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for f in ok_files:
                    z.write(f["file"], arcname=f["name"])
            job["zip"] = str(zip_path)

        job["files"] = ok_files
        job["status"] = "done"
        job["progress"] = 100
        job["finished_at"] = time.time()
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]
        job["finished_at"] = time.time()

def cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        with jobs_lock:
            expired = [jid for jid, j in jobs.items()
                       if j.get("finished_at") and now - j["finished_at"] > CLEANUP_AFTER]
            for jid in expired:
                # 文件直接存在保存目录, 只删该任务自己的文件, 不删整个目录
                j = jobs.get(jid) or {}
                for f in j.get("files") or []:
                    try:
                        Path(f["file"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                if j.get("zip"):
                    try:
                        Path(j["zip"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                jobs.pop(jid, None)

@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/api/config")
def api_config():
    return jsonify({"save_path": HOST_SAVE_PATH})

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "请粘贴汽水音乐分享链接"}), 400
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status": "pending", "progress": 0, "tracks": [], "files": []}
    threading.Thread(target=run_job, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在或已清理"}), 404
    return jsonify({k: job.get(k, "") for k in
                    ("status", "progress", "error", "tracks", "files",
                     "resolved_url")})

@app.route("/api/file/by-name/<path:filename>")
def api_file_by_name(filename):
    # 按文件名下载: 已下载列表刷新后也能下, 不依赖内存里的任务号
    # 只允许取保存目录里的文件, 防止 ../ 穿目录
    target = (WORK_DIR / filename).resolve()
    if not str(target).startswith(str(WORK_DIR.resolve()) + os.sep):
        return jsonify({"error": "文件不存在"}), 404
    if not target.is_file():
        return jsonify({"error": "文件不存在"}), 404
    resp = send_file(target, as_attachment=True)
    from urllib.parse import quote
    resp.headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(target.name)
    return resp

@app.route("/api/file/by-name/<path:filename>", methods=["DELETE"])
def api_delete_by_name(filename):
    # 按文件名删磁盘文件: 已下载列表里的红色删除按钮用, 弹窗确认后才调
    # 只允许删保存目录里的文件, 防止 ../ 穿目录
    target = (WORK_DIR / filename).resolve()
    if not str(target).startswith(str(WORK_DIR.resolve()) + os.sep):
        return jsonify({"error": "文件不存在"}), 404
    existed = target.is_file()
    if existed:
        try:
            target.unlink()
        except OSError:
            return jsonify({"error": "删除失败"}), 500
    # 文件已经不在了也算成功 (可能已被 1 小时清理), 前端照样从列表去掉
    return jsonify({"ok": True, "existed": existed})

@app.route("/api/file/<job_id>/<int:index>")
def api_file(job_id, index):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "尚未就绪"}), 404
    files = job.get("files", [])
    if index >= len(files):
        return jsonify({"error": "文件不存在"}), 404
    f = files[index]
    # 中文文件名: 只发 filename*=UTF-8''..., 不发 ASCII 兜底 filename, 否则安卓会把 "-.mp3; filename*=..." 当成文件名
    resp = send_file(f["file"], as_attachment=True)
    from urllib.parse import quote
    resp.headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(f["name"])
    return resp

@app.route("/api/zip/<job_id>")
def api_zip(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or not job.get("zip"):
        return jsonify({"error": "打包文件不存在"}), 404
    return send_file(job["zip"], as_attachment=True, download_name="qishui_playlist.zip")

@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    threading.Thread(target=cleanup_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
