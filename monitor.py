# -*- coding: utf-8 -*-
# Живой мониторинг стримов: ловит громкие моменты и шлёт клипы в Telegram за ~7-11 минут
import os, sys, subprocess, threading, time, shutil, datetime as dt
from pathlib import Path
import requests
import numpy as np
from gta6_factory import WORK, BASE, send_text, make_clip
import gta6_factory as F

F.WHISPER_MODEL = os.environ.get("LIVE_WHISPER", "base")

REC = BASE / "rec"; REC.mkdir(exist_ok=True)
WATCH = [l.strip().lower() for l in os.environ.get("WATCH", "").split(",") if l.strip()]
SPIKE_K = float(os.environ.get("SPIKE_K", "2.1"))
COOLDOWN = int(os.environ.get("COOLDOWN", "240"))
PRE, POST = 30, 60
PROC_LOCK = threading.Lock()

def headers():
    r = requests.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": os.environ["TWITCH_ID"], "client_secret": os.environ["TWITCH_SECRET"],
        "grant_type": "client_credentials"}, timeout=30).json()
    return {"Client-ID": os.environ["TWITCH_ID"], "Authorization": f"Bearer {r['access_token']}"}

def live_now(h):
    d = requests.get("https://api.twitch.tv/helix/streams",
                     params={"user_login": ",".join(WATCH), "first": 100},
                     headers=h, timeout=30).json()["data"]
    return {s["user_login"]: s for s in d}

class Watcher(threading.Thread):
    def __init__(self, login, stream):
        super().__init__(daemon=True)
        self.login, self.stream = login, stream
        self.dir = REC / login; self.dir.mkdir(exist_ok=True)
        self.stop = False
        self.last_clip = 0.0
        self.rec_start = None

    def run(self):
        try:
            vurl = subprocess.run([sys.executable, "-m", "yt_dlp", "-g", "-f",
                                   "best[height<=1080]/best", f"https://twitch.tv/{self.login}"],
                                  capture_output=True, text=True, cwd=WORK,
                                  ).stdout.strip().splitlines()[-1]
        except Exception as e:
            print("нет ссылки на эфир:", e); return
        self.rec = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", vurl, "-c", "copy",
             "-f", "segment", "-segment_time", "30", "-segment_wrap", "24",
             str(self.dir / "seg_%03d.ts")], cwd=WORK, stderr=subprocess.DEVNULL)
        self.au = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", vurl,
             "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
            stdout=subprocess.PIPE, cwd=WORK, stderr=subprocess.DEVNULL)
        self.rec_start = time.time()
        hist, spike_start = [], None
        while not self.stop:
            chunk = self.au.stdout.read(16000)
            if not chunk:
                break
            a = np.frombuffer(chunk, dtype="<i2") / 32768.0
            rms = float(np.sqrt(np.mean(a * a)) + 1e-9)
            now = time.time()
            hist.append(rms); hist = hist[-300:]
            base = sorted(hist)[len(hist) // 2] if len(hist) > 60 else rms
            loud = rms > max(base * SPIKE_K, 0.02)
            if loud and spike_start is None:
                spike_start = now
            elif not loud and spike_start:
                if now - spike_start >= 4:
                    self.fire(spike_start)
                spike_start = None
            elif loud and spike_start and now - spike_start > 8:
                self.fire(spike_start)
                spike_start = now
        self.cleanup()

    def fire(self, t):
        if time.time() - self.last_clip < COOLDOWN:
            return
        self.last_clip = time.time()
        threading.Thread(target=self.run_locked, args=(t,), daemon=True).start()

    def run_locked(self, t):
        with PROC_LOCK:
            self.make_clip(t)

    def make_clip(self, t):
        try:
            time.sleep(POST)
            off = t - self.rec_start
            first = max(0, int((off - PRE) // 30))
            last = int((off + POST) // 30)
            files = [self.dir / f"seg_{i % 24:03d}.ts" for i in range(first, last + 1)]
            files = [f for f in files if f.exists()]
            if not files:
                return
            raw = WORK / f"live_{self.login}_{int(t)}.mp4"
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                            "-i", "concat:" + "|".join(str(f) for f in files),
                            "-ss", f"{max(0.0, (off - PRE) - first * 30):.1f}", "-t", str(PRE + POST),
                            "-c:v", "libx264", "-preset", "fast", "-crf", "19",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                            "-ar", "48000", "-movflags", "+faststart", str(raw)],
                           cwd=WORK, check=True)
            day = BASE / "READY" / dt.date.today().isoformat()
            day.mkdir(parents=True, exist_ok=True)
            caption = (f"LIVE: громкий момент | GTA 6\n\nПрямо со стрима {self.stream['user_name']}\n"
                       f"Канал: https://twitch.tv/{self.login}\n\n"
                       f"#GTA6 #ГТА6 #GTAVI #live #нарезки #стрим")
            make_clip(str(raw), self.login, f"{self.stream['user_name']}: LIVE-момент",
                      caption, day / f"live_{self.login}_{int(t)}")
            raw.unlink(missing_ok=True)
            print(f"[{self.login}] LIVE-клип отправлен")
        except Exception as e:
            print(f"[{self.login}] ошибка клипа:", e)

    def cleanup(self):
        for p in (self.au, self.rec):
            try:
                p.terminate()
            except Exception:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

def main():
    if not WATCH:
        sys.exit("Не задан WATCH (список логинов через запятую) в .env")
    send_text("🟢 Монитор запущен. Слежу за: " + ", ".join(WATCH))
    watchers = {}
    while True:
        try:
            h = headers()
            live = live_now(h)
            for login in list(watchers):
                if login not in live:
                    watchers[login].stop = True
                    watchers[login].au.terminate()
                    watchers[login].rec.terminate()
                    watchers.pop(login)
                    print(login, ": эфир закончился")
            for login, s in live.items():
                if login not in watchers:
                    print(login, ": в эфире, включаю запись")
                    send_text(f"🔴 {s['user_name']} в эфире — пишу")
                    watchers[login] = Watcher(login, s)
                    watchers[login].start()
        except Exception as e:
            print("цикл мониторинга:", e)
        time.sleep(60)

if __name__ == "__main__":
    main()
