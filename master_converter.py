"""
Master Converter - converts images, videos and sound, and makes GIFs. Runs 100% locally.
Setup:   pip install -r requirements.txt
Run:     python master_converter.py
"""
import base64
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageOps, ImageTk

try:  # adds HEIC/HEIF (iPhone photos) read + write support
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

try:  # adds camera RAW photos (CR2, NEF, ARW, DNG...) through LibRaw
    import rawpy
    HAS_RAWPY = True
except ImportError:
    HAS_RAWPY = False

try:  # adds SVG drawings: resvg renders them into pixels (Pillow can't read SVG)
    import resvg_py
    HAS_RESVG = True
except ImportError:
    HAS_RESVG = False

try:  # adds drag & drop from Explorer
    from tkinterdnd2 import TkinterDnD, DND_FILES
    BaseTk, HAS_DND = TkinterDnD.Tk, True
except ImportError:
    BaseTk, HAS_DND = tk.Tk, False

Image.init()


def resource_path(name):
    """Path to a bundled file, both when run as a script and inside the .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# Output formats: label -> (Pillow format name, extension)
CANDIDATES = {
    "PNG": ("PNG", ".png"), "JPEG": ("JPEG", ".jpg"), "WEBP": ("WEBP", ".webp"),
    "BMP": ("BMP", ".bmp"), "GIF": ("GIF", ".gif"), "TIFF": ("TIFF", ".tiff"),
    "ICO": ("ICO", ".ico"), "PDF": ("PDF", ".pdf"), "TGA": ("TGA", ".tga"),
    "PPM": ("PPM", ".ppm"), "AVIF": ("AVIF", ".avif"), "HEIC": ("HEIF", ".heic"),
    "JPEG 2000": ("JPEG2000", ".jp2"), "DDS": ("DDS", ".dds"), "PCX": ("PCX", ".pcx"),
}
OUT_FORMATS = {k: v for k, v in CANDIDATES.items() if v[0] in Image.SAVE}
# written by this app itself, not Pillow (see convert_one)
OUT_FORMATS["SVG"] = ("SVG", ".svg")  # the picture embedded in an SVG file
OUT_FORMATS["RAW (pixel data)"] = ("PIXELS", ".raw")  # bare pixel bytes, no header
NO_ALPHA = {"JPEG", "BMP", "PPM", "PDF", "PCX"}
LOSSY = {"JPEG", "WEBP", "AVIF", "HEIF", "JPEG2000"}
EXIF_OK = {"JPEG", "PNG", "WEBP", "TIFF", "AVIF", "HEIF"}  # formats that can store photo info
SAFE_MODES = {"1", "L", "LA", "P", "PA", "RGB", "RGBA"}  # modes every writer understands
# names Windows won't let you use for a file, whatever the extension
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}

# camera RAW photos (opened with LibRaw; only cameras can make these, so input only)
RAW_EXTS = (".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf",
            ".orf", ".rw2", ".raw", ".rwl", ".pef", ".srw", ".x3f", ".3fr", ".erf", ".kdc",
            ".mrw", ".iiq", ".mef", ".mos")
SVG_EXTS = (".svg", ".svgz")  # drawings, rendered into pixels with resvg
INPUT_EXTS = sorted(set(Image.registered_extensions())
                    | (set(RAW_EXTS) if HAS_RAWPY else set())
                    | (set(SVG_EXTS) if HAS_RESVG else set()))


def is_camera_raw(path):
    return HAS_RAWPY and path.lower().endswith(RAW_EXTS)


def is_svg(path):
    return HAS_RESVG and path.lower().endswith(SVG_EXTS)


def render_svg(path, zoom=None):
    """An SVG drawn into pixels (transparent where the drawing is), at its own size or zoomed.
    Linked pictures next to it and system fonts for its text are found too."""
    try:
        png = resvg_py.svg_to_bytes(svg_path=path, zoom=zoom,
                                    resources_dir=os.path.dirname(os.path.abspath(path)))
    except ValueError as e:  # e.g. "expected '=' not 'i' at 1:11"
        raise ValueError(f"not a valid SVG drawing ({e})") from None
    with Image.open(io.BytesIO(bytes(png))) as im:
        im.load()
        return im.copy()


def open_image(path, fast=False):
    """Open any picture, the right way up: normal formats through Pillow (turned per EXIF),
    camera RAW through LibRaw (developed with the camera's white balance and already turned),
    SVG drawings rendered by resvg.
    fast=True is for thumbnails: RAW is developed at half size (much quicker), and small SVG
    icons are drawn bigger so the thumbnail isn't a blurry speck."""
    if is_camera_raw(path):
        with rawpy.imread(path) as raw:
            return Image.fromarray(raw.postprocess(use_camera_wb=True, output_bps=8,
                                                   half_size=fast))
    if is_svg(path):
        im = render_svg(path)
        if fast and max(im.size) < 320:  # vectors stay sharp at any size: redraw it bigger
            im = render_svg(path, zoom=320 / max(im.size))
        return im
    with Image.open(path) as im:
        im.load()
        return ImageOps.exif_transpose(im)


def raw_dims(path):
    """(width, height) of a camera RAW photo as it will come out (turned the right way)."""
    with rawpy.imread(path) as raw:
        w, h = raw.sizes.width, raw.sizes.height
        return (h, w) if raw.sizes.flip in (5, 6) else (w, h)

# XP-ish palette
BG, BLUE, DARK = "#ECE9D8", "#245EDC", "#0A246A"


# ---- light / dark mode ----
# The app is written with light colours. In dark mode, every colour it gives Tk - when making
# a widget, changing one, or drawing on a canvas - goes through dark_color() first, so the
# whole app turns dark without each colour being handled one by one. Some colours depend on
# their role: white is a box's background in one place and a 3D edge's highlight in another.
DARK_MODE = False
DARK_FACE, DARK_BOX, DARK_TEXT = "#353535", "#1E1E1E", "#E8E8E8"
_DARK_ANY = {  # light colour -> dark colour, wherever it's used
    "#ece9d8": DARK_FACE,  # window / button face (BG)
    "#f5f3e8": "#474747",  # pressed button face
    "#ffffe1": "#403f2c",  # tooltip
    "#efefef": "#2b2b2b",  # scrollbar track
    "#cccccc": "#5b5b5b",  # scrollbar thumb
    "#a6a6a6": "#777777",  # ... under the mouse
    "#606060": "#8c8c8c",  # ... held down / ruler ticks
    "#dadada": "#454545",  # scrollbar arrow under the mouse
    "#5f5f5f": "#c2c2c2",  # scrollbar arrows
    "#b0b0b0": "#626262",  # picture box border
    "#a0a0a0": "#6c6c6c",  # timeline lines
    "#666666": "#b3b3b3",  # grey text
    "#888888": "#8f8f8f",  # hint text
    "#999999": "#727272",  # greyed-out text
    "#303030": "#d4d4d4",  # timeline numbers
    "#c00000": "#ff7a7a",  # red messages
    "#e4e4e4": "#3c3c3c",  # picture still loading
    "#d8d8d8": "#3a3a3a",  # filmstrip still loading
    "#9a9a9a": "#1c1c1c",  # parts of the filmstrip left out
    "#f0f0f0": "#3b3b3b",  # right-click menu's inner frame
}
_DARK_ROLE = {  # colours that change differently depending on what they're for
    "text": {"#000000": DARK_TEXT, "#ffffff": "#ffffff"},  # (white text stays white)
    "box": {"#ffffff": DARK_BOX, "#000000": DARK_TEXT},  # box backgrounds; black shapes
    "edge": {"#ffffff": "#5e5e5e", "#8e8c82": "#1b1b1b", "#000000": "#000000"},  # 3D edges
    "face": {},
}
_TO_DARK = {r: {k: v.lower() for k, v in {**_DARK_ANY, **m}.items()}  # all lowercase, so
            for r, m in _DARK_ROLE.items()}                           # both ways look up alike
_TO_LIGHT = {r: {d: l for l, d in m.items()} for r, m in _TO_DARK.items()}
for _r, _m in _TO_DARK.items():  # every dark colour must lead back to one light colour
    assert len(set(_m.values())) == len(_m) and not set(_m.values()) & set(_m) - {"#000000", "#ffffff"}, _r
_NAMES = {"white": "#ffffff", "black": "#000000"}


def _norm(color):
    c = _NAMES.get(str(color).lower(), str(color).lower())
    return "#" + "".join(ch * 2 for ch in c[1:]) if len(c) == 4 and c[0] == "#" else c


def dark_color(color, role="face"):
    """The dark-mode partner of a light colour (unchanged if it has none)."""
    return _TO_DARK[role].get(_norm(color), color) if isinstance(color, str) else color


def light_color(color, role="face"):
    """The other way: a dark-mode colour back to its light one."""
    return _TO_LIGHT[role].get(_norm(color), color) if isinstance(color, str) else color


def themed(color, role="face"):
    """A colour for the current mode - for pictures drawn with Pillow, which Tk doesn't see."""
    return dark_color(color, role) if DARK_MODE else color


# which role a widget's colour options play; a widget's own background depends on its kind:
# frames' white is a 3D edge (the window / page borders), labels' and canvases' is a box
_OPT_ROLE = {"fg": "text", "foreground": "text", "activeforeground": "text",
             "disabledforeground": "text", "insertbackground": "text",
             "selectforeground": "text", "activebackground": "face",
             "highlightbackground": "face", "troughcolor": "face", "selectcolor": "box",
             "readonlybackground": "box"}
_BOX_WIDGETS = {"canvas", "label", "entry", "listbox", "text"}
_EDGE_WIDGETS = {"frame", "toplevel", "labelframe"}


def _widget_role(widget, opt):
    if opt in ("bg", "background"):
        kind = getattr(widget, "widgetName", "frame")
        return "box" if kind in _BOX_WIDGETS else "edge" if kind in _EDGE_WIDGETS else "face"
    return _OPT_ROLE.get(opt)


def _item_role(item_type, opt):
    if opt == "outline" or item_type == "line":
        return "edge"
    return "text" if item_type == "text" else "box"


def _map_opts(opts, role_of):
    if not isinstance(opts, dict):
        return opts
    out = dict(opts)
    for k, v in opts.items():
        role = role_of(k.lstrip("-"))
        if role:
            out[k] = dark_color(v, role)
    return out


_orig_options = tk.Misc._options
_orig_create = tk.Canvas._create
_orig_itemconfigure = tk.Canvas.itemconfigure


def _themed_options(self, cnf, kw=None):  # every widget made or changed
    if DARK_MODE:
        cnf, kw = (_map_opts(o, lambda k: _widget_role(self, k)) for o in (cnf, kw))
    return _orig_options(self, cnf, kw)


def _themed_create(self, item_type, args, kw):  # every shape drawn on a canvas
    if DARK_MODE:
        pick = lambda k: _item_role(item_type, k) if k in ("fill", "outline") else None  # noqa: E731
        kw = _map_opts(kw, pick)
        args = list(args)
        if args and isinstance(args[-1], dict):
            args[-1] = _map_opts(args[-1], pick)
    return _orig_create(self, item_type, args, kw)


def _themed_itemconfigure(self, tag_or_id, cnf=None, **kw):  # every shape changed
    if DARK_MODE and (cnf or kw):
        item_type = self.type(tag_or_id)
        pick = lambda k: _item_role(item_type, k) if k in ("fill", "outline") else None  # noqa: E731
        cnf, kw = _map_opts(cnf, pick), _map_opts(kw, pick)
    return _orig_itemconfigure(self, tag_or_id, cnf, **kw)


tk.Misc._options = _themed_options
tk.Canvas._create = _themed_create
tk.Canvas.itemconfigure = tk.Canvas.itemconfig = _themed_itemconfigure

_SYSTEM_DARK = {  # Tk's own default colours ("SystemButtonText"...) in dark mode
    "fg": DARK_TEXT, "foreground": DARK_TEXT, "activeforeground": DARK_TEXT,
    "insertbackground": DARK_TEXT, "disabledforeground": "#7a7a7a", "selectcolor": DARK_BOX,
    "troughcolor": "#262626", "highlightbackground": DARK_FACE, "activebackground": "#474747",
    "bg": DARK_FACE, "background": DARK_FACE}
_system_colors = {}  # (widget, option) -> the Tk default it had in light mode, to put back


def retheme(widget, dark):
    """Switch one existing widget (and a canvas's drawings) to the new mode."""
    convert = dark_color if dark else light_color
    for opt in _SYSTEM_DARK:
        try:
            val = str(widget.cget(opt))
        except (tk.TclError, ValueError):
            continue
        if val.lower().startswith("system"):  # a Tk default colour
            if dark:
                _system_colors[(str(widget), opt)] = val
                new = _SYSTEM_DARK[opt]
            else:
                continue
        elif not dark and (str(widget), opt) in _system_colors:
            new = _system_colors.pop((str(widget), opt))
        else:
            role = _widget_role(widget, opt)
            new = convert(val, role) if role else val
        if new != val:
            try:
                widget.configure({opt: new})
            except tk.TclError:
                pass
    if isinstance(widget, tk.Canvas):
        for item in widget.find_all():
            item_type = widget.type(item)
            for opt in ("fill", "outline"):
                try:
                    val = widget.itemcget(item, opt)
                except tk.TclError:
                    continue
                new = convert(val, _item_role(item_type, opt)) if val else val
                if new != val:
                    widget.itemconfigure(item, {opt: new})


def theme_icon(kind):
    """12 px icon for the light/dark button: a moon (click for dark), a sun (click for light)."""
    im = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if kind == "moon":
        d.ellipse([1, 1, 10, 10], fill="#1F2D5C")
        d.ellipse([4, -1, 13, 8], fill=(0, 0, 0, 0))  # bite out of it: a crescent
    else:
        d.ellipse([3, 3, 8, 8], fill="#FFD24A")
        for x0, y0, x1, y1 in ((5, 0, 6, 1), (5, 10, 6, 11), (0, 5, 1, 6), (10, 5, 11, 6),
                               (1, 1, 2, 2), (9, 1, 10, 2), (1, 9, 2, 10), (9, 9, 10, 10)):
            d.rectangle([x0, y0, x1, y1], fill="#FFD24A")  # rays
    return ImageTk.PhotoImage(im)


# ---- updates: new versions come from this project's GitHub Releases ----
APP_VERSION = "dev"  # GitHub puts the release's version here when it builds the app
GITHUB_REPO = "bocchhii/image-converter"


def parse_version(text):
    """ "v2.10.1" -> (2, 10, 1), so versions compare as numbers; None if there's none."""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) if nums else None


def fetch_latest_release():
    """(version, release notes, installer download link) of the newest release, or None
    (no internet, GitHub unreachable...). Only reads the public release list."""
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
        headers={"User-Agent": "MasterConverter", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:
        return None
    url = next((a.get("browser_download_url") for a in data.get("assets", [])
                if a.get("name", "").lower().endswith("setup.exe")), None)
    version = (data.get("tag_name") or "").lstrip("vV")
    return (version, data.get("body") or "", url) if url and parse_version(version) else None


def plain_notes(markdown):
    """GitHub release notes are written in Markdown; show them as tidy plain text."""
    lines = []
    for line in (markdown or "").replace("\r\n", "\n").split("\n"):
        line = re.sub(r"^\s*#+\s*", "", line)  # "## Title" -> "Title"
        line = re.sub(r"^(\s*)[-*]\s+", "\\1\u2022  ", line)  # "- item" -> a bullet point
        line = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m[1] or m[2], line)  # bold
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)  # [text](link) -> text
        lines.append(line.replace("`", ""))
    return "\n".join(lines).strip()


def notes_dialog(parent, heading, notes):
    """A message box with a heading and a scrollable block of text (the release notes)."""
    owner = parent.winfo_toplevel()
    win = tk.Toplevel(owner)
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(owner)
    body = ClassicWindow(win, "Master Converter", win.destroy, resizable=False,
                         taskbar=False).body
    tk.Label(body, text=heading, bg=BG, font=("Tahoma", 9, "bold"), justify="left",
             anchor="w").pack(fill="x", padx=12, pady=(12, 6))
    box = tk.Frame(body, bg=BG)
    box.pack(padx=12)
    text = tk.Text(box, width=58, height=14, wrap="word", font=FONT, bg="white", fg="black",
                   relief="sunken", bd=2, padx=6, pady=4, highlightthickness=0)
    sb = FlatScrollbar(box, command=text.yview)
    sb.config(height=1)  # stretch to the text box's height instead of setting it
    text.config(yscrollcommand=sb.set)
    text.insert("1.0", notes or "(no release notes)")
    text.config(state="disabled")  # read only
    text.pack(side="left")
    sb.pack(side="left", fill="y")
    row = tk.Frame(body, bg=BG)
    row.pack(pady=(10, 12))
    ok = xp_button(row, "OK", win.destroy)
    ok.pack()
    win.bind("<Return>", lambda e: win.destroy())
    win.bind("<Escape>", lambda e: win.destroy())
    win.update_idletasks()
    x = owner.winfo_rootx() + (owner.winfo_width() - win.winfo_reqwidth()) // 2
    y = owner.winfo_rooty() + (owner.winfo_height() - win.winfo_reqheight()) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.focus_force()
    ok.focus_set()
    win.grab_set()
    play_sound("done")
    win.wait_window()


SETTINGS_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                             "Master Converter", "settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**values):
    """Remember choices (like dark mode) for next time; quietly skipped if it can't."""
    try:
        data = {**load_settings(), **values}
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def all_widgets(root):
    out, todo = [], [root]
    while todo:
        w = todo.pop()
        out.append(w)
        todo.extend(w.winfo_children())
    return out
FONT = ("Tahoma", 9)


# ---- video: done by ffmpeg, which the imageio-ffmpeg package ships ready-made ----
def find_ffmpeg():
    """The bundled ffmpeg (pip install imageio-ffmpeg), else one on PATH, else None."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flash

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".m4v", ".mpg",
              ".mpeg", ".3gp", ".ts", ".mts", ".m2ts", ".ogv", ".gif")
VIDEO_FORMATS = {"MP4": ".mp4", "WEBM": ".webm", "MKV": ".mkv", "MOV": ".mov", "AVI": ".avi",
                 "GIF": ".gif", "MP3 (sound only)": ".mp3"}
VIDEO_SIZES = {"Original": None, "4K (2160p)": 2160, "1080p": 1080, "720p": 720,
               "480p": 480, "360p": 360}  # limit for the short side, so portrait works too
VIDEO_FPS = ["Original", "60", "30", "24", "15", "10"]


def probe_video(path):
    """Length in seconds, (width, height) and whether it has sound - read from ffmpeg's header dump."""
    info = {"duration": None, "size": None, "audio": False}
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-i", path],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=NO_WINDOW, timeout=30)
    except Exception:
        return info
    text = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = re.search(r"Stream #.*?Video:.*?\b(\d{2,5})x(\d{2,5})\b", text)
    if m:
        info["size"] = (int(m[1]), int(m[2]))
    m = re.search(r"Stream #.*?Audio: (\w+)[^\n]*?(\d+) Hz, ([^,\n]+)", text)
    info["audio"] = bool(m) or bool(re.search(r"Stream #.*?Audio:", text))
    if m:  # e.g. ("mp3", 44100, "stereo")
        info["acodec"], info["arate"], info["alayout"] = m[1], int(m[2]), m[3].strip()
    # the sound's bitrate: on its own line, or (sound-only files like FLAC) the whole file's
    m = re.search(r"Stream #[^\n]*?Audio:[^\n]*?(\d+) kb/s", text)
    whole = re.search(r"Duration:[^\n]*?bitrate: (\d+) kb/s", text)
    if m:
        info["abitrate"] = int(m[1])
    elif whole and info["audio"] and not info["size"]:
        info["abitrate"] = int(whole[1])
    return info


def ffmpeg_args(src, dst, label, quality, height, fps, mute):
    """The ffmpeg command line for one conversion. quality is 1-100 like the image slider."""
    ext = VIDEO_FORMATS[label]
    args = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-n", "-i", src]
    if ext == ".mp3":
        return args + ["-vn", "-c:a", "libmp3lame",
                       "-q:a", str(round((100 - quality) * 9 / 99)), dst]  # 0 = best, 9 = smallest
    filters = [f"fps={fps}"] if fps else []
    if height:  # shrink so the short side is at most `height`; never enlarges
        filters.append(f"scale='if(gt(iw,ih),-2,min(iw,{height}))'"
                       f":'if(gt(iw,ih),min(ih,{height}),-2)':flags=lanczos")
    if ext == ".gif":  # a palette made from the video itself looks far better than the default
        filters.append("split[a][b];[a]palettegen=stats_mode=diff[p];"
                       "[b][p]paletteuse=dither=bayer:bayer_scale=5")
        return args + ["-vf", ",".join(filters), "-loop", "0", dst]
    # most players need even sizes and 4:2:0 colour (GIFs and odd-sized videos break without it)
    filters += ["scale=trunc(iw/2)*2:trunc(ih/2)*2", "format=yuv420p"]
    args += ["-map", "0:v:0", "-vf", ",".join(filters)]
    args += ["-an"] if mute else ["-map", "0:a:0?"]  # "?" = fine if there's no sound
    if ext == ".webm":
        args += ["-c:v", "libvpx-vp9", "-crf", str(round(50 - (quality - 1) * 35 / 99)), "-b:v", "0",
                 "-deadline", "good", "-cpu-used", "4", "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k"]
    elif ext == ".avi":
        args += ["-c:v", "mpeg4", "-q:v", str(round(31 - (quality - 1) * 29 / 99)),
                 "-c:a", "libmp3lame", "-q:a", "2"]
    else:  # MP4 / MKV / MOV: H.264 + AAC plays nearly everywhere
        args += ["-c:v", "libx264", "-crf", str(round(35 - (quality - 1) * 18 / 99)),
                 "-preset", "medium", "-c:a", "aac", "-b:a", "160k"]
        if ext in (".mp4", ".mov"):
            args += ["-movflags", "+faststart"]  # starts playing before fully downloaded
    return args + [dst]


def run_ffmpeg(owner, args, dst, duration, on_frac):
    """Run one ffmpeg job on the current (worker) thread, reporting progress 0..1.
    owner._proc / owner._cancel let the owner's Cancel button stop it.
    Returns (ok, error message). A failed or cancelled job's half-written file is deleted."""
    err_lines = []
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    except OSError as e:
        return False, str(e)
    owner._proc = proc
    if owner._cancel:  # Cancel pressed just as it started
        proc.terminate()
    # read errors on the side so a full pipe can never stall ffmpeg
    reader = threading.Thread(target=lambda: err_lines.extend(
        proc.stderr.read().decode("utf-8", "replace").splitlines()), daemon=True)
    reader.start()
    for line in proc.stdout:  # "-progress" prints key=value lines as it goes
        if line.startswith(b"out_time_us=") and duration:
            try:
                secs = int(line.split(b"=")[1]) / 1e6
            except ValueError:
                continue  # "N/A" before the first frame
            on_frac(min(max(secs / duration, 0), 1))
    proc.wait()
    reader.join()
    owner._proc = None
    if proc.returncode == 0 and not owner._cancel:
        return True, ""
    if os.path.exists(dst):  # half-written: it didn't exist before we started (unique_path)
        try:
            os.remove(dst)
        except OSError:
            pass
    return False, next((l for l in reversed(err_lines) if l.strip()), "ffmpeg failed")


def grab_frame(src, t, w, h, fill=False):
    """One frame at `t` seconds as a w x h picture: letterboxed, or cropped to fill."""
    fit = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}" if fill else
           f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                            "-ss", f"{max(t, 0):.3f}", "-i", src, "-frames:v", "1", "-vf", fit,
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=NO_WINDOW, timeout=20)
    except Exception:
        return None
    if len(r.stdout) < w * h * 3:
        return None  # e.g. asked for a time past the last frame
    return Image.frombytes("RGB", (w, h), r.stdout[:w * h * 3])


def video_thumb(src, duration, box=320):
    """A picture from early in the video (10% in, at most 1 s), fitted inside box x box.
    Comes back as a PNG through a pipe, so its size (and rotation) needn't be known up front."""
    t = min(1.0, duration * 0.1) if duration else 0
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                            "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1",
                            "-vf", f"scale={box}:{box}:force_original_aspect_ratio=decrease",
                            "-f", "image2pipe", "-vcodec", "png", "-"],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=NO_WINDOW, timeout=20)
        if r.stdout:
            return Image.open(io.BytesIO(r.stdout)).convert("RGBA")
    except Exception:
        pass
    return None


# ---- sound (the Audio tab) ----
AUDIO_FORMATS = {"MP3": ".mp3", "WAV": ".wav", "M4A (AAC)": ".m4a", "FLAC": ".flac",
                 "OGG (Vorbis)": ".ogg", "OPUS": ".opus", "AIFF": ".aiff", "WMA": ".wma"}
AUDIO_CODECS = {".mp3": "libmp3lame", ".wav": "pcm_s16le", ".m4a": "aac", ".flac": "flac",
                ".ogg": "libvorbis", ".opus": "libopus", ".aiff": "pcm_s16be", ".wma": "wmav2"}
LOSSLESS_AUDIO = {".wav", ".flac", ".aiff"}  # keep every detail: no bitrate to choose
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma",
              ".aif", ".aiff", ".amr", ".ac3", ".mka", ".m4b", ".caf", ".weba")
# videos too (not GIFs: no sound), so the sound can be taken out of them
VOICE_EXTS = AUDIO_EXTS + tuple(e for e in VIDEO_EXTS if e != ".gif")
AUDIO_BITRATES = ["Original", "320 kbps", "256 kbps", "192 kbps", "160 kbps", "128 kbps",
                  "96 kbps", "64 kbps"]
# for "Original": a file already in the chosen format is copied as-is (no re-encoding)
CODEC_EXT = {"mp3": ".mp3", "aac": ".m4a", "vorbis": ".ogg", "opus": ".opus", "flac": ".flac",
             "pcm_s16le": ".wav", "pcm_s16be": ".aiff", "wmav2": ".wma"}
AUDIO_RATES = ["Original", "48000 Hz", "44100 Hz", "32000 Hz", "22050 Hz", "16000 Hz", "8000 Hz"]
AUDIO_CHANNELS = {"Original": None, "Stereo": 2, "Mono": 1}


def audio_args(src, dst, label, kbps, rate, channels, info=None):
    """The ffmpeg command line to convert the sound of src (audio or video) into dst.
    kbps=None means "Original": keep the source's own quality (see below)."""
    ext = AUDIO_FORMATS[label]
    args = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-n", "-i", src, "-vn", "-map", "0:a:0"]
    info = info or {}
    if kbps is None:
        if CODEC_EXT.get(info.get("acodec")) == ext and not rate and not channels:
            # already this format, nothing to change: copy the sound as it is - no quality
            # lost at all, and much faster than re-encoding
            return args + ["-c:a", "copy", dst]
        # otherwise re-encode at the source's own bitrate, within what the format allows
        kbps = min(max(info.get("abitrate") or 192, 64), 320)
    args += ["-c:a", AUDIO_CODECS[ext]]
    if ext == ".ogg":
        # Vorbis is made for a quality level, not a fixed bitrate (a fixed one it refuses for
        # e.g. 16 kHz mono): use the level that sounds like the chosen bitrate
        levels = {320: 10, 256: 8, 192: 6, 160: 5, 128: 4, 96: 2, 64: 0}
        q = levels[min(levels, key=lambda k: abs(k - kbps))]
        args += ["-q:a", str(q)]
    elif ext == ".opus":  # Opus allows at most 256 kbps per channel; it's transparent by then
        args += ["-b:a", f"{min(kbps, 256)}k"]
    elif ext not in LOSSLESS_AUDIO:
        args += ["-b:a", f"{kbps}k"]
    if rate:
        args += ["-ar", str(rate)]
    if channels:
        args += ["-ac", str(channels)]
    return args + [dst]


def audio_wave_thumb(src, box=320):
    """A picture of the sound: its waveform in XP blue, for the thumbnail view."""
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src,
                            "-filter_complex",
                            # peaks (not averages, which flatten everything) on a square-root
                            # scale, like media players, so quiet parts still show
                            f"[0:a:0]aformat=channel_layouts=mono,"
                            f"showwavespic=s={box}x{box * 9 // 16}:colors=#316AC5"
                            f":filter=peak:scale=sqrt",
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=NO_WINDOW, timeout=60)
        if r.stdout:
            return Image.open(io.BytesIO(r.stdout)).convert("RGBA")
    except Exception:
        pass
    return None


def unique_path(folder, base, ext):
    """folder/base+ext, or base_converted1, 2... so an existing file is never overwritten."""
    dst, n = os.path.join(folder, base + ext), 1
    while os.path.exists(dst):
        dst = os.path.join(folder, f"{base}_converted{n}{ext}")
        n += 1
    return dst


CAPTION_ACTIVE = ("#000085", "#0000AF")  # navy, a touch lighter to the right (from the reference)
CAPTION_INACTIVE = ("#808080", "#A8A8A8")


class ClassicWindow:
    """Replaces Windows' own title bar with a classic one: navy bar, bold white title, no icon,
    small raised _ / [] / X buttons, and a 3D window border. Windows' bar is removed
    (overrideredirect), so this also does its jobs: move by dragging the bar, double-click to
    maximize, minimize to the taskbar, resize from the border, grey bar when not active.
    Other systems keep their normal title bar. Put the window's contents in .body."""
    TITLE_H, BTN_W, BTN_H, GRIP, CORNER = 26, 21, 19, 5, 16

    def __init__(self, win, title, on_close, resizable=True, min_size=(240, 120), taskbar=True):
        self.win, self.title, self.on_close = win, title, on_close
        self.resizable, self.min_size = resizable, min_size
        self.maximized, self.normal_geo, self.active = False, None, True
        self.pressed, self.down = None, False  # title-bar button held down, and shown down?
        self.on_update = None  # set_update_button: shows the green "update" button when set
        self._move, self._resize, self._grad = None, None, None
        win.title(title)  # still shown on the taskbar and in Alt+Tab
        if sys.platform != "win32":
            self.body = tk.Frame(win, bg=BG)
            self.body.pack(fill="both", expand=True)
            return
        win.overrideredirect(True)
        # 3D border from nested frames, each showing its colour on two sides:
        # outside: light-grey top/left, black bottom/right; inside: white top/left, grey bottom/right
        edges = []
        parent = win
        for bg, padx, pady in ((EDGE_DARK, 0, 0), (BG, (0, 1), (0, 1)), (EDGE_SHADOW, (1, 0), (1, 0)),
                               (EDGE_LIGHT, (0, 1), (0, 1)), (BG, (1, 0), (1, 0))):
            f = tk.Frame(parent, bg=bg)
            f.pack(fill="both", expand=True, padx=padx, pady=pady)
            edges.append(f)
            parent = f
        # width=1: the bar stretches to the window instead of setting its width
        # cursor="arrow": widgets without their own cursor take their parent's, so without this
        # everything inside would pick up the border's resize cursor
        self.bar = tk.Canvas(parent, height=self.TITLE_H, width=1, highlightthickness=0,
                             bg=CAPTION_ACTIVE[0], cursor="arrow")
        self.bar.pack(fill="x", padx=1, pady=1)
        self.body = tk.Frame(parent, bg=BG, cursor="arrow")
        self.body.pack(fill="both", expand=True, padx=1, pady=(0, 1))

        self.bar.bind("<Configure>", lambda e: self.draw())
        self.bar.bind("<ButtonPress-1>", self.bar_press)
        self.bar.bind("<B1-Motion>", self.bar_drag)
        self.bar.bind("<ButtonRelease-1>", self.bar_release)
        self.bar.bind("<Double-Button-1>", self.bar_double)
        self.edges = edges
        if resizable:  # the border is handled by watching the mouse (see watch_edges)
            self._zone_shown, self._was_down = "", False
            win.after(100, self.watch_edges)
        win.bind("<FocusIn>", lambda e: self.set_active(True), add="+")
        win.bind("<FocusOut>", lambda e: win.after(30, self.check_active), add="+")
        win.bind("<Alt-F4>", lambda e: self.on_close())
        if taskbar:
            win.after(1, self.show_on_taskbar)
        else:  # a dialog: hand the keyboard (and the blue title) back to its window when it closes
            win.bind("<Destroy>", lambda e: e.widget is win and self.give_back_focus(), add="+")
            win.after(1, self.stay_above_owner)

    def stay_above_owner(self):
        """Without Windows' title bar, Tk no longer tells Windows who owns a dialog, so it could
        open behind the main window. Set the owner so it always stays in front of it."""
        try:
            import ctypes
            self.win.update_idletasks()
            owner = ctypes.windll.user32.GetParent(self.win.master.winfo_toplevel().winfo_id())
            ctypes.windll.user32.SetWindowLongPtrW(self.hwnd(), -8, owner)  # GWLP_HWNDPARENT
            self.win.lift()
        except Exception:
            pass

    def give_back_focus(self):
        try:
            self.win.master.focus_force()
        except tk.TclError:
            pass  # the whole app is closing

    # ---- Windows plumbing ----
    def hwnd(self):
        import ctypes
        return ctypes.windll.user32.GetParent(self.win.winfo_id())

    def show_on_taskbar(self):
        """Windows without a title bar are left off the taskbar; put this one back on it,
        with a minimize box so clicking its taskbar button minimizes/restores it."""
        try:
            import ctypes
            self.win.update_idletasks()
            u, h = ctypes.windll.user32, self.hwnd()
            ex = u.GetWindowLongW(h, -20)  # GWL_EXSTYLE
            u.SetWindowLongW(h, -20, (ex & ~0x80) | 0x40000)  # - TOOLWINDOW + APPWINDOW
            st = u.GetWindowLongW(h, -16)  # GWL_STYLE
            u.SetWindowLongW(h, -16, st | 0x20000 | 0x80000)  # + MINIMIZEBOX + SYSMENU
            # re-show so the taskbar notices - through Windows directly, because Tk's own
            # withdraw/deiconify would put the old style back
            u.ShowWindow(h, 0)  # SW_HIDE
            u.ShowWindow(h, 5)  # SW_SHOW
        except Exception:
            pass

    def minimize(self):
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(self.hwnd(), 6)  # SW_MINIMIZE
        except Exception:
            pass

    def work_area(self):
        """Screen area not covered by the taskbar: x, y, w, h."""
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
            return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            return 0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight()

    def toggle_maximize(self):
        if not self.resizable:
            return
        if self.maximized:
            self.win.geometry(self.normal_geo)
        else:
            self.normal_geo = self.win.geometry()
            x, y, w, h = self.work_area()
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.maximized = not self.maximized
        self.draw()

    # ---- active / inactive ----
    def check_active(self):
        try:
            f = self.win.focus_get()
            top = f.winfo_toplevel() if f is not None else None
            # its own right-click menu counts as still in the window, like Windows' menus
            self.set_active(top is self.win or (
                getattr(top, "keeps_owner_active", False) and top.master.winfo_toplevel() is self.win))
        except (KeyError, tk.TclError):
            pass

    def set_active(self, active):
        if active != self.active:
            self.active = active
            self.draw()

    def set_update_button(self, on_click):
        """Show a green download-arrow button left of _ that calls on_click (None hides it)."""
        self.on_update = on_click
        if hasattr(self, "bar"):
            self.draw()

    # ---- drawing ----
    def buttons(self):
        """[(kind, x0, y0)] from the right: X, [] and _ side by side with no gaps; X keeps
        the same distance from the bar's right edge as from its top (3 px). The update button,
        when shown, sits a little apart to the left of them."""
        W = self.bar.winfo_width()
        margin = (self.TITLE_H - self.BTN_H) // 2  # 3 px from the bar's right edge
        y = margin + 1  # 1 px lower than exactly centred: 4 px above, 3 below
        x = W - margin - self.BTN_W
        out = [("close", x, y)]
        if self.resizable:
            x -= self.BTN_W
            out.append(("restore" if self.maximized else "max", x, y))
            x -= self.BTN_W
            out.append(("min", x, y))
        if self.on_update:
            out.append(("update", x - 8 - self.BTN_W, y))  # 8 px gap before _
        return out

    def draw(self):
        c, W, H = self.bar, self.bar.winfo_width(), self.TITLE_H
        c.delete("all")
        a, b = CAPTION_ACTIVE if self.active else CAPTION_INACTIVE
        key = (W, a)
        if self._grad is None or self._grad[0] != key:
            ramp = Image.new("RGB", (256, 1))
            ca, cb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
            ramp.putdata([tuple(round(p + (q - p) * i / 255) for p, q in zip(ca, cb))
                          for i in range(256)])
            self._grad = (key, ImageTk.PhotoImage(ramp.resize((max(W, 1), H))))
        c.create_image(0, 0, image=self._grad[1], anchor="nw")
        c.create_text(6, H // 2, text=self.title, anchor="w", font=("Tahoma", 10, "bold"),
                      fill="#FFFFFF" if self.active else "#D4D0C8")
        for kind, x, y in self.buttons():
            self.draw_button(kind, x, y, self.pressed == kind and self.down)

    def draw_button(self, kind, x, y, down):
        c, w, h = self.bar, self.BTN_W, self.BTN_H
        c.create_rectangle(x, y, x + w, y + h, fill=BG, outline="")
        tl, br = (EDGE_DARK, EDGE_LIGHT) if down else (EDGE_LIGHT, EDGE_DARK)
        c.create_line(x, y + h - 1, x, y, x + w - 1, y, fill=tl)  # outer edge
        c.create_line(x, y + h - 1, x + w - 1, y + h - 1, x + w - 1, y - 1, fill=br)
        if down:
            c.create_line(x + 1, y + h - 2, x + 1, y + 1, x + w - 2, y + 1, fill=EDGE_SHADOW)
        else:
            c.create_line(x + 1, y + h - 2, x + w - 2, y + h - 2, x + w - 2, y, fill=EDGE_SHADOW)
        o = 1 if down else 0  # the symbol shifts when pressed, like a real button
        x, y = x + o, y + o

        def px(x0, y0, x1, y1):  # filled black block [x0, x1) x [y0, y1)
            c.create_rectangle(x + x0, y + y0, x + x1, y + y1, fill="#000000", outline="")
        # symbols drawn pixel by pixel for a 21 x 19 button, so they stay crisp. The face inside
        # the 3D edge is x 1-18, y 1-16, so its middle is (9.5, 8.5); every symbol is centred
        # on that and ends on the same bottom line (y 13), like classic Windows lines up _ and []
        if kind == "min":
            px(6, 12, 14, 14)  # x 6-13, on the bottom line
        elif kind == "max":
            px(5, 4, 15, 6)  # thick top, like the classic one; x 5-14, y 4-13
            px(5, 6, 6, 14)
            px(14, 6, 15, 14)
            px(5, 13, 15, 14)
        elif kind == "restore":  # two overlapping windows, x 4-15, y 3-13
            px(8, 3, 16, 5)  # back window
            px(15, 5, 16, 11)
            px(13, 10, 15, 11)
            px(8, 5, 9, 7)
            px(4, 7, 13, 9)  # front window
            px(4, 9, 5, 14)
            px(12, 9, 13, 14)
            px(4, 13, 13, 14)
        elif kind == "update":  # arrow down onto a line, x 5-14, y 2-13, 2-px strokes like X
            px(9, 2, 11, 9)  # shaft, ending inside the head so the tip stays sharp
            for i in range(5):  # head: drawn with the X's strokes
                px(5 + i, 6 + i, 7 + i, 7 + i)
                px(13 - i, 6 + i, 15 - i, 7 + i)
            px(6, 12, 14, 14)  # the line: the same as _
        else:  # close: a 2-px-thick X, x 5-14, y 5-13
            for i in range(9):
                px(5 + i, 5 + i, 7 + i, 6 + i)
                px(13 - i, 5 + i, 15 - i, 6 + i)

    # ---- title bar mouse ----
    def button_at(self, x, y):
        return next((k for k, bx, by in self.buttons()
                     if bx <= x < bx + self.BTN_W and by <= y < by + self.BTN_H), None)

    def bar_press(self, e):
        kind = self.button_at(e.x, e.y)
        if kind:
            self.pressed, self.down = kind, True
            self.draw()
        elif not self.maximized:  # start moving the window
            self._move = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def bar_drag(self, e):
        if self._move:
            self.win.geometry(f"+{e.x_root - self._move[0]}+{e.y_root - self._move[1]}")
        elif self.pressed:  # like a real button: pops back up if the mouse slides off it
            down = self.button_at(e.x, e.y) == self.pressed
            if down != self.down:
                self.down = down
                self.draw()

    def bar_release(self, e):
        self._move = None
        kind, self.pressed = self.pressed, None
        if kind:
            self.draw()
            if self.button_at(e.x, e.y) == kind:  # released on the same button: do it
                {"close": self.on_close, "min": self.minimize, "max": self.toggle_maximize,
                 "restore": self.toggle_maximize, "update": self.on_update}[kind]()

    def bar_double(self, e):
        if not self.button_at(e.x, e.y):
            self.toggle_maximize()

    # ---- resizing from the border ----
    # Windows' own resize cursors: IDC_SIZENS, IDC_SIZEWE, IDC_SIZENWSE, IDC_SIZENESW
    SIZE_CURSORS = {"n": 32645, "s": 32645, "e": 32644, "w": 32644,
                    "nw": 32642, "se": 32642, "ne": 32643, "sw": 32643}
    TK_CURSORS = {"n": "size_ns", "s": "size_ns", "e": "size_we", "w": "size_we",  # the same
                  "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw"}

    def watch_edges(self):
        """Tk doesn't reliably tell the border frames when the mouse is on them (moving out
        from the inside, they're never 'entered'), which left the resize cursor stuck and made
        the right/bottom edges unclickable. So the border asks Windows directly, about 30 times
        a second: where is the mouse, and is the left button down?"""
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            if not self.win.winfo_exists():
                return
        except (tk.TclError, Exception):
            return
        u.LoadCursorW.argtypes, u.LoadCursorW.restype = [wintypes.HINSTANCE, ctypes.c_void_p], ctypes.c_void_p
        u.SetCursor.argtypes = [ctypes.c_void_p]
        left = 0x02 if u.GetSystemMetrics(23) else 0x01  # the "left" button, even if swapped
        state = u.GetAsyncKeyState(left)
        down = bool(state & 0x8000)
        px, py = self.win.winfo_pointerxy()
        if self._resize:
            if down:
                self.resize_to(px, py)
            else:
                self._resize = None
        else:
            z = "" if self.maximized or not self.pointer_on_window() else self.zone()
            if z:
                u.SetCursor(u.LoadCursorW(None, self.SIZE_CURSORS[z]))
                if (down or state & 1) and not self._was_down:  # pressed on the border
                    w = self.win
                    self._resize = (z, px, py, w.winfo_x(), w.winfo_y(),
                                    w.winfo_width(), w.winfo_height())
            elif self._zone_shown:  # just left the border: normal arrow back
                u.SetCursor(u.LoadCursorW(None, 32512))  # IDC_ARROW
            if z != self._zone_shown:  # tell Tk the same, so it never sets a different one
                for f in self.edges:
                    f.config(cursor=self.TK_CURSORS.get(z, ""))
            self._zone_shown = z
        self._was_down = down
        self.win.after(15 if self._resize else 30, self.watch_edges)

    def pointer_on_window(self):
        """Is the mouse over this window (and not over another window covering it)?"""
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        return u.GetAncestor(u.WindowFromPoint(pt), 2) == self.hwnd()  # GA_ROOT

    def zone(self):
        w = self.win
        x, y = w.winfo_pointerx() - w.winfo_rootx(), w.winfo_pointery() - w.winfo_rooty()
        W, H, G, C = w.winfo_width(), w.winfo_height(), self.GRIP, self.CORNER
        v = "n" if y < G else "s" if y >= H - G else ""
        h = "w" if x < G else "e" if x >= W - G else ""
        if v and not h:  # near a corner along the top/bottom edge
            h = "w" if x < C else "e" if x >= W - C else ""
        if h and not v:
            v = "n" if y < C else "s" if y >= H - C else ""
        return v + h

    def resize_to(self, mx, my):
        """Follow the mouse (at mx, my) with the edge/corner grabbed in self._resize."""
        z, px, py, x, y, W, H = self._resize
        dx, dy = mx - px, my - py
        mw, mh = self.min_size
        if "e" in z:
            W = max(mw, W + dx)
        if "s" in z:
            H = max(mh, H + dy)
        if "w" in z:
            nw = max(mw, W - dx)
            x, W = x + W - nw, nw
        if "n" in z:
            nh = max(mh, H - dy)
            y, H = y + H - nh, nh
        self.win.geometry(f"{W}x{H}+{x}+{y}")


class ClassicProgress(tk.Canvas):
    """Classic Windows progress bar: a thin sunken box that fills left to right with navy
    blocks, one whole block at a time. Takes value= / maximum= like ttk.Progressbar."""
    H, BLOCK, GAP, PAD = 20, 8, 2, 2
    COLOR = "#122976"  # sampled from the reference

    def __init__(self, parent, maximum=100, value=0):
        super().__init__(parent, height=self.H, width=1, bg=BG, highlightthickness=0)
        self._max, self._value, self._shown = float(maximum), float(value), None
        self.bind("<Configure>", lambda e: self.draw(force=True))

    def configure(self, cnf=None, **kw):
        if "maximum" in kw:
            self._max = float(kw.pop("maximum"))
        if "value" in kw:
            self._value = float(kw.pop("value"))
        if cnf or kw:
            super().configure(cnf, **kw)
        self.draw()

    config = configure

    def draw(self, force=False):
        W, H = self.winfo_width(), self.winfo_height()
        x0, x1 = 1 + self.PAD, W - 1 - self.PAD
        step = self.BLOCK + self.GAP
        total = max(1, (x1 - x0 + self.GAP) // step)
        frac = min(max(self._value / self._max, 0), 1) if self._max > 0 else 0
        n = int(frac * total + 1e-9)  # whole blocks only, like the real thing
        if n == self._shown and not force:
            return  # nothing visible changed: skip redrawing
        self._shown = n
        self.delete("all")
        self.create_line(0, H - 1, 0, 0, W - 1, 0, fill=EDGE_SHADOW)  # sunken edge
        self.create_line(1, H - 1, W - 1, H - 1, W - 1, 0, fill=EDGE_LIGHT)
        for i in range(n):
            bx = x0 + i * step
            self.create_rectangle(bx, 1 + self.PAD, bx + self.BLOCK, H - 1 - self.PAD,
                                  fill=self.COLOR, outline="")


def xp_button(parent, text, cmd, bold=False):
    """The app's raised button (Add, Convert, OK...)."""
    return tk.Button(parent, text=text, command=cmd, bg=BG, relief="raised", bd=3,
                     font=("Tahoma", 9, "bold" if bold else "normal"),
                     activebackground="#F5F3E8", padx=8)


# ---- sounds: original XP-style chimes, made by the app itself (no sound files shipped) ----
def make_sound(kind, rate=44100):
    """WAV bytes for "done" (a soft bell-like chime going up) or "error" (a short, warm, low
    chord going down), with a little room echo - in the spirit of Windows XP's sounds."""
    import numpy as np
    import wave
    if kind == "done":  # (start s, frequencies Hz), bright and gentle
        notes, length, decay = [(0.00, (784.0,)), (0.11, (1174.7,))], 1.0, 5.0
    else:  # a fifth, then a lower fifth
        notes, length, decay = [(0.00, (329.6, 493.9)), (0.13, (220.0, 329.6))], 1.0, 6.0
    t = np.arange(int(rate * length)) / rate
    out = np.zeros_like(t)
    for start, freqs in notes:
        tt = np.clip(t - start, 0, None)
        on = t >= start
        for i, f in enumerate(freqs):
            level = 1.0 if i == 0 else 0.45
            # bell-like: the note plus quieter overtones that fade faster
            tone = (np.sin(2 * np.pi * f * tt)
                    + 0.35 * np.sin(2 * np.pi * 2 * f * tt) * np.exp(-tt * decay * 1.5)
                    + 0.12 * np.sin(2 * np.pi * 3 * f * tt) * np.exp(-tt * decay * 3))
            out += on * level * tone * np.exp(-tt * decay) * np.minimum(tt / 0.004, 1)  # soft start
    for delay, gain in ((0.045, 0.25), (0.09, 0.14), (0.14, 0.08)):  # small-room echo
        d = int(rate * delay)
        out[d:] += gain * out[:-d]
    out *= 0.45 / np.max(np.abs(out))  # comfortable loudness, never clipping
    out *= np.minimum((len(t) - np.arange(len(t))) / (rate * 0.05), 1)  # fade out the tail
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((out * 32767).astype("<i2").tobytes())
    return buf.getvalue()


_SOUNDS = {}


def play_sound(kind):
    """Play "done" or "error" without waiting for it (Windows only; silently skipped if the
    sound can't play, e.g. no speakers)."""
    if sys.platform != "win32" or kind is None:
        return

    def go():
        try:
            import winsound
            if kind not in _SOUNDS:
                _SOUNDS[kind] = make_sound(kind)
            winsound.PlaySound(_SOUNDS[kind], winsound.SND_MEMORY)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def dialog(title, message, buttons=("OK",), parent=None, sound=None):
    """Message box in the app's own style (same look as the Rename box), used instead of
    Windows' standard ones. Waits for an answer and returns the clicked button's text
    (None if closed). Enter = first button; Escape = No/Cancel if there is one, else close."""
    parent = parent or tk._default_root
    win = tk.Toplevel(parent)
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(parent)
    result = [None]

    def choose(text):
        result[0] = text
        win.destroy()

    cancel = next((t for t in buttons if t in ("No", "Cancel")), None)
    body = ClassicWindow(win, title, lambda: choose(cancel), resizable=False, taskbar=False).body
    tk.Frame(body, bg=BG, width=280, height=0).pack()  # keeps short messages from looking cramped
    tk.Label(body, text=message, bg=BG, font=FONT, justify="left", wraplength=420
             ).pack(anchor="w", padx=12, pady=(12, 8))  # same margins as the Rename box
    row = tk.Frame(body, bg=BG)
    row.pack(pady=(4, 12))
    btns = [xp_button(row, text, lambda t=text: choose(t)) for text in buttons]
    for b in btns:
        b.pack(side="left", padx=4)
    win.bind("<Return>", lambda e: choose(buttons[0]))
    win.bind("<Escape>", lambda e: choose(cancel))

    win.update_idletasks()  # centre over the main window (or the screen if it's hidden)
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    if parent.winfo_ismapped():
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
    else:
        x, y = (win.winfo_screenwidth() - w) // 2, (win.winfo_screenheight() - h) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.focus_force()  # windows without Windows' title bar don't take the keyboard by themselves
    btns[0].focus_set()
    win.grab_set()
    play_sound(sound)  # "done" / "error", as the box appears
    win.wait_window()
    return result[0]


class PopupMenu:
    """Right-click menu drawn by the app itself, laid out like Windows' own (1 px grey outline,
    2 px inner frame, 21 px items, a line between groups, blue highlight). Windows' menus
    draw their frame in the system's light colour, which in dark mode showed as a thick
    white border; this one is coloured like everything else, so it's the same in both modes.
    Same calls as tk.Menu: add_command(label=, command=), add_separator(), tk_popup(x, y)."""

    def __init__(self, parent):
        self.parent = parent.winfo_toplevel()
        self.items = []
        self.top = None

    def add_command(self, label, command):
        self.items.append((label, command))

    def add_separator(self):
        self.items.append(None)

    def tk_popup(self, x, y):
        top = self.top = tk.Toplevel(self.parent, bg="#A0A0A0")  # the 1 px outline
        top.keeps_owner_active = True  # its window stays "active" (blue title) while it's open
        top.withdraw()
        top.overrideredirect(True)
        top.transient(self.parent)
        inner = tk.Frame(top, bg="#F0F0F0")  # the 2 px inner frame
        inner.pack(padx=1, pady=1)
        body = tk.Frame(inner, bg=BG)
        body.pack(padx=2, pady=2)
        for item in self.items:
            if item is None:  # etched line: grey over white
                tk.Frame(body, bg="#A0A0A0", height=1).pack(fill="x", padx=1, pady=(3, 0))
                tk.Frame(body, bg="#FFFFFF", height=1).pack(fill="x", padx=1, pady=(0, 3))
                continue
            label, command = item
            row = tk.Label(body, text=label, bg=BG, fg="black", font=FONT, anchor="w",
                           padx=22, pady=3)
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, r=row: r.config(bg="#316AC5", fg="white"))
            row.bind("<Leave>", lambda e, r=row: r.config(bg=BG, fg="black"))
            row.bind("<ButtonRelease-1>", lambda e, c=command: self.choose(c))
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = min(x, top.winfo_screenwidth() - w - 2)  # keep it on the screen
        y = y if y + h <= top.winfo_screenheight() else y - h
        top.geometry(f"+{max(0, x)}+{max(0, y)}")
        top.deiconify()
        top.lift()
        top.attributes("-topmost", True)
        top.focus_force()
        top.grab_set()  # clicks anywhere come here: outside the menu closes it
        top.bind("<ButtonPress>", self.on_press)
        top.bind("<Escape>", lambda e: self.close())
        top.bind("<FocusOut>", lambda e: top.after(50, self.check_focus))
        top.after(100, self.watch_foreground)

    def watch_foreground(self):
        """While open, ask Windows ~10 times a second whether this app is still in front: a
        popup like this isn't reliably told when another program takes over, and it would
        otherwise stay floating on top of that program."""
        if not self.top:
            return
        if sys.platform == "win32":
            import ctypes
            u, pid = ctypes.windll.user32, ctypes.c_ulong()
            u.GetWindowThreadProcessId(u.GetForegroundWindow(), ctypes.byref(pid))
            if pid.value != os.getpid():  # another program is in front now
                self.close(give_back=False)
                return
        self.top.after(100, self.watch_foreground)

    def on_press(self, e):
        top = self.top
        inside = (top.winfo_rootx() <= e.x_root < top.winfo_rootx() + top.winfo_width()
                  and top.winfo_rooty() <= e.y_root < top.winfo_rooty() + top.winfo_height())
        if not inside:
            self.close()

    def check_focus(self):  # switched to another program: close, like a real menu
        try:
            f = self.top.focus_get() if self.top else None
        except (KeyError, tk.TclError):
            f = None
        if self.top and (f is None or f.winfo_toplevel() is not self.top):
            self.close(give_back=False)  # the other program keeps the focus

    def choose(self, command):
        self.close()
        command()

    def close(self, give_back=True):
        """Close the menu and hand the keyboard back to its window - the menu had it (for
        Escape), and without this the window stayed greyed out as if it had lost focus."""
        if self.top:
            top, self.top = self.top, None
            top.grab_release()
            top.destroy()
            try:
                if give_back:
                    self.parent.focus_force()
                else:  # another program took over: let the window grey out its title bar
                    self.parent.event_generate("<FocusOut>")
            except tk.TclError:
                pass

    def grab_release(self):  # (tk.Menu has it; nothing to do here)
        pass


def rename_dialog(parent, current, apply, is_busy=lambda: False):
    """The Rename box (right-click > Rename, on any tab): asks for the converted file's new
    name, checks Windows would accept it as a file name, then calls apply(name)."""
    owner = parent.winfo_toplevel()
    win = tk.Toplevel(owner)
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(owner)
    body = ClassicWindow(win, "Rename", win.destroy, resizable=False, taskbar=False).body
    tk.Label(body, text="New name for the converted file\n(the file type is added automatically):",
             bg=BG, font=FONT, justify="left").pack(anchor="w", padx=12, pady=(12, 4))
    var = tk.StringVar(value=current)
    entry = tk.Entry(body, textvariable=var, font=FONT, width=38, relief="sunken", bd=2)
    entry.pack(padx=12)
    err = tk.Label(body, text="", bg=BG, fg="#C00000", font=FONT)
    err.pack(anchor="w", padx=12)

    def ok(_=None):
        name = var.get().strip().rstrip(".")
        if not name:
            err.config(text="The name can't be empty.")
        elif any(ch in name for ch in '\\/:*?"<>|'):
            err.config(text='A name can\'t contain any of:  \\ / : * ? " < > |')
        elif name.split(".")[0].strip().upper() in RESERVED:
            err.config(text=f'Windows doesn\'t allow "{name}" as a file name.')
        elif is_busy():
            err.config(text="Wait for the conversion to finish.")
        else:
            win.destroy()
            apply(name)

    row = tk.Frame(body, bg=BG)
    row.pack(pady=(4, 12))
    xp_button(row, "OK", ok).pack(side="left", padx=4)
    xp_button(row, "Cancel", win.destroy).pack(side="left", padx=4)
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda e: win.destroy())

    win.update_idletasks()  # centre over the main window
    x = owner.winfo_rootx() + (owner.winfo_width() - win.winfo_reqwidth()) // 2
    y = owner.winfo_rooty() + (owner.winfo_height() - win.winfo_reqheight()) // 3
    win.geometry(f"+{x}+{y}")
    win.focus_force()  # windows without Windows' title bar don't take the keyboard by themselves
    entry.focus_set()
    entry.select_range(0, "end")
    win.grab_set()
    return win


def copy_name(base, taken):
    """Name for a duplicate: "clip - Copy", or "clip - Copy (2)", (3)... if that's taken."""
    name, n = f"{base} - Copy", 2
    while name in taken:
        name = f"{base} - Copy ({n})"
        n += 1
    return name


def status_label(parent, text):
    """Status line beside a button: wraps onto more lines instead of running under the button.
    width=1 stops the text from asking for more room than is left; the space it really gets
    comes from expand, and wraplength follows that space as the window is resized."""
    label = tk.Label(parent, text=text, bg=BG, font=FONT, anchor="w", justify="left", width=1)
    label.pack(side="left", fill="x", expand=True)
    label.bind("<Configure>", lambda e: label.config(wraplength=max(40, e.width - 4)))
    return label


def go_to_file(path):
    """Right-click > Go to file: open the original file's folder in Explorer with the file
    highlighted (like Windows' "Open file location")."""
    if os.path.exists(path):
        reveal(os.path.dirname(os.path.abspath(path)), [os.path.abspath(path)])
    else:
        dialog("Master Converter", f"{os.path.basename(path)} isn't there anymore "
               "(moved or deleted).", sound="error")


def reveal_all(paths):
    """Open the folder(s) holding `paths` with those files highlighted."""
    by_folder = {}
    for p in paths:
        if os.path.exists(p):  # skip files moved/deleted since
            by_folder.setdefault(os.path.dirname(p), []).append(p)
    if not by_folder:
        dialog("Master Converter", "The converted files aren't there anymore (moved or deleted).",
               sound="error")
        return
    for folder, files in list(by_folder.items())[:5]:  # don't flood the screen with windows
        reveal(folder, files)


def convert_one(src, dst, fmt, quality, keep_exif=True, ico_sizes=None):
    """Convert src into dst; returns the path actually written (RAW pixel data puts the
    picture's size in the file name, since the file itself can't say it)."""
    im = open_image(src)
    exif = im.getexif()
    exif.pop(0x0112, None)  # rotation is already baked into the pixels
    opts = {}
    if keep_exif and fmt in EXIF_OK and len(exif):
        opts["exif"] = exif.tobytes()
    if im.mode not in SAFE_MODES:  # CMYK, 16-bit, float...: most writers refuse these
        target = "RGBA" if "A" in im.getbands() else "RGB"
        try:
            im = im.convert(target)
        except ValueError:  # no direct route (e.g. float): go through greyscale
            im = im.convert("L").convert(target)
        im.info.pop("icc_profile", None)  # the old colour profile no longer matches
    has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    if fmt == "PIXELS":
        return save_pixels(im.convert("RGBA" if has_alpha else "RGB"), dst)
    if fmt == "SVG":
        return save_svg(im, dst, quality, has_alpha)
    if fmt in ("DDS", "JPEG2000") and im.mode not in ("RGB", "RGBA"):  # no palette support
        im = im.convert("RGBA")
    if fmt in NO_ALPHA:
        if has_alpha:
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
    if fmt in LOSSY:
        opts["quality"] = quality
    if fmt == "ICO":
        return save_ico(im, dst, ico_sizes)
    im.save(dst, fmt, **opts)
    return dst


def ico_bitmap(frame):
    """One icon picture in the classic icon format: a bitmap header (height doubled, as icons
    require), the colour pixels bottom-up (32-bit, with transparency), then the 1-bit
    see-through mask that older programs use."""
    s = frame.width
    header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = frame.transpose(Image.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")
    row_bytes = ((s + 31) // 32) * 4  # mask rows are padded to 4 bytes
    alpha = frame.getchannel("A").transpose(Image.FLIP_TOP_BOTTOM).tobytes()
    mask = bytearray()
    for y in range(s):
        row = bytearray(row_bytes)
        for x in range(s):
            if alpha[y * s + x] == 0:  # fully see-through
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row
    return header + pixels + bytes(mask)


ICO_SIZES = {  # the ICO "quality" list: which size(s) the icon holds
    "All sizes (16-256 px)": [256, 128, 64, 48, 32, 24, 16],
    **{f"{s} x {s}": [s] for s in (256, 128, 64, 48, 32, 24, 16)}}


def save_ico(im, dst, sizes=None):
    """An icon holding the picture at the given sizes - by default all of 256, 128, 64, 48,
    32, 24 and 16 px (the sizes Windows uses), largest first.
    Written here rather than by Pillow, whose icons Windows reports as 16 x 16 (Explorer's
    Dimensions, and the first picture Photos and other programs get) - so they looked like
    they'd lost all their detail. Tested on Windows: only with every size in the classic
    icon format and the largest first does it report and show the 256 px picture. That format
    isn't compressed, so the file is bigger (~370 KB for all sizes), but it works in every
    program, even Windows XP. Each size is made straight from the full-size original
    (sharpest), and a non-square picture is centred on a transparent square."""
    im = im.convert("RGBA")
    w, h = im.size
    side = max(w, h)
    if w != h:
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(im, ((side - w) // 2, (side - h) // 2))
        im = square
    # by default the full standard set, 256 first: tested on Windows, it only reliably reports
    # and shows the big picture when there's a 256 one first (with other sets it picks a small
    # one). A picture under a size is enlarged for it - that can't add detail it doesn't have,
    # but it's the sharpest the icon can be
    sizes = sorted(sizes or ICO_SIZES["All sizes (16-256 px)"], reverse=True)
    frames = []
    for s in sizes:
        frame = im if s == side else im.resize((s, s), Image.LANCZOS)
        frames.append((s, ico_bitmap(frame)))
    # ICO file: 6-byte header, one 16-byte entry per picture, then the pictures
    out = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    for s, data in frames:  # width/height 256 is written as 0
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(dst, "wb") as f:
        f.write(out + b"".join(data for _, data in frames))
    return dst


def save_pixels(im, dst):
    """RAW pixel data: just the pixels, row by row, 8 bits per channel, no header (what
    Photoshop calls "Raw"). A reader must be told the size and channels, so they go in the
    name: photo.raw -> photo_1920x1080_rgb.raw."""
    folder, name = os.path.split(dst)
    w, h = im.size
    real = unique_path(folder, f"{os.path.splitext(name)[0]}_{w}x{h}_{im.mode.lower()}", ".raw")
    with open(real, "wb") as f:
        f.write(im.tobytes())
    return real


def save_svg(im, dst, quality, has_alpha):
    """SVG with the picture inside at full size. Opaque pictures go in as JPEG (the quality
    slider applies); transparent ones as PNG, which keeps the transparency."""
    buf = io.BytesIO()
    if has_alpha:
        im.convert("RGBA").save(buf, "PNG", optimize=True)
        mime = "image/png"
    else:
        im.convert("RGB").save(buf, "JPEG", quality=quality)
        mime = "image/jpeg"
    w, h = im.size
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    with open(dst, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'width="{w}" height="{h}" viewBox="0 0 {w} {h}">\n'
                f'  <image width="{w}" height="{h}" xlink:href="data:{mime};base64,{data}"/>\n'
                '</svg>\n')
    return dst


def reveal(folder, files):
    """Open `folder` in the file manager; on Windows the given files come up selected."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            shell32 = ctypes.windll.shell32
            shell32.ILCreateFromPathW.restype = ctypes.c_void_p
            shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
            shell32.ILFree.argtypes = [ctypes.c_void_p]
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD]
            ctypes.windll.ole32.CoInitialize(None)
            parent = shell32.ILCreateFromPathW(os.path.normpath(folder))
            items = [i for i in (shell32.ILCreateFromPathW(os.path.normpath(f)) for f in files) if i]
            try:
                if parent and items:
                    arr = (ctypes.c_void_p * len(items))(*items)
                    if shell32.SHOpenFolderAndSelectItems(parent, len(items), arr, 0) == 0:
                        return
            finally:
                for pidl in items + [parent]:
                    if pidl:
                        shell32.ILFree(pidl)
        except Exception:
            pass
        os.startfile(folder)  # fallback: just open the folder
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])


def fit_text(font, text, max_px):
    """Shorten text with '...' so it fits in max_px."""
    if font.measure(text) <= max_px:
        return text
    while len(text) > 1 and font.measure(text + "...") > max_px:
        text = text[:-1]
    return text + "..."


def fmt_size(n):
    n = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n:.2f} {unit}"
        n /= 1024


class FlatScrollbar(tk.Canvas):
    """Vertical scrollbar in the modern flat Windows style: light-grey track, small chevron
    arrows without button boxes, a flat grey thumb that darkens on hover and while dragged.
    Drop-in for tk.Scrollbar: command=widget.yview, and the widget's yscrollcommand=sb.set."""
    W, ARROW = 17, 17
    TRACK, THUMB, THUMB_HOVER, THUMB_DOWN = "#EFEFEF", "#CCCCCC", "#A6A6A6", "#606060"
    ARROW_FG, ARROW_HOVER_BG = "#5F5F5F", "#DADADA"

    def __init__(self, parent, command):
        super().__init__(parent, width=self.W, bg=self.TRACK, highlightthickness=0, bd=0)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.hot = None  # part under the mouse: "up", "down", "thumb" or None
        self.held = None  # part being pressed
        self._drag, self._repeat = None, None
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Motion>", lambda e: self.set_hot(self.part_at(e.y)))
        self.bind("<Leave>", lambda e: self.set_hot(None))
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<MouseWheel>", lambda e: e.delta and self.command(
            "scroll", -int(e.delta / 120) or (-1 if e.delta > 0 else 1), "units"))

    def set(self, first, last):  # called by the scrolled widget
        self.first, self.last = float(first), float(last)
        self.draw()

    def thumb_box(self):
        """y0, y1 of the thumb (at least 20 px tall so it stays grabbable)."""
        H = self.winfo_height()
        t0, t1 = self.ARROW, max(self.ARROW + 1, H - self.ARROW)
        span = t1 - t0
        size = max(20, (self.last - self.first) * span)
        y0 = t0 + (span - size) * (self.first / max(1e-9, 1 - (self.last - self.first)))
        return y0, y0 + size

    def part_at(self, y):
        H = self.winfo_height()
        if y < self.ARROW:
            return "up"
        if y >= H - self.ARROW:
            return "down"
        y0, y1 = self.thumb_box()
        return "thumb" if y0 <= y <= y1 else ("page_up" if y < y0 else "page_down")

    def set_hot(self, part):
        part = part if part in ("up", "down", "thumb") else None
        if part != self.hot:
            self.hot = part
            self.draw()

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        for part, y0 in (("up", 0), ("down", H - self.ARROW)):
            if self.held == part or self.hot == part:  # arrow area lights up under the mouse
                self.create_rectangle(0, y0, W, y0 + self.ARROW,
                                      fill=self.THUMB_DOWN if self.held == part else self.ARROW_HOVER_BG,
                                      outline="")
            fg = "white" if self.held == part else self.ARROW_FG
            cx, cy, d = W / 2, y0 + self.ARROW / 2, (-1 if part == "up" else 1)
            self.create_line(cx - 4, cy - 2 * d, cx, cy + 2 * d, cx + 4, cy - 2 * d, fill=fg, width=1)
            self.create_line(cx - 3, cy - 2 * d, cx, cy + 1 * d, cx + 3, cy - 2 * d, fill=fg, width=1)
        y0, y1 = self.thumb_box()
        color = (self.THUMB_DOWN if self.held == "thumb" else
                 self.THUMB_HOVER if self.hot == "thumb" else self.THUMB)
        self.create_rectangle(1, y0, W - 1, y1, fill=color, outline="")

    def press(self, e):
        part = self.part_at(e.y)
        self.held = part
        if part == "thumb":
            self._drag = (e.y, self.first)
        else:
            self.step(part, first=True)
        self.draw()

    def step(self, part, first=False):
        """One arrow / page step; keeps repeating while the button is held, like Windows."""
        if self.held != part:
            return
        if part in ("up", "down"):
            self.command("scroll", -1 if part == "up" else 1, "units")
        else:
            y0, y1 = self.thumb_box()  # stop paging once the thumb reaches the mouse
            py = self.winfo_pointery() - self.winfo_rooty()
            if (part == "page_up" and py >= y0) or (part == "page_down" and py <= y1):
                return
            self.command("scroll", -1 if part == "page_up" else 1, "pages")
        self._repeat = self.after(400 if first else 50, self.step, part)

    def drag(self, e):
        if not self._drag:
            return
        y_start, first0 = self._drag
        span = max(1, self.winfo_height() - 2 * self.ARROW)
        size = max(20, (self.last - self.first) * span)
        room = max(1, span - size)  # how far the thumb can travel
        frac = first0 + (e.y - y_start) / room * (1 - (self.last - self.first))
        self.command("moveto", min(max(frac, 0), 1))

    def release(self, e):
        if self._repeat:
            self.after_cancel(self._repeat)
        self._repeat, self._drag, self.held = None, None, None
        self.set_hot(self.part_at(e.y) if 0 <= e.x < self.winfo_width() else None)
        self.draw()


class ThumbGrid(tk.Frame):
    """Thumbnail view, XP style: each picture in a white square with a thin grey border, a name
    and a second line underneath, 4 per row. Click / Ctrl / Shift / drag a box to select, rest
    the mouse for details, right-click for the owner's menu.
    The owner (a tab) supplies what to show and keeps the selection, so the details view shares
    it: grid_items() -> [(path, title, subtitle)], grid_thumb(path), tip_lines(i),
    context_menu(i, x, y), and the attributes selected / last_click."""
    COLS, GAP, TEXT_H, PIC_PAD = 4, 10, 34, 6
    BOX_LINE, BOX_HOVER, SEL_BLUE = "#B0B0B0", "#7DA2CE", "#316AC5"
    TIP_DELAY = 1000  # hover tooltip appears after the mouse rests ~1 s on a picture

    def __init__(self, parent, owner, hint):
        super().__init__(parent, bg=BG)
        self.owner, self.hint = owner, hint
        self.small = tkfont.Font(family="Tahoma", size=8)
        self.canvas = c = tk.Canvas(self, bg="white", relief="sunken", bd=2,
                                    highlightthickness=0, height=200)
        self.sb = FlatScrollbar(self, command=c.yview)
        self.sb_shown = False  # only packed when the pictures don't fit
        c.config(yscrollcommand=self.sb.set)
        c.pack(side="left", fill="both", expand=True)
        self.entries = []  # (path, title, subtitle) as last drawn
        self._photos, self._redraw_job = [], None
        self._photo_cache, self._photo_px = {}, None  # scaled Tk images, reused between redraws
        self.rects, self.press, self.dragging = [], None, False
        self.base_sel, self.ctrl, self.region_h = set(), False, 0
        self._band_img = None
        self.tip, self.tip_job, self.tip_pos = None, None, (0, 0)
        self.tip_index, self.tip_watch = None, None
        self.hover = None  # index of the picture under the cursor
        self.can_scroll = False
        c.bind("<Configure>", lambda e: self.schedule_redraw())
        c.bind("<Button-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Motion>", self.on_hover)
        c.bind("<Leave>", self.on_leave)
        c.bind("<Button-3>", self.on_right_click)
        if sys.platform == "darwin":  # right-click is Button-2 on macOS
            c.bind("<Button-2>", self.on_right_click)
        c.bind("<MouseWheel>", self.on_wheel)
        c.bind("<Button-4>", lambda e: self.scroll(-1, e))  # Linux wheel up
        c.bind("<Button-5>", lambda e: self.scroll(1, e))   # Linux wheel down

    # the selection lives in the owner, shared with the details view
    selected = property(lambda self: self.owner.selected,
                        lambda self, v: setattr(self.owner, "selected", v))
    last_click = property(lambda self: self.owner.last_click,
                          lambda self, v: setattr(self.owner, "last_click", v))

    def schedule_redraw(self):
        if self._redraw_job:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(60, self.redraw)

    def cell_size(self):
        w = max(self.canvas.winfo_width(), 100)
        return max(48, (w - self.GAP * (self.COLS + 1)) // self.COLS)

    def cell_xy(self, i):
        c = self.cell_size()
        ch = c + self.TEXT_H  # picture square + two lines of text
        row, col = divmod(i, self.COLS)
        return (self.GAP + col * (c + self.GAP), self.GAP + row * (ch + self.GAP), c, ch)

    def redraw(self):
        if self._redraw_job:
            self.after_cancel(self._redraw_job)
        self._redraw_job = None
        self.hide_tip()
        self.entries = self.owner.grid_items()
        if self.hover is not None and self.hover >= len(self.entries):
            self.hover = None
        cv = self.canvas
        cv.delete("all")
        self._photos, self.rects = [], []  # rects: (box, name highlight, name, 2nd line) per cell
        px = self.cell_size() - 2 * self.PIC_PAD
        live = {p for p, _, _ in self.entries}
        if px != self._photo_px:  # cells changed size: every picture needs rescaling
            self._photo_cache, self._photo_px = {}, px
        else:  # forget pictures of files no longer here
            self._photo_cache = {p: t for p, t in self._photo_cache.items() if p in live}
        if not self.entries:
            cv.create_text(cv.winfo_width() // 2, max(cv.winfo_height(), 100) // 2,
                           text=self.hint, font=FONT, fill="#888888", justify="center")
        for i, (path, title, sub) in enumerate(self.entries):
            x, y, c, ch = self.cell_xy(i)
            box = cv.create_rectangle(x, y, x + c, y + c, fill="white")  # picture square
            photo = self._photo_cache.get(path)
            if photo is None:
                th = self.owner.grid_thumb(path)
                # grey square until it has loaded (Pillow picture: coloured for the mode here)
                img = (th or Image.new("RGBA", (64, 64), themed("#E4E4E4"))).copy()
                img.thumbnail((px, px), Image.BILINEAR)
                photo = ImageTk.PhotoImage(img)
                if th is not None:
                    self._photo_cache[path] = photo
            self._photos.append(photo)  # keep a reference or Tk drops it
            cv.create_image(x + c // 2, y + c // 2, image=photo)
            label = cv.create_text(x + c // 2, y + c + 11, font=self.small,
                                   text=fit_text(self.small, title, c - 8))
            x0, y0, x1, y1 = cv.bbox(label)
            label_bg = cv.create_rectangle(x0 - 2, y0, x1 + 2, y1, outline="")
            cv.tag_lower(label_bg, label)  # highlight sits behind the name
            second = cv.create_text(x + c // 2, y + c + 25, font=self.small, fill="#666666",
                                    text=fit_text(self.small, sub, c - 4))
            self.rects.append((box, label_bg, label, second))
            self.style_cell(i)
        view_w, view_h = cv.winfo_width(), cv.winfo_height()
        content_h = self.content_height(view_w)
        self.can_scroll = bool(self.entries) and content_h > view_h
        # region at least as big as the view, so an empty/short list can't be scrolled
        self.region_h = max(content_h, view_h)
        cv.config(scrollregion=(0, 0, view_w, self.region_h))
        if not self.can_scroll:
            cv.yview_moveto(0)
        self.update_scrollbar()

    def refresh_texts(self):
        """Update just the second lines (e.g. conversion progress) without a full redraw."""
        items = self.owner.grid_items()
        if len(items) != len(self.rects):
            return self.schedule_redraw()
        self.entries = items
        c = self.cell_size()
        for (_, _, sub), (_, _, _, second) in zip(items, self.rects):
            self.canvas.itemconfig(second, text=fit_text(self.small, sub, c - 4))

    def on_wheel(self, event):
        # Windows sends multiples of 120 (touchpads: smaller); macOS sends small numbers
        if not event.delta:
            return
        steps = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        self.scroll(steps, event)

    def scroll(self, steps, event):
        self.hide_tip()
        if self.can_scroll:
            self.canvas.yview_scroll(steps, "units")
            self.update_hover(event.x, event.y)

    def content_height(self, width):
        c = max(48, (max(width, 100) - self.GAP * (self.COLS + 1)) // self.COLS)
        rows = -(-len(self.entries) // self.COLS)
        return self.GAP + rows * (c + self.TEXT_H + self.GAP)

    def update_scrollbar(self):
        """Show the scrollbar only when the pictures are taller than the box."""
        view_h = self.canvas.winfo_height()
        if view_h <= 1:  # window not laid out yet
            return
        # judge by the full width (scrollbar hidden) so it can't flicker on/off
        full_w = self.canvas.winfo_width() + (self.sb.winfo_reqwidth() if self.sb_shown else 0)
        need = bool(self.entries) and self.content_height(full_w) > view_h
        if need == self.sb_shown:
            return
        self.sb_shown = need
        if need:
            self.sb.pack(side="right", fill="y", before=self.canvas)  # sized before the canvas
        else:
            self.sb.pack_forget()
            self.canvas.yview_moveto(0)

    def style_cell(self, i):
        """Selected: blue border + name in white on blue (like XP). Hover: light-blue border.
        Otherwise: thin grey border, black name."""
        box, label_bg, label, _ = self.rects[i]
        sel, hover = i in self.selected, i == self.hover
        self.canvas.itemconfig(box, width=2 if sel else 1,
                               outline=self.SEL_BLUE if sel else
                               self.BOX_HOVER if hover else self.BOX_LINE)
        self.canvas.itemconfig(label_bg, fill=self.SEL_BLUE if sel else "")
        self.canvas.itemconfig(label, fill="white" if sel else "black")

    def refresh_selection(self):
        """Restyle the cells only (fast: no picture redraw)."""
        for i in range(len(self.rects)):
            self.style_cell(i)

    def hit_test(self, x, y):
        for i in range(len(self.entries)):
            cx, cy, c, ch = self.cell_xy(i)
            if cx <= x <= cx + c and cy <= y <= cy + ch:
                return i
        return None

    def on_press(self, event):
        self.hide_tip()
        self.hover = None
        self.press_screen = (event.x, event.y)
        self.press = (self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        self.dragging = False
        self.ctrl = bool(event.state & 0x0004)
        self.base_sel = set(self.selected) if self.ctrl else set()
        hit = self.hit_test(*self.press)
        if hit is None:
            if not self.ctrl:
                self.selected = set()
        elif self.ctrl:  # Ctrl: toggle
            self.selected = self.selected ^ {hit}
            self.last_click = hit
        elif event.state & 0x0001 and self.last_click is not None:  # Shift: range
            a, b = sorted((self.last_click, hit))
            self.selected = set(range(a, b + 1))
        else:
            self.selected = {hit}
            self.last_click = hit
        self.refresh_selection()

    def on_drag(self, event):
        if self.press is None:
            return
        if not self.dragging:  # ignore tiny jitters so plain clicks stay clicks
            if (abs(event.x - self.press_screen[0]) < 4
                    and abs(event.y - self.press_screen[1]) < 4):
                return
            self.dragging = True
        if self.can_scroll:  # auto-scroll when dragging past the top/bottom edge
            if event.y < 0:
                self.canvas.yview_scroll(-1, "units")
            elif event.y > self.canvas.winfo_height():
                self.canvas.yview_scroll(1, "units")
        w = self.canvas.winfo_width()
        x = min(max(self.canvas.canvasx(event.x), 0), w)
        y = min(max(self.canvas.canvasy(event.y), 0), self.region_h)
        x0, x1 = sorted((self.press[0], x))
        y0, y1 = sorted((self.press[1], y))

        # every cell touched by the rectangle
        hits = set()
        for i in range(len(self.entries)):
            cx, cy, c, ch = self.cell_xy(i)
            if x0 < cx + c and x1 > cx and y0 < cy + ch and y1 > cy:
                hits.add(i)
        self.selected = (self.base_sel ^ hits) if self.ctrl else hits
        self.refresh_selection()

        # translucent blue box with a solid border, like Explorer
        bw, bh = max(1, int(x1 - x0)), max(1, int(y1 - y0))
        self._band_img = ImageTk.PhotoImage(Image.new("RGBA", (bw, bh), (36, 94, 220, 70)))
        self.canvas.delete("band")
        self.canvas.create_image(x0, y0, image=self._band_img, anchor="nw", tags="band")
        self.canvas.create_rectangle(x0, y0, x0 + bw, y0 + bh, outline=BLUE, tags="band")

    def on_release(self, event):
        self.canvas.delete("band")
        self.press, self.dragging = None, False

    def on_right_click(self, event):
        self.hide_tip()
        hit = self.hit_test(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        if hit is None:
            return
        # right-clicking a picture drops every other highlight: only this one stays
        self.hover = None
        self.selected = {hit}
        self.last_click = hit
        self.refresh_selection()
        self.owner.context_menu(hit, event.x_root, event.y_root)

    # ---- hover highlight + tooltip ----
    def update_hover(self, ex, ey):
        """Highlight the picture under the cursor; returns its index (or None)."""
        hit = self.hit_test(self.canvas.canvasx(ex), self.canvas.canvasy(ey))
        if hit != self.hover:
            self.hover = hit
            self.refresh_selection()
        return hit

    def on_leave(self, event):
        self.hide_tip()
        if self.hover is not None:
            self.hover = None
            self.refresh_selection()

    def on_hover(self, event):
        pos = (event.x_root, event.y_root)
        if pos == self.tip_pos:  # ignore motion events that didn't actually move
            return
        self.tip_pos = pos
        hit = self.update_hover(event.x, event.y)
        if self.tip and hit == self.tip_index:
            return  # still on the same picture: tooltip stays, like Explorer
        self.hide_tip()  # moved to another picture / empty space: hide + restart timer
        if hit is not None:
            self.tip_job = self.after(self.TIP_DELAY, lambda: self.show_tip(hit, pos))

    def hide_tip(self):
        for job in (self.tip_job, self.tip_watch):
            if job:
                self.after_cancel(job)
        self.tip_job = self.tip_watch = None
        self.tip_index = None
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def show_tip(self, i, pos):
        self.tip_job = None
        if i >= len(self.entries):
            return
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        tk.Label(tip, text="\n".join(self.owner.tip_lines(i)), justify="left",
                 bg="#FFFFE1", fg="black", relief="solid", bd=1, font=FONT,
                 padx=6, pady=3).pack()
        tip.update_idletasks()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        x = min(pos[0] + 14, self.winfo_screenwidth() - w - 4)
        y = pos[1] + 18
        if y + h > self.winfo_screenheight():
            y = pos[1] - h - 8
        tip.wm_geometry(f"+{x}+{y}")
        self.tip = tip
        self.tip_index = i
        self.tip_watch = self.after(100, self.watch_tip)

    def watch_tip(self):
        """Safety net: hide the tooltip the moment the real pointer is off its picture."""
        self.tip_watch = None
        if not self.tip:
            return
        cx = self.winfo_pointerx() - self.canvas.winfo_rootx()
        cy = self.winfo_pointery() - self.canvas.winfo_rooty()
        inside = 0 <= cx < self.canvas.winfo_width() and 0 <= cy < self.canvas.winfo_height()
        hit = self.hit_test(self.canvas.canvasx(cx), self.canvas.canvasy(cy)) if inside else None
        if hit != self.tip_index:
            self.hide_tip()
        else:
            self.tip_watch = self.after(100, self.watch_tip)


class DetailsList(tk.Frame):
    """Details view, Explorer style: one row per file. The columns always exactly fill the
    list; dragging a divider trades width between just its two neighbours; the first column
    takes (or gives up) the room when the window is resized. Shares the owner's selection
    (see ThumbGrid); right-click opens the owner's menu; empty = a plain box with a hint."""

    def __init__(self, parent, owner, columns, hint):
        super().__init__(parent, bg=BG)
        self.owner, self.hint = owner, hint
        keys = [col[0] for col in columns]
        self.first = keys[0]
        self.tree = t = ttk.Treeview(self, columns=keys, show="headings", height=6)
        for key, title, width, least in columns:  # sizes managed here, not by ttk's stretching
            t.heading(key, text=title, anchor="w")
            t.column(key, width=width, minwidth=least, stretch=False, anchor="w")
        self._col_drag = None
        # drag-selecting rows: the row pressed on, Ctrl held?, selection before the press
        self._anchor, self._ctrl, self._base = None, False, set()
        t.bind("<Configure>", lambda e: self.fit_columns(), add="+")
        t.bind("<ButtonPress-1>", self.col_press)
        t.bind("<B1-Motion>", self.col_drag)
        t.bind("<ButtonRelease-1>", self.col_release)
        t.bind("<<TreeviewSelect>>", self.on_select)
        t.bind("<Button-3>", self.on_right_click)
        self.sb = FlatScrollbar(self, command=t.yview)  # same scrollbar as the thumbnails
        self.sb_shown = False  # only shown when the list is too long to fit
        t.config(yscrollcommand=self.on_tree_scroll)
        # while the list is empty, a plain box with a hint shows instead
        self.empty = tk.Canvas(self, bg="white", relief="sunken", bd=2, highlightthickness=0,
                               height=200)
        self.empty.bind("<Configure>", lambda e: self.draw_hint())
        self.update_view()

    # ---- rows ----
    def set_rows(self, rows):
        """Show these rows (row i = file i), updating in place so the scroll position stays."""
        t = self.tree
        for i, values in enumerate(rows):
            if t.exists(str(i)):
                t.item(str(i), values=values)
            else:
                t.insert("", "end", iid=str(i), values=values)
        for iid in t.get_children():
            if int(iid) >= len(rows):
                t.delete(iid)
        self.update_view()
        self.sync_selection()

    def update_row(self, i, values):
        if self.tree.exists(str(i)):
            self.tree.item(str(i), values=values)

    def see(self, i):
        if self.tree.exists(str(i)):
            self.tree.see(str(i))

    def sync_selection(self):
        """Show the owner's selection (e.g. after switching from the thumbnails)."""
        t = self.tree
        want = [str(i) for i in sorted(self.owner.selected) if t.exists(str(i))]
        if list(t.selection()) != want:
            t.selection_set(want)

    def on_select(self, e):
        chosen = {int(i) for i in self.tree.selection()}
        if chosen != self.owner.selected:
            self.owner.selected = chosen
            if len(chosen) == 1:  # e.g. moved with the arrow keys
                self.owner.last_click = next(iter(chosen))

    def on_right_click(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        i = int(row)
        if i not in self.owner.selected:  # right-click on another row: select just that one
            self.owner.selected, self.owner.last_click = {i}, i
            self.sync_selection()
        self.owner.context_menu(i, e.x_root, e.y_root)

    # ---- empty hint / scrollbar ----
    def draw_hint(self):
        c = self.empty
        c.delete("all")
        c.create_text(c.winfo_width() // 2, max(c.winfo_height(), 100) // 2, text=self.hint,
                      font=FONT, fill="#888888", justify="center")

    def update_view(self):
        """Empty: the plain hint box. Otherwise: the list (headers, rows, scrollbar if needed)."""
        if self.tree.get_children():
            self.empty.pack_forget()
            if not self.tree.winfo_manager():  # not packed yet
                self.tree.pack(side="left", fill="both", expand=True)
        else:
            self.tree.pack_forget()
            self.sb.pack_forget()
            self.sb_shown = False
            self.empty.pack(side="left", fill="both", expand=True)

    def on_tree_scroll(self, first, last):
        """The list reports what part of it is visible: show the scrollbar only if not all of it."""
        self.sb.set(first, last)
        need = bool(self.tree.get_children()) and (float(first) > 0 or float(last) < 1)
        if need != self.sb_shown:
            self.sb_shown = need
            if need:
                self.sb.pack(side="right", fill="y", before=self.tree)
            else:
                self.sb.pack_forget()

    # ---- columns: always exactly fill the list ----
    def fit_columns(self):
        """Make the columns add up to the list's width: the first column takes (or gives up)
        the difference; if the list is too narrow even then, the others shrink to their minimum."""
        t = self.tree
        cols = list(t["columns"])
        avail = t.winfo_width() - 4  # minus the list's border
        if avail < 50:
            return  # not laid out yet
        w = {c: int(t.column(c, "width")) for c in cols}
        least = {c: int(t.column(c, "minwidth")) for c in cols}
        extra = avail - sum(w.values())

        def give(c):  # add `extra` to column c (negative = take), not below its minimum
            nonlocal extra
            new = max(least[c], w[c] + extra)
            extra -= new - w[c]
            w[c] = new
        give(self.first)
        for c in reversed(cols):  # still too wide: shrink the others, rightmost first
            if extra >= 0:
                break
            if c != self.first:
                give(c)
        for c in cols:
            if int(t.column(c, "width")) != w[c]:
                t.column(c, width=w[c])

    def divider_at(self, x):
        """The column whose right-hand divider is at x (not the last one: nothing to its right)."""
        edge = 0
        cols = list(self.tree["columns"])
        for c in cols[:-1]:
            edge += int(self.tree.column(c, "width"))
            if abs(x - edge) <= 4:
                return c
        return None

    def col_press(self, e):
        if self.tree.identify_region(e.x, e.y) != "separator":
            # not on a divider: ttk selects the row as usual (Ctrl / Shift work); remember it
            # so dragging from here selects a range of rows, like the thumbnails' drag-box
            row = self.tree.identify_row(e.y)
            last = self.owner.last_click
            if row and e.state & 0x0001 and last is not None:
                # Shift+click: range from the last clicked file, same as the thumbnails
                i = int(row)
                self.owner.selected = set(range(min(last, i), max(last, i) + 1))
                self.sync_selection()
                self.tree.focus(row)
                self._anchor = None
                return "break"
            self._anchor = int(row) if row and not e.state & 0x0001 else None
            if self._anchor is not None:  # plain / Ctrl click: the next Shift+click ranges from here
                self.owner.last_click = self._anchor
            self._ctrl = bool(e.state & 0x0004)
            self._base = set(self.owner.selected) if self._ctrl else set()
            return None
        left = self.divider_at(e.x)
        if left:  # dragging moves width between just the two columns next to the divider
            cols = list(self.tree["columns"])
            right = cols[cols.index(left) + 1]
            self._col_drag = (left, right, e.x, int(self.tree.column(left, "width")),
                              int(self.tree.column(right, "width")))
        return "break"  # our drag, not ttk's (which resized far-away columns); the divider
        # after the last column can't be dragged, since the columns always fill the list

    def row_at(self, y):
        """The row at height y; above/below the rows = the first/last one."""
        t = self.tree
        row = t.identify_row(y)
        if row:
            return int(row)
        kids = t.get_children()
        if not kids:
            return None
        first_bbox = t.bbox(kids[0])
        return 0 if first_bbox and y < first_bbox[1] else int(kids[-1])

    def drag_select(self, e):
        """Drag with the button held: select every row from where it was pressed to here."""
        t = self.tree
        if e.y < 0:  # past the top/bottom edge: scroll along, like the thumbnails
            t.yview_scroll(-1, "units")
        elif e.y > t.winfo_height():
            t.yview_scroll(1, "units")
        here = self.row_at(min(max(e.y, 1), t.winfo_height() - 2))
        if here is None:
            return
        rows = set(range(min(self._anchor, here), max(self._anchor, here) + 1))
        self.owner.selected = (self._base ^ rows) if self._ctrl else rows
        self.owner.last_click = self._anchor
        self.sync_selection()
        t.focus(str(here))

    def col_drag(self, e):
        if not self._col_drag:
            if self._anchor is not None:
                self.drag_select(e)
                return "break"
            return None
        left, right, x0, wl, wr = self._col_drag
        t = self.tree
        lo = int(t.column(left, "minwidth"))
        hi = wl + wr - int(t.column(right, "minwidth"))
        new_left = min(max(wl + e.x - x0, lo), hi)
        t.column(left, width=new_left)
        t.column(right, width=wl + wr - new_left)
        return "break"

    def col_release(self, e):
        self._anchor = None
        if self._col_drag:
            self._col_drag = None
            return "break"
        return None


class FileBox:
    """A tab's files shown two ways over the same selection: thumbnails or details.
    Ctrl + mouse wheel switches, like Explorer: up = thumbnails, down = details."""

    def __init__(self, parent, owner, columns, hint, view="thumbs", button_parent=None):
        self.owner = owner
        self.grid = ThumbGrid(parent, owner, hint)
        self.list = DetailsList(parent, owner, columns, hint)
        for w in self.widgets():
            w.bind("<Control-MouseWheel>", self.on_ctrl_wheel)
        self.button = None
        if button_parent is not None:  # a button that switches too; it names the other view
            self.button = xp_button(button_parent, "", self.toggle)
            self.button.config(width=10)  # fits both labels, so the column never changes width
            self.button.pack(fill="x", pady=(12, 2))
        self.view = None
        self.set_view(view)

    def toggle(self):
        self.set_view("details" if self.view == "thumbs" else "thumbs")

    def widgets(self):
        """The widgets the files show in (for drag & drop)."""
        return self.grid.canvas, self.list.tree, self.list.empty

    def on_ctrl_wheel(self, e):
        if e.delta:
            self.set_view("thumbs" if e.delta > 0 else "details")
        return "break"  # don't also scroll

    def set_view(self, view):
        if view == self.view:
            return
        self.grid.hide_tip()
        hide, show = (self.list, self.grid) if view == "thumbs" else (self.grid, self.list)
        hide.pack_forget()
        show.pack(side="left", fill="both", expand=True)
        self.view = view
        if self.button is not None:
            self.button.config(text="Details" if view == "thumbs" else "Thumbnails")
        self.refresh()

    def refresh(self):
        """Redraw whichever view is showing (after files were added, removed, renamed...)."""
        if self.view == "thumbs":
            self.grid.redraw()
        else:
            self.list.set_rows(self.owner.list_rows())

    def refresh_soon(self):
        """Like refresh, but coalesced - for bursts (thumbnails arriving one by one)."""
        if self.view == "thumbs":
            self.grid.schedule_redraw()
        else:
            self.list.set_rows(self.owner.list_rows())

    def refresh_row(self, i):
        """One file's text changed (e.g. conversion progress)."""
        if self.view == "thumbs":
            self.grid.refresh_texts()
        else:
            self.list.update_row(i, self.owner.list_rows()[i])

    def refresh_selection(self):
        if self.view == "thumbs":
            self.grid.refresh_selection()
        else:
            self.list.sync_selection()

    def see(self, i):
        if self.view == "details":
            self.list.see(i)


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("Master Converter")
        self.set_icon()
        self.minsize(480, 652)
        self.center_on_screen(560, 692)
        self.configure(bg=BG)
        # classic navy title bar + 3D border; everything else goes inside chrome.body
        self.chrome = ClassicWindow(self, "Master Converter", self.on_close, min_size=(480, 652))
        content = self.chrome.body
        self.files, self.outdir = [], ""
        self.names = []  # custom name for the converted file (None = keep original)
        self.small = tkfont.Font(family="Tahoma", size=8)

        self.style_ttk()  # lists and dropdowns: classic shapes, same in light and dark

        # tabs on top, sitting on the raised border around the open page
        self.tabs = ClassicTabs(content, self.show_page)
        self.tabs.pack(fill="x", padx=6, pady=(6, 0))
        # small light / dark mode button at the right end of the tab row
        self._theme_icons = {False: theme_icon("moon"), True: theme_icon("sun")}
        self.theme_btn = xp_button(content, "", self.toggle_theme)
        self.theme_btn.config(image=self._theme_icons[False], width=16, height=12, padx=0,
                              pady=0, highlightthickness=0)
        # y=-1: in the 29 px between the title bar and the page's top edge, 6 px above it
        # and 5 below
        self.theme_btn.place(in_=self.tabs, relx=1.0, x=-3, y=-1, anchor="ne")
        area = framed_page(content)
        body = tk.Frame(area, bg=BG, padx=10, pady=8)
        self.video = VideoPanel(area, self)
        self.voice = AudioPanel(area, self)
        self.gif = GifPanel(area, self)
        self.pages = {"images": body, "videos": self.video, "voice": self.voice, "gif": self.gif}
        self.page = None
        self.tabs.add("images", "Images")
        self.tabs.add("videos", "Videos")
        self.tabs.add("voice", "Audio")
        self.tabs.add("gif", "GIF Maker")
        self.bind("<Control-Tab>", lambda e: self.tabs.step(1))
        self.bind("<Control-Shift-Tab>", lambda e: self.tabs.step(-1))

        # File list
        box = tk.LabelFrame(body, text=" Files ", bg=BG, font=FONT, padx=6, pady=6)
        box.pack(fill="both", expand=True)
        self.selected, self.last_click = set(), None  # shared by both views
        # thumbnails load on a background thread so adding files never freezes the window
        self.thumb_cache = {}  # path -> small RGBA picture
        self.dims, self.sizes = {}, {}  # path -> (width, height) / bytes, for the details view
        self._thumb_todo, self._thumb_ready = queue.Queue(), queue.Queue()
        self._thumb_pending = set()
        threading.Thread(target=self.thumb_worker, daemon=True).start()
        self.after(50, self.poll_thumbs)
        self.busy = False  # True while converting: the file list is locked
        self.bind("<Delete>", self.on_delete_key)
        btns = tk.Frame(box, bg=BG)
        # buttons packed first so a narrow window squeezes the pictures, not them
        btns.pack(side="right", fill="y", padx=(6, 0))
        self.lockable = []  # buttons greyed out while converting
        for text, cmd in (("Add...", self.add_files), ("Remove", self.remove_files),
                          ("Clear", self.clear_files)):
            b = self.btn(btns, text, cmd)
            b.pack(fill="x", pady=2)
            self.lockable.append(b)
        # thumbnails or details (Ctrl + mouse wheel switches)
        self.files_box = FileBox(
            box, self, [("name", "Name", 150, 90), ("type", "Type", 70, 55),
                        ("dims", "Dimensions", 90, 75), ("size", "Size", 80, 60)],
            "Drag & drop images here\nor click Add..." if HAS_DND
            else "Click Add... to choose images", view="thumbs", button_parent=btns)

        # Options
        opt = tk.LabelFrame(body, text=" Options ", bg=BG, font=FONT, padx=6, pady=6)
        opt.pack(fill="x", pady=8)
        tk.Label(opt, text="Convert to:", bg=BG, font=FONT).grid(row=0, column=0, sticky="w")
        self.fmt = tk.StringVar(value="PNG")
        cb = ttk.Combobox(opt, textvariable=self.fmt, values=list(OUT_FORMATS),
                          state="readonly", width=14)
        cb.grid(row=0, column=1, sticky="w", padx=6, pady=2)

        self.qlabel = tk.Label(opt, text="Quality:", bg=BG, font=FONT)
        self.qlabel.grid(row=1, column=0, sticky="w")
        self.quality = tk.IntVar(value=90)
        self.qscale = tk.Scale(opt, from_=1, to=100, orient="horizontal", variable=self.quality,
                               bg=BG, font=FONT, length=200, highlightthickness=0)
        self.qscale.grid(row=1, column=1, sticky="w", padx=6)
        self.qnote = tk.Label(opt, text="", bg=BG, fg="#888888", font=FONT)
        # pinned beside the slider, outside the grid, so it can't shift the Browse button
        self.qnote.place(in_=self.qscale, relx=1.0, rely=1.0, x=6, y=-4, anchor="sw")
        # for ICO the slider is swapped for a list of icon sizes (in the same spot)
        self.ico_size = tk.StringVar(value=next(iter(ICO_SIZES)))
        self.ico_cb = ttk.Combobox(opt, textvariable=self.ico_size, values=list(ICO_SIZES),
                                   state="readonly", width=20)
        self.fmt.trace_add("write", lambda *_: self.update_quality_state())
        self.update_quality_state()

        tk.Label(opt, text="Save to:", bg=BG, font=FONT).grid(row=2, column=0, sticky="w")
        self.outlabel = tk.Label(opt, text="(same folder as original)", bg="white",
                                 font=FONT, relief="sunken", bd=2, anchor="w", width=32)
        self.outlabel.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        self.btn(opt, "Browse...", self.pick_outdir).grid(row=2, column=2)

        self.keep_exif = tk.BooleanVar(value=True)
        tk.Checkbutton(opt, text="Keep photo info (date, camera, GPS location)",
                       variable=self.keep_exif, bg=BG, activebackground=BG, font=FONT
                       ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # Convert + progress
        convert = self.btn(body, "Convert", self.run, bold=True)
        convert.pack(fill="x", pady=(0, 6), ipady=4)
        self.lockable.append(convert)
        self.progress = ClassicProgress(body)
        self.progress.pack(fill="x")
        status_row = tk.Frame(body, bg=BG)
        status_row.pack(fill="x", pady=(4, 0))
        self.converted = []  # paths written by the last conversion
        self.show_btn = self.btn(status_row, "Show converted files", self.show_converted)
        self.show_btn.config(state="disabled")  # nothing converted yet
        self.show_btn.pack(side="right")
        self.status = status_label(status_row, "Ready.")

        # Drag & drop (works anywhere over the window)
        if HAS_DND:
            for w in (self, body, box, *self.files_box.widgets()):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tabs.select("images")
        self.files_box.refresh()
        self.color_dropdown_lists()  # the same selection blue from the start
        if load_settings().get("dark"):  # dark mode chosen last time
            self.set_theme(True)
        self.after(800, self.startup_update_tasks)

    # ---- updates ----
    def startup_update_tasks(self):
        """Just updated? Show what's new. Then look for a newer version in the background."""
        self.show_update_notes()
        self.check_for_updates()

    def show_update_notes(self):
        info = load_settings().get("just_updated")
        if not info:
            return
        save_settings(just_updated=None)  # show it once
        if info.get("version") != APP_VERSION:
            return  # the update didn't go through: nothing to announce
        notes_dialog(self, f"Master Converter was updated to version {APP_VERSION}.",
                     plain_notes(info.get("notes")))

    def check_for_updates(self):
        """Ask GitHub, off the main thread, whether there's a newer version. Skipped when
        running from the source code (no version) or if turned off in the settings file."""
        if parse_version(APP_VERSION) is None or not load_settings().get("check_updates", True):
            return
        found = queue.Queue()
        threading.Thread(target=lambda: found.put(fetch_latest_release()), daemon=True).start()

        def wait():
            try:
                latest = found.get_nowait()
            except queue.Empty:
                self.after(200, wait)
                return
            if latest:
                self.offer_update(*latest)
        self.after(200, wait)

    def offer_update(self, version, notes, url):
        if parse_version(version) <= parse_version(APP_VERSION):
            return  # up to date
        if load_settings().get("skipped_version") == version:
            self.show_update_button(version, notes, url)  # skipped: no box, just the button
            return
        choice = dialog("Update available",
                        f"Master Converter {version} is available.\nYou have version {APP_VERSION}.",
                        ("Update now", "Remind me later", "Skip this version"), sound="done")
        if choice == "Update now":
            self.install_update(version, notes, url)
        elif choice == "Skip this version":
            save_settings(skipped_version=version)
            self.show_update_button(version, notes, url)
        # "Remind me later" (or closing the box): ask again next time the app starts

    def show_update_button(self, version, notes, url):
        """The green arrow in the title bar: a skipped update can still be installed from it."""
        def clicked():
            choice = dialog("Update available",
                            f"Master Converter {version} is available.\nYou have version {APP_VERSION}.",
                            ("Update now", "Not now"))
            if choice == "Update now":
                self.install_update(version, notes, url)
        self.chrome.set_update_button(clicked)

    def install_update(self, version, notes, url):
        """Download the new installer (with a progress bar), run it silently - no questions:
        it installs over this version where it is - and close; the installer starts the new
        version, which then shows the release notes."""
        win = tk.Toplevel(self)
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self)
        body = ClassicWindow(win, "Updating", lambda: None, resizable=False, taskbar=False).body
        label = tk.Label(body, text=f"Downloading Master Converter {version}...", bg=BG,
                         font=FONT, anchor="w")
        label.pack(fill="x", padx=12, pady=(12, 6))
        bar = ClassicProgress(body, maximum=100)
        bar.pack(fill="x", padx=12, pady=(0, 14))
        tk.Frame(body, bg=BG, width=320, height=0).pack()
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.focus_force()
        win.grab_set()
        dst = os.path.join(tempfile.gettempdir(), f"MasterConverter-Setup-{version}.exe")
        events = queue.Queue()

        def download():  # off the main thread
            import urllib.request
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MasterConverter"})
                with urllib.request.urlopen(req, timeout=30) as r, open(dst + ".part", "wb") as f:
                    total, got = int(r.headers.get("Content-Length") or 0), 0
                    while chunk := r.read(256 * 1024):
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            events.put(("frac", got / total))
                os.replace(dst + ".part", dst)
                events.put(("done", None))
            except Exception as e:
                events.put(("error", str(e)))

        def poll():
            try:
                while True:
                    kind, value = events.get_nowait()
                    if kind == "frac":
                        bar.config(value=value * 100)
                    elif kind == "done":
                        label.config(text="Installing the update...")
                        bar.config(value=100)
                        win.update()
                        # remembered for the new version to show once it's running
                        save_settings(just_updated={"version": version, "notes": notes})
                        # silent install: no wizard, same place, same shortcuts; it closes
                        # this app if it's still running and starts the new one when done
                        subprocess.Popen([dst, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                          "/CLOSEAPPLICATIONS"])
                        self.after(300, self.destroy)
                        return
                    else:
                        win.destroy()
                        dialog("Master Converter", f"The update couldn't be downloaded:\n{value}",
                               sound="error")
                        return
            except queue.Empty:
                pass
            self.after(100, poll)
        threading.Thread(target=download, daemon=True).start()
        self.after(100, poll)

    # ---- light / dark mode ----
    def toggle_theme(self):
        self.set_theme(not DARK_MODE)
        save_settings(dark=DARK_MODE)

    def set_theme(self, dark):
        """Switch the whole app between light and dark."""
        global DARK_MODE
        if dark == DARK_MODE:
            return
        DARK_MODE = dark  # from here on, every colour given to Tk goes through dark_color()
        self.style_ttk()
        self.option_clear()  # defaults for widgets made from now on (menus, dialogs...)
        if dark:
            for pattern, value in (
                    ("*foreground", DARK_TEXT), ("*activeForeground", DARK_TEXT),
                    ("*disabledForeground", "#7a7a7a"), ("*selectColor", DARK_BOX),
                    ("*insertBackground", DARK_TEXT), ("*troughColor", "#262626"),
                    ("*highlightBackground", DARK_FACE), ("*background", DARK_FACE),
                    ("*Entry.background", DARK_BOX),  # text boxes: dark like the file boxes
                    ("*TCombobox*Listbox.background", DARK_BOX),
                    ("*TCombobox*Listbox.foreground", DARK_TEXT),
                    ("*TCombobox*Listbox.selectBackground", "#316AC5"),
                    ("*TCombobox*Listbox.selectForeground", "white")):
                self.option_add(pattern, value)
        widgets = all_widgets(self)
        for w in widgets:  # everything that already exists
            retheme(w, dark)
        for w in widgets:  # the custom-drawn parts: redraw them in the new colours
            if isinstance(w, (ClassicTabs, FlatScrollbar)):
                w.draw()
            elif isinstance(w, ClassicProgress):
                w.draw(force=True)
            elif isinstance(w, ThumbGrid):
                w.redraw()
            elif isinstance(w, DetailsList):
                w.draw_hint()
            elif isinstance(w, GifPanel):
                w.draw_strip()
        self.chrome.draw()
        self.theme_btn.config(image=self._theme_icons[dark])
        self.color_dropdown_lists()
        self.update_quality_state()  # the ICO size list's height differs between the modes

    def color_dropdown_lists(self):
        """A dropdown makes its list the first time it opens and keeps its colours: colour
        every dropdown's list (making it now if needed) for the current mode."""
        for w in all_widgets(self):
            if isinstance(w, ttk.Combobox):
                popdown = self.tk.call("ttk::combobox::PopdownWindow", w)
                self.tk.call(f"{popdown}.f.l", "configure",
                             "-background", DARK_BOX if DARK_MODE else "white",
                             "-foreground", DARK_TEXT if DARK_MODE else "black",
                             "-selectbackground", "#316AC5", "-selectforeground", "white")

    def style_ttk(self):
        """The details lists and dropdowns are ttk widgets, coloured through styles. Both modes
        use ttk's "alt" look - classic Windows shapes (raised arrow buttons, classic scrollbar)
        drawn by Tk, so unlike Windows' own native look they can be recoloured: the shapes
        stay exactly the same in light and dark, only the colours change."""
        style = ttk.Style(self)
        style.theme_use("alt")
        if DARK_MODE:
            face, box, text, trough, hot, grey = (DARK_FACE, DARK_BOX, DARK_TEXT, "#262626",
                                                  "#474747", "#7a7a7a")
        else:
            face, box, text, trough, hot, grey = (BG, "white", "black", "#F7F6F0",
                                                  "#F5F3E8", "#999999")
        style.configure(".", background=face, foreground=text, fieldbackground=box,
                        troughcolor=trough, selectbackground="#316AC5",
                        selectforeground="white", arrowcolor=text, font=FONT)
        style.map(".", background=[("active", hot)])
        style.configure("Treeview", background=box, fieldbackground=box, foreground=text,
                        font=FONT)
        style.configure("Treeview.Heading", background=face, foreground=text, font=FONT,
                        relief="raised")
        style.map("Treeview", background=[("selected", "#316AC5")],
                  foreground=[("selected", "white")])
        style.map("Treeview.Heading", background=[("active", hot)])
        style.map("TCombobox",
                  fieldbackground=[("readonly", box), ("disabled", face)],
                  foreground=[("readonly", text), ("disabled", grey)],
                  selectbackground=[("readonly", box)],
                  selectforeground=[("readonly", text)],
                  background=[("readonly", face), ("active", hot)])

    def hide_tip(self):
        """Close any hover tooltip (Images or Videos thumbnails)."""
        for page in (self, self.video, self.voice):
            page.files_box.grid.hide_tip()

    def show_page(self, key):
        """Called by the tab strip: swap which page fills the area under it."""
        self.hide_tip()
        if self.page == "gif" and key != "gif":
            self.gif.stop_play()  # leaving the GIF page stops the preview
        for k, page in self.pages.items():
            if k != key:
                page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.page = key

    def update_quality_state(self):
        """Lock the slider (with a grey note saying why) for formats that ignore quality.
        For ICO, the slider makes way for the list of icon sizes."""
        fmt = OUT_FORMATS[self.fmt.get()][0]
        ico = fmt == "ICO"
        if ico:
            self.qscale.grid_remove()  # its note is pinned to it, so it goes too
            # space above and below so the list's row is exactly as tall as the slider's, and
            # nothing below moves when switching to / from ICO (the list's own height differs
            # between light and dark mode, so it's worked out each time)
            self.ico_cb.update_idletasks()
            extra = max(0, self.qscale.winfo_reqheight() - self.ico_cb.winfo_reqheight())
            self.ico_cb.grid(row=1, column=1, sticky="w", padx=6,
                             pady=(extra // 2, extra - extra // 2))
        else:
            self.ico_cb.grid_remove()
            self.qscale.grid()
        self.qlabel.config(text="Icon size:" if ico else "Quality:")
        if ico:
            return
        # Pillow's JPEG 2000 writer ignores quality; SVG uses it for the JPEG inside
        used = (fmt in LOSSY and fmt != "JPEG2000") or fmt == "SVG"
        self.qscale.config(state="normal" if used else "disabled",
                           fg="black" if used else "#999999")
        self.qnote.config(text="" if used else f"(not used by {self.fmt.get()})")

    def set_busy(self, busy):
        self.busy = busy
        for b in self.lockable:
            b.config(state="disabled" if busy else "normal")

    def on_close(self):
        if (self.busy or self.video.busy or self.voice.busy or self.gif.busy) and dialog(
                "Master Converter", "A conversion is still running.\n"
                "Quit anyway? The file being written may be left incomplete.",
                ("Yes", "No")) != "Yes":
            return
        # stop ffmpeg too, or it keeps running after the window is gone
        self.video.cancel()
        self.voice.cancel()
        self.gif.cancel()
        self.gif.stop_play()
        self.destroy()

    def center_on_screen(self, w, h):
        """Open in the middle of the screen (nudged up a little for the title bar and taskbar)."""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        h = min(h, sh - 80)  # small screens: still fits
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def set_icon(self):
        try:
            if sys.platform == "win32":
                # default= also gives every dialog (Rename, messages) our icon, not Tk's feather
                self.iconbitmap(default=resource_path("icon.ico"))
            else:
                self._icon = ImageTk.PhotoImage(Image.open(resource_path("icon.png")))
                self.iconphoto(True, self._icon)
        except Exception:
            pass  # missing icon file: keep the default one

    def btn(self, parent, text, cmd, bold=False):
        return xp_button(parent, text, cmd, bold)

    # ---- adding files (button or drag & drop) ----
    def add_files(self):
        exts = " ".join("*" + e for e in INPUT_EXTS)
        paths = filedialog.askopenfilenames(
            title="Select images",
            filetypes=[("Images", exts), ("All files", "*.*")])
        self.add_paths(paths)

    def on_drop(self, event):
        """Drops go to whichever tab is showing."""
        target = {"videos": self.video, "voice": self.voice, "gif": self.gif}.get(self.page, self)
        target.add_paths(self.tk.splitlist(event.data))
        return event.action

    def add_paths(self, paths):
        if self.busy:  # the list is locked while converting
            return
        valid = tuple(INPUT_EXTS)
        found, skipped = [], 0
        for p in paths:
            if os.path.isdir(p):  # dropped a folder: take the images inside it (and subfolders)
                for root, dirs, files in os.walk(p):
                    dirs.sort()
                    found += [os.path.join(root, f) for f in sorted(files)]
            else:
                found.append(p)
        known = set(self.files)
        videos = 0
        for p in found:
            if not p.lower().endswith(valid):
                skipped += 1
                videos += p.lower().endswith(VIDEO_EXTS)
            elif p not in known:
                known.add(p)
                self.files.append(p)
                self.names.append(None)
                try:
                    self.sizes[p] = os.path.getsize(p)
                except OSError:
                    pass
                self.request_thumb(p)
        msg = f"{len(self.files)} file(s) selected."
        if skipped:
            msg += f" Skipped {skipped} unsupported."
        if videos:
            msg += " (Videos go in the Videos tab.)"
        self.status.config(text=msg)
        self.files_box.refresh()

    # ---- thumbnails: made on a worker thread, handed back through a queue ----
    def request_thumb(self, path):
        if path not in self.thumb_cache and path not in self._thumb_pending:
            self._thumb_pending.add(path)
            self._thumb_todo.put(path)

    def thumb_worker(self):
        while True:  # no Tk calls in here: Tk is only safe on the main thread
            path = self._thumb_todo.get()
            self._thumb_ready.put((path, self.make_thumb(path)))

    def poll_thumbs(self):
        got = False
        try:
            while True:
                path, (img, dims) = self._thumb_ready.get_nowait()
                self._thumb_pending.discard(path)
                if path in self.files:  # skip pictures removed while loading
                    self.thumb_cache[path] = img
                    if dims:
                        self.dims[path] = dims
                    got = True
        except queue.Empty:
            pass
        if got:
            self.files_box.refresh_soon()
        self.after(50, self.poll_thumbs)

    @staticmethod
    def make_thumb(path):
        """(small picture, (width, height)) - the size as shown, i.e. after EXIF rotation."""
        try:
            im = open_image(path, fast=True)
            # the real size: RAW thumbnails are half size, small SVGs drawn bigger
            dims = (raw_dims(path) if is_camera_raw(path) else
                    render_svg(path).size if is_svg(path) else im.size)
            im.thumbnail((320, 320))
            return im.convert("RGBA"), dims
        except Exception:
            return Image.new("RGBA", (64, 64), "#CCCCCC"), None

    def forget_unused(self):
        """Free what we kept about files no longer in the list."""
        live = set(self.files)
        for cache in ("thumb_cache", "dims", "sizes"):
            setattr(self, cache, {p: v for p, v in getattr(self, cache).items() if p in live})

    # ---- what the two views (thumbnails / details) show - see ThumbGrid / DetailsList ----
    def display_name(self, i):
        """The name shown: the new name if renamed (with the original's extension)."""
        src = os.path.basename(self.files[i])
        return (self.names[i] + os.path.splitext(src)[1]) if self.names[i] else src

    def kind(self, i):
        return os.path.splitext(self.files[i])[1].lstrip(".").upper() or "FILE"

    def grid_items(self):
        return [(p, self.display_name(i), f"{self.kind(i)} file") for i, p in enumerate(self.files)]

    def grid_thumb(self, path):
        return self.thumb_cache.get(path)

    def list_rows(self):
        rows = []
        for i, p in enumerate(self.files):
            d = self.dims.get(p)
            rows.append((self.display_name(i), f"{self.kind(i)} file",
                         f"{d[0]} x {d[1]}" if d else "...",
                         fmt_size(self.sizes[p]) if p in self.sizes else "?"))
        return rows

    def tip_lines(self, i):
        return self.file_info(self.files[i])

    def context_menu(self, i, x, y):
        """Right-click on a file (either view): Rename / Duplicate / Go to file / Remove."""
        if self.busy:
            return
        menu = PopupMenu(self)  # drawn by the app: the same thin frame in light and dark
        menu.add_command(label="Rename", command=lambda: self.rename_item(i))
        menu.add_command(label="Duplicate", command=lambda: self.duplicate_item(i))
        menu.add_command(label="Go to file", command=lambda: go_to_file(self.files[i]))
        menu.add_separator()
        menu.add_command(label="Remove", command=self.remove_files)  # same as the button
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    fmt_size = staticmethod(fmt_size)  # kept for App.fmt_size(...) callers

    def file_info(self, path):
        ext = os.path.splitext(path)[1].lstrip(".").upper() or "FILE"
        lines = [f"Item type: {ext} File"]
        try:
            if is_camera_raw(path):
                w, h = raw_dims(path)
            elif is_svg(path):
                w, h = render_svg(path).size
            else:
                with Image.open(path) as im:  # only reads the header, so it's fast
                    w, h = im.size
                    if im.getexif().get(0x0112) in (5, 6, 7, 8):  # rotated by EXIF
                        w, h = h, w
            lines.append(f"Dimensions: {w} x {h}")
        except Exception:
            lines.append("Dimensions: unknown")
        try:
            lines.append(f"Size: {self.fmt_size(os.path.getsize(path))}")
        except OSError:
            lines.append("Size: unknown")
        return lines

    # ---- Rename / Duplicate (from the right-click menu) ----
    def out_base(self, i):
        """Name (without extension) the converted file will get."""
        return self.names[i] or os.path.splitext(os.path.basename(self.files[i]))[0]

    def rename_item(self, i):
        """Renames the converted file only - the original on disk is never touched."""
        def apply(name):
            self.names[i] = name
            self.status.config(text=f"Will be saved as \"{name}\".")
            self.files_box.refresh()
        rename_dialog(self, self.out_base(i), apply, lambda: self.busy)

    def duplicate_item(self, i):
        """Adds a copy of the picture right after it. Nothing is saved until Convert."""
        if self.busy:
            return
        name = copy_name(self.out_base(i), {self.out_base(k) for k in range(len(self.files))})
        self.files.insert(i + 1, self.files[i])  # same path, so it shares the thumbnail
        self.names.insert(i + 1, name)
        self.selected = {i + 1}  # select the new copy
        self.last_click = i + 1
        self.status.config(text=f"Duplicated. {len(self.files)} file(s) selected.")
        self.files_box.refresh()

    def on_delete_key(self, event):
        """Delete key = same as the Remove button (ignored while typing in a text box)."""
        try:
            if isinstance(self.focus_get(), (tk.Entry, ttk.Entry)):
                return
        except KeyError:
            pass
        if self.page in ("videos", "voice"):
            self.pages[self.page].remove_files()
        elif self.page == "images" and self.selected:
            self.hide_tip()
            self.remove_files()

    def remove_files(self):
        if self.busy:
            return
        keep = [i for i in range(len(self.files)) if i not in self.selected]
        self.files = [self.files[i] for i in keep]
        self.names = [self.names[i] for i in keep]
        self.selected.clear()
        self.forget_unused()
        self.status.config(text=f"{len(self.files)} file(s) selected.")
        self.files_box.refresh()

    def clear_files(self):
        if self.busy:
            return
        self.files.clear()
        self.names.clear()
        self.selected.clear()
        self.forget_unused()
        self.status.config(text="Ready.")
        self.files_box.refresh()

    def show_converted(self):
        """Open the folder(s) of the last conversion with the new files highlighted."""
        reveal_all(self.converted)

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir = d
            self.outlabel.config(text=d)

    def run(self):
        if self.busy:
            return
        if not self.files:
            dialog("Master Converter", "Add some files first.", sound="error")
            return
        label = self.fmt.get()
        fmt, ext = OUT_FORMATS[label]
        # snapshot everything the worker needs, so it never reads the live list
        jobs = [(src, self.out_base(i)) for i, src in enumerate(self.files)]
        settings = (fmt, ext, self.quality.get(), self.keep_exif.get(), self.outdir,
                    ICO_SIZES[self.ico_size.get()])
        self.progress.config(maximum=len(jobs), value=0)
        self.set_busy(True)
        self.hide_tip()
        events = queue.Queue()
        threading.Thread(target=self.convert_worker, args=(jobs, settings, events),
                         daemon=True).start()
        self.after(50, self.poll_convert, events, label)

    @staticmethod
    def convert_worker(jobs, settings, events):
        """Runs off the main thread: only talks to the window through `events`."""
        fmt, ext, q, keep_exif, outdir, ico_sizes = settings
        done, errors = [], []  # done = paths of the files written
        for i, (src, base) in enumerate(jobs, 1):
            events.put(("file", os.path.basename(src)))
            dst = unique_path(outdir or os.path.dirname(src), base, ext)
            try:
                done.append(convert_one(src, dst, fmt, q, keep_exif, ico_sizes))  # path written
            except Exception as e:
                errors.append(f"{os.path.basename(src)}: {e}")
            events.put(("step", i))
        events.put(("done", done, errors))

    def poll_convert(self, events, label):
        try:
            while True:
                ev = events.get_nowait()
                if ev[0] == "file":
                    self.status.config(text=f"Converting {ev[1]}...")
                elif ev[0] == "step":
                    self.progress.config(value=ev[1])
                else:
                    done, errors = ev[1], ev[2]
                    ok = len(done)
                    self.set_busy(False)
                    self.converted = done
                    self.show_btn.config(state="normal" if done else "disabled")
                    self.status.config(text=f"Done. {ok} converted, {len(errors)} failed.")
                    if errors:
                        more = f"\n...and {len(errors) - 10} more." if len(errors) > 10 else ""
                        dialog("Some files failed", "\n".join(errors[:10]) + more, sound="error")
                    else:
                        dialog("Master Converter", f"Converted {ok} file(s) to {label}.",
                               sound="done")
                    return
        except queue.Empty:
            pass
        self.after(50, self.poll_convert, events, label)


class VideoPanel(tk.Frame):
    """The Videos tab: the files (details or thumbnails), options, and ffmpeg doing the work
    on a worker thread. The Audio tab reuses all of it, replacing only the parts marked
    "what's particular to videos" below."""
    NOUN, EXTS = "video", VIDEO_EXTS  # "3 video(s) selected", which files are accepted
    COLUMNS = [("name", "Name", 150, 90), ("length", "Length", 60, 50),
               ("res", "Resolution", 80, 75), ("status", "Status", 80, 60)]

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG, padx=10, pady=8)
        self.app = app
        self.items = []  # one dict per file: path, iid (row in the list), info (from probe_video)
        self.outdir, self.converted = "", []
        self.busy, self._cancel, self._proc = False, False, None
        self._probe_todo, self._probe_ready = queue.Queue(), queue.Queue()
        threading.Thread(target=self.probe_worker, daemon=True).start()
        self.after(100, self.poll_probes)

        # Files: details (like before) or thumbnails - Ctrl + mouse wheel switches
        box = tk.LabelFrame(self, text=" Files ", bg=BG, font=FONT, padx=6, pady=6)
        box.pack(fill="both", expand=True)
        self.selected, self.last_click = set(), None  # shared by both views
        btns = tk.Frame(box, bg=BG)
        # buttons packed first so a narrow window squeezes the list, not them
        btns.pack(side="right", fill="y", padx=(6, 0))
        self.lockable = []  # greyed out while converting
        for text, cmd in (("Add...", self.add_files), ("Remove", self.remove_files),
                          ("Clear", self.clear_files)):
            b = app.btn(btns, text, cmd)
            b.pack(fill="x", pady=2)
            self.lockable.append(b)
        self.files_box = FileBox(
            box, self, self.COLUMNS,
            f"Drag & drop {self.NOUN}s here\nor click Add..." if HAS_DND
            else f"Click Add... to choose {self.NOUN}s", view="thumbs", button_parent=btns)

        # Options
        opt = tk.LabelFrame(self, text=" Options ", bg=BG, font=FONT, padx=6, pady=6)
        opt.pack(fill="x", pady=8)
        self.build_options(opt, app)

        # Convert (turns into Cancel while running) + progress
        self.convert_btn = app.btn(self, "Convert", self.run, bold=True)
        self.convert_btn.pack(fill="x", pady=(0, 6), ipady=4)
        self.progress = ClassicProgress(self)
        self.progress.pack(fill="x")
        status_row = tk.Frame(self, bg=BG)
        status_row.pack(fill="x", pady=(4, 0))
        self.show_btn = app.btn(status_row, "Show converted files",
                                lambda: reveal_all(self.converted))
        self.show_btn.config(state="disabled")
        self.show_btn.pack(side="right")
        self.status = status_label(status_row, "Ready.")

        if not FFMPEG:
            self.convert_btn.config(state="disabled")
            self.status.config(text="This tab needs ffmpeg:  pip install imageio-ffmpeg",
                               fg="#C00000")
        if HAS_DND:
            for w in (self, box, *self.files_box.widgets()):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", app.on_drop)

    def save_to_row(self, opt, app, row):
        tk.Label(opt, text="Save to:", bg=BG, font=FONT).grid(row=row, column=0, sticky="w")
        self.outlabel = tk.Label(opt, text="(same folder as original)", bg="white",
                                 font=FONT, relief="sunken", bd=2, anchor="w", width=32)
        self.outlabel.grid(row=row, column=1, sticky="w", padx=6, pady=2)
        app.btn(opt, "Browse...", self.pick_outdir).grid(row=row, column=2)

    # ---- what's particular to videos (the Audio tab replaces these) ----
    def build_options(self, opt, app):
        tk.Label(opt, text="Convert to:", bg=BG, font=FONT).grid(row=0, column=0, sticky="w")
        self.fmt = tk.StringVar(value="MP4")
        ttk.Combobox(opt, textvariable=self.fmt, values=list(VIDEO_FORMATS), state="readonly",
                     width=17).grid(row=0, column=1, sticky="w", padx=6, pady=2)

        tk.Label(opt, text="Quality:", bg=BG, font=FONT).grid(row=1, column=0, sticky="w")
        self.quality = tk.IntVar(value=75)
        self.qscale = tk.Scale(opt, from_=1, to=100, orient="horizontal", variable=self.quality,
                               bg=BG, font=FONT, length=200, highlightthickness=0)
        self.qscale.grid(row=1, column=1, sticky="w", padx=6)
        self.qnote = tk.Label(opt, text="", bg=BG, fg="#888888", font=FONT)
        self.qnote.place(in_=self.qscale, relx=1.0, rely=1.0, x=6, y=-4, anchor="sw")

        tk.Label(opt, text="Size:", bg=BG, font=FONT).grid(row=2, column=0, sticky="w")
        row = tk.Frame(opt, bg=BG)
        row.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        self.size = tk.StringVar(value="Original")
        self.size_cb = ttk.Combobox(row, textvariable=self.size, values=list(VIDEO_SIZES),
                                    state="readonly", width=10)
        self.size_cb.pack(side="left")
        tk.Label(row, text="  Frame rate:", bg=BG, font=FONT).pack(side="left")
        self.fps = tk.StringVar(value="Original")
        self.fps_cb = ttk.Combobox(row, textvariable=self.fps, values=VIDEO_FPS,
                                   state="readonly", width=8)
        self.fps_cb.pack(side="left", padx=(4, 0))
        self.snote = tk.Label(opt, text="", bg=BG, fg="#888888", font=FONT)
        self.snote.place(in_=row, relx=1.0, rely=0.5, x=6, anchor="w")

        self.save_to_row(opt, app, 3)

        self.mute = tk.BooleanVar(value=False)
        self.mute_cb = tk.Checkbutton(opt, text="Remove sound", variable=self.mute, bg=BG,
                                      activebackground=BG, font=FONT)
        self.mute_cb.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.fmt.trace_add("write", lambda *_: self.on_format_change())
        self.on_format_change()

    def make_thumb(self, path, info):
        return video_thumb(path, info["duration"])

    def detail_text(self, it):
        """The third column / the tooltip's third line."""
        return self.res_text(it)

    DETAIL_TIP = "Dimensions"

    def job_settings(self):
        return (self.fmt.get(), self.quality.get(), VIDEO_SIZES[self.size.get()],
                None if self.fps.get() == "Original" else int(self.fps.get()), self.mute.get())

    def job_ext(self, settings):
        return VIDEO_FORMATS[settings[0]]

    def job_args(self, src, dst, settings, info):
        return ffmpeg_args(src, dst, *settings)

    def job_problem(self, info, settings):
        """A reason this file can't be converted with these settings, or None."""
        if self.job_ext(settings) == ".mp3" and not info["audio"]:
            return "has no sound to save as MP3"
        return None

    def on_format_change(self):
        """Enable only the options the chosen format uses; a grey note says why the rest are off."""
        label = self.fmt.get()
        ext = VIDEO_FORMATS[label]
        name = label.split(" ")[0]
        self.qscale.config(state="disabled" if ext == ".gif" else "normal",
                           fg="#999999" if ext == ".gif" else "black")
        self.qnote.config(text="(not used by GIF)" if ext == ".gif" else "")
        sound_only = ext == ".mp3"
        for cb in (self.size_cb, self.fps_cb):
            cb.config(state="disabled" if sound_only else "readonly")
        self.snote.config(text=f"(not used by {name})" if sound_only else "")
        self.mute_cb.config(state="disabled" if ext in (".gif", ".mp3") else "normal")
        if ext == ".gif":  # full-size, full-speed GIFs get huge: start from sensible limits
            if self.size.get() == "Original":
                self.size.set("480p")
            if self.fps.get() == "Original":
                self.fps.set("15")

    # ---- the file list ----
    def add_files(self):
        exts = " ".join("*" + e for e in self.EXTS)
        self.add_paths(filedialog.askopenfilenames(
            title=f"Select {self.NOUN}s",
            filetypes=[(f"{self.NOUN.capitalize()}s", exts), ("All files", "*.*")]))

    def add_paths(self, paths):
        if self.busy:
            return
        found, skipped = [], 0
        for p in paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    dirs.sort()
                    found += [os.path.join(root, f) for f in sorted(files)]
            else:
                found.append(p)
        known = {it["path"] for it in self.items}
        for p in found:
            if not p.lower().endswith(self.EXTS):
                skipped += 1
            elif p not in known:
                known.add(p)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = None
                self.items.append({"path": p, "info": None, "thumb": None, "status": "Ready",
                                   "bytes": size, "name": None})  # name: set by Rename
                self._probe_todo.put(p)
        msg = f"{len(self.items)} {self.NOUN}(s) selected."
        if skipped:
            msg += f" Skipped {skipped} unsupported."
        self.status.config(text=msg, fg="black")
        self.files_box.refresh()

    def probe_worker(self):
        """Off the main thread: length/size/sound, and a picture for the thumbnail view."""
        while True:
            path = self._probe_todo.get()
            info = probe_video(path) if FFMPEG else None
            thumb = self.make_thumb(path, info) if info else None
            self._probe_ready.put((path, info, thumb))

    def poll_probes(self):
        got = False
        try:
            while True:
                path, info, thumb = self._probe_ready.get_nowait()
                if info is None:
                    continue
                for item in self.items:  # every copy of it (Duplicate); none if removed
                    if item["path"] == path:
                        item["info"] = info
                        item["thumb"] = thumb or Image.new("RGBA", (64, 64), "#CCCCCC")
                        got = True
        except queue.Empty:
            pass
        if got:
            self.files_box.refresh_soon()
        self.after(100, self.poll_probes)

    @staticmethod
    def fmt_time(sec):
        sec = int(round(sec))
        h, rest = divmod(sec, 3600)
        return f"{h}:{rest // 60:02}:{rest % 60:02}" if h else f"{rest // 60}:{rest % 60:02}"

    # ---- what the two views (thumbnails / details) show - see ThumbGrid / DetailsList ----
    def length_text(self, it):
        if it["info"] is None:
            return "..."
        d = it["info"]["duration"]
        return self.fmt_time(d) if d is not None else "?"

    def res_text(self, it):
        if it["info"] is None:
            return "..."
        size = it["info"]["size"]
        return f"{size[0]} x {size[1]}" if size else "?"

    def grid_items(self):
        out = []
        for it in self.items:
            ext = os.path.splitext(it["path"])[1].lstrip(".").upper() or "FILE"
            # the second line shows the conversion status while there is one to show
            sub = (f"{ext} file, {self.length_text(it)}" if it["status"] == "Ready"
                   else it["status"])
            out.append((it["path"], self.display_name(it), sub))
        return out

    def grid_thumb(self, path):
        it = next((it for it in self.items if it["path"] == path), None)
        return it["thumb"] if it else None

    def list_rows(self):
        return [(self.display_name(it), self.length_text(it), self.detail_text(it),
                 it["status"]) for it in self.items]

    def out_base(self, it):
        """Name (without extension) the converted file will get."""
        return it["name"] or os.path.splitext(os.path.basename(it["path"]))[0]

    def display_name(self, it):
        """The name shown: the new name if renamed (with the original's extension)."""
        src = os.path.basename(it["path"])
        return (it["name"] + os.path.splitext(src)[1]) if it["name"] else src

    def tip_lines(self, i):
        it = self.items[i]
        ext = os.path.splitext(it["path"])[1].lstrip(".").upper() or "FILE"
        return [f"Item type: {ext} File", f"Length: {self.length_text(it)}",
                f"{self.DETAIL_TIP}: {self.detail_text(it)}",
                f"Size: {fmt_size(it['bytes']) if it['bytes'] is not None else 'unknown'}"]

    def context_menu(self, i, x, y):
        """Right-click on a file (either view): Rename / Duplicate / Go to file / Remove."""
        if self.busy:
            return
        menu = PopupMenu(self)  # drawn by the app: the same thin frame in light and dark
        menu.add_command(label="Rename", command=lambda: self.rename_item(i))
        menu.add_command(label="Duplicate", command=lambda: self.duplicate_item(i))
        menu.add_command(label="Go to file", command=lambda: go_to_file(self.items[i]["path"]))
        menu.add_separator()
        menu.add_command(label="Remove", command=self.remove_files)  # same as the button
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def rename_item(self, i):
        """Renames the converted file only - the original on disk is never touched."""
        def apply(name):
            self.items[i]["name"] = name
            self.status.config(text=f"Will be saved as \"{name}\".", fg="black")
            self.files_box.refresh()
        rename_dialog(self, self.out_base(self.items[i]), apply, lambda: self.busy)

    def duplicate_item(self, i):
        """Adds a copy right after it, to convert it twice (e.g. two sizes). Nothing is
        saved until Convert."""
        if self.busy:
            return
        it = self.items[i]
        name = copy_name(self.out_base(it), {self.out_base(x) for x in self.items})
        self.items.insert(i + 1, dict(it, name=name, status="Ready"))  # same file and info
        self.selected, self.last_click = {i + 1}, i + 1  # select the new copy
        self.status.config(text=f"Duplicated. {len(self.items)} {self.NOUN}(s) selected.",
                           fg="black")
        self.files_box.refresh()

    def set_status(self, i, text):
        self.items[i]["status"] = text
        self.files_box.refresh_row(i)

    def remove_files(self):
        if self.busy:
            return
        self.app.hide_tip()
        self.items = [it for i, it in enumerate(self.items) if i not in self.selected]
        self.selected, self.last_click = set(), None
        self.status.config(text=f"{len(self.items)} {self.NOUN}(s) selected.", fg="black")
        self.files_box.refresh()

    def clear_files(self):
        if self.busy:
            return
        self.app.hide_tip()
        self.items = []
        self.selected, self.last_click = set(), None
        self.status.config(text="Ready.", fg="black")
        self.files_box.refresh()

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir = d
            self.outlabel.config(text=d)

    # ---- converting ----
    def set_busy(self, busy):
        self.busy = busy
        for b in self.lockable:
            b.config(state="disabled" if busy else "normal")
        self.convert_btn.config(text="Cancel" if busy else "Convert",
                                command=self.cancel if busy else self.run)

    def run(self):
        if self.busy or not FFMPEG:
            return
        if not self.items:
            dialog("Master Converter", f"Add some {self.NOUN}s first.", sound="error")
            return
        label = self.fmt.get()
        jobs = [(i, it["path"], it["info"], self.out_base(it)) for i, it in enumerate(self.items)]
        settings = self.job_settings()
        for it in self.items:
            it["status"] = "Waiting"
        self.files_box.refresh()
        self.progress.config(maximum=len(jobs) * 100, value=0)
        self._cancel = False
        self.set_busy(True)
        events = queue.Queue()
        threading.Thread(target=self.convert_worker, args=(jobs, settings, self.outdir, events),
                         daemon=True).start()
        self.after(100, self.poll_convert, events, label)

    def cancel(self):
        self._cancel = True
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def convert_worker(self, jobs, settings, outdir, events):
        """Off the main thread: talks to the window only through `events`."""
        ext = self.job_ext(settings)
        done, errors = [], []
        for i, src, info, base in jobs:
            if self._cancel:
                break
            name = os.path.basename(src)
            events.put(("start", i, name))
            info = info or probe_video(src)  # not read yet (list added just now)
            problem = self.job_problem(info, settings)
            if problem:
                errors.append(f"{name}: {problem}")
                events.put(("end", i, "Failed"))
                continue
            dst = unique_path(outdir or os.path.dirname(src), base, ext)
            ok, msg = run_ffmpeg(self, self.job_args(src, dst, settings, info),
                                 dst, info["duration"],
                                 lambda frac, i=i: events.put(("frac", i, frac)))
            if ok:
                done.append(dst)
                events.put(("end", i, "Done"))
            elif self._cancel:
                events.put(("end", i, "Cancelled"))
                break
            else:
                errors.append(f"{name}: {msg}")
                events.put(("end", i, "Failed"))
        events.put(("done", done, errors))

    def poll_convert(self, events, label):
        try:
            while True:
                ev = events.get_nowait()
                kind = ev[0]
                if kind == "start":
                    _, i, name = ev
                    self.progress.config(value=i * 100)
                    self.status.config(text=f"Converting {name}...", fg="black")
                    self.set_status(i, "0%")
                    self.files_box.see(i)
                elif kind == "frac":
                    _, i, frac = ev
                    self.progress.config(value=i * 100 + frac * 100)
                    self.set_status(i, f"{int(frac * 100)}%")
                elif kind == "end":
                    _, i, result = ev
                    self.progress.config(value=(i + 1) * 100)
                    self.set_status(i, result)
                else:
                    self.finish(ev[1], ev[2], label)
                    return
        except queue.Empty:
            pass
        self.after(100, self.poll_convert, events, label)

    def finish(self, done, errors, label):
        cancelled = self._cancel
        self.set_busy(False)
        for it in self.items:  # anything never reached
            if it["status"] == "Waiting":
                it["status"] = "Skipped" if cancelled else "Ready"
        self.files_box.refresh()
        self.converted = done
        self.show_btn.config(state="normal" if done else "disabled")
        if cancelled:
            self.status.config(text=f"Cancelled. {len(done)} converted before stopping.")
            return
        self.status.config(text=f"Done. {len(done)} converted, {len(errors)} failed.")
        if errors:
            more = f"\n...and {len(errors) - 10} more." if len(errors) > 10 else ""
            dialog(f"Some {self.NOUN}s failed", "\n".join(errors[:10]) + more, sound="error")
        else:
            dialog("Master Converter", f"Converted {len(done)} {self.NOUN}(s) to {label}.",
                   sound="done")


class AudioPanel(VideoPanel):
    """The Audio tab: converts sound between formats (MP3, WAV, M4A, FLAC, OGG, OPUS, AIFF,
    WMA) - from audio files, or takes the sound out of videos. Everything but the options,
    the ffmpeg command and what the columns / thumbnails show comes from VideoPanel."""
    NOUN, EXTS = "audio file", VOICE_EXTS
    COLUMNS = [("name", "Name", 150, 90), ("length", "Length", 60, 50),
               ("sound", "Sound", 110, 75), ("status", "Status", 80, 60)]
    DETAIL_TIP = "Sound"

    def build_options(self, opt, app):
        tk.Label(opt, text="Convert to:", bg=BG, font=FONT).grid(row=0, column=0, sticky="w")
        self.fmt = tk.StringVar(value="MP3")
        ttk.Combobox(opt, textvariable=self.fmt, values=list(AUDIO_FORMATS), state="readonly",
                     width=17).grid(row=0, column=1, sticky="w", padx=6, pady=2)

        tk.Label(opt, text="Quality:", bg=BG, font=FONT).grid(row=1, column=0, sticky="w")
        self.bitrate = tk.StringVar(value="Original")  # keep each file's own quality
        self.bitrate_cb = ttk.Combobox(opt, textvariable=self.bitrate, values=AUDIO_BITRATES,
                                       state="readonly", width=10)
        self.bitrate_cb.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        self.qnote = tk.Label(opt, text="", bg=BG, fg="#888888", font=FONT)
        self.qnote.place(in_=self.bitrate_cb, relx=1.0, rely=0.5, x=6, anchor="w")

        tk.Label(opt, text="Sample rate:", bg=BG, font=FONT).grid(row=2, column=0, sticky="w")
        row = tk.Frame(opt, bg=BG)
        row.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        self.rate = tk.StringVar(value="Original")
        ttk.Combobox(row, textvariable=self.rate, values=AUDIO_RATES, state="readonly",
                     width=10).pack(side="left")
        tk.Label(row, text="  Channels:", bg=BG, font=FONT).pack(side="left")
        self.channels = tk.StringVar(value="Original")
        ttk.Combobox(row, textvariable=self.channels, values=list(AUDIO_CHANNELS),
                     state="readonly", width=8).pack(side="left", padx=(4, 0))

        self.save_to_row(opt, app, 3)
        for var in (self.fmt, self.bitrate):
            var.trace_add("write", lambda *_: self.on_format_change())
        self.on_format_change()

    def on_format_change(self):
        """Lossless formats keep everything, so there's no quality to choose for them.
        "Original" gets a note saying what it does."""
        lossless = AUDIO_FORMATS[self.fmt.get()] in LOSSLESS_AUDIO
        self.bitrate_cb.config(state="disabled" if lossless else "readonly")
        if lossless:
            note = f"(not used by {self.fmt.get()}: lossless)"
        elif self.bitrate.get() == "Original":
            note = "(each file keeps its own quality)"
        else:
            note = ""
        self.qnote.config(text=note)

    def tip_lines(self, i):
        lines = super().tip_lines(i)
        info = self.items[i]["info"] or {}
        if info.get("abitrate"):
            lines.insert(3, f"Bitrate: {info['abitrate']} kbps")
        return lines

    def make_thumb(self, path, info):
        return audio_wave_thumb(path) if info.get("audio") else None

    def detail_text(self, it):
        info = it["info"]
        if info is None:
            return "..."
        if not info.get("audio"):
            return "no sound"
        if "arate" not in info:
            return "?"
        return f"{info['arate'] / 1000:g} kHz, {info['alayout']}"

    def job_settings(self):
        rate = self.rate.get()
        quality = self.bitrate.get()
        return (self.fmt.get(), None if quality == "Original" else int(quality.split()[0]),
                None if rate == "Original" else int(rate.split()[0]),
                AUDIO_CHANNELS[self.channels.get()])

    def job_ext(self, settings):
        return AUDIO_FORMATS[settings[0]]

    def job_args(self, src, dst, settings, info):
        return audio_args(src, dst, *settings, info=info)  # info: "Original" needs the source's

    def job_problem(self, info, settings):
        return None if info["audio"] else "has no sound"


# the raised 3D edge Tk gives buttons on this background (sampled from the Add button)
EDGE_LIGHT, EDGE_SHADOW, EDGE_DARK = "#FFFFFF", "#8E8C82", "#000000"


class ClassicTabs(tk.Canvas):
    """Tabs with the same raised 3D edge as the buttons. They sit side by side on the page's
    border; the open one is a bit taller and wider and opens into the page below it."""
    H = 24

    def __init__(self, parent, on_change):
        super().__init__(parent, height=self.H, bg=BG, highlightthickness=0)
        self.on_change = on_change
        self.items, self.boxes, self.current = [], [], None  # boxes: (key, x0, x1)
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self.on_click)

    def add(self, key, text):
        self.items.append((key, text))
        self.draw()

    def select(self, key):
        if key != self.current:
            self.current = key
            self.draw()
            self.on_change(key)

    def step(self, d):
        keys = [k for k, _ in self.items]
        self.select(keys[(keys.index(self.current) + d) % len(keys)])
        return "break"

    def on_click(self, e):
        key = next((k for k, x0, x1 in self.boxes if x0 <= e.x < x1), None)
        if key:
            self.select(key)

    def draw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.H
        x, self.boxes, sel = 2, [], None
        for key, text in self.items:
            w = tkfont.Font(font=FONT).measure(text) + 24
            self.boxes.append((key, x, x + w))
            if key == self.current:
                sel = (x - 2, x + w + 2, text)  # drawn last, over its neighbours
            else:
                self.tab(x, 2, x + w, text, False)
            x += w
        # top edge of the page (2 px light, like the buttons), its shadow at the right end
        for y in (H - 2, H - 1):
            self.create_line(0, y, W - 3, y, fill=EDGE_LIGHT)
            self.create_line(W - 3, y, W - 2, y, fill=EDGE_SHADOW)
            self.create_line(W - 2, y, W, y, fill=EDGE_DARK)
        if sel:
            self.tab(sel[0], 0, sel[1], sel[2], True)

    def tab(self, x0, top, x1, text, selected):
        """One tab, edged exactly like the buttons: 2 px light on the left and top,
        2 px dark + 1 px shadow on the right, square corners.
        The open tab has no bottom edge, so it joins the page."""
        H = self.H
        bottom = H if selected else H - 2
        self.create_rectangle(x0, top, x1, bottom, fill=BG, outline="")
        for i in (0, 1):
            self.create_line(x0 + i, bottom, x0 + i, top + i, x1 - 1 - i, top + i, fill=EDGE_LIGHT)
            self.create_line(x1 - 1 - i, top + i, x1 - 1 - i, bottom, fill=EDGE_DARK)
        self.create_line(x1 - 3, top + 2, x1 - 3, bottom, fill=EDGE_SHADOW)
        if selected:  # clear the page's top edge under it so the tab opens into the page
            self.create_rectangle(x0 + 2, H - 2, x1 - 3, H, fill=BG, outline="")
        self.create_text((x0 + x1) // 2, (top + bottom) // 2 + (0 if selected else 1),
                         text=text, font=FONT, fill="black")


def framed_page(parent):
    """The raised 3D border around the pages (its top edge is drawn by the tabs).
    Returns the inner area the pages go in."""
    # same edge as the buttons: 2 px light left, 2 px dark + 1 px shadow right and bottom
    dark = tk.Frame(parent, bg=EDGE_DARK)
    dark.pack(fill="both", expand=True, padx=6, pady=(0, 6))
    light = tk.Frame(dark, bg=EDGE_LIGHT)
    light.pack(fill="both", expand=True, padx=(0, 2), pady=(0, 2))
    shadow = tk.Frame(light, bg=EDGE_SHADOW)
    shadow.pack(fill="both", expand=True, padx=(2, 0))
    area = tk.Frame(shadow, bg=BG)
    area.pack(fill="both", expand=True, padx=(0, 1), pady=(0, 1))
    return area


def xp_icon(kind):
    """Small black play / pause symbol for buttons (drawn, so no font glyph is needed)."""
    im = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if kind == "play":
        d.polygon([(4, 2), (13, 8), (4, 14)], fill="#000000")
    else:
        d.rectangle([3, 2, 6, 13], fill="#000000")
        d.rectangle([9, 2, 12, 13], fill="#000000")
    return ImageTk.PhotoImage(im)


def fmt_secs(t):
    m, s = divmod(max(t, 0), 60)
    return f"{int(m)}:{s:04.1f}"


GIF_WIDTHS = {"Original": None, "640 px": 640, "480 px": 480, "320 px": 320, "240 px": 240}
GIF_FPS = ["10 fps", "15 fps", "20 fps", "25 fps", "30 fps"]


def gif_filters(width, fps):
    """ffmpeg filters for a good-looking GIF: frame rate, shrink (never enlarge), own palette."""
    filters = [f"fps={fps}"]
    if width:
        filters.append(f"scale='min(iw,{width})':-1:flags=lanczos")
    filters.append("split[a][b];[a]palettegen=stats_mode=diff[p];"
                   "[b][p]paletteuse=dither=bayer:bayer_scale=5")
    return ",".join(filters)


def gif_bytes(src, start, length, width, fps):
    """Size in bytes of the GIF this piece of video would make (encoded to memory, not saved)."""
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                            "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src,
                            "-vf", gif_filters(width, fps), "-f", "gif", "-"],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           creationflags=NO_WINDOW, timeout=60)
    except Exception:
        return None
    return len(r.stdout) if r.returncode == 0 and r.stdout else None


def short_size(n):
    """1234567 -> '1.2 MB' (rounder than fmt_size: it's only an estimate)."""
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / 1024 / 1024:.1f} MB"


class GifPanel(tk.Frame):
    """The GIF Maker page: preview a video, pick a part of it on a filmstrip trim bar
    (drag the handles, click to move the playhead, Play to watch it) and save that part as a GIF."""
    STRIP_H, HANDLE_W, N_THUMBS, MIN_LEN = 72, 7, 24, 0.2
    RULER_H, CLIP_TITLE_H = 18, 13  # Movie Maker-style timeline: time ruler, then the track

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG, padx=10, pady=8)
        self.app = app
        self.src, self.info, self.load_id = None, None, 0
        self.start = self.end = self.pos = 0.0  # seconds
        self.thumbs = {}  # filmstrip frames: index -> PIL picture
        self._film_cache = None  # (key, picture): the strip without dimming, rebuilt on change
        self.drag = None  # "start" / "end" / "pos" / "move" while dragging on the strip
        self._move_from = None  # (mouse x, start, end, pos) when pressed inside the selection
        self.playing, self._play_id, self._play_proc = False, 0, None
        self.busy, self._cancel, self._proc = False, False, None
        self.outdir, self.converted = "", []
        self._events = queue.Queue()  # worker threads -> main thread
        self._seek_req, self._seek_evt = None, threading.Event()
        self._resize_job = None
        # size estimate: made on its own thread, newest request wins
        self._est_req, self._est_evt, self._est_job = None, threading.Event(), None
        threading.Thread(target=self.estimate_worker, daemon=True).start()
        threading.Thread(target=self.seek_worker, daemon=True).start()
        self.after(40, self.poll_events)
        self.icon_play, self.icon_pause = xp_icon("play"), xp_icon("pause")

        # Preview + trim bar
        box = tk.LabelFrame(self, text=" Preview ", bg=BG, font=FONT, padx=6, pady=6)
        box.pack(fill="both", expand=True)
        top = tk.Frame(box, bg=BG)
        top.pack(fill="x", pady=(0, 6))
        self.open_btn = app.btn(top, "Open video...", self.open_file)
        self.open_btn.pack(side="left")
        self.clear_btn = app.btn(top, "Clear", self.clear)
        self.clear_btn.pack(side="left", padx=(4, 0))
        self.file_label = tk.Label(top, text="No video opened.", bg=BG, font=FONT,
                                   fg="#666666", anchor="w")
        self.file_label.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.screen = tk.Canvas(box, bg="black", relief="sunken", bd=2, highlightthickness=0,
                                height=150)
        self.screen.pack(fill="both", expand=True)
        self.screen.bind("<Configure>", lambda e: self.on_screen_resize())

        bar = tk.Frame(box, bg=BG)
        bar.pack(fill="x", pady=(6, 0))
        self.play_btn = tk.Button(bar, image=self.icon_play, command=self.toggle_play, bg=BG,
                                  relief="raised", bd=3, activebackground="#F5F3E8", width=30)
        self.play_btn.pack(side="left", fill="y")
        # sunken like the preview screen; the play button stretches to the same height
        self.strip = tk.Canvas(bar, height=self.STRIP_H - 2 * self.STRIP_BD, bg="white",
                               relief="sunken", bd=self.STRIP_BD, highlightthickness=0)
        self._tick_font = tkfont.Font(family="Tahoma", size=7)
        self.strip.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self.strip.bind("<Configure>", lambda e: self.draw_strip())
        self.strip.bind("<Button-1>", self.on_strip_press)
        self.strip.bind("<B1-Motion>", self.on_strip_drag)
        self.strip.bind("<ButtonRelease-1>", self.on_strip_release)
        self.strip.bind("<Motion>", self.on_strip_motion)
        times = tk.Frame(box, bg=BG)
        times.pack(fill="x", pady=(4, 0))
        self.range_label = tk.Label(times, text="", bg=BG, font=FONT, anchor="w")
        self.range_label.pack(side="left")
        self.size_label = tk.Label(times, text="", bg=BG, font=FONT, fg="#666666")
        self.size_label.pack(side="left", padx=(8, 0))
        self.pos_label = tk.Label(times, text="", bg=BG, font=FONT, fg="#666666")
        self.pos_label.pack(side="right")

        # Options
        opt = tk.LabelFrame(self, text=" GIF options ", bg=BG, font=FONT, padx=6, pady=6)
        opt.pack(fill="x", pady=8)
        tk.Label(opt, text="Width:", bg=BG, font=FONT).grid(row=0, column=0, sticky="w")
        row = tk.Frame(opt, bg=BG)
        row.grid(row=0, column=1, sticky="w", padx=6, pady=2)
        self.width = tk.StringVar(value="480 px")
        ttk.Combobox(row, textvariable=self.width, values=list(GIF_WIDTHS), state="readonly",
                     width=10).pack(side="left")
        tk.Label(row, text="  Frame rate:", bg=BG, font=FONT).pack(side="left")
        self.fps = tk.StringVar(value="15 fps")
        ttk.Combobox(row, textvariable=self.fps, values=GIF_FPS, state="readonly",
                     width=8).pack(side="left", padx=(4, 0))
        for var in (self.width, self.fps):  # both change the size
            var.trace_add("write", lambda *_: self.schedule_estimate())

        tk.Label(opt, text="Plays:", bg=BG, font=FONT).grid(row=1, column=0, sticky="w")
        row = tk.Frame(opt, bg=BG)
        row.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        self.loop = tk.BooleanVar(value=True)
        for text, val in (("Loop forever", True), ("Play once", False)):
            tk.Radiobutton(row, text=text, variable=self.loop, value=val, bg=BG,
                           activebackground=BG, font=FONT).pack(side="left", padx=(0, 10))

        tk.Label(opt, text="Save to:", bg=BG, font=FONT).grid(row=2, column=0, sticky="w")
        self.outlabel = tk.Label(opt, text="(same folder as original)", bg="white",
                                 font=FONT, relief="sunken", bd=2, anchor="w", width=32)
        self.outlabel.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        app.btn(opt, "Browse...", self.pick_outdir).grid(row=2, column=2)

        # Make GIF (turns into Cancel while running) + progress
        self.make_btn = app.btn(self, "Make GIF", self.make_gif, bold=True)
        self.make_btn.pack(fill="x", pady=(0, 6), ipady=4)
        self.progress = ClassicProgress(self, maximum=100)
        self.progress.pack(fill="x")
        status_row = tk.Frame(self, bg=BG)
        status_row.pack(fill="x", pady=(4, 0))
        self.show_btn = app.btn(status_row, "Show GIF", lambda: reveal_all(self.converted))
        self.show_btn.config(state="disabled")
        self.show_btn.pack(side="right")
        self.status = status_label(status_row, "Open a video or GIF to start.")

        if not FFMPEG:
            for b in (self.open_btn, self.clear_btn, self.make_btn):
                b.config(state="disabled")
            self.status.config(text="GIF Maker needs ffmpeg:  pip install imageio-ffmpeg",
                               fg="#C00000")
        if HAS_DND:
            for w in (self, box, self.screen, self.strip):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", app.on_drop)

    # ---- opening a file ----
    def open_file(self):
        exts = " ".join("*" + e for e in VIDEO_EXTS)
        path = filedialog.askopenfilename(
            title="Open a video or GIF", filetypes=[("Videos and GIFs", exts), ("All files", "*.*")])
        if path:
            self.load(path)

    def add_paths(self, paths):
        """Drag & drop: open the first video in what was dropped."""
        video = next((p for p in paths if p.lower().endswith(VIDEO_EXTS)), None)
        if video:
            self.load(video)
        else:
            self.status.config(text="That's not a video or GIF.", fg="black")

    def load(self, path):
        if self.busy or not FFMPEG:
            return
        self.stop_play()
        self.load_id += 1
        self.src, self.info, self.thumbs, self._film_cache = path, None, {}, None
        self.screen.delete("all")
        self.file_label.config(text=os.path.basename(path), fg="black")
        self.status.config(text="Reading the video...", fg="black")
        self.draw_strip()
        self.update_times()
        self.schedule_estimate()  # no video info yet: just clears the old size
        threading.Thread(target=self.load_worker, args=(self.load_id, path), daemon=True).start()

    def clear(self):
        """Close the video and put the page back to how it started."""
        if self.busy:
            return
        self.stop_play()
        self.load_id += 1  # late frames/thumbnails from the old file get ignored
        self.src, self.info, self.thumbs, self._film_cache = None, None, {}, None
        self.start = self.end = self.pos = 0.0
        self.screen.delete("all")
        self.file_label.config(text="No video opened.", fg="#666666")
        self.progress.config(value=0)
        self.status.config(text="Open a video or GIF to start.", fg="black")
        self.draw_strip()
        self.update_times()
        self.schedule_estimate()  # clears the size

    def load_worker(self, load_id, path):
        info = probe_video(path)
        self._events.put(("info", load_id, info))
        if not info["duration"]:
            return
        for k in range(self.N_THUMBS):  # filmstrip frames, spread evenly over the video
            if load_id != self.load_id:
                return  # another file was opened meanwhile
            t = (k + 0.5) * info["duration"] / self.N_THUMBS
            img = (grab_frame(path, t, 64, 64, fill=True)  # near the very end there may be
                   or grab_frame(path, max(t - 0.5, 0), 64, 64, fill=True))  # no frame: step back
            self._events.put(("thumb", load_id, k, img))

    def on_info(self, info):
        d = info["duration"]
        if not d or not info["size"]:
            self.info = None
            self.status.config(text="Can't read this file as a video.", fg="#C00000")
            return
        self.info = info
        w, h = info["size"]
        self.file_label.config(text=f"{os.path.basename(self.src)}   ({w} x {h}, {fmt_secs(d)})")
        self.start, self.pos = 0.0, 0.0
        if d > 15:  # long video: start with a GIF-sized piece
            self.end = 10.0
            self.status.config(text="Selected the first 10 s - drag the handles to pick "
                                    "the part you want.")
        else:
            self.end = d
            self.status.config(text="Drag the handles to pick the part you want, then Make GIF.")
        self.draw_strip()
        self.update_times()
        self.schedule_seek(0)
        self.schedule_estimate()

    # ---- preview screen ----
    def screen_size(self):
        return max(16, self.screen.winfo_width() - 4), max(16, self.screen.winfo_height() - 4)

    def show(self, img):
        self._screen_photo = ImageTk.PhotoImage(img)  # keep a reference or Tk drops it
        self.screen.delete("all")
        w, h = self.screen.winfo_width(), self.screen.winfo_height()
        self.screen.create_image(w // 2, h // 2, image=self._screen_photo)

    def on_screen_resize(self):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(150, lambda: self.info and not self.playing
                                      and self.schedule_seek(self.pos))

    def schedule_seek(self, t):
        """Show the frame at `t`. Only the newest request matters, so quick drags stay smooth."""
        if self.info:
            self._seek_req = (self.load_id, self.src, min(t, self.info["duration"] - 0.05),
                              self.screen_size())
            self._seek_evt.set()

    def seek_worker(self):
        while True:
            self._seek_evt.wait()
            self._seek_evt.clear()
            load_id, src, t, (w, h) = self._seek_req
            img = grab_frame(src, t, w, h) or grab_frame(src, max(t - 0.5, 0), w, h)
            if img is not None:
                self._events.put(("frame", load_id, img))

    def poll_events(self):
        try:
            while True:
                ev = self._events.get_nowait()
                if ev[1] != self.load_id:
                    continue  # result for a file that's no longer open
                if ev[0] == "info":
                    self.on_info(ev[2])
                elif ev[0] == "thumb" and ev[3] is not None:
                    self.thumbs[ev[2]] = ev[3]
                    self._film_cache = None
                    self.draw_strip()
                elif ev[0] == "frame" and not self.playing:
                    self.show(ev[2])
                elif ev[0] == "estimate" and ev[2] is self._est_req:  # skip outdated ones
                    est = ev[3]
                    self.size_label.config(text=f"about {short_size(est)}" if est else "")
        except queue.Empty:
            pass
        self.after(40, self.poll_events)

    # ---- the trim bar ----
    STRIP_BD = 2  # sunken border, same as the preview screen

    def inner_box(self):
        """x0, y0, x1, y1 of the strip inside its border (ruler on top, track below)."""
        B = self.STRIP_BD
        return (B, B, max(B + 2 * self.HANDLE_W + 10, self.strip.winfo_width() - B),
                max(B + self.RULER_H + 20, self.strip.winfo_height() - B))

    def track_top(self):
        return self.inner_box()[1] + self.RULER_H + 1

    def film_box(self):
        """x0, x1 of the time line: inset a little at each end, so a trim edge pulled all
        the way out can still be grabbed."""
        x0, _, x1, _ = self.inner_box()
        return x0 + self.HANDLE_W, x1 - self.HANDLE_W

    def t2x(self, t):
        x0, x1 = self.film_box()
        return x0 + (x1 - x0) * t / self.info["duration"]

    def x2t(self, x):
        x0, x1 = self.film_box()
        return min(max((x - x0) / (x1 - x0), 0), 1) * self.info["duration"]

    def film_image(self, fw, fh):
        """The strip of frames across the whole bar, square cells, each showing the frame
        nearest the time under its middle."""
        key = (fw, fh, len(self.thumbs), DARK_MODE)
        if self._film_cache and self._film_cache[0] == key:
            return self._film_cache[1]
        film = Image.new("RGB", (fw, fh), themed("#D8D8D8"))  # grey until the frames load
        x0 = self.inner_box()[0]
        cells = max(1, round(fw / fh))
        for c in range(cells):
            cx0, cx1 = round(c * fw / cells), round((c + 1) * fw / cells)
            t = self.x2t(x0 + (cx0 + cx1) / 2)
            k = min(self.N_THUMBS - 1, int(t / self.info["duration"] * self.N_THUMBS))
            th = self.thumbs.get(k)
            if th is not None:
                film.paste(th.resize((cx1 - cx0, fh), Image.BILINEAR), (cx0, 0))
        self._film_cache = (key, film)
        return film

    # colours of the Movie Maker (XP) timeline
    TL_LINE, TL_DIM, TL_CLIP, TL_HEAD, TL_HEAD_EDGE = "#A0A0A0", "#9A9A9A", "#5A5A5A", "#5A8BE0", "#1F4FAE"

    @staticmethod
    def timecode(t):
        """0:00:02.00 - hours:minutes:seconds.hundredths, like Movie Maker."""
        cs = int(round(max(t, 0) * 100))
        h, cs = divmod(cs, 360000)
        m, cs = divmod(cs, 6000)
        return f"{h}:{m:02}:{cs // 100:02}.{cs % 100:02}"

    def ruler_step(self):
        """Seconds between labelled ticks: the smallest 'nice' step whose labels don't touch."""
        x0, x1 = self.film_box()
        per_sec = (x1 - x0) / self.info["duration"]
        label_w = self._tick_font.measure("0:00:00.00") + 12
        for step in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600):
            if step * per_sec >= label_w:
                return step
        return 3600

    def draw_strip(self):
        c = self.strip
        c.delete("all")
        ix0, iy0, ix1, iy1 = self.inner_box()
        ry1 = iy0 + self.RULER_H  # bottom of the time ruler
        ty0 = ry1 + 1  # top of the track
        c.create_rectangle(ix0, iy0, ix1, ry1, fill="white", outline="")  # ruler
        c.create_line(ix0, ry1, ix1, ry1, fill=self.TL_LINE)
        c.create_rectangle(ix0, ty0, ix1 - 1, iy1 - 1, fill="white", outline=self.TL_LINE,
                           dash=(1, 1))  # track, dotted border
        if not self.info:
            c.create_text((ix0 + ix1) // 2, (ty0 + iy1) // 2, font=FONT, fill="#999999",
                          text="Open a video to see its frames here")
            return
        # time ruler: labelled ticks at a readable spacing, small ticks in between
        step = self.ruler_step()
        d = self.info["duration"]
        n = 0
        while n * step / 5 <= d + 1e-9:
            t = n * step / 5
            x = round(self.t2x(t))
            major = n % 5 == 0
            c.create_line(x, ry1 - (6 if major else 3), x, ry1, fill="#606060")
            label = self.timecode(t)
            if major and x + 2 + self._tick_font.measure(label) <= ix1:  # skip if it'd be cut off
                c.create_text(x + 2, iy0 + 1, text=label, anchor="nw",
                              font=self._tick_font, fill="#303030")
            n += 1
        # frames along the track, under a white title band like Movie Maker's clips
        fy0 = ty0 + self.CLIP_TITLE_H + 1
        fw, fh = ix1 - ix0 - 2, iy1 - 2 - fy0
        film = self.film_image(fw, fh).copy()
        xs, xe = round(self.t2x(self.start)), round(self.t2x(self.end))
        for a, b in ((0, xs - ix0 - 1), (xe - ix0 - 1, fw)):  # grey out the parts left out
            if b > a:
                part = film.crop((a, 0, b, fh))
                film.paste(Image.blend(part, Image.new("RGB", part.size, themed(self.TL_DIM)),
                                       0.65), (a, 0))
        self._film_photo = ImageTk.PhotoImage(film)
        c.create_image(ix0 + 1, fy0, image=self._film_photo, anchor="nw")
        # the chosen part is the "clip": a boxed piece with the file's name on its title band
        c.create_rectangle(xs, ty0 + 1, xe, iy1 - 2, outline=self.TL_CLIP)
        c.create_line(xs + 1, fy0 - 1, xe, fy0 - 1, fill=self.TL_LINE)
        name = os.path.splitext(os.path.basename(self.src))[0]
        c.create_text(xs + 4, ty0 + 1, anchor="nw", font=self._tick_font, fill="black",
                      text=fit_text(self._tick_font, name, max(0, xe - xs - 10))
                      if xe - xs > 24 else "")
        my = (fy0 + iy1) / 2  # trim markers: small black triangles on the clip's edges
        # (white outline so they still show on dark video)
        c.create_polygon(xs + 1, my - 6, xs + 7, my, xs + 1, my + 6, fill="black", outline="white")
        c.create_polygon(xe - 1, my - 6, xe - 7, my, xe - 1, my + 6, fill="black", outline="white")
        self.draw_playhead()

    def draw_playhead(self):
        """Blue line down through the track, with a small blue square on the ruler."""
        c = self.strip
        c.delete("playhead")
        _, iy0, _, iy1 = self.inner_box()
        xp = round(self.t2x(self.pos))
        c.create_line(xp, iy0 + 2, xp, iy1 - 1, fill=self.TL_HEAD_EDGE, tags="playhead")
        c.create_rectangle(xp - 4, iy0 + 3, xp + 4, iy0 + 13, fill=self.TL_HEAD,
                           outline=self.TL_HEAD_EDGE, tags="playhead")
        c.create_line(xp - 2, iy0 + 5, xp + 3, iy0 + 5, fill="#A9C4F5", tags="playhead")  # shine

    def move_playhead(self):
        if self.info:
            self.draw_playhead()
        self.update_times()

    def handle_at(self, x, y):
        """'start' / 'end' when (x, y) is on one of the clip's edges in the track."""
        if y < self.track_top():
            return None  # the ruler is for moving the playhead only
        xs, xe, HW = self.t2x(self.start), self.t2x(self.end), self.HANDLE_W
        near_s, near_e = abs(x - xs) <= HW, abs(x - xe) <= HW
        if near_s and near_e:  # edges close together: pick the closer one
            return "start" if abs(x - xs) < abs(x - xe) else "end"
        return "start" if near_s else "end" if near_e else None

    def on_strip_motion(self, e):
        if self.info and not self.drag:
            self.strip.config(cursor="sb_h_double_arrow" if self.handle_at(e.x, e.y) else "")

    def on_strip_press(self, e):
        if not self.info:
            return
        self.stop_play()
        self.drag = self.handle_at(e.x, e.y) or "pos"
        # remember where on the handle it was grabbed, so it doesn't jump under the mouse
        edge = {"start": self.t2x(self.start), "end": self.t2x(self.end)}.get(self.drag, e.x)
        self._grab_dx = e.x - edge
        inside = (self.drag == "pos" and e.y >= self.track_top()
                  and self.t2x(self.start) < e.x < self.t2x(self.end))
        self.on_strip_drag(e)  # a click still moves the playhead first...
        # ...and if the mouse then moves, a press inside the selection slides the whole selection
        self._move_from = (e.x, self.start, self.end, self.pos) if inside else None

    def on_strip_release(self, e):
        self.drag, self._move_from = None, None
        self.strip.config(cursor="")
        self.on_strip_motion(e)

    def on_strip_drag(self, e):
        if not self.info or not self.drag:
            return
        d = self.info["duration"]
        if self.drag == "pos" and self._move_from and abs(e.x - self._move_from[0]) >= 4:
            self.drag = "move"  # moved past a small wiggle: it's a drag, not a click
            self.strip.config(cursor="fleur")
        if self.drag == "move":
            px, s0, e0, p0 = self._move_from
            x0, x1 = self.film_box()
            length = e0 - s0
            self.start = min(max(s0 + (e.x - px) / (x1 - x0) * d, 0), d - length)
            self.end = self.start + length
            self.pos = p0 + (self.start - s0)  # playhead rides along with the selection
            self.draw_strip()
            self.update_times()
            self.schedule_seek(self.pos)
            self.schedule_estimate()
            return
        t = self.x2t(e.x - self._grab_dx)
        gap = min(self.MIN_LEN, d)
        if self.drag == "start":
            self.start = min(t, self.end - gap)
            self.pos = self.start
        elif self.drag == "end":
            self.end = max(t, self.start + gap)
            self.pos = self.end
        else:
            self.pos = min(max(t, self.start), self.end)
        if self.drag in ("start", "end"):
            self.schedule_estimate()
        self.draw_strip()
        self.update_times()
        self.schedule_seek(self.pos)

    # ---- expected GIF size ----
    def schedule_estimate(self):
        """Re-estimate shortly after the selection or settings stop changing."""
        if self._est_job:
            self.after_cancel(self._est_job)
            self._est_job = None
        self._est_req = None  # anything still being worked out is now outdated
        if not self.info:
            self.size_label.config(text="")
            return
        self.size_label.config(text="working out size...")
        self._est_job = self.after(400, self.request_estimate)

    def request_estimate(self):
        self._est_job = None
        self._est_req = (self.load_id, self.src, self.start, self.end,
                         GIF_WIDTHS[self.width.get()], self.preview_fps())
        self._est_evt.set()

    def estimate_worker(self):
        while True:
            self._est_evt.wait()
            self._est_evt.clear()
            req = self._est_req
            if req is None:
                continue
            load_id, src, start, end, width, fps = req
            length = end - start
            if length <= 6:  # short: make the whole thing, so the number is exact
                est = gif_bytes(src, start, length, width, fps)
            else:
                est = self.sampled_estimate(req)
            if self._est_req is req:  # skip it if the selection changed meanwhile
                self._events.put(("estimate", load_id, req, est))

    def sampled_estimate(self, req):
        """Long selections: make four 1.5 s samples spread over it and scale up.
        A GIF stores its first frame whole and then only what changes, so each sample's
        first frame is measured separately and counted once, not once per sample
        (without that, busy videos came out ~25% too big; with it, within a few %)."""
        _, src, start, end, width, fps = req
        length, slen, n = end - start, 1.5, 4
        first = changes = 0
        for i in range(n):
            if self._est_req is not req:
                return None  # outdated: stop early
            t = start + (length - slen) * i / (n - 1)
            whole = gif_bytes(src, t, slen, width, fps)
            one = gif_bytes(src, t, 1 / fps, width, fps)
            if not whole or not one:
                return None
            first, changes = first + one, changes + whole - one
        return first / n + changes / (n * (slen - 1 / fps)) * (length - 1 / fps)

    def update_times(self):
        if not self.info:
            self.range_label.config(text="")
            self.pos_label.config(text="")
            return
        self.range_label.config(text=f"Selected:  {fmt_secs(self.start)} - {fmt_secs(self.end)}"
                                     f"   ({self.end - self.start:.1f} s)")
        self.pos_label.config(text=fmt_secs(self.pos))

    # ---- playing the chosen part ----
    def preview_fps(self):
        return int(self.fps.get().split()[0])  # preview at the GIF's own frame rate

    def toggle_play(self):
        if self.playing:
            self.stop_play()
        elif self.info:
            if self.pos >= self.end - 0.05 or self.pos < self.start:
                self.pos = self.start
            self.start_play(self.pos)

    def start_play(self, t0):
        self.stop_play()
        fps, (w, h) = self.preview_fps(), self.screen_size()
        self._play_id += 1
        pid = self._play_id
        self.playing = True
        self.play_btn.config(image=self.icon_pause)
        args = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-ss", f"{t0:.3f}", "-t", f"{max(self.end - t0, 0.05):.3f}", "-i", self.src,
                "-vf", f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease,"
                       f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError:
            self.stop_play()
            return
        self._play_proc = proc
        frames = queue.Queue(maxsize=fps * 2)  # small buffer: the decoder waits, memory stays low
        threading.Thread(target=self.play_reader, args=(proc, frames, pid, w, h),
                         daemon=True).start()
        self._play_t0, self._play_n, self._next_due = t0, 0, time.monotonic()
        self.after(1, self.play_tick, pid, frames, fps)

    def play_reader(self, proc, frames, pid, w, h):
        size = w * h * 3
        while pid == self._play_id:
            data = proc.stdout.read(size)
            img = Image.frombytes("RGB", (w, h), data) if len(data) == size else None
            while pid == self._play_id:
                try:
                    frames.put(img, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if img is None:
                break
        if proc.poll() is None:
            proc.kill()

    def play_tick(self, pid, frames, fps):
        if pid != self._play_id:
            return  # stopped
        try:
            img = frames.get_nowait()
        except queue.Empty:  # decoder not ready yet
            self.after(5, self.play_tick, pid, frames, fps)
            return
        if img is None:  # reached the end of the chosen part
            if self.loop.get():
                self.start_play(self.start)  # preview loops like the GIF will
            else:
                self.stop_play()
            return
        self.show(img)
        self.pos = min(self._play_t0 + self._play_n / fps, self.end)
        self._play_n += 1
        self.move_playhead()
        self._next_due = max(self._next_due + 1 / fps, time.monotonic())
        self.after(max(1, int((self._next_due - time.monotonic()) * 1000)),
                   self.play_tick, pid, frames, fps)

    def stop_play(self):
        self._play_id += 1  # makes the reader thread and the tick loop quit
        self.playing = False
        self.play_btn.config(image=self.icon_play)
        proc, self._play_proc = self._play_proc, None
        if proc and proc.poll() is None:
            proc.kill()

    # ---- making the GIF ----
    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir = d
            self.outlabel.config(text=d)

    def set_busy(self, busy):
        self.busy = busy
        for b in (self.open_btn, self.clear_btn):
            b.config(state="disabled" if busy else "normal")
        self.make_btn.config(text="Cancel" if busy else "Make GIF",
                             command=self.cancel if busy else self.make_gif)

    def cancel(self):
        self._cancel = True
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()

    def make_gif(self):
        if self.busy or not FFMPEG:
            return
        if not self.info:
            dialog("Master Converter", "Open a video first.", sound="error")
            return
        self.stop_play()
        start, length = self.start, self.end - self.start
        width = GIF_WIDTHS[self.width.get()]
        base = os.path.splitext(os.path.basename(self.src))[0]
        dst = unique_path(self.outdir or os.path.dirname(self.src), base, ".gif")
        args = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
                "-progress", "pipe:1", "-n", "-ss", f"{start:.3f}", "-t", f"{length:.3f}",
                "-i", self.src, "-vf", gif_filters(width, self.preview_fps()),
                "-loop", "0" if self.loop.get() else "-1", dst]  # -1 = play once
        self._cancel = False
        self.set_busy(True)
        self.progress.config(value=0)
        self.status.config(text="Making the GIF...", fg="black")
        events = queue.Queue()

        def work():
            ok, msg = run_ffmpeg(self, args, dst, length, lambda f: events.put(("frac", f)))
            events.put(("done", ok, msg))
        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.poll_make, events, dst)

    def poll_make(self, events, dst):
        try:
            while True:
                ev = events.get_nowait()
                if ev[0] == "frac":
                    self.progress.config(value=ev[1] * 100)
                    continue
                ok, msg = ev[1], ev[2]
                cancelled = self._cancel
                self.set_busy(False)
                if ok:
                    self.progress.config(value=100)
                    self.converted = [dst]
                    self.show_btn.config(state="normal")
                    size = App.fmt_size(os.path.getsize(dst))
                    self.status.config(text=f"Saved {os.path.basename(dst)} ({size}).")
                    # a message box with the done chime, like the other tabs
                    dialog("Master Converter", f"Made {os.path.basename(dst)} ({size}).",
                           sound="done")
                elif cancelled:
                    self.progress.config(value=0)
                    self.status.config(text="Cancelled.")
                else:
                    self.status.config(text="Couldn't make the GIF.", fg="#C00000")
                    dialog("GIF Maker", msg, sound="error")
                return
        except queue.Empty:
            pass
        self.after(100, self.poll_make, events, dst)


if __name__ == "__main__":
    if sys.platform == "win32":
        try:  # lets Windows show our icon on the taskbar instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MasterConverter.App")
        except Exception:
            pass
    App().mainloop()
