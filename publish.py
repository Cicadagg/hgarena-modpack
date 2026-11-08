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
import time
import zipfile
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = r"C:\Games\Minecraft"
GH = r"C:\Minecraft-1.20.1-Forge\tools\gh\bin\gh.exe"
MINECRAFT = "1.20.1"
FORGE = "47.4.28"
SERVER = "185.9.145.129:32972"  # "host:port" of our server; the launcher connects to it right after start
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
    return sha1[:10] + "-" + re.sub(r"[^A-Za-z0-9._-]", "_", filename)


def servers_dat(name, ip):
    """Uncompressed NBT: {servers: [{name, ip, acceptTextures: 1}]} as Minecraft writes servers.dat."""
    import struct

    def s(text):
        b = text.encode("utf-8")
        return struct.pack(">H", len(b)) + b

    server = (b"\x08" + s("name") + s(name) + b"\x08" + s("ip") + s(ip)
              + b"\x01" + s("acceptTextures") + b"\x01" + b"\x00")
    servers = b"\x09" + s("servers") + b"\x0a" + struct.pack(">i", 1) + server
    return b"\x0a" + s("") + servers + b"\x00"


def main():
    build_launcher = "--launcher" in sys.argv
    repo = json.loads(run(GH, "repo", "view", "--json", "nameWithOwner", capture=True))["nameWithOwner"]
    raw = "@RAW@"  # replaced by a commit-pinned URL below (raw "main" is CDN-cached for minutes)
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

    # configs -> one zip asset (one request instead of ~170); a copy stays in the repo for history
    if os.path.isdir(PACK_FILES):
        shutil.rmtree(PACK_FILES)
    config_files = []
    for root, dirs, files in os.walk(os.path.join(SOURCE, "config")):
        for fn in files:
            if fn.endswith(".bak") or fn.endswith(".tmp"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, SOURCE).replace("\\", "/")
            dst = os.path.join(PACK_FILES, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(full, dst)
            config_files.append((rel, full))
    zip_path = os.path.join(HERE, "build", "configs.zip")
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, full in sorted(config_files):
            # fixed timestamp: same configs -> same zip -> same SHA1, so no re-upload or re-download
            info = zipfile.ZipInfo(rel[len("config/"):], date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(full, "rb") as f:
                z.writestr(info, f.read())
    s = sha1_file(zip_path)
    name = asset_name(s, "configs.zip")
    uploads[name] = zip_path
    entries.append({"path": "config", "sha1": s, "size": os.path.getsize(zip_path), "url": dl + name,
                    "unzip": True, "merge": True})
    for rel in ONCE_FILES:
        full = os.path.join(SOURCE, rel)
        if os.path.isfile(full):
            shutil.copy2(full, os.path.join(PACK_FILES, rel))
            entries.append({"path": rel, "sha1": sha1_file(full), "size": os.path.getsize(full),
                            "url": raw + quote(rel), "once": True})

    # servers.dat with our server, so it is already in the multiplayer list
    if SERVER:
        path = os.path.join(PACK_FILES, "servers.dat")
        os.makedirs(PACK_FILES, exist_ok=True)
        with open(path, "wb") as f:
            f.write(servers_dat("HG Arena", SERVER))
        entries.append({"path": "servers.dat", "sha1": sha1_file(path), "size": os.path.getsize(path),
                        "url": raw + "servers.dat"})

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
            for attempt in range(5):  # uploads sometimes drop on a flaky connection
                try:
                    run(GH, "release", "upload", FILES_TAG, staged, "--clobber")
                    break
                except subprocess.CalledProcessError:
                    if attempt == 4:
                        raise
                    time.sleep(5)
            os.remove(staged)
    for name in existing - set(uploads):
        run(GH, "release", "delete-asset", FILES_TAG, name, "--yes")

    # launcher_* fields let the launcher update itself
    launcher = {"launcher_version": 1}
    if build_launcher:
        launcher = build_and_release(repo)
    else:
        old = os.path.join(HERE, "pack", "manifest.json")
        if os.path.isfile(old):
            with open(old, encoding="utf-8") as f:
                launcher = {k: v for k, v in json.load(f).items() if k.startswith("launcher_")}

    # commit the files first, then point the manifest at that exact commit
    run("git", "add", "-A", "pack/files")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE).returncode != 0:
        run("git", "commit", "-m", "Update pack files")
        run("git", "push")
    commit = run("git", "rev-parse", "HEAD", capture=True).strip()
    for e in entries:
        e["url"] = e["url"].replace("@RAW@", f"https://raw.githubusercontent.com/{repo}/{commit}/pack/files/")

    manifest = {"minecraft": MINECRAFT, "forge": FORGE, "server": SERVER, **launcher, "files": entries}
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
    return {"launcher_version": version, "launcher_sha1": sha1_file(exe), "launcher_size": os.path.getsize(exe),
            "launcher_url": f"https://github.com/{repo}/releases/download/{tag}/HGArenaLauncher.exe"}


if __name__ == "__main__":
    main()
