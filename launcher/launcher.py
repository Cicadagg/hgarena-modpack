"""HG Arena launcher: syncs the modpack from GitHub and starts Minecraft Forge (offline nickname)."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import minecraft_launcher_lib as mll

LAUNCHER_VERSION = 1
REPO = "__REPO__"  # owner/name, filled in by publish.py before building
BRANCH = "main"
MANIFEST_URL = os.environ.get("HGARENA_MANIFEST") or f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/pack/manifest.json"
GAME_DIR = os.environ.get("HGARENA_DIR") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), ".hgarena")
SETTINGS_PATH = os.path.join(GAME_DIR, "launcher.json")
STATE_PATH = os.path.join(GAME_DIR, "launcher-state.json")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# minecraft-launcher-lib downloads without a timeout; a hung connection would freeze forever
socket.setdefaulttimeout(30)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def offline_uuid(name):
    md5 = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    md5[6] = (md5[6] & 0x0F) | 0x30
    md5[8] = (md5[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(md5)))


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Launcher:
    def __init__(self, root):
        self.root = root
        self.settings = load_json(SETTINGS_PATH, {"name": "", "ram": 8})
        root.title("HG Arena Launcher")
        root.resizable(False, False)
        frm = ttk.Frame(root, padding=16)
        frm.grid()

        ttk.Label(frm, text="HG Arena", font=("Segoe UI", 18, "bold")).grid(column=0, row=0, columnspan=3, pady=(0, 10))
        ttk.Label(frm, text="Ник:").grid(column=0, row=1, sticky="w")
        self.name_var = tk.StringVar(value=self.settings.get("name", ""))
        ttk.Entry(frm, textvariable=self.name_var, width=28).grid(column=1, row=1, columnspan=2, sticky="we", pady=2)
        ttk.Label(frm, text="Память, ГБ:").grid(column=0, row=2, sticky="w")
        self.ram_var = tk.IntVar(value=int(self.settings.get("ram", 8)))
        ttk.Spinbox(frm, from_=4, to=32, textvariable=self.ram_var, width=6).grid(column=1, row=2, sticky="w", pady=2)
        self.repair_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Проверить все файлы", variable=self.repair_var).grid(column=1, row=3, columnspan=2, sticky="w")

        self.play_btn = ttk.Button(frm, text="Играть", command=self.on_play)
        self.play_btn.grid(column=0, row=4, columnspan=3, sticky="we", pady=(10, 4), ipady=6)
        self.progress = ttk.Progressbar(frm, length=320, mode="determinate")
        self.progress.grid(column=0, row=5, columnspan=3, pady=4)
        self.status = tk.StringVar(value="Готов")
        ttk.Label(frm, textvariable=self.status, width=48).grid(column=0, row=6, columnspan=3, sticky="w")
        ttk.Button(frm, text="Папка игры", command=self.open_folder).grid(column=0, row=7, sticky="w", pady=(8, 0))
        ttk.Label(frm, text=f"v{LAUNCHER_VERSION}", foreground="gray").grid(column=2, row=7, sticky="e", pady=(8, 0))

    # thread-safe UI helpers
    def ui(self, fn, *a):
        self.root.after(0, fn, *a)

    def set_status(self, text):
        self.ui(self.status.set, text)

    def set_max(self, value):
        self.ui(self.progress.configure, {"maximum": max(1, value), "value": 0})

    def set_progress(self, value):
        self.ui(self.progress.configure, {"value": value})

    def open_folder(self):
        os.makedirs(GAME_DIR, exist_ok=True)
        os.startfile(GAME_DIR)

    def on_play(self):
        name = self.name_var.get().strip()
        if not (3 <= len(name) <= 16) or not all(c.isalnum() or c == "_" for c in name) or not name.isascii():
            messagebox.showerror("Ник", "Ник: 3-16 символов, латиница, цифры и _")
            return
        self.settings.update(name=name, ram=int(self.ram_var.get()))
        save_json(SETTINGS_PATH, self.settings)
        self.play_btn.configure(state="disabled")
        threading.Thread(target=self.run, args=(name, int(self.ram_var.get()), self.repair_var.get()), daemon=True).start()

    def run(self, name, ram, repair):
        try:
            self.prepare_and_launch(name, ram, repair)
        except Exception as e:
            traceback.print_exc()
            log = os.path.join(GAME_DIR, "launcher-error.log")
            os.makedirs(GAME_DIR, exist_ok=True)
            with open(log, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.set_status("Ошибка, подробности в launcher-error.log")
            self.ui(messagebox.showerror, "Ошибка", f"{e}\n\nЛог: {log}")
        finally:
            self.ui(self.play_btn.configure, {"state": "normal"})

    def retry(self, what, fn, *a, **kw):
        for attempt in range(1, 6):
            try:
                return fn(*a, **kw)
            except Exception:
                if attempt == 5:
                    raise
                traceback.print_exc()
                self.set_status(f"{what}: сбой сети, попытка {attempt + 1}/5...")
                time.sleep(3 * attempt)

    def callback(self):
        return {"setStatus": self.set_status, "setProgress": self.set_progress, "setMax": self.set_max}

    def prepare_and_launch(self, name, ram, repair):
        os.makedirs(GAME_DIR, exist_ok=True)
        self.set_status("Загрузка списка файлов...")
        manifest = self.retry("Список файлов", self.fetch_manifest)
        if manifest.get("launcher_version", 0) > LAUNCHER_VERSION:
            self.ui(messagebox.showinfo, "Обновление",
                    "Вышла новая версия лаунчера. Скачайте её на странице:\n"
                    f"https://github.com/{REPO}/releases/latest")

        mc, forge = manifest["minecraft"], manifest["forge"]
        forge_id = f"{mc}-forge-{forge}"
        state = load_json(STATE_PATH, {})

        if repair or state.get("installed") != forge_id or not os.path.isfile(
                os.path.join(GAME_DIR, "versions", forge_id, forge_id + ".json")):
            self.set_status(f"Установка Minecraft {mc}...")
            self.retry("Minecraft", mll.install.install_minecraft_version, mc, GAME_DIR, callback=self.callback())
            java = self.java_path(mc, console=True)
            self.set_status(f"Установка Forge {forge}...")
            self.retry("Forge", mll.mod_loader.get_mod_loader("forge").install,
                       mc, GAME_DIR, loader_version=forge, callback=self.callback(), java=java)
            state["installed"] = forge_id
            save_json(STATE_PATH, state)

        self.sync_files(manifest, state, repair)

        self.set_status("Запуск игры...")
        options = {
            "username": name,
            "uuid": offline_uuid(name).replace("-", ""),
            "token": "0",
            "executablePath": self.java_path(mc, console=False),
            "jvmArguments": [f"-Xmx{ram}G", f"-Xms{min(ram, 4)}G", "-XX:+UseG1GC"],
            "launcherName": "hgarena-launcher",
            "launcherVersion": str(LAUNCHER_VERSION),
            "gameDirectory": GAME_DIR,
        }
        if manifest.get("server"):
            # connect straight to our server (1.20 Quick Play)
            options["quickPlayMultiplayer"] = manifest["server"]
        cmd = mll.command.get_minecraft_command(forge_id, GAME_DIR, options)
        subprocess.Popen(cmd, cwd=GAME_DIR, creationflags=NO_WINDOW)
        self.set_status("Игра запущена. Лаунчер можно закрыть.")
        self.set_progress(0)

    @staticmethod
    def fetch_manifest():
        r = requests.get(MANIFEST_URL, timeout=30, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        return r.json()

    def java_path(self, mc, console):
        data = load_json(os.path.join(GAME_DIR, "versions", mc, mc + ".json"), {})
        comp = data.get("javaVersion", {}).get("component", "java-runtime-gamma")
        java = mll.runtime.get_executable_path(comp, GAME_DIR)
        if not java:
            self.retry("Java", mll.runtime.install_jvm_runtime, comp, GAME_DIR, callback=self.callback())
            java = mll.runtime.get_executable_path(comp, GAME_DIR)
        if not java:
            raise RuntimeError("Не удалось установить Java")
        if not console:
            javaw = os.path.join(os.path.dirname(java), "javaw.exe")
            if os.path.isfile(javaw):
                return javaw
        return java

    def download(self, e, tmp, done):
        h = hashlib.sha1()
        with requests.get(e["url"], stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    self.set_progress(done)
        if h.hexdigest() != e["sha1"]:
            os.remove(tmp)
            raise RuntimeError(f"Файл повреждён при скачивании: {e['path']}")

    def sync_files(self, manifest, state, repair):
        files = state.setdefault("files", {})
        entries = manifest["files"]
        todo = []
        self.set_status("Проверка файлов модпака...")
        for e in entries:
            target = os.path.join(GAME_DIR, e["path"])
            if e.get("unzip"):
                ok = files.get(e["path"]) == e["sha1"] and os.path.isdir(target)
            else:
                ok = os.path.isfile(target) and (
                    e.get("once") or (os.path.getsize(target) == e["size"] and
                                      (files.get(e["path"]) == e["sha1"] and not repair or sha1_file(target) == e["sha1"])))
            if not ok:
                todo.append(e)

        total = sum(e["size"] for e in todo)
        self.set_max(total)
        done = 0
        for i, e in enumerate(todo, 1):
            self.set_status(f"Скачивание {i}/{len(todo)}: {os.path.basename(e['path'])}")
            target = os.path.join(GAME_DIR, e["path"])
            tmp = os.path.join(GAME_DIR, ".download.tmp")
            self.retry(os.path.basename(e["path"]), self.download, e, tmp, done)
            done += e["size"]
            if e.get("unzip"):
                if os.path.isdir(target):
                    shutil.rmtree(target)
                with zipfile.ZipFile(tmp) as z:
                    z.extractall(target)
                os.remove(tmp)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(tmp, target)
            files[e["path"]] = e["sha1"]
            save_json(STATE_PATH, state)

        # remove mods that are no longer in the pack
        wanted = {os.path.normcase(os.path.join(GAME_DIR, e["path"])) for e in entries}
        mods_dir = os.path.join(GAME_DIR, "mods")
        if os.path.isdir(mods_dir):
            for fn in os.listdir(mods_dir):
                p = os.path.join(mods_dir, fn)
                if fn.lower().endswith(".jar") and os.path.normcase(p) not in wanted:
                    os.remove(p)
                    files.pop("mods/" + fn, None)
        save_json(STATE_PATH, state)


def main():
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
