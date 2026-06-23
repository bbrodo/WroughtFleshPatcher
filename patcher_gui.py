import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Mod Patcher"
DEFAULT_MANIFEST_NAME = "manifest.json"
PATCHES_DIR_NAME = "patches"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return base_dir / path


def load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    manifest["_manifest_path"] = str(manifest_path)
    manifest["_manifest_dir"] = str(manifest_path.parent)
    return manifest


def default_manifest_path() -> Path | None:
    default_path = app_dir() / DEFAULT_MANIFEST_NAME
    if default_path.exists():
        return default_path

    patches_dir = app_dir() / PATCHES_DIR_NAME
    if not patches_dir.exists():
        return None

    manifests = sorted(patches_dir.glob("*/manifest.json"))
    if len(manifests) == 1:
        return manifests[0]
    return None


def find_xdelta() -> Path:
    exe_name = "xdelta3.exe" if os.name == "nt" else "xdelta3"
    xdelta_path = app_dir() / exe_name
    if not xdelta_path.exists():
        raise FileNotFoundError(f"Missing {xdelta_path}")
    return xdelta_path


def parse_steam_libraryfolders(path: Path) -> list[Path]:
    libraries = []
    if not path.exists():
        return libraries

    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if '"path"' not in line:
            continue
        parts = [part for part in line.split('"') if part]
        if len(parts) < 3:
            continue
        library_path = Path(parts[-1].replace("\\\\", "\\"))
        if library_path.exists():
            libraries.append(library_path)
    return libraries


def get_steam_path() -> Path | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
            path = Path(value)
            return path if path.exists() else None
    except OSError:
        return None


def find_steam_install(app_id: int, install_dir_name: str | None) -> Path | None:
    steam_path = get_steam_path()
    if steam_path is None:
        return None

    libraries = [steam_path]
    libraries += parse_steam_libraryfolders(steam_path / "steamapps" / "libraryfolders.vdf")

    for library in libraries:
        manifest_path = library / "steamapps" / f"appmanifest_{app_id}.acf"
        if not manifest_path.exists():
            continue
        if install_dir_name:
            install_path = library / "steamapps" / "common" / install_dir_name
            if install_path.exists():
                return install_path
        text = manifest_path.read_text(encoding="utf-8", errors="ignore")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if '"installdir"' not in line:
                continue
            parts = [part for part in line.split('"') if part]
            if len(parts) >= 3:
                install_path = library / "steamapps" / "common" / parts[-1]
                if install_path.exists():
                    return install_path
    return None


class PatcherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("720x420")
        self.resizable(False, False)

        self.manifest = {}
        self.target_file = ""
        self.backup_suffix = ".bak"
        self.install_dir = tk.StringVar(value="")
        self.patch_manifest = tk.StringVar(value="")
        self.patch_name = tk.StringVar(value="No patch selected")
        self.target_label = tk.StringVar(value="Target file: -")
        self.status = tk.StringVar(value="Choose the game folder, then patch.")
        self.progress = tk.DoubleVar(value=0)

        self._build_ui()
        manifest_path = default_manifest_path()
        if manifest_path:
            try:
                self.load_patch_manifest(manifest_path)
            except Exception as exc:
                self.write_log(f"ERROR loading default patch: {exc}")
                self.set_progress(0, "Choose a patch manifest.")
        else:
            self.write_log("Choose a patch manifest to begin.")

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(root, textvariable=self.patch_name, font=("Segoe UI", 15, "bold"))
        title.pack(anchor="w")

        target = ttk.Label(root, textvariable=self.target_label)
        target.pack(anchor="w", pady=(6, 12))

        patch_row = ttk.Frame(root)
        patch_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(patch_row, text="Patch:").pack(side=tk.LEFT)
        patch_entry = ttk.Entry(patch_row, textvariable=self.patch_manifest)
        patch_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(patch_row, text="Select Patch", command=self.browse_patch).pack(side=tk.LEFT)

        row = ttk.Frame(root)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Game folder:").pack(side=tk.LEFT)
        entry = ttk.Entry(row, textvariable=self.install_dir)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(row, text="Browse", command=self.browse).pack(side=tk.LEFT)

        button_row = ttk.Frame(root)
        button_row.pack(fill=tk.X, pady=(18, 8))
        ttk.Button(button_row, text="Verify", command=lambda: self.run_worker(self.verify_only)).pack(side=tk.LEFT)
        ttk.Button(button_row, text="Apply Patch", command=lambda: self.run_worker(self.apply_patch)).pack(side=tk.LEFT, padx=8)
        ttk.Button(button_row, text="Uninstall Patch", command=lambda: self.run_worker(self.uninstall)).pack(side=tk.LEFT)

        ttk.Progressbar(root, variable=self.progress, maximum=100).pack(fill=tk.X, pady=(16, 8))
        ttk.Label(root, textvariable=self.status).pack(anchor="w")

        log_frame = ttk.LabelFrame(root, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(16, 0))
        self.log = tk.Text(log_frame, height=9, wrap=tk.WORD, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def browse_patch(self):
        initial_dir = app_dir() / PATCHES_DIR_NAME
        path = filedialog.askopenfilename(
            title="Select patch manifest",
            initialdir=str(initial_dir if initial_dir.exists() else app_dir()),
            filetypes=[("Patch manifest", "manifest.json"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            try:
                self.load_patch_manifest(Path(path))
            except Exception as exc:
                messagebox.showerror(APP_TITLE, str(exc))

    def load_patch_manifest(self, manifest_path: Path):
        manifest = load_manifest(manifest_path)
        required_keys = ["target_file", "original_sha256", "patched_sha256", "patch_file"]
        missing = [key for key in required_keys if key not in manifest]
        if missing:
            raise ValueError(f"Patch manifest is missing required keys: {', '.join(missing)}")

        self.manifest = manifest
        self.target_file = manifest["target_file"]
        self.backup_suffix = manifest.get("backup_suffix", ".bak")
        self.patch_manifest.set(str(manifest_path))
        self.patch_name.set(manifest.get("name", manifest_path.parent.name))
        self.target_label.set(f"Target file: {self.target_file}")
        self.write_log(f"Loaded patch manifest: {manifest_path}")
        self._try_auto_detect()
        self.set_progress(0, "Patch selected. Choose the game folder, then patch.")

    def require_manifest(self):
        if not self.manifest:
            raise ValueError("No patch manifest selected.")

    def _try_auto_detect(self):
        if not self.manifest:
            return
        app_id = self.manifest.get("steam_app_id")
        if not app_id:
            return
        install_path = find_steam_install(int(app_id), self.manifest.get("steam_install_dir"))
        if install_path:
            self.install_dir.set(str(install_path))
            self.write_log(f"Detected Steam install: {install_path}")

    def browse(self):
        folder = filedialog.askdirectory(title="Select game folder")
        if folder:
            self.install_dir.set(folder)

    def run_worker(self, func):
        thread = threading.Thread(target=self._worker_wrapper, args=(func,), daemon=True)
        thread.start()

    def _worker_wrapper(self, func):
        try:
            self.set_progress(0, "Working...")
            func()
        except Exception as exc:
            self.set_progress(0, f"Error: {exc}")
            self.write_log(f"ERROR: {exc}")
            messagebox.showerror(APP_TITLE, str(exc))

    def write_log(self, text: str):
        def inner():
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, text + "\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        self.after(0, inner)

    def set_progress(self, value: float, text: str):
        self.after(0, lambda: self.progress.set(value))
        self.after(0, lambda: self.status.set(text))

    def game_folder(self) -> Path:
        folder = Path(self.install_dir.get()).expanduser()
        if not folder.exists():
            raise FileNotFoundError("Game folder does not exist.")
        return folder

    def target_path(self) -> Path:
        path = self.game_folder() / self.target_file
        if not path.exists():
            raise FileNotFoundError(f"Could not find {self.target_file} in the selected folder.")
        return path

    def backup_path(self) -> Path:
        target = self.target_path()
        return target.with_name(target.name + self.backup_suffix)

    def verify_only(self):
        self.require_manifest()
        target = self.target_path()
        self.write_log(f"Checking {target}")
        actual_hash = sha256_file(target)
        original_hash = self.manifest["original_sha256"].lower()
        patched_hash = self.manifest["patched_sha256"].lower()

        if actual_hash == original_hash:
            self.set_progress(100, "Verified original file. Ready to patch.")
            self.write_log("File matches original hash.")
        elif actual_hash == patched_hash:
            self.set_progress(100, "Verified patched file. Mod is already installed.")
            self.write_log("File matches patched hash.")
        else:
            raise ValueError(
                "File hash does not match the supported original or patched file. "
                "The game version may be different."
            )

    def apply_patch(self):
        self.require_manifest()
        xdelta = find_xdelta()
        target = self.target_path()
        backup = self.backup_path()
        manifest_dir = Path(self.manifest["_manifest_dir"])
        patch_path = resolve_path(manifest_dir, self.manifest["patch_file"])
        if not patch_path.exists():
            raise FileNotFoundError(f"Missing patch file: {patch_path}")

        self.write_log("Verifying original file...")
        actual_hash = sha256_file(target)
        original_hash = self.manifest["original_sha256"].lower()
        patched_hash = self.manifest["patched_sha256"].lower()

        if actual_hash == patched_hash:
            self.set_progress(100, "Mod is already installed.")
            self.write_log("Target already matches patched hash.")
            return
        if actual_hash != original_hash:
            raise ValueError("Target file is not the supported original version.")

        if not backup.exists():
            self.write_log(f"Creating backup: {backup}")
            shutil.copy2(target, backup)
        else:
            self.write_log(f"Backup already exists: {backup}")

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / target.name
            self.set_progress(35, "Applying xdelta patch...")
            self.write_log("Running xdelta3...")
            result = subprocess.run(
                [str(xdelta), "-d", "-s", str(target), str(patch_path), str(output)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "xdelta3 failed.")

            self.set_progress(70, "Verifying patched file...")
            output_hash = sha256_file(output)
            if output_hash != patched_hash:
                raise ValueError("Patched output hash did not match expected hash.")

            self.write_log("Replacing target file.")
            shutil.copy2(output, target)

        self.set_progress(100, "Patch installed.")
        self.write_log("Patch installed successfully.")

    def uninstall(self):
        self.require_manifest()
        target = self.target_path()
        backup = self.backup_path()
        if not backup.exists():
            raise FileNotFoundError("No backup file was found.")

        self.write_log("Restoring backup...")
        shutil.copy2(backup, target)

        restored_hash = sha256_file(target)
        original_hash = self.manifest["original_sha256"].lower()
        if restored_hash != original_hash:
            raise ValueError("Backup restored, but its hash does not match the expected original.")

        self.set_progress(100, "Uninstalled. Original file restored.")
        self.write_log("Original file restored successfully.")


if __name__ == "__main__":
    app = PatcherApp()
    app.mainloop()
