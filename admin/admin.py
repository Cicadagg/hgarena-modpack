"""HG Arena admin panel: one window for the routine work on the modpack.

Buttons:
  - Build the mod     gradle build of hgarena-source, the jar goes to the game folder and the local server
  - Test on this PC   starts the local server and the launcher with a local manifest (changed mods come from this PC)
  - Publish           runs publish.py: changed mods/configs go to GitHub, players get them via the launcher
  - Update everything build + publish

Runs on the developer PC only (needs Python, Java and the folders below). No passwords are stored here.
"""
import hashlib
import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from tkinter import filedialog, messagebox, scrolledtext, ttk
from urllib.parse import quote

ROOT = r"C:\Minecraft-1.20.1-Forge"
REPO = os.path.join(ROOT, "modpack-repo")
MOD_SRC = os.path.join(ROOT, "hgarena-source")
SERVER_DIR = os.path.join(ROOT, "server")
GAME_DIR = r"C:\Games\Minecraft"
MOD_JAR = "hgarena-1.0.0.jar"
FORGE_ARGS = "@libraries/net/minecraftforge/forge/1.20.1-47.4.28/win_args.txt"
LOCAL_PORT = 8765
SETTINGS = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "HGArenaAdmin", "settings.json")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no black console windows


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def find_launcher():
    """The newest HGArenaLauncher*.exe in Downloads, or the one publish.py built."""
    candidates = []
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    if os.path.isdir(downloads):
        candidates += [os.path.join(downloads, f) for f in os.listdir(downloads)
                       if f.lower().startswith("hgarenalauncher") and f.lower().endswith(".exe")]
    built = os.path.join(REPO, "build", "launcher", "dist", "HGArenaLauncher.exe")
    if os.path.isfile(built):
        candidates.append(built)
    return max(candidates, key=os.path.getmtime) if candidates else ""


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.log_queue = queue.Queue()
        self.busy = False
        self.server = None          # local Minecraft server process
        self.http = None            # local manifest server
        root.title("HG Arena: панель управления")
        root.geometry("900x620")
        root.minsize(700, 480)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        self.buttons = []

        def button(parent, text, cmd, col, row=0):
            b = ttk.Button(parent, text=text, command=cmd)
            b.grid(row=row, column=col, padx=4, pady=4, sticky="ew")
            self.buttons.append(b)
            return b

        main = ttk.LabelFrame(top, text="Модпак", padding=6)
        main.pack(fill="x")
        for i in range(4):
            main.columnconfigure(i, weight=1)
        button(main, "Обновить всё", self.update_all, 0)
        button(main, "Собрать мод", self.build_mod, 1)
        button(main, "Опубликовать моды", self.publish, 2)
        self.with_launcher = tk.BooleanVar(value=False)
        ttk.Checkbutton(main, text="вместе с новым лаунчером", variable=self.with_launcher).grid(row=0, column=3, sticky="w")

        test = ttk.LabelFrame(top, text="Тест на этом ПК", padding=6)
        test.pack(fill="x", pady=(8, 0))
        for i in range(4):
            test.columnconfigure(i, weight=1)
        button(test, "Запустить тест (сервер + игра)", self.start_test, 0)
        ttk.Button(test, text="Только сервер", command=self.start_server).grid(row=0, column=1, padx=4, sticky="ew")
        ttk.Button(test, text="Остановить сервер", command=self.stop_server).grid(row=0, column=2, padx=4, sticky="ew")
        ttk.Button(test, text="Лаунчер...", command=self.pick_launcher).grid(row=0, column=3, padx=4, sticky="ew")
        self.status = tk.StringVar()
        ttk.Label(test, textvariable=self.status, foreground="#555").grid(row=1, column=0, columnspan=4, sticky="w")

        cmd = ttk.Frame(root, padding=(8, 0))
        cmd.pack(fill="x")
        ttk.Label(cmd, text="Команда серверу:").pack(side="left")
        self.cmd = ttk.Entry(cmd)
        self.cmd.pack(side="left", fill="x", expand=True, padx=6)
        self.cmd.bind("<Return>", lambda e: self.send_command())
        ttk.Button(cmd, text="Отправить", command=self.send_command).pack(side="left")

        self.log = scrolledtext.ScrolledText(root, height=20, font=("Consolas", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.update_status()
        root.after(100, self.pump_log)

    # ---------- log ----------

    def write(self, text):
        self.log_queue.put(text if text.endswith("\n") else text + "\n")

    def pump_log(self):
        lines = []
        try:
            while len(lines) < 500:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log.configure(state="normal")
            self.log.insert("end", "".join(lines))
            if int(self.log.index("end-1c").split(".")[0]) > 5000:
                self.log.delete("1.0", "1000.0")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(100, self.pump_log)

    def update_status(self):
        srv = "сервер запущен (localhost:25654)" if self.server and self.server.poll() is None else "сервер выключен"
        launcher = self.settings.get("launcher") or find_launcher() or "не найден"
        self.status.set(f"{srv}   |   лаунчер: {launcher}")

    # ---------- running tasks ----------

    def task(self, title, steps):
        """Runs steps (functions returning True on success) one after another in a background thread."""
        if self.busy:
            messagebox.showinfo("Подождите", "Уже выполняется другая задача.")
            return
        self.busy = True
        for b in self.buttons:
            b.state(["disabled"])

        def work():
            ok = True
            self.write(f"===== {title} =====")
            for step in steps:
                try:
                    if not step():
                        ok = False
                        break
                except Exception as e:  # noqa: BLE001 - show any failure in the log
                    self.write(f"ОШИБКА: {e!r}")
                    ok = False
                    break
            self.write(f"===== {title}: {'готово' if ok else 'остановлено из-за ошибки'} =====\n")
            self.root.after(0, self.done)

        threading.Thread(target=work, daemon=True).start()

    def done(self):
        self.busy = False
        for b in self.buttons:
            b.state(["!disabled"])
        self.update_status()

    def run(self, args, cwd, env=None):
        self.write("> " + " ".join(args))
        p = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        for line in p.stdout:
            self.write(line.rstrip("\n"))
        p.wait()
        if p.returncode != 0:
            self.write(f"Код выхода {p.returncode}")
        return p.returncode == 0

    # ---------- steps ----------

    def step_build(self):
        ok = self.run([os.path.join(MOD_SRC, "gradlew.bat"), "build", "--offline", "-q",
                       "-Dnet.minecraftforge.gradle.check.certs=false"], MOD_SRC)
        if not ok:
            return False
        jar = os.path.join(MOD_SRC, "build", "libs", MOD_JAR)
        import shutil
        for d in (os.path.join(GAME_DIR, "mods"), os.path.join(SERVER_DIR, "mods")):
            try:
                shutil.copy2(jar, os.path.join(d, MOD_JAR))
                self.write(f"Мод скопирован в {d}")
            except OSError as e:
                self.write(f"Не удалось скопировать в {d}: {e} (сервер запущен? остановите его)")
                return False
        return True

    def step_publish(self):
        args = [self.python(), os.path.join(REPO, "publish.py")]
        if self.with_launcher.get():
            args.append("--launcher")
        return self.run(args, REPO)

    def python(self):
        return self.settings.get("python") or "python"

    # ---------- buttons ----------

    def build_mod(self):
        self.task("Сборка мода", [self.step_build])

    def publish(self):
        if not messagebox.askyesno("Публикация", "Выложить текущие моды и конфиги из C:\\Games\\Minecraft на GitHub?\n"
                                                 "Игроки получат их при следующем запуске лаунчера."):
            return
        self.task("Публикация", [self.step_publish])

    def update_all(self):
        if not messagebox.askyesno("Обновить всё", "Собрать мод и выложить моды и конфиги на GitHub?"):
            return
        self.task("Обновить всё", [self.step_build, self.step_publish])

    def pick_launcher(self):
        path = filedialog.askopenfilename(title="HGArenaLauncher.exe", filetypes=[("exe", "*.exe")],
                                          initialdir=os.path.dirname(self.settings.get("launcher") or find_launcher() or "C:\\"))
        if path:
            self.settings["launcher"] = path
            save_settings(self.settings)
            self.update_status()

    # ---------- local test ----------

    def start_server(self):
        if self.server and self.server.poll() is None:
            self.write("Сервер уже запущен.")
            return True
        self.write("Запуск локального сервера...")
        self.server = subprocess.Popen(["java", "@user_jvm_args.txt", FORGE_ARGS, "nogui"], cwd=SERVER_DIR,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)

        def reader(p):
            for line in p.stdout:
                if "/WARN] [mixin" in line or "/ERROR] [mixin" in line:
                    continue  # mod loading noise
                self.write(line.rstrip("\n"))
            self.write(f"Сервер остановлен (код {p.wait()}).")
            self.root.after(0, self.update_status)

        threading.Thread(target=reader, args=(self.server,), daemon=True).start()
        self.update_status()
        return True

    def stop_server(self):
        if self.server and self.server.poll() is None:
            self.send("stop")
        else:
            self.write("Сервер не запущен.")

    def send(self, line):
        if not (self.server and self.server.poll() is None):
            self.write("Сервер не запущен.")
            return
        self.write("[консоль] " + line)
        self.server.stdin.write(line + "\n")
        self.server.stdin.flush()

    def send_command(self):
        line = self.cmd.get().strip().lstrip("/")
        if line:
            self.send(line)
            self.cmd.delete(0, "end")

    def start_test(self):
        launcher = self.settings.get("launcher") or find_launcher()
        if not launcher or not os.path.isfile(launcher):
            messagebox.showwarning("Лаунчер", "Не найден HGArenaLauncher.exe. Укажите его кнопкой «Лаунчер...».")
            return
        self.task("Тест на этом ПК", [self.start_server, self.step_local_manifest, lambda: self.step_launch(launcher)])

    def step_local_manifest(self):
        """Published manifest, but every mod that differs from it is served from this PC."""
        manifest_path = os.path.join(REPO, "pack", "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        serve = os.path.join(ROOT, "playtest")
        os.makedirs(serve, exist_ok=True)
        local = {}
        mods_dir = os.path.join(GAME_DIR, "mods")
        published = {e["path"]: e for e in manifest["files"]}
        entries = [e for e in manifest["files"] if not e["path"].startswith("mods/")]
        changed = 0
        for fn in sorted(os.listdir(mods_dir)):
            if not fn.lower().endswith(".jar"):
                continue
            p = os.path.join(mods_dir, fn)
            s = sha1_file(p)
            old = published.get("mods/" + fn)
            if old and old["sha1"] == s:
                entries.append(old)
                continue
            changed += 1
            local[fn] = p
            entries.append({"path": "mods/" + fn, "sha1": s, "size": os.path.getsize(p),
                            "url": f"http://127.0.0.1:{LOCAL_PORT}/mods/{quote(fn)}"})
            self.write(f"Локальная версия: {fn}")
        manifest["files"] = entries
        manifest["server"] = "127.0.0.1:25654"  # the launcher joins the local server right away
        with open(os.path.join(serve, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        self.local_files = local
        self.serve_dir = serve
        self.start_http()
        self.write(f"Локальный манифест готов: изменённых модов {changed}.")
        return True

    def start_http(self):
        if self.http:
            return
        app = self

        class Handler(SimpleHTTPRequestHandler):
            def translate_path(self, path):
                path = path.split("?", 1)[0]
                if path.startswith("/mods/"):
                    from urllib.parse import unquote
                    return app.local_files.get(unquote(path[len("/mods/"):]), os.devnull + "_missing")
                return os.path.join(app.serve_dir, "manifest.json") if path == "/manifest.json" else os.devnull + "_missing"

            def log_message(self, *a):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def step_launch(self, launcher):
        env = dict(os.environ, HGARENA_MANIFEST=f"http://127.0.0.1:{LOCAL_PORT}/manifest.json")
        subprocess.Popen([launcher], cwd=os.path.dirname(launcher), env=env)
        self.write("Лаунчер запущен с локальным манифестом. В игре подключайтесь к localhost:25654.")
        return True

    # ---------- exit ----------

    def on_close(self):
        if self.server and self.server.poll() is None:
            if not messagebox.askyesno("Выход", "Локальный сервер запущен. Остановить его и выйти?"):
                return
            self.send("stop")
            try:
                self.server.wait(60)
            except subprocess.TimeoutExpired:
                self.server.kill()
        if self.http:
            self.http.shutdown()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
