"""Сборка релизного архива (tar.gz) без внешних утилит."""
import pathlib
import tarfile

REL = pathlib.Path("/media/lex/disk/projects/claude/Uspevaika/_release")
OUT = REL / "uspevaika-release-2026-11-25.tar.gz"

items = ["app", "install.sh", "uninstall.sh", "autoupdate.sh",
         "uspevaika.service", "uspevaika-update.service", "uspevaika-update.timer",
         "DOCS.md", "env.template", "ЧИТАЙ-МЕНЯ.md", "deploy"]

SKIP_DIRS = {"__pycache__"}
SKIP_FILES = {"admin-old.html"}

def keep(p: pathlib.Path) -> bool:
    if any(part in SKIP_DIRS for part in p.parts):
        return False
    if p.name in SKIP_FILES or p.suffix == ".pyc":
        return False
    return True

with tarfile.open(OUT, "w:gz") as tar:
    for name in items:
        p = REL / name
        if not p.exists():
            print("НЕТ:", name)
            continue
        if p.is_file():
            tar.add(p, arcname=name)
        else:
            # добавляем только файлы: rglob("*") отдаёт и папки, и tar считал
            # одну и ту же картинку дважды, раздувая архив вдвое
            for sub in sorted(p.rglob("*")):
                if sub.is_file() and keep(sub):
                    tar.add(sub, arcname=str(sub.relative_to(REL)))

with tarfile.open(OUT) as tar:
    names = tar.getnames()
    members = tar.getmembers()
print(f"файлов в архиве: {len(names)}")
print(f"размер: {OUT.stat().st_size / 1024 / 1024:.1f} МБ")
print("\nсамые большие:")
for m in sorted(members, key=lambda m: m.size, reverse=True)[:5]:
    print(f"  {m.size/1024/1024:6.1f} МБ  {m.name}")
print("\nскрипты:")
for n in names:
    if n.endswith(".sh") or n.endswith(".service"):
        print(" ", n)
