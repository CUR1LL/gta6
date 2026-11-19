# -*- coding: utf-8 -*-
# Фабрика нарезок GTA 6 для GitHub Actions: Twitch -> вертикальные клипы -> Telegram
import os, sys, json, re, shutil, subprocess, textwrap, datetime as dt
from pathlib import Path
import requests

# ================= НАСТРОЙКИ =================
GAME_NAME     = "Grand Theft Auto VI"
HOURS         = 24              # за сколько часов искать клипы
MAX_CLIPS     = 5               # сколько роликов в день
LANGS         = ["ru"]          # добавь "en", если мало материала
MIN_VIEWS     = 500             # мин. просмотров клипа на Twitch
SUBS          = True            # субтитры
WHISPER_MODEL = "base"
CAM = {                         # вебка стримеров: (x, y, w, h) в 1920x1080
    # "login_стримера": (1490, 760, 420, 310),
}
CLIENT_ID     = os.environ.get("TWITCH_ID", "")
CLIENT_SECRET = os.environ.get("TWITCH_SECRET", "")
TG_TOKEN      = os.environ.get("TG_TOKEN", "")
TG_CHAT       = os.environ.get("TG_CHAT", "")
# =============================================

BASE = Path(__file__).parent
WORK = BASE / "_work"; WORK.mkdir(exist_ok=True)
OUT  = BASE / "READY"
LOG  = BASE / "processed.json"
done = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else {}
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

def top_clips():
    resp = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials"}, timeout=30).json()
    if "access_token" not in resp:
        sys.exit(f"Twitch ключи не сработали: {resp}")
    h = {"Client-ID": CLIENT_ID, "Authorization": f"Bearer {resp['access_token']}"}
    g = requests.get("https://api.twitch.tv/helix/games",
                     params={"name": GAME_NAME}, headers=h, timeout=30).json()["data"]
    if not g:
        sys.exit(f"Категория '{GAME_NAME}' не найдена на Twitch")
    now = dt.datetime.now(dt.timezone.utc)
    clips = requests.get("https://api.twitch.tv/helix/clips", headers=h, timeout=30,
        params={"game_id": g[0]["id"], "first": 100,
                "started_at": (now - dt.timedelta(hours=HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ended_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}).json()["data"]
    clips = [c for c in clips if 8 <= c["duration"] <= 60
             and c["view_count"] >= MIN_VIEWS and c["id"] not in done
             and (not LANGS or c["language"] in LANGS)]
    clips.sort(key=lambda c: -c["view_count"])
    return clips[:MAX_CLIPS]

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

def find_cam(src):
    """Автопоиск вебки: лицо, стабильно сидящее в углу кадра."""
    try:
        import cv2, statistics
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(WORK / src))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total < 10:
        cap.release()
        return None
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    hits, checked = {}, 0
    for k in range(8):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / 8))
        ok, frame = cap.read()
        if not ok:
            continue
        checked += 1
        H, W = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.15, 5,
                                         minSize=(int(W * 0.03), int(W * 0.03)))
        for x, y, w, h in faces:
            cx, cy = (x + w / 2) / W, (y + h / 2) / H
            if (cx < 0.32 or cx > 0.68) and (cy < 0.38 or cy > 0.62):
                corner = ("L" if cx < 0.5 else "R") + ("T" if cy < 0.5 else "B")
                hits.setdefault(corner, []).append((cx, cy, w / W))
    cap.release()
    if not hits:
        return None
    corner, boxes = max(hits.items(), key=lambda kv: len(kv[1]))
    if len(boxes) < max(3, checked * 0.5):
        return None
    cx = statistics.median(b[0] for b in boxes)
    cy = statistics.median(b[1] for b in boxes)
    cw = statistics.median(b[2] for b in boxes) * 2.2
    if not 0.10 <= cw <= 0.45:
        return None
    ch = cw  # для 16:9-кадра прямоугольник 16:9 даёт равные нормализованные доли
    x0 = min(max(cx - cw / 2, 0.0), 1.0 - cw)
    y0 = min(max(cy + 0.08 * ch - ch / 2, 0.0), 1.0 - ch)
    return (int(x0 * 1920), int(y0 * 1080), int(cw * 1920), int(ch * 1080))

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
        f = ("[0:v]scale=1920:1080,setsar=1[base];"
             f"[base]crop={w}:{h}:{x}:{y},scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640[top];"
             "[base]crop=912:1080:504:0,scale=1080:1280[bot];"
             "[top][bot]vstack=inputs=2[st];"
             f"[st]{dt1 % 32}[d1];[d1]{dt2 % 656}[d2]")
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", out])

def main():
    if not FONT:
        sys.exit("Нет шрифта с кириллицей (в workflow ставится fonts-dejavu-core)")
    if not (CLIENT_ID and CLIENT_SECRET and TG_TOKEN and TG_CHAT):
        sys.exit("Не заданы секреты TWITCH_ID / TWITCH_SECRET / TG_TOKEN / TG_CHAT")
    clips = top_clips()
    if not clips:
        send_text("GTA 6: свежих клипов по фильтрам сегодня нет. Снизь MIN_VIEWS или добавь язык в LANGS.")
        print("Клипов нет.")
        return
    print(f"Отобрано клипов: {len(clips)}")
    day = OUT / dt.date.today().isoformat(); day.mkdir(parents=True, exist_ok=True)
    ok = 0
    for i, c in enumerate(clips, 1):
        login = c["url"].split("twitch.tv/")[-1].split("/")[0].lower()
        print(f"[{i}/{len(clips)}] {login}: {c['title']} ({c['view_count']} просмотров)")
        try:
            raw = f"raw_{c['id']}.mp4"
            sh([sys.executable, "-m", "yt_dlp", "-f", "best[height<=1080]/best",
                "-o", raw, f"https://clips.twitch.tv/{c['id']}"])
            segs = transcribe(raw)
            tmp = f"out_{i:02d}.mp4"
            render(raw, tmp, login, c["title"], CAM.get(login), segs)
            stem = day / f"{i:02d}_{login}_{c['view_count']}views"
            (WORK / tmp).replace(stem.with_suffix(".mp4"))
            caption = (f"{c['title']} | GTA 6\n\n"
                       f"Момент со стрима {c['broadcaster_name']}\n"
                       f"Оригинал: {c['url']}\nКанал: https://twitch.tv/{login}\n\n"
                       f"#GTA6 #ГТА6 #GTAVI #ViceCity #нарезки #стрим")
            stem.with_suffix(".txt").write_text(caption, encoding="utf-8")
            send_video(stem.with_suffix(".mp4"), caption)
            done[c["id"]] = dt.date.today().isoformat()
            LOG.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
            ok += 1
            print("  [OK] отправлено в Telegram")
        except Exception as e:
            print(f"  [ПРОПУСК] {e}")
    send_text(f"Готово: {ok} из {len(clips)} клипов отправлено. Выкладывай!")

if __name__ == "__main__":
    main()
