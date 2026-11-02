"""Заменить модули Python внутри готового KadroGen.exe (PyInstaller 6, onedir) без пересборки.

Все записи архива копируются байт в байт, кроме:
  - скрипта app (запись типа 's' в CArchive) — новый app.py
  - модулей в PYZ из --module NAME=path.py — замена или добавление

Запускать Python 3.12 (версия байт-кода должна совпадать с python312.dll программы).
"""
import argparse
import marshal
import struct
import sys
import zlib

COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE_FMT = "!8sIIII64s"
COOKIE_LEN = struct.calcsize(COOKIE_FMT)
TOC_FMT = "!IIIIBc"
TOC_LEN = struct.calcsize(TOC_FMT)
PYZ_HEADER = 17


def read_carchive(exe):
    pos = exe.rfind(COOKIE_MAGIC)
    if pos < 0:
        sys.exit("не найден архив PyInstaller")
    magic, arch_len, toc_off, toc_len, pyver, pylib = struct.unpack(COOKIE_FMT, exe[pos:pos + COOKIE_LEN])
    start = pos + COOKIE_LEN - arch_len
    if pos + COOKIE_LEN != len(exe):
        sys.exit(f"после архива есть ещё {len(exe) - pos - COOKIE_LEN} байт (подпись?) — не трогаю")
    pkg = exe[start:pos + COOKIE_LEN]
    entries, p = [], toc_off
    while p < toc_off + toc_len:
        elen, doff, clen, ulen, comp, tc = struct.unpack(TOC_FMT, pkg[p:p + TOC_LEN])
        name = pkg[p + TOC_LEN:p + elen].rstrip(b"\0").decode("utf-8")
        entries.append({"name": name, "type": tc.decode(), "compress": comp, "ulen": ulen, "raw": pkg[doff:doff + clen]})
        p += elen
    return exe[:start], entries, pyver, pylib


def serialize_toc(toc):
    out = []
    for doff, clen, ulen, comp, tc, name in toc:
        nb = name.encode("utf-8")
        nlen = len(nb) + 1
        if (TOC_LEN + nlen) % 16:
            nlen += 16 - (TOC_LEN + nlen) % 16
        out.append(struct.pack(TOC_FMT + f"{nlen}s", TOC_LEN + nlen, doff, clen, ulen, comp, tc.encode("ascii"), nb))
    return b"".join(out)


def write_carchive(head, entries, pyver, pylib):
    body, toc = bytearray(), []
    for e in entries:
        toc.append((len(body), len(e["raw"]), e["ulen"], e["compress"], e["type"], e["name"]))
        body += e["raw"]
    toc_off = len(body)
    toc_data = serialize_toc(toc)
    body += toc_data
    arch_len = toc_off + len(toc_data) + COOKIE_LEN
    body += struct.pack(COOKIE_FMT, COOKIE_MAGIC, arch_len, toc_off, len(toc_data), pyver, pylib)
    return head + bytes(body)


def compile_src(path, co_filename):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    return marshal.dumps(compile(src, co_filename, "exec", dont_inherit=True, optimize=0))


def rebuild_pyz(raw, modules):
    """raw — PYZ целиком; modules — {имя: путь к .py}. Остальные записи копируются как есть."""
    if raw[:4] != b"PYZ\0":
        sys.exit("PYZ: неверная сигнатура")
    import importlib.util
    if raw[4:8] != importlib.util.MAGIC_NUMBER:
        sys.exit("PYZ собран другой версией Python — нужен Python той же версии, что и в программе")
    (toc_off,) = struct.unpack("!i", raw[8:12])
    toc = marshal.loads(raw[toc_off:])
    module_type = next(v[0] for n, v in toc if n == "pipeline")
    out = bytearray(raw[:PYZ_HEADER])
    new_toc, done = [], set()
    for name, (tc, pos, length) in toc:
        if name in modules:
            blob = zlib.compress(compile_src(modules[name], name.replace(".", "/") + ".py"), 6)
            done.add(name)
        else:
            blob = raw[pos:pos + length]
        new_toc.append((name, (tc, len(out), len(blob))))
        out += blob
    for name, path in modules.items():
        if name not in done:
            blob = zlib.compress(compile_src(path, name.replace(".", "/") + ".py"), 6)
            new_toc.append((name, (module_type, len(out), len(blob))))
            out += blob
    toc_pos = len(out)
    out += marshal.dumps(new_toc)
    out[8:12] = struct.pack("!i", toc_pos)
    return bytes(out), sorted(done), sorted(set(modules) - done)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("out")
    ap.add_argument("--app", required=True, help="новый app.py (скрипт запуска)")
    ap.add_argument("--module", action="append", default=[], help="NAME=path.py — модуль в PYZ")
    a = ap.parse_args()
    modules = dict(m.split("=", 1) for m in a.module)
    with open(a.exe, "rb") as f:
        exe = f.read()
    head, entries, pyver, pylib = read_carchive(exe)
    print(f"bootloader {len(head)} байт, записей {len(entries)}, python {pyver}, {pylib.rstrip(bytes(1)).decode()}")
    for e in entries:
        if e["type"] == "z":
            data = zlib.decompress(e["raw"]) if e["compress"] else e["raw"]
            new, replaced, added = rebuild_pyz(data, modules)
            e["raw"] = zlib.compress(new, 9) if e["compress"] else new
            e["ulen"] = len(new)
            print(f"PYZ: заменено {replaced}, добавлено {added}")
        elif e["type"] == "s" and e["name"] == "app":
            code = compile_src(a.app, "app.py")
            e["raw"] = zlib.compress(code, 9) if e["compress"] else code
            e["ulen"] = len(code)
            print("скрипт app заменён")
    with open(a.out, "wb") as f:
        f.write(write_carchive(head, entries, pyver, pylib))
    print("готово:", a.out)


if __name__ == "__main__":
    main()
