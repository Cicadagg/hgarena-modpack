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
from concurrent.futures import ThreadPoolExecutor, FIRST_EXCEPTION, wait
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import minecraft_launcher_lib as mll
from minecraft_launcher_lib._helper import get_library_path, parse_rule_list

LAUNCHER_VERSION = 5
WORKERS = 8  # parallel connections: one GitHub connection is slow, several fill the channel
PART = 8 << 20  # big files are split into 8 MB ranges downloaded in parallel and resumed on drops
REPO = "__REPO__"  # owner/name, filled in by publish.py before building
BRANCH = "main"
MANIFEST_URL = os.environ.get("HGARENA_MANIFEST") or f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/pack/manifest.json"
GAME_DIR = os.environ.get("HGARENA_DIR") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), ".hgarena")
SETTINGS_PATH = os.path.join(GAME_DIR, "launcher.json")
STATE_PATH = os.path.join(GAME_DIR, "launcher-state.json")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# minecraft-launcher-lib downloads without a timeout; a hung connection would freeze forever
socket.setdefaulttimeout(30)
CHECK_EVERY_MS = 10 * 60 * 1000  # how often the open launcher looks for a new modpack version


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
        self.play_btn = ttk.Button(frm, text="Играть", command=lambda: self.start("play"))
        self.play_btn.grid(column=0, row=4, columnspan=3, sticky="we", pady=(10, 4), ipady=6)
        row = ttk.Frame(frm)
        row.grid(column=0, row=5, columnspan=3, sticky="we")
        for i in range(3):
            row.columnconfigure(i, weight=1)
        self.update_btn = ttk.Button(row, text="Обновить моды", command=lambda: self.start("update"))
        self.update_btn.grid(column=0, row=0, sticky="we", padx=(0, 2))
        self.restart_btn = ttk.Button(row, text="Перезапустить игру", command=lambda: self.start("restart"))
        self.restart_btn.grid(column=1, row=0, sticky="we", padx=2)
        self.repair_btn = ttk.Button(row, text="Проверить файлы", command=lambda: self.start("repair"))
        self.repair_btn.grid(column=2, row=0, sticky="we", padx=(2, 0))
        self.buttons = [self.play_btn, self.update_btn, self.restart_btn, self.repair_btn]

        self.progress = ttk.Progressbar(frm, length=360, mode="determinate")
        self.progress.grid(column=0, row=6, columnspan=3, pady=(8, 4))
        self.status = tk.StringVar(value="Готов")
        ttk.Label(frm, textvariable=self.status, width=54).grid(column=0, row=7, columnspan=3, sticky="w")
        self.update_info = tk.StringVar(value="Проверка обновлений...")
        self.update_label = ttk.Label(frm, textvariable=self.update_info, wraplength=360, foreground="gray")
        self.update_label.grid(column=0, row=8, columnspan=3, sticky="w")
        ttk.Button(frm, text="Папка игры", command=self.open_folder).grid(column=0, row=9, sticky="w", pady=(8, 0))
        ttk.Label(frm, text=f"v{LAUNCHER_VERSION}", foreground="gray").grid(column=2, row=9, sticky="e", pady=(8, 0))
        self.busy = False
        root.after(300, self.check_updates)

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

    def on_play(self):  # --autoplay after a self-update
        self.start("play")

    def start(self, mode):
        """mode: play (update + start), update (only update), restart (close the game, update, start),
        repair (check every file by its checksum, then start)."""
        if self.busy:
            return
        name = self.name_var.get().strip()
        if mode != "update":
            if not (3 <= len(name) <= 16) or not all(c.isalnum() or c == "_" for c in name) or not name.isascii():
                messagebox.showerror("Ник", "Ник: 3-16 символов, латиница, цифры и _")
                return
            self.settings.update(name=name, ram=int(self.ram_var.get()))
            save_json(SETTINGS_PATH, self.settings)
        running = self.game_running()
        if running and mode == "restart":
            pass
        elif running and mode in ("play", "repair"):
            if not messagebox.askyesno("Игра уже запущена", "Игра уже запущена. Закрыть её, обновить моды и запустить заново?"):
                return
            mode = "restart" if mode == "play" else mode
        elif running and mode == "update":
            if not messagebox.askyesno("Игра запущена", "Пока игра запущена, моды не обновить. Закрыть игру и обновить?"):
                return
        elif not running and mode == "restart":
            mode = "play"
        self.busy = True
        for b in self.buttons:
            b.configure(state="disabled")
        threading.Thread(target=self.run, args=(mode, name, int(self.ram_var.get())), daemon=True).start()

    def run(self, mode, name, ram):
        try:
            if self.game_running():
                self.set_status("Закрываю игру...")
                self.stop_game()
            manifest = self.prepare(repair=mode == "repair", after_update="--update" if mode == "update" else "--autoplay")
            if manifest is None:
                return  # the launcher is restarting itself
            self.ui(self.show_update_info, 0, 0)
            if mode == "update":
                self.set_status("Моды обновлены. Можно играть.")
                self.set_progress(0)
            else:
                self.launch(manifest, name, ram)
        except Exception as e:
            traceback.print_exc()
            log = os.path.join(GAME_DIR, "launcher-error.log")
            os.makedirs(GAME_DIR, exist_ok=True)
            with open(log, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.set_status("Ошибка, подробности в launcher-error.log")
            self.ui(messagebox.showerror, "Ошибка", f"{e}\n\nЛог: {log}")
        finally:
            self.ui(self.unlock)

    def unlock(self):
        self.busy = False
        for b in self.buttons:
            b.configure(state="normal")

    # ---------- the running game ----------
    def game_running(self):
        pid = load_json(STATE_PATH, {}).get("game_pid")
        if not pid:
            return False
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, creationflags=NO_WINDOW, timeout=10).stdout.lower()
        except Exception:
            return False
        return "java" in out  # the pid may have been reused by another program

    def stop_game(self):
        pid = load_json(STATE_PATH, {}).get("game_pid")
        if pid and self.game_running():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
            for _ in range(50):  # wait until the mod files are no longer locked
                if not self.game_running():
                    break
                time.sleep(0.2)
            time.sleep(1)

    # ---------- update check ----------
    def check_updates(self):
        if not self.busy:
            threading.Thread(target=self._check_updates, daemon=True).start()
        self.root.after(CHECK_EVERY_MS, self.check_updates)

    def _check_updates(self):
        try:
            manifest = self.fetch_manifest()
            state = load_json(STATE_PATH, {})
            todo = [e for e in manifest["files"] if self.needs(e, state.get("files", {}), False)]
            newer_launcher = manifest.get("launcher_version", 0) > LAUNCHER_VERSION
            self.ui(self.show_update_info, len(todo) + (1 if newer_launcher else 0), sum(e["size"] for e in todo))
        except Exception:
            traceback.print_exc()
            self.ui(self.update_info.set, "Не удалось проверить обновления (нет сети?)")

    def show_update_info(self, count, size):
        if count:
            amount = f"{size / 1048576:.0f} МБ" if size >= 1048576 else f"{max(1, size // 1024)} КБ"
            text = f"Есть обновление ({amount}). Нажмите «Обновить моды» или «Играть»."
            if self.game_running():
                text = f"Есть обновление ({amount}). Нажмите «Перезапустить игру»."
            self.update_info.set(text)
            self.update_label.configure(foreground="#c05000")
        else:
            self.update_info.set("Моды актуальны")
            self.update_label.configure(foreground="gray")

    def retry(self, what, fn, *a, **kw):
        for attempt in range(1, 11):
            try:
                return fn(*a, **kw)
            except Exception:
                if attempt == 10:
                    raise
                traceback.print_exc()
                self.set_status(f"{what}: сбой сети, попытка {attempt + 1}/10...")
                time.sleep(min(3 * attempt, 15))

    def callback(self):
        return {"setStatus": self.set_status, "setProgress": self.set_progress, "setMax": self.set_max}

    def prepare(self, repair, after_update):
        """Installs Minecraft/Forge if needed and syncs the modpack. Returns the manifest,
        or None when the launcher replaced itself with a new version and restarts."""
        os.makedirs(GAME_DIR, exist_ok=True)
        self.set_status("Загрузка списка файлов...")
        manifest = self.retry("Список файлов", self.fetch_manifest)
        if manifest.get("launcher_version", 0) > LAUNCHER_VERSION:
            if self.self_update(manifest, after_update):
                return None
            self.ui(messagebox.showinfo, "Обновление",
                    "Вышла новая версия лаунчера. Скачайте её на странице:\n"
                    f"https://github.com/{REPO}/releases/latest")

        mc, forge = manifest["minecraft"], manifest["forge"]
        forge_id = f"{mc}-forge-{forge}"
        state = load_json(STATE_PATH, {})

        # check the files on disk; install only the parts that are actually missing
        self.set_status("Проверка файлов игры...")
        missing = {"vanilla": "проверка", "forge": "проверка"} if repair else self.missing_game_files(mc, forge)
        print("missing game files:", missing)
        if "vanilla" in missing:
            self.set_status(f"Установка Minecraft {mc}...")
            self.retry("Minecraft", mll.install.install_minecraft_version, mc, GAME_DIR, callback=self.callback())
        if "forge" in missing:
            java = self.java_path(mc, console=True)
            self.set_status(f"Установка Forge {forge}...")
            self.retry("Forge", mll.mod_loader.get_mod_loader("forge").install,
                       mc, GAME_DIR, loader_version=forge, callback=self.callback(), java=java)
        if missing:
            state["installed"] = forge_id
            save_json(STATE_PATH, state)

        self.sync_files(manifest, state, repair)
        return manifest

    def launch(self, manifest, name, ram):
        mc, forge = manifest["minecraft"], manifest["forge"]
        forge_id = f"{mc}-forge-{forge}"
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
        game = subprocess.Popen(cmd, cwd=GAME_DIR, creationflags=NO_WINDOW)
        state = load_json(STATE_PATH, {})
        state["game_pid"] = game.pid
        save_json(STATE_PATH, state)
        self.set_status("Игра запущена. Лаунчер можно закрыть.")
        self.set_progress(0)

    @staticmethod
    def fetch_manifest():
        url = MANIFEST_URL
        if not os.environ.get("HGARENA_MANIFEST"):
            # raw.githubusercontent.com serves "main" from a cache for several minutes after a publish, so players
            # got the old mod and the server refused them. The manifest of the latest commit is never stale.
            try:
                c = requests.get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}", timeout=15,
                                 headers={"Accept": "application/vnd.github.sha"})
                sha = c.text.strip()
                if c.ok and len(sha) == 40 and all(ch in "0123456789abcdef" for ch in sha):
                    url = f"https://raw.githubusercontent.com/{REPO}/{sha}/pack/manifest.json"
            except requests.RequestException:
                pass  # API limit or no access: fall back to the branch URL
        r = requests.get(url, timeout=30, headers={"Cache-Control": "no-cache"})
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

    @staticmethod
    def missing_game_files(mc, forge):
        """Fast on-disk check (existence and size, no hashing) of Minecraft, its libraries and assets,
        and Forge with the files its installer generates. Returns {"vanilla"/"forge": first missing file}."""
        missing = {}
        vdir = os.path.join(GAME_DIR, "versions")
        libs = os.path.join(GAME_DIR, "libraries")
        forge_id = f"{mc}-forge-{forge}"

        def bad(path, size=None):
            return not os.path.isfile(path) or (size is not None and os.path.getsize(path) != size)

        def check_libs(part, libraries):
            for lib in libraries:
                if "rules" in lib and not parse_rule_list(lib["rules"], {}):
                    continue  # not for this OS
                art = lib.get("downloads", {}).get("artifact")
                if art and art.get("path"):
                    path, size = os.path.join(libs, art["path"]), art.get("size")
                else:
                    path, size = get_library_path(lib["name"], GAME_DIR), None
                if bad(path, size):
                    missing.setdefault(part, lib["name"])
                    return

        vanilla = load_json(os.path.join(vdir, mc, mc + ".json"), None)
        if not vanilla:
            return {"vanilla": mc + ".json", "forge": forge_id}
        try:
            if bad(os.path.join(vdir, mc, mc + ".jar"), vanilla["downloads"]["client"].get("size")):
                missing["vanilla"] = mc + ".jar"
            check_libs("vanilla", vanilla["libraries"])
            index = load_json(os.path.join(GAME_DIR, "assets", "indexes", vanilla["assetIndex"]["id"] + ".json"), None)
            if not index:
                missing.setdefault("vanilla", "assets index")
            else:
                for name, obj in index["objects"].items():
                    h = obj["hash"]
                    if bad(os.path.join(GAME_DIR, "assets", "objects", h[:2], h), obj.get("size")):
                        missing.setdefault("vanilla", name)
                        break
        except (KeyError, TypeError):
            missing.setdefault("vanilla", mc + ".json")

        fj = load_json(os.path.join(vdir, forge_id, forge_id + ".json"), None)
        if not fj:
            missing["forge"] = forge_id + ".json"
            return missing
        try:
            check_libs("forge", fj["libraries"])
            game_args = fj["arguments"]["game"]
            mcp = game_args[game_args.index("--fml.mcpVersion") + 1]
            for path in (
                    os.path.join(libs, "net", "minecraftforge", "forge", f"{mc}-{forge}", f"forge-{mc}-{forge}-client.jar"),
                    os.path.join(libs, "net", "minecraftforge", "forge", f"{mc}-{forge}", f"forge-{mc}-{forge}-universal.jar"),
                    os.path.join(libs, "net", "minecraft", "client", f"{mc}-{mcp}", f"client-{mc}-{mcp}-srg.jar"),
                    os.path.join(libs, "net", "minecraft", "client", f"{mc}-{mcp}", f"client-{mc}-{mcp}-extra.jar")):
                if bad(path):
                    missing.setdefault("forge", os.path.basename(path))
                    break
        except (KeyError, TypeError, ValueError, IndexError):
            missing.setdefault("forge", forge_id + ".json")
        return missing

    # ---------- self-update ----------
    def self_update(self, manifest, after_update="--autoplay"):
        """Replace the running exe with the new release and restart it. False if that is not possible."""
        if not getattr(sys, "frozen", False) or not manifest.get("launcher_sha1"):
            return False
        exe = sys.executable
        new, old = exe + ".new", exe + ".old"
        try:
            self.set_status("Обновление лаунчера...")
            e = {"path": os.path.basename(exe), "sha1": manifest["launcher_sha1"],
                 "size": manifest["launcher_size"], "url": manifest["launcher_url"]}
            self.download_all([e], lambda e, tmp: os.replace(tmp, new))
            if os.path.exists(old):
                os.remove(old)
            os.replace(exe, old)  # Windows allows renaming a running exe
            try:
                os.replace(new, exe)
            except Exception:
                os.replace(old, exe)
                raise
        except Exception:
            traceback.print_exc()
            return False
        # a fresh PyInstaller process, not a child that reuses our unpacked files
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        subprocess.Popen([exe, after_update], env=env, close_fds=True)
        os._exit(0)

    # ---------- parallel downloads ----------
    def session(self):
        s = getattr(self._tls, "s", None)
        if s is None:
            s = self._tls.s = requests.Session()
        return s

    def add_progress(self, n):
        with self.lock:
            self.done += n
            done, now = self.done, time.monotonic()
            if now - self.last_ui < 0.25 and done < self.total:
                return
            self.last_ui = now
            speed = done / max(now - self.t0, 0.001)
        self.set_progress(done)
        self.set_status(f"Скачивание: {done / 1048576:.0f} из {self.total / 1048576:.0f} МБ, "
                        f"{speed / 1048576:.1f} МБ/с")

    def fetch_range(self, url, path, start, end):
        """Write bytes [start, end) of url into path at the same offsets, resuming after drops."""
        pos = start
        for attempt in range(1, 11):
            try:
                headers = {"Range": f"bytes={pos}-{end - 1}"}
                with self.session().get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
                    r.raise_for_status()
                    skip = pos if r.status_code != 206 else 0  # Range ignored: skip up to our offset
                    with open(path, "r+b") as f:
                        f.seek(pos)
                        for chunk in r.iter_content(1 << 16):
                            if skip:
                                cut = min(skip, len(chunk))
                                chunk, skip = chunk[cut:], skip - cut
                            chunk = chunk[:end - pos]
                            if chunk:
                                f.write(chunk)
                                pos += len(chunk)
                                self.add_progress(len(chunk))
                            if pos >= end or self.cancel.is_set():
                                break
                if pos < end and not self.cancel.is_set():
                    raise IOError("соединение оборвалось")
                return
            except Exception:
                if attempt == 10 or self.cancel.is_set():
                    raise
                traceback.print_exc()
                self.set_status(f"Сбой сети, повтор {attempt + 1}/10...")
                time.sleep(min(2 * attempt, 15))

    def finish(self, e, tmp, on_done):
        if sha1_file(tmp) != e["sha1"]:
            # rare: a range came back damaged, fetch the whole file once more
            with self.lock:
                self.done -= e["size"]
            self.fetch_range(e["url"], tmp, 0, e["size"])
            if sha1_file(tmp) != e["sha1"]:
                raise RuntimeError(f"Файл повреждён при скачивании: {e['path']}")
        with self.lock:
            on_done(e, tmp)

    def download_all(self, todo, on_done):
        """Download entries over WORKERS connections, big files in PART-sized ranges.
        on_done(e, tmp) runs once per file after its SHA1 is verified (one at a time)."""
        self._tls = threading.local()
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.total = sum(e["size"] for e in todo)
        self.done, self.last_ui, self.t0 = 0, 0, time.monotonic()
        self.set_max(self.total)
        tmp_dir = os.path.join(GAME_DIR, ".download")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir)
        left, jobs = {}, []
        try:
            # biggest files first so they do not end up as a slow tail
            for i, e in enumerate(sorted(todo, key=lambda e: -e["size"])):
                tmp = os.path.join(tmp_dir, str(i))
                with open(tmp, "wb") as f:
                    f.truncate(e["size"])
                parts = [(s, min(s + PART, e["size"])) for s in range(0, e["size"], PART)]
                left[tmp] = len(parts)
                if not parts:
                    self.finish(e, tmp, on_done)
                jobs += [(e, tmp, s, end) for s, end in parts]

            def job(e, tmp, s, end):
                self.fetch_range(e["url"], tmp, s, end)
                with self.lock:
                    left[tmp] -= 1
                    last = left[tmp] == 0
                if last and not self.cancel.is_set():
                    self.finish(e, tmp, on_done)

            pool = ThreadPoolExecutor(WORKERS)
            try:
                futures = [pool.submit(job, *j) for j in jobs]
                finished, _ = wait(futures, return_when=FIRST_EXCEPTION)
                for f in finished:
                    f.result()  # re-raise the first error
            finally:
                self.cancel.set()
                pool.shutdown(wait=True, cancel_futures=True)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def needs(e, files, repair):
        """True if the entry has to be downloaded. Without repair the checksum is trusted from the state file."""
        target = os.path.join(GAME_DIR, e["path"])
        if e.get("unzip"):
            return not (files.get(e["path"]) == e["sha1"] and os.path.isdir(target) and not repair)
        return not (os.path.isfile(target) and (
            e.get("once") or (os.path.getsize(target) == e["size"] and
                              (files.get(e["path"]) == e["sha1"] and not repair or sha1_file(target) == e["sha1"]))))

    def sync_files(self, manifest, state, repair):
        files = state.setdefault("files", {})
        entries = manifest["files"]
        self.set_status("Проверка файлов модпака...")
        todo = [e for e in entries if self.needs(e, files, repair)]

        def install(e, tmp):
            target = os.path.join(GAME_DIR, e["path"])
            if e.get("unzip"):
                # "merge": unpack over the folder, keeping files the pack does not ship
                if not e.get("merge") and os.path.isdir(target):
                    shutil.rmtree(target)
                with zipfile.ZipFile(tmp) as z:
                    z.extractall(target)
                os.remove(tmp)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(tmp, target)
            files[e["path"]] = e["sha1"]
            save_json(STATE_PATH, state)

        if todo:
            self.download_all(todo, install)

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
    if getattr(sys, "frozen", False):
        try:  # left over from a self-update
            os.remove(sys.executable + ".old")
        except OSError:
            pass
    root = tk.Tk()
    app = Launcher(root)
    if "--autoplay" in sys.argv and app.name_var.get().strip():
        root.after(500, app.on_play)
    elif "--update" in sys.argv:
        root.after(500, lambda: app.start("update"))
    root.mainloop()


if __name__ == "__main__":
    main()
