"""Пайплайн: mp3 -> транскрипция -> сцены и промпты (LLM) -> картинки (-> оживление в видео) -> ролик 1920x1080.

Всё промежуточное складывается в jobs/<имя_mp3>/ и при перезапуске переиспользуется,
так что упавшую на середине работу можно просто запустить снова.

Запуск из консоли:  python pipeline.py файл.mp3 [--provider fastgen|nonstop|both] [--mode images|video] [--style cinematic] [--refs]
"""
import concurrent.futures as cf
import functools
import collections
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import requests
import endpoints
import netutil

from fastgen_api import BASE_URL, FastGen, app_dir, data_dir, file_to_data_uri
from nonstop_api import NonStop, NonStopError, MODELS as NONSTOP_MODELS
from yougen_api import YouGen, YouGenError, IMAGE_MODELS as YOUGEN_MODELS
from openai_api import OpenAIClient, DEFAULT_CHAT_MODEL as OPENAI_DEFAULT_MODEL
import subtitles as subs
import subs_style
from google_api import GoogleAI, GoogleError
from royaltechno_api import RoyalTechno, RoyalError, IMAGE_MODELS as ROYAL_MODELS
from secretslider_api import SecretSlider, SliderError
from llm_api import AnthropicClient, GeminiClient, AssemblyAI, LLM_SERVICES
from tts_api import make_tts, TTSError
import hashlib
import profiles

# ---------- настройки ----------
# fast-gen: режим "скорость" (Flower) и "качество" (Nano Banana Pro)
IMAGE_CHAINS = {
    "speed": [("flower_image_generate", 3)],
    "quality": [("gemini_nano_banana_2_image_generate", 2), ("nano_banana_2_image_generate", 1), ("nano_banana_pro_image_generate", 1), ("openai_image_generate", 1)],
}
# «качество» = те же модели с портретами героев (Flower референсы не принимает, в «скорости» их нет)
IMAGE_CHAINS_REFS = {
    "speed": [("gemini_nano_banana_2_image_generate", 2), ("nano_banana_2_image_generate", 1)],
    "quality": [("gemini_nano_banana_2_image_generate", 2), ("nano_banana_2_image_generate", 1), ("nano_banana_pro_image_generate", 1), ("openai_image_generate", 1)],
}
FASTGEN_QUALITY = "speed"  # переключается из настроек (общее для всех сервисов)
NONSTOP_CHAINS = {"speed": [("NARWHAL", 2), ("HARBOR_SEAL", 1), ("GEM_PIX_2", 1)], "quality": [("GEM_PIX_2", 2), ("NARWHAL", 1), ("HARBOR_SEAL", 1)]}
YOUGEN_CHAINS = {"speed": [("nano-banana-2", 2), ("nano-banana-2-lite", 1), ("nano-banana-pro", 1)], "quality": [("nano-banana-pro", 2), ("nano-banana-2", 1), ("nano-banana-2-lite", 1)]}
ROYAL_CHAINS = {"speed": [("nano-banana-2", 2), ("nano-banana-pro", 1)], "quality": [("nano-banana-pro", 2), ("nano-banana-2", 1)]}
IMAGE_CREDITS = {"flower_image_generate": 1, "gemini_nano_banana_2_image_generate": 1, "nano_banana_2_image_generate": 4, "nano_banana_pro_image_generate": 4, "openai_image_generate": 4}
HD_IMAGES = False  # «HD-картинки»: Nano Banana через fast-gen с upscale 2x — вдвое крупнее исходник, 8 кредитов вместо 4
HD_OPS = ("nano_banana_2_image_generate", "nano_banana_pro_image_generate", "nano_banana_2_lite_image_generate")


def image_credits(op):
    base = IMAGE_CREDITS.get(op, 4)
    return base * 2 if HD_IMAGES and op in HD_OPS else base
NONSTOP_CHAIN = [("GEM_PIX_2", 2), ("NARWHAL", 1), ("HARBOR_SEAL", 1)]
# оживление картинки в видео
VIDEO_CHAIN_FASTGEN = [("flower_video_from_image", 2), ("flow_video_from_keyframes", 1)]
VIDEO_CREDITS = {"flower_video_from_image": 1, "flow_video_from_keyframes": 1}
OMNI_SECONDS = (6, 8, 10)  # Omni Flash делает клип ровно 4/6/8/10 с за 1 кредит — для длинных сцен не нужен «замороженный хвост»


FG_IMAGE_MODEL = ""  # модель картинок fast-gen, выбранная для ролика ("" — автоматически, цепочкой)
FG_VIDEO_MODEL = ""  # модель оживления fast-gen
OMNI_OP = "flow_video_omni_flash_from_keyframes"
DEFAULT_FG_MODELS = {
    "image": [
        {
            "op": "flower_image_generate",
            "name": "Flower Nano Banana 2",
            "credits": 1
        },
        {
            "op": "gemini_nano_banana_2_image_generate",
            "name": "Gemini Nano Banana 2",
            "credits": 1
        },
        {
            "op": "nano_banana_2_image_generate",
            "name": "Nano Banana 2.1 (Flow)",
            "credits": 4
        },
        {
            "op": "nano_banana_pro_image_generate",
            "name": "Nano Banana Pro (Flow)",
            "credits": 4
        },
        {
            "op": "nano_banana_2_lite_image_generate",
            "name": "Nano Banana 2 Lite (Flow)",
            "credits": 4
        },
        {
            "op": "openai_image_generate",
            "name": "ChatGPT Image (OpenAI)",
            "credits": 4
        }
    ],
    "video": [
        {
            "op": "flow_video_from_keyframes",
            "name": "Veo 3.1 Fast (Flow)",
            "credits": 1
        },
        {
            "op": "flow_video_lite_from_keyframes",
            "name": "Veo 3.1 Lite (Flow)",
            "credits": 1
        },
        {
            "op": "flow_video_quality_from_keyframes",
            "name": "Veo 3.1 Quality (Flow)",
            "credits": 10
        },
        {
            "op": OMNI_OP,
            "name": "Omni Flash 1.1 (Flow), клип 6/8/10 с",
            "credits": 1,
            "by_duration": True
        },
        {
            "op": "flower_video_from_image",
            "name": "Flower Omni",
            "credits": 1
        }
    ]
}


def fastgen_models():
    """Модели fast-gen для выбора в окне: fastgen_models.json рядом с программой (его можно дописывать), иначе встроенный список."""
    for d in (data_dir(), app_dir()):
        p = os.path.join(d, "fastgen_models.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8-sig") as f:
                    data = json.load(f)
                out = {k: [m for m in data.get(k) or [] if isinstance(m, dict) and m.get("op")] for k in ("image", "video")}
                if out["image"] or out["video"]:
                    return out
            except (OSError, ValueError) as e:
                log(f"fastgen_models.json не прочитан: {e}")
    return DEFAULT_FG_MODELS


def apply_fg_models(cfg):
    """Выбор моделей fast-gen для этого ролика + их цены в кредитах (для лимита в час)."""
    global FG_IMAGE_MODEL, FG_VIDEO_MODEL
    models = fastgen_models()
    for m in models.get("image", []):
        if m.get("credits"):
            IMAGE_CREDITS[m["op"]] = int(m["credits"])
    for m in models.get("video", []):
        if m.get("credits"):
            VIDEO_CREDITS[m["op"]] = int(m["credits"])
    FG_IMAGE_MODEL = str(cfg.get("fg_image_model") or "").strip()
    FG_VIDEO_MODEL = str(cfg.get("fg_video_model") or "").strip()
    return FG_IMAGE_MODEL, FG_VIDEO_MODEL


def video_chain_for(scene):
    dur = float(scene.get("duration") or 0)
    n = next((s for s in OMNI_SECONDS if s >= dur), OMNI_SECONDS[-1])
    if dur <= 5.0:
        chain = list(VIDEO_CHAIN_FASTGEN)
    else:
        chain = [(f"{OMNI_OP}_{n}s", 1)] + list(VIDEO_CHAIN_FASTGEN)
    if FG_VIDEO_MODEL:  # выбранная модель — первой; если не отдаст клип, дальше обычная цепочка
        op = f"{OMNI_OP}_{n}s" if FG_VIDEO_MODEL == OMNI_OP else FG_VIDEO_MODEL
        VIDEO_CREDITS.setdefault(op, VIDEO_CREDITS.get(FG_VIDEO_MODEL, 1))
        chain = [(op, 2)] + [x for x in chain if x[0] != op]
    return chain
YOUGEN_CHAIN = [("nano-banana-2", 2), ("nano-banana-pro", 1), ("nano-banana-2-lite", 1)]
ROYAL_CHAIN = [("nano-banana-2", 2), ("nano-banana-pro", 1)]
PROVIDERS = ("fastgen", "nonstop", "yougen", "royal", "slider", "google")
PROVIDER_NAMES = {"fastgen": "fast-gen", "nonstop": "veononstop", "yougen": "yougen", "royal": "royaltechno", "slider": "secretslider", "google": "Google"}
SINGLE_KEY = {"fastgen": "fastgen_api_key", "nonstop": "veononstop_api_key", "yougen": "yougen_api_key", "royal": "royaltechno_api_key",
              "slider": "secretslider_api_key", "google": "google_api_key"}
# Google напрямую: скорость — Imagen 4 Fast (референсы не принимает), качество — Nano Banana 2 → Pro с портретами героев
GOOGLE_CHAINS = {"speed": [("imagen-fast", 3)], "quality": [("nb2", 1), ("nbpro", 1), ("imagen", 1)]}
GOOGLE_CHAINS_REFS = {"speed": [("nb2", 2)], "quality": [("nb2", 1), ("nbpro", 1)]}


def provider_keys(cfg, p):
    """Все ключи сервиса: список config.image_keys[p], либо одиночное старое поле."""
    keys = [k.strip() for k in ((cfg.get("image_keys") or {}).get(p) or []) if isinstance(k, str) and k.strip()]
    if not keys and cfg.get(SINGLE_KEY[p]):
        keys = [cfg[SINGLE_KEY[p]].strip()]
    return keys


def base_of(unit):
    """'fastgen#2' -> 'fastgen' (один сервис может работать несколькими ключами)."""
    return unit.split("#")[0]


def unit_name(unit):
    b, _, n = unit.partition("#")
    return PROVIDER_NAMES[b] + (f" (ключ {n})" if n else "")
TEXT_ENGINES = ("fastgen", "openai")  # кто распознаёт речь и пишет промпты
MODES = ("images", "video", "mixed")  # mixed — через одну: видео, картинка, видео...
TRANSITION = 0.5  # секунд на переход в моушн-режиме
SCENE_MIN, SCENE_MAX, SCENE_TARGET = 3.0, 7.0, 5.0
FPS = 25
W, H = 1920, 1080
INLINE_AUDIO_LIMIT = 15 * 1024 * 1024
FG_CHUNK_SECONDS = 1200  # fast-gen: до 25 МБ на файл, режем по 20 минут
DEFAULT_LLM_MODEL = "openai/gpt-5-2"  # сильнее держит конкретику фразы и реже тащит героя в кадр
DEFAULT_LLM_FALLBACK = "openai/gpt-4.1-mini"  # если основная модель недоступна
MAX_LIMIT_WAIT = 3 * 3600  # сколько одна сцена может ждать часовой лимит fast-gen

STYLES = {
    # ключ: (название по-русски, подсказка для модели на английском, название по-английски)
    "ultrareal": ("Ультрареализм", "ultra-realistic 16K photograph indistinguishable from reality, physically accurate natural lighting, "
                  "true-to-life skin, fabric and material textures, professional cinema camera, subtle depth of field, "
                  "no CGI or illustration look, no stylization", "Ultra-realism"),
    "cinematic": ("Кинокадр", "cinematic film still from a feature movie, anamorphic widescreen framing, subtle 35mm film grain, "
                  "dramatic motivated lighting with soft shadows, rich graded color palette, shallow depth of field, "
                  "photoreal people and places", "Cinematic"),
    "noir": ("Нуар", "classic film noir, high-contrast black and white photograph, deep shadows and hard key light, "
             "venetian blind light patterns, rain-slick streets, cigarette smoke haze, 1940s atmosphere, photoreal", "Noir"),
    "retro": ("Ретро 80–90-х", "late 1980s to 1990s snapshot on a cheap point-and-shoot film camera, warm faded colors, light leaks "
              "and soft focus, mild flash glare, home archive photo look, authentic period clothing and interiors", "Retro 80s–90s"),
    "oil": ("Масляная живопись", "classical oil painting on canvas, visible expressive brushstrokes and impasto texture, "
            "old-master chiaroscuro lighting, warm earthy palette, museum-quality fine art, no photographic look", "Oil painting"),
    "watercolor": ("Акварель", "soft watercolor painting on textured paper, translucent washes with gentle color bleeding, "
                   "loose expressive brushwork, paper grain showing through, airy and emotional illustration", "Watercolor"),
    "sketch": ("Карандашный скетч", "hand-drawn pencil sketch on paper, graphite hatching and cross-hatching, loose confident linework, "
               "subtle smudged shading, monochrome with paper texture, artist's sketchbook look", "Pencil sketch"),
    "comic": ("Комикс", "bold comic book illustration, thick black ink outlines, flat vivid colors with halftone dot shading, "
              "dynamic composition, clean cel shading, printed comic page look, no speech bubbles", "Comic book"),
    "graphic": ("Графический роман", "dark graphic novel illustration, heavy ink shadows and stark high-contrast lighting, "
                "gritty textured brushwork, muted desaturated palette with a single accent color, mature cinematic mood", "Graphic novel"),
    "manga": ("Манга", "black and white manga illustration, clean precise ink lines, screentone shading, expressive large eyes, "
              "dynamic composition, high detail, Japanese comic look, no panels or text", "Manga"),
    "anime": ("Аниме", "anime illustration in the style of a theatrical animated feature, clean linework, soft cel shading, "
              "painterly detailed backgrounds, luminous lighting, expressive characters with large eyes", "Anime"),
    "cartoon2d": ("2D-мультфильм", "2D animated cartoon frame, clean bold outlines, flat bright colors, simplified expressive characters, "
                  "stylized backgrounds with soft gradients, modern TV animation look", "2D cartoon"),
    "cartoon3d": ("3D-мультфильм", "3D animated feature film still, rounded appealing stylized characters, soft subsurface skin shading, "
                  "soft global illumination, rich detailed sets, polished CGI render, big-studio animation look", "3D cartoon"),
    "clay": ("Пластилин", "stop-motion claymation scene, handmade plasticine characters with visible fingerprints and tool marks, "
             "miniature set with real fabric and wood props, soft studio lighting, slight tilt-shift look", "Claymation"),
    "stickman": ("Стикмен", "minimalist stick figure illustration, simple round heads and thin black line bodies, flat white or light "
                 "background, a few flat accent colors, clean vector look, friendly explainer style", "Stick figures"),
}
CUSTOM_STYLE = "custom"  # «Свой стиль»: описание пользователь пишет сам (config custom_style)
DEFAULT_STYLE = "ultrareal"


def resolve_style(style_key, cfg):
    """→ (ключ для кэша библии, название, подсказка для модели). «Свой стиль» берёт текст из настроек;
    в ключ кэша входит хэш текста, чтобы правка описания перерисовала ролик."""
    if style_key == CUSTOM_STYLE:
        text = " ".join(str((cfg or {}).get("custom_style") or "").split())
        if text:
            return f"custom-{hashlib.md5(text.encode('utf-8')).hexdigest()[:8]}", "Свой стиль", text
        style_key = DEFAULT_STYLE
    if style_key not in STYLES:
        style_key = DEFAULT_STYLE
    return style_key, STYLES[style_key][0], STYLES[style_key][1]


def normalize_providers(value):
    """Принимает строку ('fastgen', 'both', 'fastgen,yougen') или список; возвращает список в порядке PROVIDERS."""
    if not value:
        return ["fastgen"]
    if isinstance(value, str):
        value = ["fastgen", "nonstop"] if value == "both" else re.split(r"[,\s]+", value.strip())
    chosen = [p for p in PROVIDERS if p in value]
    return chosen or ["fastgen"]

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOG_PATH = os.path.join(data_dir(), "videogen.log")

# флаг отмены: ставится из окна, проверяется во всех длинных циклах
CANCEL = threading.Event()

# что программа ждёт прямо сейчас (лимит сервиса) — окно показывает это с таймером, чтобы не казалось, что всё зависло
WAIT = {}


def set_wait(kind, until=None):
    WAIT.update({"kind": kind, "until": until, "since": WAIT.get("since") or time.time()})


def clear_wait(kind=None):
    if not kind or WAIT.get("kind") == kind:
        WAIT.clear()


class Retryable(SystemExit):
    """Остановка, которую стоит повторить позже: сервис не отдаёт картинки, модель не написала часть.
    Окно показывает её как обычную ошибку, а автоповтор запускает ролик снова через паузу."""


LAST_FAILURES = {"images": 0, "videos": 0}  # что не получилось в последнем собранном ролике — для доводки


class Cancelled(Exception):
    pass


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled("Остановлено пользователем")


netutil.CANCEL_CHECK = check_cancel  # циклы ожидания в клиентах сервисов теперь тоже слышат «Стоп»


def font_dirs():
    """Папки со шрифтами для субтитров: внутри программы (_internal/fonts) и рядом с ней (fonts) — туда можно класть свои."""
    out = []
    for d in (os.path.join(getattr(sys, "_MEIPASS", app_dir()), "fonts"), os.path.join(app_dir(), "fonts")):
        if d not in out:
            out.append(d)
    return out


def sub_style_of(cfg):
    """Свой вид субтитров из настроек или None — тогда старый вид («Как было»)."""
    st = cfg.get("sub_style")
    return st if isinstance(st, dict) and st and not st.get("classic") else None


def subtitle_words(cfg, job_dir, tr, had_script):
    """Слова для субтитров: текст из сценария со временем из распознавания (если сценарий был и похож на озвучку),
    иначе распознанные слова. cfg["sub_source"] == "asr" — всегда по распознаванию."""
    if not had_script or cfg.get("sub_source") == "asr":
        return tr["words"]
    try:
        with open(os.path.join(job_dir, "script.txt"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return tr["words"]
    words = subs_style.align_script(text, tr["words"])
    if not words:
        log("субтитры: сценарий не похож на озвучку — беру распознанный текст")
        return tr["words"]
    log(f"субтитры: текст из сценария ({len(words)} слов), время — по озвучке")
    return words


def ffmpeg_bin(name="ffmpeg"):
    p = os.path.join(app_dir(), "ffmpeg", name + ".exe")
    return p if os.path.exists(p) else name


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class Progress:
    def __init__(self, cb=None):
        self.cb = cb

    def __call__(self, stage, done=0, total=0, message=""):
        log(f"[{stage}] {message} {f'{done}/{total}' if total else ''}")
        if self.cb:
            self.cb(stage, done, total, message)


# ============ утилиты ============

def read_json(path, default=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for attempt in range(10):  # файл может держать антивирус или параллельное чтение — ждём и пробуем снова
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.15 * (attempt + 1))


def run_ffmpeg(args, check=True):
    r = subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", *args], capture_output=True, creationflags=NO_WINDOW)
    if check and r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode(errors="replace")[:400])
    return r


def audio_duration(path):
    out = subprocess.run(
        [ffmpeg_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True, creationflags=NO_WINDOW,
    ).stdout.strip()
    return float(out)


def load_config():
    path = os.path.join(data_dir(), "config.json")
    if not os.path.exists(path):
        raise SystemExit("Не найден config.json — введите ключи в окне программы.")
    for attempt in range(5):  # окно в этот момент может переписывать файл — подождём
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            endpoints.configure(cfg)  # свои адреса API, если заданы
            return cfg
        except (PermissionError, json.JSONDecodeError):
            if attempt == 4:
                raise
            time.sleep(0.15 * (attempt + 1))


def make_text_engine(cfg):
    """Возвращает (stt_name, stt_api, llm_name, llm_api).
    С ключом fast-gen всё идёт через него; без него — распознавание Whisper/AssemblyAI, тексты OpenAI/Claude/Gemini."""
    has_fg = bool(cfg.get("fastgen_api_key"))
    stt = (cfg.get("stt_engine") or ("fastgen" if has_fg else "whisper")).lower()
    llm = (cfg.get("llm_engine") or ("fastgen" if has_fg else "openai")).lower()
    if stt == "fastgen" and not has_fg:
        stt = "whisper"
    if llm == "fastgen" and not has_fg:
        llm = "openai"
    fg = FastGen(cfg["fastgen_api_key"]) if has_fg else None
    if stt == "fastgen":
        stt_api = fg
    elif stt == "assemblyai":
        if not cfg.get("assemblyai_api_key"):
            raise SystemExit("Для распознавания речи выбран AssemblyAI, но ключ не введён.")
        stt_api = AssemblyAI(cfg["assemblyai_api_key"])
    else:
        if not cfg.get("openai_api_key"):
            raise SystemExit("Для распознавания речи нужен ключ OpenAI (Whisper).")
        stt_api = OpenAIClient(cfg["openai_api_key"])
    if llm == "fastgen":
        llm_api = fg
    elif llm == "anthropic":
        if not cfg.get("anthropic_api_key"):
            raise SystemExit("Для текстов выбран Claude, но ключ Anthropic не введён.")
        llm_api = AnthropicClient(cfg["anthropic_api_key"], cfg.get("anthropic_model"))
    elif llm == "gemini":
        if not cfg.get("gemini_api_key"):
            raise SystemExit("Для текстов выбран Gemini, но ключ Google не введён.")
        llm_api = GeminiClient(cfg["gemini_api_key"], cfg.get("gemini_model"))
    else:
        if not cfg.get("openai_api_key"):
            raise SystemExit("Для текстов выбран OpenAI, но ключ не введён.")
        llm_api = OpenAIClient(cfg["openai_api_key"])
    return stt, stt_api, llm, llm_api


def short_base(name, keep=32):
    """Короткое безопасное имя для папки задания и выходного файла.
    Озвучка из Telegram-ботов приходит с именем в целое предложение; вместе с jobs/<имя>/clips_m/0123.mp4
    путь упирался в лимит Windows (260 символов) — WinError 206 «имя файла слишком длинное»."""
    base = re.sub(r"[^\w\-]+", "_", name).strip("_") or "audio"
    if len(base) <= 40:
        return base
    return base[:keep].rstrip("_") + "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def job_dir_for(audio_path):
    name = os.path.splitext(os.path.basename(audio_path))[0]
    base = short_base(name)
    root = os.path.join(data_dir(), "jobs")
    d = os.path.join(root, base)
    old = os.path.join(root, re.sub(r"[^\w\-]+", "_", name))
    if old != d and not os.path.exists(d) and os.path.isdir(old):
        try:
            os.rename(old, d)  # задание из старой версии с длинным именем — переносим, чтобы не делать заново
        except OSError:
            pass
    os.makedirs(d, exist_ok=True)
    return d, base


# ============ шаг 1: транскрипция ============

def clean_words(words):
    """Распознаватель иногда отдаёт слова с обратным скачком времени (в середине часового ролика вдруг 12 с).
    Такие слова выбрасываем, остальные делаем монотонными — иначе сцена получает отрицательную длительность."""
    out, last_end = [], 0.0
    for w in words or []:
        try:
            s, e = float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e < s or s < last_end - 1.5 or e - s > 30:
            continue
        s = max(s, last_end)
        e = max(e, s + 0.05)
        out.append({**w, "start": s, "end": e})
        last_end = e
    return out


def stt_fallbacks(engine):
    """Запасные распознаватели по ключам из настроек (кроме того, что уже выбран)."""
    cfg = load_config()
    out = []
    if engine != "fastgen" and cfg.get("fastgen_api_key"):
        out.append(("fastgen", FastGen(cfg["fastgen_api_key"])))
    if engine != "assemblyai" and cfg.get("assemblyai_api_key"):
        out.append(("assemblyai", AssemblyAI(cfg["assemblyai_api_key"])))
    if engine not in ("openai", "whisper") and cfg.get("openai_api_key"):
        out.append(("whisper", OpenAIClient(cfg["openai_api_key"])))
    return out


def transcribe(engine, api, audio_path, job_dir, progress, duration=None):
    try:
        data = _transcribe_raw(engine, api, audio_path, job_dir, progress, duration)
    except Cancelled:
        raise
    except Exception as e:
        first = e
        fb = stt_fallbacks(engine)
        if not fb:
            raise
        log(f"распознавание ({engine}) не удалось: {str(e)[:200]} — пробую запасной сервис")
        data = None
        for eng2, api2 in fb:
            try:
                progress("transcribe", 0, 1, f"{engine} не ответил, распознаю через {eng2}")
                data = _transcribe_raw(eng2, api2, audio_path, job_dir, progress, duration)
                break
            except Cancelled:
                raise
            except Exception as e2:
                log(f"запасное распознавание ({eng2}) тоже не удалось: {str(e2)[:200]}")
        if data is None:
            raise first
    n = len(data.get("words") or [])
    data["words"] = clean_words(data.get("words"))
    if len(data["words"]) < n:
        log(f"транскрипция: выброшено {n - len(data['words'])} слов с битыми таймкодами")
    return data


def _transcribe_raw(engine, api, audio_path, job_dir, progress, duration=None):
    """engine: 'fastgen' (Gemini через fast-gen) или 'openai' (Whisper)."""
    out = os.path.join(job_dir, "transcript.json")
    cached = read_json(out)
    if cached:
        progress("transcribe", 1, 1, "транскрипция уже есть, пропускаю")
        return cached
    if engine == "assemblyai":
        progress("transcribe", 0, 1, "распознаю речь (AssemblyAI)")
        data = api.transcribe(audio_path, progress=lambda m: progress("transcribe", 0, 1, m))
        if not data["words"]:
            raise RuntimeError("Распознавание вернулось без таймкодов слов")
        write_json(out, data)
        progress("transcribe", 1, 1, f"распознано слов: {len(data['words'])}")
        return data
    if engine in ("openai", "whisper"):
        progress("transcribe", 0, 1, "распознаю речь (OpenAI Whisper)")
        data = api.transcribe(audio_path, ffmpeg_bin(), ffmpeg_bin("ffprobe"), duration or audio_duration(audio_path), progress=progress)
        if not data["words"]:
            raise RuntimeError("Распознавание вернулось без таймкодов слов")
        write_json(out, data)
        progress("transcribe", 1, 1, f"распознано слов: {len(data['words'])}")
        return data
    # fast-gen принимает аудио до 25 МБ. Исходный mp3 не отправляем никогда (бывает 320 кбит/с стерео):
    # всегда пережимаем в моно 48 кбит/с (20 минут ≈ 7 МБ) и длинную озвучку режем по FG_CHUNK_SECONDS.
    duration = duration or audio_duration(audio_path)
    n_chunks = 1 if duration <= FG_CHUNK_SECONDS + 60 else int(math.ceil(duration / FG_CHUNK_SECONDS))
    words, texts = [], []
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        def do_chunk(k):
            texts, words = [], []
            check_cancel()
            start = k * FG_CHUNK_SECONDS
            part = os.path.join(tmp, f"part{k:03d}.mp3")
            cut = [] if n_chunks == 1 else ["-ss", str(start), "-t", str(FG_CHUNK_SECONDS)]
            run_ffmpeg([*cut, "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", part])
            if os.path.getsize(part) > 20 * 1024 * 1024:
                raise RuntimeError("Кусок аудио получился больше 20 МБ — проверьте файл озвучки")
            label = "" if n_chunks == 1 else f", часть {k + 1} из {n_chunks}"
            progress("transcribe", k, max(1, n_chunks), "загружаю аудио" + label)
            # загрузка нескольких мегабайт на слабом интернете может оборваться («write operation timed out») —
            # это не повод ронять весь ролик, повторяем
            inp = netutil.patient(lambda: api.upload_file(part) if os.path.getsize(part) > INLINE_AUDIO_LIMIT else file_to_data_uri(part),
                                  tries=6)
            progress("transcribe", k, max(1, n_chunks), "распознаю речь (Gemini)" + label)
            for attempt in range(8):  # service.overloaded / generation.failed у fast-gen — временно (обычно 10-20 мин), ждём и повторяем
                st = netutil.patient(lambda: api.run({"operation": "aistudio_gemini_3.5_transcribe", "inputs": [inp],
                                                      "word_timestamps": True, "diarization": False}, poll=3, max_wait=1800), tries=6)
                if st.get("status") == "succeeded":
                    break
                err = f"{st.get('error_code')} {st.get('error')}"
                if attempt == 7 or not any(x in err for x in ("overloaded", "generation.failed", "try again", "accounts_exhausted")):
                    raise RuntimeError(f"Транскрипция не удалась: {err}")
                wait = min(300, 60 * (attempt + 1))
                log(f"транскрипция: {err[:80]} — жду {wait} с и повторяю ({attempt + 1}/7)")
                progress("transcribe", k, max(1, n_chunks), f"сервис распознавания перегружен, жду {wait} с и повторяю")
                for _ in range(wait):
                    check_cancel()
                    time.sleep(1)
            r = st["results"][0]
            texts.append((r.get("text") or "").strip())
            for seg in (r.get("transcription") or {}).get("segments") or []:
                for w in seg.get("words") or []:
                    if w.get("start_seconds") is None:
                        continue
                    words.append({"text": w["text"], "start": float(w["start_seconds"]) + start,
                                  "end": float(w.get("end_seconds") or w["start_seconds"]) + start})
            return texts, words

        # куски длинной озвучки распознаём параллельно (по три), порядок слов восстанавливаем по номеру куска
        with cf.ThreadPoolExecutor(max_workers=max(1, min(3, n_chunks))) as ex:
            parts_out = list(ex.map(do_chunk, range(n_chunks)))
    texts = [x for ts, _ in parts_out for x in ts]
    words = [w for _, ws in parts_out for w in ws]
    if not words:
        raise RuntimeError("Транскрипция вернулась без таймкодов слов")
    data = {"text": " ".join(texts), "words": words}
    write_json(out, data)
    progress("transcribe", 1, 1, f"распознано слов: {len(words)}")
    return data


# ============ шаг 2: разбиение на сцены ============

def split_scenes(words, total_duration):
    phrases, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        strong = re.search(r"[.!?…;]$", w["text"]) is not None
        weak = re.search(r"[,:—-]$", w["text"]) is not None
        pause = nxt is not None and (nxt["start"] - w["end"]) > 0.6
        if strong or weak or pause or nxt is None:
            phrases.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                            "text": " ".join(x["text"] for x in cur), "strong": strong or pause or nxt is None})
            cur = []
    scenes, buf = [], []

    def flush():
        if buf:
            scenes.append({"start": buf[0]["start"], "end": buf[-1]["end"], "text": " ".join(p["text"] for p in buf)})
            buf.clear()

    for p in phrases:
        if not buf:
            buf.append(p)
            continue
        cand_len = p["end"] - buf[0]["start"]
        cur_len = buf[-1]["end"] - buf[0]["start"]
        if cand_len <= SCENE_MAX and (cur_len < SCENE_MIN or (cand_len <= SCENE_TARGET + 1.0 and not buf[-1]["strong"])):
            buf.append(p)
        elif cand_len <= SCENE_MAX and cur_len < SCENE_TARGET:
            buf.append(p)
        else:
            flush()
            buf.append(p)
    flush()
    fixed = []
    for s in scenes:
        length = s["end"] - s["start"]
        if length > SCENE_MAX + 1.0:
            n = int(length // SCENE_TARGET) + 1
            ws = [w for w in words if w["start"] >= s["start"] - 1e-6 and w["end"] <= s["end"] + 1e-6]
            per = max(1, len(ws) // n)
            for k in range(0, len(ws), per):
                chunk = ws[k:k + per]
                fixed.append({"start": chunk[0]["start"], "end": chunk[-1]["end"], "text": " ".join(x["text"] for x in chunk)})
        else:
            fixed.append(s)
    if len(fixed) > 1 and (fixed[-1]["end"] - fixed[-1]["start"]) < SCENE_MIN * 0.6:
        last = fixed.pop()
        fixed[-1]["end"] = last["end"]
        fixed[-1]["text"] += " " + last["text"]
    for i, s in enumerate(fixed):
        s["index"] = i
        s["start"] = 0.0 if i == 0 else fixed[i - 1]["end_cut"]
        s["end_cut"] = fixed[i + 1]["start"] if i + 1 < len(fixed) else max(total_duration, s["end"])
    for s in fixed:
        s["end"] = s.pop("end_cut")
        if s["end"] < s["start"] + 0.3:
            s["end"] = s["start"] + 0.3
        s["duration"] = round(s["end"] - s["start"], 3)
    return fixed


def scenes_ok(scenes):
    """Кэш сцен пригоден: длительности положительные и сцены идут по порядку (старые версии до 0.29 писали отрицательные)."""
    try:
        prev = 0.0
        for s in scenes:
            if float(s["duration"]) <= 0 or float(s["end"]) < float(s["start"]) or float(s["start"]) < prev - 0.01:
                return False
            prev = float(s["start"])
        return True
    except (KeyError, TypeError, ValueError):
        return False


# ============ шаг 3: LLM (чат-модели через fast-gen) ============

def extract_json(text):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        raise json.JSONDecodeError("нет JSON в ответе", t, 0)
    return json.loads(t[i:j + 1])


class LLM:
    """Языковая модель: чат-эндпоинт fast-gen (формат OpenAI) или сам OpenAI."""

    def __init__(self, engine, api, model=None):
        self.engine, self.api = engine, api
        self.model = model or (OPENAI_DEFAULT_MODEL if engine == "openai" else DEFAULT_LLM_MODEL)
        if engine in ("anthropic", "gemini"):
            self.model = getattr(api, "model", engine)

    progress = None  # ставится из make_video: сообщения об ожидании лимита видны на экране

    def _wait_minute(self, why):
        if self.progress:
            self.progress("prompts", 0, 0, why)
        log(why)
        set_wait("llm", time.time() + 60)
        for _ in range(60):
            check_cancel()
            time.sleep(1)

    fallback = None  # запасная модель (Claude/OpenAI по своему ключу), если у fast-gen кончился часовой лимит токенов

    def chat_json(self, system, user, max_tokens=4000):
        check_cancel()
        if self.fallback is not None and getattr(self, "_switched", False):
            return self.fallback.chat_json(system, user, max_tokens=max_tokens)
        if self.engine in ("anthropic", "gemini"):
            return self.api.chat_json(system, user, max_tokens=max_tokens)
        if self.engine == "openai":
            return self.api.chat_json(system + "\nОтвечай только JSON, без пояснений.", user, model=self.model, max_tokens=max_tokens)
        last = None
        attempt, limit_waits, limit = 0, 0, max_tokens
        while attempt < 4:
            check_cancel()
            try:
                r = self.api.s.post(BASE_URL + "/v1/chat/completions", json={
                    "model": self.model, "temperature": 0.7, "max_tokens": limit,
                    "messages": [{"role": "system", "content": system + "\nОтвечай только JSON, без пояснений."},
                                 {"role": "user", "content": user}],
                }, timeout=300)
                if r.status_code == 429 and self.fallback is not None:
                    # лимит токенов fast-gen кончился, а у пользователя есть свой ключ Claude/OpenAI — не ждём час, переходим
                    self._switched = True
                    msg = f"fast-gen: часовой лимит токенов исчерпан — тексты дальше через {self.fallback.engine} ({self.fallback.model})"
                    log(msg)
                    if self.progress:
                        self.progress("prompts", 0, 0, msg)
                    clear_wait("llm")
                    return self.fallback.chat_json(system, user, max_tokens=max_tokens)
                if r.status_code == 429 and limit_waits < 70:
                    # fast-gen: 200k токенов текста в час. На длинном ролике лимит кончается на середине —
                    # ждём по минуте, пока окно не освободится; попытка не сгорает.
                    limit_waits += 1
                    last = f"HTTP 429: {r.text[:200]}"
                    self._wait_minute("fast-gen: часовой лимит токенов для текстов исчерпан, жду минуту (продолжу автоматически)")
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    attempt += 1
                    if attempt >= 2 and self.model != DEFAULT_LLM_FALLBACK and DEFAULT_LLM_FALLBACK:
                        log(f"модель {self.model} не отвечает ({last[:80]}), перехожу на {DEFAULT_LLM_FALLBACK}")
                        self.model = DEFAULT_LLM_FALLBACK
                    time.sleep(10 * attempt)
                    continue
                if r.status_code >= 400:
                    if self.model != DEFAULT_LLM_FALLBACK and DEFAULT_LLM_FALLBACK:
                        log(f"модель {self.model} недоступна ({r.status_code}), перехожу на {DEFAULT_LLM_FALLBACK}")
                        self.model = DEFAULT_LLM_FALLBACK
                        continue
                    raise RuntimeError(f"fast-gen chat {r.status_code}: {r.text[:300]}")
                choice = r.json()["choices"][0]
                if choice.get("finish_reason") == "length" and limit < 32000:
                    limit = min(32000, limit * 2)
                    log(f"ответ модели не влез в лимит, повторяю с {limit} токенами")
                    continue
                out = extract_json(choice["message"]["content"])
                clear_wait("llm")
                return out
            except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
                last = str(e)
                attempt += 1
                if attempt >= 2 and self.model != DEFAULT_LLM_FALLBACK and DEFAULT_LLM_FALLBACK:
                    log(f"модель {self.model} отвечает некорректно ({last[:80]}), перехожу на {DEFAULT_LLM_FALLBACK}")
                    self.model = DEFAULT_LLM_FALLBACK
                time.sleep(5 * attempt)
        raise RuntimeError(f"Языковая модель не ответила корректно: {last}")


STYLE_SYSTEM = """Ты арт-директор видеоролика. По тексту озвучки напиши "библию" ролика — единый визуальный стиль,
персонажей, места и важные предметы, чтобы картинки от разных генераций выглядели как один фильм.
Заданный пользователем стиль (обязательно положи его в основу): {style_hint}
Ответ строго JSON:
{{
 "summary": "о чём ролик, 1-2 предложения (по-русски)",
 "style": "единый стиль на английском, 40-70 слов, начинающийся с заданного стиля: свет, объектив, палитра, время суток, настроение, текстуры. Без текста и надписей в кадре.",
 "characters": [
   {{"name": "имя или роль по-русски, как в тексте",
    "latin": "короткое имя латиницей без пробелов, например Barsik или Owner",
    "description": "ПОДРОБНОЕ описание на английском, 30-50 слов, которое будет вставляться в каждый промпт без изменений. ОБЯЗАТЕЛЬНО конкретные цвета: цвет шерсти/волос, цвет глаз, для людей — пол, возраст, причёска, борода/усы, и КОНКРЕТНАЯ одежда с цветами (например: dark green wool sweater, brown corduroy trousers, black rubber boots). Отметины, шрамы, возраст, телосложение. Никаких слов 'simple', 'casual', 'some' — только конкретика.",
    "anchors": "5-8 коротких визуальных якорей лица/морды через запятую, на английском, по которым героя узнают в любом кадре: форма лица, брови, нос, глаза, родинка/шрам, причёска, усы. Например: round face, thick grey eyebrows, hooked nose, pale blue eyes, white moustache, short silver hair"}}
 ],
 "locations": [
   {{"name": "место по-русски", "description": "постоянные детали места на английском, 15-30 слов, с цветами материалов"}}
 ],
 "props": [
   {{"name": "предмет по-русски, как в тексте", "latin": "короткое имя латиницей, например Watch",
    "description": "описание на английском, 10-25 слов, с цветами и материалами, чтобы предмет был одинаковым во всех кадрах",
    "owners": ["имя персонажа из characters, кто им пользуется"]}}
 ],
 "avoid": "чего избегать, на английском, коротко"
}}
В characters включи ВСЕХ повторяющихся персонажей (людей и животных), которые упомянуты в тексте. Не придумывай персонажей, которых в тексте нет.
Если в тексте один хозяин — это один конкретный человек, укажи его пол и внешность один раз и навсегда.
В props — только предметы, которые важны для истории и появляются больше одного раза (часы, письмо, скворечник, нож, машина). Не больше 6."""

SCENES_SYSTEM = """Ты режиссёр и оператор: пишешь раскадровку для генерации кадров видеоролика.
Тебе дана библия (стиль, персонажи, места, предметы) и список сцен (номер, длительность, текст озвучки).
Для КАЖДОЙ сцены опиши кадр. Главные правила:
1. КОНКРЕТИКА ФРАЗЫ ВАЖНЕЕ ВСЕГО. Найди в тексте сцены самую конкретную деталь — число, размер, сравнение, предмет,
   место, действие, явление — и сделай её центром кадра. «Рыба длиной 11 метров» — это не просто рыба, а рыба рядом
   с водолазом или лодкой, чтобы размер был виден. «Давление 1000 атмосфер» — смятый металлический предмет,
   «температура 2 градуса» — ледяная вода и иней. Цифры, названия, сравнения из текста не терять: показывай их наглядно.
2. ГЕРОЙ НЕ В КАЖДОМ КАДРЕ. Персонаж из библии попадает в кадр, только если сцена говорит именно о нём или он действует.
   Если сцена про место, явление, факт, историю, механизм, других людей — показывай их, без героя.
   Не ставь героя подряд больше двух сцен: чередуй кадры с ним и кадры без него (среда, детали, предметы, карты,
   приборы, руки, следы, результат действия).
3. ГРАММАТИКА КАМЕРЫ. У каждой сцены свой план (shot), угол (angle) и объектив (lens): 24 — общий/установочный,
   35 — общий повествовательный, 50 — средний, 85 — крупный, интимный. Два соседних кадра не могут иметь одинаковую
   пару план+объектив. Движение камеры (camera): static для диалогов и покоя, push-in для эмоционального открытия,
   pan для действия, track для движения героя; не двигай камеру в каждом кадре.
4. ПРАВИЛО 180 ГРАДУСОВ. Если в кадре два персонажа, задай им стороны (positions: кто слева, кто справа) и держи
   эти стороны во всех соседних сценах с той же парой, пока история не поменяла расстановку.
5. РЕКВИЗИТ. Если персонаж пользуется предметом из библии, перечисли предмет в props. Предмет, который герой держал
   в прошлой сцене, остаётся у него и в следующей, пока текст не сказал обратное.
6. Взгляд (gaze): куда смотрит главный персонаж кадра — на другого персонажа, на предмет или off-screen-left/right/up/down.
7. Если текст абстрактный — придумай уместную наглядную визуальную метафору, а не пустой пейзаж.
8. БУМАГИ И ЭКРАНЫ БЕЗ ТЕКСТА. Письма, записки, документы, книги, газеты, экраны, вывески, таблички показывай так,
   чтобы на них нельзя было прочитать ни слова: пустыми, размытыми, издалека, под углом, закрытыми рукой или в тени.
   Никогда не делай текст на бумаге или экране центром крупного плана — покажи лицо читающего, руки, конверт, печать.
Поле "prompt" (50-90 слов, английский) описывает СОДЕРЖАНИЕ кадра: действие, объекты, среда, детали. Тип плана, объектив
и угол в него НЕ писать — они в отдельных полях и будут добавлены автоматически. Называй персонажей их латинскими
именами из библии (поле latin), например "LighthouseKeeper walks along the shore". В кадре только те персонажи,
которые есть в тексте этой сцены: НИКАКИХ дополнительных людей, животных, прохожих, семей, спутников. Место из библии —
его description. Стиль из библии кратко, своими словами.
Ответ строго JSON: {"scenes": [
 {"index": <номер>, "characters": ["имя из библии", ...], "location": "название места из библии или пусто",
  "props": ["название предмета из библии", ...], "shot": "wide|medium|close|detail|top|pov", "angle": "eye|low|high",
  "lens": 24|35|50|85, "camera": "static|push-in|pull-out|pan-left|pan-right|track", "light": "2-5 слов на английском",
  "time": "dawn|morning|day|sunset|night|indoor", "mood": "одно слово на английском",
  "gaze": "цель взгляда на английском", "positions": {"LatinName": "left|right"},
  "prompt": "<содержание кадра>", "motion": "<одно предложение на английском, как сцена движется в видео, без резких движений>"}
]} — ровно по одному элементу на каждую сцену."""

NO_TEXT = ("Absolutely no text, letters, words, numbers, signs, logos, captions, subtitles or watermarks anywhere in the image. "
           "Any paper, letter, document, book, screen or sign in the frame is blank, blurred, turned away or out of focus, "
           "with no readable writing on it.")


def build_style(llm: LLM, text, job_dir, progress, style_key, hint=None):
    out = os.path.join(job_dir, "style.json")
    cached = read_json(out)
    if cached and cached.get("style_key") == style_key:
        progress("style", 1, 1, "библия стиля уже есть")
        return cached
    progress("style", 0, 1, "LLM пишет библию стиля")
    hint = hint or STYLES.get(style_key, STYLES[DEFAULT_STYLE])[1]
    style = llm.chat_json(STYLE_SYSTEM.format(style_hint=hint), "Текст озвучки:\n\n" + text[:12000], max_tokens=6000)
    style["style_key"] = style_key
    style["style_hint"] = hint
    for i, c in enumerate(style.get("characters") or []):
        c.setdefault("latin", f"Char{i + 1}")
        c["latin"] = re.sub(r"[^A-Za-z]", "", c["latin"]) or f"Char{i + 1}"
    write_json(out, style)
    progress("style", 1, 1, "готово")
    return style


SHOT_WORDS = {"wide": "Wide shot", "medium": "Medium shot", "close": "Close-up", "detail": "Extreme close-up on a detail",
              "top": "Top-down view", "pov": "First-person point of view"}
ANGLE_WORDS = {"eye": "eye level", "low": "low angle", "high": "high angle"}
LENSES = (24, 35, 50, 85)
CAMERA_WORDS = {"static": "static camera", "push-in": "slow push-in", "pull-out": "slow pull-out", "pan-left": "slow pan left",
                "pan-right": "slow pan right", "track": "slow tracking shot"}
PROMPT_SCHEMA = 2  # менять при изменении полей раскадровки: старые scenes.json перепишутся


def _one(v):
    """Поле раскадровки к строке: список → первый элемент, словарь/None → пусто. Модель не всегда держит формат."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if isinstance(v, dict) or v is None:
        return ""
    return str(v).strip()


def style_line_of(style):
    """Строка стиля для каждого промпта: подсказка выбранного стиля + «библия» (если библия уже начинается с подсказки — только она).
    Языковая модель стиль в промпты сцен почти не пишет, а без него модели картинок рисуют фото — стили работали через раз."""
    hint = (style.get("style_hint") or STYLES.get(style.get("style_key"), ("", "", ""))[1] or "").strip().rstrip(".")
    bible = (style.get("style") or "").strip().rstrip(".")
    if hint and bible and bible.lower().startswith(hint[:25].lower()):
        return bible
    return ". ".join(x for x in (hint, bible) if x)


UPLOAD_LIMIT = 4_800_000  # сервисы (fast-gen и др.) не принимают картинки больше 5 МБ; с запасом


def upload_copy(path, limit=UPLOAD_LIMIT):
    """Картинка для отправки в сервис. Не больше лимита — как есть; больше (HD-картинки бывают 5,2–5,7 МБ) —
    рядом сохраняется JPEG того же кадра, который влезает в лимит. Оригинал PNG не трогается."""
    try:
        if os.path.getsize(path) <= limit:
            return path
    except OSError:
        return path
    out = os.path.splitext(path)[0] + ".upload.jpg"
    try:
        if os.path.getsize(out) <= limit and os.path.getmtime(out) >= os.path.getmtime(path):
            return out
    except OSError:
        pass
    from PIL import Image
    with Image.open(path) as src:
        im = src.convert("RGB")
    for scale in (1.0, 0.85, 0.7, 0.5):
        img = im if scale == 1.0 else im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
        for q in (93, 87, 80):
            img.save(out + ".tmp", "JPEG", quality=q, optimize=True)
            if os.path.getsize(out + ".tmp") <= limit:
                os.replace(out + ".tmp", out)
                log(f"{os.path.basename(path)}: {os.path.getsize(path) / 1e6:.2f} МБ больше лимита — отправляю JPEG "
                    f"{os.path.getsize(out) / 1e6:.2f} МБ ({img.width}×{img.height}, качество {q})")
                return out
    os.replace(out + ".tmp", out)
    return out


VIDEO_PROMPT = ""  # свой промпт оживления из окна (для всех кадров ролика); пусто — собирается автоматически


def video_prompt(scene):
    """Промпт оживления: движение + содержание + стиль + запрет текста.
    Кадр профиля (exact): движение + промпт как есть + свой хвост из профиля, без стиля и запрета текста.
    Свой промпт из окна заменяет всё это; {кадр} (или {frame}) в нём — описание кадра."""
    if VIDEO_PROMPT:
        frame = scene.get("raw_prompt") or scene.get("prompt") or ""
        return VIDEO_PROMPT.replace("{кадр}", frame).replace("{frame}", frame).strip()
    if scene.get("exact"):
        return " ".join(x for x in (scene.get("motion", "slow gentle camera movement") + ".", scene.get("prompt") or scene.get("raw_prompt", ""),
                                    scene.get("video_suffix", "")) if x)
    return f"{scene.get('motion', 'slow gentle camera movement')}. {scene.get('raw_prompt', '')} {scene.get('style_line', '')} {NO_TEXT}"


def compose_prompt(prompt, names, chars, use_refs, scene=None, props=None, locs=None, anchors=None, style_line=""):
    """Финальный промпт кадра: стиль → план/угол/объектив → содержание → расстановка героев → предметы → место → свет/время/настроение
    → описания героев (и подсказка про референсы) → запрет текста."""
    scene, props, locs, anchors = scene or {}, props or {}, locs or {}, anchors or {}
    head = []
    if style_line:
        head.append(f"Style: {style_line}")
    if scene.get("shot") in SHOT_WORDS:
        head.append(SHOT_WORDS[scene["shot"]])
        if scene.get("angle") in ANGLE_WORDS and scene["shot"] not in ("top", "pov"):
            head.append(ANGLE_WORDS[scene["angle"]])
        if scene.get("lens") in LENSES:
            head.append(f"{scene['lens']}mm lens")
    p = (", ".join(head) + ". " if head else "") + prompt.rstrip()
    pos = scene.get("positions") or {}
    if len(pos) >= 2:
        p += " " + ", ".join(f"{n} on the {side}" for n, side in pos.items() if side in ("left", "right")) + " of the frame."
    gaze = _one(scene.get("gaze"))
    if gaze:
        gaze = (anchors.get("__latin__") or {}).get(gaze, gaze)
        if not re.search(r"[А-Яа-яЁё]", gaze):
            p += f" Gaze: {gaze}."
    pdesc = [props[n] for n in (scene.get("props") or []) if n in props]
    if pdesc:
        p += " Props (must look exactly like this): " + " ".join(pdesc)
    loc = _one(scene.get("location"))
    if loc in locs:
        p += " Setting: " + locs[loc]
    light, tod, mood = _one(scene.get("light")), _one(scene.get("time")), _one(scene.get("mood"))
    tail = [x for x in (light, None if tod in ("", "indoor") else tod, mood) if x]
    if tail:
        p += " Lighting and mood: " + ", ".join(str(x) for x in tail) + "."
    descs = [chars[n] + (f" Key features: {anchors[n]}." if anchors.get(n) else "") for n in names if n in chars]
    if descs:
        p += " Character reference (must match exactly): " + " ".join(descs)
        if use_refs:
            p += (" The attached reference image(s) show exactly how the character(s) look — keep the same face, colors, markings "
                  "and clothing. Use the reference ONLY for the character's appearance: the composition, pose, camera angle, "
                  "framing, background and lighting must follow this prompt, never copy the reference picture itself.")
        p += (" Everyone and everything named in this prompt is in the frame; do not add any extra people or animals "
              "that are not named here.")
    return p + " " + NO_TEXT


def direct_scenes(scenes, latin, owners=None):
    """Правки раскадровки кодом, потому что модель их соблюдает не всегда:
    - два соседних кадра не могут иметь одинаковые план+объектив;
    - ось 180°: та же пара героев в соседних сценах стоит по тем же сторонам;
    - предмет, который был у героя в прошлой сцене, переносится в следующую с тем же героем, если модель список не заполнила."""
    prev = None
    for s in scenes:
        for k in ("location", "shot", "angle", "camera", "light", "time", "mood", "gaze"):
            s[k] = _one(s.get(k))  # в кэше или ответе модели поле могло прийти списком
        chars_raw = s.get("characters")
        s["characters"] = [c for c in chars_raw if isinstance(c, str)] if isinstance(chars_raw, list) else []
        s["shot"] = s.get("shot") if s.get("shot") in SHOT_WORDS else "medium"
        s["angle"] = s.get("angle") if s.get("angle") in ANGLE_WORDS else "eye"
        try:
            s["lens"] = int(s.get("lens")) if int(s.get("lens")) in LENSES else 50
        except (TypeError, ValueError):
            s["lens"] = 50
        s["camera"] = s.get("camera") if s.get("camera") in CAMERA_WORDS else "static"
        pos = s.get("positions") if isinstance(s.get("positions"), dict) else {}
        s["positions"] = {k: v for k, v in pos.items() if v in ("left", "right")}
        s["props"] = [x for x in (s.get("props") if isinstance(s.get("props"), list) else []) if isinstance(x, str)]
        if prev is not None:
            if (s["shot"], s["lens"]) == (prev["shot"], prev["lens"]):
                order = ["wide", "medium", "close", "detail"]
                s["shot"] = order[(order.index(s["shot"]) + 1) % len(order)] if s["shot"] in order else "medium"
                s["lens"] = LENSES[(LENSES.index(s["lens"]) + 1) % len(LENSES)]
            same_pair = set(s.get("characters") or []) == set(prev.get("characters") or []) and len(s.get("characters") or []) >= 2
            if same_pair and prev["positions"]:
                s["positions"] = dict(prev["positions"])
            if not s["props"] and prev["props"]:
                carried = set(prev.get("characters") or []) & set(s.get("characters") or [])
                if carried:
                    s["props"] = [p for p in prev["props"] if not owners or not owners.get(p) or set(owners[p]) & carried]
        prev = s
    return scenes


def build_prompts(llm: LLM, style, scenes, job_dir, progress, use_refs, batch=25):
    """Промпты пишутся пачками и сохраняются после каждой: при сбое на середине готовое не пропадает,
    а при повторном запуске дописываются только недостающие. Переключение «скорость/качество» промпты не сбрасывает —
    описания героев и подсказка про референсы добавляются в compose_prompt без обращения к модели."""
    out = os.path.join(job_dir, "scenes.json")
    style_text = json.dumps({k: style.get(k, "") for k in ("summary", "style", "characters", "locations", "avoid")}, ensure_ascii=False)
    chars = {c["name"]: c["description"] for c in (style.get("characters") or []) if c.get("name") and c.get("description")}
    latin = {c["name"]: c.get("latin", "") for c in (style.get("characters") or []) if c.get("name")}
    anchors = {c["name"]: c["anchors"] for c in (style.get("characters") or []) if c.get("name") and c.get("anchors")}
    props = {p["name"]: p["description"] for p in (style.get("props") or []) if p.get("name") and p.get("description")}
    locs = {l["name"]: l["description"] for l in (style.get("locations") or []) if l.get("name") and l.get("description")}
    owners = {p["name"]: p.get("owners") or [] for p in (style.get("props") or []) if p.get("name")}
    # русские названия героев и предметов → латинские имена (для поля gaze)
    anchors["__latin__"] = {**{c["name"]: c.get("latin", "") for c in (style.get("characters") or []) if c.get("name") and c.get("latin")},
                            **{p["name"]: p.get("latin", "") for p in (style.get("props") or []) if p.get("name") and p.get("latin")}}
    sk = style.get("style_key")
    style_line = style_line_of(style)

    def detect(names, raw):
        """LLM мог забыть заполнить characters — добираем по латинским именам в промпте."""
        found = [n for n in (names if isinstance(names, list) else []) if isinstance(n, str) and n in chars]
        low = raw.lower()
        for n, l in latin.items():
            if l and len(l) >= 3 and l.lower() in low and n not in found:
                found.append(n)
        return found

    def finish(s):
        s["characters"] = detect(s.get("characters") or [], s.get("raw_prompt", ""))
        s["style_line"] = style_line
        s["prompt"] = compose_prompt(s["raw_prompt"], s["characters"], chars, use_refs, scene=s, props=props, locs=locs, anchors=anchors, style_line=style_line)
        s["style_key"] = sk
        s["use_refs"] = use_refs
        s["schema"] = PROMPT_SCHEMA

    todo = [s for s in scenes if s.get("placeholder") or not (s.get("raw_prompt") and s.get("style_key") == sk and s.get("schema") == PROMPT_SCHEMA)]
    if not todo:
        direct_scenes(scenes, latin, owners)
        for s in scenes:
            finish(s)
        write_json(out, scenes)
        progress("prompts", len(scenes), len(scenes), "промпты сцен уже есть")
        return scenes

    chunks = [todo[b:b + batch] for b in range(0, len(todo), batch)]
    done_lock, done_n, ok_chunks = threading.Lock(), [len(scenes) - len(todo)], [0]
    progress("prompts", done_n[0], len(scenes), "LLM пишет промпты сцен" + (" (дописываю недостающие)" if done_n[0] else ""))

    def do_chunk(chunk):
        check_cancel()
        listing = "\n".join(f"{s['index']}. [{s['duration']:.1f} c] {s['text']}" for s in chunk)
        for s in chunk:
            s.pop("prompt", None)
            s.pop("raw_prompt", None)
        for attempt in range(3):
            try:
                resp = llm.chat_json(SCENES_SYSTEM, f"Библия стиля:\n{style_text}\n\nСцены:\n{listing}", max_tokens=520 * len(chunk) + 500)
            except Cancelled:
                raise
            except Exception as e:  # пачка не удалась — не роняем весь ролик, ниже подставим простые промпты
                log(f"промпты сцен {chunk[0]['index'] + 1}-{chunk[-1]['index'] + 1}: попытка {attempt + 1}/3 не удалась: {str(e)[:150]}")
                continue
            got = {int(x["index"]): x for x in resp.get("scenes", []) if x.get("prompt")}
            for s in chunk:
                if s["index"] in got:
                    g = got[s["index"]]
                    s["raw_prompt"] = g["prompt"].strip()
                    s.pop("placeholder", None)
                    s["characters"] = g.get("characters") or []
                    for k in ("location", "props", "shot", "angle", "lens", "camera", "light", "time", "mood", "gaze", "positions"):
                        s[k] = g.get(k)
                    for k in ("location", "shot", "angle", "camera", "light", "time", "mood", "gaze"):
                        s[k] = _one(s.get(k))  # модель иногда отдаёт список вместо строки: «unhashable type: 'list'» ронял весь ролик
                    s["motion"] = (_one(g.get("motion")) or CAMERA_WORDS.get(s.get("camera"), "slow gentle camera movement")).strip()
                    finish(s)
            if all(s.get("prompt") for s in chunk):
                ok_chunks[0] += 1
                break
        for s in chunk:
            if not s.get("prompt"):
                s["characters"] = []
                s["raw_prompt"] = f"{style.get('style', '')}. Wide shot illustrating: {s['text']}."
                s["motion"] = "slow gentle camera movement"
                s["placeholder"] = True  # модель не ответила: при следующем запуске эта сцена переспрашивается, а не остаётся навсегда
                finish(s)
        with done_lock:
            done_n[0] += len(chunk)
            write_json(out, scenes)  # сохраняем после каждой пачки
            progress("prompts", done_n[0], len(scenes), "LLM пишет промпты сцен")

    with cf.ThreadPoolExecutor(max_workers=3) as ex:  # три батча одновременно — в 3 раза быстрее на длинных роликах
        list(ex.map(do_chunk, chunks))
    direct_scenes(scenes, latin, owners)  # разнообразие планов, ось 180°, перенос реквизита — после всех пачек, по соседям
    for s in scenes:
        if s.get("raw_prompt"):
            finish(s)
    if chunks and not ok_chunks[0]:
        for s in todo:  # ни одна пачка не получилась — это не «плохие промпты», а неработающая модель: не сохраняем заглушки
            for k in ("prompt", "raw_prompt", "style_key"):
                s.pop(k, None)
        write_json(out, scenes)
        raise RuntimeError("Языковая модель не ответила ни на одну пачку сцен — проверьте ключ и лимит текстов fast-gen")
    write_json(out, scenes)
    progress("prompts", len(scenes), len(scenes), "готово")
    return scenes


# ============ шаг 4: лимиты и генераторы ============

class RateGate:
    """fast-gen: не больше N одновременных и не больше лимита кредитов в час.
    Сервер считает лимит скользящим окном, а в /usage часто отдаёт null — нашему счётчику верить нельзя.
    Поэтому после ответа «лимит исчерпан» все потоки останавливаются, через PROBE_WAIT секунд идёт ОДИН пробный запрос,
    и только когда он прошёл, остальные продолжают. Раньше десять потоков долбили сервер каждую минуту,
    сжигали попытки и сцены помечались «НЕ УДАЛОСЬ» (потом копировались из соседних — одинаковые кадры)."""
    PROBE_WAIT = 90

    def __init__(self, api: FastGen, kind="image"):
        self.api, self.kind = api, kind
        self.lock = threading.Lock()
        self.blocked_until = 0.0
        self.probe = None          # поток, которому разрешён пробный запрос
        self.after_block = False   # блокировка кончилась, пробы ещё не было
        self.said_at = 0.0         # чтобы десять потоков не писали одно и то же в журнал каждые 10 секунд
        # свой подсчёт кредитов — скользящее окно в час, как у сервера. Раньше счётчик копился с начала задания
        # и на длинном ролике «упирался в лимит» после 2000 кредитов, хотя часы давно сменились
        self.spent = collections.deque()
        self.window_end = 0.0      # когда у сервера начнётся новый час (window_start + 3600), если сервер это сообщает
        self.busy = False          # последний отказ — «сервис занят», а не «кредиты кончились»
        self.refresh()
        self.sem = threading.Semaphore(self.threads)

    def refresh(self):
        u = self.api.usage()
        lim = u.get("account_limits", {})
        hourly = u.get("current_usage", {}).get("hourly_usage") or {}
        self.capabilities = u.get("capabilities") or {}
        if self.kind == "image":
            self.threads = max(1, min(10, int(lim.get("img_generation_threads_allowed") or 1)))
            self.hour_limit = int(lim.get("img_gen_per_hour_limit") or 0)
            used = hourly.get("image_generation")
        else:
            self.threads = max(1, min(10, int(lim.get("video_generation_threads_allowed") or 1)))
            self.hour_limit = int(lim.get("video_gen_per_hour_limit") or 0)
            used = hourly.get("video_generation")
        start = None
        if isinstance(used, dict):  # fast-gen отдаёт {"current_usage": N, "window_start": …} — час у сервера фиксированный
            start = used.get("window_start")
            used = used.get("current_usage") or 0
        used = used or 0
        now = time.time()
        try:
            start = float(start) if start else None
        except (TypeError, ValueError):
            start = None
        if start and start > now - 3600:
            self.window_end = start + 3600
        else:
            if start:  # час сервера уже кончился — это расход прошлого часа, в новом ещё ничего не потрачено
                used = 0
            start, self.window_end = None, 0.0
        if isinstance(used, (int, float)):
            # сервер сказал точно — верим ему; запись «стареет» тогда же, когда у сервера кончается час
            self.spent = collections.deque([(start or now, int(used))]) if used > 0 else collections.deque()
        self.server_used = int(used) if isinstance(used, (int, float)) else None
        self.checked_at = now

    @property
    def used(self):
        now = time.time()
        while self.spent and now - self.spent[0][0] > 3600:
            self.spent.popleft()
        return max(0, sum(c for _, c in self.spent))

    def _say(self, progress, msg):
        now = time.time()
        if now - self.said_at > 45:
            self.said_at = now
            log(msg)
            progress("images" if self.kind == "image" else "videos", 0, 0, msg)

    def acquire(self, credits, progress):
        me = threading.current_thread()
        while True:
            check_cancel()
            with self.lock:
                now = time.time()
                if now >= self.blocked_until and self.probe in (None, me):
                    if self.after_block:
                        self.after_block = False
                        self.probe = me  # первый после паузы идёт один, остальные ждут его результата
                        return
                    if self.hour_limit == 0 or self.used + credits <= self.hour_limit:
                        self.spent.append((now, credits))
                        return
                waiting_server = now < self.blocked_until or self.probe is not None
            if waiting_server:
                if self.busy:
                    self._say(progress, "fast-gen занят: сейчас не принимает новые генерации, кредиты есть — жду и продолжу автоматически")
                    set_wait("fastgen_busy", self.blocked_until if now < self.blocked_until else None)
                else:
                    left = int((self.blocked_until - now) // 60) if now < self.blocked_until else 0
                    self._say(progress, "fast-gen: часовой лимит кредитов исчерпан — жду новый час у сервера"
                                        + (f" (~{left} мин)" if left else "") + ", продолжу автоматически")
                    set_wait("fastgen", self.blocked_until if now < self.blocked_until else None)
                for _ in range(10):
                    check_cancel()
                    time.sleep(1)
            else:
                self._wait_minute(progress, credits)

    def request_done(self, ok, credits):
        """Запрос завершился не ответом «лимит». Проба прошла — окно открыто, пускаем остальных."""
        with self.lock:
            if ok:
                clear_wait("fastgen")
            if self.probe is threading.current_thread():
                self.probe = None
                if ok:
                    self.busy = False
                    self.spent = collections.deque([(time.time(), credits)])  # сервер снова принимает; точного остатка не знаем — следующий 429 поправит
            if not ok:
                self.spent.append((time.time(), -credits))  # кредиты не списались

    def release(self, credits):
        """Генерация не удалась — кредиты не списаны, возвращаем в счётчик."""
        self.request_done(False, credits)

    def _wait_minute(self, progress, credits=0):
        self._say(progress, f"fast-gen: по моему подсчёту лимит {self.hour_limit} кредитов/час почти выбран, жду минуту и сверяюсь с сервером (продолжу автоматически)")
        set_wait("fastgen", time.time() + 60)
        for _ in range(60):
            check_cancel()
            time.sleep(1)
        with self.lock:
            if time.time() - self.checked_at > 55:
                try:
                    self.refresh()  # сервер сам скажет, сколько осталось в текущем часе
                except Exception:
                    pass

    BUSY_WAIT = 20

    def server_limit_hit(self, progress):
        """Сервер отказал («лимит», «slots full», «too many»…). Сначала спрашиваем у него, сколько потрачено на самом деле:
        кредиты есть — это «занят», короткая пауза; кредитов нет — ждём ровно до нового часа у сервера.
        Потом идёт ОДИН пробный запрос, остальные ждут его результата."""
        with self.lock:
            now = time.time()
            if now - self.checked_at > 15:
                try:
                    self.refresh()
                except Exception:
                    pass
            room = self.hour_limit == 0 or (self.server_used is not None and self.server_used < self.hour_limit)
            if room:
                self.busy = True
                until = now + self.BUSY_WAIT
            else:
                self.busy = False
                until = self.window_end if self.window_end > now else now + self.PROBE_WAIT
                until = min(max(until, now + 30), now + 3600)
                if self.hour_limit and self.used < self.hour_limit:
                    self.spent.append((now, self.hour_limit - self.used))
            log(("fast-gen занят (кредиты есть: " + f"{self.server_used}/{self.hour_limit}" + f"), пауза {self.BUSY_WAIT} с") if room
                else f"fast-gen: лимит {self.hour_limit} кредитов/час выбран, жду новый час у сервера ({int(until - now)} с)")
            self.blocked_until = max(self.blocked_until, until) if not room else until
            self.probe = None
            self.after_block = True


class NonStopGate:
    def __init__(self, api: NonStop):
        self.api = api
        self.lock = threading.Lock()
        info = api.account_info()
        self.threads = max(1, min(24, int(info.get("concurrent_tasks") or 4)))
        self.plan = info.get("plan")
        self.sem = threading.Semaphore(self.threads)
        self.exhausted = False
        self.refresh()

    def refresh(self):
        try:
            u = self.api.usage()
            rem = u.get("image_remaining")
            self.exhausted = rem is not None and int(rem) <= 0
        except Exception:
            pass

    def available(self):
        with self.lock:
            return not self.exhausted


def is_rate_limited(err_text):
    t = (err_text or "").lower()
    return ("429" in t or ("rate" in t and "limit" in t) or "quota" in t or "too many" in t or "slots full" in t
            or "limit_exceeded" in t or "hour_limit" in t or "per_hour" in t or "лимит" in t or "credits" in t and "exceed" in t)


LAST_RESORT_CHAIN = [("gemini_nano_banana_2_image_generate", 1), ("nano_banana_2_image_generate", 1), ("openai_image_generate", 1)]


def gen_fastgen_image(api, gate, scene, out_path, progress, refs, chain=None):
    """refs: список путей к референсам героев (может быть пустым). chain — принудительная цепочка моделей."""
    prompt = scene["prompt"]
    chain = chain or (IMAGE_CHAINS_REFS if refs else IMAGE_CHAINS)[FASTGEN_QUALITY]
    if HD_IMAGES:  # увеличение 2x умеет только Nano Banana через Flow — дешёвый канал Gemini пропускаем
        chain = [(o, n) for o, n in chain if o != "gemini_nano_banana_2_image_generate"] or chain
    if FG_IMAGE_MODEL:  # выбранная модель — первой; если не отдаст картинку, дальше обычная цепочка
        chain = [(FG_IMAGE_MODEL, 3)] + [(o, n) for o, n in chain if o != FG_IMAGE_MODEL]
    inputs = [{"filename": os.path.basename(p), "input": file_to_data_uri(upload_copy(p))} for p in refs]
    started = time.time()
    for op, tries in chain:
        t = 0
        while t < tries:
            check_cancel()
            credits = image_credits(op)
            gate.acquire(credits, progress)
            err, st = None, None
            with gate.sem:
                payload = {"operation": op, "prompt": prompt, "aspect_ratio": "16:9"}
                if HD_IMAGES and op in HD_OPS:
                    payload["generation_config"] = {"upscale": {"type": "2x"}}
                if inputs:
                    payload["inputs"] = inputs
                try:
                    st = api.run(payload, poll=3, max_wait=600)
                except Exception as e:
                    err = str(e)
                if st is not None:
                    if st.get("status") == "succeeded" and st.get("results"):
                        try:
                            api.download_result(st["results"][0], out_path + ".part")
                            os.replace(out_path + ".part", out_path)
                            gate.request_done(True, credits)
                            return "fastgen:" + op
                        except Exception as e:
                            err = f"не скачалась картинка: {e}"
                    else:
                        err = f"{st.get('error_code')} {str(st.get('error'))}"
            log(f"сцена {scene['index']}: fast-gen {op} попытка {t + 1}/{tries} не удалась: {err[:200]}")
            if is_rate_limited(err):
                gate.server_limit_hit(progress)
                if time.time() - started < MAX_LIMIT_WAIT:
                    continue  # ждём окно лимита, попытка не сгорает
            else:
                gate.request_done(False, credits)
            low = err.lower()
            if inputs and ("reference" in low or "invalid_argument" in low or "invalid_request" in low):
                inputs = []  # сервер не принял портреты героев — та же модель, но без них
                log(f"сцена {scene['index']}: повторяю без референсов")
                continue
            t += 1
            time.sleep(5)
    return None


def gen_nonstop_image(api, gate, scene, out_path, progress, refs):
    if not gate.available():
        log(f"сцена {scene['index']}: veononstop — суточный лимит картинок исчерпан, пропускаю")
        return None
    prompt = scene["prompt"]
    ref_objs = [{"name": os.path.splitext(os.path.basename(p))[0], "image_base64": file_to_data_uri(upload_copy(p)).split(",", 1)[1],
                 "mime_type": "image/png"} for p in refs]
    for model_key, tries in NONSTOP_CHAINS[FASTGEN_QUALITY]:
        for t in range(tries):
            check_cancel()
            with gate.sem:
                try:
                    api.generate_image(prompt, out_path + ".part", model_key=model_key, aspect_ratio="16:9", max_wait=1500,
                                       reference_images=ref_objs or None, use_all_ref_images=bool(ref_objs))
                    os.replace(out_path + ".part", out_path)
                    return "nonstop:" + NONSTOP_MODELS.get(model_key, model_key)
                except Exception as e:
                    msg = str(e)
                    log(f"сцена {scene['index']}: veononstop {NONSTOP_MODELS.get(model_key, model_key)} попытка {t + 1}/{tries} не удалась: {msg[:200]}")
                    if "401" in msg:
                        return None
                    if is_rate_limited(msg):
                        time.sleep(30)
                    if "limit" in msg.lower() and "daily" in msg.lower():
                        gate.refresh()
                        return None
    return None


class YouGenGate:
    """yougen: N одновременных задач и дневная квота картинок/видео."""

    def __init__(self, api: YouGen):
        self.api = api
        self.lock = threading.Lock()
        self.threads = 4
        self.exhausted = False
        self.usage = {}
        self.refresh()
        self.sem = threading.Semaphore(self.threads)

    def refresh(self):
        try:
            u = self.api.usage()
            self.usage = u
            for k in ("concurrency", "max_concurrent", "concurrent", "image_concurrent", "threads"):
                v = u.get(k)
                if isinstance(v, dict):
                    v = v.get("image") or v.get("max")
                if isinstance(v, (int, float)) and v > 0:
                    self.threads = max(1, min(16, int(v)))
                    break
            img = u.get("image") if isinstance(u.get("image"), dict) else None
            rem = (img or {}).get("remaining", u.get("image_remaining"))
            self.exhausted = rem is not None and int(rem) <= 0
        except Exception as e:
            log(f"yougen: не удалось прочитать лимиты: {e}")

    def available(self):
        with self.lock:
            return not self.exhausted


def gen_yougen_image(api, gate, scene, out_path, progress, refs):
    if not gate.available():
        log(f"сцена {scene['index']}: yougen — дневная квота картинок исчерпана, пропускаю")
        return None
    prompt = scene["prompt"]
    inputs = [file_to_data_uri(upload_copy(p)) for p in refs] or None
    for model, tries in YOUGEN_CHAINS[FASTGEN_QUALITY]:
        for t in range(tries):
            check_cancel()
            with gate.sem:
                try:
                    api.generate_image(prompt, out_path + ".part", model=model, aspect_ratio="16:9", inputs=inputs, max_wait=900)
                    os.replace(out_path + ".part", out_path)
                    return "yougen:" + YOUGEN_MODELS.get(model, model)
                except Exception as e:
                    msg = str(e)
                    log(f"сцена {scene['index']}: yougen {YOUGEN_MODELS.get(model, model)} попытка {t + 1}/{tries} не удалась: {msg[:200]}")
                    if msg.startswith("401") or msg.startswith("403"):
                        return None
                    if msg.startswith("429"):
                        gate.refresh()
                        with gate.lock:
                            gate.exhausted = True
                        return None
                    time.sleep(5)
    return None


def gen_yougen_video(api, gate, scene, img_path, out_path, progress):
    prompt = video_prompt(scene)
    for t in range(2):
        check_cancel()
        with gate.sem:
            try:
                api.generate_video_from_image(prompt, img_path, out_path + ".part.mp4", model="veo-3.1-fast", aspect_ratio="16:9", max_wait=1800)
                os.replace(out_path + ".part.mp4", out_path)
                return "yougen:veo-3.1-fast"
            except Exception as e:
                msg = str(e)
                log(f"сцена {scene['index']}: yougen видео попытка {t + 1}/2: {msg[:200]}")
                if msg[:3] in ("401", "403", "429"):
                    return None
                time.sleep(5)
    return None


class RoyalGate:
    """RoyalTechno: не больше recommended_max_in_flight задач одновременно."""

    def __init__(self, api: RoyalTechno):
        self.api = api
        self.lock = threading.Lock()
        a = api.account()
        lim = a.get("limits") or {}
        self.threads = max(1, min(20, int(lim.get("recommended_max_in_flight") or 4)))
        self.balance = a.get("balance_usd", "")
        self.sem = threading.Semaphore(self.threads)
        self.exhausted = False

    def available(self):
        with self.lock:
            return not self.exhausted


def gen_royal_image(api, gate, scene, out_path, progress, refs):
    if not gate.available():
        log(f"сцена {scene['index']}: royaltechno — баланс или квота исчерпаны, пропускаю")
        return None
    prompt = scene["prompt"]
    for model, tries in ROYAL_CHAINS[FASTGEN_QUALITY]:
        for t in range(tries):
            check_cancel()
            with gate.sem:
                try:
                    api.generate_image(prompt, out_path + ".part", model=model, reference_paths=refs or None, max_wait=900,
                                       resolution="2k" if HD_IMAGES and "nano-banana" in model else None)
                    os.replace(out_path + ".part", out_path)
                    return "royal:" + ROYAL_MODELS.get(model, model)
                except RoyalError as e:
                    log(f"сцена {scene['index']}: royaltechno {ROYAL_MODELS.get(model, model)} попытка {t + 1}/{tries}: {e}")
                    if e.status in (401, 403):
                        return None
                    if e.status == 402 or e.code in ("insufficient_credits", "daily_limit_reached"):
                        with gate.lock:
                            gate.exhausted = True
                        return None
                    if e.status == 400:
                        break  # с этой моделью запрос не проходит — пробуем следующую
                    time.sleep(5)
                except Exception as e:
                    log(f"сцена {scene['index']}: royaltechno ошибка: {str(e)[:200]}")
                    time.sleep(5)
    return None


def gen_royal_video(api, gate, scene, img_path, out_path, progress):
    prompt = video_prompt(scene)
    for t in range(2):
        check_cancel()
        with gate.sem:
            try:
                api.generate_video_from_image(prompt, img_path, out_path + ".part.mp4", max_wait=2400)
                os.replace(out_path + ".part.mp4", out_path)
                return "royal:veo-3.1"
            except RoyalError as e:
                log(f"сцена {scene['index']}: royaltechno видео попытка {t + 1}/2: {e}")
                if e.status in (400, 401, 402, 403):
                    return None
                time.sleep(10)
            except Exception as e:
                log(f"сцена {scene['index']}: royaltechno видео ошибка: {str(e)[:200]}")
                time.sleep(10)
    return None


class GoogleGate:
    """Google Gemini API: лимиты зависят от тарифа; держим 4 картинки и 2 видео одновременно, на 429 ждём."""

    def __init__(self, api):
        self.api = api
        self.lock = threading.Lock()
        self.threads = 4
        self.sem = threading.Semaphore(self.threads)
        self.vsem = threading.Semaphore(2)
        self.exhausted = False
        api.models()  # проверка ключа

    def available(self):
        with self.lock:
            return not self.exhausted


def gen_google_image(api, gate, scene, out_path, progress, refs):
    if not gate.available():
        log(f"сцена {scene['index']}: Google — ключ отклонён или квота исчерпана, пропускаю")
        return None
    chain = (GOOGLE_CHAINS_REFS if refs else GOOGLE_CHAINS)[FASTGEN_QUALITY]
    for kind, tries in chain:
        for t in range(tries):
            check_cancel()
            with gate.sem:
                try:
                    model = api.generate_image(scene["prompt"], out_path + ".part", kind=kind, reference_paths=refs or None)
                    os.replace(out_path + ".part", out_path)
                    return "google:" + model
                except GoogleError as e:
                    log(f"сцена {scene['index']}: Google {kind} попытка {t + 1}/{tries}: {e}")
                    if e.status in (401, 403):
                        with gate.lock:
                            gate.exhausted = True
                        return None
                    if e.status in (400, 404, 422):
                        break  # эта модель не подходит — следующая
                    time.sleep(5)
                except Exception as e:
                    log(f"сцена {scene['index']}: Google ошибка: {str(e)[:200]}")
                    time.sleep(5)
    return None


def gen_google_video(api, gate, scene, img_path, out_path, progress):
    prompt = video_prompt(scene)
    for t in range(2):
        check_cancel()
        with gate.vsem:
            try:
                model = api.generate_video_from_image(prompt, img_path, out_path + ".part.mp4", max_wait=1800)
                os.replace(out_path + ".part.mp4", out_path)
                return "google:" + model
            except GoogleError as e:
                log(f"сцена {scene['index']}: Google видео попытка {t + 1}/2: {e}")
                if e.status in (400, 401, 403, 404, 422):
                    return None
                time.sleep(15)
            except Exception as e:
                log(f"сцена {scene['index']}: Google видео ошибка: {str(e)[:200]}")
                time.sleep(15)
    return None


class SliderGate:
    def __init__(self, api: SecretSlider):
        self.api = api
        self.lock = threading.Lock()
        self.exhausted = False
        self.threads = 4
        self.credits = None
        try:
            b = api.balance()
            self.credits = b.get("api_credits")
            self.exhausted = self.credits is not None and int(self.credits) <= 0
            self.threads = max(1, min(8, int(b.get("rate_limit_per_minute") or 8) // 2))
        except Exception as e:
            log(f"secretslider: не удалось прочитать баланс: {e}")
        self.sem = threading.Semaphore(self.threads)

    def available(self):
        with self.lock:
            return not self.exhausted


def gen_slider_image(api, gate, scene, out_path, progress, refs):
    """Поштучно слайдер не используем (одна активная задача на ключ) — только пакетом, см. slider_batch."""
    return None


def slider_batch(ctx, scenes, img_dir, progress):
    """Сцены слайдера уходят пакетами (до 100 промптов); если ключей несколько — по пакету на каждый ключ параллельно.
    Возвращает множество индексов сцен, которые удались."""
    units = [(a, g) for a, g in ctx.get("sliders", []) if g.available()]
    if not units or not scenes:
        return set()
    done, lock = set(), threading.Lock()

    def run(api, gate, part):
        for k in range(0, len(part), 100):
            chunk = part[k:k + 100]
            check_cancel()
            paths = [os.path.join(img_dir, f"{s['index']:04d}.png") for s in chunk]
            progress("images", 0, 0, f"secretslider: отправляю пакет из {len(chunk)} сцен")
            try:
                got = api.generate_batch([s["prompt"] for s in chunk], [p + ".part" for p in paths],
                                         progress=lambda m: progress("images", 0, 0, m), log=log)
            except SliderError as e:
                log(f"secretslider пакет: {e}")
                if e.status == 402:
                    with gate.lock:
                        gate.exhausted = True
                return
            except Exception as e:
                log(f"secretslider пакет: {str(e)[:200]}")
                return
            for s, p, g in zip(chunk, paths, got):
                if g:
                    os.replace(g, p)
                    with lock:
                        done.add(s["index"])

    parts = [scenes[n::len(units)] for n in range(len(units))]
    with cf.ThreadPoolExecutor(max_workers=len(units)) as ex:
        list(ex.map(lambda a: run(a[0][0], a[0][1], a[1]), zip(units, parts)))
    return done

def make_references(ctx, style, job_dir, progress):
    """Один портрет на каждого героя. Возвращает {имя: путь}."""
    chars = style.get("characters") or []
    if not chars:
        return {}
    ref_dir = os.path.join(job_dir, "refs")
    os.makedirs(ref_dir, exist_ok=True)
    refs = {}
    for i, c in enumerate(chars):
        check_cancel()
        path = os.path.join(ref_dir, f"{c.get('latin') or 'Char%d' % (i + 1)}.png")
        if os.path.exists(path):
            refs[c["name"]] = path
            continue
        progress("refs", i, len(chars), f"рисую референс: {c['name']}")
        prompt = (f"Character reference sheet, single subject, full body and face clearly visible, neutral background, "
                  f"soft even lighting. {style.get('style', '')}. {c['description']} Only this character, nothing else. {NO_TEXT}")
        scene = {"index": f"ref-{i}", "prompt": prompt}
        model = None
        for prov in ctx["providers"]:
            model = ctx["image_gen"][prov](scene, path, progress, [])
            if model:
                break
        if model:
            refs[c["name"]] = path
        else:
            log(f"референс для {c['name']} не получился, герой пойдёт без референса")
    progress("refs", len(chars), len(chars), "референсы готовы")
    return refs


# ---- картинки всех сцен ----

def provider_chain(ctx, index):
    """Сцена i идёт в сервис номер i (по кругу), при сбое — в следующие по списку,
    а затем в любой другой сервис, для которого сохранён ключ."""
    provs = ctx["providers"]
    k = index % len(provs)
    return provs[k:] + provs[:k] + [p for p in ctx.get("fallback", []) if p not in provs]


# ============ автоотбор картинок: нейросеть-зритель смотрит каждый кадр, брак перерисовывается ============
QA_SYSTEM = """Ты контролёр качества кадров для видеоролика. Тебе дано описание кадра (то, что просили нарисовать) и сама картинка.
Реши, годится ли картинка в ролик. БРАК (ok=false), если есть хотя бы одно:
- читаемый текст, буквы, цифры, надписи, логотипы, вывески с буквами, субтитры, водяные знаки;
- лишние люди или животные, которых нет в описании; два одинаковых человека; лицо-дубль;
- уродства анатомии: лишние или кривые пальцы, руки, ноги, искажённое лицо, слившиеся тела;
- главный предмет или действие из описания не узнаётся или нарисовано не то (например бесформенный свёрток вместо ребёнка);
- картинка не про то, что в описании; пустой кадр; коллаж из нескольких картинок; рамки, чёрные полосы;
- сильные артефакты генерации: мыло, разваливающиеся объекты, невозможная геометрия.
НЕ брак: несовпадение мелких деталей, не идеальный свет, стиль чуть отличается, необычный ракурс.
Ответ строго JSON: {"ok": true|false, "score": 1-10 (общее качество кадра), "issues": "по-английски, 5-20 слов: что именно не так и чего избегать при перерисовке; пусто, если ok"}"""
QA_SYSTEM_EXACT = """Ты контролёр качества кадров для видеоролика. Тебе дано описание кадра (то, что просили нарисовать) и сама картинка.
Текст на картинке допустим ТОЛЬКО если он есть в описании. Реши, годится ли картинка в ролик. БРАК (ok=false), если есть хотя бы одно:
- надпись, которую просили в описании, написана с ошибкой: не та буква, лишняя или пропущенная буква, лишние или пропущенные диакритические знаки (точки, чёрточки, хвостики над и под буквами), лишние символы, звёздочки, кавычки;
- на картинке есть текст, буквы или цифры, которых нет в описании (в том числе слова на другом языке), логотипы, водяные знаки;
- лишние люди или животные, которых нет в описании; уродства анатомии: лишние или кривые пальцы, искажённое лицо, слившиеся тела;
- главный предмет или действие из описания не узнаётся или нарисовано не то; картинка не про то, что в описании;
- нарушен стиль, который прямо требует описание (например, цвет там, где просили только мел); коллаж, рамки, чёрные полосы;
- сильные артефакты генерации: мыло, разваливающиеся объекты, невозможная геометрия.
НЕ брак: несовпадение мелких деталей, не идеальный свет, необычный ракурс.
Ответ строго JSON: {"ok": true|false, "score": 1-10 (общее качество кадра), "issues": "по-английски, 5-20 слов: что именно не так и чего избегать при перерисовке; пусто, если ok"}"""
QA_MAX_REDO = 2  # сколько раз перерисовывать забракованный кадр, потом берём лучший из вариантов


def qa_check(client, scene, path):
    """→ (ok, score, issues). При любой ошибке проверки кадр считается годным (проверка не должна валить ролик)."""
    try:
        desc = scene.get("raw_prompt") or scene.get("prompt") or ""
        chars = ", ".join(scene.get("characters") or [])
        user = f"Описание кадра: {desc}" + (f"\nПерсонажи по описанию: {chars}" if chars else "") + "\nОцени картинку."
        j = client.see_json(QA_SYSTEM_EXACT if scene.get("exact") else QA_SYSTEM, user, path)
        ok = bool(j.get("ok", True))
        try:
            score = float(j.get("score") or (8 if ok else 3))
        except (TypeError, ValueError):
            score = 8 if ok else 3
        return ok, score, str(j.get("issues") or "")[:200]
    except Exception as e:
        log(f"автоотбор: проверка не удалась ({str(e)[:120]}), кадр оставлен")
        return True, 10, ""


QA_STATS = {"checked": 0, "bad": 0, "redone": 0, "kept_bad": 0}


# «Перерисовать» из окна: номера сцен, отмеченных пользователем во время создания ролика.
REDO = set()
REDO_LOCK = threading.Lock()


def request_redo(index):
    with REDO_LOCK:
        REDO.add(int(index))


def take_redo():
    """Забирает накопленные запросы (и очищает их)."""
    with REDO_LOCK:
        batch = set(REDO)
        REDO.clear()
    return batch


EDITS_FILE = "edits.json"  # свои промпты кадров из окна просмотра: {"номер": "промпт"}


def apply_edits(scenes, job_dir):
    """Свои правки промптов поверх сцен (правка из окна кадра переживает повторную сборку и «Продолжить»)."""
    ed = read_json(os.path.join(job_dir, EDITS_FILE), {}) or {}
    n = 0
    for s in scenes:
        p = ed.get(str(s["index"]))
        if p and s.get("prompt") != p:
            s["prompt"] = p
            n += 1
    if n:
        log(f"свои промпты из правки кадров: {n}")
    return scenes


def save_edit(job_dir, index, prompt):
    path = os.path.join(job_dir, EDITS_FILE)
    ed = read_json(path, {}) or {}
    ed[str(int(index))] = prompt
    write_json(path, ed)
    sp = os.path.join(job_dir, "scenes.json")
    sc = read_json(sp) or []
    if 0 <= int(index) < len(sc):
        sc[int(index)]["prompt"] = prompt
        write_json(sp, sc)


def redo_files(job_dir, index):
    """Удаляет картинку сцены и всё, что из неё сделано (клип с движением, оживление), чтобы нарисовать заново."""
    for rel in [f"images/{index:04d}.png", f"videos/{index:04d}.mp4"] + [f"{d}/{index:04d}.mp4" for d in clip_dirs(job_dir)]:
        try:
            os.remove(os.path.join(job_dir, rel))
        except OSError:
            pass
    for name in ("images.json", "videos.json"):
        pth = os.path.join(job_dir, name)
        st = read_json(pth, {})
        if str(index) in st:
            st.pop(str(index), None)
            write_json(pth, st)


class Prerender:
    """Кадры с движением камеры рендерятся, пока ещё рисуются остальные картинки: этап «сборка» почти исчезает."""

    def __init__(self, scenes, img_dir, job_dir, mode, motion, video_first=0):
        self.pool = cf.ThreadPoolExecutor(max_workers=max(1, min(3, (os.cpu_count() or 2) // 2)))
        self.img_dir = img_dir
        self.clips_dir = os.path.join(job_dir, clips_name(motion))
        os.makedirs(self.clips_dir, exist_ok=True)
        self.extra = TRANSITION if motion else 0.0
        self.skip = set()  # сцены, которые будут оживляться видео — их клип делается позже
        self.skip = {s["index"] for s in animated_scenes(scenes, mode, video_first)}
        self.futs = []

    def submit(self, s):
        if s["index"] in self.skip:
            return
        img = os.path.join(self.img_dir, f"{s['index']:04d}.png")
        clip = os.path.join(self.clips_dir, f"{s['index']:04d}.mp4")
        self.futs.append(self.pool.submit(render_scene_clip, img, s["index"], s["duration"] + self.extra, clip))

    def wait(self):
        for f in self.futs:
            try:
                f.result()
            except Exception as e:  # клип дорисует сборка
                log(f"предварительный рендер: {str(e)[:120]}")
        self.pool.shutdown(wait=True)


def drop_failed_images(scenes, job_dir):
    """Сцены, которые в прошлый раз подменили копией соседнего кадра, рисуем заново: убираем их png и запись о неудаче."""
    img_dir = os.path.join(job_dir, "images")
    state_path = os.path.join(job_dir, "images.json")
    state = read_json(state_path, {})
    redo = 0
    for sc in scenes:
        st = state.get(str(sc["index"])) or {}
        pth = os.path.join(img_dir, f"{sc['index']:04d}.png")
        if st and not st.get("ok"):
            try:
                if os.path.exists(pth):
                    os.remove(pth)
                state.pop(str(sc["index"]), None)
                redo += 1
            except OSError:
                pass
    if redo:
        write_json(state_path, state)
        log(f"{redo} сцен были копиями соседних кадров — рисую их заново")


class VideoOverlap:
    """Оживление стартует вместе с картинками: сцена уходит в видео, как только её картинка готова,
    а не после всех картинок. Пока идут картинки, сообщения оживления пишутся только в журнал."""

    def __init__(self, ctx, scenes, job_dir, progress, mode, video_first):
        self.quiet = threading.Event()
        self.quiet.set()
        self.stop = threading.Event()
        img_dir = os.path.join(job_dir, "images")
        state_path = os.path.join(job_dir, "images.json")

        def ready(s):
            if self.stop.is_set():
                return None
            if os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png")):
                return True
            st = read_json(state_path, {}).get(str(s["index"])) or {}
            return None if st and not st.get("ok") else False

        self.pool = cf.ThreadPoolExecutor(max_workers=1)
        self.fut = self.pool.submit(generate_all_videos, ctx, scenes, img_dir, job_dir, progress, mode, video_first,
                                    image_ready=ready, quiet=self.quiet)

    def abort(self):
        self.stop.set()
        self.quiet.clear()

    def result(self):
        self.quiet.clear()
        try:
            return self.fut.result()
        finally:
            self.pool.shutdown(wait=True)


def warn_hours(ctx, todo, progress):
    """Длинный ролик на тарифе fast-gen: честно говорим, сколько часов уйдёт из-за лимита кредитов в час."""
    try:
        units = [u for u in ctx["providers"] if base_of(u) == "fastgen"]
        if not units or not todo:
            return
        per_scene = image_credits(IMAGE_CHAINS[FASTGEN_QUALITY][0][0])
        mine = sum(1 for s in todo if base_of(provider_chain(ctx, s["index"])[0]) == "fastgen")
        limit = sum(ctx["gates"][u].hour_limit for u in units)
        need = mine * per_scene
        if limit and need > limit:
            hours = need / limit
            msg = (f"внимание: {mine} сцен × {per_scene} кредита = {need} кредитов, а лимит fast-gen {limit} в час — "
                   f"картинки займут около {hours:.1f} ч, программа будет ждать лимит автоматически"
                   + (" (в режиме «скорость» было бы в 4 раза быстрее)" if per_scene > 1 else ""))
            log(msg)
            progress("images", 0, 0, msg)
    except Exception:
        pass


def generate_all_images(ctx, scenes, style, job_dir, progress, refs, on_image=None, only=None):
    """only — список сцен, которые надо нарисовать заново (кнопка «Перерисовать»): их файлы удаляются, остальные не трогаются."""
    img_dir = os.path.join(job_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    state_path = os.path.join(job_dir, "images.json")
    if only is not None:
        for s in only:
            redo_files(job_dir, s["index"])
    state = read_json(state_path, {})
    workers = sum(ctx["gates"][p].threads for p in ctx["providers"])
    # сцены, которые в прошлый раз не удались и были подменены копией соседнего кадра, при повторном запуске рисуем заново
    redo = 0
    for sc in scenes:
        st = state.get(str(sc["index"])) or {}
        pth = os.path.join(img_dir, f"{sc['index']:04d}.png")
        if st and not st.get("ok") and os.path.exists(pth):
            try:
                os.remove(pth)
                redo += 1
            except OSError:
                pass
    if redo:
        log(f"{redo} сцен были копиями соседних кадров — рисую их заново")
    todo = [s for s in scenes if not os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png"))]
    done_count = len(scenes) - len(todo)
    names = " + ".join(f"{unit_name(p)} ({ctx['gates'][p].threads})" for p in ctx["providers"])
    progress("images", done_count, len(scenes), f"перерисовываю {len(todo)} кадров" if only is not None else f"генерирую картинки: {names}")
    if only is None:
        warn_hours(ctx, todo, progress)
    lock = threading.Lock()
    # Secret Slider: свои сцены (по кругу) — одним пакетом, до начала остальных
    if "slider" in ctx["bases"] and only is None:
        mine = [s for s in todo if base_of(provider_chain(ctx, s["index"])[0]) == "slider"] if len(ctx["bases"]) > 1 else list(todo)
        ok = slider_batch(ctx, mine, img_dir, progress)
        for s in mine:
            if s["index"] in ok:
                state[str(s["index"])] = {"ok": True, "model": "slider:batch"}
                done_count += 1
        write_json(state_path, state)
        progress("images", done_count, len(scenes), f"secretslider: готово {len(ok)} из {len(mine)}")
        todo = [s for s in todo if s["index"] not in ok]

    def draw(s, path, scene_refs):
        """Одна попытка нарисовать сцену по цепочке сервисов. → название модели или None."""
        model = None
        try:
            for prov in provider_chain(ctx, s["index"]):
                if prov not in ctx["providers"]:
                    log(f"сцена {s['index']}: выбранные сервисы не справились, пробую {unit_name(prov)}")
                try:
                    model = ctx["image_gen"][prov](s, path, progress, scene_refs)
                except Cancelled:
                    raise
                except Exception as e:
                    log(f"сцена {s['index']}: {prov} упал: {str(e)[:200]}")
                    model = None
                if model:
                    break
            if not model and ctx.get("api") and ctx["gates"].get("fastgen"):
                # последний рубеж: любая другая модель fast-gen, лишь бы сцена сделалась
                tried = set(m for m, _ in (IMAGE_CHAINS_REFS if scene_refs else IMAGE_CHAINS)[FASTGEN_QUALITY])
                rest = [(m, t) for m, t in LAST_RESORT_CHAIN if m not in tried]
                if rest:
                    log(f"сцена {s['index']}: последний рубеж fast-gen: {', '.join(m for m, _ in rest)}")
                    try:
                        model = gen_fastgen_image(ctx["api"], ctx["gates"]["fastgen"], s, path, progress, scene_refs, chain=rest)
                    except Cancelled:
                        raise
                    except Exception as e:
                        log(f"сцена {s['index']}: последний рубеж упал: {str(e)[:200]}")
        except Cancelled:
            return None
        return model

    def work(s):
        nonlocal done_count
        if CANCEL.is_set():
            return
        path = os.path.join(img_dir, f"{s['index']:04d}.png")
        scene_refs = [refs[n] for n in s.get("characters", []) if n in refs]
        model = draw(s, path, scene_refs)
        if model and ctx.get("qa") and not CANCEL.is_set():
            # автоотбор: смотрим кадр; брак перерисовываем с подсказкой, что было не так; из вариантов берём лучший
            ok, score, issues = qa_check(ctx["qa"], s, path)
            with lock:
                QA_STATS["checked"] += 1
            cands = []
            attempt = 0
            while not ok and attempt < QA_MAX_REDO and not CANCEL.is_set():
                attempt += 1
                with lock:
                    if attempt == 1:
                        QA_STATS["bad"] += 1
                    QA_STATS["redone"] += 1
                cand = f"{path}.qa{attempt}.png"
                try:
                    os.replace(path, cand)
                except OSError:
                    break
                cands.append((score, cand))
                log(f"сцена {s['index'] + 1}: автоотбор забраковал ({issues}), рисую заново ({attempt}/{QA_MAX_REDO})")
                progress("images", done_count, len(scenes), f"сцена {s['index'] + 1}: брак, перерисовываю ({attempt}/{QA_MAX_REDO})")
                s2 = dict(s)
                s2["prompt"] = f"{s['prompt']} IMPORTANT, previous attempt was rejected because: {issues}. Avoid this."
                m2 = draw(s2, path, scene_refs)
                if not m2:
                    break
                model = m2
                ok, score, issues = qa_check(ctx["qa"], s, path)
                with lock:
                    QA_STATS["checked"] += 1
            if cands:
                if os.path.exists(path):
                    cands.append((score, path))
                best = max(cands, key=lambda c: c[0])
                if not ok:
                    with lock:
                        QA_STATS["kept_bad"] += 1
                    log(f"сцена {s['index'] + 1}: все варианты с замечаниями, беру лучший (оценка {best[0]:.0f}/10)")
                if best[1] != path:
                    try:
                        os.replace(best[1], path)
                    except OSError:
                        pass
                for _, c in cands:
                    if c != path and os.path.exists(c):
                        try:
                            os.remove(c)
                        except OSError:
                            pass
        with lock:
            state[str(s["index"])] = {"ok": bool(model), "model": model}
            write_json(state_path, state)
            done_count += 1
            progress("images", done_count, len(scenes), f"сцена {s['index'] + 1}: {'OK ' + model if model else 'НЕ УДАЛОСЬ'}")
        if model and on_image:
            try:
                on_image(s)
            except Exception:
                pass

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(work, todo))
    check_cancel()
    failed = [s for s in todo if not os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png"))]
    if failed:
        # второй заход: к этому времени лимиты обычно отпустили, а копия соседнего кадра — худший вариант
        log(f"повторяю {len(failed)} сцен, которые не удались с первого раза")
        done_count -= len(failed)
        progress("images", done_count, len(scenes), f"повторяю {len(failed)} неудавшихся сцен")
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            list(ex.map(work, failed))
        check_cancel()

    # кадры, которые пользователь отметил «Перерисовать», пока шли остальные
    while True:
        batch = take_redo()
        again = [s for s in scenes if s["index"] in batch]
        if not again:
            break
        log(f"перерисовываю {len(again)} кадров по кнопке в окне: {', '.join(str(s['index'] + 1) for s in again)}")
        for s in again:
            redo_files(job_dir, s["index"])
        done_count -= len(again)
        progress("images", done_count, len(scenes), f"перерисовываю {len(again)} кадров")
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            list(ex.map(work, again))
        check_cancel()

    if ctx.get("qa") and QA_STATS["checked"]:
        q = QA_STATS
        msg = f"автоотбор: проверено {q['checked']} кадров, забраковано {q['bad']}, перерисовано {q['redone']}, осталось с замечаниями {q['kept_bad']}"
        log(msg)
        progress("images", done_count, len(scenes), msg)

    missing = [s for s in scenes if not os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png"))]
    if len(missing) < len(scenes) and len(missing) > max(MAX_COPIES_MIN, int(len(scenes) * MAX_COPIES_SHARE)):
        # пара копий соседних кадров незаметна, а сотня одинаковых картинок — брак: честнее остановиться
        write_json(state_path, state)
        have = len(scenes) - len(missing)
        msg = (f"Нарисовано {have} из {len(scenes)} картинок — сервис перестал их отдавать (обычно это суточный или часовой лимит ключа). "
               f"Всё готовое сохранено. Запустите это же видео ещё раз позже или добавьте ключ другого сервиса — "
               f"программа продолжит с того же места и дорисует только недостающие {len(missing)}.")
        log(msg)
        raise Retryable(msg)

    last_good = None
    for s in scenes:
        p = os.path.join(img_dir, f"{s['index']:04d}.png")
        if os.path.exists(p):
            last_good = p
        elif last_good:
            shutil.copyfile(last_good, p)
            state[str(s["index"])] = {"ok": False, "model": "copy_prev"}
    for s in reversed(scenes):
        p = os.path.join(img_dir, f"{s['index']:04d}.png")
        if os.path.exists(p):
            last_good = p
        elif last_good:
            shutil.copyfile(last_good, p)
    write_json(state_path, state)
    if any(not os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png")) for s in scenes):
        raise RuntimeError("Не удалось получить ни одной картинки — проверьте ключи и лимиты")
    return img_dir


# ============ шаг 4б: оживление картинок в видео ============

def gen_fastgen_video(api, gate, scene, img_path, out_path, progress):
    """Часовой лимит видео (Video Lite — 15 клипов) раньше сжигал попытки: после 15 клипов все остальные сцены за секунды
    помечались «не оживилась». Теперь, как и у картинок: сервер сказал «лимит» → ждём окно, попытка не сгорает."""
    prompt = video_prompt(scene)
    started = time.time()
    for op, tries in video_chain_for(scene):
        t = 0
        while t < tries:
            check_cancel()
            credits = VIDEO_CREDITS.get(op, 1)
            gate.acquire(credits, progress)
            err, st = None, None
            with gate.sem:
                payload = {"operation": op, "prompt": prompt, "aspect_ratio": "16:9", "inputs": [file_to_data_uri(img_path)]}
                if "keyframes" in op:
                    payload["keyframes"] = True
                if "omni_flash" in op:
                    payload["duration_seconds"] = int(op.rsplit("_", 1)[1].rstrip("s"))
                try:
                    st = api.run(payload, poll=5, max_wait=1500)
                except Exception as e:
                    err = str(e)
                    if "403" in err or "video.generate" in err:
                        gate.request_done(False, credits)
                        log(f"сцена {scene['index']}: fast-gen видео недоступно на этом ключе: {err[:160]}")
                        return None
                if st is not None:
                    if st.get("status") == "succeeded" and st.get("results"):
                        try:
                            api.download_result(st["results"][0], out_path + ".part.mp4")
                            os.replace(out_path + ".part.mp4", out_path)
                            gate.request_done(True, credits)
                            return "fastgen:" + op
                        except Exception as e:
                            err = f"не скачалось видео: {e}"
                    else:
                        err = f"{st.get('error_code')} {str(st.get('error'))}"
            log(f"сцена {scene['index']}: fast-gen видео {op} попытка {t + 1}/{tries}: {err[:200]}")
            if is_rate_limited(err):
                gate.server_limit_hit(progress)
                if time.time() - started < MAX_LIMIT_WAIT:
                    continue  # ждём окно лимита, попытка не сгорает
            else:
                gate.request_done(False, credits)
            t += 1
            time.sleep(5)
    return None


def gen_nonstop_video(api, gate, scene, img_path, out_path, progress):
    prompt = video_prompt(scene)
    for t in range(2):
        check_cancel()
        with gate.sem:
            try:
                api.generate_video_from_image(prompt, img_path, out_path + ".part.mp4", aspect_ratio="16:9", max_wait=1800)
                os.replace(out_path + ".part.mp4", out_path)
                return "nonstop:veo"
            except Exception as e:
                msg = str(e)
                log(f"сцена {scene['index']}: veononstop видео попытка {t + 1}/2: {msg[:200]}")
                if "401" in msg:
                    return None
                if is_rate_limited(msg):
                    time.sleep(30)
    return None


def animated_scenes(scenes, mode, video_first=0):
    """Какие сцены оживляются: «только видео» — все; иначе первые video_first сцен, а в «50 на 50» ещё и каждая вторая."""
    if mode == "video":
        return list(scenes)
    return [s for s in scenes if s["index"] < int(video_first or 0) or (mode == "mixed" and s["index"] % 2 == 0)]


def generate_all_videos(ctx, scenes, img_dir, job_dir, progress, mode="video", video_first=0, image_ready=None, quiet=None):
    """Оживляем сцены из animated_scenes(). Возвращает {index: mp4}.
    image_ready(s) → True (картинка готова) / False (ещё нет) / None (не будет); quiet — пока взведён, сообщения только в журнал."""
    def say(*a):
        if quiet is not None and quiet.is_set():
            if len(a) > 3 and a[3]:
                log(a[3])
            return
        return progress(*a)

    total_scenes = len(scenes)
    scenes = animated_scenes(scenes, mode, video_first)
    if not scenes:
        return {}
    why = {"video": "режим «Только видео» — все сцены",
           "mixed": "режим «50 на 50» — каждая вторая" + (f" плюс первые {int(video_first)}" if video_first else "")}.get(
        mode, f"первые {int(video_first)} сцен")
    msg = f"оживляю {len(scenes)} из {total_scenes} сцен: {why}"
    log(msg)
    say("videos", 0, len(scenes), msg)
    vid_dir = os.path.join(job_dir, "videos")
    os.makedirs(vid_dir, exist_ok=True)
    state_path = os.path.join(job_dir, "videos.json")
    state = read_json(state_path, {})
    video_gen = {}  # g=g в lambda обязателен: иначе все замыкания берут последний g (GoogleGate без acquire)
    if "fastgen" in ctx["bases"]:
        try:
            g = RateGate(ctx["api"], kind="video")
            if g.capabilities.get("video.generate", True):
                video_gen["fastgen"] = (g, lambda s, img, out, pr, g=g: gen_fastgen_video(ctx["api"], g, s, img, out, pr))
                need = sum(1 for s in scenes if not os.path.exists(os.path.join(vid_dir, f"{s['index']:04d}.mp4")))
                if g.hour_limit and need > g.hour_limit:
                    msg = (f"внимание: {need} клипов для оживления, а лимит fast-gen {g.hour_limit} видео в час — "
                           f"это около {need / g.hour_limit:.1f} ч, программа будет ждать лимит автоматически. Чтобы не ждать: "
                           f"«Стоп», затем режим «Только картинки» с ползунком «Оживить первые» — готовые картинки и клипы сохранятся")
                    log(msg)
                    say("videos", 0, 0, msg)
            else:
                log("fast-gen: на этом ключе видео недоступно (video.generate=false)")
        except Exception as e:
            log(f"fast-gen видео: не удалось прочитать лимиты: {e}")
    if "nonstop" in ctx["bases"]:
        g = ctx["gates"]["nonstop"]
        video_gen["nonstop"] = (g, lambda s, img, out, pr, g=g: gen_nonstop_video(ctx["nonstop"], g, s, img, out, pr))
    if "yougen" in ctx["bases"]:
        g = ctx["gates"]["yougen"]
        video_gen["yougen"] = (g, lambda s, img, out, pr, g=g: gen_yougen_video(ctx["yougen"], g, s, img, out, pr))
    if "royal" in ctx["bases"]:
        g = ctx["gates"]["royal"]
        video_gen["royal"] = (g, lambda s, img, out, pr, g=g: gen_royal_video(ctx["royal"], g, s, img, out, pr))
    if "google" in ctx["bases"]:
        g = ctx["gates"]["google"]
        video_gen["google"] = (g, lambda s, img, out, pr, g=g: gen_google_video(ctx["google"], g, s, img, out, pr))
    provs = [p for p in ctx["bases"] if p in video_gen]
    workers = sum(video_gen[p][0].threads for p in provs)
    if not provs:
        say("videos", 0, 0, "оживление недоступно на выбранных ключах — кадры останутся с плавным движением")
        return {}
    # не оживившиеся в прошлый раз сцены при повторном запуске пробуем снова
    todo = [s for s in scenes if not os.path.exists(os.path.join(vid_dir, f"{s['index']:04d}.mp4"))]
    done_count = len(scenes) - len(todo)
    say("videos", done_count, len(scenes), f"оживляю картинки ({workers} потоков, это самый долгий этап)")
    lock = threading.Lock()

    def work(s):
        nonlocal done_count
        if CANCEL.is_set():
            return
        img = os.path.join(img_dir, f"{s['index']:04d}.png")
        out = os.path.join(vid_dir, f"{s['index']:04d}.mp4")
        if image_ready is not None:
            while True:  # оживление идёт параллельно с картинками: ждём свою картинку
                r = image_ready(s)
                if r is True:
                    break
                if r is None or CANCEL.is_set():
                    return
                time.sleep(2)
        k = s["index"] % len(provs)
        model = None
        try:
            for prov in provs[k:] + provs[:k]:
                model = video_gen[prov][1](s, upload_copy(img), out, say)
                if model:
                    break
        except Cancelled:
            return
        with lock:
            state[str(s["index"])] = {"ok": bool(model), "model": model}
            write_json(state_path, state)
            done_count += 1
            say("videos", done_count, len(scenes), f"сцена {s['index'] + 1}: {'OK ' + model if model else 'не оживилась, останется картинка с движением'}")

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(work, todo))
    check_cancel()
    made = sum(1 for s in scenes if os.path.exists(os.path.join(vid_dir, f"{s['index']:04d}.mp4")))
    if made < len(scenes):
        msg = (f"оживилось {made} из {len(scenes)} сцен — остальные сервис не отдал (обычно это лимит видео на ключе), "
               f"они останутся картинками с движением. Чтобы дооживить: запустите это же видео ещё раз позже")
        log(msg)
        say("videos", len(scenes), len(scenes), msg)
    return {s["index"]: os.path.join(vid_dir, f"{s['index']:04d}.mp4") for s in scenes
            if os.path.exists(os.path.join(vid_dir, f"{s['index']:04d}.mp4"))}


def redo_pass(ctx, scenes, style, job_dir, progress, refs, mode, motion, video_first, videos):
    """Кадры, отмеченные «Перерисовать» уже после этапа картинок (во время оживления): рисуем заново,
    обновляем клипы с движением и оживление, и только потом собираем ролик."""
    img_dir = os.path.join(job_dir, "images")
    anim = {s["index"] for s in animated_scenes(scenes, mode, video_first)}
    while True:
        batch = take_redo()
        again = [s for s in scenes if s["index"] in batch]
        if not again:
            return videos
        log(f"перерисовываю перед сборкой {len(again)} кадров: {', '.join(str(s['index'] + 1) for s in again)}")
        pre = Prerender(scenes, img_dir, job_dir, mode, motion, video_first)
        generate_all_images(ctx, scenes, style, job_dir, progress, refs, on_image=pre.submit, only=again)
        pre.wait()
        if any(s["index"] in anim for s in again):
            for s in again:
                videos.pop(s["index"], None)
            videos.update(generate_all_videos(ctx, scenes, img_dir, job_dir, progress, mode, video_first))
        check_cancel()


# ============ шаг 5: сборка видео ============

def kenburns_path(i, duration, mode=None):
    """Движение картинки сцены. mode (по умолчанию KB_MODE):
    classic — цикл как было: статика, зум, статика, влево, статика, вправо;
    static — всё стоит; soft — каждый кадр медленно наезжает на 6 %;
    full — каждый кадр двигается: наезд, проезды в разные стороны, отъезд, диагонали."""
    D = max(duration, 0.1)
    mode = mode or KB_MODE

    def lin(t):
        x = min(1.0, max(0.0, t / D))
        return x * x * (3 - 2 * x) if mode == "full" else x  # в «максимуме» движение плавно разгоняется и тормозит

    if mode == "static":
        return None
    if mode == "soft":
        return lambda t: (1.0 + 0.06 * lin(t), 0.5, 0.5)
    if mode == "full":
        kinds = [
            lambda t: (1.0 + 0.15 * lin(t), 0.5, 0.5),
            lambda t: (1.12, 1.0 - lin(t), 0.5),
            lambda t: (1.15 - 0.15 * lin(t), 0.5, 0.5),
            lambda t: (1.12, lin(t), 0.5),
            lambda t: (1.12, 0.5, 1.0 - lin(t)),
            lambda t: (1.0 + 0.12 * lin(t), 0.2 + 0.6 * lin(t), 0.3),
            lambda t: (1.12, 0.5, lin(t)),
            lambda t: (1.12 - 0.08 * lin(t), 0.8 - 0.6 * lin(t), 0.7),
        ]
        return kinds[i % len(kinds)]
    kinds = [
        None,
        lambda t: (1.0 + 0.12 * lin(t), 0.5, 0.5),
        None,
        lambda t: (1.10, 1.0 - lin(t), 0.5),
        None,
        lambda t: (1.10, lin(t), 0.5),
    ]
    return kinds[i % len(kinds)]


EDGE_CROP = 0.035  # срезаем 3.5% по краям: там генераторы ставят маленький логотип-водяной знак


def _fit_16_9(im):
    sw, sh = im.size
    dx, dy = int(sw * EDGE_CROP), int(sh * EDGE_CROP)
    im = im.crop((dx, dy, sw - dx, sh - dy))
    sw, sh = im.size
    if sw / sh > W / H:
        nw = int(round(sh * W / H)); im = im.crop(((sw - nw) // 2, 0, (sw - nw) // 2 + nw, sh))
    else:
        nh = int(round(sw * H / W)); im = im.crop((0, (sh - nh) // 2, sw, (sh - nh) // 2 + nh))
    return im


def render_scene_clip(img_path, i, duration, out_path, kb=None):
    if os.path.exists(out_path):
        try:
            if os.path.getmtime(out_path) >= os.path.getmtime(img_path):
                return out_path
            os.remove(out_path)  # картинку перерисовали позже клипа — клип устарел
        except OSError:
            return out_path
    duration = max(0.3, float(duration))
    from PIL import Image

    path = kenburns_path(i, duration, kb)
    tmp = out_path + ".tmp.mp4"
    im = _fit_16_9(Image.open(img_path).convert("RGB"))
    if path is None:
        still = out_path + ".still.png"
        im.resize((W, H), Image.LANCZOS).save(still)
        run_ffmpeg(["-loop", "1", "-framerate", str(FPS), "-i", still, "-t", f"{duration:.3f}", "-r", str(FPS),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an", tmp])
        os.remove(still)
        os.replace(tmp, out_path)
        return out_path
    frames = max(2, int(round(duration * FPS)))
    BW, BH = W * 2, H * 2
    im = im.resize((BW, BH), Image.LANCZOS)
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-t", f"{duration:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an", tmp]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    try:
        for n in range(frames):
            if n % 25 == 0:
                check_cancel()
            z, cx, cy = path(n / FPS)
            ww, wh = BW / z, BH / z
            x0, y0 = (BW - ww) * cx, (BH - wh) * cy
            frame = im.transform((W, H), Image.AFFINE, (ww / W, 0, x0, 0, wh / H, y0), resample=Image.BILINEAR)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg не смог закодировать клип {i}: {err[:400]}")
    finally:
        if proc.poll() is None:
            proc.kill()
    os.replace(tmp, out_path)
    return out_path


VIDEO_ZOOM = 1.04      # увеличение оживлённых клипов: водяной знак Veo (внизу справа) уходит за край кадра
VIDEO_ANCHOR = "top"   # к какому краю прижать увеличенный клип: top — срезается только низ (там знак), верх цел


def render_video_clip(src_mp4, duration, out_path):
    """Подогнать сгенерированный клип под длительность сцены: 1920x1080, 25 к/с, без звука.
    Если клип короче сцены — последний кадр держится до конца. Клип увеличивается на VIDEO_ZOOM и прижимается
    к краю VIDEO_ANCHOR — как «104 % и вверх» в CapCut; в том же проходе ffmpeg, лишней нагрузки нет."""
    if os.path.exists(out_path):
        return out_path
    duration = max(0.3, float(duration))  # как и для картинок: отрицательная длительность → ffmpeg «duration out of range»
    tmp = out_path + ".tmp.mp4"
    z = max(1.0, min(1.3, float(VIDEO_ZOOM or 1)))
    y = {"top": "0", "bottom": "ih-oh"}.get(VIDEO_ANCHOR, "(ih-oh)/2")
    pre = f"crop=iw/{z:.4f}:ih/{z:.4f}:(iw-ow)/2:{y}," if z > 1.0 else ""
    vf = (f"{pre}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
          f"tpad=stop_mode=clone:stop_duration={duration + 1:.3f}")
    run_ffmpeg(["-i", src_mp4, "-vf", vf, "-t", f"{duration:.3f}", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", tmp])
    os.replace(tmp, out_path)
    return out_path


TRANSITIONS = ["fade", "slideleft", "circleopen", "wipeleft", "dissolve", "smoothleft", "fadeblack", "zoomin"]
TRANS_CLASSIC = list(TRANSITIONS)
# все переходы xfade из ffmpeg 4.4+ (имя для ffmpeg, название в окне)
XFADE_ALL = [("fade", "Затемнение"), ("dissolve", "Растворение"), ("fadeblack", "Через чёрный"), ("fadewhite", "Через белый"),
             ("fadegrays", "Через серый"), ("slideleft", "Сдвиг влево"), ("slideright", "Сдвиг вправо"), ("slideup", "Сдвиг вверх"),
             ("slidedown", "Сдвиг вниз"), ("smoothleft", "Плавно влево"), ("smoothright", "Плавно вправо"), ("smoothup", "Плавно вверх"),
             ("smoothdown", "Плавно вниз"), ("wipeleft", "Шторка влево"), ("wiperight", "Шторка вправо"), ("wipeup", "Шторка вверх"),
             ("wipedown", "Шторка вниз"), ("wipetl", "Шторка в угол ↖"), ("wipebr", "Шторка в угол ↘"), ("circleopen", "Круг раскрывается"),
             ("circleclose", "Круг закрывается"), ("circlecrop", "Круг через чёрный"), ("rectcrop", "Прямоугольник"),
             ("radial", "Радар"), ("vertopen", "Раскрытие по вертикали"), ("horzopen", "Раскрытие по горизонтали"),
             ("diagtl", "Диагональ ↖"), ("diagbr", "Диагональ ↘"), ("hlslice", "Полосы влево"), ("vdslice", "Полосы вниз"),
             ("pixelize", "Пикселизация"), ("hblur", "Размытие"), ("distance", "Перетекание"), ("squeezeh", "Сжатие"),
             ("zoomin", "Наезд")]
XFADE_NAMES = {k for k, _ in XFADE_ALL}
TRANS_SOFT = ["fade", "dissolve", "fadeblack"]
TRANS_PRESETS = {
    "classic": {"set": TRANS_CLASSIC, "order": "cycle", "duration": 0.5, "kb": "classic"},
    "soft": {"set": TRANS_SOFT, "order": "random", "duration": 0.6, "kb": "soft"},
    "full": {"set": [k for k, _ in XFADE_ALL], "order": "random", "duration": 0.5, "kb": "full"},
    "none": {"set": [], "order": "cycle", "duration": 0.5, "kb": "classic"},
}
TRANS_VIDEO = []  # переходы перед видео-сценами; пусто — те же, что перед картинками
TRANS_ORDER = "cycle"  # cycle — по кругу; random — вперемешку (у каждого стыка свой, без повтора подряд)
KB_MODE = "classic"


def trans_settings(cfg):
    """Настройки переходов из config.json → {preset, set, order, duration, kb}. Пусто — «как сейчас»."""
    t = cfg.get("transitions") if isinstance(cfg.get("transitions"), dict) else {}
    preset = t.get("preset") if t.get("preset") in TRANS_PRESETS or t.get("preset") == "custom" else "classic"
    base = dict(TRANS_PRESETS.get(preset) or TRANS_PRESETS["classic"])
    if preset == "custom":
        base["set"] = [x for x in (t.get("set") or []) if x in XFADE_NAMES]
        base["order"] = t.get("order") if t.get("order") in ("cycle", "random") else "cycle"
        try:
            base["duration"] = max(0.2, min(2.0, float(t.get("duration", 0.5))))
        except (TypeError, ValueError):
            base["duration"] = 0.5
        base["kb"] = t.get("kb") if t.get("kb") in ("classic", "static", "soft", "full") else "classic"
    base["preset"] = preset
    base["set"] = list(base["set"])
    # свои переходы перед видео-сценами (пусто — как у картинок); работают в любом наборе, кроме «без переходов»
    vs = [x for x in (t.get("video_set") or []) if x in XFADE_NAMES]
    base["video_set"] = vs if base["set"] else []
    return base


def apply_trans(cfg, motion):
    """Применить настройки переходов к сборке. → включены ли переходы (пустой набор = склейка встык)."""
    global TRANSITIONS, TRANSITION, TRANS_ORDER, KB_MODE, TRANS_VIDEO
    st = trans_settings(cfg)
    TRANSITIONS = st["set"] or list(TRANS_CLASSIC)
    TRANS_VIDEO = list(st["video_set"])
    TRANSITION = round(st["duration"], 2)
    TRANS_ORDER = st["order"]
    KB_MODE = st["kb"]
    return bool(motion and st["set"])


def trans_kind(k, kinds=None, order=None):
    """Переход для стыка номер k (0 — между первой и второй сценой)."""
    kinds = kinds or TRANSITIONS
    if (order or TRANS_ORDER) != "random" or len(kinds) < 2:
        return kinds[k % len(kinds)]
    n, b = len(kinds), k // len(kinds)
    if b >= 900:
        return kinds[(k * 7) % n]
    for x in range(0, b, 50):  # прогреть кэш по порядку — без глубокой рекурсии
        _bag(x, n)
    return kinds[_bag(b, n)[k % n]]


@functools.lru_cache(maxsize=4096)
def _bag(b, n):
    """Каждые n стыков — все переходы по разу в перемешанном порядке; на стыке «мешков» без повтора."""
    import random
    order = list(range(n))
    random.Random(b * 7919 + 13).shuffle(order)
    if b and order[0] == _bag(b - 1, n)[-1]:
        order.append(order.pop(0))
    return tuple(order)


def clips_name(motion):
    """Папка клипов: своя для каждой длины перехода и вида движения, чтобы не взять клипы прошлой сборки с другой длиной."""
    name = "clips" + ("_m" if motion else "")
    if motion and abs(TRANSITION - 0.5) > 1e-3:
        name += f"_{int(round(TRANSITION * 100))}"
    if KB_MODE != "classic":
        name += "_" + KB_MODE
    return name


def clip_dirs(job_dir):
    """Все папки клипов задания (clips, clips_m, clips_m_80_full…)."""
    try:
        return [n for n in os.listdir(job_dir) if n.startswith("clips") and os.path.isdir(os.path.join(job_dir, n))]
    except OSError:
        return []


def assemble_video(scenes, img_dir, videos, audio_path, job_dir, out_mp4, progress, motion=False):
    clips_dir = os.path.join(job_dir, clips_name(motion))
    os.makedirs(clips_dir, exist_ok=True)
    n = len(scenes)
    workers = max(1, min(4, (os.cpu_count() or 2) // 2))
    extra = TRANSITION if motion else 0.0  # в моушн-режиме клипы длиннее на время перехода

    grouped = motion and n > XFADE_GROUP

    def one(s):
        check_cancel()
        i = s["index"]
        dur = s["duration"] + extra  # каждый клип длиннее на переход: следующий начинается на T раньше и заканчивается вовремя
        tail = grouped and ((i + 1) % XFADE_GROUP == 0 or i == n - 1)  # последний клип группы: ещё +T, его съест переход второго уровня
        if tail:
            dur += TRANSITION
        suffix = "_x" if tail else ""
        if i in videos:
            clip = os.path.join(clips_dir, f"{i:04d}_v{suffix}.mp4")
            render_video_clip(videos[i], dur, clip)
        else:
            clip = os.path.join(clips_dir, f"{i:04d}{suffix}.mp4")
            render_scene_clip(os.path.join(img_dir, f"{i:04d}.png"), i, dur, clip)
        return clip

    done = 0
    clips = [None] * n
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, s): s["index"] for s in scenes}
        for f in cf.as_completed(futs):
            clips[futs[f]] = f.result()
            done += 1
            progress("render", done, n, "собираю кадры")
    if motion and n > 1:
        progress("render", n, n, "моушн-переходы и звук")
        return concat_with_transitions(clips, scenes, audio_path, out_mp4, job_dir, progress, vids=set(videos))
    progress("render", n, n, "склеиваю и добавляю звук")
    lst = os.path.join(job_dir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '" + c.replace("\\", "/").replace("'", "'\\''") + "'\n")
    tmp = out_mp4 + ".tmp.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-i", audio_path, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", tmp])
    os.replace(tmp, out_mp4)
    return out_mp4


MAX_COPIES_SHARE = 0.05  # больше 5 % сцен без картинки (и больше MAX_COPIES_MIN штук) — останавливаемся, а не клеим копии
MAX_COPIES_MIN = 3
XFADE_GROUP = 30  # клипов в одной команде ffmpeg. Больше нельзя: 413 входов + 412 переходов = командная строка длиннее
                  # 32 767 символов, и Windows отвечает WinError 206 «имя файла слишком длинное» (тестеры ловили на длинных роликах)


def cut_kinds(n, vids=(), image_set=None, video_set=None, order=None):
    """Переходы для всех стыков ролика из n сцен: kinds[i] — переход НА сцену i (kinds[0] не используется).
    Перед видео-сценой — из video_set (если задан), перед картинкой — из image_set; у каждого набора свой счёт,
    чтобы «вперемешку» и «по кругу» шли ровно внутри набора."""
    image_set = image_set or TRANSITIONS
    video_set = video_set if video_set is not None else TRANS_VIDEO
    vids = set(vids or ())
    out, ci, cv = [None], 0, 0
    for i in range(1, n):
        if i in vids and video_set:
            out.append(trans_kind(cv, video_set, order))
            cv += 1
        else:
            out.append(trans_kind(ci, image_set, order))
            ci += 1
    return out


def xfade_chain(clips, durations, kind0=0, kinds=None):
    """Входы и filter_complex для цепочки xfade. durations — длительности сцен без T; каждый клип длиннее на T.
    kinds — переход на каждый следующий клип (len(clips) - 1 шт.); нет — по счёту с kind0."""
    args = []
    for c in clips:
        args += ["-i", c]
    parts, prev, offset = [], "[0:v]", 0.0
    for k in range(1, len(clips)):
        offset += durations[k - 1]
        kind = kinds[k - 1] if kinds else trans_kind(kind0 + k - 1)
        out = f"[v{k}]" if k < len(clips) - 1 else "[vout]"
        parts.append(f"{prev}[{k}:v]xfade=transition={kind}:duration={TRANSITION}:offset={offset - TRANSITION:.3f}{out}")
        prev = out
    return args, ";".join(parts)


def trans_preview(t, images, work_dir, seconds=1.6):
    """Короткий ролик из 3 кадров с выбранными переходами и движением — для предпросмотра в окне. → путь к mp4."""
    st = trans_settings({"transitions": t or {}})
    os.makedirs(work_dir, exist_ok=True)
    T = st["duration"] if st["set"] else 0.0
    clips = []
    for k, img in enumerate(images[:3]):
        out = os.path.join(work_dir, f"p{k}.mp4")
        try:
            os.remove(out)
        except OSError:
            pass
        render_scene_clip(img, k + 1, seconds + T, out, kb=st["kb"])  # с 1: в «как сейчас» первый кадр двигается
        clips.append(out)
    res = os.path.join(work_dir, "preview.mp4")
    args = []
    for c in clips:
        args += ["-i", c]
    if st["set"] and len(clips) > 1:
        pk = cut_kinds(len(clips), {1}, st["set"], st["video_set"], st["order"])  # второй кадр — как видео-сцена
        parts, prev, offset = [], "[0:v]", 0.0
        for k in range(1, len(clips)):
            offset += seconds
            kind = pk[k]
            out = f"[v{k}]"
            parts.append(f"{prev}[{k}:v]xfade=transition={kind}:duration={T}:offset={offset - T:.3f}{out}")
            prev = out
        fc = ";".join(parts) + f";{prev}scale=960:540[vout]"
    else:
        fc = "".join(f"[{k}:v]" for k in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0,scale=960:540[vout]"
    run_ffmpeg(args + ["-filter_complex", fc, "-map", "[vout]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                       "-pix_fmt", "yuv420p", "-movflags", "+faststart", res])
    return res


def concat_with_transitions(clips, scenes, audio_path, out_mp4, job_dir=None, progress=None, vids=()):
    say = progress or (lambda *a: None)
    """Склейка через xfade: каждый переход длится TRANSITION с, сцены остаются синхронными с озвучкой.
    Длинные ролики — группами по XFADE_GROUP: сначала промежуточные ролики без звука, потом переходы между ними."""
    durs = [s["duration"] for s in scenes]
    K = cut_kinds(len(clips), vids)
    kinds_all = K[1:]
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    if len(clips) > XFADE_GROUP:
        gdir = os.path.join(job_dir or os.path.dirname(clips[0]), "groups")
        os.makedirs(gdir, exist_ok=True)
        gclips, gdurs = [], []
        groups = [list(range(b, min(b + XFADE_GROUP, len(clips)))) for b in range(0, len(clips), XFADE_GROUP)]
        for gi, idx in enumerate(groups):
            check_cancel()
            say("render", gi, len(groups), f"переходы: часть {gi + 1} из {len(groups)}")
            if len(idx) == 1:
                gclips.append(clips[idx[0]])
            else:
                gout = os.path.join(gdir, f"g{gi:03d}.mp4")
                args, fc = xfade_chain([clips[i] for i in idx], [durs[i] for i in idx], kinds=[K[i] for i in idx[1:]])
                run_ffmpeg(args + ["-filter_complex", fc, "-map", "[vout]", "-an", *enc, gout])
                gclips.append(gout)
            gdurs.append(sum(durs[i] for i in idx))
        clips, durs = gclips, gdurs
        kinds_all = [K[g[0]] for g in groups[1:]]  # стык групп — это переход на первую сцену следующей группы
    args, fc = xfade_chain(clips, durs, kinds=kinds_all)
    args += ["-i", audio_path]
    total_min = sum(durs) / 60
    say("render", 0, 0, f"финальная склейка всего ролика ({total_min:.0f} мин видео) — идёт без сообщений, "
                          f"на длинном ролике это десятки минут; ffmpeg работает, если нагружает процессор")
    tmp = out_mp4 + ".tmp.mp4"
    if fc:
        run_ffmpeg(args + ["-filter_complex", fc, "-map", "[vout]", "-map", f"{len(clips)}:a:0", *enc,
                           "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", tmp])
    else:
        run_ffmpeg(args + ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", tmp])
    os.replace(tmp, out_mp4)
    return out_mp4


# ============ озвучка сценария ============

def script_job_dir(script_text):
    h = hashlib.sha1(script_text.strip().encode("utf-8")).hexdigest()[:10]
    d = os.path.join(data_dir(), "jobs", "script_" + h)
    os.makedirs(d, exist_ok=True)
    return d, "script_" + h


def detect_language(text):
    """Грубое определение языка сценария по буквам: нужно для озвучки (ElevenLabs/Lumean хотят код языка,
    у Edge голос по умолчанию зависит от языка). Возвращает ISO-код."""
    t = (text or "")[:6000].lower()
    letters = [ch for ch in t if ch.isalpha()]
    if not letters:
        return "ru"
    cyr = sum(1 for ch in letters if "\u0400" <= ch <= "\u04ff")
    if cyr / len(letters) > 0.3:
        return "uk" if sum(t.count(ch) for ch in "іїєґ") > len(letters) * 0.004 else "ru"
    has = lambda s: any(ch in t for ch in s)
    if has("ąęłżźćńś"):
        return "pl"
    if has("ßäöü") and not has("ñ¿¡"):
        return "de"
    if has("ğşı"):
        return "tr"
    if has("ñ¿¡"):
        return "es"
    if has("ãõç") and not has("éèê"):
        return "pt"
    if has("àèéêçù") and t.count(" le ") + t.count(" les ") + t.count(" des ") > 2:
        return "fr"
    if t.count(" il ") + t.count(" che ") + t.count(" della ") > 3:
        return "it"
    return "en"


def _synth(client, text, voice_id, out, progress, language):
    """Передаём язык только тем сервисам, чей synthesize его принимает."""
    import inspect
    kw = {"progress": progress}
    if "language" in inspect.signature(client.synthesize).parameters:
        kw["language"] = language
    return client.synthesize(text, voice_id or None, out, **kw)


def make_voiceover(cfg, script_text, tts_service, voice_id, job_dir, progress):
    """Озвучить сценарий выбранным сервисом; результат кэшируется в job_dir/voiceover.mp3."""
    out = os.path.join(job_dir, "voiceover.mp3")
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        progress("tts", 1, 1, "озвучка уже есть, пропускаю")
        return out
    key = cfg.get(f"{tts_service}_api_key")
    if tts_service in ("fastgen", "royal") and not key:
        key = (provider_keys(cfg, tts_service) or [""])[0]
    from tts_api import KEYLESS
    if not key and tts_service not in KEYLESS:
        raise SystemExit(f"Для озвучки выбран {tts_service}, но ключ не введён.")
    with open(os.path.join(job_dir, "script.txt"), "w", encoding="utf-8") as f:
        f.write(script_text)
    lang = detect_language(script_text)
    progress("tts", 0, 1, f"озвучиваю сценарий ({tts_service}, язык {lang})")
    client = make_tts(tts_service, key)
    try:
        _synth(client, script_text, voice_id, out + ".part", lambda m: progress("tts", 0, 1, m), lang)
    except Cancelled:
        raise
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        # до сервиса вообще не достучались — резать текст на части бессмысленно, говорим по-человечески
        host = {"lumean": "api.lumean.app", "voicer": "voicer.mat3u.com", "voicegen": "qw1voicegencore.pro", "fastgen": "api.fast-gen.ai", "royal": "api.royaltechno.cc"}.get(tts_service, tts_service)
        raise SystemExit(f"Не удалось подключиться к сервису озвучки {tts_service} ({host}). Сервис либо временно недоступен, либо не открывается "
                         f"из вашей сети: проверьте, открывается ли https://{host} в браузере, попробуйте включить или выключить VPN "
                         f"и запустите снова. Либо выберите озвучку «Edge · бесплатно» — она работает без ключа.") from e
    except Exception as e:
        # длинный сценарий сервис мог не принять целиком — озвучиваем частями и склеиваем
        if len(script_text) < 2500:
            raise
        log(f"озвучка целиком не удалась ({str(e)[:120]}), пробую частями")
        parts = split_text(script_text, 3500)
        files = []
        for k, part in enumerate(parts):
            check_cancel()
            progress("tts", k, len(parts), f"озвучиваю сценарий ({tts_service}), часть {k + 1} из {len(parts)}")
            f = os.path.join(job_dir, f"voice_part{k:03d}.mp3")
            if not (os.path.exists(f) and os.path.getsize(f) > 1000):
                _synth(client, part, voice_id, f + ".part", lambda m: progress("tts", k, len(parts), m), lang)
                os.replace(f + ".part", f)
            files.append(f)
        lst = os.path.join(job_dir, "voice_concat.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for f in files:
                fh.write("file '" + f.replace("\\", "/").replace("'", "'\\''") + "'\n")
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", out + ".part"])
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
    os.replace(out + ".part", out)
    progress("tts", 1, 1, "озвучка готова")
    return out


def split_text(text, limit):
    """Режем текст на куски не длиннее limit символов по границам предложений (в крайнем случае — по пробелу)."""
    sents = re.split(r"(?<=[.!?…])\s+", text.strip())
    parts, cur = [], ""
    for snt in sents:
        while len(snt) > limit:  # одно гигантское «предложение»
            cut = snt.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                parts.append(cur); cur = ""
            parts.append(snt[:cut].strip()); snt = snt[cut:].strip()
        if len(cur) + len(snt) + 1 > limit and cur:
            parts.append(cur); cur = snt
        else:
            cur = (cur + " " + snt).strip()
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


# ============ главные функции ============

def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


SPEED_COPIES = (0.98, 1.02)


def make_speed_copies(src, progress, speeds=SPEED_COPIES):
    """Копии готового ролика со скоростью 0,98 и 1,02 рядом с ним (<имя>_0,98.mp4, <имя>_1,02.mp4).
    Битрейт берётся у самого ролика (+10%), поэтому копии весят примерно как оригинал, а не в 2–3 раза больше,
    как при фиксированном «качестве». Один запуск ffmpeg: файл читается один раз, пишется сразу в два."""
    out = subprocess.run([ffmpeg_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    info = json.loads(out.stdout or "{}")
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(info.get("format", {}).get("duration") or 0)
    br = int(video.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0)
    if not br and dur:
        br = int(os.path.getsize(src) * 8 / dur)
    br = max(800_000, int(br * 1.1))
    try:
        n_, d_ = (video.get("r_frame_rate") or "25/1").split("/")
        fps = float(n_) / float(d_)
    except (ValueError, ZeroDivisionError):
        fps = 25.0
    base, _ = os.path.splitext(src)
    n = len(speeds)
    fc = ["[0:v]split=%d%s" % (n, "".join("[v%d]" % i for i in range(n)))]
    if audio:
        fc.append("[0:a]asplit=%d%s" % (n, "".join("[a%d]" % i for i in range(n))))
    outs, dsts = [], []
    for i, sp in enumerate(speeds):
        fc.append("[v%d]setpts=PTS/%s,fps=%s[vo%d]" % (i, sp, fps, i))
        dst = f"{base}_{str(sp).replace('.', ',')}.mp4"
        dsts.append(dst)
        outs += ["-map", "[vo%d]" % i, "-c:v", "libx264", "-preset", "fast", "-b:v", str(br), "-maxrate", str(int(br * 1.5)),
                 "-bufsize", str(br * 2), "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        if audio:
            fc.append("[a%d]atempo=%s[ao%d]" % (i, sp, i))
            outs += ["-map", "[ao%d]" % i, "-c:a", "aac", "-b:a", "192k"]
        outs += ["-sn", "-dn", "-map_metadata", "-1", dst + ".tmp.mp4"]
    names = " / ".join(str(sp).replace(".", ",") for sp in speeds)
    progress("render", 0, 100, f"копии со скоростью {names}: битрейт {br // 1000} кбит/с, как у ролика")
    cmd = [ffmpeg_bin(), "-y", "-nostdin", "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", src,
           "-filter_complex", ";".join(fc)] + outs
    errlog = open(base + "_speed_err.txt", "w", encoding="utf-8")  # stderr в файл: через трубу ffmpeg забивает буфер и виснет
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errlog, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    out_dur = dur / min(speeds) if dur else 0
    last = -1
    for line in proc.stdout:
        if CANCEL.is_set():
            proc.kill()
            break
        m = re.match(r"out_time_us=(\d+)", line) or re.match(r"out_time_ms=(\d+)", line)
        if m and out_dur:
            pct = min(99, int(100 * int(m.group(1)) / 1e6 / out_dur))
            if pct != last:
                last = pct
                progress("render", pct, 100, f"копии со скоростью {names}: {pct}%")
    proc.wait()
    errlog.close()
    try:
        with open(errlog.name, encoding="utf-8", errors="replace") as f:
            err = f.read()
        os.remove(errlog.name)
    except OSError:
        err = ""
    check_cancel()
    if proc.returncode != 0:
        log("копии скорости: ffmpeg: " + err[-1500:])
        for d in dsts:
            try:
                os.remove(d + ".tmp.mp4")
            except OSError:
                pass
        raise RuntimeError("копии скорости не сделались: " + (err.strip().splitlines() or ["ffmpeg"])[-1][:200])
    for d in dsts:
        os.replace(d + ".tmp.mp4", d)
    progress("render", 100, 100, f"копии со скоростью {names} готовы: " + ", ".join(os.path.basename(d) for d in dsts))
    return dsts


def cleanup_job(job_dir):
    """После успешной сборки убираем то, что легко пересобрать: клипы, группы переходов, видео без субтитров.
    Картинки, оживлённые клипы, озвучка и json остаются — повторный запуск по-прежнему ничего не рисует заново.
    Раньше не удалялось ничего: сорокаминутный ролик оставлял около 5 ГБ."""
    freed = 0
    for name in clip_dirs(job_dir) + ["groups"]:
        p = os.path.join(job_dir, name)
        if os.path.isdir(p):
            freed += dir_size(p)
            shutil.rmtree(p, ignore_errors=True)
    for name in ("nosubs.mp4", "concat.txt", "voice_concat.txt"):
        p = os.path.join(job_dir, name)
        if os.path.exists(p):
            try:
                freed += os.path.getsize(p)
                os.remove(p)
            except OSError:
                pass
    if freed > 50 * 1024 * 1024:
        log(f"убрал промежуточные файлы задания: освобождено {freed / 1e9:.1f} ГБ")


JOB_KEEP_DAYS = 30


def prune_old_jobs():
    """При запуске программы: папки заданий, к которым не прикасались JOB_KEEP_DAYS дней, удаляются целиком. Готовые видео лежат
    в output и не затрагиваются."""
    root = os.path.join(data_dir(), "jobs")
    if not os.path.isdir(root):
        return 0
    limit, freed = time.time() - JOB_KEEP_DAYS * 86400, 0
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            newest = max([os.path.getmtime(p)] + [os.path.getmtime(os.path.join(p, f)) for f in os.listdir(p)]) if os.path.isdir(p) else os.path.getmtime(p)
        except OSError:
            continue
        if newest < limit:
            size = dir_size(p) if os.path.isdir(p) else os.path.getsize(p)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
            freed += size
    if freed:
        log(f"удалены задания старше {JOB_KEEP_DAYS} дней: освобождено {freed / 1e9:.1f} ГБ")
    return freed


QA_ENGINES = {  # движок автоотбора: (название, модель по умолчанию, адрес для OpenAI-совместимых)
    "gemini": ("Gemini", "gemini-3.6-flash", None),
    "anthropic": ("Claude", "claude-haiku-5-5", None),
    "openai": ("OpenAI", "gpt-4.1-mini", None),
    "openrouter": ("OpenRouter", "google/gemini-2.5-flash", "https://openrouter.ai/api/v1"),
}


class OpenAIVision:
    """Зрение через OpenAI или совместимый сервис (OpenRouter): единый интерфейс see_json/model/check."""

    def __init__(self, key, model, base=None):
        self.c = OpenAIClient(key)
        self.model, self.base = model, base

    def check(self):
        if self.base:  # у OpenRouter список моделей открытый, ключ проверится первым же запросом
            return "OK"
        return self.c.check()

    def see_json(self, system, user, image_path, max_tokens=600):
        return self.c.see_json(system, user, image_path, model=self.model, max_tokens=max_tokens, base=self.base)


def make_qa_client(cfg):
    """Нейросеть-зритель для автоотбора по ключу пользователя. None — автоотбор выключен или нет ключа."""
    if not cfg.get("qa_images"):
        return None
    eng = cfg.get("qa_engine") or "gemini"
    if eng not in QA_ENGINES:
        eng = "gemini"
    key = cfg.get(f"{eng}_api_key")
    if not key:
        return None
    _, default_model, base = QA_ENGINES[eng]
    model = cfg.get("qa_model") or default_model
    if eng == "gemini":
        return GeminiClient(key, model)
    if eng == "anthropic":
        return AnthropicClient(key, model)
    return OpenAIVision(key, model, base)


def make_context(cfg, providers):
    """providers — выбранные сервисы. Каждый ключ сервиса — отдельный «юнит» (fastgen, fastgen#2, …) со своими лимитами;
    сцены делятся между юнитами по кругу. Сервисы с ключом, но без галочки, — подстраховка."""
    keys = {p: provider_keys(cfg, p) for p in PROVIDERS}
    for p in providers:
        if not keys[p]:
            raise SystemExit(f"Отмечен {PROVIDER_NAMES[p]}, но его ключ не введён")
    api = FastGen(keys["fastgen"][0]) if keys["fastgen"] else None
    ctx = {"api": api, "providers": [], "bases": list(providers), "gates": {}, "image_gen": {},
           "nonstop": None, "yougen": None, "royal": None, "slider": None, "google": None, "sliders": [], "fallback": []}

    def add(p, n, key, selected):
        unit = p if n == 0 else f"{p}#{n + 1}"
        try:
            if p == "fastgen":
                c = api if n == 0 else FastGen(key)
                g = RateGate(c, "image")
                fn = lambda sc, out, pr, refs, c=c, g=g: gen_fastgen_image(c, g, sc, out, pr, refs)
            elif p == "nonstop":
                c = NonStop(key); g = NonStopGate(c); ctx["nonstop"] = ctx["nonstop"] or c
                fn = lambda sc, out, pr, refs, c=c, g=g: gen_nonstop_image(c, g, sc, out, pr, refs)
            elif p == "yougen":
                c = YouGen(key); g = YouGenGate(c); ctx["yougen"] = ctx["yougen"] or c
                fn = lambda sc, out, pr, refs, c=c, g=g: gen_yougen_image(c, g, sc, out, pr, refs)
            elif p == "royal":
                c = RoyalTechno(key); g = RoyalGate(c); ctx["royal"] = ctx["royal"] or c
                fn = lambda sc, out, pr, refs, c=c, g=g: gen_royal_image(c, g, sc, out, pr, refs)
            elif p == "google":
                c = GoogleAI(key); g = GoogleGate(c); ctx["google"] = ctx["google"] or c
                fn = lambda sc, out, pr, refs, c=c, g=g: gen_google_image(c, g, sc, out, pr, refs)
            else:
                c = SecretSlider(key); g = SliderGate(c); ctx["slider"] = ctx["slider"] or c; ctx["sliders"].append((c, g))
                fn = lambda sc, out, pr, refs: None
        except Exception as e:
            if selected:
                raise
            log(f"подстраховка {unit_name(unit)} недоступна: {str(e)[:120]}")
            return
        ctx["gates"][unit] = g
        ctx["image_gen"][unit] = fn
        (ctx["providers"] if selected else ctx["fallback"]).append(unit)

    for p in providers:
        for n, k in enumerate(keys[p]):
            add(p, n, k, True)
    for p in ("fastgen", "nonstop", "yougen", "royal", "google"):
        if p not in providers:
            for n, k in enumerate(keys[p]):
                add(p, n, k, False)
    return ctx


LAST_JOB_DIR = None
REWRITE_KEYS = {"openai": "openai_api_key", "anthropic": "anthropic_api_key", "gemini": "gemini_api_key", "fastgen": "fastgen_api_key"}


def rewrite_script(cfg, rw, progress):
    """Источник (ссылка YouTube или текст) → переписанный сценарий выбранной моделью."""
    import rewrite as rwmod
    say = lambda m: progress("rewrite", 0, 1, m)
    if (rw.get("source") or "text") == "youtube":
        url = (rw.get("url") or "").strip()
        if not url:
            raise SystemExit("Вставьте ссылку на ролик YouTube.")
        try:
            text = rwmod.youtube_transcript(url, say)
        except Exception as e:
            log(f"субтитры YouTube не получены ({str(e)[:120]}), пробую скачать звук")
            audio = rwmod.youtube_audio(url, os.path.join(data_dir(), "jobs"), os.path.dirname(ffmpeg_bin()), say)
            engine, api, _, _ = make_text_engine(cfg)
            jd, _ = job_dir_for(audio)
            text = transcribe(engine, api, audio, jd, progress, audio_duration(audio))["text"]
    else:
        text = (rw.get("text") or "").strip()
        if not text:
            raise SystemExit("Вставьте текст сценария для рерайта.")
    engine = rw.get("engine") or cfg.get("rewrite_engine") or "fastgen"
    key = cfg.get(REWRITE_KEYS.get(engine, ""), "")
    if not key:
        raise SystemExit(f"Для рерайта выбран {rwmod.ENGINE_NAMES.get(engine, engine)}, но его ключ не введён.")
    return rwmod.rewrite(text, engine, key, None, rw.get("words") or cfg.get("rewrite_words") or 1500,
                         rw.get("language") or cfg.get("rewrite_lang") or "польском", os.path.join(data_dir(), "jobs"), say)


# ============ профили: свои мастер-промпты (profiles.py) ============

def profile_search_dirs():
    return profiles.profile_dirs(data_dir(), app_dir())


def list_profiles():
    return profiles.list_profiles(profile_search_dirs())


def load_active_profile(cfg, name=None):
    """Профиль по имени; name=None — из настроек (config.json, поле profile). Пусто — без профиля."""
    name = (cfg.get("profile") or "") if name is None else name
    if not name:
        return None
    try:
        return profiles.load_profile(name, profile_search_dirs())
    except profiles.ProfileError as e:
        raise SystemExit(str(e))


def profile_job_dir(job_dir, prof):
    """У профиля своя папка задания рядом с обычной: свои сцены и картинки, не путаются с обычным режимом.
    Распознавание и сценарий берутся из обычной папки, чтобы не платить за них второй раз."""
    d = f"{job_dir}@{profiles.safe_name(prof['name'])}"
    os.makedirs(d, exist_ok=True)
    src, dst = os.path.join(job_dir, "transcript.json"), os.path.join(d, "transcript.json")
    if os.path.exists(src) and not os.path.exists(dst):
        try:
            shutil.copy(src, dst)
        except OSError:
            pass
    src, dst = os.path.join(job_dir, "script.txt"), os.path.join(d, "script.txt")
    try:  # сценарий всегда как в обычной папке: правили текст — профиль видит новый
        if os.path.exists(src):
            shutil.copy(src, dst)
        elif os.path.exists(dst):
            os.remove(dst)
    except OSError:
        pass
    return d


# ============ новый ролик из готового: тот же визуал, другая озвучка ============

REMIX_DIR = "новая озвучка"


def remix_job_dir(job_dir, src_dir):
    """Папка ремикса рядом с папкой новой озвучки. Совпала с исходной (тот же файл озвучки) — стоп."""
    d = profile_job_dir(job_dir, {"name": REMIX_DIR})
    if os.path.normcase(os.path.abspath(d)) == os.path.normcase(os.path.abspath(src_dir)):
        raise SystemExit("Новая озвучка должна быть другим файлом — этот ролик и так сделан из неё.")
    return d


def _link_or_copy(src, dst):
    if os.path.exists(dst):
        return
    try:
        os.link(src, dst)  # жёсткая ссылка: место на диске не тратится
    except OSError:
        shutil.copy2(src, dst)


def remix_prepare(src_dir, job_dir):
    """Картинки и оживлённые клипы исходного ролика → в папку ремикса."""
    n = 0
    for sub, ext in (("images", ".png"), ("videos", ".mp4")):
        sd, dd = os.path.join(src_dir, sub), os.path.join(job_dir, sub)
        if not os.path.isdir(sd):
            continue
        os.makedirs(dd, exist_ok=True)
        for f in os.listdir(sd):
            if f.endswith(ext) and f[:4].isdigit():
                _link_or_copy(os.path.join(sd, f), os.path.join(dd, f))
                n += 1
    for f in ("images.json", "videos.json"):
        if os.path.exists(os.path.join(src_dir, f)) and not os.path.exists(os.path.join(job_dir, f)):
            shutil.copy(os.path.join(src_dir, f), os.path.join(job_dir, f))
    return n


def remap_times(old_words, new_words, times, old_dur, new_dur):
    """Моменты старой озвучки → моменты новой: по совпавшим словам (та же речь, другой голос/темп).
    Не совпало и половины слов — растягиваем пропорционально длительности. → (новые моменты, способ)."""
    import bisect
    ow = [w for w in old_words or [] if str(w.get("text", "")).strip()]
    nw = [w for w in new_words or [] if str(w.get("text", "")).strip()]
    scale = (new_dur / old_dur) if old_dur else 1.0
    if len(ow) < 10 or len(nw) < 10:
        return [t * scale for t in times], "пропорционально"
    m = subs_style.match_keys([subs_style._key(str(w["text"])) for w in ow], [subs_style._key(str(w["text"])) for w in nw])
    pairs = [(i, j) for i, j in enumerate(m) if j is not None]
    if len(pairs) < 0.5 * len(ow):
        return [t * scale for t in times], "пропорционально"
    starts = [float(w["start"]) for w in ow]
    out = []
    pa = [i for i, _ in pairs]
    for t in times:
        i = min(len(ow) - 1, bisect.bisect_left(starts, t - 0.05))  # первое слово сцены
        k = bisect.bisect_left(pa, i)
        if k < len(pairs) and pairs[k][0] == i:
            j = pairs[k][1]
            nt = float(nw[j]["start"])
            if i > 0 and m[i - 1] is not None and m[i - 1] == j - 1:  # граница в паузе — на той же доле паузы, что в исходнике
                gap_old = starts[i] - float(ow[i - 1]["end"])
                pos = (t - float(ow[i - 1]["end"])) / gap_old if gap_old > 0 else 1.0
                pe = float(nw[j - 1]["end"])
                nt = pe + max(0.0, min(1.0, pos)) * max(0.0, nt - pe)
        elif k == 0:
            i1, j1 = pairs[0]
            nt = max(0.0, float(nw[j1]["start"]) - (starts[i1] - t) * scale)
        elif k >= len(pairs):
            i0, j0 = pairs[-1]
            nt = float(nw[j0]["end"]) + (t - float(ow[i0]["end"])) * scale
        else:
            (i0, j0), (i1, j1) = pairs[k - 1], pairs[k]
            a, b = float(ow[i0]["end"]), starts[i1]
            na, nb = float(nw[j0]["end"]), float(nw[j1]["start"])
            nt = na + (nb - na) * ((t - a) / (b - a) if b > a else 0.5)
        out.append(nt)
    return out, f"по словам ({len(pairs)} из {len(ow)} совпали)"


def remix_scenes(src_dir, tr, duration, job_dir, progress):
    """Сцены исходного ролика с новыми таймкодами под новую озвучку; промпты и картинки те же."""
    old = read_json(os.path.join(src_dir, "scenes.json")) or []
    if not old:
        raise SystemExit("У исходного ролика нет сцен (scenes.json) — сначала доделайте его.")
    old_tr = read_json(os.path.join(src_dir, "transcript.json")) or {}
    old_dur = float(old[-1].get("end") or 0) or duration
    starts = [float(s.get("start") or 0) for s in old]
    new_starts, how = remap_times(old_tr.get("words"), tr.get("words"), starts[1:], old_dur, duration)
    bounds = [0.0] + new_starts + [duration]
    for k in range(1, len(bounds) - 1):  # только вперёд, сцена не короче 0,4 с
        bounds[k] = max(bounds[k], bounds[k - 1] + 0.4)
    if bounds[-2] > duration - 0.4:  # хвост не влез — сжимаем последние границы
        for k in range(len(bounds) - 2, 0, -1):
            bounds[k] = min(bounds[k], bounds[k + 1] - 0.4)
    scenes = []
    for k, sc in enumerate(old):
        s2 = dict(sc)
        s2.update({"index": k, "start": round(bounds[k], 3), "end": round(bounds[k + 1], 3)})
        s2["duration"] = round(s2["end"] - s2["start"], 3)
        scenes.append(s2)
    write_json(os.path.join(job_dir, "scenes.json"), scenes)
    msg = f"сцены разложены по новой озвучке {how}: {len(scenes)} сцен, {old_dur:.0f} с → {duration:.0f} с"
    log(msg)
    progress("scenes", len(scenes), len(scenes), msg)
    return scenes


def run_job_dir(cfg, audio_path=None, script_text=None, voice_file=None, rewrite=None, profile=None, prompts_file=None, remix_from=None):
    """Папка задания заранее (для окна, чтобы показывать кадры). None — станет известна только после рерайта."""
    if rewrite and rewrite.get("on"):
        return None
    if script_text and script_text.strip():
        d = job_dir_for(os.path.abspath(voice_file))[0] if voice_file else script_job_dir(script_text)[0]
    elif audio_path:
        d = job_dir_for(os.path.abspath(audio_path))[0]
    else:
        return None
    if remix_from:
        return profile_job_dir(d, {"name": REMIX_DIR})
    if prompts_file:
        return profile_job_dir(d, {"name": READY_DIR})
    try:
        prof = load_active_profile(cfg, profile)
    except SystemExit:
        prof = None
    return profile_job_dir(d, prof) if prof else d


def make_profile_chat(prof, cfg, progress):
    """Чат для мастер-промпта: fast-gen (по умолчанию) или OpenAI-совместимый адрес (openai_base_url)."""
    def wait(sec, why):
        log(why)
        set_wait("llm", time.time() + sec)
        try:
            for _ in range(int(sec)):
                check_cancel()
                time.sleep(1)
        finally:
            clear_wait("llm")

    engine = (prof.get("llm") or "fastgen").lower()
    if engine == "manual" or cfg.get("profile_manual"):  # промпты пишет пользователь в своём чате и вставляет в окне
        return None, "вы сами (вставка ответа из чата)"
    if engine == "fastgen":
        key = cfg.get("fastgen_api_key") or (provider_keys(cfg, "fastgen") or [""])[0]
        if not key:
            raise SystemExit(f"Профиль «{prof['name']}» пишет промпты через fast-gen, но ключ fast-gen не введён.")
        model = prof.get("llm_model") or cfg.get("llm_model") or DEFAULT_LLM_MODEL
        return profiles.make_chat(prof, lambda: FastGen(key).s, BASE_URL + "/v1/chat/completions", model,
                                  wait=wait, check_cancel=check_cancel, log=log, service="fast-gen"), f"fast-gen ({model})"
    if engine == "openai":
        key = cfg.get("openai_api_key")
        if not key:
            raise SystemExit(f"Профиль «{prof['name']}» пишет промпты через OpenAI, но ключ OpenAI не введён.")
        model = prof.get("llm_model") or cfg.get("openai_model") or OPENAI_DEFAULT_MODEL

        def session():
            s = requests.Session()
            s.headers["Authorization"] = f"Bearer {key}"
            return s
        return profiles.make_chat(prof, session, endpoints.base("openai") + "/chat/completions", model,
                                  wait=wait, check_cancel=check_cancel, log=log, service="OpenAI"), f"OpenAI ({model})"
    raise SystemExit(f"Профиль «{prof['name']}»: неизвестная модель llm={engine!r} (можно fastgen или openai)")


def profile_script_text(job_dir, tr):
    """Текст для профиля: сценарий, если он есть (озвучка программой или «Готовая озвучка» к сценарию), иначе распознанный текст."""
    p = os.path.join(job_dir, "script.txt")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t, "сценарий"
        except OSError:
            pass
    return (tr.get("text") or " ".join(w["text"] for w in tr.get("words") or [])).strip(), "распознанный текст озвучки"


def apply_new_scenes(job_dir, scenes):
    """Записать сцены задания. Промпт кадра поменялся — его картинка и клипы удаляются (нарисуется заново);
    поменялась только длительность — пересобирается лишь клип. → сколько кадров перерисуется."""
    apply_edits(scenes, job_dir)  # свои промпты из правки кадра — не считаем их «изменившимися» и не стираем кадр
    scenes_path = os.path.join(job_dir, "scenes.json")
    old = {s.get("index"): s for s in (read_json(scenes_path) or []) if isinstance(s, dict)}
    redrawn = 0
    for s in scenes:
        o = old.get(s["index"])
        if not o:
            continue
        if o.get("prompt") != s["prompt"]:
            redo_files(job_dir, s["index"])
            redrawn += 1
        elif abs(float(o.get("duration") or 0) - s["duration"]) > 0.02:
            for sub in clip_dirs(job_dir):
                try:
                    os.remove(os.path.join(job_dir, sub, f"{s['index']:04d}.mp4"))
                except OSError:
                    pass
    for i in [i for i in old if isinstance(i, int) and i >= len(scenes)]:  # кадров стало меньше — лишние убрать
        redo_files(job_dir, i)
    if redrawn:
        log(f"у {redrawn} кадров изменились промпты — эти кадры будут нарисованы заново")
    write_json(scenes_path, scenes)
    return redrawn


# ============ готовые промпты из файла (.txt рядом с озвучкой) ============

READY_DIR = "готовые"


def find_prompts_file(audio_path):
    """Файл готовых промптов рядом с озвучкой: <имя>.prompts.txt или <имя>.txt, если в нём пронумерованные промпты."""
    if not audio_path:
        return ""
    base = os.path.splitext(audio_path)[0]
    for p in (base + ".prompts.txt", base + ".txt"):
        if os.path.isfile(p):
            try:
                blocks = read_prompts_file(p)
            except (OSError, UnicodeDecodeError):
                continue
            if sum(len(b["prompts"]) for b in blocks) >= profiles.READY_MIN_PROMPTS:
                return p
    return ""


def read_prompts_file(path):
    with open(path, encoding="utf-8-sig") as f:
        return profiles.parse_blocks(f.read())


def ready_scenes(prompts_file, tr, duration, job_dir, progress):
    """Сцены из готовых промптов: блок кончается фразой SYNC — она ищется в озвучке, кадры блока делят его время поровну."""
    blocks = read_prompts_file(prompts_file)
    if not blocks:
        raise SystemExit(f"В файле «{os.path.basename(prompts_file)}» не нашёл ни одного промпта. Нужны строки вида «001. …», "
                         f"после блока — строка SYNC и фраза.")
    summ = profiles.blocks_summary(blocks, duration)
    for w in summ["issues"]:
        log(f"готовые промпты: {w}")
    bounds, report = profiles.block_bounds(blocks, tr["words"], duration, log)
    scenes = profiles.build_scenes(profiles.ready_profile(), [""] * len(blocks), [b["prompts"] for b in blocks], bounds)
    profiles.BOARD.reset(profiles.READY_NAME, [""] * len(blocks), 0)
    for k, b in enumerate(blocks):
        profiles.BOARD.update(k + 1, first=b["numbers"][0], per=len(b["prompts"]), status="done", prompts=b["prompts"],
                              count=len(b["prompts"]), note="из файла", model=os.path.basename(prompts_file))
    apply_new_scenes(job_dir, scenes)
    found = sum(1 for r in report if r[1])
    log("готовые промпты: " + ", ".join(f"блок {k + 1}: {bounds[k + 1] - bounds[k]:.0f} с / {len(b['prompts'])} кадров"
                                         for k, b in enumerate(blocks))
        + (f"; фраз SYNC найдено {found} из {len(report)}" if report else ""))
    progress("scenes", len(scenes), len(scenes), f"сцен: {len(scenes)} (готовые промпты: блоков {len(blocks)}, "
                                                 f"~{summ.get('sec_per_frame', 0)} с на кадр)")
    return scenes


def profile_scenes(prof, cfg, tr, duration, job_dir, progress):
    """Сцены по мастер-промпту профиля: части сценария → промпты от модели → время частей в озвучке ÷ число кадров."""
    text, source = profile_script_text(job_dir, tr)
    parts = profiles.split_parts(text, prof)
    if not parts:
        raise SystemExit("Профиль: пустой текст — нечего делить на части.")
    chat, chat_name = make_profile_chat(prof, cfg, progress)
    progress("scenes", 0, 0, f"профиль «{prof['name']}»: {len(parts)} частей по ~{len(text) // len(parts)} символов "
                             f"({source}), промпты пишет {chat_name}")
    cache_dir = os.path.join(job_dir, "profile")
    profiles.BOARD.reset(prof["name"], parts, prof["prompts_per_part"], prof=prof, cache_dir=cache_dir, manual=chat is None)
    try:
        prompts = profiles.all_prompts(chat, prof, parts, cache_dir, progress, log, check_cancel, profiles.BOARD)
    except profiles.ProfileError as e:
        raise Retryable(str(e))
    bounds = profiles.part_bounds(parts, tr["words"], duration, log)
    scenes = profiles.build_scenes(prof, parts, prompts, bounds)
    apply_new_scenes(job_dir, scenes)
    per = ", ".join(f"{bounds[k + 1] - bounds[k]:.0f} с/{len(p)} кадров" for k, p in enumerate(prompts))
    log(f"профиль «{prof['name']}»: части {per}")
    progress("scenes", len(scenes), len(scenes), f"сцен: {len(scenes)} (профиль «{prof['name']}», частей {len(parts)})")
    return scenes


def resolve_source(cfg, audio_path, script_text, tts_service, tts_voice, progress, rewrite=None, voice_file=None):
    """Либо готовый mp3, либо сценарий (свой или после рерайта), который надо озвучить. Возвращает (audio_path, job_dir, base).
    voice_file — готовая озвучка к сценарию: озвучка не делается, сценарий сохраняется в задание (нужен профилям)."""
    global LAST_JOB_DIR
    if rewrite and rewrite.get("on"):
        script_text = rewrite_script(cfg, rewrite, progress)
        progress("rewrite", 1, 1, f"сценарий переписан: {len(script_text.split())} слов")
    if script_text and script_text.strip() and voice_file:
        audio_path = os.path.abspath(voice_file)
        job_dir, base = job_dir_for(audio_path)
        LAST_JOB_DIR = job_dir
        with open(os.path.join(job_dir, "script.txt"), "w", encoding="utf-8") as f:
            f.write(script_text.strip())
        progress("tts", 1, 1, "озвучка готовая — беру файл")
        return audio_path, job_dir, base
    if script_text and script_text.strip():
        job_dir, base = script_job_dir(script_text)
        LAST_JOB_DIR = job_dir
        audio_path = make_voiceover(cfg, script_text.strip(), tts_service or cfg.get("tts_service") or "voicegen",
                                    tts_voice if tts_voice is not None else cfg.get("tts_voice", ""), job_dir, progress)
        return audio_path, job_dir, base
    audio_path = os.path.abspath(audio_path)
    job_dir, base = job_dir_for(audio_path)
    LAST_JOB_DIR = job_dir
    return audio_path, job_dir, base


def prepare(audio_path=None, progress_cb=None, provider=None, mode="images", use_refs=False,
            script_text=None, tts_service=None, tts_voice=None, rewrite=None, profile=None, voice_file=None, prompts_file=None):
    """Первая половина: (озвучка) + транскрипция + сцены. Возвращает оценку для подтверждения."""
    progress = Progress(progress_cb)
    CANCEL.clear()
    cfg = load_config()
    providers = normalize_providers(provider or cfg.get("image_providers") or cfg.get("image_provider"))
    prof = None if prompts_file else load_active_profile(cfg, profile)
    audio_path, job_dir, base = resolve_source(cfg, audio_path, script_text, tts_service, tts_voice, progress, rewrite, voice_file)
    if prompts_file:
        job_dir = profile_job_dir(job_dir, {"name": READY_DIR})
    elif prof:
        job_dir = profile_job_dir(job_dir, prof)
    duration = audio_duration(audio_path)
    engine, text_api, _, _ = make_text_engine(cfg)
    tr = transcribe(engine, text_api, audio_path, job_dir, progress, duration)
    scenes_path = os.path.join(job_dir, "scenes.json")
    if prompts_file:  # кадров = промптов в файле
        scenes = [{"index": i} for i in range(sum(len(b["prompts"]) for b in read_prompts_file(prompts_file)))]
        use_refs = False
    elif prof:  # кадров = частей × промптов на часть; сами промпты пишутся только при запуске
        text, _ = profile_script_text(job_dir, tr)
        scenes = [{"index": i} for i in range(len(profiles.split_parts(text, prof)) * prof["prompts_per_part"])]
        use_refs = False
    else:
        scenes = read_json(scenes_path)
        if scenes and not scenes_ok(scenes):
            log("сцены из старой версии с битыми таймкодами — режу заново")
            scenes = None
        if not scenes:
            scenes = split_scenes(tr["words"], duration)
            write_json(scenes_path, scenes)
    n = len(scenes)
    img_dir = os.path.join(job_dir, "images")
    have_images = sum(1 for s in scenes if os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png")))
    todo = n - have_images
    share = {p: len([i for i in range(todo) if providers[i % len(providers)] == p]) for p in providers}
    est = {
        "scenes": n, "duration": round(duration), "have_images": have_images,
        "providers": providers, "text_engine": engine,
        "fastgen_credits_images": share.get("fastgen", 0) * (4 if use_refs else 1) + (8 if use_refs and "fastgen" in providers else 0),
        "nonstop_images": share.get("nonstop", 0),
        "yougen_images": share.get("yougen", 0),
        "royal_images": share.get("royal", 0),
        "slider_images": share.get("slider", 0),
        "google_images": share.get("google", 0),
        "video_clips": n if mode == "video" else ((n + 1) // 2 if mode == "mixed" else 0),
        "minutes": round(1 + n * {"images": 0.15, "mixed": 0.7, "video": 1.2}.get(mode, 0.15)),
        "audio_path": audio_path, "job_dir": job_dir,
        "text_preview": tr["text"][:300],
    }
    progress("prepare", 1, 1, f"сцен: {n}, длительность {duration:.0f} с")
    return est


def make_video(audio_path=None, progress_cb=None, out_dir=None, provider=None, mode="images", style_key=None, use_refs=False,
               subtitles=False, motion=False, script_text=None, tts_service=None, tts_voice=None, fastgen_quality=None, rewrite=None,
               video_first=0, hd=False, profile=None, voice_file=None, prompts_file=None, remix_from=None, look=None):
    """remix_from — папка готового ролика: его картинки и клипы под новую озвучку, без генерации;
    look — разовые настройки вида {"transitions": пресет}; profile — имя профиля (None — из настроек, "" — без профиля); voice_file — готовая озвучка к сценарию;
    prompts_file — готовые промпты из .txt (профиль и модель для промптов тогда не нужны)."""
    global FASTGEN_QUALITY, HD_IMAGES, LAST_JOB_DIR, VIDEO_PROMPT, VIDEO_ZOOM, VIDEO_ANCHOR
    HD_IMAGES = bool(hd)
    LAST_FAILURES.update({"images": 0, "videos": 0})
    progress = Progress(progress_cb)
    CANCEL.clear()
    clear_wait()
    take_redo()
    cfg = load_config()
    VIDEO_PROMPT = " ".join(str(cfg.get("video_prompt") or "").split())
    try:
        VIDEO_ZOOM = max(100.0, min(130.0, float(cfg.get("video_zoom_pct", 104)))) / 100
    except (TypeError, ValueError):
        VIDEO_ZOOM = 1.04
    VIDEO_ANCHOR = cfg.get("video_anchor") if cfg.get("video_anchor") in ("top", "center", "bottom") else "top"
    fg_img, fg_vid = apply_fg_models(cfg)
    providers = normalize_providers(provider or cfg.get("image_providers") or cfg.get("image_provider"))
    if mode not in MODES:
        raise SystemExit(f"Неизвестный режим: {mode}")
    style_key, style_name, style_hint = resolve_style(style_key or cfg.get("style") or DEFAULT_STYLE, cfg)
    FASTGEN_QUALITY = (fastgen_quality or cfg.get("quality") or cfg.get("fastgen_quality") or "speed")
    if FASTGEN_QUALITY not in IMAGE_CHAINS:
        FASTGEN_QUALITY = "speed"
    if remix_from:
        prompts_file, profile = None, ""
        if not os.path.isdir(remix_from):
            raise SystemExit("Папка исходного ролика не найдена.")
    prof = None if (prompts_file or remix_from) else load_active_profile(cfg, profile)
    look_prof = prof
    if look_prof is None and prompts_file and cfg.get("profile"):
        try:  # готовые промпты: профиль для текстов не нужен, но вид ролика (переходы) берём из выбранного
            look_prof = load_active_profile(cfg, cfg.get("profile"))
        except SystemExit:
            look_prof = None
    pt = (look or {}).get("transitions") or (look_prof or {}).get("transitions")
    if isinstance(pt, str):
        pt = {"preset": pt}
    if isinstance(pt, dict) and pt.get("preset") in list(TRANS_PRESETS) + ["custom"]:
        cfg = dict(cfg, transitions=pt)
        motion = pt.get("preset") != "none"
        log(f"переходы: {pt.get('preset')}" + (f" (профиль «{look_prof['name']}»)" if look_prof and not (look or {}).get("transitions") else ""))
    motion = apply_trans(cfg, motion)
    if motion or KB_MODE != "classic":
        log(f"переходы: {'вперемешку' if TRANS_ORDER == 'random' else 'по кругу'} из {len(TRANSITIONS)} по {TRANSITION} с, движение картинок: {KB_MODE}"
            if motion else f"переходы выключены, движение картинок: {KB_MODE}")
    if prompts_file and not os.path.isfile(prompts_file):
        raise SystemExit(f"Файл готовых промптов не найден: {prompts_file}")
    profiles.BOARD.reset(profiles.READY_NAME if prompts_file else (prof["name"] if prof else None))  # панель частей в окне
    if prof or prompts_file:
        use_refs = False  # героев с портретами нет: всё описано в самих промптах
    audio_path, job_dir, base = resolve_source(cfg, audio_path, script_text, tts_service, tts_voice, progress, rewrite, voice_file)
    if remix_from:
        job_dir = remix_job_dir(job_dir, remix_from)
        LAST_JOB_DIR = job_dir
        n = remix_prepare(remix_from, job_dir)
        log(f"новая озвучка для готового ролика: взято {n} картинок и клипов из «{os.path.basename(remix_from)}»")
    elif prompts_file:
        job_dir = profile_job_dir(job_dir, {"name": READY_DIR})
        LAST_JOB_DIR = job_dir
    elif prof:
        job_dir = profile_job_dir(job_dir, prof)
        LAST_JOB_DIR = job_dir
    out_dir = out_dir or os.path.join(data_dir(), "output")
    os.makedirs(out_dir, exist_ok=True)
    out_mp4 = os.path.join(out_dir, base + ".mp4")
    if os.path.exists(os.path.join(job_dir, "script.txt")):
        shutil.copy(os.path.join(job_dir, "script.txt"), os.path.join(out_dir, base + ".txt"))  # сценарий рядом с видео

    duration = audio_duration(audio_path)
    progress("start", 0, 0, f"аудио {duration:.0f} с · картинки: {' + '.join(PROVIDER_NAMES[p] for p in providers)} · режим: {mode} · "
                            + (f"готовые промпты: {os.path.basename(prompts_file)}" if prompts_file
                               else (f"профиль: {prof['name']}" if prof else f"стиль: {style_name}")))
    ctx = make_context(cfg, providers)
    ctx["qa"] = make_qa_client(cfg)
    for k in QA_STATS:
        QA_STATS[k] = 0
    if ctx["qa"]:
        progress("start", 0, 0, f"автоотбор картинок: {ctx['qa'].model}")
    engine, text_api, llm_name, llm_api = make_text_engine(cfg)
    llm = LLM(llm_name, llm_api, cfg.get("openai_model") if llm_name == "openai" else cfg.get("llm_model"))
    if llm_name == "fastgen":
        # запасной движок на случай часового лимита fast-gen: свой ключ Claude, иначе OpenAI
        if cfg.get("anthropic_api_key"):
            llm.fallback = LLM("anthropic", AnthropicClient(cfg["anthropic_api_key"], cfg.get("anthropic_model")))
        elif cfg.get("openai_api_key"):
            llm.fallback = LLM("openai", OpenAIClient(cfg["openai_api_key"]), cfg.get("openai_model"))
    llm.progress = progress
    progress("start", 0, 0, f"речь: {engine} · тексты: {llm_name} ({llm.model}) · fast-gen: {FASTGEN_QUALITY}"
                            + (f" · модель картинок: {fg_img}" if fg_img else "") + (f" · модель видео: {fg_vid}" if fg_vid else ""))

    tr = transcribe(engine, text_api, audio_path, job_dir, progress, duration)
    refs, ref_pool = {}, None
    if remix_from:
        scenes = remix_scenes(remix_from, tr, duration, job_dir, progress)
        style = {"style_key": "remix", "style": "", "characters": [], "props": [], "locations": []}
        use_refs = False
    elif prompts_file:
        scenes = ready_scenes(prompts_file, tr, duration, job_dir, progress)
        style = {"style_key": "ready", "style": "", "characters": [], "props": [], "locations": []}
    elif prof:
        scenes = profile_scenes(prof, cfg, tr, duration, job_dir, progress)
        style = {"style_key": "profile:" + prof["key"], "style": "", "characters": [], "props": [], "locations": []}
    else:
        scenes_path = os.path.join(job_dir, "scenes.json")
        scenes = read_json(scenes_path)
        if scenes and not scenes_ok(scenes):
            log("сцены из старой версии с битыми таймкодами — режу заново")
            scenes = None
        if not scenes:
            scenes = split_scenes(tr["words"], duration)
            write_json(scenes_path, scenes)
        progress("scenes", len(scenes), len(scenes), f"сцен: {len(scenes)}")
        style = build_style(llm, tr["text"], job_dir, progress, style_key, style_hint)
    if remix_from:  # всё нарисовано и оживлено — только сборка
        img_dir = os.path.join(job_dir, "images")
        missing = [s["index"] + 1 for s in scenes if not os.path.exists(os.path.join(img_dir, f"{s['index']:04d}.png"))]
        if missing:
            raise SystemExit(f"В исходном ролике нет {len(missing)} картинок (кадры {', '.join(map(str, missing[:8]))}"
                             f"{'…' if len(missing) > 8 else ''}) — сначала доделайте его кнопкой «Продолжить».")
        vid_dir = os.path.join(job_dir, "videos")
        videos = {s["index"]: os.path.join(vid_dir, f"{s['index']:04d}.mp4") for s in scenes
                  if os.path.exists(os.path.join(vid_dir, f"{s['index']:04d}.mp4"))}
        progress("images", len(scenes), len(scenes), f"картинки готовые: {len(scenes)}" + (f", клипов: {len(videos)}" if videos else ""))
    else:
        if use_refs:
            # портреты героев зависят только от библии — рисуем их, пока пишутся промпты сцен (сообщения пока в журнал)
            ref_quiet = threading.Event()
            ref_quiet.set()

            def ref_progress(*a):
                if ref_quiet.is_set():
                    if len(a) > 3 and a[3]:
                        log(a[3])
                    return
                return progress(*a)
            ref_pool = cf.ThreadPoolExecutor(max_workers=1)
            ref_fut = ref_pool.submit(make_references, ctx, style, job_dir, ref_progress)
        if not prof and not prompts_file:
            scenes = build_prompts(llm, style, scenes, job_dir, progress, use_refs)
        if ref_pool:
            ref_quiet.clear()
            if not ref_fut.done():
                progress("refs", 0, 0, "дорисовываю портреты героев")
            refs = ref_fut.result()
            ref_pool.shutdown(wait=True)
        apply_edits(scenes, job_dir)
        drop_failed_images(scenes, job_dir)
        pre = Prerender(scenes, os.path.join(job_dir, "images"), job_dir, mode, motion, video_first)
        overlap = VideoOverlap(ctx, scenes, job_dir, progress, mode, video_first) if (mode in ("video", "mixed") or video_first) else None
        try:
            img_dir = generate_all_images(ctx, scenes, style, job_dir, progress, refs, on_image=pre.submit)
        except BaseException:
            if overlap:
                overlap.abort()
            raise
        videos = overlap.result() if overlap else {}
        pre.wait()
        videos = redo_pass(ctx, scenes, style, job_dir, progress, refs, mode, motion, video_first, videos)
    if subtitles:
        raw_mp4 = os.path.join(job_dir, "nosubs.mp4")
        assemble_video(scenes, img_dir, videos, audio_path, job_dir, raw_mp4, progress, motion=motion)
        check_cancel()
        progress("subtitles", 0, 1, "накладываю субтитры")
        # .srt кладём в подпапку: файл с тем же именем рядом с mp4 плееры подхватывают сами и рисуют
        # свои субтитры поверх уже вшитых — у тестера текст «двоился»
        os.makedirs(os.path.join(out_dir, "subtitles"), exist_ok=True)
        srt_path = os.path.join(out_dir, "subtitles", base + ".srt")
        sub_words = subtitle_words(cfg, job_dir, tr, bool((script_text and script_text.strip()) or (rewrite and rewrite.get("on"))))
        subs.write_srt(sub_words, srt_path)
        style_cfg = sub_style_of(cfg)
        if style_cfg:
            subs_style.write_ass(sub_words, os.path.join(job_dir, "subs.ass"), style_cfg)
            fonts_sub = subs_style.prepare_fonts(job_dir, subs_style.normalize(style_cfg)["font"], font_dirs())
            subs_style.burn(ffmpeg_bin(), raw_mp4, "subs.ass", out_mp4 + ".tmp.mp4", job_dir, fonts_sub, NO_WINDOW)
        else:
            subs.burn(ffmpeg_bin(), raw_mp4, srt_path, out_mp4 + ".tmp.mp4", NO_WINDOW)
        os.replace(out_mp4 + ".tmp.mp4", out_mp4)
        progress("subtitles", 1, 1, "субтитры наложены, .srt сохранён в папке output/subtitles")
    else:
        assemble_video(scenes, img_dir, videos, audio_path, job_dir, out_mp4, progress, motion=motion)
    if cfg.get("speed_copies"):
        try:
            make_speed_copies(out_mp4, progress)
        except Cancelled:
            raise
        except Exception as e:  # ролик готов, копии — довесок: не роняем результат
            log(f"копии скорости: {e}")
            progress("render", 100, 100, f"копии скорости не сделались: {str(e)[:120]}")
    clear_wait()
    cleanup_job(job_dir)
    img_state = read_json(os.path.join(job_dir, "images.json"), {}) or {}
    bad_img = sum(1 for v in img_state.values() if isinstance(v, dict) and not v.get("ok"))
    anim = animated_scenes(scenes, mode, video_first) if (mode in ("video", "mixed") or video_first) else []
    have_vid = [s for s in anim if os.path.exists(os.path.join(job_dir, "videos", f"{s['index']:04d}.mp4"))]
    bad_vid = len(anim) - len(have_vid) if have_vid else 0  # ни одного клипа — оживление недоступно, повторять бессмысленно
    LAST_FAILURES.update({"images": bad_img, "videos": bad_vid})
    progress("done", 1, 1, f"готово: {out_mp4}")
    return out_mp4


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    def arg(name, default=None):
        return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

    def _read_script(path):
        if not path:
            return None
        with open(path, encoding="utf-8-sig") as f:
            return f.read()

    make_video(sys.argv[1], provider=arg("--provider"), mode=arg("--mode", "images"), style_key=arg("--style"),
               use_refs="--refs" in sys.argv, subtitles="--subs" in sys.argv, motion="--motion" in sys.argv,
               fastgen_quality=arg("--quality"), profile=arg("--profile"), script_text=_read_script(arg("--script")), prompts_file=arg("--prompts"),
               voice_file=sys.argv[1] if arg("--script") else None)
