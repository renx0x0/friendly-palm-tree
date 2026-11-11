"""VideoGen: обычное окно Windows (pywebview).
Экраны: 1) чем делать картинки, 2) режим и стиль, 3) ключи + озвучка, 4) расчёт и подтверждение, 5) прогресс.

Запуск: python app.py   (после сборки — VideoGen.exe)
Скрытый режим без окна: VideoGen.exe --run файл.mp3 [--provider ...] [--mode images|video] [--style ...] [--refs]
"""
import json
import math
import os
import platform
import re
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
    if "--run" in sys.argv or "--assemble" in sys.argv:
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

VERSION = "1.7.1-p85"
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
        elif x.get("status") == "assembling":  # монтаж шёл в фоне, когда программу закрыли — смотрим, чем кончился
            rec = pipeline.read_json(os.path.join(x.get("job_dir") or "", "job.json"), {}) or {}
            if rec.get("status") == "done" and rec.get("result"):
                x["status"], x["result"] = "done", rec["result"]
            elif rec.get("status") != "assembling":
                x["status"], x["error"] = "error", rec.get("error") or "монтаж прервался — доделайте ролик кнопкой «Продолжить»"


def queue_save():
    try:
        with open(queue_path() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(QUEUE, f, ensure_ascii=False, indent=1)
        os.replace(queue_path() + ".tmp", queue_path())
    except Exception:
        traceback.print_exc()


def queue_public():
    return [{k: x.get(k) for k in ("id", "title", "name", "source", "voice", "style", "status", "result", "error", "profile", "out_dir", "prompts_file", "out_sub")} for x in QUEUE]


def style_key(v, default=None):
    """Ключ стиля из интерфейса: один из STYLES или «свой»; иначе default."""
    return v if v in pipeline.STYLES or v == pipeline.CUSTOM_STYLE else default


def _video_fit(cfg, data):
    """Увеличение и прижим оживлённых клипов (убрать водяной знак Veo) — из окна в настройки."""
    if "video_zoom_pct" in data:
        try:
            cfg["video_zoom_pct"] = max(100, min(130, int(data.get("video_zoom_pct"))))
        except (TypeError, ValueError):
            pass
    if data.get("video_anchor") in ("top", "center", "bottom"):
        cfg["video_anchor"] = data["video_anchor"]


def job_title(opts):
    if pipeline.safe_title(opts.get("name")):
        return pipeline.safe_title(opts.get("name"))[:80]
    rw = opts.get("rewrite") or {}
    if rw.get("on"):
        return (rw.get("url") or " ".join((rw.get("text") or "").split()[:7]) or "рерайт")[:80]
    if opts.get("source") == "script":
        return " ".join((opts.get("script") or "").split()[:7]).lstrip("—–-:;,. ")[:80] or "сценарий"
    return os.path.basename((opts.get("audio") or "").strip().strip('"')) or "озвучка"


def run_queue(common):
    """Задания по очереди, одно за другим. Общие настройки у всех одни, у каждого задания свои текст/файл и голос."""
    global QUEUE_STOP, QRUN_ALL
    QRUN_ALL = True
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
            if item.get("args"):  # «Новая озвучка» из очереди — аргументы уже готовы
                with LOCK:
                    STATE["title"] = item.get("title") or "ролик"
                run_job(**item["args"])
                if item_after_run(item):
                    break
                continue
            if item.get("opts"):  # поставлено во время работы — со своими настройками
                err, args, _ = Api()._job(item["opts"])
                if err:
                    with LOCK:
                        item["status"], item["error"] = "error", err
                    queue_save()
                    continue
                run_job(**args)
                if item_after_run(item):
                    break
                continue
            rw = item.get("rewrite") if item.get("source") == "rewrite" else None
            script = item.get("script") if item.get("source") == "script" else None
            audio = item.get("audio") if item.get("source") == "mp3" else None
            # стиль у задания свой, если включён «Разный стиль» и выбран; иначе общий
            run_job(audio, common["provider"], common["mode"], style_key(item.get("style"), common["style"]), common["use_refs"], common["subtitles"], common["motion"],
                    script, rw, common["tts"], (item.get("voice") or "").strip(), common["video_first"], common["hd"],
                    voice_file=(item.get("voice_file") or None) if script else None, out_name=item.get("name") or None,
                    profile=item.get("profile") if "profile" in item else None,
                    prompts_file=item.get("prompts_file") or None, out_dir=item.get("out_dir") or None,
                    out_sub=item.get("out_sub") if "out_sub" in item else None)
            if item_after_run(item):
                break
    finally:
        QRUN_ALL = False
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
PREVIEW_LOCK = threading.Lock()
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
        line = f"{time.strftime('%H:%M:%S')}  {message}" + (f"  {done}/{total}" if total else "")
        key = stage + re.sub(r"\d+", "#", str(message))  # та же строка, поменялись только числа («склейка: 41%») — обновляем, а не копим
        if STATE["log"] and STATE.get("_logkey") == key:
            STATE["log"][-1] = line
        else:
            STATE["log"].append(line)
        STATE["_logkey"] = key
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


JOB_CFG_KEYS = ("quality", "qa_images", "speed_copies", "custom_style", "video_prompt", "fg_image_model", "fg_video_model", "profile_manual", "video_zoom_pct", "video_anchor",
                "sub_style", "sub_source", "transitions", "video_quality", "output_dir", "fx", "stock", "heroes", "tts_service", "tts_voice", "style", "image_providers")


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
            profile=None, voice_file=None, prompts_file=None, remix_from=None, look=None, out_name=None, out_dir=None, out_sub=None):
    """Один ролик. Сбой сервиса — пауза и повтор (всё готовое берётся из задания); ролик собран, но часть картинок
    подменена соседними или сцены не оживились — пауза и доводка только их. Выключить: "auto_retry": false в config.json."""
    LAST_RUN.clear()
    LAST_RUN.update(dict(audio=audio, provider=provider, mode=mode, style=style, use_refs=use_refs, subtitles=subtitles, motion=motion,
                         script=script, rewrite=rewrite, tts=tts, voice=voice, video_first=video_first, hd=hd, profile=profile, voice_file=voice_file,
                         prompts_file=prompts_file, remix_from=remix_from, look=look, out_name=out_name, out_dir=out_dir, out_sub=out_sub))
    keep_awake(True)
    tries = fixes = 0
    with LOCK:
        STATE["deferred"] = False
    try:
        apply_engine(provider)
        pipeline.LAST_JOB_DIR = None  # кадры прошлого проекта не должны мелькать в окне нового ролика
        d = pipeline.run_job_dir(load_cfg(), audio, script, voice_file, rewrite, profile, prompts_file, remix_from)
        with LOCK:
            STATE["job_dir"] = d
        job_record(d, "running")
        auto = load_cfg().get("auto_retry", True)
        while True:
            err = None
            try:
                out = pipeline.make_video(audio, progress_cb=progress_cb, provider=provider, mode=mode, style_key=style,
                                          use_refs=use_refs, subtitles=subtitles, motion=motion, script_text=script, rewrite=rewrite,
                                          tts_service=tts, tts_voice=voice, video_first=video_first, hd=hd, profile=profile, voice_file=voice_file,
                                          prompts_file=prompts_file, remix_from=remix_from, look=look, assembly_hook=assembly_hook,
                                          out_name=out_name, out_dir=out_dir or None, out_sub=out_sub)
            except pipeline.Retryable as e:  # сервис не отдаёт картинки, часть не написалась — стоит повторить позже
                err = str(e)
            except (pipeline.Cancelled, SystemExit):  # «Стоп» или ошибка настроек (нет ключа и т.п.) — повтор не поможет
                raise
            except Exception as e:  # сбой сети или сервиса
                err = f"{type(e).__name__}: {e}"
                with LOCK:
                    STATE["log"].append(traceback.format_exc()[-1500:])
            if err is None:
                if pipeline.ASSEMBLY_DEFERRED:  # картинки и видео готовы, монтаж идёт в фоне — очередь идёт дальше
                    with LOCK:
                        STATE["deferred"] = True
                        STATE["job_dir"] = pipeline.LAST_JOB_DIR or STATE.get("job_dir")
                    break
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
            deferred = STATE.get("deferred") and not err and not stopped
        if deferred:
            with ASM_LOCK:
                e = ASM_JOBS.get(os.path.normcase(jd or ""))
                final = e and e.get("state") in ("done", "error", "stopped")
            if not final:  # если монтаж уже успел кончиться, job.json записал он сам
                job_record(jd, "assembling")
        else:
            job_record(jd or pipeline.LAST_JOB_DIR, "done" if res and not err else ("stopped" if stopped else "error"), res, err)
        keep_awake(False)


# ===== фоновый монтаж: следующий ролик очереди генерируется, пока предыдущий монтируется =====
ASM_LOCK = threading.Lock()
ASSEMBLY = {}    # текущий фоновый монтаж: proc, job_dir, title, out, started
ASM_JOBS = {}    # папка задания (normcase) → {"state": running|done|error|stopped, "result", "error", "item", "title"}
ASM_DONE = []    # недавно закончившиеся — окно показывает уведомление
QRUN_ALL = False  # очередь запущена «Начать» (берёт все задания), а не дозапуском во время работы


def assembly_busy():
    with ASM_LOCK:
        p = ASSEMBLY.get("proc")
        return p is not None and p.poll() is None


def _queue_has_next():
    with LOCK:
        if QUEUE_STOP or STATE.get("cancelled"):
            return False
        return any(x.get("status") == "queued" and (x.get("opts") or x.get("args") or QRUN_ALL) for x in QUEUE)


def assembly_hook(spec_path, out_mp4, can_defer):
    """Вызывается из make_video перед монтажом. Сначала ждём фоновый монтаж прошлого ролика (два сразу комп не тянет).
    → True — монтаж отдан в фон (в очереди есть следующий ролик), False — монтировать здесь же, как обычно."""
    if assembly_busy():
        with ASM_LOCK:
            t = ASSEMBLY.get("title") or ""
        progress_cb("render", 0, 0, f"жду, пока смонтируется предыдущий ролик «{t}» — потом смонтирую этот")
        while assembly_busy():
            pipeline.check_cancel()
            time.sleep(1)
    if not (can_defer and load_cfg().get("parallel_assembly", True) and _queue_has_next()):
        return False
    try:
        start_assembly(spec_path, out_mp4)
    except Exception as e:  # не запустился отдельный процесс — монтируем по-старому
        pipeline.log(f"фоновый монтаж не запустился ({e}) — монтирую сразу")
        return False
    return True


def start_assembly(spec_path, out_mp4):
    d = os.path.dirname(spec_path)
    try:
        os.remove(os.path.join(d, pipeline.ASSEMBLE_STATUS))
    except OSError:
        pass
    cmd = [sys.executable] + ([] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]) + ["--assemble", spec_path]
    proc = subprocess.Popen(cmd, cwd=app_dir(), creationflags=0x08000000 if os.name == "nt" else 0,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with LOCK:
        title = STATE.get("title") or (STATE.get("queue") or {}).get("title") or os.path.basename(out_mp4)
    with ASM_LOCK:
        ASSEMBLY.clear()
        ASSEMBLY.update({"proc": proc, "job_dir": d, "title": title, "out": out_mp4, "started": time.time()})
        ASM_JOBS[os.path.normcase(d)] = {"state": "running", "title": title, "item": None}
    pipeline.log(f"монтаж «{title}» идёт в фоне (процесс {proc.pid}), берусь за следующий ролик")
    threading.Thread(target=_watch_assembly, args=(proc, d, title, out_mp4), daemon=True).start()


def _watch_assembly(proc, d, title, out_mp4):
    proc.wait()
    st = pipeline.read_json(os.path.join(d, pipeline.ASSEMBLE_STATUS), {}) or {}
    with ASM_LOCK:
        e = ASM_JOBS.setdefault(os.path.normcase(d), {})
        if e.get("state") == "stopped":
            state, err = "stopped", "монтаж остановлен"
        elif st.get("state") == "done" and os.path.exists(out_mp4):
            state, err = "done", None
        else:
            state, err = "error", st.get("error") or f"процесс монтажа завершился с кодом {proc.returncode}"
        e.update({"state": state, "result": out_mp4 if state == "done" else None, "error": err})
        item = e.get("item")
        ASM_DONE.append({"title": title, "state": state, "result": e["result"], "error": err, "at": time.time()})
        del ASM_DONE[:-5]
    if state != "done":
        if st.get("state") not in ("done", "error"):  # процесс убит — файл хода монтажа он поправить не успел
            st.update({"state": state, "error": err, "updated": time.time()})
            pipeline.write_json(os.path.join(d, pipeline.ASSEMBLE_STATUS), st)
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) or {}
        if rec.get("status") == "assembling":
            rec.update({"status": "stopped" if state == "stopped" else "error", "error": err, "updated": time.time()})
            pipeline.write_json(os.path.join(d, "job.json"), rec)
    pipeline.log(f"фоновый монтаж «{title}»: " + ({"done": f"готово — {out_mp4}", "stopped": "остановлен"}.get(state) or f"ошибка — {err}"))
    if item is not None:
        with LOCK:
            item["status"] = state
            if state == "done":
                item["result"] = out_mp4
            else:
                item["error"] = err
        queue_save()


def item_after_run(item):
    """Статус задания очереди после run_job. Монтаж ушёл в фон — «монтируется», итог допишет _watch_assembly."""
    with LOCK:
        if STATE.get("cancelled"):
            item["status"] = "stopped"
        elif STATE.get("error"):
            item["status"], item["error"] = "error", STATE["error"]
        elif STATE.get("deferred"):
            jd = STATE.get("job_dir") or ""
            item["job_dir"] = jd
            with ASM_LOCK:
                e = ASM_JOBS.get(os.path.normcase(jd))
                if e and e.get("state") in ("done", "error", "stopped"):
                    item["status"] = e["state"]
                    item["result"], item["error"] = e.get("result"), e.get("error")
                else:
                    item["status"] = "assembling"
                    if e is not None:
                        e["item"] = item
        else:
            item["status"], item["result"] = "done", STATE.get("result")
        stopped = STATE.get("cancelled")
    queue_save()
    return stopped


def assembly_state():
    """Для окна: что монтируется в фоне и что недавно домонтировалось."""
    with ASM_LOCK:
        p, d = ASSEMBLY.get("proc"), ASSEMBLY.get("job_dir")
        cur = None
        if p is not None and p.poll() is None and d:
            st = pipeline.read_json(os.path.join(d, pipeline.ASSEMBLE_STATUS), {}) or {}
            cur = {"title": ASSEMBLY.get("title"), "message": st.get("message") or "монтаж", "stage": st.get("stage"),
                   "done": st.get("done") or 0, "total": st.get("total") or 0, "since": int(time.time() - ASSEMBLY.get("started", time.time())),
                   "job_id": os.path.basename(d)}
        done = [dict(x) for x in ASM_DONE]
    return cur, done


def job_chain(args):
    """Ролик, а после него — задания, поставленные в очередь во время работы (у каждого свои настройки)."""
    api, item, chained = Api(), None, False
    try:
        while args:
            run_job(**args)
            if item is not None:
                item_after_run(item)
            args, item = None, None
            while True:
                with LOCK:
                    if STATE.get("cancelled") or QUEUE_STOP:
                        break
                    nxt = next((x for x in QUEUE if x.get("status") == "queued" and (x.get("opts") or x.get("args"))), None)
                    if nxt is None:
                        break
                    nxt["status"] = "running"
                if nxt.get("args"):  # «Новая озвучка», поставленная в очередь, — аргументы уже готовы
                    err, a, title = None, dict(nxt["args"]), nxt.get("title") or "ролик"
                else:
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
        st["assembly"], st["asm_done"] = assembly_state()
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
            "theme": cfg.get("theme", "dark"), "accent": cfg.get("accent", "blue"), "hw_encode": bool(cfg.get("hw_encode", False)),
            "music": cfg.get("music_volume", 50),
            "tracks": music_tracks(),
            "providers": [p for p in (cfg.get("image_providers") or []) if p in pipeline.PROVIDERS],
            "keys": {p: [mask(k) for k in pipeline.provider_keys(cfg, p)] for p in pipeline.PROVIDERS},
            "lang": cfg.get("lang", "ru"),
            "mode": cfg.get("mode", "images"),
            "style": style_key(cfg.get("style"), pipeline.DEFAULT_STYLE),
            "custom_style": cfg.get("custom_style", ""), "video_prompt": cfg.get("video_prompt", ""),
            "fg_models": pipeline.fastgen_models(), "fg_image_model": cfg.get("fg_image_model", ""), "fg_video_model": cfg.get("fg_video_model", ""),
            "profile_manual": bool(cfg.get("profile_manual", False)), "video_zoom_pct": cfg.get("video_zoom_pct", 104), "video_anchor": cfg.get("video_anchor", "top"),
            "video_quality": cfg.get("video_quality") if cfg.get("video_quality") in pipeline.VIDEO_QUALITIES else "high",
            "output_dir": cfg.get("output_dir") or "", "default_output": os.path.join(data_dir(), "output"),
            "out_sub": bool(cfg.get("out_sub", True)),
            "fx": pipeline.fx_settings(cfg),
            "stock": pipeline.stock.settings(cfg), "stock_vision": pipeline.vision_ready(cfg), "stock_keys": {k: bool(v) for k, v in pipeline.stock.keys_of(cfg).items()},
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
                           ("voicegen", "voicegen_api_key"), ("voicer", "voicer_api_key"), ("lumean", "lumean_api_key"),
                           ("pexels", "pexels_api_key"), ("pixabay", "pixabay_api_key")):
            if field in data:
                v = (data.get(field) or "").strip()
                if v and "•" not in v:
                    cfg[key] = v
                elif v == "":
                    cfg[key] = ""
                if field in pipeline.SINGLE_KEY and "•" not in v:
                    cfg.setdefault("image_keys", {})[field] = [v] if v else []
        if "out_sub" in data:  # «в подпапку с названием ролика» — запоминается, по умолчанию включено
            cfg["out_sub"] = bool(data["out_sub"])
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
        if "profile_manual" in data:
            cfg["profile_manual"] = bool(data.get("profile_manual"))
        _video_fit(cfg, data)
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
        if "hw_encode" in data:
            cfg["hw_encode"] = bool(data["hw_encode"])
        if data.get("accent") in ("blue", "sage", "lilac", "sand", "steel"):
            cfg["accent"] = data["accent"]
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
        if isinstance(data.get("stock"), dict):
            cfg["stock"] = pipeline.stock.settings({"stock": data["stock"]})
        if isinstance(data.get("fx"), dict):
            cfg["fx"] = pipeline.fx_settings({"fx": data["fx"]})
        if data.get("video_quality") in ("standard", "high", "max"):
            cfg["video_quality"] = data["video_quality"]
        if "parallel_assembly" in data:
            cfg["parallel_assembly"] = bool(data["parallel_assembly"])
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
        pf = (opts.get("prompts_file") or "").strip().strip('"')
        if pf and not os.path.isfile(pf):
            return "Файл готовых промптов не найден — выберите его заново."
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
        prompts_file = (opts.get("prompts_file") or "").strip().strip('"') or None
        if style == pipeline.CUSTOM_STYLE and not custom_style and not profile and not prompts_file:  # с профилем/готовыми промптами стиль не нужен
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
        if "profile_manual" in opts:
            cfg["profile_manual"] = bool(opts.get("profile_manual"))
        _video_fit(cfg, opts)
        if voice:
            rec = [x for x in cfg.get("tts_recent", []) if x.get("id") != voice]
            rec.insert(0, {"id": voice, "service": tts})
            cfg["tts_recent"] = rec[:12]
        save_cfg(cfg)
        args = dict(audio=audio, provider=provider, mode=mode, style=style, use_refs=use_refs, subtitles=subtitles, motion=motion,
                    script=script, rewrite=rw, tts=tts, voice=voice, video_first=video_first, hd=hd,
                    profile=profile, voice_file=voice_file, prompts_file=prompts_file,
                    out_name=pipeline.safe_title(opts.get("name")) or None,
                    out_dir=str(opts.get("out_dir") or "").strip().strip('"') or None,
                    out_sub=bool(opts["out_sub"]) if "out_sub" in opts else None)
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
        entry = {"id": uuid.uuid4().hex[:8], "title": title[:80], "name": pipeline.safe_title(o.get("name")) or None, "source": src,
                 "script": (o.get("script") or "").strip() or None,
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
                "rewrite": {**(item.get("rewrite") or {}), "on": src == "rewrite"}, "tts": item.get("tts"),
                "voice_file": (item.get("voice_file") or "").strip().strip('"') if src == "script" else ""}
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
        name = pipeline.safe_title(item.get("name"))
        extra = {}
        if "profile" in item:
            extra["profile"] = str(item.get("profile") or "")
        pf = str(item.get("prompts_file") or "").strip().strip('"')
        if pf:
            extra["prompts_file"] = pf
        od = str(item.get("out_dir") or "").strip().strip('"')
        if od:
            extra["out_dir"] = od
        if "out_sub" in item:
            extra["out_sub"] = bool(item.get("out_sub"))
        entry = {"id": uuid.uuid4().hex[:8], "title": (name or title)[:80], "name": name or None, **extra, "source": src,
                 "script": (item.get("script") or "").strip() or None,
                 "audio": (item.get("audio") or "").strip().strip('"') or None,
                 "rewrite": self._rewrite_opts(opts) if src == "rewrite" else None,
                 "voice_file": opts["voice_file"] or None,
                 "voice": (item.get("voice") or "").strip(), "style": style_key(item.get("style"), ""), "status": "queued", "result": None, "error": None}
        with LOCK:
            QUEUE.append(entry)
        queue_save()
        return {"ok": True, "items": queue_public()}

    def queue_set_name(self, qid, name):
        """Название ролика в очереди — так будет назван готовый файл."""
        name = pipeline.safe_title(name)
        with LOCK:
            for x in QUEUE:
                if x["id"] == qid and x.get("status") in ("queued", "error", "stopped"):
                    x["name"] = name or None
                    if x.get("opts"):
                        x["opts"]["name"] = name
                    if name:
                        x["title"] = name[:80]
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

    def find_prompts(self, audio):
        """Готовые промпты рядом с озвучкой (<имя>.prompts.txt или <имя>.txt) — подставляются сами при выборе mp3."""
        p = pipeline.find_prompts_file(str(audio or "").strip().strip('"'))
        return self.prompts_info(p, audio) if p else {"ok": False}

    def pick_output_dir(self):
        """Своя папка для готовых видео. → путь или ""."""
        res = WINDOW.create_file_dialog(getattr(webview, "FOLDER_DIALOG", 20))
        if not res:
            return ""
        p = res[0] if isinstance(res, (list, tuple)) else str(res)
        cfg = load_cfg()
        cfg["output_dir"] = p
        save_cfg(cfg)
        return p

    def clear_output_dir(self):
        cfg = load_cfg()
        cfg["output_dir"] = ""
        save_cfg(cfg)
        return True

    def pick_prompts(self):
        res = WINDOW.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=("Промпты (*.txt)", "Все файлы (*.*)"))
        if not res:
            return ""
        return res[0] if isinstance(res, (list, tuple)) else str(res)

    def prompts_info(self, path, audio=None):
        """Сводка до запуска: блоки, промпты, секунды на кадр, сколько клипа Veo уйдёт в обрезку, замечания."""
        path = str(path or "").strip().strip('"')
        try:
            blocks = pipeline.read_prompts_file(path)
        except (OSError, UnicodeDecodeError) as e:
            return {"ok": False, "error": f"Не открылся файл: {e}"}
        if not blocks:
            return {"ok": False, "error": "В файле нет пронумерованных промптов («001. …»)."}
        dur = None
        audio = str(audio or "").strip().strip('"')
        if audio and os.path.isfile(audio):
            try:
                dur = pipeline.audio_duration(audio)
            except Exception:
                dur = None
        return {"ok": True, "path": path, "name": os.path.basename(path), **pipeline.profiles.blocks_summary(blocks, dur)}

    def profile_request(self, n):
        """«Скопировать запрос для чата»: мастер-промпт с этой частью — в буфер обмена (и в окно, если буфер недоступен)."""
        try:
            text = pipeline.profiles.BOARD.request_text(int(n))
        except (TypeError, ValueError):
            text = None
        if not text:
            return {"ok": False, "error": "Нет ролика с профилем: запустите ролик — части появятся в этой панели."}
        copied = False
        try:
            copied = bool(set_clipboard(text))
        except Exception:
            pass
        return {"ok": True, "copied": copied, "text": text, "chars": len(text)}

    def profile_paste(self, n, text, dry=False):
        """«Вставить ответ»: свои промпты из чата для части n (dry — только проверить, не сохраняя)."""
        try:
            return pipeline.profiles.BOARD.paste(int(n), str(text or ""), bool(dry))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def profile_part(self, n):
        """Промпты одной части — по клику на часть в панели."""
        try:
            return pipeline.profiles.BOARD.part(int(n))
        except (TypeError, ValueError):
            return {"n": n, "prompts": [], "problems": [], "status": "wait", "live": ""}

    def _stock_clear(self, d):
        def clear(i):  # старые файлы сцены и клипы из неё — пересоберутся
            for rel in [f"images/{i:04d}.png", f"videos/{i:04d}.mp4"]:
                try:
                    os.remove(os.path.join(d, rel))
                except OSError:
                    pass
            pipeline.remove_scene_clips(d, i)
        return clear

    @staticmethod
    def _topic(scenes):
        return " ".join(str(s.get("text") or "") for s in scenes[:12])[:400]

    def stock_replace(self, job_id, i, query=None):
        """Просмотр кадра: «Другой со стока» / «Свой запрос…» для стоковой сцены."""
        d = self._frame_dir(job_id)
        if not d:
            return {"ok": False, "error": "Папка ролика не найдена."}
        if self._running_dir() and os.path.normcase(self._running_dir()) == os.path.normcase(d):
            return {"ok": False, "error": "Ролик сейчас делается — замените кадр, когда он закончится."}
        scenes = pipeline.read_json(os.path.join(d, "scenes.json"), []) or []
        i = int(i)
        if not (0 <= i < len(scenes)):
            return {"ok": False, "error": "Нет такой сцены."}
        cfg = load_cfg()
        return pipeline.stock.replace(cfg, d, data_dir(), scenes[i], query, ffmpeg=pipeline.ffmpeg_bin(), creationflags=pipeline.NO_WINDOW,
                                      log=pipeline.log, clear=self._stock_clear(d), verify=pipeline.stock_verify(cfg),
                                      llm=pipeline.stock_llm(cfg), topic=self._topic(scenes))

    def stock_to_generation(self, job_id, i):
        d = self._frame_dir(job_id)
        if not d:
            return {"ok": False, "error": "Папка ролика не найдена."}
        if self._running_dir() and os.path.normcase(self._running_dir()) == os.path.normcase(d):
            return {"ok": False, "error": "Ролик сейчас делается — сделайте это, когда он закончится."}
        return pipeline.stock.to_generation(d, int(i), clear=self._stock_clear(d))

    def _stock_rec(self, d):
        if getattr(self, "_srec_dir", None) != d or time.time() - getattr(self, "_srec_at", 0) > 2:
            self._srec, self._srec_dir, self._srec_at = pipeline.stock.record(d), d, time.time()
        return self._srec

    def frames_action(self, job_id, idxs, action):
        """Окно «Кадры», выбрано несколько: redo — перерисовать (стоковые — сгенерировать вместо стока);
        stock_next — другие со стока (только стоковые); stock_gen — стоковые сгенерировать вместо стока;
        to_stock — сгенерированные заменить кадрами со стока (подошло — заменяем, нет — генерация остаётся)."""
        import concurrent.futures as cf
        d = self._frame_dir(job_id)
        if not d:
            return {"ok": False, "error": "Папка ролика не найдена."}
        if self._running_dir() and os.path.normcase(self._running_dir()) == os.path.normcase(d):
            return {"ok": False, "error": "Ролик сейчас делается — дождитесь конца."}
        scenes = pipeline.read_json(os.path.join(d, "scenes.json"), []) or []
        rec = pipeline.stock.record(d)
        clear = self._stock_clear(d)
        idxs = sorted({int(x) for x in idxs or [] if 0 <= int(x) < len(scenes)})
        stock_idx = [i for i in idxs if (rec.get(str(i)) or {}).get("src")]
        plain_idx = [i for i in idxs if i not in stock_idx]
        done, skipped, errors = 0, 0, []
        if action == "redo":
            for i in idxs:
                if i in stock_idx:
                    pipeline.stock.to_generation(d, i, clear=clear)
                else:
                    pipeline.redo_files(d, i)
                done += 1
        elif action == "stock_gen":
            for i in stock_idx:
                pipeline.stock.to_generation(d, i, clear=clear)
                done += 1
            skipped = len(plain_idx)
        elif action in ("stock_next", "to_stock"):
            target = stock_idx if action == "stock_next" else plain_idx
            skipped = len(idxs) - len(target)
            cfg = load_cfg()
            verify = pipeline.stock_verify(cfg, pipeline.log)
            queries = {}
            llm, topic = pipeline.stock_llm(cfg), self._topic(scenes)
            if action == "to_stock" and target:  # запросы по предложениям сцен — моделью текстов (нет ключа — из промптов)
                queries = pipeline.stock.make_queries(llm, [scenes[i] for i in target], pipeline.log, topic)

            def one(i):
                q, hist = (queries.get(i) or (None, None, ""))[:2] if action == "to_stock" else (None, None)
                return i, pipeline.stock.replace(cfg, d, data_dir(), scenes[i], q, ffmpeg=pipeline.ffmpeg_bin(), creationflags=pipeline.NO_WINDOW,
                                                 log=pipeline.log, clear=clear, verify=verify, historic=hist, llm=llm, topic=topic)
            with cf.ThreadPoolExecutor(max_workers=3) as ex:
                for i, r in ex.map(one, target):
                    if r.get("ok"):
                        done += 1
                    else:
                        errors.append(f"{i + 1}: {r.get('error')}")
        self._srec_dir = None
        return {"ok": True, "done": done, "skipped": skipped, "kept": len(errors) if action == "to_stock" else 0, "errors": errors[:5]}

    def stock_check(self, source, key=None):
        """Проверить ключ стока (Pexels / Pixabay): введённый в окне или сохранённый."""
        key = str(key or "").strip()
        if not key or "•" in key:
            key = pipeline.stock.keys_of(load_cfg()).get(source, "")
        if not key:
            return {"ok": False, "error": "Ключ не введён."}
        return pipeline.stock.check_key(source, key)

    def job_stats(self):
        """Цифры для панели прогресса: картинки, клипы сцен, оживление, брак — прямо по файлам проекта."""
        with LOCK:
            jd, n = STATE.get("job_dir") or pipeline.LAST_JOB_DIR, STATE.get("scenes") or 0
        if not jd or not os.path.isdir(jd):
            return {"ok": False}
        cnt = lambda d, ext: sum(1 for f in os.listdir(d) if f.endswith(ext) and f[:4].isdigit()) if os.path.isdir(d) else 0
        scenes = pipeline.read_json(os.path.join(jd, "scenes.json")) or []
        n = n or len(scenes)
        clips = max([cnt(os.path.join(jd, c), ".mp4") for c in pipeline.clip_dirs(jd)] or [0])
        img_state = pipeline.read_json(os.path.join(jd, "images.json"), {}) or {}
        bad = sum(1 for v in img_state.values() if isinstance(v, dict) and not v.get("ok"))
        vids = cnt(os.path.join(jd, "videos"), ".mp4")
        anim = 0
        try:
            rec = pipeline.read_json(os.path.join(jd, "job.json"), {}) or {}
            a = rec.get("args") or {}
            if scenes and (a.get("mode") in ("video", "mixed") or a.get("video_first")):
                anim = len(pipeline.animated_scenes(scenes, a.get("mode") or "images", a.get("video_first") or 0))
        except Exception:
            anim = 0
        return {"ok": True, "job": os.path.basename(jd), "scenes": n, "images": cnt(os.path.join(jd, "images"), ".png"),
                "clips": clips, "videos": vids, "anim": anim, "bad": bad}

    def gallery(self, known):
        """Кадры сцен, которых окно ещё не видело. known — список индексов, уже показанных."""
        with LOCK:
            job_dir, n = STATE.get("job_dir") or pipeline.LAST_JOB_DIR, STATE.get("scenes") or 0
        if not job_dir:
            return {"total": 0, "items": [], "job": ""}
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
                    items.append({"i": i, "key": key, "img": thumb, "video": has_video,
                                  "stock": bool((self._stock_rec(job_dir).get(str(i)) or {}).get("src"))})
        if not n:
            scenes = pipeline.read_json(os.path.join(job_dir, "scenes.json")) or []
            n = len(scenes)
        return {"total": n, "items": items[:24], "redo": redo, "job": os.path.basename(job_dir)}

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
        with LOCK:
            busy = bool(STATE["running"] or STATE["preparing"] or STATE.get("queue_active"))
            if busy:
                STATE["message"] = "останавливаю… (дожидаюсь текущих запросов)"
        if busy:
            pipeline.CANCEL.set()
            pipeline.log("нажат «Стоп»")
        return {"ok": True, "running": busy}

    KEY_ROWS = (("fastgen", "fast-gen", "картинки, видео, тексты"), ("nonstop", "veononstop", "картинки и видео"), ("yougen", "yougen", "картинки и видео"),
                ("royal", "royaltechno", "картинки и видео"), ("slider", "secretslider", "картинки"), ("google", "Google", "картинки и видео"),
                ("openai", "OpenAI", "тексты, автоотбор"), ("anthropic", "Claude", "тексты, автоотбор"), ("gemini", "Gemini", "тексты, автоотбор"),
                ("assemblyai", "AssemblyAI", "распознавание речи"), ("voicegen", "voicegen", "озвучка"), ("voicer", "voicer", "озвучка"), ("lumean", "lumean", "озвучка"),
                ("pexels", "Pexels", "стоки"), ("pixabay", "Pixabay", "стоки"))

    def keys_table(self):
        """Раздел «Ключи и сервисы»: какие ключи введены (без самих ключей)."""
        cfg = load_cfg()
        rows = []
        for kid, name, role in self.KEY_ROWS:
            if kid in pipeline.PROVIDERS:
                keys = pipeline.provider_keys(cfg, kid)
            else:
                k = cfg.get(f"{kid}_api_key") or ""
                keys = [k] if k else []
            rows.append({"id": kid, "name": name, "role": role, "count": len(keys), "mask": mask(keys[0]) if keys else "",
                         "selected": kid in (cfg.get("image_providers") or [])})
        return rows

    def keys_check(self, kid):
        """Проверка одного сервиса для таблицы ключей. → {"ok", "text"}"""
        kid = str(kid or "")
        try:
            if kid in ("pexels", "pixabay"):
                r = self.stock_check(kid)
                return {"ok": bool(r.get("ok")), "text": "OK" + (f" · {r.get('note')}" if r.get("note") else "") if r.get("ok") else (r.get("error") or "ошибка")}
            r = self.check_keys([kid]).get(kid)
            vals = r if isinstance(r, list) else [r]
            vals = [str(v or "") for v in vals]
            ok = bool(vals) and all(v.startswith("OK") for v in vals)
            return {"ok": ok, "text": " | ".join(vals) if vals else "нет ответа"}
        except Exception as e:
            return {"ok": False, "text": str(e)[:200]}

    def hw_check(self):
        """Настройки → «Кодирование на видеокарте»: есть ли подходящий кодек и проходит ли пробное кодирование."""
        try:
            e = pipeline.hw_encoder()
            return {"ok": True, "found": bool(e), "label": e[1] if e else ""}
        except Exception as ex:
            return {"ok": False, "error": str(ex)[:200]}

    def hero_counts(self, prompts_file):
        """В скольких кадрах файла готовых промптов встречается имя каждого героя. → {"ok", "total", "counts": {имя: N}}"""
        path = str(prompts_file or "").strip().strip('"')
        if not path or not os.path.isfile(path):
            return {"ok": False}
        try:
            blocks = pipeline.read_prompts_file(path)
            prompts = [t for b in blocks for t in b["prompts"]]
            counts = {}
            for h in (load_cfg().get("heroes") or []):
                n = h.get("name") or ""
                if n:
                    rx = pipeline.hero_name_re(n)
                    counts[n] = sum(1 for t in prompts if rx.search(t))
            return {"ok": True, "total": len(prompts), "counts": counts}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def job_summary(self):
        """Что делается на экране создания — по записи самого ролика (job.json), а не по текущим настройкам шагов."""
        with LOCK:
            d = STATE.get("job_dir") or pipeline.LAST_JOB_DIR
        if not d:
            return {"ok": False}
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) or {}
        args, cfg = rec.get("args") or {}, rec.get("cfg") or {}
        fx = [k for k, v in ((cfg.get("fx") or {}).items()) if isinstance(v, dict) and v.get("on")]
        heroes = [h for h in (cfg.get("heroes") or []) if h.get("on", True) and h.get("file")]
        stock = cfg.get("stock") or {}
        return {"ok": bool(rec), "title": rec.get("title") or "", "mode": args.get("mode") or "", "subtitles": bool(args.get("subtitles")),
                "motion": bool(args.get("motion")), "fx": fx, "heroes": len(heroes) if (args.get("profile") or args.get("prompts_file")) else 0,
                "stock": int(stock.get("share") or 0) if stock.get("on") else 0, "profile": args.get("profile") or "",
                "ready": bool(args.get("prompts_file")), "remix": bool(args.get("remix_from"))}

    def result_info(self):
        """Готовый ролик: длина и размер файла — для экрана «Готово»."""
        with LOCK:
            res = STATE.get("result")
        if not res or not os.path.exists(res):
            return {"ok": False}
        out = {"ok": True, "path": res, "name": os.path.basename(res), "size_mb": round(os.path.getsize(res) / 1048576, 1), "duration": 0}
        try:
            out["duration"] = round(float(pipeline.audio_duration(res)), 1)
        except Exception:
            pass
        try:  # кредиты fast-gen по оставшимся кадрам: модель кадра × цена модели; повторы и брак сюда не попадают
            with LOCK:
                d = STATE.get("job_dir") or pipeline.LAST_JOB_DIR
            models = pipeline.fastgen_models() or {}
            price = {m.get("op"): m.get("credits") or 0 for kind in ("image", "video") for m in (models.get(kind) or []) if m.get("op")}
            total, other = 0, 0
            for name in ("images.json", "videos.json"):
                for v in (pipeline.read_json(os.path.join(d, name), {}) or {}).values():
                    m = str((v or {}).get("model") or "") if isinstance(v, dict) else ""
                    if not m or not (v or {}).get("ok", True):
                        continue
                    if m.startswith("fastgen:"):
                        total += price.get(m.split(":", 1)[1], 0)
                    elif not m.startswith(("stock:", "copy_prev")):
                        other += 1
            out["credits"], out["other"] = total, other
        except Exception:
            pass
        return out

    def open_result(self):
        with LOCK:
            res = STATE.get("result")
        if res and os.path.exists(res):
            os.startfile(res)
            return True
        return False

    # ===== профили: создать, изменить, удалить =====
    def profiles_list(self):
        return pipeline.list_profiles()

    def profile_read(self, name):
        try:
            r = pipeline.profiles.read_profile_raw(name, pipeline.profile_search_dirs())
            r.pop("dir", None)
            r["placeholders"] = pipeline.profiles.PLACEHOLDERS
            r["defaults"] = {k: pipeline.profiles.DEFAULTS.get(k) for k in pipeline.profiles.EDIT_KEYS}
            return {"ok": True, **r}
        except pipeline.profiles.ProfileError as e:
            return {"ok": False, "error": str(e)}

    def profile_blank(self):
        """Пустой профиль для «Создать»: настройки по умолчанию."""
        d = {k: pipeline.profiles.DEFAULTS.get(k) for k in pipeline.profiles.EDIT_KEYS}
        d["transitions"] = ""
        return {"ok": True, "name": "", "master": "", "settings": d, "rules": 0, "placeholders": pipeline.profiles.PLACEHOLDERS, "defaults": d}

    def profile_save(self, data):
        with LOCK:
            busy = STATE["running"] or STATE["preparing"] or STATE.get("queue_active")
        old = (data or {}).get("old_name")
        if busy and old and old != (data or {}).get("name"):
            return {"ok": False, "error": "Пока делается ролик, профиль нельзя переименовать — его папка занята."}
        try:
            name = pipeline.profiles.save_profile_raw(pipeline.profile_search_dirs(), data or {})
        except (pipeline.profiles.ProfileError, OSError) as e:
            return {"ok": False, "error": str(e)}
        cfg = load_cfg()
        if old and cfg.get("profile") == old and old != name:
            cfg["profile"] = name
            save_cfg(cfg)
        pipeline.log(f"профиль «{name}» сохранён")
        return {"ok": True, "name": name, "profiles": pipeline.list_profiles(),
                "check": pipeline.profiles.master_check((data or {}).get("master"))}

    def profile_delete(self, name):
        with LOCK:
            busy = STATE["running"] or STATE["preparing"] or STATE.get("queue_active")
        if busy:
            return {"ok": False, "error": "Пока делается ролик, профили лучше не удалять — дождитесь конца."}
        try:
            pipeline.profiles.delete_profile_raw(name, pipeline.profile_search_dirs())
        except (pipeline.profiles.ProfileError, OSError) as e:
            return {"ok": False, "error": str(e)}
        cfg = load_cfg()
        if cfg.get("profile") == name:
            cfg["profile"] = ""
            save_cfg(cfg)
        return {"ok": True, "profiles": pipeline.list_profiles()}

    def profile_voice(self, name):
        return pipeline.profiles.profile_voice(name, pipeline.profile_search_dirs()) if name else None

    def profile_defaults(self, name):
        """Что профиль подставляет в окно: голос и папку выкладки."""
        if not name:
            return {}
        try:
            st = pipeline.profiles.read_profile_raw(name, pipeline.profile_search_dirs())["settings"]
        except pipeline.profiles.ProfileError:
            return {}
        out = {"voice": pipeline.profiles.profile_voice(name, pipeline.profile_search_dirs()), "output_dir": st.get("output_dir") or "",
               "look": st.get("look") or ""}
        if st.get("subfolder"):  # профиль просит подпапку — включаем; не просит — не трогаем выбор пользователя
            out["subfolder"] = True
        return out

    def pick_folder(self):
        """Выбрать папку (без сохранения в настройки) — для папки выкладки профиля."""
        res = WINDOW.create_file_dialog(getattr(webview, "FOLDER_DIALOG", 20))
        if not res:
            return ""
        return res[0] if isinstance(res, (list, tuple)) else str(res)

    def profile_master_check(self, master):
        return pipeline.profiles.master_check(master)

    # ===== вид субтитров =====
    def sub_style_get(self):
        cfg = load_cfg()
        st = cfg.get("sub_style")
        fonts = [f["name"] for f in pipeline.subs_style.list_fonts(pipeline.font_dirs())]
        return {"style": pipeline.subs_style.normalize(st if isinstance(st, dict) else {}), "classic": not pipeline.sub_style_of(cfg),
                "presets": {k: pipeline.subs_style.normalize(v) for k, v in pipeline.subs_style.PRESETS.items()},
                "fonts": fonts, "defaults": pipeline.subs_style.DEFAULT,
                "source": "asr" if cfg.get("sub_source") == "asr" else "script"}

    def sub_style_save(self, style, classic=False, source=None):
        cfg = load_cfg()
        cfg["sub_style"] = {"classic": True} if classic else pipeline.subs_style.normalize(style or {})
        if source in ("script", "asr"):
            cfg["sub_source"] = source
        save_cfg(cfg)
        return {"ok": True}

    def sub_preview(self, style):
        """Субтитр тем же ffmpeg, что и при сборке: живой ролик на пару секунд (видно появление, караоке, по словам).
        Фон — видео или картинка из набора для предпросмотра, иначе кадр последнего ролика."""
        import base64
        work = os.path.join(data_dir(), "jobs", "_subs_preview")
        out = os.path.join(work, "preview.mp4")
        own_vid = self._pv_files(self.PV_VID)
        bg = own_vid[0] if own_vid else self._preview_images(1)[0]
        try:
            pipeline.subs_style.preview(pipeline.ffmpeg_bin(), style or {}, out, work, pipeline.font_dirs(), background=bg,
                                        creationflags=pipeline.NO_WINDOW)
            with open(out, "rb") as f:
                return {"ok": True, "video": "data:video/mp4;base64," + base64.b64encode(f.read()).decode()}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def look_preview(self, subs=True):
        """Шаг «Оформление»: 3 секунды кадра с текущими эффектами и, если включены, субтитрами — как будет в ролике."""
        import base64
        if not PREVIEW_LOCK.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            cfg = load_cfg()
            work = os.path.join(data_dir(), "jobs", "_look_preview")
            os.makedirs(work, exist_ok=True)
            fxv = pipeline.fx_preview(cfg.get("fx") if isinstance(cfg.get("fx"), dict) else {}, self._preview_images(1)[0], os.path.join(work, "fx.mp4"))
            out = fxv
            if subs:
                st = cfg.get("sub_style")
                out = os.path.join(work, "look.mp4")
                pipeline.subs_style.preview(pipeline.ffmpeg_bin(), pipeline.subs_style.normalize(st if isinstance(st, dict) else {}) if pipeline.sub_style_of(cfg) else {},
                                            out, work, pipeline.font_dirs(), background=fxv, creationflags=pipeline.NO_WINDOW)
            with open(out, "rb") as f:
                return {"ok": True, "video": "data:video/mp4;base64," + base64.b64encode(f.read()).decode()}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        finally:
            PREVIEW_LOCK.release()

    # ===== переходы и движение картинок =====
    def trans_get(self):
        cfg = load_cfg()
        st = pipeline.trans_settings(cfg)
        return {"settings": st, "all": [{"id": k, "name": n} for k, n in pipeline.XFADE_ALL],
                "presets": {k: dict(v, set=list(v["set"])) for k, v in pipeline.TRANS_PRESETS.items()}, "motion": bool(cfg.get("motion"))}

    def trans_save(self, t):
        cfg = load_cfg()
        t = dict(t or {})
        st = pipeline.trans_settings({"transitions": t})
        cfg["transitions"] = {"preset": st["preset"]} if st["preset"] != "custom" else \
            {"preset": "custom", "set": st["set"], "order": st["order"], "duration": st["duration"], "kb": st["kb"]}
        if st["video_set"]:
            cfg["transitions"]["video_set"] = st["video_set"]
        save_cfg(cfg)
        return {"ok": True, "settings": st}

    # ===== шаблоны оформления: эффекты + субтитры + переходы + движение под своим названием =====
    LOOK_KEYS = ("fx", "sub_style", "sub_source", "subtitles", "transitions", "motion")

    def looks_list(self):
        cfg = load_cfg()
        looks = cfg.get("looks") if isinstance(cfg.get("looks"), dict) else {}
        return {"names": sorted(looks, key=str.lower), "current": cfg.get("look_current") or ""}

    def look_save(self, name):
        name = " ".join(str(name or "").split())[:60]
        if not name:
            return {"ok": False, "error": "Введите название шаблона."}
        cfg = load_cfg()
        looks = cfg.get("looks") if isinstance(cfg.get("looks"), dict) else {}
        looks[name] = {k: cfg.get(k) for k in self.LOOK_KEYS if k in cfg}
        cfg["looks"], cfg["look_current"] = looks, name
        save_cfg(cfg)
        return dict(self.looks_list(), ok=True)

    def look_apply(self, name):
        cfg = load_cfg()
        tpl = (cfg.get("looks") or {}).get(name) if isinstance(cfg.get("looks"), dict) else None
        if not isinstance(tpl, dict):
            return {"ok": False, "error": f"Шаблона «{name}» нет."}
        for k in self.LOOK_KEYS:
            if k in tpl:
                cfg[k] = tpl[k]
        cfg["look_current"] = name
        save_cfg(cfg)
        return {"ok": True, "fx": pipeline.fx_settings(cfg), "subtitles": bool(cfg.get("subtitles")), "motion": bool(cfg.get("motion"))}

    def look_get(self, name):
        cfg = load_cfg()
        tpl = (cfg.get("looks") or {}).get(name) if isinstance(cfg.get("looks"), dict) else None
        if not isinstance(tpl, dict):
            return {"ok": False}
        return {"ok": True, "subtitles": bool(tpl.get("subtitles")), "motion": bool(tpl.get("motion")), "fx": pipeline.fx_settings(tpl)}

    def look_delete(self, name):
        cfg = load_cfg()
        looks = cfg.get("looks") if isinstance(cfg.get("looks"), dict) else {}
        looks.pop(name, None)
        cfg["looks"] = looks
        if cfg.get("look_current") == name:
            cfg["look_current"] = ""
        save_cfg(cfg)
        return dict(self.looks_list(), ok=True)

    # ===== набор для предпросмотра: свои картинки и видео в папке preview — одни и те же во всех окнах =====
    PV_IMG, PV_VID = (".png", ".jpg", ".jpeg", ".webp"), (".mp4", ".mov", ".webm")

    def _pv_dir(self):
        return os.path.join(data_dir(), "preview")

    def _pv_files(self, ext):
        d = self._pv_dir()
        try:
            return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith(ext)]
        except OSError:
            return []

    def preview_set_info(self):
        return {"dir": self._pv_dir(), "images": len(self._pv_files(self.PV_IMG)), "videos": len(self._pv_files(self.PV_VID))}

    def preview_set_open(self):
        os.makedirs(self._pv_dir(), exist_ok=True)
        os.startfile(self._pv_dir())
        return True

    def preview_set_from_last(self):
        """Набор из последнего ролика: 4 картинки и до 3 оживлённых клипов подряд из середины."""
        imgs = [p for p in self._preview_images(4, use_set=False) if os.sep + "_trans_preview" + os.sep not in p]
        vids = self._preview_video(3, use_set=False) or []
        if not imgs:
            return {"ok": False, "error": "В последних роликах нет картинок — сначала сделайте хотя бы один ролик."}
        d = self._pv_dir()
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            if f.lower().endswith(self.PV_IMG + self.PV_VID):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass
        for k, p in enumerate(imgs):
            shutil.copy(p, os.path.join(d, f"кадр{k + 1}{os.path.splitext(p)[1]}"))
        for k, p in enumerate(vids):
            shutil.copy(p, os.path.join(d, f"видео{k + 1}.mp4"))
        return dict(self.preview_set_info(), ok=True)

    def _preview_images(self, n=3, use_set=True):
        """n картинок для предпросмотров: из набора preview (если там есть картинки), иначе подряд из последнего ролика.
        Нет ничего — рисуем пробные."""
        own = self._pv_files(self.PV_IMG) if use_set else []
        if own:
            return [own[k % len(own)] for k in range(n)]
        with LOCK:
            jd = STATE.get("job_dir") or pipeline.LAST_JOB_DIR
        root = os.path.join(data_dir(), "jobs")
        dirs = [jd] if jd else []
        if os.path.isdir(root):
            dirs += sorted((os.path.join(root, x) for x in os.listdir(root) if not x.startswith("_")), key=lambda d: -os.path.getmtime(d))[:6]
        for d in dirs:
            idir = os.path.join(d, "images") if d else ""
            imgs = sorted(f for f in os.listdir(idir) if f.endswith(".png")) if idir and os.path.isdir(idir) else []
            if len(imgs) >= n:
                k = max(0, len(imgs) // 2 - 1)
                return [os.path.join(idir, f) for f in imgs[k:k + n]]
        from PIL import Image, ImageDraw
        work = os.path.join(data_dir(), "jobs", "_trans_preview")
        os.makedirs(work, exist_ok=True)
        out = []
        for k in range(n):
            p = os.path.join(work, f"sample{k}.png")
            if not os.path.exists(p):
                im = Image.new("RGB", (1376, 768), (40 + 50 * k, 70 + 20 * k, 120 - 30 * k))
                dr = ImageDraw.Draw(im)
                for x in range(0, 1376, 86):
                    dr.line([(x, 0), (x, 768)], fill=(230, 230, 230), width=2)
                dr.ellipse([520, 220, 856, 556], outline=(255, 220, 0), width=10)
                im.save(p)
            out.append(p)
        return out

    def fx_overlay_pick(self):
        """Свой оверлей: выбираем mp4/mov с шумами, готовим (1920×1080, 25 к/с) и определяем режим по фону."""
        try:
            res = WINDOW.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                            file_types=("Видео (*.mp4;*.mov;*.webm;*.mkv;*.avi)", "Все файлы (*.*)"))
            if not res:
                return {"ok": False, "cancel": True}
            src = res[0] if isinstance(res, (list, tuple)) else str(res)
            path, dur, mode, name = pipeline.overlay_prepare(src)
            return {"ok": True, "file": path, "dur": dur, "mode": mode, "name": name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    # ----- герои из карточек чата -----
    def _heroes_view(self):
        out = []
        for h in (load_cfg().get("heroes") or []):
            f = h.get("file") or ""
            ok = bool(f) and os.path.isfile(f)
            out.append({"name": h.get("name", ""), "desc": h.get("desc", ""), "portrait": h.get("portrait", ""), "on": h.get("on", True) is not False,
                        "img": make_thumb(f, 320) if ok else None})
        return out

    def heroes_list(self):
        return self._heroes_view()

    def heroes_add(self, text):
        """Вставили карточки из чата (HERO / DESCRIPTION / PORTRAIT): добавляем новых, у существующих обновляем описание."""
        cards = pipeline.hero_cards(text)
        if not cards:
            return {"ok": False, "error": "Не нашёл карточек. Нужны строки «HERO: ИМЯ», «DESCRIPTION: …», «PORTRAIT: …» (имя латиницей, без пробелов)."}
        cfg = load_cfg()
        cur = cfg.get("heroes") or []
        by = {h["name"]: h for h in cur}
        for c in cards:
            if c["name"] in by:
                by[c["name"]].update({"desc": c["desc"] or by[c["name"]].get("desc", ""), "portrait": c["portrait"]})
            else:
                h = {"name": c["name"], "desc": c["desc"], "portrait": c["portrait"], "file": "", "on": True}
                cur.append(h)
                by[c["name"]] = h
        cfg["heroes"] = cur
        save_cfg(cfg)
        return {"ok": True, "added": [c["name"] for c in cards], "heroes": self._heroes_view()}

    def hero_make(self, name):
        cfg = load_cfg()
        h = next((x for x in (cfg.get("heroes") or []) if x.get("name") == name), None)
        if not h:
            return {"ok": False, "error": "нет такого героя"}
        try:
            h["file"] = pipeline.hero_make_portrait(cfg, h)
        except BaseException as e:
            return {"ok": False, "error": str(e)[:300]}
        save_cfg(cfg)
        return {"ok": True, "heroes": self._heroes_view()}

    def hero_upload(self, name):
        """Свой портрет вместо нарисованного: файл → png ≤1600 px."""
        cfg = load_cfg()
        h = next((x for x in (cfg.get("heroes") or []) if x.get("name") == name), None)
        if not h:
            return {"ok": False, "error": "нет такого героя"}
        res = WINDOW.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                        file_types=("Картинки (*.png;*.jpg;*.jpeg;*.webp)", "Все файлы (*.*)"))
        if not res:
            return {"ok": False, "cancel": True}
        src = res[0] if isinstance(res, (list, tuple)) else str(res)
        try:
            from PIL import Image
            im = Image.open(src).convert("RGB")
            im.thumbnail((1600, 1600))
            dst = os.path.join(pipeline.hero_dir(), name + ".png")
            im.save(dst)
            h["file"] = dst
            save_cfg(cfg)
            return {"ok": True, "heroes": self._heroes_view()}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def hero_set(self, name, patch):
        cfg = load_cfg()
        h = next((x for x in (cfg.get("heroes") or []) if x.get("name") == name), None)
        if not h or not isinstance(patch, dict):
            return {"ok": False}
        for k in ("on", "desc", "portrait"):
            if k in patch:
                h[k] = bool(patch[k]) if k == "on" else str(patch[k])[:2000]
        save_cfg(cfg)
        return {"ok": True, "heroes": self._heroes_view()}

    def hero_delete(self, name):
        cfg = load_cfg()
        cfg["heroes"] = [h for h in (cfg.get("heroes") or []) if h.get("name") != name]
        save_cfg(cfg)
        try:
            os.remove(os.path.join(pipeline.hero_dir(), name + ".png"))
        except OSError:
            pass
        return {"ok": True, "heroes": self._heroes_view()}

    def heroes_delete_many(self, names):
        """Удалить выбранных героев вместе с их портретами."""
        names = {str(n) for n in (names or [])}
        cfg = load_cfg()
        cfg["heroes"] = [h for h in (cfg.get("heroes") or []) if h.get("name") not in names]
        save_cfg(cfg)
        for n in names:
            try:
                os.remove(os.path.join(pipeline.hero_dir(), n + ".png"))
            except OSError:
                pass
        return {"ok": True, "heroes": self._heroes_view()}

    def fx_overlay_list(self):
        try:
            return pipeline.overlay_builtin_list()
        except Exception:
            return []

    def fx_overlay_builtin(self, fid):
        try:
            path, dur, mode, name, clean = pipeline.overlay_builtin(fid)
            return {"ok": True, "file": path, "dur": dur, "mode": mode, "name": name, "clean": clean}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    def fx_preview(self, fx):
        """3 секунды кадра вашего ролика с выбранными эффектами — для окна."""
        import base64
        if not PREVIEW_LOCK.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            work = os.path.join(data_dir(), "jobs", "_trans_preview")
            os.makedirs(work, exist_ok=True)
            p = pipeline.fx_preview(fx if isinstance(fx, dict) else {}, self._preview_images(1)[0], os.path.join(work, "fx_preview.mp4"))
            with open(p, "rb") as f:
                return {"ok": True, "video": "data:video/mp4;base64," + base64.b64encode(f.read()).decode()}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        finally:
            PREVIEW_LOCK.release()

    def _preview_video(self, n=1, use_set=True):
        """Оживлённые клипы для предпросмотра: из набора preview, иначе из последних роликов. n>1 — список подряд идущих."""
        own = self._pv_files(self.PV_VID) if use_set else []
        if own:
            got = [own[k % len(own)] for k in range(n)]
            return got if n > 1 else got[0]
        with LOCK:
            jd = STATE.get("job_dir") or pipeline.LAST_JOB_DIR
        root = os.path.join(data_dir(), "jobs")
        dirs = [jd] if jd else []
        if os.path.isdir(root):
            dirs += sorted((os.path.join(root, x) for x in os.listdir(root) if not x.startswith("_")), key=lambda d: -os.path.getmtime(d))[:10]
        for d in dirs:
            vd = os.path.join(d, "videos") if d else ""
            vids = sorted(f for f in os.listdir(vd) if f.endswith(".mp4") and f[:4].isdigit()) if vd and os.path.isdir(vd) else []
            if vids:
                k = max(0, min(len(vids) - n, len(vids) // 2 - n // 2))
                got = [os.path.join(vd, f) for f in vids[k:k + n]]
                return got if n > 1 else got[0]
        return [] if n > 1 else None

    def trans_preview(self, t, kind="images"):
        """Предпросмотр переходов: kind="images" — три картинки; "video" — картинка → оживлённый клип → картинка."""
        import base64
        if not PREVIEW_LOCK.acquire(blocking=False):
            return {"ok": False, "busy": True}
        try:
            work = os.path.join(data_dir(), "jobs", "_trans_preview")
            kind = kind if kind in ("video", "videos") else "images"
            video = self._preview_video() if kind == "video" else (self._preview_video(3) if kind == "videos" else None)
            p = pipeline.trans_preview(t or {}, self._preview_images(3), work, kind=kind, video=video)
            with open(p, "rb") as f:
                return {"ok": True, "kind": kind, "real_video": bool(video),
                        "video": "data:video/mp4;base64," + base64.b64encode(f.read()).decode()}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}
        finally:
            PREVIEW_LOCK.release()

    # ===== кадры готового ролика =====
    def _job_path(self, job_id):
        name = os.path.basename(str(job_id or ""))
        d = os.path.join(data_dir(), "jobs", name)
        return d if name and not name.startswith(".") and os.path.isdir(d) else None

    def job_frames(self, job_id, offset=0, limit=24):
        """Миниатюры кадров ролика порциями: номер, картинка, есть ли оживлённое видео."""
        d = self._job_path(job_id)
        if not d:
            return {"ok": False, "error": "Папка ролика не найдена."}
        img_dir, vid_dir = os.path.join(d, "images"), os.path.join(d, "videos")
        scenes = pipeline.read_json(os.path.join(d, "scenes.json"), []) or []
        imgs = {}
        if os.path.isdir(img_dir):
            for f in os.listdir(img_dir):
                if f.endswith(".png") and f[:4].isdigit():
                    imgs[int(f[:4])] = os.path.join(img_dir, f)
        vids = set()
        if os.path.isdir(vid_dir):
            vids = {int(f[:4]) for f in os.listdir(vid_dir) if f.endswith(".mp4") and f[:4].isdigit()}
        total = max([len(scenes)] + [i + 1 for i in imgs] + [i + 1 for i in vids])
        items = []
        for i in range(int(offset), min(total, int(offset) + int(limit))):
            sc = scenes[i] if i < len(scenes) and isinstance(scenes[i], dict) else {}
            items.append({"i": i, "img": make_thumb(imgs[i], 360) if i in imgs else None, "video": i in vids, "stock": bool((self._stock_rec(d).get(str(i)) or {}).get("src")),
                          "t": round(float(sc.get("start", 0) or 0), 1), "text": str(sc.get("text") or "")[:300],
                          "prompt": str(sc.get("raw_prompt") or sc.get("prompt") or "")[:400]})
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) or {}
        res = rec.get("result")
        return {"ok": True, "total": total, "images": len(imgs), "videos": len(vids), "items": items,
                "result": res if res and os.path.exists(res) else None}

    def open_frame(self, job_id, i, kind="auto"):
        """Открыть кадр в обычном просмотрщике: видео (если оживлён) или картинку."""
        d = self._frame_dir(job_id)
        if not d:
            return False
        i = int(i)
        vid, img = os.path.join(d, "videos", f"{i:04d}.mp4"), os.path.join(d, "images", f"{i:04d}.png")
        for p in ((vid, img) if kind in ("auto", "video") else (img,)):
            if os.path.exists(p):
                os.startfile(p)
                return True
        return False

    def _frame_dir(self, job_id):
        if job_id:
            return self._job_path(job_id)
        with LOCK:
            return STATE.get("job_dir") or pipeline.LAST_JOB_DIR

    def frame_media(self, job_id, i):
        """Просмотр кадра в окне: картинка покрупнее, есть ли видео, текст сцены и промпт."""
        import base64, io
        d = self._frame_dir(job_id)
        if not d:
            return {"ok": False, "error": "Папка ролика не найдена."}
        i = int(i)
        img = os.path.join(d, "images", f"{i:04d}.png")
        vid = os.path.join(d, "videos", f"{i:04d}.mp4")
        scenes = pipeline.read_json(os.path.join(d, "scenes.json"), []) or []
        sc = scenes[i] if 0 <= i < len(scenes) and isinstance(scenes[i], dict) else {}
        url = None
        if os.path.exists(img):
            try:
                from PIL import Image
                im = Image.open(img).convert("RGB")
                im.thumbnail((1920, 1920))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            except Exception:
                url = None
        sk = pipeline.stock.record(d).get(str(i)) or {}
        stock_info = {"src": pipeline.stock.SOURCE_NAMES.get(sk["src"], sk["src"]), "kind": sk.get("kind"), "q": sk.get("q"), "page": sk.get("page")} if sk.get("src") else None
        return {"ok": True, "i": i, "img": url, "video": os.path.exists(vid), "total": len(scenes), "stock": stock_info,
                "video_mb": round(os.path.getsize(vid) / 1048576, 1) if os.path.exists(vid) else 0,
                "text": str(sc.get("text") or ""), "prompt": str(sc.get("raw_prompt") or sc.get("prompt") or ""),
                "start": round(float(sc.get("start", 0) or 0), 1), "end": round(float(sc.get("end", 0) or 0), 1)}

    def _running_dir(self):
        with LOCK:
            if STATE["running"] or STATE["preparing"]:
                return STATE.get("job_dir")
        return None

    def frame_edit(self, job_id, i, prompt=None):
        """Правка кадра: новый промпт (или тот же) → картинка удаляется и рисуется заново при «Продолжить».
        Пока этот ролик делается — только перерисовка с тем же промптом (как кнопка ↻)."""
        d = self._frame_dir(job_id)
        if not d:
            return {"ok": False, "error": "Папка ролика не найдена."}
        i = int(i)
        scenes = pipeline.read_json(os.path.join(d, "scenes.json"), []) or []
        if not 0 <= i < len(scenes):
            return {"ok": False, "error": "Нет такого кадра."}
        cur = scenes[i].get("prompt") or ""
        prompt = " ".join(str(prompt or "").split())
        changed = bool(prompt) and prompt != " ".join(cur.split())
        run = self._running_dir()
        if run and os.path.normcase(run) == os.path.normcase(d):
            if changed:
                return {"ok": False, "error": "Пока этот ролик делается, промпт менять нельзя — дождитесь конца или нажмите «Стоп». "
                                              "Перерисовать с тем же промптом можно сейчас."}
            return dict(self.redo_scene(i), running=True)
        if changed:
            pipeline.save_edit(d, i, prompt)
        pipeline.redo_files(d, i)
        pipeline.log(f"кадр {i + 1}: {'новый промпт, ' if changed else ''}будет нарисован заново")
        return {"ok": True, "changed": changed, "job_id": os.path.basename(d)}

    def frame_replace(self, job_id, i):
        """Своя картинка вместо кадра: выбрать файл → он станет кадром (оживление этого кадра удаляется)."""
        d = self._frame_dir(job_id)
        if not d:
            return {"ok": False, "error": "Папка ролика не найдена."}
        run = self._running_dir()
        if run and os.path.normcase(run) == os.path.normcase(d):
            return {"ok": False, "error": "Пока этот ролик делается, кадры не заменить — дождитесь конца или нажмите «Стоп»."}
        res = WINDOW.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                        file_types=("Картинки (*.png;*.jpg;*.jpeg;*.webp)", "Все файлы (*.*)"))
        if not res:
            return {"ok": False, "cancel": True}
        src = res[0] if isinstance(res, (list, tuple)) else str(res)
        i = int(i)
        try:
            from PIL import Image
            im = Image.open(src).convert("RGB")
        except Exception as e:
            return {"ok": False, "error": f"Не открылась картинка: {e}"}
        pipeline.redo_files(d, i)
        os.makedirs(os.path.join(d, "images"), exist_ok=True)
        im.save(os.path.join(d, "images", f"{i:04d}.png"))
        st_path = os.path.join(d, "images.json")
        st = pipeline.read_json(st_path, {}) or {}
        st[str(i)] = {"ok": True, "model": "своя картинка"}
        pipeline.write_json(st_path, st)
        THUMBS.pop(os.path.join(d, "images", f"{i:04d}.png"), None)
        pipeline.log(f"кадр {i + 1}: заменён своей картинкой {os.path.basename(src)}")
        return {"ok": True, "job_id": os.path.basename(d)}

    def frame_video(self, job_id, i):
        """Оживлённый клип целиком для плеера в окне (до 40 МБ; больше — открыть в обычном плеере)."""
        import base64
        d = self._frame_dir(job_id)
        p = os.path.join(d, "videos", f"{int(i):04d}.mp4") if d else ""
        if not p or not os.path.exists(p):
            return {"ok": False, "error": "Видео нет."}
        if os.path.getsize(p) > 40 * 1048576:
            return {"ok": False, "big": True, "error": "Видео большое — откройте его в плеере."}
        with open(p, "rb") as f:
            return {"ok": True, "video": "data:video/mp4;base64," + base64.b64encode(f.read()).decode()}

    def open_job_folder(self, job_id, sub=""):
        d = self._job_path(job_id)
        if not d:
            return False
        p = os.path.join(d, sub) if sub in ("images", "videos") and os.path.isdir(os.path.join(d, sub)) else d
        os.startfile(p)
        return True

    def open_path(self, path):
        """Открыть готовый ролик (только из папок программы)."""
        p = os.path.abspath(str(path or ""))
        roots = [os.path.abspath(data_dir())] + ([os.path.abspath(load_cfg().get("output_dir"))] if load_cfg().get("output_dir") else [])
        if os.path.exists(p) and p.lower().endswith((".mp4", ".srt", ".txt")) and any(os.path.normcase(p).startswith(os.path.normcase(r)) for r in roots):
            os.startfile(p)
            return True
        return False

    def reveal_job(self, job_id):
        """Папка с готовым роликом, файл выделен (Проводник). Ролик ещё не готов — папка output."""
        d = self._job_path(job_id)
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) if d else {}
        res = (rec or {}).get("result")
        if res and os.path.exists(res):
            try:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(res)])
                return {"ok": True}
            except Exception:
                os.startfile(os.path.dirname(res))
                return {"ok": True}
        return {"ok": False, "error": "Готового видео у этого ролика нет — возможно, его переместили или ещё не собрали."}

    def open_job_files(self, job_id):
        """Сценарий и .srt готового ролика — папка «файлы ролика» внутри проекта."""
        d = self._job_path(job_id)
        f = os.path.join(d, pipeline.FILES_DIR) if d else ""
        if not f or not os.path.isdir(f) or not os.listdir(f):
            return {"ok": False, "error": "У этого ролика нет сохранённых файлов (сценарий и .srt появляются после сборки, начиная с p41)."}
        os.startfile(f)
        return {"ok": True}

    def play_job(self, job_id):
        d = self._job_path(job_id)
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) if d else {}
        res = (rec or {}).get("result")
        if res and os.path.exists(res):
            os.startfile(res)
            return {"ok": True}
        return {"ok": False, "error": "Готового видео у этого ролика нет — возможно, его переместили или ещё не собрали."}

    def open_jobs(self):
        d = os.path.join(data_dir(), "jobs")
        os.makedirs(d, exist_ok=True)
        os.startfile(d)
        return True

    def open_output(self):
        out = load_cfg().get("output_dir") or os.path.join(data_dir(), "output")
        try:
            os.makedirs(out, exist_ok=True)
        except OSError:
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
            label = "новая озвучка" if args.get("remix_from") else "готовые промпты" if args.get("prompts_file") else ((args.get("profile") if rec else prof) or prof or "")
            thumb = None
            if imgs:
                try:
                    first = sorted(f for f in os.listdir(img_dir) if f.endswith(".png"))[0]
                    thumb = make_thumb(os.path.join(img_dir, first), 420)
                except Exception:
                    thumb = None
            try:
                length = int(float(scenes[-1].get("end") or 0)) if scenes else 0
            except (TypeError, ValueError, AttributeError):
                length = 0
            out.append({"id": n, "title": title[:80], "profile": label, "thumb": thumb, "length": length,
                        "scenes": len(scenes), "images": imgs, "videos": vids, "updated": int(updated),
                        "status": "running" if running_dir and os.path.normcase(running_dir) == os.path.normcase(d) else self._asm_status(d, rec),
                        "result": rec.get("result"), "has_video": bool(rec.get("result") and os.path.exists(rec.get("result"))),
                        "known": bool(rec), "mode": args.get("mode", ""),
                        "parent": os.path.basename(os.path.normpath(args["remix_from"])) if args.get("remix_from") else "",
                        "fg_image_model": (rec.get("cfg") or {}).get("fg_image_model") or "", "fg_video_model": (rec.get("cfg") or {}).get("fg_video_model") or ""})
        # переозвучки — под своим оригиналом (раскрываются), а не отдельными строками
        by_id = {x["id"]: x for x in out}
        top = []
        for x in out:
            par = by_id.get(x.get("parent") or "")
            if par is not None and par is not x:
                par.setdefault("children", []).append(x)
            else:
                top.append(x)
        for x in top:
            kids = sorted(x.get("children") or [], key=lambda k: -k["updated"])
            x["children"] = kids
            x["group_updated"] = max([x["updated"]] + [k["updated"] for k in kids])
        top.sort(key=lambda x: -x["group_updated"])
        return top[:limit]

    @staticmethod
    def _asm_status(d, rec):
        """«монтируется» только пока процесс монтажа жив; иначе — чем он кончился (файл статуса монтажа)."""
        st = rec.get("status", "")
        if st != "assembling":
            return st
        with ASM_LOCK:
            p = ASSEMBLY.get("proc")
            alive = p is not None and p.poll() is None and os.path.normcase(ASSEMBLY.get("job_dir") or "") == os.path.normcase(d)
        if alive:
            return "assembling"
        a = pipeline.read_json(os.path.join(d, pipeline.ASSEMBLE_STATUS), {}) or {}
        if a.get("state") == "running" and time.time() - float(a.get("updated") or 0) < 120:
            return "assembling"  # монтаж из прошлого запуска программы ещё идёт
        return "done" if a.get("state") == "done" else "error"

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

    def continue_job(self, job_id, audio=None, opts=None):
        """«Продолжить»: тот же ролик с теми же настройками; всё готовое возьмётся из задания.
        Нужен файл озвучки, а он неизвестен (ролик из старой версии или файл переехал) → need_audio.
        opts — перезапуск с изменениями: fg_image_model / fg_video_model ("__keep__" — как было), mode,
        reset_images (картинки нарисовать заново), reset_videos (оживить заново). Промпты, распознавание и озвучка остаются."""
        global QUEUE_STOP
        name = os.path.basename(str(job_id or ""))
        root = os.path.join(data_dir(), "jobs")
        d = os.path.join(root, name)
        if not name or not os.path.isdir(d):
            return {"ok": False, "error": "Папка этого ролика не найдена — возможно, заготовки удалены."}
        with LOCK:
            if STATE["running"] or STATE["preparing"] or STATE.get("queue_active"):
                return {"ok": False, "error": "Сейчас делается другой ролик. Продолжите этот, когда он закончится."}
        if assembly_busy() and os.path.normcase(ASSEMBLY.get("job_dir") or "") == os.path.normcase(d):
            return {"ok": False, "error": "Этот ролик сейчас монтируется в фоне — дождитесь конца."}
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
            if not base.startswith("script_") and got != pipeline.plain_base(base):
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
        o = opts or {}
        for k in ("fg_image_model", "fg_video_model"):
            if k in o and o[k] != "__keep__":
                cfg[k] = str(o[k] or "").strip()
        if o.get("mode") in pipeline.MODES:
            args["mode"] = o["mode"]
        if o.get("reset_images") or o.get("reset_videos"):
            freed = self._reset_job(d, images=bool(o.get("reset_images")), videos=True)
            pipeline.log(f"перезапуск «{name}»: удалено {'картинки и ' if o.get('reset_images') else ''}оживление ({freed / 1e6:.0f} МБ)")
        if not args.get("prompts_file"):  # у готовых промптов профиля нет — выбранный в окне не трогаем
            cfg["profile"] = args.get("profile") or ""
        save_cfg(cfg)
        with LOCK:
            STATE.update({"running": True, "stage": "start", "done": 0, "total": 0, "message": "продолжаю", "log": [], "result": None,
                          "error": None, "cancelled": False, "started_at": time.time(), "scenes": 0, "queue": None,
                          "title": rec.get("title") or base})
        QUEUE_STOP = False
        threading.Thread(target=job_chain, args=(args,), daemon=True).start()
        return {"ok": True, "mode": args.get("mode", "images")}

    def remix_default_dir(self, job_id):
        d = self._job_path(job_id)
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) if d else {}
        res = (rec or {}).get("result") or ""
        return os.path.dirname(res) if res and os.path.isdir(os.path.dirname(res)) else ""

    def remix_job(self, job_id, audio, opts=None):
        """«Новая озвучка»: тот же визуал готового ролика под другую озвучку. Картинки и клипы не генерируются заново,
        сцены раскладываются по новой озвучке; переходы и субтитры — как выбрано сейчас (или из opts)."""
        global QUEUE_STOP
        d = self._job_path(job_id)
        if not d:
            return {"ok": False, "error": "Папка этого ролика не найдена."}
        audio = str(audio or "").strip().strip('"')
        if not os.path.isfile(audio):
            return {"ok": False, "error": "Файл новой озвучки не найден."}
        if not pipeline.read_json(os.path.join(d, "scenes.json")):
            return {"ok": False, "error": "У этого ролика ещё нет сцен — сначала доделайте его."}
        rec = pipeline.read_json(os.path.join(d, "job.json"), {}) or {}
        a = rec.get("args") or {}
        cfg = load_cfg()
        o = opts or {}
        base = os.path.basename(d).partition("@")[0]
        script = rec.get("script") or self._job_script(d, base) or None
        # вид ролика запоминается сейчас, в момент нажатия: постоит в очереди — соберётся ровно так, что бы ни поменяли в программе
        src = dict(cfg)
        tname = o.get("look_name")
        tpl = (cfg.get("looks") or {}).get(tname) if tname and isinstance(cfg.get("looks"), dict) else None
        if isinstance(tpl, dict):
            src.update({k: tpl[k] for k in self.LOOK_KEYS if k in tpl})
        look = {"fx": src.get("fx") or {}, "sub_style": src.get("sub_style"), "sub_source": src.get("sub_source")}
        if isinstance(src.get("transitions"), dict):
            look["transitions"] = src["transitions"]
        motion = bool(src.get("motion"))
        subtitles = bool(src.get("subtitles"))
        if o.get("fx") == "none":
            look["fx"] = {}
        elif isinstance(o.get("fx"), dict):
            look["fx"] = pipeline.fx_settings({"fx": o["fx"]})
        if isinstance(o.get("sub_style"), dict):
            look["sub_style"] = {"classic": True} if o["sub_style"].get("classic") else pipeline.subs_style.normalize(o["sub_style"])
        if o.get("sub_source") in ("script", "asr"):
            look["sub_source"] = o["sub_source"]
        if "subtitles" in o:
            subtitles = bool(o["subtitles"])
        if isinstance(o.get("transitions"), dict):  # свои переходы и движение из окна «Вид ›»
            st = pipeline.trans_settings({"transitions": dict(o["transitions"])})
            t = {"preset": "custom", "set": st["set"], "order": st["order"], "duration": st["duration"], "kb": st["kb"]}
            if st.get("video_set"):
                t["video_set"] = st["video_set"]
            look["transitions"] = t
        elif o.get("transitions") in pipeline.TRANS_PRESETS:
            look["transitions"] = {"preset": o["transitions"]}
        if "motion" in o:
            motion = bool(o["motion"])
        if not motion:  # «без движения»: ни переходов, ни наездов
            look["transitions"] = {"preset": "none"}
        args = dict(audio=None if script else audio, provider=a.get("provider") or pipeline.normalize_providers(cfg.get("image_providers")),
                    mode=a.get("mode") or cfg.get("mode", "images"), style=a.get("style") or style_key(cfg.get("style"), pipeline.DEFAULT_STYLE),
                    use_refs=False, subtitles=subtitles, motion=motion, script=script, rewrite=None,
                    tts=cfg.get("tts_service"), voice=cfg.get("tts_voice", ""), video_first=a.get("video_first", 0), hd=bool(a.get("hd")),
                    profile="", voice_file=audio if script else None, prompts_file=None, remix_from=d, look=look)
        if os.path.normcase(os.path.abspath(pipeline.run_job_dir(cfg, args["audio"], script, args["voice_file"], None, "", None, d))) == os.path.normcase(os.path.abspath(d)):
            return {"ok": False, "error": "Это тот же файл озвучки, из которого сделан ролик. Выберите новую озвучку."}
        name = pipeline.safe_title(o.get("name"))
        if name:
            args["out_name"] = name
        od = str(o.get("out_dir") or "").strip().strip('"')
        orig = rec.get("result") or ""
        if od:  # выбрал свою папку в окне
            args["out_dir"], args["out_sub"] = od, False
        elif orig and os.path.isdir(os.path.dirname(orig)):  # по умолчанию — туда же, где лежит оригинал
            args["out_dir"], args["out_sub"] = os.path.dirname(orig), False
        title = name or f"{rec.get('title') or base} · новая озвучка"
        with LOCK:
            busy = STATE["running"] or STATE["preparing"] or STATE.get("queue_active")
            if busy:  # сейчас делается другой ролик — в очередь, запустится сам после него
                entry = {"id": uuid.uuid4().hex[:8], "title": title[:80], "name": name or None, "source": "remix", "script": None, "audio": None,
                         "rewrite": None, "voice": "", "style": "", "status": "queued", "result": None, "error": None, "args": args}
                QUEUE.append(entry)
                pos = len(QUEUE)
        if busy:
            queue_save()
            pipeline.log(f"«{title}» поставлен в очередь (№{pos}) — начнётся после текущего ролика")
            return {"ok": True, "queued": True, "pos": pos, "title": title, "items": queue_public()}
        with LOCK:
            STATE.update({"running": True, "stage": "start", "done": 0, "total": 0, "message": "новая озвучка для готового ролика", "log": [],
                          "result": None, "error": None, "cancelled": False, "started_at": time.time(), "scenes": 0, "queue": None, "title": title})
        QUEUE_STOP = False
        threading.Thread(target=job_chain, args=(args,), daemon=True).start()
        return {"ok": True, "mode": args["mode"]}

    @staticmethod
    def _reset_job(d, images=False, videos=True):
        """Удалить из задания картинки и/или оживление (и всё, что из них собрано). Промпты и сцены остаются. → освобождено байт."""
        names = ["clips", "clips_m", "groups", "nosubs.mp4"]
        if videos:
            names += ["videos", "videos.json"]
        if images:
            names += ["images", "images.json"]
        freed = 0
        for n in names:
            p = os.path.join(d, n)
            try:
                if os.path.isdir(p):
                    freed += pipeline.dir_size(p)
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    freed += os.path.getsize(p)
                    os.remove(p)
            except OSError:
                pass
        return freed

    def assembly_info(self):
        cur, done = assembly_state()
        return {"current": cur, "done": done, "parallel": bool(load_cfg().get("parallel_assembly", True))}

    def assembly_cancel(self):
        """Остановить фоновый монтаж. Картинки и клипы ролика остаются — «Продолжить» смонтирует заново."""
        with ASM_LOCK:
            p, d = ASSEMBLY.get("proc"), ASSEMBLY.get("job_dir")
            if p is None or p.poll() is not None:
                return {"ok": False, "error": "Сейчас ничего не монтируется."}
            ASM_JOBS.setdefault(os.path.normcase(d), {})["state"] = "stopped"
        try:
            if os.name == "nt":  # вместе с ffmpeg, который запустил процесс монтажа
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=0x08000000)
            else:
                p.kill()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def delete_job(self, job_id, with_video=False):
        """«Удалить проект»: папка задания целиком. with_video — ещё и готовый файл видео (job.json → result)."""
        name = os.path.basename(str(job_id or ""))
        d = os.path.join(data_dir(), "jobs", name)
        if not name or not os.path.isdir(d):
            return {"ok": False, "error": "Папка этого ролика не найдена."}
        with LOCK:
            if assembly_busy() and os.path.normcase(ASSEMBLY.get("job_dir") or "") == os.path.normcase(d):
                return {"ok": False, "error": "Этот ролик сейчас монтируется в фоне — дождитесь конца или остановите монтаж."}
            if (STATE["running"] or STATE.get("queue_active")) and STATE.get("job_dir") and \
                    os.path.normcase(STATE["job_dir"]) == os.path.normcase(d):
                return {"ok": False, "error": "Этот ролик сейчас делается — сначала нажмите «Стоп»."}
        freed, video_error = pipeline.dir_size(d), None
        if with_video:
            res = (pipeline.read_json(os.path.join(d, "job.json"), {}) or {}).get("result")
            if res and os.path.isfile(res) and res.lower().endswith((".mp4", ".mov", ".mkv")):
                try:
                    freed += os.path.getsize(res)
                    os.remove(res)
                except OSError:
                    video_error = "Видео не удалилось — возможно, оно открыто в плеере."
        shutil.rmtree(d, ignore_errors=True)
        return {"ok": not os.path.isdir(d), "freed_mb": round(freed / 1048576), "video_error": video_error,
                "error": None if not os.path.isdir(d) else "Не удалось удалить папку (открыта в другой программе?)."}

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

    def js_error(self, msg):
        """Ошибка в окне (JavaScript) — в журнал программы, чтобы её было видно в отчёте."""
        pipeline.log(f"ошибка окна: {str(msg)[:600]}")
        return True

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
                                                      "при следующем запуске того же mp3 работа продолжится."
                                          + (" Ролик, который монтируется в фоне, доделается сам." if assembly_busy() else ""))
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
    if "--assemble" in sys.argv:  # фоновый монтаж ролика из очереди — без окна, отдельным процессом
        try:
            pipeline.assemble_from_spec(sys.argv[sys.argv.index("--assemble") + 1])
        except BaseException:
            traceback.print_exc()
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
