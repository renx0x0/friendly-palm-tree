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
SOURCES = ("pexels", "pixabay", "openverse", "commons", "archive_org")
SOURCE_NAMES = {"pexels": "Pexels", "pixabay": "Pixabay", "openverse": "Openverse", "commons": "Wikimedia Commons (архив)",
                "archive_org": "Internet Archive (хроника)"}
ARCHIVES = ("archive_org", "commons")   # архив: для исторических сцен — первыми, для остальных — последними
NO_KEY = ("openverse", "commons", "archive_org")
ARCHIVE_SECONDS = 5.0                    # из старого фильма берём кусок не длиннее 5 с
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
    on = bool(raw.get("on", share > 0))  # галочка «Использовать стоки»: выключили — настройки помнятся, но не применяются
    return {"on": on, "share": share, "media": media, "sources": src, "archive": bool(raw.get("archive", "commons" in src)),
            "verify": bool(raw.get("verify", True)), "select": bool(raw.get("select", True)), "active": on and share > 0}


def keys_of(cfg):
    return {s: str(cfg.get(k) or "").strip() for s, k in KEYS.items()}


def usable_sources(st, keys):
    """Источники, которыми можно пользоваться: у Pexels и Pixabay нужен ключ, у Commons — нет."""
    out = [s for s in st["sources"] if s in NO_KEY or keys.get(s)]
    if st["archive"]:
        out += [a for a in ARCHIVES if a not in out]
    else:
        out = [s for s in out if s not in ARCHIVES]
    return out


def source_order(srcs, historic):
    rest = [x for x in srcs if x not in ARCHIVES]
    arch = [x for x in ARCHIVES if x in srcs]
    return arch + rest if historic else rest + arch


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
    "You pick stock footage for a narrated video. For each scene you get its narration sentence and the image prompt, "
    "and the main topic of the whole video. Return a short English search query for stock sites (2-5 words, concrete visible things: "
    "objects, places, actions; no names of people, no years, no style words like cinematic or vintage). "
    "IMPORTANT: if the sentence is abstract or general, keep the main subject of the video topic in the query "
    "(topic 'hedgehog': 'hedgehog garden', not 'winter preparation'). "
    "Also set historic=true if the scene shows the past (before ~1970: old photos, newsreels, archive), else false. "
    'Answer JSON: {"items": [{"i": <scene number>, "q": "<query>", "historic": true|false}]}'
)


def make_queries(llm, scenes, log=print, topic=""):
    """{index: (query, historic)} — пачками по 40 сцен; модель не ответила — запрос из промпта."""
    out = {}
    for b in range(0, len(scenes), 40):
        chunk = scenes[b:b + 40]
        listing = (f"Video topic: {topic[:400]}\n" if topic else "") + "\n".join(f"{s['index'] + 1}. narration: {str(s.get('text') or '')[:300]} | prompt: {str(s.get('prompt') or '')[:300]}"
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


def _slug_desc(url):
    """Описание из ссылки Pexels: .../video/brown-hedgehog-in-the-grass-12345/ → «brown hedgehog in the grass»."""
    m = re.search(r"/(?:video|photo)/([^/]+?)-?\d*/?$", str(url or ""))
    return re.sub(r"[-_]+", " ", re.sub(r"-\d+$", "", m.group(1))).strip() if m else ""


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
                        "dur": float(v.get("duration") or 0), "page": v.get("url"), "author": (v.get("user") or {}).get("name", ""),
                        "desc": _slug_desc(v.get("url"))})
        return out
    data = _get("https://api.pexels.com/v1/search", {"query": q, "orientation": "landscape", "per_page": 15}, {"Authorization": key})
    return [{"src": "pexels", "id": str(p.get("id")), "kind": "photo", "url": (p.get("src") or {}).get("large2x") or (p.get("src") or {}).get("original"),
             "w": p.get("width"), "h": p.get("height"), "page": p.get("url"), "author": p.get("photographer", ""),
             "desc": " ".join(x for x in (str(p.get("alt") or ""), _slug_desc(p.get("url"))) if x)}
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
                        "dur": float(v.get("duration") or 0), "page": v.get("pageURL"), "author": v.get("user", ""), "desc": str(v.get("tags") or "")})
        return out
    data = _get("https://pixabay.com/api/", {"key": key, "q": q[:100], "image_type": "photo", "orientation": "horizontal", "per_page": 20,
                                             "safesearch": "true"})
    return [{"src": "pixabay", "id": str(p.get("id")), "kind": "photo", "url": p.get("largeImageURL"), "w": p.get("imageWidth"),
             "h": p.get("imageHeight"), "page": p.get("pageURL"), "author": p.get("user", ""), "desc": str(p.get("tags") or "")}
            for p in data.get("hits") or [] if p.get("largeImageURL")]


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
                        "dur": float(ii.get("duration") or 0), "page": ii.get("descriptionurl"), "author": lic, "desc": str(p.get("title") or "")[5:]})
        else:
            out.append({"src": "commons", "id": str(p.get("pageid")), "kind": "photo", "url": ii.get("thumburl") or ii.get("url"),
                        "w": w, "h": h, "page": ii.get("descriptionurl"), "author": lic, "desc": str(p.get("title") or "")[5:]})
    return out


def search_openverse(q, kind):
    """Openverse (WordPress): свободные фото — только CC0 и общественное достояние, ключ не нужен."""
    if kind != "photo":
        return []
    data = _get("https://api.openverse.org/v1/images/", {"q": q[:200], "license": "cc0,pdm", "aspect_ratio": "wide", "page_size": 20})
    out = []
    for r in data.get("results") or []:
        w, h = r.get("width") or 0, r.get("height") or 0
        if (w and w < 1000) or (w and h and w < h) or not r.get("url"):
            continue
        out.append({"src": "openverse", "id": str(r.get("id")), "kind": "photo", "url": r["url"], "w": w, "h": h,
                    "page": r.get("foreign_landing_url"), "author": str(r.get("license") or ""),
                    "desc": " ".join([str(r.get("title") or "")] + [str(t.get("name") or "") for t in (r.get("tags") or [])[:8]])})
    return out


def _secs(v):
    """Длительность из метаданных archive.org: «623.4» или «10:23» / «1:02:03»."""
    try:
        if ":" in str(v):
            t = 0.0
            for part in str(v).split(":"):
                t = t * 60 + float(part)
            return t
        return float(v or 0)
    except ValueError:
        return 0.0


def search_archive_org(q, kind):
    """Internet Archive: старые фильмы и кинохроника в общественном достоянии (Prelinger и др.).
    Фильм длинный — берём из него кусок до ARCHIVE_SECONDS с (3 места на фильм), качаем только этот кусок."""
    if kind != "video":
        return []
    from urllib.parse import quote
    data = _get("https://archive.org/advancedsearch.php", {
        "q": f"({q}) AND mediatype:movies AND (collection:prelinger OR licenseurl:*publicdomain*)",
        "fl[]": ["identifier", "title"], "rows": 10, "output": "json"})
    out = []
    for d in ((data.get("response") or {}).get("docs") or [])[:5]:
        ident, title = d.get("identifier"), str(d.get("title") or "")
        if not ident:
            continue
        try:
            md = _get(f"https://archive.org/metadata/{quote(ident)}")
        except Exception:
            continue
        mp4 = [f for f in md.get("files") or [] if str(f.get("name", "")).lower().endswith(".mp4")]
        if not mp4:
            continue
        f = max(mp4, key=lambda f: int(f.get("size") or 0))
        w, h = int(float(f.get("width") or 0)), int(float(f.get("height") or 0))
        if w and h and w < h:
            continue
        length = _secs(f.get("length")) or _secs((md.get("metadata") or {}).get("runtime"))
        url = f"https://archive.org/download/{quote(ident)}/{quote(f['name'])}"
        spots = [length * x for x in (0.2, 0.45, 0.7)] if length > 3 * ARCHIVE_SECONDS else [0.0]
        for st in spots:
            out.append({"src": "archive_org", "id": f"{ident}@{int(st)}", "kind": "video", "url": url, "seg": [round(st, 1), ARCHIVE_SECONDS],
                        "dur": ARCHIVE_SECONDS, "w": w, "h": h, "page": f"https://archive.org/details/{ident}", "author": "public domain",
                        "desc": title})
    return out


def search(source, keys, q, kind):
    if source == "pexels":
        return search_pexels(keys["pexels"], q, kind)
    if source == "pixabay":
        return search_pixabay(keys["pixabay"], q, kind)
    if source == "openverse":
        return search_openverse(q, kind)
    if source == "archive_org":
        return search_archive_org(q, kind)
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
    if cand.get("seg"):  # кусок из длинного фильма (архив): ffmpeg берёт только его, весь фильм не качаем
        ss, t = cand["seg"]
        tmp = vid_path + ".part.mp4"
        r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{float(ss):.2f}", "-t", f"{float(t):.2f}", "-user_agent", UA, "-i", cand["url"],
                            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                            "-vf", "scale='min(1920,iw)':-2", tmp], capture_output=True, creationflags=creationflags, timeout=300)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 2000:
            raise RuntimeError("ffmpeg: " + r.stderr.decode(errors="replace")[:200])
        os.replace(tmp, vid_path)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{float(t) / 2:.2f}", "-i", vid_path, "-frames:v", "1", img_path],
                       capture_output=True, creationflags=creationflags)
        return
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


# ---------------------------------------------------------------- проверка кадра нейросетью-зрителем

VERIFY_SYSTEM = (
    "Ты подбираешь стоковый кадр для видеоролика. Тебе дано предложение закадрового текста, описание желаемого кадра и сам кадр. "
    "Реши, годится ли кадр как иллюстрация к этому предложению. ПОДХОДИТ (ok=true), если по смыслу та же тема: те же предметы, место, "
    "действие или эпоха; точного совпадения мелких деталей не нужно. НЕ ПОДХОДИТ (ok=false), если: на кадре другая тема, другой предмет или место; "
    "виден крупный текст, логотип или водяной знак; современная съёмка там, где речь явно об истории (и наоборот); кадр случайный или абстрактный. "
    'Ответ строго JSON: {"ok": true|false, "why": "по-русски, до 10 слов"}')


def verify_still(client, scene, still_path):
    """→ (ok, почему). Любая ошибка проверки — кадр годится (проверка не должна валить ролик)."""
    try:
        user = (f"Закадровый текст: {str(scene.get('text') or '')[:300]}\n"
                f"Описание желаемого кадра: {str(scene.get('raw_prompt') or scene.get('prompt') or '')[:300]}\nОцени кадр.")
        j = client.see_json(VERIFY_SYSTEM, user, still_path)
        return bool(j.get("ok", True)), str(j.get("why") or "")[:80]
    except Exception:
        return True, ""


# ---------------------------------------------------------------- отбор по описанию клипа (без картинок и ключа зрителя)

CHOOSE_SYSTEM = (
    "Ты подбираешь стоковый клип для видеоролика. Дано: тема ролика, предложение закадрового текста и список кандидатов — у каждого "
    "только текстовое описание (теги или название). Выбери кандидатов, которые по описанию подходят как иллюстрация к ЭТОМУ предложению "
    "в рамках темы ролика: на кадре должно быть то, о чём говорится, или близкая по смыслу сцена из той же темы. "
    "НЕ подходят: другая тема (спорт, телефоны, технологии, города, море, если ролик про другое), абстрактные фоны, «зелёный экран», "
    "случайные кадры, не связанные с предложением. Лучше не выбрать ничего, чем взять не то. "
    'Ответ строго JSON: {"fit": [номера кандидатов от самого подходящего; пустой список, если ничего не подходит]}')


def _words(text):
    return [w for w in re.findall(r"[a-z]{3,}", str(text or "").lower())]


def lexical_ok(query, desc):
    """Без модели: у клипа есть описание — в нём должно встретиться хотя бы одно слово запроса (по первым 5 буквам)."""
    if not str(desc or "").strip():
        return True
    d = {w[:5] for w in _words(desc)}
    return any(w[:5] in d for w in _words(query) if len(w) > 3)


def make_chooser(llm, topic="", log=print):
    """choose(scene, query, cands) → кандидаты, годные по описанию, лучшие первыми. Нет модели — отсев по словам запроса."""
    def choose(scene, query, cands):
        cands = [c for c in cands if lexical_ok(query, c.get("desc"))]
        if llm is None or not cands:
            return cands
        cands = cands[:8]
        listing = "\n".join(f"{k + 1}. {str(c.get('desc') or '(без описания)')[:140]}" for k, c in enumerate(cands))
        user = (f"Тема ролика: {topic[:300]}\nПредложение: {str(scene.get('text') or '')[:300]}\nПоисковый запрос: {query}\nКандидаты:\n{listing}")
        try:
            resp = llm.chat_json(CHOOSE_SYSTEM, user, max_tokens=120)
            fit = [int(x) - 1 for x in (resp or {}).get("fit") or [] if str(x).strip().lstrip("-").isdigit()]
            return [cands[k] for k in fit if 0 <= k < len(cands)]
        except Exception as e:
            log(f"стоки: отбор по описанию не ответил ({str(e)[:100]}) — беру по порядку")
            return cands
    return choose


# ---------------------------------------------------------------- подбор: кандидаты → скачать → проверить

_REC_LOCK = threading.Lock()


def _rm(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


class _Run:
    """Общее состояние одного прогона (кэш поиска, занятые клипы, негодные источники) — для параллельной работы по сценам."""

    def __init__(self, cfg, data_dir, log=print, ffmpeg="ffmpeg", creationflags=0, verify=None, choose=None):
        self.st = settings(cfg)
        self.keys = keys_of(cfg)
        self.srcs = usable_sources(self.st, self.keys)
        self.data_dir, self.log, self.ffmpeg, self.flags, self.verify, self.choose = data_dir, log, ffmpeg, creationflags, verify, choose
        self.lock = threading.Lock()
        self.cache, self.dead = {}, set()
        self.used = set(load_used(data_dir))

    def _search(self, src, q, kind):
        ck = (src, q, kind)
        with self.lock:
            if ck in self.cache:
                return self.cache[ck]
        try:
            res = search(src, self.keys, q, kind)
        except Exception as e:
            self.log(f"стоки: {SOURCE_NAMES[src]} — {str(e)[:120]}")
            if "401" in str(e) or "403" in str(e):
                with self.lock:
                    self.dead.add(src)  # ключ не подходит — дальше этот сток не спрашиваем
            res = []
        with self.lock:
            self.cache[ck] = res
        return res

    def candidates(self, q, historic, dur):
        kinds = {"video": ["video"], "photo": ["photo"], "both": ["video", "photo"]}[self.st["media"]]
        for kind in kinds:  # сначала видео со всех источников, потом фото
            for src in source_order(self.srcs, historic):
                if src in self.dead:
                    continue
                cands = [c for c in self._search(src, q, kind) if c.get("url")]
                if kind == "video":
                    cands = [c for c in cands if (c.get("dur") or 0) >= dur] or cands
                yield from cands

    def fetch(self, scene, q, historic, tmp_dir, skip=(), tries=3):
        """→ (cand, tmp_video, tmp_image) или None. Файлы лежат во временной папке, пока кадр не проверен."""
        i = scene["index"]
        os.makedirs(tmp_dir, exist_ok=True)
        tv, ti = os.path.join(tmp_dir, f"{i:04d}.mp4"), os.path.join(tmp_dir, f"{i:04d}.png")
        n = 0
        stream = self.candidates(q, historic, float(scene.get("duration") or 0))
        if self.st.get("select", True):
            pool = []
            for c in stream:
                key = f"{c['src']}:{c['id']}"
                with self.lock:
                    if key in self.used or key in skip:
                        continue
                pool.append(c)
                if len(pool) >= 12:
                    break
            if self.choose is not None:
                stream = self.choose(scene, q, pool)
            else:
                stream = [c for c in pool if lexical_ok(q, c.get("desc"))]
            if not stream:
                self.log(f"стоки: сцена {i + 1} «{q}» — по описаниям ничего подходящего")
                return None
        for c in stream:
            key = f"{c['src']}:{c['id']}"
            with self.lock:
                if key in self.used or key in skip:
                    continue
                self.used.add(key)  # занят сразу: параллельные сцены не возьмут тот же клип
            n += 1
            try:
                if c["kind"] == "video":
                    save_video(c, tv, ti, self.ffmpeg, self.flags)
                else:
                    save_photo(c, ti)
            except Exception as e:
                self.log(f"стоки: сцена {i + 1} — не скачалось ({str(e)[:100]})")
                _rm(tv, ti)
                if n >= tries:
                    break
                continue
            if self.verify is not None and os.path.exists(ti):
                ok, why = self.verify(scene, ti)
                if not ok:
                    self.log(f"стоки: сцена {i + 1} «{q}» — кадр не подходит ({why or 'не по теме'}), беру следующий")
                    _rm(tv, ti)
                    if n >= tries:
                        break
                    continue
            return c, tv, ti
        return None


def _install(job_dir, i, cand, tv, ti):
    img_dir, vid_dir = os.path.join(job_dir, "images"), os.path.join(job_dir, "videos")
    os.makedirs(img_dir, exist_ok=True)
    os.replace(ti, os.path.join(img_dir, f"{i:04d}.png"))
    if cand["kind"] == "video":
        os.makedirs(vid_dir, exist_ok=True)
        os.replace(tv, os.path.join(vid_dir, f"{i:04d}.mp4"))
    else:
        _rm(tv)


def _commit(job_dir, data_dir, i, cand, q, historic, tried=()):
    mark_used(data_dir, cand, os.path.basename(job_dir))
    with _REC_LOCK:
        rec = record(job_dir)
        rec[str(i)] = {"src": cand["src"], "id": cand["id"], "kind": cand["kind"], "q": q, "page": cand.get("page"),
                       "author": cand.get("author"), "historic": bool(historic), "tried": list(tried)[-30:]}
        _save_rec(job_dir, rec)
        _set_img_state(job_dir, i, {"ok": True, "model": f"stock:{cand['src']}:{cand['kind']}"})


# ---------------------------------------------------------------- главное

def fill(cfg, scenes, job_dir, data_dir, llm=None, progress=None, log=print, ffmpeg="ffmpeg", creationflags=0, check_cancel=None, verify=None, topic=""):
    """Подобрать стоки для части сцен. → {index: путь к видео со стока}. Фото со стока ложатся в images как обычные картинки.
    verify(scene, still) → (ok, why): нейросеть-зритель смотрит кадр; не подошёл — следующий кандидат, а если все не подошли — сцена генерируется."""
    topic = topic or " ".join(str(s.get("text") or "") for s in scenes[:12])[:400]
    run = _Run(cfg, data_dir, log, ffmpeg, creationflags, verify, make_chooser(llm, topic, log))
    st = run.st
    if not st["active"]:
        return {}
    if not run.srcs:
        log("стоки: нет ключа ни Pexels, ни Pixabay и не включены Openverse / архив — сцены будут сгенерированы")
        return {}
    img_dir, vid_dir = os.path.join(job_dir, "images"), os.path.join(job_dir, "videos")
    os.makedirs(img_dir, exist_ok=True)
    rec = record(job_dir)
    stock_vids = {int(i): os.path.join(vid_dir, f"{int(i):04d}.mp4") for i, r in rec.items()
                  if r.get("kind") == "video" and os.path.exists(os.path.join(vid_dir, f"{int(i):04d}.mp4"))}
    want = set(pick_scenes(scenes, st["share"]))
    todo = [s for s in scenes if s["index"] in want and str(s["index"]) not in rec
            and not os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png"))]
    if not todo:
        return stock_vids
    say = (lambda d, n, m: progress("images", d, n, m)) if progress else (lambda *a: None)
    say(0, len(todo), f"стоки: подбираю запросы для {len(todo)} сцен ({', '.join(SOURCE_NAMES[s] for s in run.srcs)})"
        + (", кадры проверяет нейросеть-зритель" if verify else ""))
    queries = make_queries(llm, todo, log, topic)
    tmp_dir = os.path.join(job_dir, "_stk")
    counter = {"done": 0, "got": 0}

    def work(s):
        if check_cancel:
            check_cancel()
        i = s["index"]
        q, historic = queries.get(i) or (fallback_query(s), False)
        if not q:
            return
        r = run.fetch(s, q, historic, tmp_dir)
        if r is None:
            log(f"стоки: сцена {i + 1} «{q}» — подходящего не нашлось, будет генерация")
        else:
            cand, tv, ti = r
            _install(job_dir, i, cand, tv, ti)
            _commit(job_dir, data_dir, i, cand, q, historic)
            with run.lock:
                counter["got"] += 1
                if cand["kind"] == "video":
                    stock_vids[i] = os.path.join(vid_dir, f"{i:04d}.mp4")
        with run.lock:
            counter["done"] += 1
            d = counter["done"]
        if r is not None:
            say(d, len(todo), f"стоки: сцена {i + 1} — {SOURCE_NAMES[r[0]['src']]}, {'видео' if r[0]['kind'] == 'video' else 'фото'} «{q}»")

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for f in [ex.submit(work, s) for s in todo]:
            f.result()
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass
    log(f"стоки: взято {counter['got']} из {len(todo)} сцен, остальные будут сгенерированы")
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


# ---------------------------------------------------------------- замена одного кадра из окна просмотра

def record(job_dir):
    try:
        with open(os.path.join(job_dir, "stock.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_rec(job_dir, rec):
    with open(os.path.join(job_dir, "stock.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)


def _set_img_state(job_dir, i, val):
    p = os.path.join(job_dir, "images.json")
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if val is None:
        st.pop(str(i), None)
    else:
        st[str(i)] = val
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def replace(cfg, job_dir, data_dir, scene, query=None, ffmpeg="ffmpeg", creationflags=0, log=print, clear=None, verify=None, historic=None, llm=None, topic=""):
    """Заменить кадр сцены кадром со стока: по запросу сцены (или своему), без уже взятых. Сцена может быть и стоковой, и сгенерированной.
    Старые файлы сцены (clear) убираются только когда новый кадр найден, скачан и подошёл — иначе остаётся как было."""
    run = _Run(cfg, data_dir, log, ffmpeg, creationflags, verify, make_chooser(llm, topic, log))
    if not run.srcs:
        return {"ok": False, "error": "Нет ключа ни Pexels, ни Pixabay и не включены Openverse / архив — откройте «Стоки» на шаге 02."}
    i = scene["index"]
    old = record(job_dir).get(str(i)) or {}
    q = " ".join(str(query or old.get("q") or fallback_query(scene)).split())[:80]
    if not q:
        return {"ok": False, "error": "Пустой запрос."}
    hist = bool(old.get("historic")) if historic is None else bool(historic)
    skip = set(old.get("tried") or [])
    if old.get("src"):
        skip.add(f"{old['src']}:{old['id']}")
    r = run.fetch(scene, q, hist, os.path.join(job_dir, "_stk"), skip)
    if r is None:
        return {"ok": False, "error": f"По запросу «{q}» ничего подходящего не нашлось — кадр оставлен как есть. Попробуйте свой запрос."}
    cand, tv, ti = r
    if clear:
        clear(i)
    _install(job_dir, i, cand, tv, ti)
    tried = list(old.get("tried") or []) + ([f"{old['src']}:{old['id']}"] if old.get("src") else [])
    _commit(job_dir, data_dir, i, cand, q, hist, tried)
    try:
        os.rmdir(os.path.join(job_dir, "_stk"))
    except OSError:
        pass
    return {"ok": True, "src": SOURCE_NAMES[cand["src"]], "kind": cand["kind"], "q": q}


def to_generation(job_dir, i, clear=None):
    """Убрать сток со сцены: при следующем запуске она будет сгенерирована (и стоки её больше не трогают)."""
    rec = record(job_dir)
    rec[str(i)] = {"skip": True}
    _save_rec(job_dir, rec)
    if clear:
        clear(i)
    _set_img_state(job_dir, i, None)
    return {"ok": True}
