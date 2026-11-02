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
import unicodedata
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
    "max_tokens": 32000,
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
    for k in ("chars_per_part", "min_parts", "max_parts", "prompts_per_part", "number_width", "max_tokens", "parallel", "attempts"):
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
        elif cur is not None and s:
            cur[1] += " " + s
    return [(n, t.strip()) for n, t in prompts if t.strip()], sync


def _norm_letters(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(ch for ch in s if ch.isalnum())


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


def check_reply(prompts, sync, first, last, part_text, check_sync=True):
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
    return problems


# ---------------------------------------------------------------- языковая модель

class ChatError(RuntimeError):
    pass


def make_chat(prof, session, url, model, *, extra_headers=None, wait=None, check_cancel=None, log=print, progress=None):
    """chat(text) -> str через OpenAI-совместимый /chat/completions (fast-gen или OpenAI).
    session — функция без аргументов, отдающая requests.Session с заголовком авторизации (на каждый запрос своя:
    части пишутся из нескольких потоков). wait(sec, why) — ожидание с проверкой «Стоп»."""
    m = (model or "").lower()
    new_style = m.split("/")[-1].startswith(("gpt-5", "o1", "o3", "o4"))

    def sleep(sec, why):
        if wait:
            wait(sec, why)
        else:
            time.sleep(sec)

    def chat(text):
        body = {"model": model, "messages": [{"role": "user", "content": text}]}
        if new_style and "openai.com" in url:
            body["max_completion_tokens"] = prof["max_tokens"]
        else:
            body["max_tokens"] = prof["max_tokens"]
            body["temperature"] = prof["temperature"]
        last, errors, limit_waits = None, 0, 0
        while True:
            if check_cancel:
                check_cancel()
            try:
                r = session().post(url, json=body, headers=extra_headers or None, timeout=900)
            except requests.RequestException as e:
                last, errors = str(e), errors + 1
                if errors >= 4:
                    raise ChatError(f"модель не отвечает: {last[:200]}")
                sleep(10 * errors, "нет связи с моделью, повторяю")
                continue
            if r.status_code == 429 and limit_waits < 90:
                limit_waits += 1
                why = "лимит токенов для текстов исчерпан, жду минуту (продолжу автоматически)"
                if progress:
                    progress("prompts", 0, 0, why)
                sleep(60, why)
                continue
            if r.status_code >= 500 or r.status_code == 429:
                last, errors = f"HTTP {r.status_code}: {r.text[:200]}", errors + 1
                if errors >= 4:
                    raise ChatError(f"модель не отвечает: {last}")
                sleep(10 * errors, "модель не отвечает, повторяю")
                continue
            if r.status_code >= 400:
                raise ChatError(f"модель отказала (HTTP {r.status_code}): {r.text[:300]}")
            try:
                choice = r.json()["choices"][0]
                content = choice["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise ChatError(f"непонятный ответ модели: {r.text[:300]}")
            if isinstance(content, list):  # некоторые модели отдают список частей
                content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
            if choice.get("finish_reason") == "length":
                log("ответ модели обрезан по лимиту max_tokens — увеличьте max_tokens в profile.json, если это повторяется")
            return content or ""
    return chat


# ---------------------------------------------------------------- промпты по частям

def part_prompts(chat, prof, part_no, parts_total, part_text, cache_dir, log=print, check_cancel=None):
    """Промпты для одной части: из кэша или от модели (с проверкой и повторами). → [str]"""
    filled, first, last = fill_template(prof, part_no, parts_total, part_text)
    key = hashlib.sha1(filled.encode("utf-8")).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    cpath = os.path.join(cache_dir, f"part_{part_no:02d}.json")
    try:
        with open(cpath, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("key") == key and cached.get("prompts"):
            return cached["prompts"]
    except (OSError, ValueError):
        pass
    best = None
    for attempt in range(1, prof["attempts"] + 1):
        if check_cancel:
            check_cancel()
        reply = chat(filled)
        with open(os.path.join(cache_dir, f"part_{part_no:02d}_answer{attempt}.txt"), "w", encoding="utf-8") as f:
            f.write(reply)
        prompts, sync = parse_reply(reply)
        problems = check_reply(prompts, sync, first, last, part_text, prof["check_sync"])
        score = (-abs(len(prompts) - (last - first + 1)), -len(problems))
        if prompts and (best is None or score > best[0]):
            best = (score, prompts, sync, problems)
        if not problems:
            break
        log(f"часть {part_no}: {'; '.join(problems)} — попытка {attempt} из {prof['attempts']}")
    if best is None:
        raise ProfileError(f"Часть {part_no}: модель не вернула ни одного промпта. Ответ сохранён в {cache_dir}")
    _, prompts, sync, problems = best
    if problems:
        log(f"часть {part_no}: беру лучший ответ ({len(prompts)} промптов), замечания: {'; '.join(problems)}")
    texts = [t for _, t in prompts]
    with open(cpath, "w", encoding="utf-8") as f:
        json.dump({"key": key, "prompts": texts, "sync": sync, "problems": problems}, f, ensure_ascii=False, indent=1)
    return texts


def all_prompts(chat, prof, parts, cache_dir, progress, log=print, check_cancel=None):
    """Промпты для всех частей; части пишутся параллельно (prof['parallel'])."""
    n = len(parts)
    result = [None] * n
    done = [0]
    lock = threading.Lock()
    progress("prompts", 0, n, f"профиль «{prof['name']}»: пишу промпты, частей {n}")

    def one(k):
        res = part_prompts(chat, prof, k + 1, n, parts[k], cache_dir, log, check_cancel)
        with lock:
            result[k] = res
            done[0] += 1
            progress("prompts", done[0], n, f"профиль «{prof['name']}»: готово частей {done[0]} из {n}")

    with cf.ThreadPoolExecutor(max_workers=min(prof["parallel"], n)) as ex:
        futs = [ex.submit(one, k) for k in range(n)]
        for f in futs:
            f.result()
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
