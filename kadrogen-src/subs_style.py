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
                      ("margin_v", 0, 500), ("max_chars", 10, 90), ("max_lines", 1, 3)):
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


def ass_document(cues, style):
    s = normalize(style)
    align, _ = _anchor(s)
    lr = 40 if s["full_width"] else 160
    if s["box"]:
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
        f"{-1 if s['bold'] else 0},0,0,0,100,100,0,0,{border},{outline},{shadow},{align},{lr},{lr},{s['margin_v']},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [f"Dialogue: 0,{_t(c['start'])},{_t(c['end'])},Main,,0,0,0,,{_cue_text(c, s)}" for c in cues]
    return head + "\n".join(lines) + "\n"


def write_ass(words, path, style):
    s = normalize(style)
    cues = build_cues(words, s["max_chars"], s["max_lines"])
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(ass_document(cues, s))
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


def burn(ffmpeg, src_mp4, ass_name, out_mp4, work_dir, fonts_sub, creationflags=0):
    """Вжечь .ass в видео: перекодирование картинки один раз, звук копируется. ffmpeg работает из work_dir."""
    vf = f"subtitles={ass_name}:fontsdir={fonts_sub}"
    r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", src_mp4, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out_mp4],
                       cwd=work_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg (субтитры): " + (r.stderr or "")[-600:])


SAMPLE = "Пани Барбара боялась операции, но врач сказал: всё будет хорошо"


def preview(ffmpeg, style, out_png, work_dir, font_dirs, background=None, text=SAMPLE, creationflags=0):
    """Один кадр с субтитром — ровно как его нарисует сборка. background — картинка ролика (или серый фон)."""
    s = normalize(style)
    os.makedirs(work_dir, exist_ok=True)
    words, t = [], 0.0
    for w in text.split():
        d = 0.18 + len(w) * 0.05
        words.append({"text": w, "start": t, "end": t + d})
        t += d + 0.05
    cues = build_cues(words, s["max_chars"], s["max_lines"], max_sec=60, glue_gap=60, pause_break=60)
    cue = dict(cues[0], end=t + 1.0)
    with open(os.path.join(work_dir, "preview.ass"), "w", encoding="utf-8-sig") as f:
        f.write(ass_document([cue], s))
    sub = prepare_fonts(work_dir, s["font"], font_dirs)
    at = min(t * 0.65, t - 0.05) if s["effect"] in ("karaoke", "words") else max(0.5, t * 0.5)
    if background and os.path.isfile(background):
        src = ["-loop", "1", "-framerate", "25", "-t", f"{t + 1:.2f}", "-i", background]
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
    else:
        src = ["-f", "lavfi", "-i", f"color=c=0x3a3f47:s={W}x{H}:d={t + 1:.2f}:r=25"]
        vf = ""
    r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", *src, "-vf", vf + f"subtitles=preview.ass:fontsdir={sub}",
                        "-ss", f"{at:.2f}", "-frames:v", "1", "-q:v", "3", out_png],
                       cwd=work_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    if r.returncode != 0 or not os.path.exists(out_png):
        raise RuntimeError("предпросмотр субтитров: " + (r.stderr or "")[-400:])
    return out_png
