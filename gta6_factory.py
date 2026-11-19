# -*- coding: utf-8 -*-
# Фабрика нарезок GTA 6 v2: Twitch-клипы + громкие моменты из полных стримов -> Telegram
import os, sys, json, re, shutil, subprocess, textwrap, datetime as dt
from pathlib import Path
import requests

# ================= НАСТРОЙКИ =================
GAME_NAME     = "Grand Theft Auto VI"
HOURS         = 24
MAX_CLIPS     = 5
LANGS         = ["ru", "en"]
MIN_VIEWS     = 500
SUBS          = True
WHISPER_MODEL = "base"
WATCH         = ["dinah", "lirik"]
VOD_PER_DAY   = 1
VOD_LEN       = 50
VOD_FRESH_H   = 36
CAM = {}
CLIENT_ID     = os.environ.get("TWITCH_ID", "")
CLIENT_SECRET = os.environ.get("TWITCH_SECRET", "")
TG_TOKEN      = os.environ.get("TG_TOKEN", "")
TG_CHAT       = os.environ.get("TG_CHAT", "")
# =============================================

BASE = Path(__file__).parent
WORK = BASE / "_work"; WORK.mkdir(exist_ok=True)
OUT  = BASE / "READY"
LOG  = BASE / "processed.json"
done = {} if os.environ.get("RESET") == "1" else (json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else {})
CAMLOG = BASE / "cam_memory.json"
cam_mem = json.loads(CAMLOG.read_text(encoding="utf-8")) if CAMLOG.exists() else {}
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         r"C:\Windows\Fonts\arialbd.ttf",
         "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
FONT = next((f for f in FONTS if os.path.exists(f)), None)

def sh(cmd):
    r = subprocess.run(cmd, cwd=WORK, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-800:])
    return r.stdout

def send_text(msg):
    if TG_TOKEN and TG_CHAT:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": msg}, timeout=60)

def send_video(mp4, caption):
    if mp4.stat().st_size > 49_000_000:
        send_text(f"Клип больше 49 МБ, не влез в Telegram: {mp4.name}\n{caption[:300]}")
        return
    with open(mp4, "rb") as v:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendVideo",
                          data={"chat_id": TG_CHAT, "caption": caption[:1024],
                                "supports_streaming": "true"},
                          files={"video": v}, timeout=600).json()
    if not r.get("ok"):
        raise RuntimeError(f"Telegram: {r}")

def twitch_headers():
    resp = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials"}, timeout=30).json()
    if "access_token" not in resp:
        sys.exit(f"Twitch ключи не сработали: {resp}")
    return {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {resp['access_token']}"}

def helix(path, params, h):
    return requests.get("https://api.twitch.tv/helix/" + path,
                        params=params, headers=h, timeout=30).json()

def top_clips(h):
    g = helix("games", {"name": GAME_NAME}, h)["data"]
    if not g:
        sys.exit(f"Категория '{GAME_NAME}' не найдена на Twitch")
    now = dt.datetime.now(dt.timezone.utc)
    clips = helix("clips", {"game_id": g[0]["id"], "first": 100,
        "started_at": (now - dt.timedelta(hours=HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ended_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, h)["data"]
    clips = [c for c in clips if 8 <= c["duration"] <= 60
             and c["view_count"] >= MIN_VIEWS and c["id"] not in done
             and (not LANGS or c["language"] in LANGS)]
    clips.sort(key=lambda c: -c["view_count"])
    return clips[:MAX_CLIPS]

def audio_energy(m3u8):
    import numpy as np
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", m3u8,
                          "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=WORK)
    vals, chunk, buf = [], 16000, b""
    while True:
        piece = p.stdout.read(chunk)
        if not piece:
            break
        buf += piece
        while len(buf) >= chunk:
            part, buf = buf[:chunk], buf[chunk:]
            a = np.frombuffer(part, dtype="<i2") / 32768.0
            vals.append(float(np.sqrt(np.mean(a * a)) + 1e-9))
    p.wait()
    return vals

def pick_peaks(vals, k, length):
    if len(vals) < 600:
        return []
    sm = [sum(vals[max(0, i - 2):i + 3]) / len(vals[max(0, i - 2):i + 3]) for i in range(len(vals))]
    lo, hi = 300, len(sm) - 300 - length
    if hi <= lo:
        return []
    picks = []
    for i in sorted(range(lo, hi), key=lambda i: -sm[i]):
        if all(abs(i - p) > 180 for p in picks):
            picks.append(i)
        if len(picks) >= k:
            break
    return sorted(picks)

def fmt_hms(s):
    return f"{int(s) // 3600}:{int(s) % 3600 // 60:02d}:{int(s) % 60:02d}"

def vod_jobs(h):
    jobs = []
    for login in WATCH:
        try:
            u = helix("users", {"login": login}, h)["data"]
            if not u:
                continue
            vids = helix("videos", {"user_id": u[0]["id"], "type": "archive", "first": 1}, h)["data"]
            if not vids:
                continue
            v = vids[0]
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
            if age > dt.timedelta(hours=VOD_FRESH_H):
                continue
            key = f"vod_{v['id']}"
            if key in done:
                continue
            print(f"VOD {login}: анализирую {v['title']}")
            vurl = sh([sys.executable, "-m", "yt_dlp", "-g", "-f", "best[height<=1080]/best", v["url"]]).strip().splitlines()[-1]
            aurl = sh([sys.executable, "-m", "yt_dlp", "-g", "-f", "worstaudio/bestaudio", v["url"]]).strip().splitlines()[-1]
            for start in pick_peaks(audio_energy(aurl), VOD_PER_DAY, VOD_LEN):
                jobs.append({"login": login, "name": u[0]["display_name"],
                             "start": start, "url": vurl, "vod": v})
                print(f"  пик на {fmt_hms(start)}")
            done[key] = dt.date.today().isoformat()
            LOG.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"  [VOD пропуск] {login}: {e}")
    return jobs

def cut_vod(job, out):
    sh(["ffmpeg", "-nostdin", "-y", "-ss", str(job["start"]), "-t", str(VOD_LEN),
        "-i", job["url"], "-c:v", "libx264", "-preset", "fast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", out])

def find_cam(src):
    try:
        import cv2, statistics
    except ImportError:
        print("  (opencv не установлен — пропускаю поиск вебки)")
        return None
    print("  cv2:", getattr(cv2, "__file__", "ПУСТАЯ ОБЁРТКА"), getattr(cv2, "__version__", "?"))
    if not hasattr(cv2, "CascadeClassifier"):
        print("  у cv2 нет CascadeClassifier — поиск вебки отключён")
        return None
    cap = cv2.VideoCapture(str(WORK / src))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 10:
        cap.release()
        return None
    casc = [cv2.CascadeClassifier(cv2.data.haarcascades + n)
            for n in ("haarcascade_frontalface_default.xml",
                      "haarcascade_frontalface_alt2.xml",
                      "haarcascade_profileface.xml")]
    hits, checked = {}, 0
    for k in range(10):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / 10))
        ok, frame = cap.read()
        if not ok:
            continue
        checked += 1
        H, W = frame.shape[:2]
        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        found = []
        for c in casc:
            faces = c.detectMultiScale(gray, 1.1, 3,
                                       minSize=(int(W * 0.025), int(W * 0.025)))
            for x, y, w, h in faces:
                if all(abs(x - fx) + abs(y - fy) > (w + fw) / 2 for fx, fy, fw, fh in found):
                    found.append((x, y, w, h))
        for x, y, w, h in found:
            cx, cy = (x + w / 2) / W, (y + h / 2) / H
            if (cy < 0.35 or cy > 0.62) or (cx < 0.3 or cx > 0.7):
                zone = ("T" if cy < 0.5 else "B") + ("L" if cx < 0.4 else "R" if cx > 0.6 else "C")
                hits.setdefault(zone, []).append((cx, cy, w / W))
    cap.release()
    print("  зоны с лицами:", {k: len(v) for k, v in hits.items()})
    if not hits or checked == 0:
        return None
    zone, boxes = max(hits.items(), key=lambda kv: len(kv[1]))
    if len(boxes) < max(4, checked * 0.4):
        return None
    cx = statistics.median(b[0] for b in boxes)
    cy = statistics.median(b[1] for b in boxes)
    fw = statistics.median(b[2] for b in boxes)
    cw = min(max(fw * 3.2, 0.14), 0.55)
    ch = cw * 1.1
    x0 = min(max(cx - cw / 2, 0.0), 1.0 - cw)
    y0 = min(max(cy + 0.10 * ch - ch / 2, 0.0), 1.0 - ch)
    return (int(x0 * 1920), int(y0 * 1080), int(cw * 1920), int(ch * 1080))

def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

def make_ass(segments):
    head = ("[Script Info]\nPlayResX: 1080\nPlayResY: 1920\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
            "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
            "-1,0,0,0,100,100,0,0,1,4,0,2,60,60,470,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for a, b, text in segments:
        words = textwrap.wrap(text, 26) or [text]
        pairs = ["\\N".join(words[i:i + 2]) for i in range(0, len(words), 2)]
        total = sum(len(p) for p in pairs) or 1
        t = a
        for p in pairs:
            d = (b - a) * len(p) / total
            lines.append(f"Dialogue: 0,{ts(t)},{ts(t + d)},Default,,0,0,0,,{p}")
            t += d
    (WORK / "subs.ass").write_text(head + "\n".join(lines) + "\n", encoding="utf-8")

def render(src, out, login, title, cam, segments):
    title = re.sub(r"[^\w\s.,!?'\"-]", "", title, flags=re.UNICODE).strip() or "GTA 6"
    (WORK / "title.txt").write_text("\n".join(textwrap.wrap(title, 24)[:3]), encoding="utf-8")
    (WORK / "credit.txt").write_text(f"twitch.tv/{login}", encoding="utf-8")
    shutil.copy(FONT, WORK / "font.ttf")
    dt1 = ("drawtext=fontfile=font.ttf:textfile=title.txt:fontsize=52:fontcolor=white:"
           "box=1:boxcolor=black@0.55:boxborderw=16:x=(w-text_w)/2:y=%d:line_spacing=8")
    dt2 = ("drawtext=fontfile=font.ttf:textfile=credit.txt:fontsize=28:"
           "fontcolor=white@0.85:x=28:y=%d")
    if cam:
        x, y, w, h = [v - v % 2 for v in cam]
        top = int(min(max(1080 / (w / h), 560), 800)) // 2 * 2
        bot = 1920 - top
        bw = int(1080 * 1080 / bot) // 2 * 2
        bx = (1920 - bw) // 2
        f = ("[0:v]scale=1920:1080,setsar=1[base];"
             f"[base]crop={w}:{h}:{x}:{y},scale=1080:{top}[top];"
             f"[base]crop={bw}:1080:{bx}:0,scale=1080:{bot}[bot];"
             "[top][bot]vstack=inputs=2[st];"
             f"[st]{dt1 % 32}[d1];[d1]{dt2 % (top + 16)}[d2]")
    else:
        f = ("[0:v]scale=1920:1080,setsar=1,crop=608:1080:656:0,scale=1080:1920[st];"
             f"[st]{dt1 % 100}[d1];[d1]{dt2 % 1560}[d2]")
    last = "[d2]"
    if segments:
        make_ass(segments)
        f += f";{last}subtitles=subs.ass[vout]"
        last = "[vout]"
    sh(["ffmpeg", "-nostdin", "-y", "-i", src, "-filter_complex", f,
        "-map", last, "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", out])

_model = None
def transcribe(src):
    global _model
    if not SUBS:
        return []
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  (faster-whisper не установлен — без субтитров)")
        return []
    if _model is None:
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segs, _ = _model.transcribe(str(WORK / src), vad_filter=True)
    return [(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]

def make_clip(raw, login, title, caption, stem):
    try:
        segs = transcribe(raw)
    except Exception as e:
        print("  субтитры упали, делаю без них:", e)
        segs = []
    cam, source = CAM.get(login), "пресет"
    if cam is None:
        try:
            cam = find_cam(raw)
            source = "авто"
        except Exception as e:
            print("  поиск вебки упал:", e)
    if cam is None:
        cam = tuple(cam_mem.get(login, ())) or None
        source = "память"
    elif source == "авто":
        cam_mem[login] = list(cam)
        CAMLOG.write_text(json.dumps(cam_mem, ensure_ascii=False), encoding="utf-8")
    print("  вебка:", cam if cam else "не найдена — центр геймплея", f"({source})")
    tmp = f"out_{stem.name}.mp4"
    render(raw, tmp, login, title, cam, segs)
    (WORK / tmp).replace(stem.with_suffix(".mp4"))
    stem.with_suffix(".txt").write_text(caption, encoding="utf-8")
    send_video(stem.with_suffix(".mp4"), caption)

def main():
    if not FONT:
        sys.exit("Нет шрифта с кириллицей (в workflow ставится fonts-dejavu-core)")
    if not (CLIENT_ID and CLIENT_SECRET and TG_TOKEN and TG_CHAT):
        sys.exit("Не заданы секреты TWITCH_ID / TWITCH_SECRET / TG_TOKEN / TG_CHAT")
    h = twitch_headers()
    clips = top_clips(h)
    vjobs = vod_jobs(h)
    total = len(clips) + len(vjobs)
    if total == 0:
        send_text("GTA 6: сегодня ничего не найдено ни в клипах, ни в стримах.")
        return
    print(f"Клипов: {len(clips)}, VOD-моментов: {len(vjobs)}")
    day = OUT / dt.date.today().isoformat(); day.mkdir(parents=True, exist_ok=True)
    ok, errors = 0, []
    for i, c in enumerate(clips, 1):
        login = c["url"].split("twitch.tv/")[-1].split("/")[0].lower()
        print(f"[clip {i}/{len(clips)}] {login}: {c['title']}")
        try:
            raw = f"raw_{c['id']}.mp4"
            sh([sys.executable, "-m", "yt_dlp", "-f", "best[height<=1080]/best",
                "-o", raw, f"https://clips.twitch.tv/{c['id']}"])
            caption = (f"{c['title']} | GTA 6\n\nМомент со стрима {c['broadcaster_name']}\n"
                       f"Оригинал: {c['url']}\nКанал: https://twitch.tv/{login}\n\n"
                       f"#GTA6 #ГТА6 #GTAVI #ViceCity #нарезки #стрим")
            make_clip(raw, login, c["title"], caption, day / f"{i:02d}_{login}_{c['view_count']}views")
            done[c["id"]] = dt.date.today().isoformat()
            LOG.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
            ok += 1
        except Exception as e:
            errors.append(f"{login}: {str(e)[:250]}")
            print(f"  [ПРОПУСК] {e}")
    for i, job in enumerate(vjobs, 1):
        login = job["login"]
        print(f"[vod {i}/{len(vjobs)}] {login} @ {fmt_hms(job['start'])}")
        try:
            raw = f"raw_vod_{login}_{job['start']}.mp4"
            cut_vod(job, raw)
            caption = (f"Громкий момент со стрима {job['name']} | GTA 6\n\n"
                       f"Полный стрим: {job['vod']['url']}\nКанал: https://twitch.tv/{login}\n\n"
                       f"#GTA6 #ГТА6 #GTAVI #ViceCity #нарезки #стрим")
            make_clip(raw, login, f"{job['name']}: громкий момент", caption,
                      day / f"vod_{i:02d}_{login}_{job['start']}s")
            ok += 1
        except Exception as e:
            errors.append(f"vod {login}: {str(e)[:250]}")
            print(f"  [ПРОПУСК] {e}")
    msg = f"Готово: {ok} из {total} роликов отправлено. Выкладывай!"
    if errors:
        msg += "\n\nОшибки (первая):\n" + errors[0]
    send_text(msg)

if __name__ == "__main__":
    main()
