import threading
"""Субтитры со своим видом: шрифт, размер, цвета, обводка, тень, плашка, положение, ширина и эффекты появления.

Субтитры пишутся в .ass (координаты сразу в пикселях 1920×1080) и вжигаются в видео ffmpeg (libass) — тем же
проходом, что и раньше. Предпросмотр рисует один кадр тем же ffmpeg, поэтому в окне видно ровно то, что будет в ролике.

Шрифты: .ttf/.otf/.ttc из папок fonts (рядом с программой и внутри неё). Для отрисовки нужный файл копируется
во временную папку рядом с .ass — так ffmpeg находит его без путей с «C:\\» (их трудно экранировать в фильтре).
"""
import os
import re
import shutil
import subprocess

W, H = 1920, 1080
FONT_EXT = (".ttf", ".otf", ".ttc")
EFFECTS = ("none", "fade", "pop", "slide", "karaoke", "words")
POSITIONS = ("bottom", "middle", "top")

DEFAULT = {
    "font": "Intro Demo Black CAPS",
    "size": 56,               # px при 1080p
    "bold": False,
    "upper": False,           # ЗАГЛАВНЫМИ
    "color": "#FFFFFF",
    "outline": 4,             # px; 0 — без обводки
    "outline_color": "#000000",
    "shadow": 2,              # px; 0 — без тени
    "shadow_color": "#000000",
    "shadow_alpha": 60,       # прозрачность тени, %
    "box": False,             # плашка под текстом вместо обводки
    "box_color": "#000000",
    "box_alpha": 40,          # прозрачность плашки, %
    "box_radius": 0,          # скругление углов плашки, px; 0 — прямые углы
    "position": "bottom",
    "margin_v": 45,           # отступ от края (низ/верх), px
    "full_width": False,      # на всё видео (узкие поля) или как раньше (широкие поля)
    "max_chars": 42,          # символов в строке
    "max_lines": 2,
    "effect": "none",
    "highlight": "#FFD400",   # цвет подсветки слова (караоке)
}

PRESETS = {
    "Как было": {},
    "Классика YouTube": {"font": "Arial", "size": 58, "bold": True, "outline": 5, "shadow": 0, "max_chars": 42, "effect": "fade"},
    "Жёлтое караоке": {"size": 64, "upper": True, "outline": 6, "shadow": 3, "effect": "karaoke", "highlight": "#FFD400",
                       "max_chars": 32},
    "Плашка": {"font": "Arial", "size": 52, "bold": True, "box": True, "box_alpha": 30, "outline": 0, "shadow": 0,
               "margin_v": 60, "effect": "slide"},
    "Крупно по центру, по словам": {"size": 86, "upper": True, "outline": 7, "shadow": 4, "position": "middle",
                                    "max_chars": 18, "max_lines": 2, "effect": "words", "full_width": True},
}


# ---------------------------------------------------------------- настройки

def normalize(style):
    """Настройки из окна → полный набор с допустимыми значениями."""
    s = dict(DEFAULT)
    for k, v in (style or {}).items():
        if k in DEFAULT and v is not None:
            s[k] = v

    def num(k, lo, hi):
        try:
            s[k] = max(lo, min(hi, int(round(float(s[k])))))
        except (TypeError, ValueError):
            s[k] = DEFAULT[k]
    for k, lo, hi in (("size", 16, 200), ("outline", 0, 20), ("shadow", 0, 20), ("shadow_alpha", 0, 100), ("box_alpha", 0, 100),
                      ("margin_v", 0, 500), ("max_chars", 10, 90), ("max_lines", 1, 3), ("box_radius", 0, 60)):
        num(k, lo, hi)
    for k in ("bold", "upper", "box", "full_width"):
        s[k] = bool(s[k])
    for k in ("color", "outline_color", "shadow_color", "box_color", "highlight"):
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(s[k] or "")):
            s[k] = DEFAULT[k]
    if s["position"] not in POSITIONS:
        s["position"] = "bottom"
    if s["effect"] not in EFFECTS:
        s["effect"] = "none"
    s["font"] = str(s["font"] or DEFAULT["font"]).replace(",", " ").strip()[:80]
    return s


def ass_color(hex_rgb, transparency_pct=0):
    """#RRGGBB + прозрачность % → &HAABBGGRR (в ASS 00 — непрозрачно, FF — прозрачно)."""
    h = hex_rgb.lstrip("#")
    a = int(round(255 * max(0, min(100, transparency_pct)) / 100))
    return f"&H{a:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


# ---------------------------------------------------------------- текст из сценария

_SERVICE_LINE = re.compile(r"^\W*(SYNC\b|BATCH\b|PART\s*\d|CZ[EĘ]ŚĆ\s*\d|ЧАСТЬ\s*\d|---|===|\*\*\*)", re.I)


def _key(word):
    return "".join(ch for ch in word.lower() if ch.isalnum())


def script_tokens(text):
    """Слова сценария для субтитров: служебные строки (SYNC, «ЧАСТЬ 2», BATCH…) и пометки в [скобках] убираются,
    знаки препинания остаются при словах. Отдельные «—», «…» приклеиваются к соседнему слову."""
    toks = []
    for line in (text or "").splitlines():
        if _SERVICE_LINE.match(line):
            continue
        line = re.sub(r"\[[^\]]*\]", " ", line)
        for w in line.split():
            if _key(w):
                toks.append(w)
            elif toks:
                toks[-1] += ("" if w in ",.;:!?…" else " ") + w
    return toks


def match_keys(sk, rk):
    """Сопоставить две последовательности слов (уже нормализованных): для каждого слова sk — индекс в rk или None.
    Совпадения ищутся целиком, затем ещё раз в «дырах» между ними — мельче."""
    import difflib
    match = [None] * len(sk)

    def fill(a0, a1, b0, b1, depth=0):
        if a0 >= a1 or b0 >= b1:
            return
        sm = difflib.SequenceMatcher(None, sk[a0:a1], rk[b0:b1], autojunk=(a1 - a0) > 3000)
        pa, pb = a0, b0
        for m in sm.get_matching_blocks():
            if not m.size:
                continue
            if depth < 2 and (m.a + a0 > pa and m.b + b0 > pb):
                fill(pa, m.a + a0, pb, m.b + b0, depth + 1)
            for k in range(m.size):
                match[a0 + m.a + k] = b0 + m.b + k
            pa, pb = a0 + m.a + m.size, b0 + m.b + m.size
        if depth < 2 and pa < a1 and pb < b1:
            fill(pa, a1, pb, b1, depth + 1)

    fill(0, len(sk), 0, len(rk))
    return match


def align_script(script_text, words, min_share=0.45):
    """Слова сценария с временем из распознавания: [{"text","start","end"}] или None, если сценарий
    не похож на озвучку (совпало меньше min_share слов) — тогда субтитры по распознаванию.
    Совпавшие слова берут время своего распознанного слова; несовпавшие (ослышки, числа словами, пропуски)
    делят время между соседними совпавшими пропорционально длине."""
    toks = script_tokens(script_text)
    rw = [w for w in words or [] if str(w.get("text", "")).strip()]
    if not toks or not rw:
        return None
    sk = [_key(t) for t in toks]
    rk = [_key(str(w["text"])) for w in rw]
    match = match_keys(sk, rk)
    if sum(m is not None for m in match) < min_share * len(toks):
        return None
    out = [None] * len(toks)
    for i, m in enumerate(match):
        if m is not None:
            out[i] = {"text": toks[i], "start": float(rw[m]["start"]), "end": float(rw[m]["end"])}
    i = 0
    while i < len(toks):
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < len(toks) and out[j] is None:
            j += 1
        # дыра toks[i:j]: время от конца предыдущего совпавшего до начала следующего,
        # если между ними есть распознанные слова — по их границам
        pm = match[i - 1] if i > 0 else None
        nm = match[j] if j < len(toks) else None
        b0 = pm + 1 if pm is not None else 0
        b1 = nm if nm is not None else len(rw)
        if b1 > b0:
            t0, t1 = float(rw[b0]["start"]), float(rw[b1 - 1]["end"])
        else:
            t0 = out[i - 1]["end"] if i > 0 else float(rw[0]["start"])
            t1 = out[j]["start"] if j < len(toks) else t0 + 0.3 * (j - i)
        if t1 - t0 < 0.08 * (j - i):  # слов больше, чем времени (диктор пропустил) — короткий показ
            t1 = t0 + 0.08 * (j - i)
        lens = [max(1, len(_key(t))) for t in toks[i:j]]
        tot, acc = float(sum(lens)), 0
        for k in range(i, j):
            a = t0 + (t1 - t0) * acc / tot
            acc += lens[k - i]
            out[k] = {"text": toks[k], "start": a, "end": t0 + (t1 - t0) * acc / tot}
        i = j
    for k in range(1, len(out)):  # время только вперёд
        if out[k]["start"] < out[k - 1]["start"]:
            out[k]["start"] = out[k - 1]["start"]
        if out[k]["end"] < out[k]["start"]:
            out[k]["end"] = out[k]["start"] + 0.05
    return out


# ---------------------------------------------------------------- реплики

def build_cues(words, max_chars=42, max_lines=2, max_sec=6.0, min_sec=0.8, glue_gap=5.0, pause_break=0.7):
    """Слова с таймкодами → реплики [{"start","end","lines": [[слово,…],…]}].
    Реплика рвётся на знаке конца фразы, на паузе, по длине и по времени; строки — не длиннее max_chars.
    Короткая пауза между репликами склеивается: субтитр держится до следующего, без мигания."""
    ws = [w for w in words or [] if str(w.get("text", "")).strip()]
    cues, cur = [], []
    limit = max_chars * max_lines

    def text_len(lst):
        return len(" ".join(str(w["text"]).strip() for w in lst))

    for i, w in enumerate(ws):
        if cur:
            gap = float(w["start"]) - float(cur[-1]["end"])
            too_long = text_len(cur + [w]) > limit or len(wrap_words(cur + [w], max_chars, max_lines)) > max_lines or float(w["end"]) - float(cur[0]["start"]) > max_sec
            strong = re.search(r"[.!?…]$", str(cur[-1]["text"]).strip()) is not None
            if too_long or gap > pause_break or (strong and text_len(cur) >= max_chars * 0.5):
                cues.append(cur)
                cur = []
        cur.append(w)
    if cur:
        cues.append(cur)
    out = []
    for k, c in enumerate(cues):
        start, end = float(c[0]["start"]), float(c[-1]["end"]) + 0.25
        if k + 1 < len(cues):
            nxt = float(cues[k + 1][0]["start"])
            end = nxt if nxt - float(c[-1]["end"]) <= glue_gap else min(end, nxt)
        end = max(end, start + min_sec)
        out.append({"start": start, "end": end, "words": c, "lines": wrap_words(c, max_chars, max_lines)})
    for k in range(len(out) - 1):  # реплики не наезжают друг на друга
        out[k]["end"] = min(out[k]["end"], out[k + 1]["start"])
    return out


def wrap_words(ws, max_chars, max_lines):
    """Слова реплики → строки не длиннее max_chars; при двух строках — по возможности поровну и по знаку препинания."""
    texts = [str(w["text"]).strip() for w in ws]
    if len(" ".join(texts)) <= max_chars or max_lines == 1:
        return [list(ws)]
    best, best_cost = None, None
    for cut in range(1, len(ws)):
        a, b = " ".join(texts[:cut]), " ".join(texts[cut:])
        if len(a) > max_chars or (max_lines == 2 and len(b) > max_chars):
            continue
        cost = abs(len(a) - len(b)) - (12 if re.search(r"[,.;:!?…]$", texts[cut - 1]) else 0)
        if best_cost is None or cost < best_cost:
            best, best_cost = cut, cost
    if best is None:  # не влезает в две строки — режем жадно
        lines, cur = [], []
        for w, t in zip(ws, texts):
            if cur and len(" ".join(str(x["text"]).strip() for x in cur + [w])) > max_chars:
                lines.append(cur)
                cur = []
            cur.append(w)
        if cur:
            lines.append(cur)
        return lines
    return [list(ws[:best]), list(ws[best:])]


# ---------------------------------------------------------------- .ass

def _t(sec):
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text):
    return str(text).replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _anchor(s):
    """Номер выравнивания ASS и точка привязки (для выезда)."""
    if s["position"] == "middle":
        return 5, (W // 2, H // 2)
    if s["position"] == "top":
        return 8, (W // 2, s["margin_v"])
    return 2, (W // 2, H - s["margin_v"])


def _cue_text(cue, s):
    up = (lambda x: x.upper()) if s["upper"] else (lambda x: x)
    align, (x, y) = _anchor(s)
    t0 = cue["start"]
    if s["effect"] in ("karaoke", "words"):
        parts, flat = [], [w for line in cue["lines"] for w in line]
        for li, line in enumerate(cue["lines"]):
            if li:
                parts.append("\\N")
            for wi, w in enumerate(line):
                word = _esc(up(str(w["text"]).strip()))
                sep = " " if wi else ""
                if s["effect"] == "karaoke":
                    # подсветка идёт до начала следующего слова — без «дыр» между словами
                    k = next(i for i, x in enumerate(flat) if x is w)
                    nxt = float(flat[k + 1]["start"]) if k + 1 < len(flat) else float(w["end"])
                    dur = max(1, int(round((nxt - float(w["start"])) * 100)))
                    lead = max(0, int(round((float(w["start"]) - t0) * 100)))
                    parts.append(f"{sep}{{\\kf{dur}}}{word}" if k else f"{{\\k{lead}}}{{\\kf{dur}}}{word}")
                else:
                    at = max(0, int(round((float(w["start"]) - t0) * 1000)))
                    parts.append(f"{sep}{{\\alpha&HFF&\\t({at},{at + 80},\\alpha&H00&)}}{word}")
        return "".join(parts)
    text = "\\N".join(" ".join(_esc(up(str(w["text"]).strip())) for w in line) for line in cue["lines"])
    if s["effect"] == "fade":
        return "{\\fad(180,120)}" + text
    if s["effect"] == "pop":
        return "{\\fad(60,60)\\fscx70\\fscy70\\t(0,140,\\fscx100\\fscy100)}" + text
    if s["effect"] == "slide":
        dy = 45 if align != 8 else -45
        return f"{{\\an{align}\\move({x},{y + dy},{x},{y},0,180)\\fad(120,80)}}" + text
    return text


_FONT_CACHE = {}


def _font_for(s, font_dirs):
    """Файл шрифта для замера ширины текста (PIL): из папок fonts, иначе системный Arial; нет — None."""
    key = (s["font"].lower(), s["bold"], tuple(font_dirs or ()))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = ""
    for f in list_fonts(font_dirs or []):
        if f["file"] and f["name"].lower() == s["font"].lower():
            path = f["file"]
            break
    if not path:
        win = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        for n in (("arialbd.ttf", "arial.ttf") if s["bold"] else ("arial.ttf",)) + ("DejaVuSans-Bold.ttf" if s["bold"] else "DejaVuSans.ttf",):
            for d in (win, "/usr/share/fonts/truetype/dejavu"):
                if os.path.isfile(os.path.join(d, n)):
                    path = os.path.join(d, n)
                    break
            if path:
                break
    font = None
    if path:
        try:
            from PIL import ImageFont
            font = ImageFont.truetype(path, 100)
        except Exception:
            font = None
    _FONT_CACHE[key] = font
    return font


def _text_width(font, text, size):
    """Ширина строки так, как её нарисует libass: у него размер шрифта = высота строки (подъём + спуск)."""
    if font is None:
        return len(text) * size * 0.55
    asc, desc = font.getmetrics()
    return font.getlength(text) * size / float(asc + desc or 100)


def _round_rect(x0, y0, x1, y1, r):
    """Контур скруглённого прямоугольника для рисования в ASS (\\p1)."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    k = r * 0.448  # 1 − 0.552: кривая Безье ≈ четверть окружности
    f = lambda v: str(int(round(v)))
    return " ".join([
        "m", f(x0 + r), f(y0), "l", f(x1 - r), f(y0),
        "b", f(x1 - k), f(y0), f(x1), f(y0 + k), f(x1), f(y0 + r), "l", f(x1), f(y1 - r),
        "b", f(x1), f(y1 - k), f(x1 - k), f(y1), f(x1 - r), f(y1), "l", f(x0 + r), f(y1),
        "b", f(x0 + k), f(y1), f(x0), f(y1 - k), f(x0), f(y1 - r), "l", f(x0), f(y0 + r),
        "b", f(x0), f(y0 + k), f(x0 + k), f(y0), f(x0 + r), f(y0)])


def _box_event(cue, s, font):
    """Скруглённая плашка под репликой: свой слой 0 под текстом, тот же тайминг, своё появление."""
    up = (lambda x: x.upper()) if s["upper"] else (lambda x: x)
    lines = [" ".join(up(str(w["text"]).strip()) for w in line) for line in cue["lines"]]
    size = s["size"]
    tw = max(_text_width(font, ln, size) for ln in lines) if lines else 0
    th = size * len(lines)
    padx, pady = max(10, size * 0.42), max(4, size * 0.16)
    align, (x, y) = _anchor(s)
    if align == 2:
        y1 = y + pady * 0.6
        y0 = y1 - th - 2 * pady
    elif align == 8:
        y0 = y - pady * 0.6
        y1 = y0 + th + 2 * pady
    else:
        y0, y1 = y - th / 2 - pady, y + th / 2 + pady
    x0, x1 = x - tw / 2 - padx, x + tw / 2 + padx
    fade = "\\fad(180,120)" if s["effect"] == "fade" else "\\fad(120,80)" if s["effect"] in ("pop", "slide") else ""
    shape = _round_rect(x0, y0, x1, y1, s["box_radius"])
    col = ass_color(s["box_color"])[4:]  # &H00BBGGRR → BBGGRR
    alpha = int(round(255 * s["box_alpha"] / 100))
    return (f"Dialogue: 0,{_t(cue['start'])},{_t(cue['end'])},Box,,0,0,0,,"
            f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H{col}&\\1a&H{alpha:02X}&{fade}\\p1}}{shape}{{\\p0}}")


def ass_document(cues, style, font_dirs=None):
    s = normalize(style)
    align, _ = _anchor(s)
    lr = 40 if s["full_width"] else 160
    rounded = s["box"] and s["box_radius"] > 0
    if rounded:  # плашку рисуем сами (скруглённую), текст — без обводки поверх неё
        border, outline_c, outline, shadow = 1, ass_color(s["box_color"], s["box_alpha"]), 0, 0
    elif s["box"]:
        border, outline_c, outline, shadow = 3, ass_color(s["box_color"], s["box_alpha"]), max(6, s["size"] // 5), 0
    else:
        border, outline_c, outline, shadow = 1, ass_color(s["outline_color"]), s["outline"], s["shadow"]
    primary, secondary = ass_color(s["color"]), ass_color(s["color"])
    if s["effect"] == "karaoke":  # в караоке «уже спетое» — Primary, ещё нет — Secondary
        primary = ass_color(s["highlight"])
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Main,{s['font']},{s['size']},{primary},{secondary},{outline_c},{ass_color(s['shadow_color'], s['shadow_alpha'])},"
        f"{-1 if s['bold'] else 0},0,0,0,100,100,0,0,{border},{outline},{shadow},{align},{lr},{lr},{s['margin_v']},1\n"
        + (f"Style: Box,Arial,20,&H00000000,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n" if rounded else "")
        + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    font = _font_for(s, font_dirs) if rounded else None
    lines = []
    for c in cues:
        if rounded:
            lines.append(_box_event(c, s, font))
        lines.append(f"Dialogue: 1,{_t(c['start'])},{_t(c['end'])},Main,,0,0,0,,{_cue_text(c, s)}")
    return head + "\n".join(lines) + "\n"


def write_ass(words, path, style, font_dirs=None):
    s = normalize(style)
    cues = build_cues(words, s["max_chars"], s["max_lines"])
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(ass_document(cues, s, font_dirs))
    return len(cues)


# ---------------------------------------------------------------- шрифты и отрисовка

def font_family(path):
    """Название семейства шрифта из файла (как его ищет libass)."""
    try:
        from PIL import ImageFont
        fam, _ = ImageFont.truetype(path, 20).getname()
        return fam or os.path.splitext(os.path.basename(path))[0]
    except Exception:
        return os.path.splitext(os.path.basename(path))[0]


def list_fonts(dirs):
    """[{"name", "file"}] из папок fonts, без повторов; плюс системный Arial."""
    out, seen = [], set()
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n.lower().endswith(FONT_EXT):
                p = os.path.join(d, n)
                fam = font_family(p)
                if fam.lower() not in seen:
                    seen.add(fam.lower())
                    out.append({"name": fam, "file": p})
    if "arial" not in seen:
        out.append({"name": "Arial", "file": ""})
    return out


def prepare_fonts(work_dir, family, dirs, sub="subfonts"):
    """Скопировать файл(ы) нужного шрифта в work_dir/sub — ffmpeg получит относительный путь без «C:\\»."""
    dst = os.path.join(work_dir, sub)
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    for f in list_fonts(dirs):
        if f["file"] and f["name"].lower() == (family or "").lower():
            try:
                shutil.copy(f["file"], dst)
            except OSError:
                pass
    return sub


CLASSIC_FONT = "Intro Demo Black CAPS"
CLASSIC_STYLE = ("FontName={font},FontSize=15,Bold=0,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H60000000,"
                 "Outline=1,Shadow=0.6,MarginV=12,Alignment=2")  # вид «Как было» — как в исходном KadroGen


def burn_classic(ffmpeg, src_mp4, srt_path, out_mp4, work_dir, font_dirs, creationflags=0, crf=20, venc=None, on_pct=None):
    """Субтитры «Как было» (.srt со старым стилем). Шрифта нет — Arial."""
    shutil.copy(srt_path, os.path.join(work_dir, "subs.srt"))
    sub = prepare_fonts(work_dir, CLASSIC_FONT, font_dirs)
    has_font = any(f["name"].lower() == CLASSIC_FONT.lower() and f["file"] for f in list_fonts(font_dirs))
    style = CLASSIC_STYLE.format(font=CLASSIC_FONT if has_font else "Arial")
    return burn(ffmpeg, src_mp4, "subs.srt", out_mp4, work_dir, sub, creationflags, crf=crf, force_style=style, venc=venc, on_pct=on_pct)


def _duration(ffmpeg, path, creationflags=0):
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    except Exception:
        return 0.0


def burn(ffmpeg, src_mp4, ass_name, out_mp4, work_dir, fonts_sub, creationflags=0, crf=20, force_style=None, venc=None, on_pct=None):
    """Вжечь субтитры в видео: перекодирование картинки один раз, звук копируется. ffmpeg работает из work_dir.
    venc(crf) — аргументы видеокодека (видеокарта или x264); on_pct(0..100) — ход работы."""
    vf = f"subtitles={ass_name}:fontsdir={fonts_sub}" + (f":force_style='{force_style}'" if force_style else "")
    total = _duration(ffmpeg, src_mp4, creationflags) if on_pct else 0.0
    tries = [venc(crf)] if venc else []
    tries.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf))])
    last_err = ""
    for k, codec in enumerate(tries):
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-i", src_mp4, "-vf", vf, *codec, "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out_mp4]
        proc = subprocess.Popen(cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)
        err = []
        th = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
        th.start()
        last = -1
        for line in proc.stdout:
            line = line.decode(errors="replace").strip()
            if on_pct and total and (line.startswith("out_time_us=") or line.startswith("out_time_ms=")):
                try:
                    p = max(0, min(99, int(int(line.split("=", 1)[1]) / 1e6 / total * 100)))
                except ValueError:
                    continue
                if p != last:
                    last = p
                    on_pct(p)
        proc.wait()
        th.join(timeout=5)
        if proc.returncode == 0:
            return
        last_err = (b"".join(err)).decode(errors="replace")
        if k == 0 and len(tries) > 1 and codec[1] != "libx264":
            continue  # видеокарта не вышла — тот же проход процессором
        break
    raise RuntimeError("ffmpeg (субтитры): " + last_err[-600:])


SAMPLE = "Пани Барбара боялась операции, но врач сказал: всё будет хорошо"


def preview(ffmpeg, style, out_path, work_dir, font_dirs, background=None, text=SAMPLE, creationflags=0):
    """Субтитр ровно как его нарисует сборка. out_path .mp4 — живой ролик в пару секунд (видно появление и караоке),
    .jpg/.png — один кадр. background — картинка или видео для фона (или серый фон)."""
    s = normalize(style)
    os.makedirs(work_dir, exist_ok=True)
    words, t = [], 0.4
    for w in text.split():
        d = 0.18 + len(w) * 0.05
        words.append({"text": w, "start": t, "end": t + d})
        t += d + 0.05
    cues = build_cues(words, s["max_chars"], s["max_lines"], max_sec=60, glue_gap=60, pause_break=60)
    cue = dict(cues[0], end=t + 0.8)
    with open(os.path.join(work_dir, "preview.ass"), "w", encoding="utf-8-sig") as f:
        f.write(ass_document([cue], s, font_dirs))
    sub = prepare_fonts(work_dir, s["font"], font_dirs)
    total = t + 1.2
    video = str(out_path).lower().endswith(".mp4")
    bg = background if background and os.path.isfile(background) else None
    if bg and bg.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
        src = ["-stream_loop", "-1", "-t", f"{total:.2f}", "-i", bg]
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=25,"
    elif bg:
        src = ["-loop", "1", "-framerate", "25", "-t", f"{total:.2f}", "-i", bg]
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
    else:
        src = ["-f", "lavfi", "-i", f"color=c=0x3a3f47:s={W}x{H}:d={total:.2f}:r=25"]
        vf = ""
    vf += f"subtitles=preview.ass:fontsdir={sub}"
    if video:
        out = [ "-vf", vf + ",scale=960:540", "-t", f"{total:.2f}", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    else:
        at = min(t * 0.65, t - 0.05) if s["effect"] in ("karaoke", "words") else max(0.9, t * 0.5)
        out = ["-vf", vf, "-ss", f"{at:.2f}", "-frames:v", "1", "-q:v", "3", out_path]
    r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", *src, *out],
                       cwd=work_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("предпросмотр субтитров: " + (r.stderr or "")[-400:])
    return out_path
