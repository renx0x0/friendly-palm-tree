# KadroGen — своя сборка с профилями

## Что где

| Папка | Что внутри |
|---|---|
| `builds/<версия>/` | Готовая версия для установки: `KadroGen.exe`, `ui.html`, `fastgen_models.json`, `profiles/` и `ЧТО_НОВОГО.txt` |
| `kadrogen-src/` | Исходники с правками (`app.py`, `pipeline.py`, `profiles.py`, `ui.html`) и вся история изменений |
| `kadrogen-src/profiles/` | Профили: мастер-промпт и настройки для каждой ниши |
| `kadrogen-src/base/` | Оригинальный `KadroGen.exe` 1.7.1 — основа, в которую подставляются изменённые модули |
| `kadrogen-src/tools/patch_exe.py` | Сборка нового exe из оригинального без установки Python у пользователя |

## Как установить версию

1. Закрыть KadroGen.
2. Из `builds/<версия>/` положить в папку программы (где лежат `ffmpeg` и DLL): `KadroGen.exe`, `ui.html`, `fastgen_models.json`.
3. Папку `profiles` копировать, только если менялись профили (свои правки `master_prompt.txt` не затирать).

Ставится только поверх KadroGen 1.7.1. Автообновление от автора в этой сборке выключено.
