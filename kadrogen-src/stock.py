"""Стоки: живые видео и фото с Pexels / Pixabay и архивные снимки и хроника с Wikimedia Commons — вместо генерации
для части сцен (или для всех).

Для каждой выбранной сцены модель текстов пишет короткий английский поисковый запрос по её предложению и промпту
(и отмечает, историческая ли это сцена). Программа ищет на стоках горизонтальное видео не короче сцены или фото,
скачивает лучшее и кладёт его в проект как кадр сцены: фото → images/NNNN.png (дальше как обычная картинка: движение,
эффекты), видео → videos/NNNN.mp4 (как оживлённый клип: обрезка под сцену, наезд на хвост) + кадр из него в images.
Не нашлось — сцена генерируется как обычно. Использованные клипы запоминаются (data/stock_used.json) и не повторяются.

Лицензии: Pexels и Pixabay — бесплатно для своих роликов, в т.ч. монетизированных, без указания автора.
С Commons берутся только файлы в общественном достоянии (Public domain / CC0), чтобы не нужно было указывать авторов.
"""
import json
import os
import re
import subprocess
import threading
import time

import requests

UA = "KadroGen/1.7 (stock footage for the user's own videos)"
SOURCES = ("pexels", "pixabay", "commons")
SOURCE_NAMES = {"pexels": "Pexels", "pixabay": "Pixabay", "commons": "Wikimedia Commons (архив)"}
KEYS = {"pexels": "pexels_api_key", "pixabay": "pixabay_api_key"}
_USED_LOCK = threading.Lock()


def settings(cfg):
    """cfg["stock"] → {"share" 0..100, "media" both|video|photo, "sources" [...], "archive" bool}."""
    raw = cfg.get("stock") if isinstance(cfg.get("stock"), dict) else {}
    try:
        share = max(0, min(100, int(round(float(raw.get("share") or 0)))))
    except (TypeError, ValueError):
        share = 0
    media = raw.get("media") if raw.get("media") in ("both", "video", "photo") else "both"
    src = [s for s in (raw.get("sources") or ["pexels", "pixabay"]) if s in SOURCES]
    return {"share": share, "media": media, "sources": src, "archive": bool(raw.get("archive", "commons" in src))}


def keys_of(cfg):
    return {s: str(cfg.get(k) or "").strip() for s, k in KEYS.items()}


def usable_sources(st, keys):
    """Источники, которыми можно пользоваться: у Pexels и Pixabay нужен ключ, у Commons — нет."""
    out = [s for s in st["sources"] if s == "commons" or keys.get(s)]
    if st["archive"] and "commons" not in out:
        out.append("commons")
    return out


def pick_scenes(scenes, share):
    """Какие сцены берём со стоков: share % равномерно по ролику (100 — все)."""
    if share >= 100:
        return [s["index"] for s in scenes]
    if share <= 0:
        return []
    out = []
    for k, s in enumerate(scenes):
        if int((k + 1) * share / 100) > int(k * share / 100):
            out.append(s["index"])
    return out


# ---------------------------------------------------------------- запросы

_STOP = set("a an the of in on at to with and or for from by into over under near is are was were be this that these those "
            "his her their its very ultra realistic photo photograph image cinematic shot frame scene style film black white "
            "sepia vintage 16 9 1920 1080 hd 4k 8k detailed high quality no modern camera movement wide general view".split())


def fallback_query(scene):
    """Без модели текстов: первые значимые английские слова промпта."""
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", str(scene.get("prompt") or ""))
    keep = [w.lower() for w in words if w.lower() not in _STOP and len(w) > 2][:5]
    return " ".join(keep)


QUERY_SYSTEM = (
    "You pick stock footage for a narrated video. For each scene you get its narration sentence and the image prompt. "
    "Return a short English search query for stock sites (2-5 words, concrete visible things: objects, places, actions; "
    "no names of people, no years, no style words like cinematic or vintage), and historic=true if the scene shows the past "
    "(before ~1970: old photos, newsreels, archive), else false. "
    'Answer JSON: {"items": [{"i": <scene number>, "q": "<query>", "historic": true|false}]}'
)


def make_queries(llm, scenes, log=print):
    """{index: (query, historic)} — пачками по 40 сцен; модель не ответила — запрос из промпта."""
    out = {}
    for b in range(0, len(scenes), 40):
        chunk = scenes[b:b + 40]
        listing = "\n".join(f"{s['index'] + 1}. narration: {str(s.get('text') or '')[:300]} | prompt: {str(s.get('prompt') or '')[:300]}"
                            for s in chunk)
        got = {}
        if llm is not None:
            try:
                resp = llm.chat_json(QUERY_SYSTEM, listing, max_tokens=60 * len(chunk) + 300)
                for it in (resp or {}).get("items") or []:
                    try:
                        i = int(it.get("i")) - 1
                    except (TypeError, ValueError):
                        continue
                    q = " ".join(str(it.get("q") or "").split())[:80]
                    if q:
                        got[i] = (q, bool(it.get("historic")))
            except Exception as e:
                log(f"стоки: запросы от модели не получены ({str(e)[:120]}) — беру слова из промптов")
        for s in chunk:
            out[s["index"]] = got.get(s["index"]) or (fallback_query(s), False)
    return out


# ---------------------------------------------------------------- поиск

def _get(url, params=None, headers=None, timeout=25):
    h = {"User-Agent": UA}
    h.update(headers or {})
    r = requests.get(url, params=params, headers=h, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError("лимит запросов стока (429)")
    r.raise_for_status()
    return r.json()


def search_pexels(key, q, kind):
    if kind == "video":
        data = _get("https://api.pexels.com/videos/search", {"query": q, "orientation": "landscape", "per_page": 15, "size": "medium"},
                    {"Authorization": key})
        out = []
        for v in data.get("videos") or []:
            files = [f for f in v.get("video_files") or [] if (f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 1280
                                                                and (f.get("width") or 0) >= (f.get("height") or 1))]
            if not files:
                continue
            f = min(files, key=lambda f: abs((f.get("width") or 0) - 1920))
            out.append({"src": "pexels", "id": str(v.get("id")), "kind": "video", "url": f["link"], "w": f.get("width"), "h": f.get("height"),
                        "dur": float(v.get("duration") or 0), "page": v.get("url"), "author": (v.get("user") or {}).get("name", "")})
        return out
    data = _get("https://api.pexels.com/v1/search", {"query": q, "orientation": "landscape", "per_page": 15}, {"Authorization": key})
    return [{"src": "pexels", "id": str(p.get("id")), "kind": "photo", "url": (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("original"),
             "w": p.get("width"), "h": p.get("height"), "page": p.get("url"), "author": p.get("photographer", "")}
            for p in data.get("photos") or [] if (p.get("width") or 0) >= (p.get("height") or 1)]


def search_pixabay(key, q, kind):
    if kind == "video":
        data = _get("https://pixabay.com/api/videos/", {"key": key, "q": q[:100], "per_page": 20, "safesearch": "true"})
        out = []
        for v in data.get("hits") or []:
            vs = v.get("videos") or {}
            f = next((vs[k] for k in ("large", "medium") if (vs.get(k) or {}).get("url") and (vs[k].get("width") or 0) >= 1280), None)
            if not f or (f.get("width") or 0) < (f.get("height") or 1):
                continue
            out.append({"src": "pixabay", "id": str(v.get("id")), "kind": "video", "url": f["url"], "w": f.get("width"), "h": f.get("height"),
                        "dur": float(v.get("duration") or 0), "page": v.get("pageURL"), "author": v.get("user", "")})
        return out
    data = _get("https://pixabay.com/api/", {"key": key, "q": q[:100], "image_type": "photo", "orientation": "horizontal", "per_page": 20,
                                             "safesearch": "true"})
    return [{"src": "pixabay", "id": str(p.get("id")), "kind": "photo", "url": p.get("largeImageURL"), "w": p.get("imageWidth"),
             "h": p.get("imageHeight"), "page": p.get("pageURL"), "author": p.get("user", "")} for p in data.get("hits") or [] if p.get("largeImageURL")]


_PD = re.compile(r"^(public domain|pd\b|pd-|cc0|cc-zero|no restrictions)", re.I)


def search_commons(q, kind):
    """Wikimedia Commons — только общественное достояние (Public domain / CC0): архивные фото и хроника."""
    ft = "video" if kind == "video" else "bitmap"
    data = _get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{q} filetype:{ft}", "gsrnamespace": 6,
        "gsrlimit": 20, "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 1920})
    out = []
    for p in ((data.get("query") or {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = str((meta.get("LicenseShortName") or {}).get("value") or "")
        if not _PD.search(lic.strip()):
            continue
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if w < 800 or w < h:
            continue
        if kind == "video":
            if not str(ii.get("mime") or "").startswith("video/"):
                continue
            out.append({"src": "commons", "id": str(p.get("pageid")), "kind": "video", "url": ii.get("url"), "w": w, "h": h,
                        "dur": float(ii.get("duration") or 0), "page": ii.get("descriptionurl"), "author": lic})
        else:
            out.append({"src": "commons", "id": str(p.get("pageid")), "kind": "photo", "url": ii.get("thumburl") or ii.get("url"),
                        "w": w, "h": h, "page": ii.get("descriptionurl"), "author": lic})
    return out


def search(source, keys, q, kind):
    if source == "pexels":
        return search_pexels(keys["pexels"], q, kind)
    if source == "pixabay":
        return search_pixabay(keys["pixabay"], q, kind)
    return search_commons(q, kind)


# ---------------------------------------------------------------- использованные

def _used_path(data_dir):
    return os.path.join(data_dir, "stock_used.json")


def load_used(data_dir):
    try:
        with open(_used_path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_used(data_dir, cand, job):
    with _USED_LOCK:
        d = load_used(data_dir)
        d[f"{cand['src']}:{cand['id']}"] = {"job": job, "at": int(time.time())}
        tmp = _used_path(data_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _used_path(data_dir))


# ---------------------------------------------------------------- скачивание

def _download(url, path, timeout=120):
    tmp = path + ".part"
    with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    os.replace(tmp, path)
    return path


def save_photo(cand, img_path):
    from PIL import Image
    raw = img_path + ".dl"
    _download(cand["url"], raw)
    try:
        im = Image.open(raw).convert("RGB")
        if im.width > 2560:
            im = im.resize((2560, int(im.height * 2560 / im.width)), Image.LANCZOS)
        im.save(img_path)
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass


def save_video(cand, vid_path, img_path, ffmpeg, creationflags=0):
    """Клип в videos/NNNN.mp4 (не mp4 — перекодируем в h264) и его средний кадр в images/NNNN.png для окна."""
    ext = os.path.splitext(cand["url"].split("?")[0])[1].lower() or ".mp4"
    raw = vid_path + ".dl" + ext
    _download(cand["url"], raw, timeout=300)
    try:
        if ext == ".mp4":
            os.replace(raw, vid_path)
        else:
            r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", raw, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                                "-pix_fmt", "yuv420p", "-vf", "scale='min(1920,iw)':-2", vid_path], capture_output=True, creationflags=creationflags)
            if r.returncode != 0:
                raise RuntimeError("ffmpeg: " + r.stderr.decode(errors="replace")[:200])
        mid = max(0.0, min(float(cand.get("dur") or 2) / 2, 4.0))
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{mid:.2f}", "-i", vid_path, "-frames:v", "1", img_path],
                       capture_output=True, creationflags=creationflags)
    finally:
        if os.path.exists(raw):
            try:
                os.remove(raw)
            except OSError:
                pass


# ---------------------------------------------------------------- главное

def fill(cfg, scenes, job_dir, data_dir, llm=None, progress=None, log=print, ffmpeg="ffmpeg", creationflags=0, check_cancel=None):
    """Подобрать стоки для части сцен. → {index: путь к видео со стока}. Фото со стока ложатся в images как обычные картинки."""
    st = settings(cfg)
    if not st["share"]:
        return {}
    keys = keys_of(cfg)
    srcs = usable_sources(st, keys)
    if not srcs:
        log("стоки: нет ключа ни Pexels, ни Pixabay и архив выключен — сцены будут сгенерированы")
        return {}
    img_dir, vid_dir = os.path.join(job_dir, "images"), os.path.join(job_dir, "videos")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(vid_dir, exist_ok=True)
    rec_path = os.path.join(job_dir, "stock.json")
    try:
        with open(rec_path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = {}
    stock_vids = {int(i): os.path.join(vid_dir, f"{int(i):04d}.mp4") for i, r in rec.items()
                  if r.get("kind") == "video" and os.path.exists(os.path.join(vid_dir, f"{int(i):04d}.mp4"))}
    want = set(pick_scenes(scenes, st["share"]))
    todo = [s for s in scenes if s["index"] in want and str(s["index"]) not in rec
            and not os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png"))]
    if not todo:
        return stock_vids
    say = (lambda d, n, m: progress("images", d, n, m)) if progress else (lambda *a: None)
    say(0, len(todo), f"стоки: подбираю запросы для {len(todo)} сцен ({', '.join(SOURCE_NAMES[s] for s in srcs)})")
    queries = make_queries(llm, todo, log)
    used = set(load_used(data_dir))
    job = os.path.basename(job_dir)
    img_state_path = os.path.join(job_dir, "images.json")
    try:
        with open(img_state_path, encoding="utf-8") as f:
            img_state = json.load(f)
    except (OSError, ValueError):
        img_state = {}
    cache, got, dead = {}, 0, set()
    for k, s in enumerate(todo):
        if check_cancel:
            check_cancel()
        i = s["index"]
        q, historic = queries.get(i) or (fallback_query(s), False)
        if not q:
            continue
        dur = float(s.get("duration") or 0)
        order = [x for x in srcs if x != "commons"]
        if "commons" in srcs:  # историческая сцена — сначала архив, остальные — архив последним
            order = ["commons"] + order if historic else order + ["commons"]
        kinds = {"video": ["video", "photo"], "photo": ["photo"], "both": ["video", "photo"]}[st["media"]]
        pick = None
        for src in order:
            if src in dead:
                continue
            for kind in kinds:
                ck = (src, q, kind)
                try:
                    if ck not in cache:
                        cache[ck] = search(src, keys, q, kind)
                except Exception as e:
                    log(f"стоки: {SOURCE_NAMES[src]} — {str(e)[:120]}")
                    if "401" in str(e) or "403" in str(e):
                        dead.add(src)  # ключ не подходит — дальше этот сток не спрашиваем
                    cache[ck] = []
                cands = [c for c in cache[ck] if f"{c['src']}:{c['id']}" not in used and c.get("url")]
                if kind == "video":
                    longer = [c for c in cands if (c.get("dur") or 0) >= dur]
                    cands = longer or cands
                if cands:
                    pick = cands[0]
                    break
            if pick:
                break
        if not pick:
            log(f"стоки: сцена {i + 1} «{q}» — ничего не нашлось, будет генерация")
            continue
        img_path = os.path.join(img_dir, f"{i:04d}.png")
        vid_path = os.path.join(vid_dir, f"{i:04d}.mp4")
        try:
            if pick["kind"] == "video":
                save_video(pick, vid_path, img_path, ffmpeg, creationflags)
                stock_vids[i] = vid_path
            else:
                save_photo(pick, img_path)
        except Exception as e:
            log(f"стоки: сцена {i + 1} — не скачалось ({str(e)[:120]}), будет генерация")
            for p in (vid_path, img_path):
                if pick["kind"] == "video" and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            stock_vids.pop(i, None)
            continue
        used.add(f"{pick['src']}:{pick['id']}")
        mark_used(data_dir, pick, job)
        rec[str(i)] = {"src": pick["src"], "id": pick["id"], "kind": pick["kind"], "q": q, "page": pick.get("page"), "author": pick.get("author")}
        img_state[str(i)] = {"ok": True, "model": f"stock:{pick['src']}:{pick['kind']}"}
        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        with open(img_state_path, "w", encoding="utf-8") as f:
            json.dump(img_state, f, ensure_ascii=False)
        got += 1
        say(k + 1, len(todo), f"стоки: сцена {i + 1} — {SOURCE_NAMES[pick['src']]}, {'видео' if pick['kind'] == 'video' else 'фото'} «{q}»")
    log(f"стоки: взято {got} из {len(todo)} сцен, остальные будут сгенерированы")
    return stock_vids


def check_key(source, key):
    """Проверка ключа из окна: один пробный запрос."""
    try:
        if source == "pexels":
            search_pexels(key, "nature", "photo")
        elif source == "pixabay":
            search_pixabay(key, "nature", "photo")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
