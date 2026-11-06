"""Publish the modpack from the local game folder to GitHub.

Usage:
    python publish.py             # upload changed mods/configs/map, update manifest, push
    python publish.py --launcher  # also build HGArenaLauncher.exe and publish it as the latest release

Mods go to the "files" release (GitHub release assets, no size/bandwidth problems),
configs go into the repo itself (pack/files) and are downloaded via raw.githubusercontent.com.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = r"C:\Games\Minecraft"
GH = r"C:\Minecraft-1.20.1-Forge\tools\gh\bin\gh.exe"
MINECRAFT = "1.20.1"
FORGE = "47.4.28"
SERVER = ""  # "host:port" of our server; the launcher connects to it right after start
ONCE_FILES = ["options.txt"]  # copied only if the player has none yet
FILES_TAG = "files"
PACK_FILES = os.path.join(HERE, "pack", "files")


def run(*cmd, capture=False):
    print(">", " ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE, check=True, text=True, encoding="utf-8",
                       capture_output=capture)
    return r.stdout if capture else None


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_name(sha1, filename):
    return sha1[:10] + "-" + re.sub(r"[^A-Za-z0-9._+-]", "_", filename)


def main():
    build_launcher = "--launcher" in sys.argv
    repo = json.loads(run(GH, "repo", "view", "--json", "nameWithOwner", capture=True))["nameWithOwner"]
    raw = f"https://raw.githubusercontent.com/{repo}/main/pack/files/"
    dl = f"https://github.com/{repo}/releases/download/{FILES_TAG}/"
    entries, uploads = [], {}

    # mods -> release assets
    mods = os.path.join(SOURCE, "mods")
    for fn in sorted(os.listdir(mods)):
        if fn.lower().endswith(".jar"):
            p = os.path.join(mods, fn)
            s = sha1_file(p)
            name = asset_name(s, fn)
            uploads[name] = p
            entries.append({"path": "mods/" + fn, "sha1": s, "size": os.path.getsize(p), "url": dl + name})

    # configs and once-files -> repo
    if os.path.isdir(PACK_FILES):
        shutil.rmtree(PACK_FILES)
    for root, dirs, files in os.walk(os.path.join(SOURCE, "config")):
        for fn in files:
            if fn.endswith(".bak") or fn.endswith(".tmp"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, SOURCE).replace("\\", "/")
            dst = os.path.join(PACK_FILES, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
            entries.append({"path": rel, "sha1": sha1_file(full), "size": os.path.getsize(full), "url": raw + quote(rel)})
    for rel in ONCE_FILES:
        full = os.path.join(SOURCE, rel)
        if os.path.isfile(full):
            shutil.copy2(full, os.path.join(PACK_FILES, rel))
            entries.append({"path": rel, "sha1": sha1_file(full), "size": os.path.getsize(full),
                            "url": raw + quote(rel), "once": True})

    # release with assets
    try:
        assets = json.loads(run(GH, "release", "view", FILES_TAG, "--json", "assets", capture=True))["assets"]
    except subprocess.CalledProcessError:
        run(GH, "release", "create", FILES_TAG, "--prerelease", "--title", "Modpack files",
            "--notes", "Файлы модпака для лаунчера. Скачивать вручную не нужно.")
        assets = []
    existing = {a["name"] for a in assets}
    for name, path in uploads.items():
        if name not in existing:
            # the asset gets the local file name, so upload a copy named like the asset
            staged = os.path.join(HERE, "build", "upload", name)
            os.makedirs(os.path.dirname(staged), exist_ok=True)
            shutil.copy2(path, staged)
            run(GH, "release", "upload", FILES_TAG, staged)
            os.remove(staged)
    for name in existing - set(uploads):
        run(GH, "release", "delete-asset", FILES_TAG, name, "--yes")

    launcher_version = 1
    if build_launcher:
        launcher_version = build_and_release(repo)
    else:
        old = os.path.join(HERE, "pack", "manifest.json")
        if os.path.isfile(old):
            with open(old, encoding="utf-8") as f:
                launcher_version = json.load(f).get("launcher_version", 1)

    manifest = {"minecraft": MINECRAFT, "forge": FORGE, "server": SERVER,
                "launcher_version": launcher_version, "files": entries}
    with open(os.path.join(HERE, "pack", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    run("git", "add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE).returncode != 0:
        run("git", "commit", "-m", "Update modpack")
        run("git", "push")
    print(f"\nГотово: {len(entries)} файлов в манифесте.")


def build_and_release(repo):
    src = os.path.join(HERE, "launcher", "launcher.py")
    with open(src, encoding="utf-8") as f:
        code = f.read()
    version = int(re.search(r"LAUNCHER_VERSION = (\d+)", code).group(1))
    build = os.path.join(HERE, "build", "launcher")
    os.makedirs(build, exist_ok=True)
    with open(os.path.join(build, "launcher.py"), "w", encoding="utf-8") as f:
        f.write(code.replace('"__REPO__"', json.dumps(repo)))
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
        "--name", "HGArenaLauncher", "--distpath", os.path.join(build, "dist"),
        "--workpath", os.path.join(build, "work"), "--specpath", build,
        os.path.join(build, "launcher.py"))
    exe = os.path.join(build, "dist", "HGArenaLauncher.exe")
    tag = f"launcher-v{version}"
    if subprocess.run([GH, "release", "view", tag], cwd=HERE, capture_output=True).returncode == 0:
        run(GH, "release", "upload", tag, exe, "--clobber")
    else:
        run(GH, "release", "create", tag, exe, "--latest", "--title", f"Лаунчер v{version}",
            "--notes", "Скачайте HGArenaLauncher.exe, запустите, введите ник и нажмите «Играть».")
    return version


if __name__ == "__main__":
    main()
