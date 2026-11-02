# KadroGen — своя сборка с профилями

## Что где

| Папка | Что внутри |
|---|---|
| `builds/<версия>/` | Готовая версия, разложенная как папка программы: `KadroGen.exe`, `_internal/ui.html`, `fastgen_models.json`, `profiles/` и `ЧТО_НОВОГО.txt` |
| `kadrogen-src/` | Исходники с правками (`app.py`, `pipeline.py`, `profiles.py`, `ui.html`) и вся история изменений |
| `kadrogen-src/profiles/` | Профили: мастер-промпт и настройки для каждой ниши |
| `kadrogen-src/base/` | Оригинальный `KadroGen.exe` 1.7.1 — основа, в которую подставляются изменённые модули |
| `kadrogen-src/tools/patch_exe.py` | Сборка нового exe из оригинального без установки Python у пользователя |

## Как установить версию

1. Закрыть KadroGen.
2. Скопировать содержимое `builds/<версия>/` поверх папки программы (где `KadroGen.exe`, `_internal`, `ffmpeg`):
   - `KadroGen.exe` и `fastgen_models.json` — в папку программы;
   - `_internal/ui.html` — в `_internal` (интерфейс программа берёт оттуда);
   - `profiles/` — в папку программы, только если менялись профили (свои правки `master_prompt.txt` не затирать).

Ставится только поверх KadroGen 1.7.1. Автообновление от автора в этой сборке выключено.
