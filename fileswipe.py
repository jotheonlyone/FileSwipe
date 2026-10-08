#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
  FileSwipe - Moderner Dateien-Ausmister mit integrierter Bildvorschau
  Eine vollständige Windows-Desktop-Anwendung mit Tkinter & Pillow.
  
  Packen als eigenständige .exe mit PyInstaller:
    pip install pillow pyinstaller
    pyinstaller --onefile --windowed --name="FileSwipe" fileswipe.py
=============================================================================
"""

import os
import sys
import stat
import time
import ctypes
import tempfile
import base64
import threading
import subprocess
from pathlib import Path
from datetime import datetime

# ===========================================================================
# 1. Windows AppUserModelID GANZ OBEN VOR DEM FENSTERSTART setzen!
#    Sorgt dafür, dass Windows das Fenster und die Taskleiste mit der eigenen
#    App-ID und dem eigenen Icon statt der Standard-Tkinter-Feder anzeigt!
# ===========================================================================
if sys.platform.startswith("win"):
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('mycompany.fileswipe.app.1.0')
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import ttk, filedialog

# Optionale Pillow-Unterstützung für erweiterte Bildformate (JPG, WEBP etc.)
try:
    from PIL import Image, ImageTk, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ---------------------------------------------------------------------------
# Konstanten & Windows-Shell-API für echten Papierkorb (ohne externe Libs!)
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform.startswith("win")

# Externe Ordner, die zwingend ignoriert werden (System- & App-Schutz)
DEFAULT_EXCLUDED_DIRS = {
    "appdata", "windows", "system32", "syswow64", "$recycle.bin",
    "system volume information", "recovery", "program files",
    "program files (x86)", "programdata", "local settings",
    "application data", ".git", ".vscode", "node_modules", "site-packages", "temp"
}

# Dateitypen-Kategorien
EXT_CATEGORIES = {
    "Bilder": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".svg", ".heic", ".ico", ".raw", ".cr2", ".cr3"},
    "Videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpeg"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
    "Dokumente": {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".csv", ".rtf", ".odt", ".epub", ".log", ".json", ".xml", ".md", ".py", ".html"},
    "Archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".iso"},
    "Apps": {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".lnk", ".appref-ms"}
}

APP_EXTENSIONS = EXT_CATEGORIES["Apps"]
TEXT_PREVIEW_EXTS = {".txt", ".log", ".json", ".xml", ".csv", ".py", ".md", ".html", ".css", ".js", ".ini", ".cfg", ".bat", ".cmd"}
IMAGE_EXTS = EXT_CATEGORIES["Bilder"]

def format_size(size_bytes: int) -> str:
    """Formatiert Byte-Zahlen in lesbare Einheiten (KB, MB, GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def is_hidden_or_system(filepath: str) -> bool:
    """
    Prüft, ob eine Datei versteckt oder eine Systemdatei ist.
    Funktioniert nativ unter Windows über GetFileAttributesW und Unix via Prefix '.'.
    """
    basename = os.path.basename(filepath)
    if basename.startswith(".") or basename.startswith("~$"):
        return True
    
    if IS_WINDOWS:
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(filepath))
            if attrs != -1:
                FILE_ATTRIBUTE_HIDDEN = 0x02
                FILE_ATTRIBUTE_SYSTEM = 0x04
                if (attrs & FILE_ATTRIBUTE_HIDDEN) or (attrs & FILE_ATTRIBUTE_SYSTEM):
                    return True
        except Exception:
            pass
    return False

def move_to_recycle_bin(filepath: str) -> bool:
    """
    Verschiebt eine Datei sicher in den echten Windows-Papierkorb.
    1. Native Windows Shell-API (SHFileOperationW mit FOF_ALLOWUNDO)
    2. PowerShell-Fallback (Microsoft.VisualBasic FileSystem SendToRecycleBin)
    """
    if not os.path.exists(filepath):
        return False
    
    abs_path = os.path.abspath(filepath)
    
    if IS_WINDOWS:
        try:
            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", ctypes.c_void_p),
                    ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", ctypes.c_bool),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p),
                ]
            
            FO_DELETE = 0x0003
            FOF_ALLOWUNDO = 0x0040      # In den Windows-Papierkorb!
            FOF_NOCONFIRMATION = 0x0010
            FOF_SILENT = 0x0004
            
            p_from = abs_path + "\0\0"
            
            fileop = SHFILEOPSTRUCTW()
            fileop.hwnd = None
            fileop.wFunc = FO_DELETE
            fileop.pFrom = p_from
            fileop.pTo = None
            fileop.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
            fileop.fAnyOperationsAborted = False
            fileop.hNameMappings = None
            fileop.lpszProgressTitle = None
            
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(fileop))
            if result == 0 and not fileop.fAnyOperationsAborted and not os.path.exists(abs_path):
                return True
        except Exception:
            pass
            
        try:
            safe_p = abs_path.replace("'", "''")
            cmd = (
                f'Add-Type -AssemblyName Microsoft.VisualBasic; '
                f'[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(\'{safe_p}\', \'OnlyErrorDialogs\', \'SendToRecycleBin\')'
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                creationflags=0x08000000,
                capture_output=True,
                timeout=8
            )
            if res.returncode == 0 and not os.path.exists(abs_path):
                return True
        except Exception:
            pass

    try:
        os.remove(abs_path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------
class FileItem:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.filename = os.path.basename(path)
        self.ext = os.path.splitext(self.filename)[1].lower()
        self.directory = os.path.dirname(self.path)
        self.size = 0
        self.mtime = 0
        self.category = "Dokumente"
        self.is_system = False
        self.is_app = self.ext in APP_EXTENSIONS
        
        try:
            st = os.stat(self.path)
            self.size = st.st_size
            self.mtime = st.st_mtime
        except Exception:
            pass
            
        self.is_system = is_hidden_or_system(self.path)
        
        for cat_name, extensions in EXT_CATEGORIES.items():
            if self.ext in extensions:
                self.category = cat_name
                break

    @property
    def formatted_size(self) -> str:
        return format_size(self.size)

    @property
    def formatted_date(self) -> str:
        try:
            return datetime.fromtimestamp(self.mtime).strftime("%d.%m.%Y %H:%M")
        except Exception:
            return "Unbekannt"

    @property
    def icon_symbol(self) -> str:
        icons = {
            "Bilder": "🖼️",
            "Videos": "🎬",
            "Audio": "🎵",
            "Dokumente": "📄",
            "Archive": "📦",
            "Apps": "⚙️",
        }
        return icons.get(self.category, "📁")


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------
class AppSettings:
    def __init__(self):
        self.include_system_files = False
        self.include_apps = False
        self.min_size_mb = 0.0
        self.category_filter = "Alle"
        self.sort_order = "Zufällig"
        self.selected_folders = []


# ---------------------------------------------------------------------------
# Hauptfenster FileSwipe
# ---------------------------------------------------------------------------
class FileSwipeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FileSwipe - Ausmisten leicht gemacht 🧹")
        self.geometry("820x740")
        self.minsize(720, 640)
        
        # State
        self.settings = AppSettings()
        self.files_pool = []
        self.current_index = 0
        self.marked_for_deletion = []
        self.kept_files = []
        self.history = []
        self.is_scanning = False
        self.current_folder_name = ""
        self.current_img_obj = None
        
        # Windows-Icon & Taskleisten-Icon einbinden (ohne externe Datei)
        self.setup_app_icons()

        # Drag / Swipe State
        self.drag_start_x = 0
        self.is_dragging = False
        
        # Design & Farben
        self.configure_styles()
        
        # UI Aufbau
        self.build_ui()
        
        # Tastatur-Shortcuts
        self.bind("<Left>", lambda e: self.trigger_swipe_left())
        self.bind("<Right>", lambda e: self.trigger_swipe_right())
        self.bind("<Up>", lambda e: self.on_undo())
        self.bind("<BackSpace>", lambda e: self.on_undo())
        self.bind("<space>", lambda e: self.trigger_swipe_right())
        self.bind("<Control-r>", lambda e: self.open_review_dialog())
        self.bind("<Control-s>", lambda e: self.open_settings_dialog())
        
        # Startet im Startbildschirm (kein automatischer Scan)
        self.show_welcome_view()

    def setup_app_icons(self):
        """
        Lädt das Icon 'app_icon.ico' für Titelleiste und Windows-Taskleiste.
        Funktioniert direkt im Entwicklungsmodus sowie als gepackte Onefile-EXE.
        """
        self.app_ico_path = None
        self.app_photo_icon = None

        # Pfad zur Icon-Datei ermitteln (funktioniert auch als Onefile-EXE):
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        icon_path = os.path.join(base_path, 'app_icon.ico')

        # Fallback falls im aktuellen Arbeitsverzeichnis
        if not os.path.exists(icon_path):
            candidate = os.path.abspath('app_icon.ico')
            if os.path.exists(candidate):
                icon_path = candidate

        if os.path.exists(icon_path):
            self.app_ico_path = icon_path
            try:
                self.iconbitmap(default=icon_path)
            except Exception:
                pass
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

            # Für Windows Taskleiste und Alt-Tab (Win32 API)
            if IS_WINDOWS:
                try:
                    self.update_idletasks()
                    hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
                    IMAGE_ICON = 1
                    LR_LOADFROMFILE = 0x00000010
                    LR_DEFAULTSIZE = 0x00000040
                    WM_SETICON = 0x0080
                    hicon_big = ctypes.windll.user32.LoadImageW(
                        None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
                    )
                    hicon_small = ctypes.windll.user32.LoadImageW(
                        None, icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
                    )
                    if hicon_small:
                        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_small)
                    if hicon_big:
                        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_big)
                except Exception:
                    pass

    def apply_window_icon(self, window):
        """Setzt das App-Icon verlässlich auf jedes Unterfenster/Dialog."""
        if self.app_ico_path and os.path.exists(self.app_ico_path):
            try:
                window.iconbitmap(default=self.app_ico_path)
            except Exception:
                pass
            try:
                window.iconbitmap(self.app_ico_path)
            except Exception:
                pass
            if IS_WINDOWS:
                try:
                    window.update_idletasks()
                    hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
                    IMAGE_ICON = 1
                    LR_LOADFROMFILE = 0x00000010
                    LR_DEFAULTSIZE = 0x00000040
                    WM_SETICON = 0x0080
                    hicon_big = ctypes.windll.user32.LoadImageW(
                        None, self.app_ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
                    )
                    hicon_small = ctypes.windll.user32.LoadImageW(
                        None, self.app_ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
                    )
                    if hicon_small:
                        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_small)
                    if hicon_big:
                        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_big)
                except Exception:
                    pass

    def configure_styles(self):
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
            
        self.bg_color = "#18181b"       # Dunkles Zinc
        self.card_bg = "#27272a"        # Card Zinc
        self.card_border = "#3f3f46"    # Umrandung
        self.text_primary = "#f4f4f5"   # Weißer Text
        self.text_secondary = "#a1a1aa" # Grauer Text
        self.accent_red = "#ef4444"     # Löschen
        self.accent_green = "#22c55e"   # Behalten
        self.accent_blue = "#3b82f6"
        
        self.configure(bg=self.bg_color)
        
        self.style.configure(".", background=self.bg_color, foreground=self.text_primary, font=("Segoe UI", 10))
        self.style.configure("TProgressbar", thickness=10, background=self.accent_blue, troughcolor="#27272a")

        # Dropdowns (Combobox)
        self.option_add('*TCombobox*Listbox.background', '#27272a')
        self.option_add('*TCombobox*Listbox.foreground', '#f4f4f5')
        self.option_add('*TCombobox*Listbox.selectBackground', '#3b82f6')
        self.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
        self.option_add('*TCombobox*Listbox.font', ('Segoe UI', 10))
        
        self.style.configure('TCombobox', 
            fieldbackground='#27272a',
            background='#323238',
            foreground='#ffffff',
            arrowcolor='#38bdf8',
            padding=4
        )
        self.style.map('TCombobox',
            fieldbackground=[('readonly', '#27272a')],
            selectbackground=[('readonly', '#27272a')],
            selectforeground=[('readonly', '#ffffff')],
            foreground=[('readonly', '#ffffff')]
        )

        # Tabellen (Treeview)
        self.style.configure("Treeview",
            background="#27272a",
            foreground="#f4f4f5",
            fieldbackground="#27272a",
            rowheight=28,
            font=("Segoe UI", 10)
        )
        self.style.configure("Treeview.Heading",
            background="#1f1f23",
            foreground="#38bdf8",
            relief="flat",
            font=("Segoe UI", 9, "bold")
        )
        self.style.map("Treeview",
            background=[('selected', '#3b82f6')],
            foreground=[('selected', '#ffffff')]
        )

    def build_ui(self):
        # 1. Kopfzeile
        header_frame = tk.Frame(self, bg=self.bg_color, padx=20, pady=12)
        header_frame.pack(fill="x")
        
        title_box = tk.Frame(header_frame, bg=self.bg_color)
        title_box.pack(side="left")
        
        lbl_app = tk.Label(title_box, text="FileSwipe", font=("Segoe UI", 16, "bold"), fg="#ffffff", bg=self.bg_color)
        lbl_app.pack(side="left")
        self.lbl_subtitle = tk.Label(title_box, text=" • Bereit zum Start", font=("Segoe UI", 10), fg=self.text_secondary, bg=self.bg_color)
        self.lbl_subtitle.pack(side="left", padx=6, pady=(3, 0))
        
        btn_box = tk.Frame(header_frame, bg=self.bg_color)
        btn_box.pack(side="right")
        
        self.btn_review = tk.Button(
            btn_box, text="🗑️ Papierkorb-Prüfung (0)", font=("Segoe UI", 9, "bold"),
            bg="#27272a", fg="#fb7185", activebackground="#3f3f46", activeforeground="#ffffff",
            relief="flat", padx=12, pady=5, cursor="hand2", command=self.open_review_dialog
        )
        self.btn_review.pack(side="left", padx=4)
        
        self.btn_change_folder = tk.Button(
            btn_box, text="📂 Ordner wählen", font=("Segoe UI", 9),
            bg="#27272a", fg=self.text_primary, activebackground="#3f3f46", activeforeground="#ffffff",
            relief="flat", padx=10, pady=5, cursor="hand2", command=self.choose_custom_folder
        )
        self.btn_change_folder.pack(side="left", padx=4)
        
        btn_settings = tk.Button(
            btn_box, text="⚙️ Einstellungen", font=("Segoe UI", 9),
            bg="#27272a", fg=self.text_primary, activebackground="#3f3f46", activeforeground="#ffffff",
            relief="flat", padx=10, pady=5, cursor="hand2", command=self.open_settings_dialog
        )
        btn_settings.pack(side="left", padx=4)

        # 2. Zentraler Bereich: Start-Ansicht ODER Karte ODER Abschluss
        self.main_container = tk.Frame(self, bg=self.bg_color, padx=24, pady=8)
        self.main_container.pack(fill="both", expand=True)

        # 3. Aktionsbereich (Buttons: Löschen / Undo / Behalten)
        self.action_bar = tk.Frame(self, bg=self.bg_color, padx=24, pady=8)
        self.action_bar.pack(fill="x")
        
        self.btn_delete = tk.Button(
            self.action_bar, text="🗑️  LÖSCHEN  (←)", font=("Segoe UI", 12, "bold"),
            bg="#dc2626", fg="#ffffff", activebackground="#b91c1c", activeforeground="#ffffff",
            relief="flat", padx=24, pady=10, cursor="hand2", command=self.trigger_swipe_left
        )
        self.btn_delete.pack(side="left", expand=True, fill="x", padx=(0, 6))
        
        self.btn_undo = tk.Button(
            self.action_bar, text="↩ Rückgängig (↑)", font=("Segoe UI", 10),
            bg="#27272a", fg=self.text_secondary, activebackground="#3f3f46", activeforeground="#ffffff",
            relief="flat", padx=14, pady=10, cursor="hand2", command=self.on_undo
        )
        self.btn_undo.pack(side="left", padx=4)
        
        self.btn_keep = tk.Button(
            self.action_bar, text="✨  BEHALTEN  (→)", font=("Segoe UI", 12, "bold"),
            bg="#16a34a", fg="#ffffff", activebackground="#15803d", activeforeground="#ffffff",
            relief="flat", padx=24, pady=10, cursor="hand2", command=self.trigger_swipe_right
        )
        self.btn_keep.pack(side="right", expand=True, fill="x", padx=(6, 0))

        # 4. Fußzeile: Fortschrittsbalken und Statusleiste
        footer_frame = tk.Frame(self, bg=self.bg_color, padx=24, pady=10)
        footer_frame.pack(fill="x", side="bottom")
        
        self.progressbar = ttk.Progressbar(footer_frame, orient="horizontal", mode="determinate", style="TProgressbar")
        self.progressbar.pack(fill="x", pady=(0, 6))
        
        footer_info = tk.Frame(footer_frame, bg=self.bg_color)
        footer_info.pack(fill="x")
        
        self.lbl_progress_text = tk.Label(footer_info, text="Wähle einen Ordner aus...", font=("Segoe UI", 9), fg=self.text_secondary, bg=self.bg_color)
        self.lbl_progress_text.pack(side="left")
        
        self.lbl_stats = tk.Label(footer_info, text="Vorgemerkt: 0 B", font=("Segoe UI", 9, "bold"), fg="#f43f5e", bg=self.bg_color)
        self.lbl_stats.pack(side="right")

    # -----------------------------------------------------------------------
    # Status- & Zähler-Aktualisierung
    # -----------------------------------------------------------------------
    def update_stats_and_counters(self):
        count = len(self.marked_for_deletion)
        total_bytes = sum(f.size for f in self.marked_for_deletion)
        self.btn_review.config(text=f"🗑️ Papierkorb-Prüfung ({count})")
        self.lbl_stats.config(text=f"Vorgemerkt: {count} ({format_size(total_bytes)})")

    # -----------------------------------------------------------------------
    # Startbildschirm (Ordner wählen) mit App-Icon
    # -----------------------------------------------------------------------
    def show_welcome_view(self):
        for widget in self.main_container.winfo_children():
            widget.destroy()
            
        self.btn_delete.config(state="disabled")
        self.btn_undo.config(state="disabled")
        self.btn_keep.config(state="disabled")
        self.lbl_subtitle.config(text=" • Kein Ordner geöffnet")
        self.lbl_progress_text.config(text="Wähle einen Ordner aus, um zu starten")
        self.progressbar.config(value=0)
        self.update_stats_and_counters()

        welcome_card = tk.Frame(
            self.main_container, bg=self.card_bg, highlightbackground=self.card_border, highlightthickness=1,
            cursor="hand2"
        )
        welcome_card.pack(fill="both", expand=True)
        welcome_card.bind("<Button-1>", lambda e: self.choose_custom_folder())

        center_box = tk.Frame(welcome_card, bg=self.card_bg)
        center_box.place(relx=0.5, rely=0.45, anchor="center")
        center_box.bind("<Button-1>", lambda e: self.choose_custom_folder())

        if self.app_photo_icon:
            lbl_big_icon = tk.Label(center_box, image=self.app_photo_icon, bg=self.card_bg)
        else:
            lbl_big_icon = tk.Label(center_box, text="📁", font=("Segoe UI Emoji", 56), fg="#38bdf8", bg=self.card_bg)
        lbl_big_icon.pack(pady=(0, 10))
        lbl_big_icon.bind("<Button-1>", lambda e: self.choose_custom_folder())

        lbl_prompt = tk.Label(
            center_box, text="Hier klicken, um einen Ordner zum Ausmisten auszuwählen",
            font=("Segoe UI", 15, "bold"), fg="#ffffff", bg=self.card_bg, justify="center"
        )
        lbl_prompt.pack(pady=(0, 6))
        lbl_prompt.bind("<Button-1>", lambda e: self.choose_custom_folder())

        lbl_hint = tk.Label(
            center_box, text="Öffnet den Explorer. Systemordner & Apps bleiben automatisch sicher geschützt.",
            font=("Segoe UI", 10), fg=self.text_secondary, bg=self.card_bg
        )
        lbl_hint.pack(pady=(0, 20))
        lbl_hint.bind("<Button-1>", lambda e: self.choose_custom_folder())

        btn_quick_box = tk.Frame(center_box, bg=self.card_bg)
        btn_quick_box.pack()

        user_home = Path.home()
        quick_targets = [
            ("📥 Downloads", user_home / "Downloads"),
            ("📄 Dokumente", user_home / "Documents"),
            ("🖼️ Bilder", user_home / "Pictures"),
            ("🖥️ Desktop", user_home / "Desktop"),
        ]

        for title, p in quick_targets:
            if p.exists():
                b = tk.Button(
                    btn_quick_box, text=title, font=("Segoe UI", 9),
                    bg="#323238", fg="#ffffff", activebackground="#3f3f46", activeforeground="#ffffff",
                    relief="flat", padx=10, pady=6, cursor="hand2",
                    command=lambda folder_path=str(p): self.start_scan_for_folder(folder_path)
                )
                b.pack(side="left", padx=4)

        b_all = tk.Button(
            btn_quick_box, text="🔍 Anderer Ordner...", font=("Segoe UI", 9, "bold"),
            bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8", activeforeground="#ffffff",
            relief="flat", padx=12, pady=6, cursor="hand2", command=self.choose_custom_folder
        )
        b_all.pack(side="left", padx=4)

    def choose_custom_folder(self):
        picked = filedialog.askdirectory(title="Ordner zum Ausmisten auswählen")
        if picked:
            self.start_scan_for_folder(picked)

    def start_scan_for_folder(self, folder_path: str):
        self.settings.selected_folders = [folder_path]
        self.current_folder_name = os.path.basename(folder_path) or folder_path
        self.lbl_subtitle.config(text=f" • Ordner: {self.current_folder_name}")
        self.start_scan()

    # -----------------------------------------------------------------------
    # Scan-Vorgang im Hintergrund
    # -----------------------------------------------------------------------
    def start_scan(self):
        if self.is_scanning:
            return
            
        self.is_scanning = True
        self.files_pool.clear()
        self.current_index = 0
        self.history.clear()
        self.lbl_progress_text.config(text="Scanne Verzeichnis nach Dateien...")
        self.btn_delete.config(state="disabled")
        self.btn_undo.config(state="disabled")
        self.btn_keep.config(state="disabled")
        
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        scanned = []
        folders = self.settings.selected_folders
        min_bytes = int(self.settings.min_size_mb * 1024 * 1024)
        cat_filter = self.settings.category_filter
        include_sys = self.settings.include_system_files
        include_apps = self.settings.include_apps
        
        for root_dir in folders:
            if not os.path.exists(root_dir):
                continue
                
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [
                    d for d in dirs 
                    if d.lower() not in DEFAULT_EXCLUDED_DIRS 
                    and (include_sys or not d.startswith("."))
                ]
                
                parts = [p.lower() for p in Path(root).parts]
                if any(p in DEFAULT_EXCLUDED_DIRS for p in parts):
                    continue
                    
                for file in files:
                    if not include_sys:
                        if file.startswith(".") or file.startswith("~$"):
                            continue
                            
                    full_path = os.path.join(root, file)
                    
                    if not include_sys and is_hidden_or_system(full_path):
                        continue
                        
                    try:
                        item = FileItem(full_path)
                        
                        if not include_apps and item.is_app:
                            continue
                            
                        if item.size < min_bytes:
                            continue
                            
                        if cat_filter != "Alle" and item.category != cat_filter:
                            continue
                            
                        scanned.append(item)
                    except Exception:
                        continue
                        
        if self.settings.sort_order == "Größte zuerst":
            scanned.sort(key=lambda x: x.size, reverse=True)
        elif self.settings.sort_order == "Neueste zuerst":
            scanned.sort(key=lambda x: x.mtime, reverse=True)
        elif self.settings.sort_order == "Älteste zuerst":
            scanned.sort(key=lambda x: x.mtime)
        elif self.settings.sort_order == "Zufällig":
            import random
            random.shuffle(scanned)
            
        self.after(0, self._scan_completed, scanned)

    def _scan_completed(self, items):
        self.is_scanning = False
        self.files_pool = items
        self.current_index = 0
        
        if not self.files_pool:
            self.show_no_files_view()
            return
            
        self.btn_delete.config(state="normal")
        self.btn_undo.config(state="normal")
        self.btn_keep.config(state="normal")
        
        self.build_card_view()
        self.progressbar.config(maximum=len(self.files_pool), value=0)
        self.update_current_card()
        self.update_stats_and_counters()

    def show_no_files_view(self):
        for widget in self.main_container.winfo_children():
            widget.destroy()
            
        box = tk.Frame(self.main_container, bg=self.card_bg, highlightbackground=self.card_border, highlightthickness=1)
        box.pack(fill="both", expand=True)
        
        c = tk.Frame(box, bg=self.card_bg)
        c.place(relx=0.5, rely=0.45, anchor="center")
        
        tk.Label(c, text="🔍", font=("Segoe UI Emoji", 48), bg=self.card_bg).pack(pady=6)
        tk.Label(c, text="Keine passenden Dateien gefunden", font=("Segoe UI", 14, "bold"), fg="#ffffff", bg=self.card_bg).pack(pady=4)
        tk.Label(c, text=f"Im Ordner '{self.current_folder_name}' gibt es keine Dateien mit deinen Filtereinstellungen.", font=("Segoe UI", 9), fg=self.text_secondary, bg=self.card_bg).pack(pady=4)
        
        tk.Button(
            c, text="📂 Anderen Ordner wählen", font=("Segoe UI", 10, "bold"),
            bg=self.accent_blue, fg="#ffffff", relief="flat", padx=14, pady=8, cursor="hand2",
            command=self.choose_custom_folder
        ).pack(pady=12)

    # -----------------------------------------------------------------------
    # Aufbau der aktiven Datei-Karte mit Vorschau & Swipe-Gesten
    # -----------------------------------------------------------------------
    def build_card_view(self):
        for widget in self.main_container.winfo_children():
            widget.destroy()

        self.card = tk.Frame(
            self.main_container, bg=self.card_bg, highlightbackground=self.card_border, highlightthickness=1,
            cursor="sb_h_double_arrow"
        )
        self.card.pack(fill="both", expand=True)

        self.card.bind("<ButtonPress-1>", self.on_drag_start)
        self.card.bind("<B1-Motion>", self.on_drag_motion)
        self.card.bind("<ButtonRelease-1>", self.on_drag_release)

        # 1. Header Bar auf Karte
        top_card_bar = tk.Frame(self.card, bg=self.card_bg, padx=18, pady=12)
        top_card_bar.pack(fill="x")

        self.badge_cat = tk.Label(top_card_bar, text="DOKUMENTE", font=("Segoe UI", 9, "bold"), fg=self.accent_blue, bg="#1e293b", padx=8, pady=3)
        self.badge_cat.pack(side="left")

        self.lbl_system_warn = tk.Label(top_card_bar, text="⚠️ Systemdatei", font=("Segoe UI", 9, "bold"), fg="#f59e0b", bg="#3a2707", padx=8, pady=3)
        self.lbl_app_warn = tk.Label(top_card_bar, text="⚙️ App / Programm", font=("Segoe UI", 9, "bold"), fg="#ec4899", bg="#371526", padx=8, pady=3)

        self.lbl_card_counter = tk.Label(top_card_bar, text="0 / 0", font=("Segoe UI", 10), fg=self.text_secondary, bg=self.card_bg)
        self.lbl_card_counter.pack(side="right")

        # 2. Dateiname & Pfad
        self.lbl_filename = tk.Label(
            self.card, text="", font=("Segoe UI", 15, "bold"),
            fg=self.text_primary, bg=self.card_bg, wraplength=680, justify="center"
        )
        self.lbl_filename.pack(padx=20, pady=(2, 2))

        self.lbl_filepath = tk.Label(
            self.card, text="", font=("Segoe UI", 8),
            fg=self.text_secondary, bg=self.card_bg, wraplength=660, justify="center"
        )
        self.lbl_filepath.pack(padx=20, pady=(0, 8))

        # 3. INTERAKTIVE DATEI-VORSCHAU-BOX (Direkt im Fenster)
        self.preview_frame = tk.Frame(
            self.card, bg="#18181b", highlightbackground="#3f3f46", highlightthickness=1, height=220
        )
        self.preview_frame.pack(fill="both", expand=True, padx=24, pady=4)
        self.preview_frame.pack_propagate(False)

        # Thumbnail Label für direkte Bildvorschau (Pillow / Tk PhotoImage)
        self.lbl_thumbnail = tk.Label(self.preview_frame, bg="#18181b")
        self.lbl_thumbnail.pack_forget()

        # Platzhalter-Box für Nicht-Bilddateien (z. B. PDF, ZIP, Programme)
        self.fallback_preview = tk.Frame(self.preview_frame, bg="#18181b")
        self.fallback_preview.pack(expand=True)

        self.lbl_icon = tk.Label(self.fallback_preview, text="📁", font=("Segoe UI Emoji", 48), fg="#ffffff", bg="#18181b")
        self.lbl_icon.pack(pady=(6, 2))

        self.lbl_placeholder_filename = tk.Label(
            self.fallback_preview, text="", font=("Segoe UI", 12, "bold"),
            fg="#f4f4f5", bg="#18181b", wraplength=540, justify="center"
        )
        self.lbl_placeholder_filename.pack(pady=(0, 4))

        self.lbl_preview_info = tk.Label(
            self.fallback_preview, text="", font=("Segoe UI", 9),
            fg=self.text_secondary, bg="#18181b"
        )
        self.lbl_preview_info.pack(pady=(0, 6))

        # 4. Daten-Leiste
        meta_grid = tk.Frame(self.card, bg=self.card_bg, padx=20)
        meta_grid.pack(fill="x", pady=6)

        self.lbl_size = tk.Label(meta_grid, text="Größe: --", font=("Segoe UI", 11, "bold"), fg="#38bdf8", bg=self.card_bg)
        self.lbl_size.pack(side="left", padx=10)

        self.lbl_date = tk.Label(meta_grid, text="Geändert: --", font=("Segoe UI", 9), fg=self.text_secondary, bg=self.card_bg)
        self.lbl_date.pack(side="left", padx=10)

        self.btn_open_folder = tk.Button(
            meta_grid, text="📂 Im Explorer zeigen", font=("Segoe UI", 8),
            bg="#323238", fg=self.text_secondary, activebackground="#404046", activeforeground="#ffffff",
            relief="flat", padx=8, pady=3, cursor="hand2", command=self.open_current_in_explorer
        )
        self.btn_open_folder.pack(side="right", padx=10)

        # 5. Nächste Datei Teaser-Vorschau
        self.next_file_bar = tk.Frame(self.card, bg="#1f1f23", padx=16, pady=4)
        self.next_file_bar.pack(fill="x", side="bottom")

        self.lbl_next_teaser = tk.Label(
            self.next_file_bar, text="Als Nächstes: Keine weiteren Dateien", font=("Segoe UI", 8),
            fg="#71717a", bg="#1f1f23"
        )
        self.lbl_next_teaser.pack(anchor="w")

        # 6. SWIPE-OVERLAY BANNER
        self.swipe_banner = tk.Label(
            self.card, text="", font=("Segoe UI", 20, "bold"),
            fg="#ffffff", bg="#450a0a", padx=24, pady=12, relief="solid", bd=2
        )

    # -----------------------------------------------------------------------
    # Maus Drag & Swipe Steuerung
    # -----------------------------------------------------------------------
    def on_drag_start(self, event):
        self.drag_start_x = event.x_root
        self.is_dragging = True

    def on_drag_motion(self, event):
        if not self.is_dragging or self.current_index >= len(self.files_pool):
            return
        dx = event.x_root - self.drag_start_x
        
        if dx < -35:
            self.swipe_banner.config(text="🗑️  LÖSCHEN VORMERKEN", fg="#ef4444", bg="#450a0a")
            self.swipe_banner.place(relx=0.5, rely=0.45, anchor="center")
        elif dx > 35:
            self.swipe_banner.config(text="✨  BEHALTEN", fg="#22c55e", bg="#052e16")
            self.swipe_banner.place(relx=0.5, rely=0.45, anchor="center")
        else:
            self.swipe_banner.place_forget()

    def on_drag_release(self, event):
        if not self.is_dragging:
            return
        self.is_dragging = False
        dx = event.x_root - self.drag_start_x
        self.swipe_banner.place_forget()
        
        threshold = 75
        if dx < -threshold:
            self.on_mark_delete()
        elif dx > threshold:
            self.on_keep_file()

    def trigger_swipe_left(self):
        if self.current_index >= len(self.files_pool):
            return
        if hasattr(self, 'swipe_banner') and self.swipe_banner.winfo_exists():
            self.swipe_banner.config(text="🗑️  LÖSCHEN VORMERKEN", fg="#ef4444", bg="#450a0a")
            self.swipe_banner.place(relx=0.5, rely=0.45, anchor="center")
            self.after(90, self._finish_swipe_left)
        else:
            self.on_mark_delete()

    def _finish_swipe_left(self):
        if hasattr(self, 'swipe_banner') and self.swipe_banner.winfo_exists():
            self.swipe_banner.place_forget()
        self.on_mark_delete()

    def trigger_swipe_right(self):
        if self.current_index >= len(self.files_pool):
            return
        if hasattr(self, 'swipe_banner') and self.swipe_banner.winfo_exists():
            self.swipe_banner.config(text="✨  BEHALTEN", fg="#22c55e", bg="#052e16")
            self.swipe_banner.place(relx=0.5, rely=0.45, anchor="center")
            self.after(90, self._finish_swipe_right)
        else:
            self.on_keep_file()

    def _finish_swipe_right(self):
        if hasattr(self, 'swipe_banner') and self.swipe_banner.winfo_exists():
            self.swipe_banner.place_forget()
        self.on_keep_file()

    # -----------------------------------------------------------------------
    # Aktualisierung der aktuellen Karte & Laden der Vorschau
    # -----------------------------------------------------------------------
    def update_current_card(self):
        total = len(self.files_pool)
        if self.current_index >= total:
            self.on_all_finished()
            return
            
        item = self.files_pool[self.current_index]
        
        self.lbl_card_counter.config(text=f"{self.current_index + 1} von {total}")
        self.badge_cat.config(text=item.category.upper())
        self.lbl_filename.config(text=item.filename)
        self.lbl_filepath.config(text=item.path)
        self.lbl_size.config(text=f"Größe: {item.formatted_size}")
        self.lbl_date.config(text=f"Geändert: {item.formatted_date}")
        
        if item.is_system:
            self.lbl_system_warn.pack(side="left", padx=4)
        else:
            self.lbl_system_warn.pack_forget()
            
        if item.is_app:
            self.lbl_app_warn.pack(side="left", padx=4)
        else:
            self.lbl_app_warn.pack_forget()

        if self.current_index + 1 < total:
            next_item = self.files_pool[self.current_index + 1]
            self.lbl_next_teaser.config(text=f"Als Nächstes: {next_item.icon_symbol} {next_item.filename} ({next_item.formatted_size})")
        else:
            self.lbl_next_teaser.config(text="Als Nächstes: Letzte Datei in diesem Scan!")
            
        self.load_file_preview(item)
            
        self.progressbar.config(value=self.current_index)
        pct = int((self.current_index / total) * 100) if total > 0 else 100
        self.lbl_progress_text.config(text=f"Datei {self.current_index + 1} von {total} ({pct}%)")
        self.update_stats_and_counters()

    def load_file_preview(self, item: FileItem):
        self.lbl_thumbnail.pack_forget()
        self.fallback_preview.pack_forget()
        self.current_img_obj = None

        ext = item.ext.lower()

        # 1. BILD-VORSCHAU: Direkt im Anwendungsfenster mit Pillow laden und passend skalieren
        if ext in IMAGE_EXTS:
            try:
                if HAS_PIL:
                    with Image.open(item.path) as img:
                        # Automatische Korrektur der Orientierung bei Smartphone-Fotos
                        try:
                            img = ImageOps.exif_transpose(img)
                        except Exception:
                            pass
                        # Passend für die Vorschau-Box skalieren (unter Beibehaltung des Seitenverhältnisses)
                        max_w = 420
                        max_h = 210
                        resample_filter = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', Image.BILINEAR)
                        img.thumbnail((max_w, max_h), resample_filter)
                        tk_img = ImageTk.PhotoImage(img)
                        self.lbl_thumbnail.config(image=tk_img)
                        self.lbl_thumbnail.image = tk_img  # Garbage Collection Schutz
                        self.current_img_obj = tk_img
                        self.lbl_thumbnail.pack(expand=True)
                        return
                elif ext in {".png", ".gif"}:
                    img = tk.PhotoImage(file=item.path)
                    w = img.width()
                    h = img.height()
                    factor = max(1, w // 420, h // 210)
                    if factor > 1:
                        img = img.subsample(factor, factor)
                    self.lbl_thumbnail.config(image=img)
                    self.lbl_thumbnail.image = img
                    self.current_img_obj = img
                    self.lbl_thumbnail.pack(expand=True)
                    return
            except Exception:
                pass

        # 2. KEIN BILD (z. B. PDF, ZIP, Dokumente): Großes Platzhalter-Icon & Dateiname anzeigen
        self.lbl_icon.config(text=item.icon_symbol)
        self.lbl_placeholder_filename.config(text=item.filename)
        ext_clean = item.ext.upper().lstrip(".") if item.ext else "DATEI"
        type_desc = f"{ext_clean}-Format • {item.category}"
        self.lbl_preview_info.config(text=f"{type_desc} • {item.formatted_size}")
        self.fallback_preview.pack(expand=True)

    # -----------------------------------------------------------------------
    # Entscheidungen
    # -----------------------------------------------------------------------
    def on_mark_delete(self):
        if self.current_index >= len(self.files_pool):
            return
        item = self.files_pool[self.current_index]
        
        if item not in self.marked_for_deletion:
            self.marked_for_deletion.append(item)
            
        self.history.append(("delete", item))
        self.current_index += 1
        self.update_current_card()

    def on_keep_file(self):
        if self.current_index >= len(self.files_pool):
            return
        item = self.files_pool[self.current_index]
        self.kept_files.append(item)
        self.history.append(("keep", item))
        self.current_index += 1
        self.update_current_card()

    def on_undo(self):
        if not self.history or self.current_index <= 0:
            return
            
        action, item = self.history.pop()
        if action == "delete":
            if item in self.marked_for_deletion:
                self.marked_for_deletion.remove(item)
        elif action == "keep":
            if item in self.kept_files:
                self.kept_files.remove(item)
                
        self.current_index -= 1
        
        if self.current_index < len(self.files_pool):
            self.btn_delete.config(state="normal")
            self.btn_undo.config(state="normal")
            self.btn_keep.config(state="normal")
            if not hasattr(self, 'card') or not self.card.winfo_children():
                self.build_card_view()
                
        self.update_current_card()

    def open_current_in_explorer(self):
        if self.current_index >= len(self.files_pool):
            return
        item = self.files_pool[self.current_index]
        if not os.path.exists(item.path):
            self.show_custom_info("Hinweis", "Datei existiert nicht mehr.")
            return
            
        if IS_WINDOWS:
            subprocess.run(["explorer", f"/select,{os.path.normpath(item.path)}"])
        else:
            subprocess.run(["xdg-open", os.path.dirname(item.path)])

    # -----------------------------------------------------------------------
    # Abschluss-Bildschirm
    # -----------------------------------------------------------------------
    def on_all_finished(self):
        self.btn_delete.config(state="disabled")
        self.btn_keep.config(state="disabled")
        self.progressbar.config(value=len(self.files_pool))
        self.lbl_progress_text.config(text="Ausmisten beendet!")
        self.update_stats_and_counters()

        for widget in self.main_container.winfo_children():
            widget.destroy()

        finish_card = tk.Frame(
            self.main_container, bg=self.card_bg, highlightbackground=self.card_border, highlightthickness=1
        )
        finish_card.pack(fill="both", expand=True)

        center_box = tk.Frame(finish_card, bg=self.card_bg)
        center_box.place(relx=0.5, rely=0.45, anchor="center")

        count = len(self.marked_for_deletion)
        del_bytes = sum(f.size for f in self.marked_for_deletion)

        if self.app_photo_icon:
            lbl_icon = tk.Label(center_box, image=self.app_photo_icon, bg=self.card_bg)
        else:
            lbl_icon = tk.Label(center_box, text="🎉", font=("Segoe UI Emoji", 52), bg=self.card_bg)
        lbl_icon.pack(pady=(0, 6))

        lbl_title = tk.Label(
            center_box, text="Ordner vollständig durchgesehen!",
            font=("Segoe UI", 16, "bold"), fg="#ffffff", bg=self.card_bg
        )
        lbl_title.pack(pady=(0, 6))

        summary_text = (
            f"🗑️ {count} Datei(en) zum Löschen vorgemerkt • {format_size(del_bytes)} freigebbar\n"
            "Keine Datei wurde bisher gelöscht!"
        )
        lbl_summary = tk.Label(
            center_box, text=summary_text, font=("Segoe UI", 11),
            fg=self.text_secondary, bg=self.card_bg, justify="center"
        )
        lbl_summary.pack(pady=(0, 22))

        btn_row = tk.Frame(center_box, bg=self.card_bg)
        btn_row.pack()

        btn_check = tk.Button(
            btn_row, text="🔍 Jetzt überprüfen", font=("Segoe UI", 11, "bold"),
            bg="#dc2626", fg="#ffffff", activebackground="#b91c1c", activeforeground="#ffffff",
            relief="flat", padx=20, pady=10, cursor="hand2",
            command=self.open_review_dialog
        )
        btn_check.pack(side="left", padx=6)

        btn_new_folder = tk.Button(
            btn_row, text="📂 Anderen Ordner ausmisten", font=("Segoe UI", 10),
            bg="#323238", fg="#ffffff", activebackground="#3f3f46", activeforeground="#ffffff",
            relief="flat", padx=16, pady=10, cursor="hand2",
            command=self.choose_custom_folder
        )
        btn_new_folder.pack(side="left", padx=6)

    # -----------------------------------------------------------------------
    # Papierkorb-Prüfung Dialog
    # -----------------------------------------------------------------------
    def open_review_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("Papierkorb-Prüfung (Sicherheits-Review)")
        dialog.geometry("780x560")
        dialog.minsize(680, 460)
        dialog.configure(bg=self.bg_color)
        dialog.transient(self)
        dialog.grab_set()
        self.apply_window_icon(dialog)

        top_box = tk.Frame(dialog, bg=self.bg_color, padx=20, pady=14)
        top_box.pack(fill="x")
        
        lbl_h = tk.Label(top_box, text="Vorgemerkte Dateien überprüfen", font=("Segoe UI", 14, "bold"), fg="#ffffff", bg=self.bg_color)
        lbl_h.pack(anchor="w")
        
        total_bytes = sum(f.size for f in self.marked_for_deletion)
        header_sub_text = (
            f"{len(self.marked_for_deletion)} Datei(en) markiert • Insgesamt {format_size(total_bytes)} freigebbar\n"
            "ℹ️ Erst bei Klick unten wird in den Windows-Papierkorb verschoben."
        )
        lbl_sub = tk.Label(
            top_box, text=header_sub_text, font=("Segoe UI", 9),
            fg=self.text_secondary, bg=self.bg_color, justify="left"
        )
        lbl_sub.pack(anchor="w", pady=(3, 0))

        # Baumansicht / Tabelle
        tree_frame = tk.Frame(dialog, bg=self.bg_color, padx=20)
        tree_frame.pack(fill="both", expand=True)

        columns = ("name", "size", "cat", "path")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        tree.heading("name", text="Dateiname")
        tree.heading("size", text="Größe")
        tree.heading("cat", text="Kategorie")
        tree.heading("path", text="Pfad")
        
        tree.column("name", width=240, anchor="w")
        tree.column("size", width=90, anchor="e")
        tree.column("cat", width=90, anchor="center")
        tree.column("path", width=320, anchor="w")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def populate_tree():
            for row in tree.get_children():
                tree.delete(row)
            for idx, f in enumerate(self.marked_for_deletion):
                tree.insert("", "end", iid=str(idx), values=(f.filename, f.formatted_size, f.category, f.path))

        populate_tree()

        # Untere Aktionsleiste
        btn_bar = tk.Frame(dialog, bg=self.bg_color, padx=20, pady=16)
        btn_bar.pack(fill="x")

        def remove_selected():
            selected = tree.selection()
            if not selected:
                self.show_custom_info("Hinweis", "Wähle Dateien in der Liste aus, die du doch behalten möchtest.", parent=dialog)
                return
            indices = sorted([int(i) for i in selected], reverse=True)
            for idx in indices:
                if idx < len(self.marked_for_deletion):
                    self.marked_for_deletion.pop(idx)
            
            self.update_stats_and_counters()
            populate_tree()
            new_bytes = sum(f.size for f in self.marked_for_deletion)
            updated_sub = (
                f"{len(self.marked_for_deletion)} Datei(en) markiert • Insgesamt {format_size(new_bytes)} freigebbar\n"
                "ℹ️ Erst bei Klick unten wird in den Windows-Papierkorb verschoben."
            )
            lbl_sub.config(text=updated_sub)

        def confirm_and_execute_move():
            count = len(self.marked_for_deletion)
            if count == 0:
                self.show_custom_info("Liste leer", "Es sind keine Dateien zum Löschen vorgemerkt.", parent=dialog)
                return
                
            bytes_freed = sum(f.size for f in self.marked_for_deletion)
            
            confirm_win = tk.Toplevel(dialog)
            confirm_win.title("Sicherheits-Bestätigung")
            confirm_win.geometry("520x260")
            confirm_win.resizable(False, False)
            confirm_win.configure(bg="#18181b")
            confirm_win.transient(dialog)
            confirm_win.grab_set()
            self.apply_window_icon(confirm_win)

            c_box = tk.Frame(confirm_win, bg="#18181b", padx=24, pady=20)
            c_box.pack(fill="both", expand=True)

            tk.Label(c_box, text="🗑️ Wirklich in den Papierkorb verschieben?", font=("Segoe UI", 12, "bold"), fg="#ffffff", bg="#18181b").pack(anchor="w", pady=(0, 8))
            
            confirm_info_text = (
                f"Sollen diese {count} Datei(en) mit insgesamt {format_size(bytes_freed)} jetzt in den Windows-Papierkorb verschoben werden?\n\n"
                "ℹ️ Die Dateien landen im echten Windows-Papierkorb und können dort bei Bedarf jederzeit wiederhergestellt werden."
            )
            tk.Label(
                c_box, text=confirm_info_text,
                font=("Segoe UI", 9), fg="#d4d4d8", bg="#18181b", justify="left", wraplength=470
            ).pack(anchor="w", pady=(0, 16))

            c_btn_row = tk.Frame(c_box, bg="#18181b")
            c_btn_row.pack(fill="x", side="bottom")

            def on_cancel():
                confirm_win.destroy()

            def on_confirm():
                confirm_win.destroy()
                
                success_count = 0
                fail_count = 0
                for item in list(self.marked_for_deletion):
                    if move_to_recycle_bin(item.path):
                        success_count += 1
                        self.marked_for_deletion.remove(item)
                    else:
                        fail_count += 1

                self.update_stats_and_counters()
                dialog.destroy()
                
                info_msg = (
                    f"✅ {success_count} Datei(en) ({format_size(bytes_freed)}) sicher in den Windows-Papierkorb verschoben!\n" +
                    (f"⚠️ {fail_count} Datei(en) konnten nicht verschoben werden." if fail_count > 0 else "")
                )
                self.show_custom_info("Aufräumen erfolgreich!", info_msg)
                self.show_welcome_view()

            tk.Button(
                c_btn_row, text="Abbrechen", font=("Segoe UI", 9),
                bg="#27272a", fg="#f4f4f5", activebackground="#3f3f46", activeforeground="#ffffff",
                relief="flat", padx=14, pady=7, cursor="hand2", command=on_cancel
            ).pack(side="left")

            tk.Button(
                c_btn_row, text="🗑️ Ja, in Papierkorb verschieben", font=("Segoe UI", 9, "bold"),
                bg="#dc2626", fg="#ffffff", activebackground="#b91c1c", activeforeground="#ffffff",
                relief="flat", padx=16, pady=7, cursor="hand2", command=on_confirm
            ).pack(side="right")

        btn_keep_sel = tk.Button(
            btn_bar, text="↩ Ausgewählte doch behalten", font=("Segoe UI", 9),
            bg="#27272a", fg=self.text_primary, activebackground="#3f3f46", activeforeground="#ffffff",
            relief="flat", padx=14, pady=8, cursor="hand2", command=remove_selected
        )
        btn_keep_sel.pack(side="left")

        btn_execute = tk.Button(
            btn_bar, text="🗑️ In Windows-Papierkorb verschieben", font=("Segoe UI", 10, "bold"),
            bg="#dc2626", fg="#ffffff", activebackground="#b91c1c", activeforeground="#ffffff",
            relief="flat", padx=18, pady=8, cursor="hand2", command=confirm_and_execute_move
        )
        btn_execute.pack(side="right")

    # -----------------------------------------------------------------------
    # Einstellungs-Dialog
    # -----------------------------------------------------------------------
    def open_settings_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title("Einstellungen")
        dialog.geometry("660x580")
        dialog.minsize(600, 500)
        dialog.configure(bg=self.bg_color)
        dialog.transient(self)
        dialog.grab_set()
        self.apply_window_icon(dialog)

        content = tk.Frame(dialog, bg=self.bg_color, padx=22, pady=18)
        content.pack(fill="both", expand=True)

        tk.Label(content, text="⚙️ Einstellungen", font=("Segoe UI", 15, "bold"), fg="#ffffff", bg=self.bg_color).pack(anchor="w", pady=(0, 14))

        sys_frame = tk.LabelFrame(
            content, text=" System- & App-Sicherheitsfilter ", font=("Segoe UI", 10, "bold"),
            fg="#38bdf8", bg=self.card_bg, padx=14, pady=12, highlightbackground=self.card_border, highlightthickness=1
        )
        sys_frame.pack(fill="x", pady=6)

        var_include_sys = tk.BooleanVar(value=self.settings.include_system_files)
        chk_sys = tk.Checkbutton(
            sys_frame, text="System- und versteckte Dateien mit einbeziehen",
            variable=var_include_sys, font=("Segoe UI", 10),
            fg="#ffffff", bg=self.card_bg, selectcolor="#18181b", activebackground=self.card_bg, activeforeground="#ffffff"
        )
        chk_sys.pack(anchor="w")

        lbl_sys_hint = tk.Label(
            sys_frame,
            text="• Standardmäßig AUSgeschlossen: Schützt AppData, Windows, System32 sowie versteckte .ini/.dat-Dateien.",
            font=("Segoe UI", 8), fg=self.text_secondary, bg=self.card_bg, wraplength=570, justify="left"
        )
        lbl_sys_hint.pack(anchor="w", pady=(2, 6))

        var_include_apps = tk.BooleanVar(value=self.settings.include_apps)
        chk_apps = tk.Checkbutton(
            sys_frame, text="Apps & ausführbare Programme mit einbeziehen (.exe, .msi usw.)",
            variable=var_include_apps, font=("Segoe UI", 10),
            fg="#ffffff", bg=self.card_bg, selectcolor="#18181b", activebackground=self.card_bg, activeforeground="#ffffff"
        )
        chk_apps.pack(anchor="w", pady=(4, 0))

        lbl_apps_hint = tk.Label(
            sys_frame,
            text="• Standardmäßig AUSgeschlossen: Verhindert das versehentliche Löschen von Anwendungsdateien & Installern.",
            font=("Segoe UI", 8), fg=self.text_secondary, bg=self.card_bg, wraplength=570, justify="left"
        )
        lbl_apps_hint.pack(anchor="w", pady=(2, 0))

        # Filter & Sortierung
        filter_frame = tk.LabelFrame(
            content, text=" Filter & Sortierung ", font=("Segoe UI", 10, "bold"),
            fg="#38bdf8", bg=self.card_bg, padx=14, pady=12, highlightbackground=self.card_border, highlightthickness=1
        )
        filter_frame.pack(fill="x", pady=8)

        row1 = tk.Frame(filter_frame, bg=self.card_bg)
        row1.pack(fill="x", pady=6)
        tk.Label(row1, text="Dateikategorie:", width=18, anchor="w", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg=self.card_bg).pack(side="left")
        
        categories = ["Alle", "Bilder", "Videos", "Audio", "Dokumente", "Archive", "Apps"]
        var_cat = tk.StringVar(value=self.settings.category_filter)
        cmb_cat = ttk.Combobox(row1, textvariable=var_cat, values=categories, state="readonly", width=20)
        cmb_cat.pack(side="left")

        row2 = tk.Frame(filter_frame, bg=self.card_bg)
        row2.pack(fill="x", pady=6)
        tk.Label(row2, text="Mindestgröße:", width=18, anchor="w", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg=self.card_bg).pack(side="left")
        
        size_options = {
            "0 MB (Alle)": 0.0,
            "> 5 MB": 5.0,
            "> 20 MB": 20.0,
            "> 50 MB": 50.0,
            "> 100 MB": 100.0,
            "> 500 MB": 500.0,
            "> 1 GB": 1024.0,
        }
        current_size_label = "0 MB (Alle)"
        for k, v in size_options.items():
            if abs(v - self.settings.min_size_mb) < 0.1:
                current_size_label = k
                break
                
        var_size = tk.StringVar(value=current_size_label)
        cmb_size = ttk.Combobox(row2, textvariable=var_size, values=list(size_options.keys()), state="readonly", width=20)
        cmb_size.pack(side="left")

        row3 = tk.Frame(filter_frame, bg=self.card_bg)
        row3.pack(fill="x", pady=6)
        tk.Label(row3, text="Reihenfolge:", width=18, anchor="w", font=("Segoe UI", 9, "bold"), fg="#ffffff", bg=self.card_bg).pack(side="left")
        
        sort_opts = ["Zufällig", "Größte zuerst", "Neueste zuerst", "Älteste zuerst"]
        var_sort = tk.StringVar(value=self.settings.sort_order)
        cmb_sort = ttk.Combobox(row3, textvariable=var_sort, values=sort_opts, state="readonly", width=20)
        cmb_sort.pack(side="left")

        def save_and_close():
            self.settings.include_system_files = var_include_sys.get()
            self.settings.include_apps = var_include_apps.get()
            self.settings.category_filter = var_cat.get()
            self.settings.min_size_mb = size_options.get(var_size.get(), 0.0)
            self.settings.sort_order = var_sort.get()
            
            dialog.destroy()
            
            if self.settings.selected_folders:
                self.start_scan()

        bottom_bar = tk.Frame(dialog, bg=self.bg_color, padx=22, pady=16)
        bottom_bar.pack(fill="x", side="bottom")

        tk.Button(
            bottom_bar, text="Abbrechen", font=("Segoe UI", 9),
            bg="#27272a", fg=self.text_secondary, activebackground="#3f3f46", activeforeground="#ffffff",
            relief="flat", padx=14, pady=7, cursor="hand2", command=dialog.destroy
        ).pack(side="left")

        tk.Button(
            bottom_bar, text="Speichern & Anwenden", font=("Segoe UI", 9, "bold"),
            bg=self.accent_blue, fg="#ffffff", activebackground="#2563eb", activeforeground="#ffffff",
            relief="flat", padx=18, pady=7, cursor="hand2", command=save_and_close
        ).pack(side="right")

    # -----------------------------------------------------------------------
    # Eigener Info-Dialog
    # -----------------------------------------------------------------------
    def show_custom_info(self, title: str, message: str, parent=None):
        info_win = tk.Toplevel(parent or self)
        info_win.title(title)
        info_win.geometry("460x200")
        info_win.resizable(False, False)
        info_win.configure(bg="#18181b")
        info_win.transient(parent or self)
        info_win.grab_set()
        self.apply_window_icon(info_win)

        box = tk.Frame(info_win, bg="#18181b", padx=24, pady=20)
        box.pack(fill="both", expand=True)

        tk.Label(box, text=title, font=("Segoe UI", 12, "bold"), fg="#ffffff", bg="#18181b").pack(anchor="w", pady=(0, 8))
        tk.Label(box, text=message, font=("Segoe UI", 9), fg="#d4d4d8", bg="#18181b", justify="left", wraplength=410).pack(anchor="w", pady=(0, 16))

        tk.Button(
            box, text="OK", font=("Segoe UI", 9, "bold"),
            bg=self.accent_blue, fg="#ffffff", activebackground="#2563eb", activeforeground="#ffffff",
            relief="flat", padx=18, pady=6, cursor="hand2", command=info_win.destroy
        ).pack(side="bottom", anchor="e")


# ---------------------------------------------------------------------------
# Programmstart
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = FileSwipeApp()
    app.mainloop()
