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


APP_TITLE = "Wrought Flesh Patcher"
DEFAULT_MANIFEST_NAME = "manifest.json"
PATCHES_DIR_NAME = "patches"
INSTALL_MARKER_NAME = "wroughtflesh_patcher_install.json"
INSTALL_MARKER_SCHEMA = 1
DEFAULT_STEAM_APP_ID = 1762010
DEFAULT_STEAM_INSTALL_DIR = "Wrought Flesh"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS).resolve()
    return app_dir()


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


def safe_filename(value: str, fallback: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value.strip())
    cleaned = cleaned.strip("_")
    return cleaned or fallback


def load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    manifest["_manifest_path"] = str(manifest_path)
    manifest["_manifest_dir"] = str(manifest_path.parent)
    return manifest


def patch_id(manifest: dict) -> str:
    name = str(manifest.get("name", "")).strip()
    patched_hash = str(manifest.get("patched_sha256", "")).lower()
    target_file = str(manifest.get("target_file", "")).strip()
    return sha256_text(f"{name}\n{target_file}\n{patched_hash}")[:16]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().lower()


def default_manifest_path() -> Path | None:
    default_path = app_dir() / DEFAULT_MANIFEST_NAME
    if default_path.exists():
        return default_path

    return None


def find_xdelta() -> Path:
    exe_name = "xdelta3.exe" if os.name == "nt" else "xdelta3"
    for base_dir in (app_dir(), bundled_dir()):
        xdelta_path = base_dir / exe_name
        if xdelta_path.exists():
            return xdelta_path
    raise FileNotFoundError(f"Missing {exe_name}")


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
        self.geometry("780x600")
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
        self.create_mod_name = tk.StringVar(value="")
        self.create_steam_app_id = tk.StringVar(value="1762010")
        self.create_steam_install_dir = tk.StringVar(value="Wrought Flesh")
        self.create_target_file = tk.StringVar(value="WroughtFlesh.pck")
        self.create_original_file = tk.StringVar(value="")
        self.create_patched_file = tk.StringVar(value="")
        self.create_output_dir = tk.StringVar(value=str(app_dir() / PATCHES_DIR_NAME))
        self.create_patch_file = tk.StringVar(value="")

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
        self._try_auto_detect_default_game_folder()

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True)

        apply_tab = ttk.Frame(notebook, padding=12)
        create_tab = ttk.Frame(notebook, padding=12)
        notebook.add(apply_tab, text="Apply Patch")
        notebook.add(create_tab, text="Create Patch")

        self._build_apply_tab(apply_tab)
        self._build_create_tab(create_tab)

        ttk.Progressbar(root, variable=self.progress, maximum=100).pack(fill=tk.X, pady=(12, 8))
        ttk.Label(root, textvariable=self.status).pack(anchor="w")

        log_frame = ttk.LabelFrame(root, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.log = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _build_apply_tab(self, root):
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

    def _build_create_tab(self, root):
        ttk.Label(root, text="Create Patch", font=("Segoe UI", 15, "bold")).pack(anchor="w")

        form = ttk.Frame(root)
        form.pack(fill=tk.X, pady=(12, 0))

        self._add_entry_row(form, "Mod name:", self.create_mod_name, 0)
        self._add_entry_row(form, "Steam app ID:", self.create_steam_app_id, 1)
        self._add_entry_row(form, "Steam install dir:", self.create_steam_install_dir, 2)
        self._add_entry_row(form, "Target file:", self.create_target_file, 3)
        self._add_file_row(form, "Original file:", self.create_original_file, 4)
        self._add_file_row(form, "Modded file:", self.create_patched_file, 5)
        self._add_folder_row(form, "Output folder:", self.create_output_dir, 6)
        self._add_entry_row(form, "Patch filename:", self.create_patch_file, 7)

        button_row = ttk.Frame(root)
        button_row.pack(fill=tk.X, pady=(18, 0))
        ttk.Button(
            button_row,
            text="Use Game Backup As Original",
            command=self.use_game_backup_as_original,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(button_row, text="Create Patch", command=lambda: self.run_worker(self.create_patch)).pack(side=tk.LEFT)

    def _add_entry_row(self, root, label, variable, row):
        ttk.Label(root, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        root.columnconfigure(1, weight=1)

    def _add_file_row(self, root, label, variable, row):
        ttk.Label(root, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(root, text="Browse", command=lambda: self.browse_file(variable)).grid(row=row, column=2, pady=4)
        root.columnconfigure(1, weight=1)

    def _add_folder_row(self, root, label, variable, row):
        ttk.Label(root, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(root, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(root, text="Browse", command=lambda: self.browse_folder(variable)).grid(row=row, column=2, pady=4)
        root.columnconfigure(1, weight=1)

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
        app_id = int(self.manifest.get("steam_app_id") or DEFAULT_STEAM_APP_ID)
        install_dir_name = self.manifest.get("steam_install_dir") or DEFAULT_STEAM_INSTALL_DIR
        install_path = find_steam_install(app_id, install_dir_name)
        if install_path:
            self.install_dir.set(str(install_path))
            self.write_log(f"Detected Steam install: {install_path}")

    def _try_auto_detect_default_game_folder(self):
        if self.install_dir.get().strip():
            return
        install_path = find_steam_install(DEFAULT_STEAM_APP_ID, DEFAULT_STEAM_INSTALL_DIR)
        if install_path:
            self.install_dir.set(str(install_path))
            self.write_log(f"Detected Wrought Flesh Steam install: {install_path}")

    def browse(self):
        folder = filedialog.askdirectory(title="Select game folder")
        if folder:
            self.install_dir.set(folder)

    def browse_file(self, variable: tk.StringVar):
        path = filedialog.askopenfilename(
            title="Select file",
            filetypes=[
                ("Game and backup files", "*.pck *.bak"),
                ("Backup files", "*.bak"),
                ("PCK files", "*.pck"),
                ("All files", "*.*"),
            ],
        )
        if path:
            variable.set(path)

    def browse_folder(self, variable: tk.StringVar):
        folder = filedialog.askdirectory(title="Select folder")
        if folder:
            variable.set(folder)

    def create_tab_backup_original_path(self) -> Path:
        folder = self.game_folder()
        target_file = self.create_target_file.get().strip()
        if not target_file:
            raise ValueError("Target file is required.")

        backup_suffix = ".bak"
        marker = self.read_install_marker()
        if marker and marker.get("target_file") == target_file:
            backup_suffix = str(marker.get("backup_suffix") or backup_suffix)
        return folder / f"{target_file}{backup_suffix}"

    def use_game_backup_as_original(self):
        try:
            backup = self.create_tab_backup_original_path()
            if not backup.exists():
                raise FileNotFoundError(f"Could not find backup file: {backup}")
            self.create_original_file.set(str(backup))
            self.write_log(f"Using game backup as original file: {backup}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

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

    def install_marker_path(self) -> Path:
        return self.game_folder() / INSTALL_MARKER_NAME

    def read_install_marker(self) -> dict | None:
        marker_path = self.install_marker_path()
        if not marker_path.exists():
            return None
        with marker_path.open("r", encoding="utf-8") as file:
            marker = json.load(file)
        if not isinstance(marker, dict):
            raise ValueError(f"{INSTALL_MARKER_NAME} is not a valid patch marker.")
        return marker

    def write_install_marker(self):
        marker = {
            "schema": INSTALL_MARKER_SCHEMA,
            "patch_id": patch_id(self.manifest),
            "name": self.manifest.get("name", ""),
            "target_file": self.target_file,
            "original_sha256": self.manifest["original_sha256"].lower(),
            "patched_sha256": self.manifest["patched_sha256"].lower(),
            "backup_suffix": self.backup_suffix,
        }
        marker_path = self.install_marker_path()
        marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        self.write_log(f"Wrote install marker: {marker_path}")

    def remove_install_marker(self):
        marker_path = self.install_marker_path()
        if marker_path.exists():
            marker_path.unlink()
            self.write_log(f"Removed install marker: {marker_path}")

    def target_path(self) -> Path:
        path = self.game_folder() / self.target_file
        if not path.exists():
            raise FileNotFoundError(f"Could not find {self.target_file} in the selected folder.")
        return path

    def backup_path(self, backup_suffix: str | None = None) -> Path:
        target = self.target_path()
        suffix = self.backup_suffix if backup_suffix is None else backup_suffix
        return target.with_name(target.name + suffix)

    def restore_backup(self, expected_hash: str, backup_suffix: str | None = None):
        target = self.target_path()
        backup = self.backup_path(backup_suffix)
        if not backup.exists():
            raise FileNotFoundError("No backup file was found.")

        self.write_log("Restoring backup...")
        shutil.copy2(backup, target)

        restored_hash = sha256_file(target)
        if restored_hash != expected_hash.lower():
            raise ValueError("Backup restored, but its hash does not match the expected original.")

        self.remove_install_marker()
        self.write_log("Original file restored successfully.")

    def uninstall_active_patch_before_apply(self, target: Path, actual_hash: str, original_hash: str) -> str:
        marker = self.read_install_marker()
        if not marker:
            return actual_hash

        marker_target = marker.get("target_file")
        marker_hash = str(marker.get("patched_sha256", "")).lower()
        marker_name = marker.get("name") or "previous patch"
        marker_backup_suffix = str(marker.get("backup_suffix") or self.backup_suffix)

        if marker_target != self.target_file:
            self.write_log(f"Ignoring install marker for a different target: {marker_target}")
            return actual_hash

        if actual_hash == original_hash:
            self.write_log("Install marker found, but target is already original. Removing stale marker.")
            self.remove_install_marker()
            return actual_hash

        if actual_hash != marker_hash:
            raise ValueError(
                "An install marker exists, but the target file does not match the recorded patch. "
                "Uninstall manually or restore a clean game file before patching."
            )

        self.write_log(f"Detected installed patch: {marker_name}")
        self.set_progress(20, "Uninstalling previous patch...")
        self.restore_backup(original_hash, marker_backup_suffix)
        return sha256_file(target)

    def verify_only(self):
        self.require_manifest()
        target = self.target_path()
        self.write_log(f"Checking {target}")
        marker = self.read_install_marker()
        actual_hash = sha256_file(target)
        original_hash = self.manifest["original_sha256"].lower()
        patched_hash = self.manifest["patched_sha256"].lower()
        current_patch_id = patch_id(self.manifest)

        if actual_hash == original_hash:
            if marker:
                self.write_log("Install marker exists, but file is original.")
            self.set_progress(100, "Verified original file. Ready to patch.")
            self.write_log("File matches original hash.")
        elif actual_hash == patched_hash:
            if marker and marker.get("patch_id") == current_patch_id:
                self.set_progress(100, "Verified patched file. This patch is installed.")
            else:
                self.set_progress(100, "Verified patched file. Marker will be updated on apply.")
            self.write_log("File matches patched hash.")
        elif marker and marker.get("target_file") == self.target_file:
            marker_name = marker.get("name") or "previous patch"
            self.set_progress(100, f"Detected installed patch: {marker_name}")
            self.write_log(f"Install marker indicates active patch: {marker_name}")
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
            self.write_install_marker()
            return
        actual_hash = self.uninstall_active_patch_before_apply(target, actual_hash, original_hash)
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
        self.write_install_marker()
        self.write_log("Patch installed successfully.")

    def uninstall(self):
        self.require_manifest()
        original_hash = self.manifest["original_sha256"].lower()
        marker = self.read_install_marker()
        marker_backup_suffix = None
        if marker and marker.get("target_file") == self.target_file:
            marker_backup_suffix = str(marker.get("backup_suffix") or self.backup_suffix)
        self.restore_backup(original_hash, marker_backup_suffix)

        self.set_progress(100, "Uninstalled. Original file restored.")

    def create_patch(self):
        xdelta = find_xdelta()
        mod_name = self.create_mod_name.get().strip()
        if not mod_name:
            raise ValueError("Mod name is required.")

        patched = Path(self.create_patched_file.get()).expanduser()
        output_dir = Path(self.create_output_dir.get()).expanduser()
        target_file = self.create_target_file.get().strip()
        if not target_file:
            raise ValueError("Target file is required.")

        original_text = self.create_original_file.get().strip()
        if original_text:
            original = Path(original_text).expanduser()
        else:
            original = self.create_tab_backup_original_path()
            if original.exists():
                self.create_original_file.set(str(original))
                self.write_log(f"Using game backup as original file: {original}")

        if not original.exists():
            raise FileNotFoundError("Original file does not exist. Select a clean original file or use the game backup.")
        if not patched.exists():
            raise FileNotFoundError("Modded file does not exist.")
        if original.resolve() == patched.resolve():
            raise ValueError("Original file and modded file must be different.")

        output_parent_dir = output_dir
        patch_name = self.create_patch_file.get().strip()
        if not patch_name:
            patch_name = f"{safe_filename(mod_name, 'patch')}.xdelta"
        if not patch_name.lower().endswith(".xdelta"):
            patch_name += ".xdelta"

        patch_folder_name = safe_filename(Path(patch_name).stem, "patch")
        output_dir = output_parent_dir / patch_folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        patch_path = output_dir / patch_name
        manifest_path = output_dir / DEFAULT_MANIFEST_NAME

        self.write_log("Creating xdelta patch...")
        self.set_progress(20, "Creating xdelta patch...")
        result = subprocess.run(
            [str(xdelta), "-e", "-s", str(original), str(patched), str(patch_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "xdelta3 failed.")

        self.write_log("Hashing original and modded files...")
        self.set_progress(65, "Hashing files...")
        original_hash = sha256_file(original)
        patched_hash = sha256_file(patched)

        steam_app_id_text = self.create_steam_app_id.get().strip()
        steam_app_id = int(steam_app_id_text) if steam_app_id_text else 0
        manifest = {
            "name": mod_name,
            "steam_app_id": steam_app_id,
            "steam_install_dir": self.create_steam_install_dir.get().strip(),
            "target_file": target_file,
            "original_sha256": original_hash,
            "patched_sha256": patched_hash,
            "patch_file": patch_name,
            "backup_suffix": ".bak",
        }

        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.set_progress(100, "Patch created.")
        self.write_log(f"Created patch: {patch_path}")
        self.write_log(f"Created manifest: {manifest_path}")


if __name__ == "__main__":
    app = PatcherApp()
    app.mainloop()
