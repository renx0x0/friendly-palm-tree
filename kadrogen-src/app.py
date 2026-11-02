"""VideoGen: обычное окно Windows (pywebview).
Экраны: 1) чем делать картинки, 2) режим и стиль, 3) ключи + озвучка, 4) расчёт и подтверждение, 5) прогресс.

Запуск: python app.py   (после сборки — VideoGen.exe)
Скрытый режим без окна: VideoGen.exe --run файл.mp3 [--provider ...] [--mode images|video] [--style ...] [--refs]
"""
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile

import requests



try:
    import webview
except Exception as _e:  # pythonnet / .NET не загрузился — объясняем по-человечески
    import ctypes
    if "--run" in sys.argv:
        raise
    ctypes.windll.user32.MessageBoxW(
        0,
        "Не удалось загрузить компоненты окна (.NET / pythonnet).\n\n"
        "Что попробовать:\n"
        "1. Правой кнопкой по скачанному архиву → Свойства → галочка «Разблокировать» → OK, распаковать заново.\n"
        "2. Установить .NET Framework 4.8 (входит в Windows 10/11, но может быть отключён).\n"
        "3. Распаковать программу в папку без русских букв и пробелов, например C:\\KadroGen.\n\n"
        f"Техническая причина: {str(_e)[:300]}",
        "Kadro.Gen", 0x10)
    raise

from fastgen_api import FastGen, app_dir, data_dir
import endpoints
import pipeline

VERSION = "1.7.1-p8"
# Публичная папка Google Диска с архивами VideoGen-x.y.z.zip. Программа при запуске смотрит список файлов
# и предлагает скачать самую свежую версию. Пусто — проверка выключена.
UPDATE_FOLDER_ID = ""  # своя сборка с профилями: автообновление от автора выключено, иначе оно затрёт правки

CONFIG_PATH = os.path.join(data_dir(), "config.json")
RES_DIR = getattr(sys, "_MEIPASS", app_dir())
HTML_PATH = os.path.join(RES_DIR, "ui.html")

STAGE_NAMES = {
    "start": "Подготовка", "transcribe": "Распознавание речи", "scenes": "Разбиение на сцены",
    "style": "Стиль ролика", "prompts": "Описания сцен", "refs": "Референсы героев", "images": "Картинки",
    "videos": "Оживление картинок", "render": "Сборка видео", "subtitles": "Субтитры", "prepare": "Расчёт", "done": "Готово",
    "rewrite": "Рерайт сценария", "tts": "Озвучка",
    "tts": "Озвучка",
}

STATE = {"running": False, "preparing": False, "stage": "", "done": 0, "total": 0, "message": "", "log": [],
         "result": None, "error": None, "started_at": None, "estimate": None, "cancelled": False, "job_dir": None,
         "scenes": 0, "script": None, "update": None, "queue": None, "queue_active": False}
QUEUE = []          # задания на ночь: [{id, title, source, script, audio, rewrite, voice, status, result, error}]
QUEUE_STOP = False  # «Стоп» во время очереди: текущее задание прерывается, следующие не стартуют


def queue_path():
    return os.path.join(data_dir(), "queue.json")


def queue_load():
    global QUEUE
    try:
        with open(queue_path(), encoding="utf-8") as f:
            QUEUE = [x for x in json.load(f) if isinstance(x, dict) and x.get("id")]
    except Exception:
        QUEUE = []
    for x in QUEUE:
        if x.get("status") == "running":  # программу закрыли посреди задания — доделаем при следующем запуске очереди
            x["status"] = "queued"


def queue_save():
    try:
        with open(queue_path() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(QUEUE, f, ensure_ascii=False, indent=1)
        os.replace(queue_path() + ".tmp", queue_path())
    except Exception:
        traceback.print_exc()


def queue_public():
    return [{k: x.get(k) for k in ("id", "title", "source", "voice", "style", "status", "result", "error")} for x in QUEUE]


def style_key(v, default=None):
    """Ключ стиля из интерфейса: один из STYLES или «свой»; иначе default."""
    return v if v in pipeline.STYLES or v == pipeline.CUSTOM_STYLE else default


def job_title(opts):
    rw = opts.get("rewrite") or {}
    if rw.get("on"):
        return (rw.get("url") or " ".join((rw.get("text") or "").split()[:7]) or "рерайт")[:80]
    if opts.get("source") == "script":
        return " ".join((opts.get("script") or "").split()[:7]).lstrip("—–-:;,. ")[:80] or "сценарий"
    return os.path.basename((opts.get("audio") or "").strip().strip('"')) or "озвучка"


def run_queue(common):
    """Задания по очереди, одно за другим. Общие настройки у всех одни, у каждого задания свои текст/файл и голос."""
    global QUEUE_STOP
    try:
        while not QUEUE_STOP:
            with LOCK:
                item = next((x for x in QUEUE if x.get("status") == "queued"), None)
                if item is None:
                    break
                item["status"] = "running"
                pos = QUEUE.index(item) + 1
                STATE.update({"running": True, "stage": "start", "done": 0, "total": 0, "message": "запуск", "log": [],
                              "result": None, "error": None, "cancelled": False, "started_at": time.time(), "scenes": 0,
                              "queue": {"id": item["id"], "pos": pos, "total": len(QUEUE), "title": item.get("title", "")}})
            queue_save()
            if item.get("opts"):  # поставлено во время работы — со своими настройками
                err, args, _ = Api()._job(item["opts"])
                if err:
                    with LOCK:
                        item["status"], item["error"] = "error", err
                    queue_save()
                    continue
                run_job(**args)
                with LOCK:
                    if STATE.get("cancelled"):
                        item["status"] = "stopped"
                    elif STATE.get("error"):
                        item["status"], item["error"] = "error", STATE["error"]
                    else:
                        item["status"], item["result"] = "done", STATE.get("result")
                    stopped = STATE.get("cancelled")
                queue_save()
                if stopped:
                    break
                continue
            rw = item.get("rewrite") if item.get("source") == "rewrite" else None
            script = item.get("script") if item.get("source") == "script" else None
            audio = item.get("audio") if item.get("source") == "mp3" else None
            # стиль у задания свой, если включён «Разный стиль» и выбран; иначе общий
            run_job(audio, common["provider"], common["mode"], style_key(item.get("style"), common["style"]), common["use_refs"], common["subtitles"], common["motion"],
                    script, rw, common["tts"], (item.get("voice") or "").strip(), common["video_first"], common["hd"],
                    voice_file=(item.get("voice_file") or None) if script else None)
            with LOCK:
                if STATE.get("cancelled"):
                    item["status"] = "stopped"
                elif STATE.get("error"):
                    item["status"], item["error"] = "error", STATE["error"]
                else:
                    item["status"], item["result"] = "done", STATE.get("result")
                stopped = STATE.get("cancelled")
            queue_save()
            if stopped:
                break
    finally:
        with LOCK:
            STATE["queue_active"] = False
            STATE["running"] = False
        pipeline.log("очередь: " + ", ".join(f"{x.get('title','')[:30]}: {x.get('status')}" for x in QUEUE))
_FG_LIMITS = {}  # ключ fast-gen -> (время, лимит кредитов в час, потоков)


def _vf(v):
    """«Оживить первые N сцен»: целое 0..150."""
    try:
        return max(0, min(150, int(v or 0)))
    except (TypeError, ValueError):
        return 0


def fg_limits(key):
    """Лимиты тарифа fast-gen для прогноза времени; кэш 10 минут, чтобы не дёргать сервер на каждый клик."""
    hit = _FG_LIMITS.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1], hit[2], hit[3]
    try:
        u = FastGen(key).usage()
        lim = u.get("account_limits", {})
        vid_on = (u.get("capabilities") or {}).get("video.generate", True)
        out = (int(lim.get("img_gen_per_hour_limit") or 0), int(lim.get("img_generation_threads_allowed") or 1),
               int(lim.get("video_gen_per_hour_limit") or 0) if vid_on else 0)
    except Exception:
        out = (0, 0, 0)
    _FG_LIMITS[key] = (time.time(), *out)
    return out


_FG_USAGE = {"at": 0.0, "key": None, "data": None, "logged": False}
_LIMIT_KINDS = (("ultra", None), ("img", "картинки"), ("image", "картинки"), ("video", "видео"), ("prompt", "тексты"), ("text", "тексты"), ("token", "тексты"),
                ("aistudio", "озвучка"),
                ("llm", "тексты"), ("chat", "тексты"), ("tts", "озвучка"), ("voice", "озвучка"), ("speech", "озвучка"), ("audio", "озвучка"))


def _limit_kind(name):
    n = name.lower()
    return next((label for k, label in _LIMIT_KINDS if k in n), None)


def _days_left(obj):
    """Сколько дней до конца тарифа, если сервер это сообщает (поле с expire/until/ends в названии)."""
    import datetime
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                kl = k.lower()
                if isinstance(v, (dict, list)):
                    stack.append(v)
                elif any(w in kl for w in ("expir", "until", "ends", "end_at", "valid_to")) and v:
                    try:
                        if isinstance(v, (int, float)):
                            t = datetime.datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), datetime.timezone.utc)
                        else:
                            t = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                            if t.tzinfo is None:
                                t = t.replace(tzinfo=datetime.timezone.utc)
                        return max(0, (t - datetime.datetime.now(datetime.timezone.utc)).days)
                    except (ValueError, OSError, OverflowError):
                        pass
                elif "days" in kl and isinstance(v, (int, float)):
                    return int(v)
        elif isinstance(o, list):
            stack.extend(o)
    return None


def fg_usage_summary(u):
    """Лимиты fast-gen на час и сколько осталось — для строки лимитов в окне."""
    lim = u.get("account_limits") or {}
    hourly = (u.get("current_usage") or {}).get("hourly_usage") or {}
    used_by = {}
    for hk, hv in hourly.items():
        kind = _limit_kind(hk)
        if isinstance(hv, dict):  # {"current_usage": N, "window_start": …}
            hv = hv.get("current_usage")
        if hv is None and kind:
            hv = 0  # в этом часе ещё не тратили
        if kind and isinstance(hv, (int, float)) and kind not in used_by:
            used_by[kind] = hv
    items, seen = [], set()
    for k, v in lim.items():
        kind = _limit_kind(k)
        if not kind or kind in seen or not isinstance(v, (int, float)) or "thread" in k.lower() or not v:
            continue
        seen.add(kind)
        used = used_by.get(kind)
        items.append({"label": kind, "limit": int(v), "used": None if used is None else int(used),
                      "left": None if used is None else max(0, int(v - used))})
    return {"items": items, "days": _days_left(u)}


def estimate_plan(opts):
    """Прогноз до запуска: сколько сцен, кредитов и времени, с учётом часового лимита fast-gen.
    Сцены считаются по длительности (в среднем 5,7 с на сцену по реальным роликам), точное число даст распознавание."""
    cfg = load_cfg()
    providers = pipeline.normalize_providers(opts.get("providers"))
    mode = opts.get("mode", "images")
    quality = ("quality" if opts.get("use_refs") else "speed") if "use_refs" in opts else cfg.get("quality", "speed")
    duration, words = None, 0
    if opts.get("source") == "script":
        words = len((opts.get("script") or "").split())
        duration = words / 2.4 if words else None
    else:
        audio = (opts.get("audio") or "").strip().strip('"')
        if audio and os.path.exists(audio):
            try:
                duration = pipeline.audio_duration(audio)
            except Exception:
                duration = None
    if not duration:
        return {"ok": False}
    scenes = max(1, round(duration / 5.7))
    fg = "fastgen" in providers
    limit, threads, vlimit = fg_limits(cfg.get("fastgen_api_key", "")) if fg and cfg.get("fastgen_api_key") else (0, 0, 0)
    vf = _vf(opts.get("video_first", cfg.get("video_first", 0)))
    clips = scenes if mode == "video" else len({i for i in range(scenes) if i < vf or (mode == "mixed" and i % 2 == 0)})
    per_min = {"images": 0.15, "mixed": 0.7, "video": 1.2}.get(mode, 0.15)
    if mode == "images" and vf:
        per_min = 0.15 + 1.05 * min(1.0, vf / max(1, scenes))  # оживлённые сцены ≈ как в режиме видео
    # оживление через fast-gen: свой часовой лимит (Video Lite — 15 клипов в час), а на ключе без видео его нет вовсе
    video_off = bool(fg and clips and len(providers) == 1 and vlimit == 0)
    vwait = max(0, int(math.ceil(clips / len(providers) / vlimit)) - 1) if fg and clips and vlimit and clips / len(providers) > vlimit else 0

    hd = bool(opts.get("hd", cfg.get("hd_images", False)))

    def plan(q):
        per_scene = (8 if hd else 1) if q == "quality" else 1
        credits = (int(math.ceil(scenes / len(providers))) * per_scene + (8 if q == "quality" else 0)) if fg else 0
        wait_h = max(0, int(math.ceil(credits / limit)) - 1) if limit and credits > limit else 0
        minutes = int(round(1 + scenes * per_min + (wait_h + vwait) * 60))
        return {"credits": credits, "wait_hours": wait_h + vwait, "minutes": minutes}

    cur, alt = plan(quality), plan("speed" if quality == "quality" else "quality")
    return {"ok": True, "duration": int(round(duration)), "scenes": scenes, "words": words, "quality": quality, "fastgen": fg,
            "limit": limit, "threads": threads, "clips": clips, "vlimit": vlimit, "video_off": video_off, "video_wait_hours": vwait,
            **cur, "alt_minutes": alt["minutes"], "alt_wait_hours": alt["wait_hours"]}


def key_error(e):
    """Текст для строки «Проверить»: обрыв связи и отказ по ключу — разные вещи."""
    s = str(e)
    low = s.lower()
    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)) or "connection aborted" in low \
            or "10054" in low or "getaddrinfo" in low or "timed out" in low or "max retries" in low:
        return "Ошибка: нет связи с сервисом — проверьте интернет, VPN или антивирус и нажмите «Проверить» ещё раз"
    return f"Ошибка: ключ не принят ({s[:80]})"


def set_clipboard(text):
    """Текст в буфер обмена Windows (юникод), без сторонних библиотек."""
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, len(data))
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        return bool(u32.SetClipboardData(13, h))
    finally:
        u32.CloseClipboard()


def build_report():
    """Отчёт для поддержки: версия, настройки без ключей, последняя ошибка и хвост журнала. Сохраняется в report.txt."""
    cfg = load_cfg()
    keys = ", ".join(k.replace("_api_key", "") for k in cfg if k.endswith("_api_key") and cfg.get(k)) or "нет"
    with LOCK:
        st = dict(STATE)
    lines = [f"Kadro.Gen {VERSION} · Windows {platform.version()} · папка данных: {'рядом с программой' if data_dir() == app_dir() else data_dir()}",
             f"сервисы: {', '.join(cfg.get('image_providers') or []) or '—'} · режим: {cfg.get('mode')} · {cfg.get('quality')} · моушн: {cfg.get('motion')} · HD: {cfg.get('hd_images')} · субтитры: {cfg.get('subtitles')}",
             f"речь: {cfg.get('stt_engine')} · тексты: {cfg.get('llm_engine')} ({cfg.get('llm_model')}) · источник: {cfg.get('source')} · озвучка: {cfg.get('tts_service')}",
             f"ключи введены: {keys}"]
    if st.get("error"):
        lines += ["", "ОШИБКА: " + str(st["error"])]
    tb = [l for l in (st.get("log") or []) if "Traceback" in l]
    if tb:
        lines += ["", tb[-1]]
    lines += ["", "--- журнал, последние 150 строк ---"]
    try:
        with open(pipeline.LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines += [l.rstrip() for l in f.readlines()[-150:]]
    except OSError:
        lines.append("(журнал не найден)")
    text = "\n".join(lines)
    path = os.path.join(data_dir(), "report.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        path = ""
    return text, path


def apply_update(target, pid):
    """Режим помощника: `KadroGen.exe --apply-update <папка программы> <pid старой копии>`, запущен из папки обновления.
    Ждёт выхода старой копии, копирует свою папку поверх программы (config.json, jobs, output не трогает), запускает её."""
    import ctypes
    k32 = ctypes.windll.kernel32
    try:
        h = k32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if h:
            k32.WaitForSingleObject(h, 60000)
            k32.CloseHandle(h)
    except Exception:
        pass
    time.sleep(0.5)
    src = app_dir()
    skip = {"config.json", "jobs", "output", "report.txt", "videogen.log", "update"}
    failed = None
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        if rel == ".":
            dirs[:] = [d for d in dirs if d not in skip]
            files = [f for f in files if f not in skip]
        dst_dir = target if rel == "." else os.path.join(target, rel)
        os.makedirs(dst_dir, exist_ok=True)
        for f in files:
            if f.endswith((".old", ".new")):
                continue
            for attempt in range(40):  # антивирус или ещё не до конца вышедший процесс — ждём до 20 с на файл
                try:
                    shutil.copy2(os.path.join(root, f), os.path.join(dst_dir, f))
                    break
                except OSError as e:
                    if attempt == 39:
                        failed = failed or f"{os.path.join(rel, f)}: {e}"
                    time.sleep(0.5)
    exe = os.path.join(target, "KadroGen.exe")
    if failed:
        ctypes.windll.user32.MessageBoxW(0, "Не удалось обновить часть файлов:\n" + failed[:300] + "\n\nСкачайте архив с Диска и распакуйте "
                                         "поверх папки программы — ключи и задания останутся.", "Kadro.Gen", 0x30)
    if os.path.exists(exe):
        subprocess.Popen([exe], cwd=target, close_fds=True, creationflags=0x00000008 | 0x00000200)


def cleanup_old_files():
    """После автообновления: убрать *.old, когда прошлая копия программы уже вышла."""
    def work():
        app = app_dir()
        for _ in range(20):
            left = False
            for root, dirs, files in os.walk(app):
                if "jobs" in root or "output" in root:
                    continue
                for f in files:
                    if f.endswith(".old") or f.endswith(".new"):
                        try:
                            os.remove(os.path.join(root, f))
                        except OSError:
                            left = True
            upd = os.path.join(data_dir(), "update")
            if os.path.isdir(upd):
                shutil.rmtree(upd, ignore_errors=True)
                left = left or os.path.isdir(upd)
            if not left:
                return
            time.sleep(3)
    threading.Thread(target=work, daemon=True).start()


def update_bat(app, src, upd):
    """Скрипт подмены файлов: ждёт, пока KadroGen.exe перестанет быть занят (copy поверх работающего exe не проходит),
    копирует новую версию поверх старой, не трогая config.json, jobs и output, и запускает программу заново."""
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        f'set "APP={app}"',
        f'set "NEW={src}"',
        "set /a N=0",
        ":wait",
        "timeout /t 1 /nobreak >nul",
        "set /a N+=1",
        "if %N% gtr 180 goto run",
        'copy /y "%NEW%\\KadroGen.exe" "%APP%\\KadroGen.exe" >nul 2>&1 || goto wait',
        'robocopy "%NEW%" "%APP%" /E /IS /IT /R:10 /W:2 /XD jobs output update /XF config.json >nul',
        ":run",
        'start "" "%APP%\\KadroGen.exe"',
        f'rd /s /q "{upd}" >nul 2>&1',
        'del "%~f0"',
    ]
    return "\r\n".join(lines) + "\r\n"


def _phase(**kw):
    with LOCK:
        STATE["update"] = {**(STATE.get("update") or {}), **kw}


def launch_helper(src):
    """Новая версия из папки обновления запускается как помощник: дождётся нашего выхода, скопирует себя на место, запустит программу."""
    helper = os.path.join(src, "KadroGen.exe")
    subprocess.Popen([helper, "--apply-update", app_dir(), str(os.getpid())], cwd=src, close_fds=True,
                     creationflags=0x00000008 | 0x00000200)


def prepare_update(u):
    """Тихое автообновление: архив скачивается и распаковывается заранее, установка — при закрытии программы."""
    try:
        src = download_update(u["url"], u["version"], _phase)
        _phase(phase="ready", src=src, version=u["version"], error=None)
        pipeline.log(f"обновление {u['version']} скачано, установится при закрытии программы")
    except Exception as e:
        _phase(phase="error", error=str(e)[:200])


def run_update(url, version):
    """Кнопка «Установить»: если архив уже скачан заранее — сразу ставим, иначе скачиваем и ставим."""
    phase = _phase
    try:
        with LOCK:
            cur = dict(STATE.get("update") or {})
        src = cur.get("src") if cur.get("phase") == "ready" and cur.get("version") == version else None
        if not src:
            src = download_update(url, version, phase)
        phase(phase="install")
        launch_helper(src)
        time.sleep(1.0)
        try:
            WINDOW.destroy()
        except Exception:
            pass
        time.sleep(0.5)
        os._exit(0)
    except Exception as e:
        phase(phase="error", error=str(e)[:200])


def download_update(url, version, phase):
    """Скачать архив новой версии и распаковать в папку обновления. Возвращает папку с KadroGen.exe."""
    upd = os.path.join(data_dir(), "update")
    if True:
        shutil.rmtree(upd, ignore_errors=True)
        os.makedirs(upd, exist_ok=True)
        zpath = os.path.join(upd, f"KadroGen-{version}.zip")
        phase(phase="download", done=0, total=0, version=version, error=None)
        with requests.get(url, stream=True, timeout=60) as r:
            if r.status_code != 200 or "text/html" in (r.headers.get("Content-Type") or ""):
                raise RuntimeError("Google Диск не отдал файл напрямую")
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(zpath, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    f.write(chunk)
                    done += len(chunk)
                    phase(done=done, total=total)
        phase(phase="unpack")
        with zipfile.ZipFile(zpath) as z:
            if z.testzip() is not None:
                raise RuntimeError("архив повреждён")
            exe = [n for n in z.namelist() if n.lower().endswith("kadrogen.exe")]
            if not exe:
                raise RuntimeError("в архиве нет KadroGen.exe")
            z.extractall(os.path.join(upd, "new"))
        src = os.path.dirname(os.path.join(upd, "new", exe[0].replace("/", os.sep)))
        return src
THUMBS = {}  # путь -> (mtime, data-url)


def make_thumb(path, size=480):
    """Миниатюра картинки как data-url (для галереи в окне)."""
    import base64, io
    from PIL import Image
    try:
        mt = os.path.getmtime(path)
        cached = THUMBS.get(path)
        if cached and cached[0] == mt:
            return cached[1]
        im = Image.open(path).convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        THUMBS[path] = (mt, url)
        return url
    except Exception:
        return None
LOCK = threading.Lock()
WINDOW = None


CFG_LOCK = threading.RLock()  # окно читает config.json каждые 1.5 с, пайплайн пишет — без замка os.replace ловил WinError 5


def load_cfg():
    with CFG_LOCK:
        if os.path.exists(CONFIG_PATH):
            for attempt in range(5):
                try:
                    with open(CONFIG_PATH, encoding="utf-8") as f:
                        return json.load(f)
                except PermissionError:
                    time.sleep(0.1 * (attempt + 1))
                except Exception:
                    break
    return {}


def sync_keys(cfg):
    """image_keys — списки ключей по сервисам; старые одиночные поля = первый ключ (их читают речь, тексты, видео)."""
    ik = cfg.get("image_keys") if isinstance(cfg.get("image_keys"), dict) else {}
    for p, single in pipeline.SINGLE_KEY.items():
        lst = [k.strip() for k in (ik.get(p) or []) if isinstance(k, str) and k.strip()]
        if not lst and cfg.get(single):
            lst = [str(cfg[single]).strip()]
        ik[p] = lst
        cfg[single] = lst[0] if lst else ""
    cfg["image_keys"] = ik


def save_cfg(cfg):
    sync_keys(cfg)
    cfg.setdefault("fastgen_api_key", "")
    cfg.setdefault("veononstop_api_key", "")
    cfg.setdefault("yougen_api_key", "")
    cfg.setdefault("royaltechno_api_key", "")
    cfg.setdefault("google_api_key", "")
    cfg.setdefault("secretslider_api_key", "")
    cfg.setdefault("anthropic_api_key", "")
    cfg.setdefault("gemini_api_key", "")
    cfg.setdefault("assemblyai_api_key", "")
    cfg.setdefault("voicegen_api_key", "")
    cfg.setdefault("voicer_api_key", "")
    cfg.setdefault("lumean_api_key", "")
    cfg.setdefault("stt_engine", "fastgen")
    cfg.setdefault("llm_engine", "fastgen")
    cfg.setdefault("anthropic_model", "claude-sonnet-5-5")
    cfg.setdefault("gemini_model", "gemini-3.6-flash")
    for k in ("openai", "anthropic", "gemini"):
        cfg.setdefault(k + "_base_url", "")
    # старые идентификаторы моделей → актуальные
    cfg["anthropic_model"] = {"claude-haiku-4-5-20251001": "claude-haiku-4-5"}.get(cfg.get("anthropic_model"), cfg.get("anthropic_model"))
    endpoints.configure(cfg)
    cfg.setdefault("quality", cfg.get("fastgen_quality", "speed"))
    cfg.setdefault("tts_recent", [])
    cfg.setdefault("motion", False)
    cfg.setdefault("hd_images", False)
    cfg.setdefault("source", "mp3")
    cfg.setdefault("tts_service", "voicegen")
    cfg.setdefault("tts_voice", "")
    cfg.setdefault("openai_api_key", "")
    cfg.setdefault("openai_model", "gpt-4.1-mini")
    cfg.setdefault("text_engine", "fastgen")
    if "image_providers" not in cfg:
        cfg["image_providers"] = pipeline.normalize_providers(cfg["image_provider"]) if cfg.get("image_provider") else []
    cfg.pop("image_provider", None)
    cfg.setdefault("lang", "ru")
    cfg.setdefault("mode", "images")
    cfg.setdefault("style", pipeline.DEFAULT_STYLE)
    cfg.setdefault("use_refs", False)
    cfg.setdefault("subtitles", False)
    cfg.setdefault("llm_model", pipeline.DEFAULT_LLM_MODEL)
    tmp = CONFIG_PATH + ".tmp"
    with CFG_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        for attempt in range(10):  # файл может держать антивирус или параллельное чтение — ждём и пробуем снова
            try:
                os.replace(tmp, CONFIG_PATH)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.15 * (attempt + 1))


def music_tracks():
    """Фоновые треки из папки music (в сборке — рядом с ui.html)."""
    try:
        d = os.path.join(RES_DIR, "music")
        return sorted(f for f in os.listdir(d) if f.lower().endswith(".mp3"))
    except OSError:
        return []


def mask(key):
    """Точки вместо символов ключа, длина как у ключа (последние 4 знака видны)."""
    if not key:
        return ""
    return "•" * max(4, min(len(key) - 4, 60)) + key[-4:]


def progress_cb(stage, done, total, message):
    with LOCK:
        STATE.update({"stage": stage, "done": done, "total": total, "message": message})
        STATE["log"].append(f"{time.strftime('%H:%M:%S')}  {message}" + (f"  {done}/{total}" if total else ""))
        STATE["log"] = STATE["log"][-400:]


def apply_engine(providers):
    """Есть ключ fast-gen — речь и тексты по умолчанию через него; иначе — выбранные движки."""
    cfg = load_cfg()
    if cfg.get("fastgen_api_key"):
        # с ключом fast-gen по умолчанию всё через него, но свой выбор (Claude/OpenAI/Gemini) уважаем
        cfg.setdefault("stt_engine", "fastgen"); cfg.setdefault("llm_engine", "fastgen")
        if cfg.get("llm_engine") == "fastgen":
            cfg["llm_model"] = pipeline.DEFAULT_LLM_MODEL  # при fast-gen модель для описаний сцен зафиксирована
    else:
        if cfg.get("stt_engine") == "fastgen":
            cfg["stt_engine"] = "whisper"
        if cfg.get("llm_engine") == "fastgen":
            cfg["llm_engine"] = "openai"
    save_cfg(cfg)


def run_rewrite(rw):
    """Только рерайт: результат кладётся в STATE["script"], пользователь смотрит и правит его на экране проверки."""
    try:
        apply_engine(None)
        text = pipeline.rewrite_script(load_cfg(), rw, progress_cb)
        with LOCK:
            STATE["script"] = text
    except SystemExit as e:
        with LOCK:
            STATE["error"] = str(e)
    except Exception as e:
        with LOCK:
            STATE["error"] = f"{type(e).__name__}: {e}"
            STATE["log"].append(traceback.format_exc()[-1500:])
    finally:
        with LOCK:
            STATE["preparing"] = False


def run_prepare(audio, provider, mode, use_refs, script=None, rewrite=None, profile=None, voice_file=None):
    try:
        apply_engine(provider)
        d = pipeline.run_job_dir(load_cfg(), audio, script, voice_file, rewrite, profile)
        with LOCK:
            STATE["job_dir"] = d
        est = pipeline.prepare(audio, progress_cb=progress_cb, provider=provider, mode=mode, use_refs=use_refs, script_text=script, rewrite=rewrite,
                               profile=profile, voice_file=voice_file)
        with LOCK:
            STATE["estimate"] = est
            STATE["job_dir"] = est.get("job_dir") or d
            STATE["scenes"] = est.get("scenes", 0)
    except SystemExit as e:
        with LOCK:
            STATE["error"] = str(e)
    except Exception as e:
        with LOCK:
            STATE["error"] = f"{type(e).__name__}: {e}"
            STATE["log"].append(traceback.format_exc()[-1500:])
    finally:
        with LOCK:
            STATE["preparing"] = False


def keep_awake(on):
    """ES_CONTINUOUS | ES_SYSTEM_REQUIRED: пока идёт работа, Windows не уходит в сон (экран гаснет как обычно)."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if on else 0x80000000)
    except Exception:
        pass


RETRY_DELAYS = (120, 300, 600, 1200, 1800)  # повтор ролика после сбоя сервиса: 2, 5, 10, 20, 30 мин
FIX_DELAYS = (300, 900)                      # доводка собранного ролика: подменённые картинки и неоживлённые сцены
LAST_RUN = {}                                # последний запуск — для кнопки «Повторить»


class _Failed(Exception):
    pass


JOB_CFG_KEYS = ("quality", "qa_images", "speed_copies", "custom_style", "video_prompt", "fg_image_model", "fg_video_model",
                "tts_service", "tts_voice", "style", "image_providers")


def job_record(d, status, result=None, error=None):
    """job.json в папке задания: с какими настройками ролик запускали — для «Недавних роликов» и кнопки «Продолжить»."""
    if not d or not LAST_RUN:
        return
    try:
        p = os.path.join(d, "job.json")
        rec = pipeline.read_json(p, {}) or {}
        if status == "running":
            args = {k: v for k, v in LAST_RUN.items() if k != "script"}
            cfg = load_cfg()
            title = STATE.get("title") or job_title({"source": "script" if LAST_RUN.get("script") else "mp3", "script": LAST_RUN.get("script") or "",
                                                       "audio": LAST_RUN.get("voice_file") or LAST_RUN.get("audio") or ""})
            rec.update({"version": 1, "title": title, "args": args, "script": LAST_RUN.get("script"),
                        "cfg": {k: cfg.get(k) for k in JOB_CFG_KEYS}, "started": time.time(), "result": None, "error": None})
        rec.update({"status": status, "updated": time.time()})
        if result:
            rec["result"] = result
        if error:
            rec["error"] = str(error)[:500]
        pipeline.write_json(p, rec)
    except Exception as e:  # запись истории не должна мешать ролику
        pipeline.log(f"job.json не записан: {e}")


def _pause(sec, why):
    """Пауза перед повтором с обратным отсчётом в окне. False — нажали «Стоп»."""
    pipeline.log(why)
    with LOCK:
        STATE["message"] = why
        STATE["log"].append(f"{time.strftime('%H:%M:%S')}  {why}")
    pipeline.WAIT.clear()
    pipeline.set_wait("retry", time.time() + sec)
    pipeline.WAIT["why"] = why
    try:
        end = time.time() + sec
        while time.time() < end:
            if pipeline.CANCEL.is_set():
                return False
            time.sleep(1)
        return True
    finally:
        pipeline.clear_wait("retry")


def run_job(audio, provider, mode, style, use_refs, subtitles=False, motion=False, script=None, rewrite=None, tts=None, voice=None, video_first=0, hd=False,
            profile=None, voice_file=None):
    """Один ролик. Сбой сервиса — пауза и повтор (всё готовое берётся из задания); ролик собран, но часть картинок
    подменена соседними или сцены не оживились — пауза и доводка только их. Выключить: "auto_retry": false в config.json."""
    LAST_RUN.clear()
    LAST_RUN.update(dict(audio=audio, provider=provider, mode=mode, style=style, use_refs=use_refs, subtitles=subtitles, motion=motion,
                         script=script, rewrite=rewrite, tts=tts, voice=voice, video_first=video_first, hd=hd, profile=profile, voice_file=voice_file))
    keep_awake(True)
    tries = fixes = 0
    try:
        apply_engine(provider)
        d = pipeline.run_job_dir(load_cfg(), audio, script, voice_file, rewrite, profile)
        with LOCK:
            STATE["job_dir"] = d
        job_record(d, "running")
        auto = load_cfg().get("auto_retry", True)
        while True:
            err = None
            try:
                out = pipeline.make_video(audio, progress_cb=progress_cb, provider=provider, mode=mode, style_key=style,
                                          use_refs=use_refs, subtitles=subtitles, motion=motion, script_text=script, rewrite=rewrite,
                                          tts_service=tts, tts_voice=voice, video_first=video_first, hd=hd, profile=profile, voice_file=voice_file)
            except pipeline.Retryable as e:  # сервис не отдаёт картинки, часть не написалась — стоит повторить позже
                err = str(e)
            except (pipeline.Cancelled, SystemExit):  # «Стоп» или ошибка настроек (нет ключа и т.п.) — повтор не поможет
                raise
            except Exception as e:  # сбой сети или сервиса
                err = f"{type(e).__name__}: {e}"
                with LOCK:
                    STATE["log"].append(traceback.format_exc()[-1500:])
            if err is None:
                with LOCK:  # у профиля папка задания своя — окно смотрит туда
                    STATE["result"] = out
                    STATE["job_dir"] = pipeline.LAST_JOB_DIR or STATE.get("job_dir")
                bad = dict(pipeline.LAST_FAILURES)
                if not (auto and (bad["images"] or bad["videos"]) and fixes < len(FIX_DELAYS)):
                    break
                what = " и ".join(x for x in (f"картинок подменено соседними: {bad['images']}" if bad["images"] else "",
                                              f"сцен не оживилось: {bad['videos']}" if bad["videos"] else "") if x)
                sec = FIX_DELAYS[fixes]
                fixes += 1
                if not _pause(sec, f"ролик готов, но {what} — через {sec // 60} мин доделаю их и пересоберу ролик "
                                   f"(доводка {fixes} из {len(FIX_DELAYS)}; «Стоп» — оставить как есть)"):
                    break
                with LOCK:
                    STATE["result"] = None
                continue
            if not auto or tries >= len(RETRY_DELAYS):
                raise _Failed(err if tries == 0 else f"{err} (после {tries} повторов)")
            sec = RETRY_DELAYS[tries]
            tries += 1
            if not _pause(sec, f"сбой: {err[:300]} — повтор {tries} из {len(RETRY_DELAYS)} через {sec // 60} мин, всё готовое сохранено"):
                raise pipeline.Cancelled()
    except pipeline.Cancelled:
        with LOCK:
            STATE["cancelled"] = True
            STATE["message"] = "остановлено"
    except (_Failed, SystemExit) as e:
        with LOCK:
            STATE["error"] = str(e)
    except Exception as e:
        with LOCK:
            STATE["error"] = f"{type(e).__name__}: {e}"
            STATE["log"].append(traceback.format_exc()[-1500:])
    finally:
        with LOCK:
            STATE["running"] = False
            STATE["ended_at"] = time.time()
            res, err, stopped, jd = STATE.get("result"), STATE.get("error"), STATE.get("cancelled"), STATE.get("job_dir")
        job_record(jd or pipeline.LAST_JOB_DIR, "done" if res and not err else ("stopped" if stopped else "error"), res, err)
        keep_awake(False)


def job_chain(args):
    """Ролик, а после него — задания, поставленные в очередь во время работы (у каждого свои настройки)."""
    api, item, chained = Api(), None, False
    try:
        while args:
            run_job(**args)
            if item is not None:
                with LOCK:
                    if STATE.get("cancelled"):
                        item["status"] = "stopped"
                    elif STATE.get("error"):
                        item["status"], item["error"] = "error", STATE["error"]
                    else:
                        item["status"], item["result"] = "done", STATE.get("result")
                queue_save()
            args, item = None, None
            while True:
                with LOCK:
                    if STATE.get("cancelled") or QUEUE_STOP:
                        break
                    nxt = next((x for x in QUEUE if x.get("status") == "queued" and x.get("opts")), None)
                    if nxt is None:
                        break
                    nxt["status"] = "running"
                err, a, title = api._job(nxt["opts"])
                if err:
                    with LOCK:
                        nxt["status"], nxt["error"] = "error", err
                    queue_save()
                    continue
                with LOCK:
                    pos = QUEUE.index(nxt) + 1
                    STATE.update({"running": True, "queue_active": True, "stage": "start", "done": 0, "total": 0, "message": "следующий ролик из очереди",
                                  "log": [], "result": None, "error": None, "cancelled": False, "started_at": time.time(), "scenes": 0,
                                  "title": title, "queue": {"id": nxt["id"], "pos": pos, "total": len(QUEUE), "title": title}})
                queue_save()
                args, item, chained = a, nxt, True
                break
    finally:
        if chained:
            with LOCK:
                STATE["queue_active"] = False
                STATE["running"] = False


def _vtuple(v):
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def check_update():
    """Ищет в папке на Google Диске файл VideoGen-x.y.z.zip новее текущей версии."""
    if not UPDATE_FOLDER_ID:
        return None
    import re
    try:
        r = requests.get(f"https://drive.google.com/embeddedfolderview?id={UPDATE_FOLDER_ID}", timeout=10)
        if r.status_code != 200:
            return None
        html = r.text
        best = None
        # в списке папки каждая запись: ссылка file/d/<id>/view и заголовок с именем файла
        for m in re.finditer(r'href="https://drive\.google\.com/file/d/([\w-]+)/view.*?class="flip-entry-title">([^<]+)<', html, re.S):
            fid, name = m.group(1), m.group(2).strip()
            vm = re.match(r"(?:KadroGen|Kadro\.Gen|VideoGen)-(\d+\.\d+\.\d+)\.zip$", name, re.I)
            if not vm:
                continue
            ver = vm.group(1)
            if _vtuple(ver) > _vtuple(VERSION) and (best is None or _vtuple(ver) > _vtuple(best["version"])):
                best = {"version": ver, "url": f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t",
                        "page": f"https://drive.google.com/file/d/{fid}/view", "notes": ""}
        return best
    except Exception:
        return None


class Api:
    def get_state(self):
        cfg = load_cfg()
        with LOCK:
            st = dict(STATE)
        st["stage_name"] = STAGE_NAMES.get(st["stage"], st["stage"])
        st["queue_items"] = queue_public()
        st["elapsed"] = int(((not st.get("running") and st.get("ended_at")) or time.time()) - st["started_at"]) if st.get("started_at") else 0
        w = dict(pipeline.WAIT)
        st["wait"] = ({"kind": w.get("kind"), "why": w.get("why", ""), "left": max(0, int(w["until"] - time.time())) if w.get("until") else None,
                       "since": int(time.time() - w.get("since", time.time()))} if w and st["running"] else None)
        st["config"] = {
            "fastgen": mask(cfg.get("fastgen_api_key", "")),
            "nonstop": mask(cfg.get("veononstop_api_key", "")),
            "yougen": mask(cfg.get("yougen_api_key", "")),
            "has_fastgen": bool(cfg.get("fastgen_api_key")),
            "has_nonstop": bool(cfg.get("veononstop_api_key")),
            "has_yougen": bool(cfg.get("yougen_api_key")),
            "royal": mask(cfg.get("royaltechno_api_key", "")),
            "has_royal": bool(cfg.get("royaltechno_api_key")),
            "slider": mask(cfg.get("secretslider_api_key", "")), "has_slider": bool(cfg.get("secretslider_api_key")),
            "anthropic": mask(cfg.get("anthropic_api_key", "")), "has_anthropic": bool(cfg.get("anthropic_api_key")),
            "gemini": mask(cfg.get("gemini_api_key", "")), "has_gemini": bool(cfg.get("gemini_api_key")),
            "assemblyai": mask(cfg.get("assemblyai_api_key", "")), "has_assemblyai": bool(cfg.get("assemblyai_api_key")),
            "voicegen": mask(cfg.get("voicegen_api_key", "")), "has_voicegen": bool(cfg.get("voicegen_api_key")),
            "voicer": mask(cfg.get("voicer_api_key", "")), "has_voicer": bool(cfg.get("voicer_api_key")),
            "lumean": mask(cfg.get("lumean_api_key", "")), "has_lumean": bool(cfg.get("lumean_api_key")),
            "stt_engine": cfg.get("stt_engine", "fastgen"), "llm_engine": cfg.get("llm_engine", "fastgen"),
            "anthropic_model": cfg.get("anthropic_model", "claude-sonnet-5-5"), "gemini_model": cfg.get("gemini_model", "gemini-3.6-flash"),
            "base_urls": {k: cfg.get(k + "_base_url", "") for k in ("openai", "anthropic", "gemini")}, "base_defaults": dict(endpoints.DEFAULTS),
            "quality": cfg.get("quality", "speed"), "motion": bool(cfg.get("motion", False)), "hd_images": bool(cfg.get("hd_images", False)), "video_first": _vf(cfg.get("video_first", 0)),
            "tts_recent": cfg.get("tts_recent", []), "data_dir": data_dir(), "portable": data_dir() == app_dir(),
            "profiles": pipeline.list_profiles(), "profile": cfg.get("profile", ""),
            "source": cfg.get("source", "mp3"), "tts_service": cfg.get("tts_service", "voicegen"), "tts_voice": cfg.get("tts_voice", ""),
            "rewrite": bool(cfg.get("rewrite", False)), "rewrite_engine": cfg.get("rewrite_engine", "fastgen"), "rewrite_source": cfg.get("rewrite_source", "youtube"),
            "rewrite_url": cfg.get("rewrite_url", ""), "rewrite_words": cfg.get("rewrite_words", 1500), "rewrite_lang": cfg.get("rewrite_lang", "польском"),
            "openai": mask(cfg.get("openai_api_key", "")),
            "has_openai": bool(cfg.get("openai_api_key")),
            "text_engine": cfg.get("text_engine", "fastgen"),
            "openai_model": cfg.get("openai_model", "gpt-4.1-mini"),
            "theme": cfg.get("theme", "dark"),
            "music": cfg.get("music_volume", 50),
            "tracks": music_tracks(),
            "providers": [p for p in (cfg.get("image_providers") or []) if p in pipeline.PROVIDERS],
            "keys": {p: [mask(k) for k in pipeline.provider_keys(cfg, p)] for p in pipeline.PROVIDERS},
            "lang": cfg.get("lang", "ru"),
            "mode": cfg.get("mode", "images"),
            "style": style_key(cfg.get("style"), pipeline.DEFAULT_STYLE),
            "custom_style": cfg.get("custom_style", ""), "video_prompt": cfg.get("video_prompt", ""),
            "fg_models": pipeline.fastgen_models(), "fg_image_model": cfg.get("fg_image_model", ""), "fg_video_model": cfg.get("fg_video_model", ""),
            "speed_copies": bool(cfg.get("speed_copies", False)),
            "qa_images": bool(cfg.get("qa_images", False)), "qa_engine": cfg.get("qa_engine", "gemini"),
            "openrouter": mask(cfg.get("openrouter_api_key", "")), "has_openrouter": bool(cfg.get("openrouter_api_key")),
            "qa_model": cfg.get("qa_model", ""),
            "queue_styles": bool(cfg.get("queue_styles", False)),
            "use_refs": bool(cfg.get("use_refs", False)),
            "subtitles": bool(cfg.get("subtitles", False)),
            "llm_model": cfg.get("llm_model", pipeline.DEFAULT_LLM_MODEL),
        }
        st["styles"] = [{"key": k, "name": v[0], "name_en": v[2]} for k, v in pipeline.STYLES.items()]
        st["version"] = VERSION
        return st

    def save_config(self, data):
        cfg = load_cfg()
        for field, key in (("fastgen", "fastgen_api_key"), ("nonstop", "veononstop_api_key"), ("yougen", "yougen_api_key"),
                           ("openai", "openai_api_key"), ("royal", "royaltechno_api_key"), ("slider", "secretslider_api_key"), ("google", "google_api_key"),
                           ("anthropic", "anthropic_api_key"), ("gemini", "gemini_api_key"), ("assemblyai", "assemblyai_api_key"), ("openrouter", "openrouter_api_key"),
                           ("voicegen", "voicegen_api_key"), ("voicer", "voicer_api_key"), ("lumean", "lumean_api_key")):
            if field in data:
                v = (data.get(field) or "").strip()
                if v and "•" not in v:
                    cfg[key] = v
                elif v == "":
                    cfg[key] = ""
                if field in pipeline.SINGLE_KEY and "•" not in v:
                    cfg.setdefault("image_keys", {})[field] = [v] if v else []
        if isinstance(data.get("keys"), dict):
            ik = cfg.setdefault("image_keys", {})
            for p, rows in data["keys"].items():
                if p not in pipeline.PROVIDERS:
                    continue
                old = pipeline.provider_keys(cfg, p)
                new = []
                for r in rows or []:
                    v, idx = (str((r or {}).get("value") or "")).strip(), (r or {}).get("idx", -1)
                    if not v:
                        continue
                    if "•" in v:
                        if isinstance(idx, int) and 0 <= idx < len(old) and old[idx] not in new:
                            new.append(old[idx])
                    elif v not in new:
                        new.append(v)
                ik[p] = new
        if "providers" in data:
            cfg["image_providers"] = [p for p in (data["providers"] or []) if p in pipeline.PROVIDERS]
        if "profile" in data:
            p = str(data.get("profile") or "").strip()
            cfg["profile"] = p if (not p or p in pipeline.list_profiles()) else cfg.get("profile", "")
        if data.get("lang") in ("ru", "en"):
            cfg["lang"] = data["lang"]
        for k in ("openai", "anthropic", "gemini"):
            if k + "_base_url" in data:
                v = str(data[k + "_base_url"] or "").strip().rstrip("/")
                cfg[k + "_base_url"] = v if v.startswith(("http://", "https://")) else ""
        if data.get("mode") in pipeline.MODES:
            cfg["mode"] = data["mode"]
        if style_key(data.get("style")):
            cfg["style"] = data["style"]
        if "video_prompt" in data:
            cfg["video_prompt"] = str(data.get("video_prompt") or "").strip()[:4000]
        for k in ("fg_image_model", "fg_video_model"):
            if k in data:
                cfg[k] = str(data.get(k) or "").strip()[:120]
        if "custom_style" in data:
            cfg["custom_style"] = " ".join(str(data.get("custom_style") or "").split())
        if "queue_styles" in data:
            cfg["queue_styles"] = bool(data.get("queue_styles"))
        if data.get("text_engine") in pipeline.TEXT_ENGINES:
            cfg["text_engine"] = data["text_engine"]
        if data.get("openai_model"):
            cfg["openai_model"] = data["openai_model"].strip()
        if "music" in data:
            try:
                cfg["music_volume"] = max(0, min(100, int(data["music"])))
            except (TypeError, ValueError):
                pass
        if data.get("theme") in ("light", "dark"):
            cfg["theme"] = data["theme"]
        for k, allowed in (("stt_engine", ("fastgen", "whisper", "assemblyai")), ("llm_engine", ("fastgen", "openai", "anthropic", "gemini")),
                           ("quality", ("speed", "quality")), ("source", ("mp3", "script")),
                           ("tts_service", ("voicegen", "voicer", "lumean", "edge", "fastgen", "royal"))):
            if data.get(k) in allowed:
                cfg[k] = data[k]
        for k in ("anthropic_model", "gemini_model"):
            if data.get(k):
                cfg[k] = data[k].strip()
        if "tts_voice" in data:
            cfg["tts_voice"] = (data.get("tts_voice") or "").strip()
        if "motion" in data:
            cfg["motion"] = bool(data["motion"])
        if "hd" in data:
            cfg["hd_images"] = bool(data["hd"])
        if "qa" in data:
            cfg["qa_images"] = bool(data["qa"])
        if "speed" in data:
            cfg["speed_copies"] = bool(data["speed"])
        if data.get("qa_engine") in pipeline.QA_ENGINES:
            cfg["qa_engine"] = data["qa_engine"]
        if "qa_model" in data:
            cfg["qa_model"] = str(data.get("qa_model") or "").strip()
        if "video_first" in data:
            cfg["video_first"] = _vf(data["video_first"])
        if "rewrite" in data:
            cfg["rewrite"] = bool(data["rewrite"])
        if data.get("rewrite_engine") in ("openai", "anthropic", "gemini", "fastgen"):
            cfg["rewrite_engine"] = data["rewrite_engine"]
        if data.get("rewrite_source") in ("youtube", "text"):
            cfg["rewrite_source"] = data["rewrite_source"]
        if "rewrite_url" in data:
            cfg["rewrite_url"] = (data.get("rewrite_url") or "").strip()
        if "rewrite_words" in data:
            try:
                cfg["rewrite_words"] = max(100, min(20000, int(data["rewrite_words"])))
            except (TypeError, ValueError):
                pass
        if data.get("rewrite_lang"):
            cfg["rewrite_lang"] = str(data["rewrite_lang"]).strip()
        if "use_refs" in data:
            cfg["use_refs"] = bool(data["use_refs"])
        if "subtitles" in data:
            cfg["subtitles"] = bool(data["subtitles"])
        if data.get("llm_model"):
            cfg["llm_model"] = data["llm_model"].strip()
        save_cfg(cfg)
        return {"ok": True}

    def check_keys(self, which):
        cfg = load_cfg()
        out = {}

        def ck_fastgen(key):
            u = FastGen(key).usage()
            lim = u.get("account_limits", {})
            caps = u.get("capabilities") or {}
            video = "видео доступно" if caps.get("video.generate") else "видео на ключе выключено"
            base = f"OK · {lim.get('img_gen_per_hour_limit')} кредитов картинок в час · {lim.get('img_generation_threads_allowed')} потоков · {video}"
            try:  # тексты сцен идут через чат fast-gen — проверяем, что модель реально отвечает
                r = FastGen(key).s.post(pipeline.BASE_URL + "/v1/chat/completions", json={
                    "model": pipeline.DEFAULT_LLM_MODEL, "max_tokens": 30, "temperature": 0,
                    "messages": [{"role": "user", "content": 'Ответь строго JSON: {"ok": true}'}]}, timeout=45)
                if r.status_code >= 400:
                    return base + f" · тексты: модель не ответила ({r.status_code})"
                pipeline.extract_json(r.json()["choices"][0]["message"]["content"])
                return base + " · тексты отвечают"
            except Exception as e:
                return base + f" · тексты: {str(e)[:60]}"

        def qa_ping(client, name):
            """Проверка ключа для автоотбора: модель должна увидеть картинку."""
            client.check()
            from PIL import Image, ImageDraw
            tmp = os.path.join(data_dir(), "_qa_test.png")
            im = Image.new("RGB", (320, 180), (40, 60, 90))
            ImageDraw.Draw(im).ellipse((110, 40, 210, 140), fill=(230, 200, 60))
            im.save(tmp)
            try:
                j = client.see_json("Ответь строго JSON.", 'Что на картинке? Верни {"shape": "circle|square|other", "ok": true}', tmp, max_tokens=100)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if not isinstance(j, dict):
                raise RuntimeError("модель ответила не JSON")
            return f"OK · {name} видит картинки"

        def llm_ping(client, name):
            client.check()
            j = client.chat_json("Отвечай строго JSON.", 'Верни {"ok": true}', max_tokens=200)
            if not isinstance(j, dict):
                raise RuntimeError("модель ответила не JSON")
            return f"OK · {name} отвечает"

        def ck_nonstop(key):
            from nonstop_api import NonStop
            info = NonStop(key).account_info()
            return f"OK · тариф {info.get('plan')} · {info.get('concurrent_tasks')} одновременных задач"

        def ck_yougen(key):
            from yougen_api import YouGen
            u = YouGen(key).usage()
            img = u.get("image") if isinstance(u.get("image"), dict) else {}
            rem = img.get("remaining", u.get("image_remaining"))
            lim = img.get("limit", u.get("image_limit"))
            return "OK" + (f" · картинок сегодня осталось {rem} из {lim}" if rem is not None else "")

        def ck_royal(key):
            from royaltechno_api import RoyalTechno
            a = RoyalTechno(key).account()
            plans = ", ".join(pl.get("name", "") for pl in a.get("plans") or []) or "без плана"
            return f"OK · баланс {a.get('balance_usd')} · {plans} · до {(a.get('limits') or {}).get('recommended_max_in_flight')} задач сразу"

        def ck_slider(key):
            from secretslider_api import SecretSlider
            b = SecretSlider(key).balance()
            return f"OK · кредитов {b.get('api_credits')} · лимит {b.get('rate_limit_per_day')} в день"

        def ck_google(key):
            from google_api import GoogleAI
            return GoogleAI(key).check()

        checkers = {"fastgen": ck_fastgen, "nonstop": ck_nonstop, "yougen": ck_yougen, "royal": ck_royal, "slider": ck_slider, "google": ck_google}
        for p, fn in checkers.items():
            if p not in which:
                continue
            keys = pipeline.provider_keys(cfg, p)
            if not keys:
                out[p] = ["Ключ не введён"]
                continue
            res = []
            for k in keys:
                try:
                    res.append(fn(k))
                except Exception as e:
                    res.append(key_error(e))
            out[p] = res
        from llm_api import AnthropicClient, GeminiClient, AssemblyAI
        if "qa" in which:
            eng = cfg.get("qa_engine") or "gemini"
            if cfg.get(f"{eng}_api_key"):
                try:
                    c = pipeline.make_qa_client({**cfg, "qa_images": True})
                    out["qa"] = qa_ping(c, c.model)
                except Exception as e:
                    out["qa"] = f"Ошибка: {str(e)[:120]}"
            else:
                out["qa"] = "Ключ не введён"
        for name, key, cls in (("anthropic", "anthropic_api_key", AnthropicClient), ("gemini", "gemini_api_key", GeminiClient), ("assemblyai", "assemblyai_api_key", AssemblyAI)):
            if name in which:
                if cfg.get(key):
                    try:
                        c = cls(cfg[key]) if name == "assemblyai" else cls(cfg[key], cfg.get(f"{name}_model"))
                        out[name] = llm_ping(c, cfg.get(f"{name}_model")) if name != "assemblyai" else c.check()
                    except Exception as e:
                        out[name] = f"Ошибка: {str(e)[:100]}"
                else:
                    out[name] = "Ключ не введён"
        from tts_api import make_tts
        if "edge" in which:
            try:
                from tts_api import make_tts
                out["edge"] = make_tts("edge", "").check()
            except Exception as e:
                out["edge"] = f"Ошибка: {str(e)[:100]}"
        for name in ("voicegen", "voicer", "lumean"):
            if name in which:
                if cfg.get(f"{name}_api_key"):
                    try:
                        out[name] = make_tts(name, cfg[f"{name}_api_key"]).check()
                    except Exception as e:
                        out[name] = f"Ошибка: {str(e)[:100]}"
                else:
                    out[name] = "Ключ не введён"
        if "openai" in which:
            if cfg.get("openai_api_key"):
                try:
                    from openai_api import OpenAIClient
                    c = OpenAIClient(cfg["openai_api_key"])
                    c.check()
                    j = c.chat_json("Отвечай строго JSON.", 'Верни {"ok": true}', model=cfg.get("openai_model") or "gpt-4.1-mini", max_tokens=200)
                    out["openai"] = f"OK · {cfg.get('openai_model') or 'gpt-4.1-mini'} отвечает" if isinstance(j, dict) else "Ошибка: модель ответила не JSON"
                except Exception as e:
                    out["openai"] = key_error(e)
            else:
                out["openai"] = "Ключ не введён"
        return out

    def pick_file(self):
        res = WINDOW.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Аудио (*.mp3;*.wav;*.m4a;*.ogg)", "Все файлы (*.*)"),
        )
        if not res:
            return ""
        return res[0] if isinstance(res, (list, tuple)) else str(res)

    def _rewrite_opts(self, opts):
        rw = opts.get("rewrite") or {}
        if not rw.get("on"):
            return None
        return {"on": True, "source": rw.get("source") or "youtube", "url": (rw.get("url") or "").strip(), "text": (rw.get("text") or "").strip(),
                "engine": rw.get("engine") or "fastgen", "words": rw.get("words") or 1500, "language": (rw.get("language") or "польском").strip()}

    def _validate(self, opts, providers):
        return self._validate_source(opts) or self._validate_common(providers)

    def _validate_source(self, opts):
        cfg = load_cfg()
        rw = self._rewrite_opts(opts)
        if rw:
            if rw["source"] == "youtube" and not rw["url"]:
                return "Вставьте ссылку на ролик YouTube."
            if rw["source"] == "text" and not rw["text"]:
                return "Вставьте текст сценария для рерайта."
            if not cfg.get(pipeline.REWRITE_KEYS[rw["engine"]]):
                return "Для рерайта выбран " + {"openai": "ChatGPT", "anthropic": "Claude", "gemini": "Gemini", "fastgen": "fast-gen"}[rw["engine"]] + ", введите его ключ."
            tts = opts.get("tts") or cfg.get("tts_service")
            if not cfg.get(f"{tts}_api_key") and tts != "edge":
                return f"После рерайта сценарий озвучивается ({tts}), введите ключ озвучки."
        elif opts.get("source") == "script":
            if not (opts.get("script") or "").strip():
                return "Вставьте текст сценария."
            vf = (opts.get("voice_file") or "").strip().strip('"')
            if vf:
                return None if os.path.isfile(vf) else "Файл готовой озвучки не найден."
            tts = opts.get("tts") or cfg.get("tts_service")
            if not cfg.get(f"{tts}_api_key") and tts != "edge":
                return f"Для озвучки выбран {tts}, но его ключ не введён."
        elif not os.path.isfile(opts.get("audio") or ""):
            return "Файл озвучки не найден. Выберите mp3 или перетащите его в окно."
        return None

    def _validate_common(self, providers):
        cfg = load_cfg()
        if "fastgen" in providers and not cfg.get("fastgen_api_key"):
            return "Введите ключ fast-gen."
        if cfg.get("qa_images"):
            eng = cfg.get("qa_engine") or "gemini"
            if not cfg.get(f"{eng}_api_key"):
                return f"Включён автоотбор картинок, введите ключ {pipeline.QA_ENGINES.get(eng, ('', '', None))[0]} в строке «Автоотбор» или выключите опцию."
        has_fg = bool(cfg.get("fastgen_api_key"))
        stt = cfg.get("stt_engine") or ("fastgen" if has_fg else "whisper"); llm = cfg.get("llm_engine") or ("fastgen" if has_fg else "openai")
        if stt == "assemblyai" and not cfg.get("assemblyai_api_key"):
            return "Для распознавания речи выбран AssemblyAI, введите его ключ."
        if (stt == "whisper" or (stt == "fastgen" and not has_fg)) and not cfg.get("openai_api_key"):
            return "Для распознавания речи нужен ключ OpenAI (Whisper)."
        if llm != "fastgen" or not has_fg:
            need = {"openai": "openai_api_key", "anthropic": "anthropic_api_key", "gemini": "gemini_api_key"}.get(llm, "openai_api_key")
            if not cfg.get(need):
                return "Для текстов выбран " + {"openai": "OpenAI", "anthropic": "Claude", "gemini": "Gemini"}.get(llm, llm) + ", введите его ключ."
        if "nonstop" in providers and not cfg.get("veononstop_api_key"):
            return "Отмечен veononstop, но его ключ не введён."
        if "yougen" in providers and not cfg.get("yougen_api_key"):
            return "Отмечен yougen, но его ключ не введён."
        if "royal" in providers and not cfg.get("royaltechno_api_key"):
            return "Отмечен royaltechno, но его ключ не введён."
        if "slider" in providers and not cfg.get("secretslider_api_key"):
            return "Отмечен secretslider, но его ключ не введён."
        if "google" in providers and not cfg.get("google_api_key"):
            return "Отмечен Google, но его ключ не введён."
        return None

    def prepare(self, opts):
        """Озвучка (если сценарий), транскрипция и расчёт: сколько сцен, кредитов, минут."""
        audio = (opts.get("audio") or "").strip().strip('"')
        provider = pipeline.normalize_providers(opts.get("providers"))
        mode = opts.get("mode", "images")
        use_refs = (load_cfg().get("quality") == "quality")  # качество = Nano Banana с портретами героев
        err = self._validate(opts, provider)
        if err:
            return {"ok": False, "error": err}
        cfg = load_cfg()
        cfg.update({"source": opts.get("source", "mp3"), "tts_service": opts.get("tts") or cfg.get("tts_service"),
                    "tts_voice": opts.get("voice") or ""})
        v = (opts.get("voice") or "").strip()
        if v:
            rec = [x for x in cfg.get("tts_recent", []) if x.get("id") != v]
            rec.insert(0, {"id": v, "service": cfg["tts_service"]})
            cfg["tts_recent"] = rec[:12]
        save_cfg(cfg)
        script = (opts.get("script") or "").strip() if opts.get("source") == "script" else None
        rw = self._rewrite_opts(opts)
        if rw:
            script = None
            cfg = load_cfg()
            cfg.update({"rewrite": True, "rewrite_engine": rw["engine"], "rewrite_source": rw["source"], "rewrite_url": rw["url"],
                        "rewrite_words": int(rw["words"]), "rewrite_lang": rw["language"]})
            save_cfg(cfg)
        with LOCK:
            if STATE["running"] or STATE["preparing"]:
                return {"ok": False, "error": "Программа уже занята."}
            STATE.update({"preparing": True, "stage": "prepare", "done": 0, "total": 0, "message": "распознаю речь",
                          "log": [], "result": None, "error": None, "estimate": None, "cancelled": False, "started_at": time.time()})
        threading.Thread(target=run_prepare, args=(audio, provider, mode, use_refs, script, rw), daemon=True).start()
        return {"ok": True}

    def rewrite(self, opts):
        """Переписать сценарий (YouTube или текст) и вернуть его на экран проверки."""
        rw = self._rewrite_opts(opts)
        if not rw:
            return {"ok": False, "error": "Рерайт не включён."}
        cfg = load_cfg()
        if rw["source"] == "youtube" and not rw["url"]:
            return {"ok": False, "error": "Вставьте ссылку на ролик YouTube."}
        if rw["source"] == "text" and not rw["text"]:
            return {"ok": False, "error": "Вставьте текст сценария для рерайта."}
        if not cfg.get(pipeline.REWRITE_KEYS[rw["engine"]]):
            return {"ok": False, "error": "Для рерайта выбран " + {"openai": "ChatGPT", "anthropic": "Claude", "gemini": "Gemini", "fastgen": "fast-gen"}[rw["engine"]] + ", введите его ключ."}
        cfg.update({"rewrite": True, "rewrite_engine": rw["engine"], "rewrite_source": rw["source"], "rewrite_url": rw["url"],
                    "rewrite_words": int(rw["words"]), "rewrite_lang": rw["language"]})
        save_cfg(cfg)
        with LOCK:
            if STATE["running"] or STATE["preparing"]:
                return {"ok": False, "error": "Программа уже занята."}
            STATE.update({"preparing": True, "stage": "rewrite", "done": 0, "total": 0, "message": "переписываю сценарий",
                          "log": [], "result": None, "error": None, "script": None, "cancelled": False, "started_at": time.time()})
        threading.Thread(target=run_rewrite, args=(rw,), daemon=True).start()
        return {"ok": True}

    def _job(self, opts):
        """Проверить запуск и записать его настройки в config.json. → (ошибка, аргументы run_job, название)."""
        audio = (opts.get("audio") or "").strip().strip('"')
        provider = pipeline.normalize_providers(opts.get("providers"))
        mode, style, subtitles = opts.get("mode", "images"), style_key(opts.get("style"), pipeline.DEFAULT_STYLE), bool(opts.get("subtitles"))
        cfg = load_cfg()
        use_refs = (cfg.get("quality") == "quality")
        profile = opts.get("profile")
        if profile is None:
            profile = cfg.get("profile", "")
        custom_style = " ".join(str(opts.get("custom_style") or "").split())
        if style == pipeline.CUSTOM_STYLE and not custom_style and not profile:  # с профилем стиль не нужен
            return "Опишите свой стиль на экране «Режим» или выберите стиль из списка.", None, None
        motion = bool(opts.get("motion"))
        hd = bool(opts.get("hd"))
        video_first = _vf(opts.get("video_first"))
        script = (opts.get("script") or "").strip() if opts.get("source") == "script" else None
        voice_file = (opts.get("voice_file") or "").strip().strip('"') or None if script else None
        rw = self._rewrite_opts(opts)
        if rw:
            script = None
        err = self._validate(opts, provider)
        if err:
            return err, None, None
        tts = opts.get("tts") or cfg.get("tts_service") or "voicegen"
        voice = (opts.get("voice") or "").strip()
        cfg.update({"image_providers": provider, "mode": mode, "style": style, "custom_style": custom_style, "use_refs": use_refs, "subtitles": subtitles, "motion": motion, "hd_images": hd,
                    "qa_images": bool(opts.get("qa", cfg.get("qa_images", False))), "speed_copies": bool(opts.get("speed", cfg.get("speed_copies", False))),
                    "source": opts.get("source", cfg.get("source", "mp3")), "tts_service": tts, "tts_voice": voice, "video_first": video_first, "profile": profile})
        if "video_prompt" in opts:
            cfg["video_prompt"] = str(opts.get("video_prompt") or "").strip()[:4000]
        for k in ("fg_image_model", "fg_video_model"):
            if k in opts:
                cfg[k] = str(opts.get(k) or "").strip()[:120]
        if voice:
            rec = [x for x in cfg.get("tts_recent", []) if x.get("id") != voice]
            rec.insert(0, {"id": voice, "service": tts})
            cfg["tts_recent"] = rec[:12]
        save_cfg(cfg)
        args = dict(audio=audio, provider=provider, mode=mode, style=style, use_refs=use_refs, subtitles=subtitles, motion=motion,
                    script=script, rewrite=rw, tts=tts, voice=voice, video_first=video_first, hd=hd, profile=profile, voice_file=voice_file)
        return None, args, job_title(opts)

    def _enqueue(self, opts):
        """Запуск во время работы: ролик встаёт в очередь со своими настройками и начнётся сам после текущего."""
        o = dict(opts)
        if o.get("profile") is None:
            o["profile"] = load_cfg().get("profile", "")
        err = self._validate(o, pipeline.normalize_providers(o.get("providers")))
        if err:
            return {"ok": False, "error": err}
        title = job_title(o)
        src = "rewrite" if (o.get("rewrite") or {}).get("on") else ("script" if o.get("source") == "script" else "mp3")
        entry = {"id": uuid.uuid4().hex[:8], "title": title[:80], "source": src, "script": (o.get("script") or "").strip() or None,
                 "audio": (o.get("audio") or "").strip().strip('"') or None, "rewrite": None, "voice": (o.get("voice") or "").strip(),
                 "style": style_key(o.get("style"), ""), "status": "queued", "result": None, "error": None, "opts": o}
        with LOCK:
            QUEUE.append(entry)
            pos = sum(1 for x in QUEUE if x.get("status") == "queued")
        queue_save()
        return {"ok": True, "queued": True, "pos": pos, "title": title, "items": queue_public()}

    def start(self, opts):
        global QUEUE_STOP
        with LOCK:
            busy = STATE["running"] or STATE["preparing"] or STATE.get("queue_active")
        if busy:
            return self._enqueue(opts)
        err, args, title = self._job(opts)
        if err:
            return {"ok": False, "error": err}
        with LOCK:
            busy = STATE["running"] or STATE["preparing"] or STATE.get("queue_active")
            if not busy:
                STATE.update({"running": True, "stage": "start", "done": 0, "total": 0, "message": "запуск", "log": [], "result": None,
                              "error": None, "cancelled": False, "started_at": time.time(), "title": title, "queue": None})
        if busy:
            return self._enqueue(opts)
        QUEUE_STOP = False
        threading.Thread(target=job_chain, args=(args,), daemon=True).start()
        return {"ok": True}

    def retry_last(self):
        """Кнопка «Повторить»: тот же ролик с теми же настройками; всё готовое возьмётся из задания."""
        global QUEUE_STOP
        with LOCK:
            if STATE["running"] or STATE["preparing"] or STATE.get("queue_active"):
                return {"ok": False, "error": "Уже идёт создание видео."}
            if not LAST_RUN:
                return {"ok": False, "error": "Повторять нечего: программа перезапускалась. Запустите ролик заново — готовое возьмётся из задания."}
            STATE.update({"running": True, "stage": "start", "done": 0, "total": 0, "message": "повтор", "log": [], "result": None,
                          "error": None, "cancelled": False, "started_at": time.time(), "queue": None})
        QUEUE_STOP = False
        threading.Thread(target=job_chain, args=(dict(LAST_RUN),), daemon=True).start()
        return {"ok": True}

    # ---------- очередь заданий ----------
    def queue_list(self):
        with LOCK:
            return {"items": queue_public(), "active": bool(STATE.get("queue_active"))}

    def queue_add(self, item):
        src = (item or {}).get("source") or "mp3"
        opts = {"source": "script" if src == "script" else "mp3", "script": item.get("script") or "", "audio": item.get("audio") or "",
                "rewrite": {**(item.get("rewrite") or {}), "on": src == "rewrite"}, "tts": item.get("tts")}
        err = self._validate_source(opts)
        if err:
            return {"ok": False, "error": err}
        if src == "script":
            title = " ".join((item.get("script") or "").split()[:7]).lstrip("—–-:;,. ")
        elif src == "rewrite":
            rw = self._rewrite_opts(opts["rewrite"] and opts) or {}
            title = rw.get("url") or " ".join((rw.get("text") or "").split()[:7]) or "рерайт"
        else:
            title = os.path.basename(item.get("audio") or "")
        entry = {"id": uuid.uuid4().hex[:8], "title": title[:80], "source": src, "script": (item.get("script") or "").strip() or None,
                 "audio": (item.get("audio") or "").strip().strip('"') or None,
                 "rewrite": self._rewrite_opts(opts) if src == "rewrite" else None,
                 "voice": (item.get("voice") or "").strip(), "style": style_key(item.get("style"), ""), "status": "queued", "result": None, "error": None}
        with LOCK:
            QUEUE.append(entry)
        queue_save()
        return {"ok": True, "items": queue_public()}

    def queue_set_voice(self, qid, voice):
        with LOCK:
            for x in QUEUE:
                if x["id"] == qid:
                    x["voice"] = (voice or "").strip()
        queue_save()
        return True

    def queue_set_style(self, qid, style):
        """Стиль одного задания; пусто — как у всех."""
        with LOCK:
            for x in QUEUE:
                if x["id"] == qid:
                    x["style"] = style_key(style, "")
        queue_save()
        return True

    def queue_remove(self, qid):
        with LOCK:
            QUEUE[:] = [x for x in QUEUE if not (x["id"] == qid and x.get("status") != "running")]
        queue_save()
        return queue_public()

    def queue_move(self, qid, delta):
        with LOCK:
            ids = [x["id"] for x in QUEUE]
            if qid in ids:
                i = ids.index(qid); j = max(0, min(len(QUEUE) - 1, i + int(delta)))
                QUEUE.insert(j, QUEUE.pop(i))
        queue_save()
        return queue_public()

    def queue_retry(self, qid):
        with LOCK:
            for x in QUEUE:
                if x["id"] == qid and x.get("status") in ("error", "stopped"):
                    x.update({"status": "queued", "error": None})
        queue_save()
        return queue_public()

    def queue_clear(self, only_done=True):
        with LOCK:
            QUEUE[:] = [x for x in QUEUE if x.get("status") in ("queued", "running")] if only_done else [x for x in QUEUE if x.get("status") == "running"]
        queue_save()
        return queue_public()

    def queue_start(self, opts):
        """Общие настройки — как у обычного запуска; текст/файл и голос у каждого задания свои."""
        global QUEUE_STOP
        provider = pipeline.normalize_providers(opts.get("providers"))
        err = self._validate_common(provider)
        if err:
            return {"ok": False, "error": err}
        cfg = load_cfg()
        tts = opts.get("tts") or cfg.get("tts_service") or "voicegen"
        with LOCK:
            todo = [x for x in QUEUE if x.get("status") == "queued"]
            if not todo:
                return {"ok": False, "error": "Очередь пуста — добавьте хотя бы одно задание."}
            if STATE["running"] or STATE["preparing"] or STATE.get("queue_active"):
                return {"ok": False, "error": "Уже идёт создание видео."}
            if any(x.get("source") in ("script", "rewrite") for x in todo) and not cfg.get(f"{tts}_api_key") and tts not in ("edge", "fastgen", "royal"):
                return {"ok": False, "error": f"Для озвучки выбран {tts}, но его ключ не введён."}
            for x in todo:
                if x.get("source") == "rewrite" and not cfg.get(pipeline.REWRITE_KEYS.get((x.get("rewrite") or {}).get("engine", "fastgen"), "fastgen_api_key")):
                    return {"ok": False, "error": "Для рерайта не введён ключ выбранной модели."}
            style = style_key(opts.get("style"), pipeline.DEFAULT_STYLE)
            custom_style = " ".join(str(opts.get("custom_style") or "").split())
            if not custom_style and (style == pipeline.CUSTOM_STYLE or any(x.get("style") == pipeline.CUSTOM_STYLE for x in todo)):
                return {"ok": False, "error": "Для «Своего стиля» опишите его на экране «Режим» или выберите стиль из списка."}
            STATE.update({"queue_active": True, "running": True, "stage": "start", "message": "очередь: запуск", "log": [], "result": None, "error": None, "cancelled": False})
            QUEUE_STOP = False
        common = {"provider": provider, "mode": opts.get("mode", "images"), "style": style, "use_refs": cfg.get("quality") == "quality",
                  "subtitles": bool(opts.get("subtitles")), "motion": bool(opts.get("motion")), "hd": bool(opts.get("hd")),
                  "video_first": _vf(opts.get("video_first")), "tts": tts}
        cfg.update({"image_providers": provider, "mode": common["mode"], "style": style, "custom_style": custom_style, "use_refs": common["use_refs"], "subtitles": common["subtitles"],
                    "motion": common["motion"], "hd_images": common["hd"], "qa_images": bool(opts.get("qa", cfg.get("qa_images", False))),
                    "speed_copies": bool(opts.get("speed", cfg.get("speed_copies", False))),
                    "tts_service": tts, "video_first": common["video_first"]})
        for x in todo:
            v = (x.get("voice") or "").strip()
            if v:
                rec = [r for r in cfg.get("tts_recent", []) if r.get("id") != v]
                rec.insert(0, {"id": v, "service": tts}); cfg["tts_recent"] = rec[:12]
        save_cfg(cfg)
        threading.Thread(target=run_queue, args=(common,), daemon=True).start()
        return {"ok": True}

    def fg_usage(self):
        """Строка лимитов fast-gen в окне: осталось в этом часе / лимит в час. Обновляется не чаще раза в минуту."""
        key = load_cfg().get("fastgen_api_key", "")
        if not key:
            return {"ok": False}
        c = _FG_USAGE
        if c["key"] != key or time.time() - c["at"] > 60 or c["data"] is None:
            try:
                u = FastGen(key).usage()
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}
            if not c["logged"]:  # один раз за запуск — полный ответ в журнал, чтобы сверить названия полей
                pipeline.log("fast-gen usage: " + json.dumps(u, ensure_ascii=False)[:3000])
                c["logged"] = True
            c.update({"at": time.time(), "key": key, "data": u})
        return {"ok": True, **fg_usage_summary(c["data"])}

    def profile_state(self):
        """Панель «Промпты по частям»: состояние частей профиля (без самих промптов)."""
        return pipeline.profiles.BOARD.summary()

    def profile_part(self, n):
        """Промпты одной части — по клику на часть в панели."""
        try:
            return pipeline.profiles.BOARD.part(int(n))
        except (TypeError, ValueError):
            return {"n": n, "prompts": [], "problems": [], "status": "wait", "live": ""}

    def gallery(self, known):
        """Кадры сцен, которых окно ещё не видело. known — список индексов, уже показанных."""
        with LOCK:
            job_dir, n = STATE.get("job_dir") or pipeline.LAST_JOB_DIR, STATE.get("scenes") or 0
        if not job_dir:
            return {"total": 0, "items": []}
        known = set(int(x) for x in (known or []))
        img_dir = os.path.join(job_dir, "images")
        vid_dir = os.path.join(job_dir, "videos")
        items = []
        with pipeline.REDO_LOCK:
            redo = sorted(pipeline.REDO)
        if os.path.isdir(img_dir):
            for name in sorted(os.listdir(img_dir)):
                if not name.endswith(".png") or name.endswith(".part"):
                    continue
                try:
                    i = int(name[:4])
                except ValueError:
                    continue
                if i in redo:  # ждёт перерисовки — старую картинку не показываем
                    continue
                has_video = os.path.exists(os.path.join(vid_dir, f"{i:04d}.mp4"))
                key = i * 2 + (1 if has_video else 0)
                if key in known or (i * 2 in known and not has_video):
                    continue
                thumb = make_thumb(os.path.join(img_dir, name))
                if thumb:
                    items.append({"i": i, "key": key, "img": thumb, "video": has_video})
        if not n:
            scenes = pipeline.read_json(os.path.join(job_dir, "scenes.json")) or []
            n = len(scenes)
        return {"total": n, "items": items[:12], "redo": redo}

    def redo_scene(self, index):
        """Кнопка ↻ на кадре. Пока ролик рисуется — кадр перерисуется до сборки; когда ролик готов — картинка
        удаляется, и повторный запуск того же ролика дорисует только её."""
        try:
            i = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad index"}
        with LOCK:
            job_dir, running, stage = STATE.get("job_dir") or pipeline.LAST_JOB_DIR, STATE.get("running"), STATE.get("stage")
        if not job_dir:
            return {"ok": False, "error": "нет задания"}
        if running:
            if stage in ("render", "subtitles", "done"):
                return {"ok": False, "late": True}
            pipeline.request_redo(i)
            pipeline.log(f"кадр {i + 1}: отмечен на перерисовку")
            return {"ok": True, "queued": True}
        pipeline.redo_files(job_dir, i)
        pipeline.log(f"кадр {i + 1}: удалён, будет нарисован заново при следующем запуске")
        return {"ok": True, "queued": False}

    def reveal_key(self, name, idx=0):
        """Показать сохранённый ключ целиком (по нажатию на глаз). idx — номер ключа у сервисов картинок."""
        cfg = load_cfg()
        if name in pipeline.PROVIDERS:
            keys = pipeline.provider_keys(cfg, name)
            return keys[idx] if isinstance(idx, int) and 0 <= idx < len(keys) else ""
        key = {"fastgen": "fastgen_api_key", "nonstop": "veononstop_api_key", "yougen": "yougen_api_key", "royal": "royaltechno_api_key",
               "slider": "secretslider_api_key", "google": "google_api_key", "openai": "openai_api_key", "anthropic": "anthropic_api_key", "gemini": "gemini_api_key",
               "assemblyai": "assemblyai_api_key", "voicegen": "voicegen_api_key", "voicer": "voicer_api_key", "lumean": "lumean_api_key"}.get(name)
        return cfg.get(key, "") if key else ""

    def tts_voices(self, service):
        cfg = load_cfg()
        key = cfg.get(f"{service}_api_key")
        if service in ("fastgen", "royal") and not key:
            key = (pipeline.provider_keys(cfg, service) or [""])[0]
        if not key and service != "edge":
            return {"ok": False, "error": "Сначала введите ключ."}
        try:
            from tts_api import make_tts
            vs = make_tts(service, key).voices()
            return {"ok": True, "voices": vs[:200]}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def cancel(self):
        global QUEUE_STOP
        QUEUE_STOP = True
        pipeline.CANCEL.set()
        with LOCK:
            STATE["message"] = "останавливаю… (дожидаюсь текущих запросов)"
        return True

    def open_result(self):
        with LOCK:
            res = STATE.get("result")
        if res and os.path.exists(res):
            os.startfile(res)
            return True
        return False

    def open_output(self):
        out = os.path.join(data_dir(), "output")
        os.makedirs(out, exist_ok=True)
        os.startfile(out)
        return True

    def open_url(self, url):
        if url and url.startswith("http"):
            os.startfile(url)
        return True

    def open_help(self):
        """Инструкция из папки программы — в браузере."""
        p = os.path.join(RES_DIR, "docs", "instruction.html")
        if not os.path.exists(p):
            p = os.path.join(app_dir(), "docs", "instruction.html")
        if os.path.exists(p):
            os.startfile(p)
            return True
        return False

    def check_update(self):
        u = check_update()
        if u:
            with LOCK:
                u["phase"] = (STATE.get("update") or {}).get("phase")
        return u

    def recent_jobs(self, limit=15):
        """«Недавние ролики»: папки заданий с кадрами, свежие сверху, с прогрессом и тем, чего не хватает для продолжения."""
        root = os.path.join(data_dir(), "jobs")
        if not os.path.isdir(root):
            return []
        with LOCK:
            running_dir = STATE.get("job_dir") if (STATE["running"] or STATE.get("queue_active")) else None
        out = []
        for n in os.listdir(root):
            d = os.path.join(root, n)
            if not os.path.isdir(d):
                continue
            rec = pipeline.read_json(os.path.join(d, "job.json"), {}) or {}
            scenes = pipeline.read_json(os.path.join(d, "scenes.json"), []) or []
            if not rec and not scenes:
                continue  # служебная папка (распознавание, озвучка) — кадров в ней нет
            base, _, prof = n.partition("@")
            img_dir, vid_dir = os.path.join(d, "images"), os.path.join(d, "videos")
            imgs = sum(1 for f in os.listdir(img_dir) if f.endswith(".png")) if os.path.isdir(img_dir) else 0
            vids = sum(1 for f in os.listdir(vid_dir) if f.endswith(".mp4")) if os.path.isdir(vid_dir) else 0
            try:
                updated = max([os.path.getmtime(d)] + [os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)])
            except OSError:
                updated = 0
            args = rec.get("args") or {}
            title = rec.get("title")
            if not title:
                st = self._job_script(d, base)
                title = " ".join(st.split()[:7]) if st else base
            out.append({"id": n, "title": title[:80], "profile": (args.get("profile") if rec else prof) or prof or "",
                        "scenes": len(scenes), "images": imgs, "videos": vids, "updated": int(updated),
                        "status": "running" if running_dir and os.path.normcase(running_dir) == os.path.normcase(d) else rec.get("status", ""),
                        "result": rec.get("result"), "known": bool(rec)})
        out.sort(key=lambda x: -x["updated"])
        return out[:limit]

    @staticmethod
    def _job_script(d, base):
        for p in (os.path.join(d, "script.txt"), os.path.join(os.path.dirname(d), base, "script.txt")):
            try:
                with open(p, encoding="utf-8") as f:
                    t = f.read().strip()
                if t:
                    return t
            except OSError:
                pass
        return ""

    def continue_job(self, job_id, audio=None):
        """«Продолжить»: тот же ролик с теми же настройками; всё готовое возьмётся из задания.
        Нужен файл озвучки, а он неизвестен (ролик из старой версии или файл переехал) → need_audio."""
        global QUEUE_STOP
        name = os.path.basename(str(job_id or ""))
        root = os.path.join(data_dir(), "jobs")
        d = os.path.join(root, name)
        if not name or not os.path.isdir(d):
            return {"ok": False, "error": "Папка этого ролика не найдена — возможно, заготовки удалены."}
        with LOCK:
            if STATE["running"] or STATE["preparing"] or STATE.get("queue_active"):
                return {"ok": False, "error": "Сейчас делается другой ролик. Продолжите этот, когда он закончится."}
        base, _, prof = name.partition("@")
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) or {}
        cfg = load_cfg()
        if rec.get("args"):
            args = dict(rec["args"])
            args["script"] = rec.get("script")
            if args.get("rewrite"):  # рерайт уже сделан — продолжаем по готовому тексту, заново не переписываем
                args["rewrite"], args["script"] = None, args["script"] or self._job_script(d, base)
            cfg.update({k: v for k, v in (rec.get("cfg") or {}).items() if v is not None})
        else:  # ролик из версии без job.json: настройки — текущие, профиль — из имени папки, сценарий — из script.txt
            args = dict(audio=None, provider=pipeline.normalize_providers(cfg.get("image_providers")), mode=cfg.get("mode", "images"),
                        style=style_key(cfg.get("style"), pipeline.DEFAULT_STYLE), use_refs=cfg.get("quality") == "quality",
                        subtitles=bool(cfg.get("subtitles")), motion=bool(cfg.get("motion")), script=self._job_script(d, base) or None,
                        rewrite=None, tts=cfg.get("tts_service") or "voicegen", voice=cfg.get("tts_voice", ""), video_first=_vf(cfg.get("video_first")),
                        hd=bool(cfg.get("hd_images")), profile=prof, voice_file=None)
        if audio:
            audio = str(audio).strip().strip('"')
            got = pipeline.short_base(os.path.splitext(os.path.basename(audio))[0])
            if not base.startswith("script_") and got != base:
                return {"ok": False, "error": f"У этого файла другое имя: задание ролика лежит в папке «{base}», а файл «{os.path.basename(audio)}» "
                                              f"начнёт новую папку «{got}». Выберите тот же файл озвучки, что и в прошлый раз."}
            if args.get("script"):
                args["voice_file"] = audio
            else:
                args["audio"] = audio
        if base.startswith("script_"):  # озвучку делала программа — она лежит в задании и возьмётся сама
            args["voice_file"] = None
        elif args.get("script"):
            if not (args.get("voice_file") and os.path.isfile(args["voice_file"])):
                return {"ok": False, "need_audio": True, "expect": base}
        elif not (args.get("audio") and os.path.isfile(args["audio"])):
            return {"ok": False, "need_audio": True, "expect": base}
        cfg["profile"] = args.get("profile") or ""
        save_cfg(cfg)
        with LOCK:
            STATE.update({"running": True, "stage": "start", "done": 0, "total": 0, "message": "продолжаю", "log": [], "result": None,
                          "error": None, "cancelled": False, "started_at": time.time(), "scenes": 0, "queue": None,
                          "title": rec.get("title") or base})
        QUEUE_STOP = False
        threading.Thread(target=job_chain, args=(args,), daemon=True).start()
        return {"ok": True, "mode": args.get("mode", "images")}

    def storage_info(self):
        out, jobs = os.path.join(data_dir(), "output"), os.path.join(data_dir(), "jobs")
        mb = lambda p: round(pipeline.dir_size(p) / 1048576, 1) if os.path.isdir(p) else 0
        return {"output_mb": mb(out), "jobs_mb": mb(jobs)}

    def clear_jobs(self):
        """Удалить заготовки (картинки, озвучку, клипы) всех заданий. Готовые видео в output не трогаем."""
        with LOCK:
            if STATE["running"] or STATE["preparing"]:
                return {"ok": False, "error": "Сначала дождитесь окончания или нажмите «Стоп»."}
        jobs = os.path.join(data_dir(), "jobs")
        before = pipeline.dir_size(jobs) if os.path.isdir(jobs) else 0
        if os.path.isdir(jobs):
            for n in os.listdir(jobs):
                shutil.rmtree(os.path.join(jobs, n), ignore_errors=True)
        after = pipeline.dir_size(jobs) if os.path.isdir(jobs) else 0
        return {"ok": True, "freed_mb": round((before - after) / 1048576)}

    def install_update(self, url, version):
        """Автообновление: только если программа лежит в папке, куда можно писать."""
        if data_dir() != app_dir():
            return {"ok": False, "error": "noport"}
        with LOCK:
            if STATE["running"] or STATE["preparing"]:
                return {"ok": False, "error": "busy"}
            STATE["update"] = {"phase": "download", "done": 0, "total": 0, "version": version, "error": None}
        threading.Thread(target=run_update, args=(url, version), daemon=True).start()
        return {"ok": True}

    def update_state(self):
        with LOCK:
            return STATE.get("update")

    def estimate(self, opts):
        try:
            return estimate_plan(opts)
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def report(self):
        text, path = build_report()
        try:
            copied = set_clipboard(text)
        except Exception:
            copied = False
        return {"ok": True, "copied": copied, "path": path}

    def open_report(self):
        p = os.path.join(data_dir(), "report.txt")
        if os.path.exists(p):
            subprocess.Popen(["explorer", "/select,", p])
            return True
        return False


def on_drop(event):
    files = event.get("dataTransfer", {}).get("files", [])
    for f in files:
        path = f.get("pywebviewFullPath")
        if not path:
            continue
        if path.lower().endswith((".txt", ".md")):  # текст сценария — в поле сценария
            try:
                raw = open(path, "rb").read()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("cp1251", errors="replace")
                WINDOW.evaluate_js(f"window.setScript({json.dumps(text)})")
            except Exception:
                pass
        else:
            WINDOW.evaluate_js(f"window.setAudio({json.dumps(path)})")
        break


def on_loaded():
    try:
        zone = WINDOW.dom.get_element("#dropzone")
        if zone:
            zone.events.drop += on_drop
    except Exception:
        traceback.print_exc()


def on_closing():
    with LOCK:
        running = STATE["running"]
        upd = dict(STATE.get("update") or {})
    if running:
        ok = WINDOW.create_confirm_dialog("Kadro.Gen", "Видео ещё создаётся. Закрыть программу? Прогресс сохранится — "
                                                      "при следующем запуске того же mp3 работа продолжится.")
        if not ok:
            return False
        pipeline.CANCEL.set()
    if upd.get("phase") == "ready" and upd.get("src") and data_dir() == app_dir():
        try:
            launch_helper(upd["src"])  # скачанное заранее обновление встанет, пока программа закрыта
        except Exception:
            traceback.print_exc()
    return True


def single_instance():
    """Вторая копия программы не открывается: одна общая config.json на два окна — путаница в настройках.
    Если Kadro.Gen уже запущен, показываем его окно и выходим."""
    import ctypes
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    global _MUTEX
    _MUTEX = k32.CreateMutexW(None, False, "KadroGen.SingleInstance")
    if k32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return True
    hwnd = u32.FindWindowW(None, "Kadro.Gen")
    if hwnd:
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
    return False


_MUTEX = None


def main():
    global WINDOW
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--apply-update" in sys.argv:
        i = sys.argv.index("--apply-update")
        apply_update(sys.argv[i + 1], sys.argv[i + 2] if len(sys.argv) > i + 2 else 0)
        return
    if not os.path.exists(CONFIG_PATH):
        save_cfg({})
    if len(sys.argv) > 1 and sys.argv[1] == "-m":
        return  # кто-то запустил exe как «python -m …» — это не наш сценарий, окно не открываем
    if "--engine" in sys.argv:  # «Контент завод» делает видео без окна (engine.py); мьютекс окна не нужен
        import engine
        try:
            engine.run(sys.argv[sys.argv.index("--engine") + 1])
        except BaseException:
            pass  # ошибка уже записана в файл статуса и журнал
        return
    if "--run" not in sys.argv and not single_instance():
        return
    cleanup_old_files()
    queue_load()
    threading.Thread(target=pipeline.prune_old_jobs, daemon=True).start()

    def auto_update():
        if data_dir() != app_dir() or not load_cfg().get("auto_update", True):
            return
        u = check_update()
        if u:
            prepare_update(u)
    threading.Thread(target=auto_update, daemon=True).start()
    if "--run" in sys.argv:
        def arg(name, default=None):
            return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default
        try:
            script = None
            if arg("--script"):
                with open(arg("--script"), encoding="utf-8-sig") as f:
                    script = f.read()
            out = pipeline.make_video(arg("--run"), provider=arg("--provider"), mode=arg("--mode", "images"),
                                      style_key=arg("--style"), use_refs="--refs" in sys.argv, subtitles="--subs" in sys.argv,
                                      profile=arg("--profile"), script_text=script, voice_file=arg("--run") if script else None)
            pipeline.log(f"RUN OK: {out}")
        except BaseException as e:
            pipeline.log(f"RUN FAILED: {type(e).__name__}: {e}")
            raise
        return
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    WINDOW = webview.create_window(
        "Kadro.Gen", HTML_PATH, js_api=Api(),
        width=1280, height=820, min_size=(900, 640), text_select=True,
        background_color="#f3f5f8" if load_cfg().get("theme", "dark") == "light" else "#0a0d12", maximized=True,
    )
    WINDOW.events.loaded += on_loaded
    WINDOW.events.closing += on_closing
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
