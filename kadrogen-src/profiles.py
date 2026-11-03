"""Профили: свои мастер-промпты для кадров вместо встроенной раскадровки KadroGen.

Профиль — папка profiles/<Имя>/ рядом с программой:
    master_prompt.txt  — мастер-промпт с подстановками (см. PLACEHOLDERS)
    profile.json       — настройки (необязательно, всё имеет значения по умолчанию, см. DEFAULTS)

Как работает:
    1. Сценарий делится на части по концам предложений: частей = символы // chars_per_part, но не меньше min_parts.
    2. Каждая часть уходит в языковую модель вместе с мастер-промптом; ответ — нумерованные промпты и SYNC.
    3. Ответ проверяется (число промптов, номера подряд, SYNC = последнее предложение части), при ошибке — повтор.
    4. Границы частей находятся в распознанной озвучке, время каждой части делится поровну между её кадрами.

Модуль не знает про остальную программу: модель вызывается через переданную функцию chat(text) -> str.
"""
import bisect
import difflib
import hashlib
import json
import os
import re
import threading
import time
import concurrent.futures as cf

import requests

PLACEHOLDERS = {
    "{{PART_NUMBER}}": "номер части (1, 2, 3…)",
    "{{PARTS_TOTAL}}": "сколько всего частей",
    "{{PROMPTS_PER_PART}}": "сколько промптов на часть",
    "{{PROMPTS_TOTAL}}": "сколько промптов всего",
    "{{FIRST}}": "номер первого промпта этой части (001, 051…)",
    "{{LAST}}": "номер последнего промпта этой части (050, 100…)",
    "{{PART_TEXT}}": "текст части сценария",
}

DEFAULTS = {
    "chars_per_part": 4500,     # символов на часть
    "min_parts": 4,             # частей не меньше
    "max_parts": 0,             # частей не больше (0 — без предела)
    "prompts_per_part": 50,     # промптов на часть
    "number_width": 3,          # 001, 002… в {{FIRST}} / {{LAST}}
    "llm": "fastgen",           # fastgen | openai (любой OpenAI-совместимый адрес: openai_base_url в config.json)
    "llm_model": "",            # пусто — модель из настроек KadroGen
    "llm_fallback_model": "",   # запасная модель того же сервиса, если основная раз за разом падает (пусто — не переключать)
    "max_tokens": 32000,
    "reasoning_effort": "",     # low | medium | high — просить модель думать перед ответом (если сервис не поддерживает — без этого)
    "prompt_rules": [],         # проверка каждого промпта: [{"name", "if": "текст", "need": [...], "need_any": [...]}]
    "max_bad_prompts": 3,       # сколько промптов части может нарушить правила, прежде чем часть переписывается
    "temperature": 0.7,
    "parallel": 2,              # сколько частей писать одновременно
    "attempts": 3,              # сколько раз переспрашивать часть, если ответ не прошёл проверку
    "check_sync": True,         # сверять SYNC с последним предложением части
    "prompt_prefix": "",        # дописать в начало каждого промпта картинки
    "prompt_suffix": "",        # дописать в конец каждого промпта картинки
    "video_suffix": "",         # дописать в конец промпта оживления
    "motion": "slow gentle camera movement",  # движение для оживления кадра
    "voice": {},                # шаблон озвучки (следующий шаг)
}

MASTER_FILE = "master_prompt.txt"
SETTINGS_FILE = "profile.json"


class ProfileError(RuntimeError):
    pass


class Board:
    """Что сейчас происходит с частями — для панели в окне. Обновляется из потоков, читается окном раз в секунду."""
    LIVE_TAIL = 4000

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"profile": None, "parts": []}

    def reset(self, name=None, parts=None, per=50, first=1, prof=None, cache_dir=None, manual=False):
        with self.lock:
            self.prof, self.cache_dir, self.texts, self.prompts_done = prof, cache_dir, list(parts or []), False
            self.data = {"profile": name, "manual": bool(manual), "parts": [
                {"n": i + 1, "chars": len(p), "first": first + i * per, "per": per, "status": "wait", "started": None,
                 "ended": None, "attempt": 0, "count": 0, "live": "", "prompts": None, "problems": [], "note": "", "model": ""}
                for i, p in enumerate(parts or [])]}

    def update(self, n, **kw):
        with self.lock:
            for p in self.data["parts"]:
                if p["n"] == n:
                    p.update(kw)

    def summary(self):
        now = time.time()
        with self.lock:
            parts = []
            for p in self.data["parts"]:
                q = {k: v for k, v in p.items() if k not in ("prompts", "live")}
                q["elapsed"] = int(((p["ended"] or now) - p["started"])) if p["started"] else 0
                q["live"] = p["live"][-self.LIVE_TAIL:] if p["status"] == "writing" else ""
                q["ready"] = p["prompts"] is not None
                parts.append(q)
            return {"profile": self.data["profile"], "manual": self.data.get("manual", False),
                    "can_paste": bool(self.cache_dir and self.prof), "parts": parts}

    def part(self, n):
        with self.lock:
            for p in self.data["parts"]:
                if p["n"] == n:
                    return {"n": n, "first": p["first"], "prompts": list(p["prompts"] or []), "problems": list(p["problems"]),
                            "status": p["status"], "live": p["live"], "model": p.get("model", "")}
        return {"n": n, "prompts": [], "problems": [], "status": "wait", "live": ""}


    def request_text(self, n):
        """Запрос для чата: мастер-промпт с подставленной частью — ровно то, что ушло бы в модель."""
        with self.lock:
            prof, texts = self.prof, self.texts
        if not prof or not (1 <= n <= len(texts)):
            return None
        return fill_template(prof, n, len(texts), texts[n - 1])[0]

    def paste(self, n, text, dry=False):
        """Свой ответ из чата для части n: проверка как у ответа модели, сохранение в задание (dry — только проверить).
        → {"ok", "count", "problems", "need_restart"}"""
        with self.lock:
            prof, texts, cache_dir, done = self.prof, self.texts, self.cache_dir, self.prompts_done
        if not prof or not cache_dir or not (1 <= n <= len(texts)):
            return {"ok": False, "error": "Нет ролика с профилем: запустите ролик — части появятся в этой панели."}
        _, first, last = fill_template(prof, n, len(texts), texts[n - 1])
        prompts, sync = parse_reply(text)
        if not prompts:
            return {"ok": False, "error": "Не нашёл ни одного промпта. Нужны строки вида «001. Hand-drawn …»."}
        problems = check_reply(prompts, sync, first, last, texts[n - 1], prof["check_sync"],
                               prof.get("prompt_rules") or [], prof["max_bad_prompts"])
        if dry:
            return {"ok": True, "count": len(prompts), "problems": problems, "need_restart": done, "dry": True}
        os.makedirs(cache_dir, exist_ok=True)
        with open(manual_path(cache_dir, n), "w", encoding="utf-8") as f:
            f.write(text)
        try:  # старые промпты этой части больше не нужны — при следующем запуске возьмутся вставленные
            os.remove(os.path.join(cache_dir, f"part_{n:02d}.json"))
        except OSError:
            pass
        texts_ = [t for _, t in prompts]
        self.update(n, status="warn" if problems else "done", prompts=texts_, problems=problems, count=len(texts_),
                    note="свои промпты", model="вставлено вручную", live="", ended=time.time())
        return {"ok": True, "count": len(texts_), "problems": problems, "need_restart": done}


def manual_path(cache_dir, n):
    return os.path.join(cache_dir, f"part_{n:02d}_manual.txt")


BOARD = Board()


# ---------------------------------------------------------------- загрузка профиля

def profile_dirs(*bases):
    """Папки profiles/ в переданных местах (папка данных, папка программы) без повторов."""
    out, seen = [], set()
    for b in bases:
        if not b:
            continue
        d = os.path.normcase(os.path.abspath(os.path.join(b, "profiles")))
        if d not in seen:
            seen.add(d)
            out.append(os.path.join(b, "profiles"))
    return out


def list_profiles(dirs):
    names = set()
    for d in dirs:
        try:
            for n in os.listdir(d):
                if os.path.isfile(os.path.join(d, n, MASTER_FILE)):
                    names.add(n)
        except OSError:
            pass
    return sorted(names, key=str.lower)


def _read_text(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def load_profile(name, dirs):
    if not name:
        return None
    for d in dirs:
        pdir = os.path.join(d, name)
        if os.path.isfile(os.path.join(pdir, MASTER_FILE)):
            break
    else:
        raise ProfileError(f"Профиль «{name}» не найден: нужна папка profiles\\{name} с файлом {MASTER_FILE}")
    prof = dict(DEFAULTS)
    sp = os.path.join(pdir, SETTINGS_FILE)
    if os.path.isfile(sp):
        try:
            data = json.loads(_read_text(sp))
        except ValueError as e:
            raise ProfileError(f"Профиль «{name}»: ошибка в {SETTINGS_FILE}: {e}")
        if not isinstance(data, dict):
            raise ProfileError(f"Профиль «{name}»: {SETTINGS_FILE} должен быть объектом {{…}}")
        prof.update({k: v for k, v in data.items() if not k.startswith("_")})
    template = _read_text(os.path.join(pdir, MASTER_FILE)).strip()
    if not template:
        raise ProfileError(f"Профиль «{name}»: {MASTER_FILE} пустой")
    for k in ("chars_per_part", "min_parts", "max_parts", "prompts_per_part", "number_width", "max_tokens", "parallel", "attempts", "max_bad_prompts"):
        try:
            prof[k] = int(prof[k])
        except (TypeError, ValueError):
            raise ProfileError(f"Профиль «{name}»: {k} должно быть числом")
    if prof["chars_per_part"] < 200 or prof["prompts_per_part"] < 1:
        raise ProfileError(f"Профиль «{name}»: слишком маленькие chars_per_part или prompts_per_part")
    prof["min_parts"] = max(1, prof["min_parts"])
    prof["parallel"] = max(1, min(6, prof["parallel"]))
    prof["attempts"] = max(1, min(6, prof["attempts"]))
    prof.update({"name": name, "dir": pdir, "template": template})
    sig = json.dumps({k: prof[k] for k in sorted(prof) if k not in ("dir", "voice")}, ensure_ascii=False, sort_keys=True)
    prof["key"] = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
    return prof


# ---------------------------------------------------------------- редактор профилей (окно «Профили»)

EDIT_KEYS = ("chars_per_part", "min_parts", "max_parts", "prompts_per_part", "llm", "llm_model", "llm_fallback_model",
             "reasoning_effort", "temperature", "parallel", "attempts", "check_sync", "prompt_prefix", "prompt_suffix",
             "motion", "video_suffix", "transitions", "voice")
TRASH_DIR = "_удалённые"


def check_name(name):
    """Имя профиля = имя папки. → (имя, ошибка)."""
    n = " ".join(str(name or "").split())
    if not n:
        return n, "Введите название профиля."
    if len(n) > 60:
        return n, "Название длиннее 60 символов."
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', n) or n.startswith((".", "_")) or n.endswith("."):
        return n, 'В названии нельзя: < > : " / \\ | ? * и точку или _ в начале.'
    return n, None


def find_profile_dir(name, dirs):
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(os.path.join(p, MASTER_FILE)):
            return p
    return None


def read_profile_raw(name, dirs):
    """Профиль для окна: мастер-промпт и настройки (недостающие — по умолчанию)."""
    pdir = find_profile_dir(name, dirs)
    if not pdir:
        raise ProfileError(f"Профиль «{name}» не найден")
    data = {}
    sp = os.path.join(pdir, SETTINGS_FILE)
    if os.path.isfile(sp):
        try:
            data = json.loads(_read_text(sp)) or {}
        except ValueError:
            data = {}
    st = {k: data.get(k, DEFAULTS.get(k)) for k in EDIT_KEYS}
    if st.get("transitions") is None:
        st["transitions"] = ""
    elif isinstance(st["transitions"], dict):
        st["transitions"] = st["transitions"].get("preset", "")
    if not isinstance(st.get("voice"), dict):
        st["voice"] = {}
    rules = data.get("prompt_rules") or []
    return {"name": name, "master": _read_text(os.path.join(pdir, MASTER_FILE)), "settings": st, "dir": pdir,
            "rules": len(rules) if isinstance(rules, list) else 0}


def _clean_settings(st):
    out = {}
    ints = {"chars_per_part": (200, 200000), "min_parts": (1, 100), "max_parts": (0, 100), "prompts_per_part": (1, 1000),
            "parallel": (1, 6), "attempts": (1, 6)}
    for k, (lo, hi) in ints.items():
        if k in st:
            try:
                out[k] = max(lo, min(hi, int(float(st[k]))))
            except (TypeError, ValueError):
                raise ProfileError(f"«{k}» должно быть числом")
    if "temperature" in st:
        try:
            out["temperature"] = max(0.0, min(2.0, round(float(st["temperature"]), 2)))
        except (TypeError, ValueError):
            raise ProfileError("«temperature» должно быть числом")
    if "llm" in st:
        out["llm"] = st["llm"] if st["llm"] in ("fastgen", "openai") else "fastgen"
    if "reasoning_effort" in st:
        out["reasoning_effort"] = st["reasoning_effort"] if st["reasoning_effort"] in ("", "low", "medium", "high") else ""
    if "check_sync" in st:
        out["check_sync"] = bool(st["check_sync"])
    for k in ("llm_model", "llm_fallback_model", "prompt_prefix", "prompt_suffix", "motion", "video_suffix"):
        if k in st:
            out[k] = " ".join(str(st[k] or "").split())
    if "voice" in st:
        v = st["voice"] if isinstance(st["voice"], dict) else {}
        svc = v.get("service") if v.get("service") in ("edge", "fastgen", "royal", "voicegen", "voicer", "lumean") else ""
        vid = " ".join(str(v.get("voice_id") or "").split())[:200]
        out["voice"] = {"service": svc, "voice_id": vid} if svc else None
    if "transitions" in st:
        t = st["transitions"]
        out["transitions"] = t if t in ("classic", "soft", "full", "none") else None
    return out


def save_profile_raw(dirs, data):
    """Создать или изменить профиль. data: {name, old_name?, master, settings{…}}. Ключи profile.json,
    которых нет в окне (prompt_rules, пояснения «_…»), сохраняются как были."""
    name, err = check_name(data.get("name"))
    if err:
        raise ProfileError(err)
    master = str(data.get("master") or "").replace("\r\n", "\n").strip()
    if not master:
        raise ProfileError("Мастер-промпт пустой — вставьте его.")
    old = data.get("old_name") or None
    old_dir = find_profile_dir(old, dirs) if old else None
    if old and not old_dir:
        raise ProfileError(f"Профиль «{old}» не найден")
    target = find_profile_dir(name, dirs)
    if target and (not old_dir or os.path.normcase(target) != os.path.normcase(old_dir)):
        raise ProfileError(f"Профиль «{name}» уже есть — выберите другое название.")
    base = os.path.dirname(old_dir) if old_dir else dirs[0]
    pdir = os.path.join(base, name)
    if old_dir and os.path.normcase(old_dir) != os.path.normcase(pdir):
        os.rename(old_dir, pdir)
    os.makedirs(pdir, exist_ok=True)
    sp = os.path.join(pdir, SETTINGS_FILE)
    cur = {}
    if os.path.isfile(sp):
        try:
            cur = json.loads(_read_text(sp)) or {}
        except ValueError:
            cur = {}
    clean = _clean_settings(data.get("settings") or {})
    for k, v in clean.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    tmp = sp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, sp)
    mp = os.path.join(pdir, MASTER_FILE)
    with open(mp + ".tmp", "w", encoding="utf-8") as f:
        f.write(master + "\n")
    os.replace(mp + ".tmp", mp)
    load_profile(name, [base])  # проверка, что профиль читается
    return name


def delete_profile_raw(name, dirs):
    """Удалить профиль: папка переезжает в profiles/_удалённые (можно вернуть руками)."""
    pdir = find_profile_dir(name, dirs)
    if not pdir:
        raise ProfileError(f"Профиль «{name}» не найден")
    trash = os.path.join(os.path.dirname(pdir), TRASH_DIR)
    os.makedirs(trash, exist_ok=True)
    os.rename(pdir, os.path.join(trash, f"{name} {time.strftime('%Y-%m-%d %H-%M-%S')}"))
    return True


def master_check(master):
    """Какие подстановки есть в мастер-промпте и чего не хватает."""
    used = [k for k in PLACEHOLDERS if k in (master or "")]
    warn = []
    if "{{PART_TEXT}}" not in (master or ""):
        warn.append("Нет {{PART_TEXT}} — модель не получит текст части сценария.")
    return {"used": used, "warn": warn, "all": PLACEHOLDERS}


def safe_name(name):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return s[:40] or "profile"


# ---------------------------------------------------------------- деление сценария

_SENT_END = re.compile(r'[.!?…]+["»”’)\]]*(?=\s|$)|\n[ \t]*\n')


def sentence_spans(text):
    """[(начало, конец)] предложений в тексте. Конец предложения — . ! ? … (с закрывающими кавычками) или пустая строка."""
    spans, start = [], 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def sentences(text):
    return [text[a:b].strip() for a, b in sentence_spans(text)]


def count_parts(n_chars, prof, n_sentences=None):
    n = max(prof["min_parts"], n_chars // prof["chars_per_part"])
    if prof.get("max_parts"):
        n = min(n, prof["max_parts"])
    if n_sentences is not None:
        n = min(n, max(1, n_sentences))
    return max(1, n)


def split_parts(text, prof):
    """Делит текст на части примерно равной длины, только по концам предложений."""
    text = text.replace("\r\n", "\n").strip()
    spans = sentence_spans(text)
    if not spans:
        return []
    n = count_parts(len(text), prof, len(spans))
    ends = [b for _, b in spans]          # конец i-го предложения в символах
    total = ends[-1]
    cuts, prev = [], -1
    for k in range(1, n):
        goal = total * k / n
        lo, hi = prev + 1, len(spans) - 1 - (n - k)   # оставить хотя бы по предложению на остальные части
        best = min(range(lo, hi + 1), key=lambda i: abs(ends[i] - goal))
        cuts.append(best)
        prev = best
    parts, s0 = [], 0
    for c in cuts + [len(spans) - 1]:
        parts.append(text[spans[s0][0]:spans[c][1]].strip())
        s0 = c + 1
    return parts


# ---------------------------------------------------------------- мастер-промпт и ответ модели

def fill_template(prof, part_no, parts_total, part_text):
    per, w = prof["prompts_per_part"], prof["number_width"]
    first = (part_no - 1) * per + 1
    values = {
        "{{PART_NUMBER}}": str(part_no),
        "{{PARTS_TOTAL}}": str(parts_total),
        "{{PROMPTS_PER_PART}}": str(per),
        "{{PROMPTS_TOTAL}}": str(per * parts_total),
        "{{FIRST}}": str(first).zfill(w),
        "{{LAST}}": str(first + per - 1).zfill(w),
        "{{PART_TEXT}}": part_text,
    }
    tpl = prof["template"]
    out = tpl
    for k, v in values.items():
        if k != "{{PART_TEXT}}":
            out = out.replace(k, v)
    if "{{PART_TEXT}}" in out:
        out = out.replace("{{PART_TEXT}}", part_text)
    else:
        out = out.rstrip() + "\n\n" + part_text
    return out, first, first + per - 1


_NUM = re.compile(r"^\s*(?:\*\*)?\s*(\d{1,4})\s*[.)]\s*(?:\*\*)?\s*(.*)$")
_SYNC = re.compile(r"^\W*SYNC\b\W*(.*)$", re.I)


def parse_reply(text):
    """Ответ модели → ([(номер, промпт)], фраза SYNC или None)."""
    prompts, sync, cur = [], None, None
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        m = _SYNC.match(s)
        if m and not _NUM.match(s):
            rest = m.group(1).strip()
            if not rest:
                rest = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
            sync = rest.strip("«»\"' ") or None
            break
        m = _NUM.match(line)
        if m and m.group(2).strip():
            cur = [int(m.group(1)), m.group(2).strip()]
            prompts.append(cur)
        elif not s:
            cur = None  # пустая строка заканчивает промпт: «BATCH 2 OF 6…», «Batch 1 complete…» к нему не прилипнут
        elif cur is not None:
            cur[1] += " " + s
    return [(n, t.strip()) for n, t in prompts if t.strip()], sync


def _norm_letters(s):
    """Только буквы и цифры в нижнем регистре: так сравниваются сценарий, SYNC и распознанные слова."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def sync_ok(sync, part_text):
    ns = _norm_letters(sync)
    if len(ns) < 3:
        return False
    whole = _norm_letters(part_text)
    tail = whole[-max(len(ns) * 3, 300):]
    if ns in tail:
        return True
    sents = sentences(part_text)
    last = _norm_letters(sents[-1]) if sents else ""
    return difflib.SequenceMatcher(None, ns, last).ratio() >= 0.85


def prompt_missing(text, rules):
    """Каких обязательных частей нет в промпте (по правилам профиля). → [название правила]"""
    low = (text or "").lower()
    out = []
    for r in rules or []:
        cond = (r.get("if") or "").lower()
        if cond and cond not in low:
            continue
        need = [x.lower() for x in r.get("need") or []]
        any_ = [x.lower() for x in r.get("need_any") or []]
        if any(x not in low for x in need) or (any_ and not any(x in low for x in any_)):
            out.append(r.get("name") or ", ".join(need + any_))
    return out


def bad_prompts(prompts, rules):
    """[(номер, [чего нет])] для промптов, нарушающих правила профиля."""
    return [(n, m) for n, m in ((n, prompt_missing(t, rules)) for n, t in prompts) if m]


def check_reply(prompts, sync, first, last, part_text, check_sync=True, rules=None, max_bad=3):
    problems = []
    nums = [n for n, _ in prompts]
    need = last - first + 1
    if len(prompts) != need:
        problems.append(f"промптов {len(prompts)} вместо {need}")
    elif nums != list(range(first, last + 1)):
        problems.append(f"номера не по порядку (ожидались {first}–{last})")
    if check_sync:
        if not sync:
            problems.append("нет строки SYNC")
        elif not sync_ok(sync, part_text):
            problems.append("SYNC не совпадает с концом части")
    bad = bad_prompts(prompts, rules)
    if len(bad) > max_bad:
        n, miss = bad[0]
        problems.append(f"{len(bad)} промптов без обязательных частей (например №{n}: нет «{', '.join(miss)}»)")
    return problems


# ---------------------------------------------------------------- языковая модель

class ChatError(RuntimeError):
    pass


def read_sse(r, on_text=None, check_cancel=None):
    """Ответ в формате server-sent events (OpenAI stream). → (весь текст, finish_reason, модель).
    on_text(текст_пока) — не чаще 2 раз в секунду."""
    pieces, finish, last_push, served = [], None, 0.0, None
    try:
        for raw in r.iter_lines():
            if check_cancel:
                check_cancel()
            if not raw or not raw.startswith(b"data:"):
                continue
            payload = raw[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                obj = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            if obj.get("error"):
                raise ChatError(f"модель прервала ответ: {str(obj['error'])[:300]}")
            served = obj.get("model") or served
            for ch in obj.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if isinstance(piece, list):
                    piece = "".join(x.get("text", "") for x in piece if isinstance(x, dict))
                if piece:
                    pieces.append(piece)
                finish = ch.get("finish_reason") or finish
            if on_text and time.time() - last_push > 0.5:
                last_push = time.time()
                on_text("".join(pieces))
    finally:
        r.close()
    text = "".join(pieces)
    if on_text:
        on_text(text)
    return text, finish, served


_BUSY_WORDS = ("slot", "concurr", "parallel", "in progress", "busy", "одновремен", "занят")


def explain_429(r, service="fast-gen"):
    """Что значит «429» в ответе: занято / лимит / просто «подождите». → (текст для экрана, сколько ждать секунд)."""
    text = (r.text or "").strip()
    low = text.lower()
    try:
        after = int(float(r.headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        after = 0
    short = " ".join(text.split())[:120]
    if any(w in low for w in _BUSY_WORDS):
        wait, msg = 20, f"{service} занят: одновременных запросов больше, чем разрешено ключу"
    elif any(w in low for w in ("token", "hour", "quota", "limit", "лимит")):
        wait, msg = 60, f"{service}: лимит ({short})"
    else:
        wait, msg = 30, f"{service} просит подождать (429: {short or 'без пояснения'})"
    if after:
        wait = after
    wait = max(5, min(600, wait))
    return f"{msg} — жду {wait} с, продолжу сам", wait


def make_chat(prof, session, url, model, *, extra_headers=None, wait=None, check_cancel=None, log=print, status=None, service="fast-gen"):
    """chat(text) -> str через OpenAI-совместимый /chat/completions (fast-gen или OpenAI).
    session — функция без аргументов, отдающая requests.Session с заголовком авторизации (на каждый запрос своя:
    части пишутся из нескольких потоков). wait(sec, why) — ожидание с проверкой «Стоп»."""
    m = (model or "").lower()
    new_style = m.split("/")[-1].startswith(("gpt-5", "o1", "o3", "o4"))
    stream_ok = [bool(prof.get("stream", True))]
    cur_model = [model]
    effort = [str(prof.get("reasoning_effort") or "").strip()]  # не поддерживается сервисом — выключаем
    chat_served = {}
    backoff = (15, 30, 60, 90, 120)  # паузы между повторами при сбоях сервиса: всего около 5 минут

    def on_fail(errors, why):
        """Повторить ещё раз или сдаться. При третьем сбое подряд — запасная модель, если она задана."""
        if errors > len(backoff):
            raise ChatError(why)
        fb = (prof.get("llm_fallback_model") or "").strip()
        if errors == 3 and fb and cur_model[0] != fb:
            log(f"{service}: модель {cur_model[0]} не отвечает — перехожу на запасную {fb}")
            cur_model[0] = fb
        sec = backoff[errors - 1]
        msg = f"{service}: {why} — повтор {errors} из {len(backoff)} через {sec} с"
        log(msg)
        if chat.status:
            chat.status(msg)
        sleep(sec, msg)  # ответ по кусочкам: видно, что модель пишет; не умеет сервис — выключаем

    def sleep(sec, why):
        if wait:
            wait(sec, why)
        else:
            time.sleep(sec)

    def server_error(r):
        """Короткое объяснение ошибки сервиса: русский текст из ответа, если он есть."""
        try:
            j = r.json()
            msg = ((j.get("translations") or {}).get("ru") or j.get("error") or j.get("message") or "")
            if isinstance(msg, dict):
                msg = msg.get("message") or ""
            code = j.get("code") or ""
            return f"{msg} ({code})" if code and msg else (msg or code or (r.text or "")[:120])
        except ValueError:
            return " ".join((r.text or "").split())[:120] or f"HTTP {r.status_code}"

    def chat(text, on_text=None):
        status = chat.status
        body = {"model": cur_model[0], "messages": [{"role": "user", "content": text}]}
        if new_style and "openai.com" in url:
            body["max_completion_tokens"] = prof["max_tokens"]
        else:
            body["max_tokens"] = prof["max_tokens"]
            body["temperature"] = prof["temperature"]
        last, errors, waited = None, 0, 0.0
        while True:
            if check_cancel:
                check_cancel()
            streaming = stream_ok[0]
            body["model"] = cur_model[0]
            if effort[0]:
                body["reasoning_effort"] = effort[0]
            else:
                body.pop("reasoning_effort", None)
            if streaming:
                body["stream"] = True
            else:
                body.pop("stream", None)
            try:
                r = session().post(url, json=body, headers=extra_headers or None, timeout=900, stream=streaming)
            except requests.RequestException as e:
                errors += 1
                on_fail(errors, f"нет связи ({str(e)[:120]})")
                continue
            if r.status_code == 429 and waited < 3 * 3600:
                why, sec = explain_429(r, service)
                log(f"{why}. Ответ сервера: {(r.text or '')[:400]}")
                if status:
                    status(why)
                sleep(sec, why)
                waited += sec
                continue
            if r.status_code >= 500 or r.status_code == 429:
                errors += 1
                why = server_error(r)
                log(f"{service}: HTTP {r.status_code}: {(r.text or '')[:400]}")
                on_fail(errors, why)
                continue
            if r.status_code >= 400 and effort[0] and "reasoning" in (r.text or "").lower():
                log(f"{service}: модель не принимает reasoning_effort={effort[0]} — дальше без него")
                effort[0] = ""
                continue
            if r.status_code >= 400 and streaming and "stream" in (r.text or "").lower():
                stream_ok[0] = False
                log(f"{service}: ответ по кусочкам не поддерживается — дальше обычным ответом")
                continue
            if r.status_code >= 400:
                raise ChatError(f"модель отказала (HTTP {r.status_code}): {r.text[:300]}")
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                try:
                    content, finish, served = read_sse(r, on_text, check_cancel)
                except requests.RequestException as e:
                    errors += 1
                    on_fail(errors, f"обрыв связи ({str(e)[:120]})")
                    continue
                if not content.strip():
                    stream_ok[0] = False
                    log(f"{service}: ответ по кусочкам пришёл пустым — повторяю обычным ответом")
                    continue
                choice = {"finish_reason": finish}
            else:
                try:
                    js = r.json()
                    served = js.get("model")
                    choice = js["choices"][0]
                    content = choice["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError):
                    raise ChatError(f"непонятный ответ модели: {r.text[:300]}")
                if isinstance(content, list):  # некоторые модели отдают список частей
                    content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
                if on_text:
                    on_text(content or "")
            if choice.get("finish_reason") == "length":
                log("ответ модели обрезан по лимиту max_tokens — увеличьте max_tokens в profile.json, если это повторяется")
            chat_served[threading.get_ident()] = served or cur_model[0]
            return content or ""
    chat.status = status
    chat.served = chat_served
    return chat


# ---------------------------------------------------------------- промпты по частям

_NUM_LINE = re.compile(r"(?m)^\s*(?:\*\*)?\s*\d{1,4}\s*[.)]\s*\S")


def part_prompts(chat, prof, part_no, parts_total, part_text, cache_dir, log=print, check_cancel=None, board=None):
    """Промпты для одной части: из кэша или от модели (с проверкой и повторами). → [str]"""
    board = board or Board()
    filled, first, last = fill_template(prof, part_no, parts_total, part_text)
    key = hashlib.sha1(filled.encode("utf-8")).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    cpath = os.path.join(cache_dir, f"part_{part_no:02d}.json")
    mpath = manual_path(cache_dir, part_no)

    def use_manual():
        """Свой ответ из чата (кнопка «Вставить ответ») важнее ответа модели и кэша."""
        with open(mpath, encoding="utf-8") as f:
            prompts, sync = parse_reply(f.read())
        problems = check_reply(prompts, sync, first, last, part_text, prof["check_sync"],
                               prof.get("prompt_rules") or [], prof["max_bad_prompts"])
        texts = [t for _, t in prompts]
        board.update(part_no, status="warn" if problems else "done", ended=time.time(), prompts=texts, problems=problems,
                     count=len(texts), live="", note="свои промпты", model="вставлено вручную")
        log(f"часть {part_no}: свои промпты из чата, {len(texts)} шт." + (f", замечания: {'; '.join(problems)}" if problems else ""))
        return texts

    if os.path.exists(mpath):
        return use_manual()
    if chat is None:  # промпты пишет сам пользователь — ждём, пока он вставит ответ в панели
        board.update(part_no, status="manual", note="ждёт ваш ответ из чата", started=time.time())
        while not os.path.exists(mpath):
            if check_cancel:
                check_cancel()
            time.sleep(1)
        return use_manual()
    try:
        with open(cpath, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("key") == key and cached.get("prompts"):
            board.update(part_no, status="warn" if cached.get("problems") else "done", prompts=cached["prompts"],
                         problems=cached.get("problems") or [], count=len(cached["prompts"]), note="из кэша", model=cached.get("model", ""))
            return cached["prompts"]
    except (OSError, ValueError):
        pass
    best, last_err = None, None
    for attempt in range(1, prof["attempts"] + 1):
        if check_cancel:
            check_cancel()
        board.update(part_no, status="writing", attempt=attempt, started=time.time(), ended=None, live="", count=0,
                     note="" if attempt == 1 else f"попытка {attempt}: " + "; ".join(best[3] if best else []))

        def on_text(t, n=part_no):
            board.update(n, live=t, count=len(_NUM_LINE.findall(t)))
        try:
            reply = chat(filled, on_text)
            if os.path.exists(mpath):  # пока модель писала, пользователь вставил свой ответ — берём его
                return use_manual()
        except ChatError as e:  # сервис так и не ответил — это неудачная попытка части, а не конец ролика
            last_err = str(e)
            log(f"часть {part_no}: {last_err} — попытка {attempt} из {prof['attempts']}")
            continue
        except Exception as e:
            board.update(part_no, status="error", ended=time.time(), note=str(e)[:200])
            raise
        served = (getattr(chat, "served", None) or {}).get(threading.get_ident(), "")
        board.update(part_no, model=served)
        with open(os.path.join(cache_dir, f"part_{part_no:02d}_answer{attempt}.txt"), "w", encoding="utf-8") as f:
            f.write(reply)
        prompts, sync = parse_reply(reply)
        rules = prof.get("prompt_rules") or []
        problems = check_reply(prompts, sync, first, last, part_text, prof["check_sync"], rules, prof["max_bad_prompts"])
        nbad = len(bad_prompts(prompts, rules))
        log(f"часть {part_no}: ответила модель {served or '?'}, промптов {len(prompts)}, не по правилам {nbad}")
        score = (-abs(len(prompts) - (last - first + 1)), -len(problems), -nbad)
        if prompts and (best is None or score > best[0]):
            best = (score, prompts, sync, problems, served)
        if not problems:
            break
        log(f"часть {part_no}: {'; '.join(problems)} — попытка {attempt} из {prof['attempts']}")
    if best is None:
        why = last_err or "модель не вернула ни одного промпта"
        board.update(part_no, status="error", ended=time.time(), note=why[:200])
        raise ProfileError(f"Часть {part_no} из {parts_total} не написалась: {why}. Готовые части сохранены — "
                           f"запустите этот же ролик ещё раз, программа продолжит с этой части.")
    _, prompts, sync, problems, served = best
    if problems:
        log(f"часть {part_no}: беру лучший ответ ({len(prompts)} промптов), замечания: {'; '.join(problems)}")
    texts = [t for _, t in prompts]
    board.update(part_no, status="warn" if problems else "done", ended=time.time(), prompts=texts, problems=problems,
                 count=len(texts), live="", model=served)
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump({"key": key, "prompts": texts, "sync": sync, "problems": problems, "model": served}, f, ensure_ascii=False, indent=1)
    return texts


def all_prompts(chat, prof, parts, cache_dir, progress, log=print, check_cancel=None, board=None):
    """Промпты для всех частей; части пишутся параллельно (prof['parallel']).
    Пока модель пишет, раз в 10 с на экране: какие части в работе, сколько минут ждём, последнее сообщение сервиса."""
    n = len(parts)
    result = [None] * n
    done = [0]
    active = {}            # номер части -> время начала
    note = [None, 0.0]     # последнее сообщение сервиса (лимит, «занят»), время
    lock = threading.Lock()
    stop = threading.Event()

    def line():
        with lock:
            d, act, (msg, at) = done[0], sorted(active.items()), note
        s = f"профиль «{prof['name']}»: готово частей {d} из {n}"
        if act:
            now = time.time()
            if chat is None:
                s += " · ждут ваш ответ из чата: " + ", ".join(f"часть {k}" for k, _ in act)
            else:
                s += " · модель пишет " + ", ".join(f"часть {k} ({int((now - t0) // 60)} мин)" for k, t0 in act)
        if msg and time.time() - at < 90:
            s += " · " + msg
        return d, s

    def ticker():
        while not stop.wait(10):
            d, s = line()
            progress("prompts", d, n, s)

    def set_status(msg):
        with lock:
            note[0], note[1] = msg, time.time()

    if chat is not None:
        chat.status = set_status
    progress("prompts", 0, n, f"профиль «{prof['name']}»: пишу промпты, частей {n}")

    failed = threading.Event()  # одна часть не написалась — остальные не начинаем: ролик всё равно остановится

    def one(k):
        if failed.is_set():
            return
        with lock:
            active[k + 1] = time.time()
        try:
            res = part_prompts(chat, prof, k + 1, n, parts[k], cache_dir, log, check_cancel, board)
        except BaseException:
            failed.set()
            raise
        finally:
            with lock:
                active.pop(k + 1, None)
        with lock:
            result[k] = res
            done[0] += 1
        d, msg = line()
        progress("prompts", d, n, msg)

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    try:  # вручную части не пишутся «по очереди»: можно вставлять в любом порядке
        with cf.ThreadPoolExecutor(max_workers=n if chat is None else min(prof["parallel"], n)) as ex:
            futs = [ex.submit(one, k) for k in range(n)]
            for f in futs:
                f.result()
    finally:
        stop.set()
    with board.lock:
        board.prompts_done = True
    return result


# ---------------------------------------------------------------- тайминг по озвучке

_WORD = re.compile(r"\w+(?:['’\-]\w+)*")


def norm_word(w):
    return _norm_letters(w)


def script_words(text):
    return [x for x in (norm_word(w) for w in _WORD.findall(text)) if x]


def _locate(sw, tw, s, ctx=120, min_match=12):
    """Индекс слова распознавания, соответствующего слову сценария №s. Сравнивается окно сценария вокруг s
    с окном распознавания вокруг ожидаемого места (пропорционально длине) — быстро и без ложных совпадений
    с далёкими частями текста. None — совпадений слишком мало."""
    if not sw or not tw:
        return None
    est = s * len(tw) / len(sw)
    win = max(400, int(0.12 * len(tw)))
    b_lo, b_hi = max(0, int(est) - win), min(len(tw), int(est) + win)
    a_lo, a_hi = max(0, s - ctx), min(len(sw), s + ctx)
    sm = difflib.SequenceMatcher(None, sw[a_lo:a_hi], tw[b_lo:b_hi], autojunk=False)
    pairs = []
    for a, b, size in sm.get_matching_blocks():
        pairs.extend((a_lo + a + i, b_lo + b + i) for i in range(size))
    if len(pairs) < min(min_match, (a_hi - a_lo) // 3):
        return None
    pa = [a for a, _ in pairs]
    j = bisect.bisect_left(pa, s)
    if j < len(pairs) and pairs[j][0] == s:
        return float(pairs[j][1])
    if j == 0:
        a1, b1 = pairs[0]
        return max(0.0, b1 - (a1 - s))
    if j >= len(pairs):
        a0, b0 = pairs[-1]
        return min(len(tw) - 1.0, b0 + (s - a0))
    (a0, b0), (a1, b1) = pairs[j - 1], pairs[j]
    return b0 + (s - a0) * (b1 - b0) / max(1, a1 - a0)


def part_bounds(parts, words, duration, log=print):
    """Времена границ частей в озвучке: [0, t1, …, duration].
    words — слова распознавания [{"text","start","end"}]. Части сопоставляются со словами через difflib."""
    n = len(parts)
    if n <= 1:
        return [0.0, float(duration)]
    tw = [norm_word(w.get("text", "")) for w in words]
    sw, starts = [], []
    for p in parts:
        starts.append(len(sw))
        sw.extend(script_words(p))
    lens = [len(p) for p in parts]
    tot = sum(lens) or 1
    out, acc = [0.0], 0
    for k in range(1, n):
        acc += lens[k - 1]
        s = starts[k]
        t = _locate(sw, tw, s)
        if t is None or not words:
            log(f"профиль: граница части {k + 1} не найдена в озвучке — ставлю по длине текста")
            out.append(round(duration * acc / tot, 3))
            continue
        i = int(round(min(max(t, 0), len(words) - 1)))
        if i <= 0:
            tm = float(words[0]["start"])
        else:
            tm = (float(words[i - 1]["end"]) + float(words[i]["start"])) / 2
        out.append(round(tm, 3))
    out.append(float(duration))
    for k in range(1, len(out)):  # границы строго по возрастанию
        out[k] = max(out[k], out[k - 1] + 0.5)
    out[-1] = max(out[-1], float(duration))
    return out


def build_scenes(prof, parts, prompts_by_part, bounds):
    """Сцены для KadroGen: кадры каждой части делят её время поровну."""
    scenes = []
    pre, suf = (prof.get("prompt_prefix") or "").strip(), (prof.get("prompt_suffix") or "").strip()
    for k, prompts in enumerate(prompts_by_part):
        t0, t1 = bounds[k], bounds[k + 1]
        m = len(prompts)
        ws = parts[k].split()
        for j, raw in enumerate(prompts):
            a = t0 + (t1 - t0) * j / m
            b = t0 + (t1 - t0) * (j + 1) / m
            frag = " ".join(ws[len(ws) * j // m: len(ws) * (j + 1) // m])
            prompt = " ".join(x for x in (pre, raw, suf) if x)
            scenes.append({
                "index": len(scenes), "start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3),
                "text": frag, "part": k + 1, "raw_prompt": raw, "prompt": prompt, "exact": True,
                "profile": prof["name"], "profile_key": prof["key"], "characters": [], "use_refs": False,
                "motion": prof.get("motion") or "slow gentle camera movement",
                "video_suffix": (prof.get("video_suffix") or "").strip(),
            })
    if scenes:
        scenes[-1]["end"] = round(bounds[-1], 3)
        scenes[-1]["duration"] = round(scenes[-1]["end"] - scenes[-1]["start"], 3)
    return scenes


# ---------------------------------------------------------------- готовые промпты из файла (.txt рядом с озвучкой)

READY_NAME = "готовые промпты"
READY_MIN_PROMPTS = 3


def parse_blocks(text):
    """Файл готовых промптов → блоки [{"prompts": [str], "numbers": [int], "sync": фраза | None}].
    Блок кончается строкой SYNC; фраза — на той же строке («SYNC: …») или на следующей непустой.
    Строки между блоками (заголовки «Часть 2» и т.п.) пропускаются; после последнего SYNC может быть хвост без SYNC."""
    blocks, cur, cur_prompt, expect_sync = [], [], None, False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if expect_sync:
            if not s:
                continue
            blocks.append((cur, s.strip("«»\"' ")))
            cur, cur_prompt, expect_sync = [], None, False
            continue
        m = _SYNC.match(s)
        if m and not _NUM.match(s):
            rest = m.group(1).strip()
            if rest:
                blocks.append((cur, rest.strip("«»\"' ")))
                cur, cur_prompt = [], None
            else:
                expect_sync = True
            continue
        m = _NUM.match(line)
        if m and m.group(2).strip():
            cur_prompt = [int(m.group(1)), m.group(2).strip()]
            cur.append(cur_prompt)
        elif not s:
            cur_prompt = None  # пустая строка заканчивает промпт: служебные строки чата между пачками пропускаются
        elif cur_prompt is not None:
            cur_prompt[1] += " " + s
    if cur:
        blocks.append((cur, None))
    out = []
    for prompts, sync in blocks:
        prompts = [(n, t.strip()) for n, t in prompts if t.strip()]
        if prompts:
            out.append({"prompts": [t for _, t in prompts], "numbers": [n for n, _ in prompts], "sync": sync or None})
    return out


def blocks_summary(blocks, duration=None, clip_seconds=8.0):
    """Что получится из файла: блоки, промпты, нумерация, секунды на кадр и сколько клипа Veo уйдёт в обрезку."""
    total = sum(len(b["prompts"]) for b in blocks)
    nums = [n for b in blocks for n in b["numbers"]]
    issues = []
    if nums and nums != list(range(nums[0], nums[0] + len(nums))):
        dup = sorted({n for n in nums if nums.count(n) > 1})
        issues.append("нумерация не подряд" + (f" (повторы: {', '.join(map(str, dup[:5]))})" if dup else ""))
    missing = [k + 1 for k, b in enumerate(blocks[:-1]) if not b["sync"]]
    if missing:
        issues.append(f"у блоков {', '.join(map(str, missing))} нет SYNC — их граница встанет по числу промптов")
    out = {"blocks": len(blocks), "prompts": total, "per_block": [len(b["prompts"]) for b in blocks], "issues": issues}
    if duration and total:
        per = duration / total
        out.update({"duration": round(duration), "sec_per_frame": round(per, 1),
                    "clip_trim_pct": int(round(max(0.0, 1 - per / clip_seconds) * 100))})
        if per < 2:
            issues.append(f"кадры выходят короткими: ~{per:.1f} с на кадр")
        elif per > 15:
            issues.append(f"кадры выходят длинными: ~{per:.0f} с на кадр")
    return out


def _find_phrase_end(pw, tw, start, expect):
    """Где в распознанной озвучке кончается фраза SYNC: индекс последнего слова и оценка совпадения (0…1).
    Ищем от конца прошлого блока вперёд; при равной оценке — ближе к ожидаемому месту."""
    if not pw or not tw:
        return None, 0.0
    L = len(pw)
    best = (0.0, 0, None)
    for i in range(max(0, start), len(tw)):
        seg = tw[i:i + L + 2]
        if not seg or not (set(seg[:3]) & set(pw[:3])):  # окно должно начинаться похоже на фразу — иначе не тратим время
            continue
        sm = difflib.SequenceMatcher(None, pw, seg, autojunk=False)
        score = sm.ratio()
        if score < 0.5:
            continue
        last = max((b + size - 1 for _, b, size in sm.get_matching_blocks() if size), default=None)
        if last is None:
            continue
        key = (round(score, 2), -abs(i - expect))
        if best[2] is None or key > (round(best[0], 2), -abs(best[1] - expect)):
            best = (score, i, i + last)
    return best[2], best[0]


def block_bounds(blocks, words, duration, log=print):
    """Времена границ блоков в озвучке по фразам SYNC: [0, t1, …, duration]. Не нашли фразу — граница по числу промптов."""
    n = len(blocks)
    if n <= 1:
        return [0.0, float(duration)], []
    tw = [norm_word(w.get("text", "")) for w in words]
    counts = [len(b["prompts"]) for b in blocks]
    total = sum(counts) or 1
    anchors, report, pos, acc = {0: 0.0, n: float(duration)}, [], 0, 0
    for k in range(n - 1):
        acc += counts[k]
        sync = blocks[k].get("sync")
        expect = int(len(tw) * acc / total)
        e, score = _find_phrase_end(script_words(sync or ""), tw, pos, expect) if sync else (None, 0.0)
        if e is not None and score >= 0.6:
            t = (float(words[e]["end"]) + float(words[e + 1]["start"])) / 2 if e + 1 < len(words) else float(words[e]["end"])
            anchors[k + 1] = round(t, 3)
            pos = e + 1
            report.append((k + 1, True, round(score, 2), anchors[k + 1]))
        else:
            report.append((k + 1, False, round(score, 2), None))
            if sync:
                log(f"готовые промпты: фраза SYNC блока {k + 1} не найдена в озвучке («{sync[:60]}») — граница по числу промптов")
    out, known = [], sorted(anchors)
    cum = [0]
    for c in counts:
        cum.append(cum[-1] + c)
    for k in range(n + 1):  # неизвестные границы — между соседними найденными, пропорционально числу промптов
        if k in anchors:
            out.append(anchors[k])
            continue
        lo = max(a for a in known if a < k)
        hi = min(a for a in known if a > k)
        frac = (cum[k] - cum[lo]) / max(1, cum[hi] - cum[lo])
        out.append(round(anchors[lo] + (anchors[hi] - anchors[lo]) * frac, 3))
    for k in range(1, len(out)):
        out[k] = max(out[k], out[k - 1] + 0.5)
    out[-1] = max(out[-1], float(duration))
    return out, report


def ready_profile():
    p = dict(DEFAULTS)
    p.update({"name": READY_NAME, "key": "ready"})
    return p


def profile_voice(name, dirs):
    """Голос профиля {"service", "voice_id"} или None."""
    try:
        v = read_profile_raw(name, dirs)["settings"].get("voice") or {}
    except ProfileError:
        return None
    return {"service": v["service"], "voice_id": v.get("voice_id", "")} if v.get("service") else None
