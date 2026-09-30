"""
Master Converter - converts images, videos and sound, and makes GIFs. Runs 100% locally.
Setup:   pip install -r requirements.txt
Run:     python master_converter.py
"""
import base64
import colorsys
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


# ---- themes ----
# The app is written with the Windows 98 Ivory (beige) colours. In any other theme, every
# colour it gives Tk - when making a widget, changing one, or drawing on a canvas - goes
# through theme_color() first, so the whole app changes without each colour being handled
# one by one. Some colours depend on their role: white is a box's background in one place and
# a 3D edge's highlight in another. A new theme is just a new table in THEME_COLORS.
THEME = "xp"  # the theme the widgets are in now (they're made in "xp", then switched)
DARK_MODE = False  # THEME == "dark"
DARK_FACE, DARK_BOX, DARK_TEXT = "#353535", "#1E1E1E", "#E8E8E8"
THEME_NAMES = {"98": "Windows 98", "xp": "Windows 98 Ivory", "dark": "Windows 98 Dark",
               "pink": "Windows 98 Pink", "jungle": "Windows 98 Jungle"}
DEFAULT_THEME = "98"
THEME_COLORS = {  # theme -> ({written colour: its colour in this theme}, {role: {...}})
    "xp": ({}, {}),
    "98": ({  # Windows 95 / 98: grey face, white boxes, black / grey / white 3D edges
        "#ece9d8": "#C0C0C0",  # window / button face (BG)
        "#f5f3e8": "#C8C8C8",  # pressed button face
        "#8e8c82": "#808080",  # 3D edges' shadow
        "#efefef": "#E0E0E0",  # scrollbar track
        "#ebe8d7": "#BFBFBF",  # under the menu bar's line: the face, a touch darker
        "#f7f6f0": "#DFDFDF",  # the lists' scrollbar trough
        "#ffffe1": "#DEDEDE",  # hover tooltip (details of a file): light grey, not yellow
        "#d4d0c8": "#C3C3C3",  # an inactive window's title text: neutral grey
    }, {}),
    "dark": ({
        "#ece9d8": DARK_FACE,  # window / button face (BG)
        "#f5f3e8": "#474747",  # pressed button face
        "#ffffe1": "#404040",  # tooltip: a dark grey
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
        "#ebe8d7": "#333333",  # under the menu bar's line: a touch darker in dark mode
        "#0000ee": "#8AB4FF",  # links (About's GitHub link): a light blue that reads on dark
        "#f7f6f0": "#262626",  # the lists' scrollbar trough
    }, {  # colours that change differently depending on what they're for
        "text": {"#000000": DARK_TEXT, "#ffffff": "#ffffff"},  # (white text stays white)
        "box": {"#ffffff": DARK_BOX, "#000000": DARK_TEXT},  # box backgrounds; black shapes
        "edge": {"#ffffff": "#5e5e5e", "#8e8c82": "#1b1b1b", "#000000": "#000000"},  # 3D edges
    }),
    "pink": ({  # pink windows, teal title bars (colours taken from the reference picture)
        "#ece9d8": "#F3C0C9",  # window / button face (BG)
        "#f5f3e8": "#F6CFD6",  # pressed button face
        "#8e8c82": "#E0566E",  # 3D edges' shadow
        "#efefef": "#F9DFE3",  # scrollbar track
        "#ebe8d7": "#E9B3BD",  # under the menu bar's line: the face, a touch darker
        "#f7f6f0": "#F9DFE2",  # the lists' scrollbar trough
        "#ffffe1": "#CBFEFE",  # hover tooltip: light cyan
        "#999999": "#DE4B65",  # greyed-out text
        "#808080": "#DD4A64",  # ... (the right-click menu's)
        "#cccccc": "#EDA9B5",  # scrollbar thumb
        "#a6a6a6": "#E7919F",  # ... under the mouse
        "#606060": "#B84A5D",  # ... held down / ruler ticks
        "#dadada": "#F7D6DC",  # scrollbar arrow under the mouse
        "#5f5f5f": "#84414D",  # scrollbar arrows
        "#b0b0b0": "#E7A2AE",  # picture box border
        "#a0a0a0": "#E29AA7",  # timeline lines
        "#e4e4e4": "#F7E3E6",  # picture still loading
        "#d8d8d8": "#F2D2D8",  # filmstrip still loading
        "#888888": "#BD6878",  # hint text
        "#666666": "#7E3F4B",  # grey text
        "#7da2ce": "#6FC9BE",  # a picture box under the mouse
        "#245edc": "#00A9A6",  # the dragged selection rectangle
        "#5a8be0": "#3CC4C0",  # GIF Maker's playhead
        "#1f4fae": "#008C89",  # ... its edge
        "#a9c4f5": "#B5EFEB",  # ... its shine
    }, {
        "box": {"#ffffff": "#FFF0F4"},  # box backgrounds: a very light pink
        "edge": {"#ffffff": "#F8DDDE", "#000000": "#704049"},  # 3D edges: light / darkest
    }),
    "jungle": ({  # Windows 98's Jungle desktop theme (colours taken from the reference picture)
        "#ece9d8": "#B69F67",  # window / button face (BG)
        "#f5f3e8": "#C1AB75",  # pressed button face
        "#8e8c82": "#685832",  # 3D edges' shadow
        "#efefef": "#DFD0B8",  # scrollbar track
        "#ebe8d7": "#AA935C",  # under the menu bar's line: the face, a touch darker
        "#f7f6f0": "#DFD0B7",  # the lists' scrollbar trough
        "#ffffe1": "#EDE2C6",  # hover tooltip: light khaki
        "#999999": "#D2BE90",  # greyed-out text
        "#808080": "#D1BD8F",  # ... (the right-click menu's)
        "#cccccc": "#C9B587",  # scrollbar thumb
        "#a6a6a6": "#B19B63",  # ... under the mouse
        "#606060": "#5E4F2C",  # ... held down / ruler ticks
        "#dadada": "#E8DCC6",  # scrollbar arrow under the mouse
        "#5f5f5f": "#4A3C1C",  # scrollbar arrows
        "#b0b0b0": "#B8A67A",  # picture box border
        "#a0a0a0": "#A8955F",  # timeline lines
        "#e4e4e4": "#EEE7D8",  # picture still loading
        "#d8d8d8": "#E4D9C2",  # filmstrip still loading
        "#888888": "#6F5B30",  # hint text
        "#666666": "#4A3A18",  # grey text
        "#7da2ce": "#B0402A",  # a picture box under the mouse
        "#245edc": "#7C0000",  # the dragged selection rectangle
        "#5a8be0": "#C04818",  # GIF Maker's playhead
        "#1f4fae": "#600000",  # ... its edge
        "#a9c4f5": "#FFA858",  # ... its shine
    }, {
        "box": {"#ffffff": "#FBF5E6"},  # box backgrounds: a very light khaki
        "edge": {"#ffffff": "#D8C592", "#000000": "#281602"},  # 3D edges: light / darkest
    }),
}
# what each theme's title bars and selections look like: the inactive title bar (left,
# right), the title text (active, inactive), and a selection's background and text
THEME_LOOK = {
    "xp": (("#808080", "#A8A8A8"), ("#FFFFFF", "#D4D0C8"), ("#316AC5", "#FFFFFF")),
    "98": (("#808080", "#A8A8A8"), ("#FFFFFF", "#C3C3C3"), ("#316AC5", "#FFFFFF")),
    "dark": (("#808080", "#A8A8A8"), ("#FFFFFF", "#D4D0C8"), ("#316AC5", "#FFFFFF")),
    "pink": (("#00B8A8", "#F18EA1"), ("#FFFFFF", "#00544C"), ("#A1DAD1", "#000000")),
    "jungle": (("#7D7040", "#7D7040"), ("#FFA040", "#903018"), ("#800000", "#FFA040")),
}


CUSTOM_INACTIVE = (("#808080", "#A8A8A8"), "#C3C3C3")  # the Custom theme's inactive title bar


def caption_inactive():
    """The current theme's inactive title bar colours (left, right) - in the Custom theme,
    whatever its appearance, Windows 98's grey."""
    return CUSTOM_INACTIVE[0] if CUSTOM_SELECT else THEME_LOOK[THEME][0]


def title_text(active):
    """The current theme's title text colour. The Custom theme's: white or black, whichever
    reads on its title bar colour (a white bar: black text); inactive, light grey."""
    if CUSTOM_SELECT:
        return CUSTOM_SELECT[1] if active else CUSTOM_INACTIVE[1]
    return THEME_LOOK[THEME][1][0 if active else 1]


CUSTOM_SELECT = None  # the Custom theme's selection (its title bar colour), while it's in use


def select_colors():
    """The current theme's selection: (background, text) - the Custom theme's is its title
    bar colour."""
    return CUSTOM_SELECT or THEME_LOOK[THEME][2]


def selection_for(color):
    """A selection in this colour: (the colour, white or black text - whichever reads).
    (The black is #010101: the dark theme turns plain black text light.)"""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return color.upper(), "#010101" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#FFFFFF"
_ROLES = ("text", "box", "edge", "face")
_TO = {t: {r: {k: v.lower() for k, v in {**any_, **roles.get(r, {})}.items()} for r in _ROLES}
       for t, (any_, roles) in THEME_COLORS.items()}  # all lowercase, so both ways look up alike
_FROM = {t: {r: {v: k for k, v in m.items()} for r, m in rm.items()} for t, rm in _TO.items()}
for _t, _rm in _TO.items():  # every theme colour must lead back to one written colour
    for _r, _m in _rm.items():
        assert len(set(_m.values())) == len(_m) and not set(_m.values()) & set(_m) - {"#000000", "#ffffff"}, (_t, _r)
_NAMES = {"white": "#ffffff", "black": "#000000"}


def _norm(color):
    c = _NAMES.get(str(color).lower(), str(color).lower())
    return "#" + "".join(ch * 2 for ch in c[1:]) if len(c) == 4 and c[0] == "#" else c


def theme_color(color, role="face", theme=None):
    """A written (Ivory) colour in a theme - the current one unless given (unchanged if
    the theme doesn't change it)."""
    theme = theme or THEME
    return _TO[theme][role].get(_norm(color), color) if isinstance(color, str) else color


def written_color(color, role="face", theme=None):
    """The other way: a theme's colour back to the colour the app wrote."""
    theme = theme or THEME
    return _FROM[theme][role].get(_norm(color), color) if isinstance(color, str) else color


def themed(color, role="face"):
    """A colour for the current theme - for pictures drawn with Pillow, which Tk doesn't see."""
    return theme_color(color, role)


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
            out[k] = theme_color(v, role)
    return out


_orig_options = tk.Misc._options
_orig_create = tk.Canvas._create
_orig_itemconfigure = tk.Canvas.itemconfigure


def _themed_options(self, cnf, kw=None):  # every widget made or changed
    if THEME != "xp":
        cnf, kw = (_map_opts(o, lambda k: _widget_role(self, k)) for o in (cnf, kw))
    return _orig_options(self, cnf, kw)


def _themed_create(self, item_type, args, kw):  # every shape drawn on a canvas
    if THEME != "xp":
        pick = lambda k: _item_role(item_type, k) if k in ("fill", "outline") else None  # noqa: E731
        kw = _map_opts(kw, pick)
        args = list(args)
        if args and isinstance(args[-1], dict):
            args[-1] = _map_opts(args[-1], pick)
    return _orig_create(self, item_type, args, kw)


def _themed_itemconfigure(self, tag_or_id, cnf=None, **kw):  # every shape changed
    if THEME != "xp" and (cnf or kw):
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
SYSTEM_COLORS = {  # the Tk default colours each theme changes (the others keep Windows' own)
    "dark": _SYSTEM_DARK,
    "pink": {"disabledforeground": "#DE4B65", "selectcolor": "#FFF0F4",  # greyed text; tick boxes
             "selectbackground": "#A1DAD1", "selectforeground": "#000000"},  # selected text
    "jungle": {"disabledforeground": "#D2BE90", "selectcolor": "#FBF5E6",
               "selectbackground": "#800000", "selectforeground": "#FFA040"},
}
_SYSTEM_OPTS = list(dict.fromkeys(o for colors in SYSTEM_COLORS.values() for o in colors))


def system_colors(theme):
    """The Tk default colours a theme changes, with its selection as it is now (the Custom
    theme's follows its title bar)."""
    colors = dict(SYSTEM_COLORS.get(theme, {}))
    if CUSTOM_SELECT or "selectbackground" in colors:
        colors["selectbackground"], colors["selectforeground"] = select_colors()
    return colors
_system_colors = {}  # (widget, option) -> the Tk default it had before, to put back


def retheme(widget, old, new):
    """Switch one existing widget (and a canvas's drawings) from theme old to theme new."""
    system = system_colors(new)

    def convert(val, role):
        return theme_color(written_color(val, role, old), role, new)
    for opt in _SYSTEM_OPTS:
        try:
            val = str(widget.cget(opt))
        except (tk.TclError, ValueError):
            continue
        key = (str(widget), opt)
        if val.lower().startswith("system"):  # a Tk default colour
            if opt in system:
                _system_colors[key] = val
                to = system[opt]
            else:
                continue
        elif key in _system_colors:  # a Tk default another theme changed
            to = system[opt] if opt in system else _system_colors.pop(key)
        else:
            role = _widget_role(widget, opt)
            to = convert(val, role) if role and opt in _SYSTEM_DARK else val
        if to != val:
            try:
                widget.configure({opt: to})
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
                to = convert(val, _item_role(item_type, opt)) if val else val
                if to != val:
                    widget.itemconfigure(item, {opt: to})


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


def run_installer_after_exit(setup):
    """Start the downloaded installer - silently, over this version - but only once this app
    has completely closed.
    Why wait: the installed .exe is a one-file bundle, so two processes run it - a small
    launcher (which unpacks the app to a temp folder) and the app itself. If the installer
    starts while they're still running, its "close running apps" step (Windows' Restart
    Manager) catches the launcher half-way through closing and leaves it stuck, holding the
    .exe open; the silent installer then can't replace the file and quietly gives up - the
    app closes, and reopens as the old version. So a small hidden helper waits for the app
    and its launcher to exit (ending the launcher if it's stuck - all it has left to do is
    delete its temp folder, which the helper then does), and only then runs the installer.
    The installer logs to %TEMP%\\MasterConverter-update.log."""
    args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
            "/LOG=" + os.path.join(tempfile.gettempdir(), "MasterConverter-update.log")]
    launcher = 0
    if getattr(sys, "frozen", False):  # the launcher: our parent, running the same .exe
        try:
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, os.getppid())  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    if os.path.normcase(buf.value) == os.path.normcase(sys.executable):
                        launcher = os.getppid()
                k.CloseHandle(h)
        except Exception:
            launcher = 0
    q = lambda s: "'" + str(s).replace("'", "''") + "'"  # noqa: E731 - a PowerShell string
    unpacked = getattr(sys, "_MEIPASS", "")
    script = "\r\n".join([
        "$ErrorActionPreference = 'SilentlyContinue'",
        f"Wait-Process -Id {os.getpid()} -Timeout 60",  # the app: closing right now
        f"$launcher = {launcher}",
        "if ($launcher) {",
        "    Wait-Process -Id $launcher -Timeout 15",  # normally gone within a second or two
        "    $p = Get-Process -Id $launcher",
        f"    if ($p -and $p.Path -eq {q(sys.executable)}) {{",  # stuck: end it and tidy up
        "        Stop-Process -Id $launcher -Force; Start-Sleep -Milliseconds 500",
        f"        if ({q(unpacked)}) {{ Remove-Item -LiteralPath {q(unpacked)} -Recurse -Force }}",
        "    }",
        "}",
        "Start-Sleep -Milliseconds 300",
        f"Start-Process -FilePath {q(setup)} -ArgumentList "
        + ",".join(q(a) for a in args[:-1]) + "," + q('"' + args[-1] + '"'),
    ])
    helper = os.path.join(tempfile.gettempdir(), "MasterConverter-update.ps1")
    try:
        with open(helper, "w", encoding="utf-8-sig") as f:
            f.write(script)
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-WindowStyle", "Hidden", "-File", helper],
                         creationflags=0x08000000 | 0x00000200)  # no window, own group
    except Exception:  # no PowerShell?: start the installer straight away, as before
        subprocess.Popen([setup] + args)


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
    tk.Label(body, text=heading, bg=BG, font=(FONT[0], FONT[1], "bold"), justify="left",
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
CUSTOM_ICON = os.path.join(os.path.dirname(SETTINGS_PATH), "custom_icon.png")  # Settings > App icon


def app_folder():
    """Where Master Converter is: the installed .exe's folder, or the script's."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APPEARANCE_NAMES = {"98": "Light grey", "xp": "Ivory", "dark": "Dark grey", "pink": "Pink",
                    "jungle": "Jungle"}  # Settings
CUSTOM_THEME = "custom"  # the Theme menu's last choice: your own appearance + title bar


def saved_theme(settings):
    """The Theme menu's choice: a built-in theme, or "custom". (Older settings: "dark": true
    was dark mode; a title bar colour chosen before there was a Custom theme becomes it.)"""
    theme = settings.get("theme")
    if theme == CUSTOM_THEME and settings.get("custom_theme"):
        return theme
    if theme not in THEME_NAMES:
        theme = "dark" if settings.get("dark") else DEFAULT_THEME
    if settings.get("palette") and "custom_theme" not in settings:
        return CUSTOM_THEME
    return theme


def theme_look(settings, choice=None):
    """(appearance, title bar palette name, custom colour) for a Theme menu choice."""
    choice = choice or saved_theme(settings)
    if choice != CUSTOM_THEME:
        return choice, THEME_PALETTE[choice], None
    ct = settings.get("custom_theme")
    if ct is None:  # (from older settings: the title bar colour chosen then)
        theme = settings.get("theme") if settings.get("theme") in THEME_NAMES else (
            "dark" if settings.get("dark") else DEFAULT_THEME)
        ct = {"appearance": theme, "palette": settings.get("palette"),
              "custom_color": settings.get("custom_color")}
    appearance = ct.get("appearance") if ct.get("appearance") in THEME_NAMES else DEFAULT_THEME
    palette = ct.get("palette")
    if palette not in TITLE_PALETTES and palette != CUSTOM_PALETTE:
        palette = THEME_PALETTE[appearance]
    return appearance, palette, ct.get("custom_color")


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
# MS Sans Serif: the Windows 95 / 98 dialog font (a crisp bitmap font; comes with Windows)
FONT = ("MS Sans Serif", 8)


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
    """The ffmpeg command line for one conversion. quality is 0-100 like the image slider."""
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


CAPTION_ACTIVE = ("#000080", "#1084D0")  # Windows 98: navy fading to light blue
# Settings > Color palette: the active title bar's colours (left, right)
TITLE_PALETTES = {
    "Windows 98 blue": ("#000080", "#1084D0"),
    "Teal": ("#006A6A", "#2AA8A8"),
    "Plum": ("#4B1466", "#9A58BE"),
    "Maroon": ("#700000", "#B83A3A"),
    "Forest green": ("#1F5A1F", "#5E9E4A"),
    "Rose": ("#861E4E", "#D06A92"),
    "Slate": ("#2E3C4C", "#7E8EA0"),
    "Charcoal": ("#101010", "#4A4A4A"),
    "Teal and pink": ("#00BDBA", "#DC97B8"),  # Windows 98 Pink's
    "Jungle black": ("#000000", "#000000"),  # Windows 98 Jungle's: plain black
}
CUSTOM_PALETTE = "Custom..."  # the last choice in the list: pick any colour
THEME_PALETTE = {"98": "Windows 98 blue", "xp": "Windows 98 blue", "dark": "Windows 98 blue",
                 "pink": "Teal and pink", "jungle": "Jungle black"}  # unless one's chosen


def palette_colors(name, custom_color=None):
    """A palette's title bar colours (left, right); Custom... is made from its one colour."""
    if name == CUSTOM_PALETTE and custom_color:
        return custom_palette(custom_color)
    return TITLE_PALETTES.get(name) or TITLE_PALETTES["Windows 98 blue"]


def custom_palette(color):
    """A title bar from one chosen colour: it on the left, fading to a lighter shade of it on
    the right, like the ready-made palettes."""
    rgb = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    light = [round(v + (255 - v) * 0.35) for v in rgb]
    return color.upper(), "#" + "".join(f"{v:02X}" for v in light)


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
        self.on_help = None  # set by WhatsThis: shows the ? button (dialogs) when set
        self.icon = None  # set_icon: a small picture before the title
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

    def set_icon(self, path, size=16):
        """Show the picture at path, size x size, at the left of the title bar."""
        self.icon = ImageTk.PhotoImage(Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS))
        if hasattr(self, "bar"):
            self.draw()

    def set_update_button(self, on_click):
        """Show a green download-arrow button left of _ that calls on_click (None hides it)."""
        self.on_update = on_click
        if hasattr(self, "bar"):
            self.draw()

    # ---- drawing ----
    def buttons(self):
        """[(kind, x0, y0)] from the right: X, [] and _ side by side with no gaps; X 3 px
        from the bar's right edge, all centred up and down. The update button, when shown,
        sits a little apart to the left of them, and the ? button (What's This?) left of
        everything - so the two never overlap."""
        W = self.bar.winfo_width()
        margin = 3  # from the bar's right edge
        y = (self.TITLE_H - self.BTN_H + 1) // 2  # centred; an odd spare pixel goes above
        x = W - margin - self.BTN_W
        out = [("close", x, y)]
        if self.resizable:
            x -= self.BTN_W
            out.append(("restore" if self.maximized else "max", x, y))
            x -= self.BTN_W
            out.append(("min", x, y))
        if self.on_update:  # its own spot, 8 px apart, just left of _
            x -= self.BTN_W + 8
            out.append(("update", x, y))
        if self.on_help:  # "What's This?": 2 px left of what's there (X on dialogs; _ or the
            x -= self.BTN_W + 2  # update button on the main window), like Windows 98
            out.append(("help", x, y))
        return out

    def draw(self):
        c, W, H = self.bar, self.bar.winfo_width(), self.TITLE_H
        c.delete("all")
        a, b = CAPTION_ACTIVE if self.active else caption_inactive()
        key = (W, a, b)
        if self._grad is None or self._grad[0] != key:
            ramp = Image.new("RGB", (256, 1))
            ca, cb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
            ramp.putdata([tuple(round(p + (q - p) * i / 255) for p, q in zip(ca, cb))
                          for i in range(256)])
            self._grad = (key, ImageTk.PhotoImage(ramp.resize((max(W, 1), H))))
        c.create_image(0, 0, image=self._grad[1], anchor="nw")
        x = 6
        if self.icon:
            c.create_image(x, H // 2, image=self.icon, anchor="w")
            x += self.icon.width() + 5
        c.create_text(x, H // 2, text=self.title, anchor="w", font=(FONT[0], 10, "bold"),
                      fill=title_text(self.active))
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
        elif kind == "help":  # a bold ?, x 6-13, y 3-14
            px(7, 3, 13, 5)  # top of the hook
            px(6, 4, 8, 7)  # its left end
            px(12, 4, 14, 8)  # right side
            px(10, 7, 13, 9)  # curling back in
            px(9, 8, 11, 11)  # stem
            px(9, 12, 11, 14)  # the dot
        elif kind == "update":  # arrow down onto a line, x 5-14, y 2-13, 2-px strokes like X
            px(9, 2, 11, 9)  # shaft, ending inside the head so the tip stays sharp
            for i in range(5):  # head: drawn with the X's strokes
                px(5 + i, 6 + i, 7 + i, 7 + i)
                px(13 - i, 6 + i, 15 - i, 7 + i)
            px(6, 12, 14, 14)  # the line: the same as _
        else:  # close: Windows 98's X - 2-px arms, a little wider than tall; x 5-14, y 5-13
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
                 "restore": self.toggle_maximize, "update": self.on_update,
                 "help": self.on_help}[kind]()

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
        color = CUSTOM_SELECT[0] if CUSTOM_SELECT else self.COLOR  # (Custom: its title bar's)
        for i in range(n):
            bx = x0 + i * step
            self.create_rectangle(bx, 1 + self.PAD, bx + self.BLOCK, H - 1 - self.PAD,
                                  fill=color, outline="")


_FONTS = {}


def font_of(font, widget):
    """A tkfont.Font for a font description (kept, so each is made once per Tk)."""
    key = (id(widget.tk), str(font))
    if key not in _FONTS:
        _FONTS[key] = tkfont.Font(widget, font=font)
    return _FONTS[key]


def engraved_colors():
    """(light, dark) of greyed-out text: the theme's 3D edge colours, like Windows 98. (In
    the dark theme its edges are too close to the face: there, like the other themes' look -
    readable text with a soft copy 1 px down and right - a mid grey over a near-black.)"""
    if THEME == "dark":
        return "#636363", "#1A1A1A"
    return theme_color(EDGE_LIGHT, "edge"), theme_color(EDGE_SHADOW, "edge")


def draw_engraved(canvas, x, y, text, font=FONT, underline=-1, anchor="nw", width=0,
                  justify="left"):
    """Greyed-out text the way Windows 98 draws it, engraved into the face: the text in the
    edges' shadow colour over a copy in their light colour 1 px down and right. (width:
    wrap at that many pixels; underline only with anchor "nw".)"""
    f = font_of(font, canvas)
    for d, color in zip((1, 0), engraved_colors()):
        canvas.create_text(x + d, y + d, text=text, font=font, fill=color, anchor=anchor,
                           width=width, justify=justify)
        if 0 <= underline < len(text) and anchor == "nw":
            ux = x + d + f.measure(text[:underline])
            uy = y + d + f.metrics("ascent") + 1
            canvas.create_rectangle(ux, uy, ux + f.measure(text[underline]), uy + 1,
                                    fill=color, outline="")


class EngravedLabel(tk.Canvas):
    """A greyed-out note such as "(not used by PNG)", its text engraved into the face like
    Windows 98's greyed-out text. Works like a tk.Label for its text, anchor ("center" or
    "w"), wraplength and justify, and is the same size. Given another text colour with fg
    (a red error message), it shows plain text in that colour instead."""
    GREYS = ("#666666", "#888888", "#999999")  # (the greys the notes used to be: engraved)

    def __init__(self, parent, text="", font=FONT, anchor="center", wraplength=0,
                 justify="left", fg=None):
        super().__init__(parent, bg=BG, highlightthickness=0, bd=0, width=0, height=0)
        self.opts = {"text": text, "font": font, "anchor": anchor, "wraplength": wraplength,
                     "justify": justify, "fg": fg}
        self.bind("<Configure>", lambda e: self.draw(resize=False))
        self.draw()

    def configure(self, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        for key in list(kw):
            if key in self.opts:
                self.opts[key] = kw.pop(key)
        if kw:
            super().configure(kw)
        self.draw()
    config = configure

    def cget(self, key):
        return self.opts[key] if key in self.opts else super().cget(key)

    def draw(self, resize=True):
        self.delete("all")
        o = self.opts
        text = str(o["text"])
        if resize:  # a label's size, so the notes sit where the old labels did
            if not text:
                tk.Canvas.configure(self, width=0, height=0)
                return
            ref = tk.Label(self, text=text, font=o["font"], wraplength=o["wraplength"],
                           justify=o["justify"])
            tk.Canvas.configure(self, width=ref.winfo_reqwidth(), height=ref.winfo_reqheight())
            ref.destroy()
        if not text:
            return
        f = font_of(o["font"], self)
        empty = tk.Label(self, text="x", font=o["font"])  # the space a label leaves round
        inset = (empty.winfo_reqwidth() - f.measure("x")) // 2  # its text
        top = (empty.winfo_reqheight() - f.metrics("linespace")) // 2
        empty.destroy()
        width = int(o["wraplength"] or 0)
        if o["anchor"] == "w":
            x, anchor = inset, "nw"
        else:
            x, anchor = max(self.winfo_width(), int(self.cget("width"))) // 2, "n"
        fg = o["fg"]
        if fg and _norm(fg) not in self.GREYS:  # (an error: plain, in its own colour)
            self.create_text(x, top, text=text, font=o["font"], fill=fg, anchor=anchor,
                             width=width, justify=o["justify"])
        else:
            draw_engraved(self, x, top, text, o["font"], anchor=anchor, width=width,
                          justify=o["justify"])


class RaisedEdge(tk.Frame):
    """A raised 3D edge in the theme's own edge colours - 2 px light top / left, 1 px shadow
    and 2 px dark bottom / right, then 1 px of face - the same edge as the tabs and the page
    border. (Tk's own raised edge works its shadow out from the face colour, so it doesn't
    match them in themes with coloured edges.) Put the contents in .inner. It can also be
    shown pressed in, like a pushed button."""

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=EDGE_DARK, **kw)  # outer bottom / right
        self.pressed = False
        self.top_left = tk.Frame(self, bg=EDGE_LIGHT)  # outer top / left
        self.top_left.pack(fill="both", expand=True, padx=(0, 2), pady=(0, 2))
        self.bottom_right = tk.Frame(self.top_left, bg=EDGE_SHADOW)  # inner bottom / right
        self.bottom_right.pack(fill="both", expand=True, padx=(2, 0), pady=(2, 0))
        self.inner_top_left = tk.Frame(self.bottom_right, bg=BG)  # inner top / left
        self.inner_top_left.pack(fill="both", expand=True, padx=(0, 1), pady=(0, 1))
        self.inner = tk.Frame(self.inner_top_left, bg=BG)
        self.inner.pack(fill="both", expand=True, padx=(1, 0), pady=(1, 0))

    def set_pressed(self, pressed):
        """Pressed in: shadow and dark top / left, light bottom / right (like Tk's sunken)."""
        if pressed == self.pressed:
            return
        self.pressed = pressed
        colors = ((EDGE_LIGHT, EDGE_SHADOW, BG, EDGE_DARK) if pressed else
                  (EDGE_DARK, EDGE_LIGHT, EDGE_SHADOW, BG))
        for frame, color in zip((self, self.top_left, self.bottom_right, self.inner_top_left),
                                colors):
            tk.Frame.configure(frame, bg=color)

    def redraw(self):
        """Colours again after a theme change (the pressed look's colours aren't the ones
        the frames were made with)."""
        pressed, self.pressed = self.pressed, not self.pressed
        self.set_pressed(pressed)


class ClassicButton(RaisedEdge):
    """The app's raised button (Add, Convert, OK...): a flat Tk button inside a RaisedEdge,
    so it's shaded like the tabs in every theme. It pushes in while it's held down, and
    works like a tk.Button: its options (text, state, command...) and invoke() are the
    button's; pack / grid / place it like any widget."""

    def __init__(self, parent, **kw):
        super().__init__(parent)
        # (1 px of face round it: where Tk's button had its focus ring, so the size is the same)
        self.button = tk.Button(self.inner, relief="flat", bd=0, highlightthickness=0, bg=BG,
                                activebackground="#F5F3E8", **kw)
        self.button.pack(fill="both", expand=True, padx=1, pady=1)
        self.button.classic = self
        tags = list(self.button.bindtags())  # after Tk's own button bindings: follow its
        tags.insert(tags.index("Button") + 1, "ClassicButton")  # pressed / released state
        self.button.bindtags(tuple(tags))
        for seq in ("<ButtonPress-1>", "<ButtonRelease-1>", "<Enter>", "<Leave>"):
            self.button.bind_class("ClassicButton", seq, ClassicButton._follow)
        self.engraved = None  # greyed out: its text engraved, drawn over the button
        self.engrave()

    @staticmethod
    def _follow(e):
        classic = getattr(e.widget, "classic", None)
        if classic is not None:
            classic.follow()

    def follow(self):
        """Look pushed in while Tk's button is (held down with the mouse over it)."""
        try:
            down = str(self.button.cget("relief")) == "sunken"
            self.set_pressed(down)
            tk.Frame.configure(self.inner, bg="#F5F3E8" if down else BG)  # (its pressed face)
        except tk.TclError:  # (closed by its own click)
            pass

    def redraw(self):
        super().redraw()
        self.follow()
        self.engrave()

    def engrave(self):
        """Greyed out, a text button shows its text engraved into the face, like Windows 98
        (Tk would just draw it grey): drawn on a canvas over the button while it's off."""
        try:
            off = (str(self.button.cget("state")) == "disabled"
                   and not str(self.button.cget("image")))
        except tk.TclError:
            return
        if not off:
            if self.engraved is not None:
                self.engraved.place_forget()
            return
        if self.engraved is None:
            self.engraved = tk.Canvas(self.inner, bg=BG, highlightthickness=0, bd=0)
            self.engraved.bind("<Configure>", lambda e: self.draw_engraved())
        # (the button's own area: inside the 1 px of face round it)
        self.engraved.place(x=1, y=1, relwidth=1, relheight=1, width=-2, height=-2)
        self.draw_engraved()

    def draw_engraved(self):
        c = self.engraved
        c.delete("all")
        text, font = str(self.button.cget("text")), self.button.cget("font")
        f = font_of(font, c)
        draw_engraved(c, (c.winfo_width() - f.measure(text)) // 2,
                      (c.winfo_height() - f.metrics("linespace")) // 2, text, font,
                      int(self.button.cget("underline")))

    # its own options, not the button's: the cursor (What's This? sets and puts back each
    # widget's), and the colours (a theme change recolours the button on its own)
    OWN = {"cursor", "bg", "background", "fg", "foreground", "activebackground",
           "activeforeground", "disabledforeground", "highlightbackground", "highlightcolor"}

    def configure(self, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        own = {k: kw.pop(k) for k in list(kw) if k in self.OWN}
        if own:
            tk.Frame.configure(self, own)
        if not kw:
            return None
        out = self.button.configure(kw)
        if {"state", "text", "font", "underline", "image"} & set(kw):
            self.engrave()
        return out
    config = configure

    def cget(self, key):
        return tk.Frame.cget(self, key) if key in self.OWN else self.button.cget(key)
    __getitem__ = cget

    def __setitem__(self, key, value):
        self.button.configure({key: value})

    def invoke(self):
        return self.button.invoke()

    def focus_set(self):  # the keyboard goes to the button (Enter / Space press it)
        self.button.focus_set()
    focus = focus_set


def xp_button(parent, text, cmd, bold=False):
    """The app's raised button (Add, Convert, OK...)."""
    return ClassicButton(parent, text=text, command=cmd, padx=8,
                         font=(FONT[0], FONT[1], "bold" if bold else "normal"))


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
SOUNDS_ON = True  # Settings > Sound effects (read from the settings file at start)


def play_sound(kind):
    """Play "done" or "error" without waiting for it (Windows only; silently skipped if the
    sound can't play, e.g. no speakers)."""
    if sys.platform != "win32" or kind is None or not SOUNDS_ON:
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


def brush_icon():
    """A little Windows 98-style paintbrush (11 x 16), shaded to look 3D like the icons of
    the time - lit from the top left: the yellow-green handle bright on its left and dark
    olive on its right, the metal band light on top and shadowed below, the white bristles
    grey on their shaded side; black outline."""
    rows = ["....KKK....",
            "...KHYGK...",
            "...KHYGK...",
            "...KHYGK...",
            "...KHYGK...",
            "...KHYGK...",
            "..KKHYGKK..",
            ".KHHYYYGGK.",
            ".KKKKKKKKK.",
            ".KWSSSSSDK.",
            ".KDDDDDDDK.",
            ".KKKKKKKKK.",
            ".KWKWKWKLK.",
            ".KWKWKWKLK.",
            ".KWWWWWWLK.",
            ".KKKKKKKKK."]
    colors = {"K": (0, 0, 0, 255), "H": (255, 255, 96, 255), "Y": (192, 200, 0, 255),
              "G": (104, 104, 0, 255), "W": (255, 255, 255, 255), "S": (208, 208, 208, 255),
              "D": (128, 128, 128, 255), "L": (176, 176, 176, 255)}
    im = Image.new("RGBA", (11, len(rows)), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in colors:
                im.putpixel((x, y), colors[ch])
    return ImageTk.PhotoImage(im)


class PopupMenu:
    """Right-click menu drawn by the app itself, laid out like Windows' own (raised 3D edge
    like the buttons', 21 px items, a line between groups, blue highlight). Windows' menus
    draw their frame in the system's light colour, which in dark mode showed as a thick
    white border; this one is coloured like everything else, so it's the same in both modes.
    Same calls as tk.Menu: add_command(label=, command=), add_separator(), tk_popup(x, y)."""

    def __init__(self, parent, on_close=None, indent=22, refill=None):
        self.parent = parent.winfo_toplevel()
        self.indent = indent  # space left and right of the items' text
        self.items = []
        self.top = None
        self.on_close = on_close  # called when the menu closes (the menu bar un-highlights)
        # refill: a list that stays open when an item is chosen (the Theme list) - the item
        # acts, then the list redraws itself from refill() (which item is pressed in...);
        # it closes when anything else is clicked
        self.refill = refill

    def add_command(self, label, command, checked=False, button=None):
        """checked: shown pressed in - e.g. the theme in use. button: (picture, command) for
        a small button at the item's right end (only on a checked item)."""
        self.items.append((label, command, checked, button))

    def add_separator(self):
        self.items.append(None)

    def tk_popup(self, x, y, min_width=0):
        top = self.top = tk.Toplevel(self.parent, bg=BG)
        top.keeps_owner_active = True  # its window stays "active" (blue title) while it's open
        top.withdraw()
        top.overrideredirect(True)
        top.transient(self.parent)
        # the same raised 3D edge as the buttons (RaisedEdge), light and shadow just as thick
        edge = RaisedEdge(top)
        edge.pack()
        self.body = tk.Frame(edge.inner, bg=BG)
        self.body.pack(padx=1, pady=1)
        self.min_width = min_width
        self.fill()
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

    def choose(self, command, close=False):
        """An item was clicked. A list that stays open (refill) acts and redraws itself;
        any other menu - or close=True - closes first."""
        if self.refill and not close:
            command()
            if self.top:  # (still open: redraw it, e.g. with the new theme pressed in)
                self.items = []
                for item in self.refill():
                    if item is None:
                        self.add_separator()
                    else:
                        self.add_command(*item)
                self.fill()
                self.top.focus_force()  # keeps the keyboard (Esc) and the outside-click watch
            return
        self.close()
        command()

    def close(self, give_back=True):
        """Close the menu and hand the keyboard back to its window - the menu had it (for
        Escape), and without this the window stayed greyed out as if it had lost focus."""
        if self.top:
            top, self.top = self.top, None
            top.grab_release()
            top.destroy()
            if self.on_close:
                self.on_close()
            try:
                if give_back:
                    self.parent.focus_force()
                else:  # another program took over: let the window grey out its title bar
                    self.parent.event_generate("<FocusOut>")
            except tk.TclError:
                pass

    def grab_release(self):  # (tk.Menu has it; nothing to do here)
        pass

    def fill(self):
        """Make the items' rows (again, after a choice in a list that stays open)."""
        body = self.body
        for w in body.winfo_children():
            w.destroy()
        for item in self.items:
            if item is None:  # etched line: grey over white
                tk.Frame(body, bg="#A0A0A0", height=1).pack(fill="x", padx=1, pady=(3, 0))
                tk.Frame(body, bg="#FFFFFF", height=1).pack(fill="x", padx=1, pady=(0, 3))
                continue
            label, command, checked, button = item
            if checked:
                self.latched_row(body, label, command, button)
                continue
            row = tk.Label(body, text=label, bg=BG, fg="black", font=FONT, anchor="w",
                           padx=self.indent, pady=3)
            row.pack(fill="x")
            if command is None:  # not available: greyed out, like Windows' disabled items
                row.config(fg="#808080")
                continue
            row.bind("<Enter>", lambda e, r=row: r.config(
                bg=select_colors()[0], fg=select_colors()[1]))
            row.bind("<Leave>", lambda e, r=row: r.config(bg=BG, fg="black"))
            row.bind("<ButtonRelease-1>", lambda e, c=command: self.choose(c))
        tk.Frame(body, bg=BG, width=self.min_width, height=0).pack()

    def latched_row(self, body, label, command, button=None):
        """An item shown pressed in, exactly like a pressed menu bar button (Home): the same
        sunken edge on the plain face, the same size as the other items, its text in place.
        button: (picture, command) - a small clickable picture at its right end."""
        row = tk.Label(body, text=label, bg=BG, fg="black", font=FONT, anchor="w",
                       padx=self.indent, pady=3, relief="sunken", bd=2)
        row.pack(fill="x")
        if command is not None:
            row.bind("<ButtonRelease-1>", lambda e: self.choose(command))
        if button:
            picture, action = button
            icon = tk.Label(body, image=picture, bg=BG, bd=0, padx=0, pady=0)
            icon.place(in_=row, relx=1.0, rely=0.5, x=-5, anchor="e")

            def over(e):
                return 0 <= e.x < icon.winfo_width() and 0 <= e.y < icon.winfo_height()

            def show(pressed):  # pressed: the picture sinks 1 px down and right, like a button
                icon.place_configure(x=-4 if pressed else -5, y=1 if pressed else 0)

            def up(e):  # released on it: act (going somewhere else: the list closes)
                show(False)
                if over(e):
                    self.choose(action, close=True)
            icon.bind("<ButtonPress-1>", lambda e: show(True))
            # like every button: held down, it pops up while the mouse is off it and sinks
            # again when the mouse comes back
            icon.bind("<B1-Motion>", lambda e: show(over(e)))
            icon.bind("<ButtonRelease-1>", up)


class MenuBar(tk.Frame):
    """Classic menu bar under the title bar (File  Edit  View ... style): each title has its
    first letter underlined and works with a click or Alt + that letter. The titles are
    flat buttons that press in when clicked, like every other button in the app (the way
    Windows 98's menu bars did). menus: a list of (title, "menu", items) - items is a
    function returning the drop-down list's items, [(label, command or None = greyed out)
    or None = line]; the title stays pressed in while its list is open - or
    (title, "page", command): acts when released (switches page)."""

    def __init__(self, parent, menus):
        super().__init__(parent, bg=BG)
        self.labels = {}
        bold = tkfont.Font(family=FONT[0], size=FONT[1], weight="bold")
        for title, kind, action in menus:
            # each title sits in a box as wide as its bold version, so making the current
            # page's title bold (set_current) doesn't push the others along
            cell = tk.Frame(self, bg=BG)
            cell.pack(side="left")
            btn = tk.Button(cell, text=title, bg=BG, fg="black", font=FONT, underline=0,
                            relief="flat", bd=2, padx=4, pady=0, highlightthickness=0,
                            activebackground=BG, takefocus=0)
            cell.config(width=bold.measure(title) + 16, height=btn.winfo_reqheight())
            cell.pack_propagate(False)
            btn.pack(fill="both", expand=True)
            self.labels[title] = btn
            if kind == "page":  # a real button: presses in, acts when released on it
                btn.config(command=action)
                key = lambda e=None, b=btn: self.flash(b)  # noqa: E731
            else:  # presses in and opens its list straight away
                btn.bind("<ButtonPress-1>", lambda e, b=btn, it=action: self.open(b, it))
                key = lambda e=None, b=btn, it=action: self.open(b, it)  # noqa: E731
            self.winfo_toplevel().bind(f"<Alt-{title[0].lower()}>", key)

    def set_current(self, title):
        """Show which page is open: its title in bold."""
        for name, btn in self.labels.items():
            btn.config(font=(FONT[0], FONT[1], "bold") if name == title else FONT)

    def flash(self, btn):  # Alt + letter on a page title: a short press, then act
        btn.config(relief="sunken")
        btn.after(120, lambda: (btn.config(relief="flat"), btn.invoke()))
        return "break"

    def open(self, btn, items):
        # pressed in while its list is open - the same look as a pressed Home button
        btn.config(relief="sunken")
        # the list stays open while you try its choices (e.g. themes): it closes when you
        # click anywhere else, or press Esc
        menu = PopupMenu(self, indent=8, on_close=lambda: btn.config(relief="flat"),
                         refill=items)
        entries = items()
        for item in entries:
            if item is None:
                menu.add_separator()
            else:
                menu.add_command(*item)
        # the text starts 8 px in (left-aligned), but the list keeps the usual width: the
        # longest item with 22 px on both sides (+ each item's 2 px border), like the
        # right-click menus
        font = tkfont.Font(font=FONT)
        widest = max((font.measure(e[0]) for e in entries if e), default=0)
        menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height(),
                      min_width=widest + 44 + 4)
        return "break"


SHOW_HELP = True  # Settings > Help: the ? (What's This?) buttons, on or off


class WhatsThis:
    """Windows 95 / 98's "What's This?" help for a dialog: a ? button next to X; clicking it
    turns the pointer into an arrow with a question mark, and the next click on any part of
    the dialog shows a short note about it (lookup(widget, x_root, y_root) -> text or None).
    The note closes with the next click or key."""

    def __init__(self, win, chrome, lookup):
        self.win, self.chrome, self.lookup = win, chrome, lookup
        self.tag = f"WhatsThis{id(self)}"  # put in front of every widget's own events
        self.active, self.note, self.saved = False, None, {}
        chrome.on_help = self.start if SHOW_HELP else None  # (turned off in Settings: no ?)
        win.bind_class(self.tag, "<ButtonPress-1>", self.pick)
        win.bind_class(self.tag, "<ButtonPress-3>", lambda e: self.stop() or "break")
        win.bind_class(self.tag, "<Escape>", lambda e: self.stop() or "break")  # (not the dialog)
        # a note closes with the next click anywhere in the dialog, or any key
        for event in ("<ButtonPress>", "<KeyPress>"):
            win.bind(event, lambda e: self.close_note(), add="+")
        chrome.draw()

    def start(self):
        if self.active:  # ? again: leave help mode
            self.stop()
            return
        self.close_note()
        self.active = True
        for w in all_widgets(self.win):
            try:
                self.saved[w] = (w.cget("cursor"), w.bindtags())
                w.config(cursor="question_arrow")
                w.bindtags((self.tag,) + w.bindtags())
            except tk.TclError:
                pass

    def stop(self):
        self.active = False
        for w, (cursor, tags) in self.saved.items():
            try:
                w.config(cursor=cursor)
                w.bindtags(tags)
            except tk.TclError:
                pass
        self.saved = {}

    def pick(self, e):
        if e.widget is self.chrome.bar:  # the title bar (? again, X...): out of help mode
            self.stop()
            return None  # ... and the click still reaches its button
        widget = e.widget  # (a click inside a button is the button's)
        while isinstance(widget, tk.Misc) and not isinstance(widget, ClassicButton):
            widget = widget.master
        widget = widget if isinstance(widget, ClassicButton) else e.widget
        text = self.lookup(widget, e.x_root, e.y_root)
        self.stop()
        if text:
            self.show(text, e.x_root, e.y_root)
        return "break"

    def show(self, text, x, y):
        note = self.note = tk.Toplevel(self.win)
        note.overrideredirect(True)
        note.attributes("-topmost", True)
        tk.Label(note, text=text, bg="#FFFFE1", fg="black", relief="solid", bd=1, font=FONT,
                 justify="left", wraplength=230, padx=6, pady=4).pack()
        note.update_idletasks()
        x = min(x + 4, note.winfo_screenwidth() - note.winfo_reqwidth() - 4)
        note.geometry(f"+{x}+{y + 16}")
        note.bind("<ButtonPress>", lambda e: self.close_note())

    def close_note(self):
        if self.note:
            self.note.destroy()
            self.note = None


BASIC_COLORS = [  # the 48 "Basic colors" of Windows' classic colour picker
    "#FF8080", "#FFFF80", "#80FF80", "#00FF80", "#80FFFF", "#0080FF", "#FF80C0", "#FF80FF",
    "#FF0000", "#FFFF00", "#80FF00", "#00FF40", "#00FFFF", "#0080C0", "#8080C0", "#FF00FF",
    "#804040", "#FF8040", "#00FF00", "#008080", "#004080", "#8080FF", "#800040", "#FF0080",
    "#800000", "#FF8000", "#008000", "#008040", "#0000FF", "#0000A0", "#800080", "#8000FF",
    "#400000", "#804000", "#004000", "#004040", "#000080", "#000040", "#400040", "#400080",
    "#000000", "#808000", "#808040", "#808080", "#408080", "#C0C0C0", "#400040", "#FFFFFF",
]


def color_dialog(parent, title, initial="#000085", beside=False):
    """(beside: open off to the right of the main window, partly outside it, instead of
    centred over it.)
    Colour picker laid out exactly like Windows 95 / 98's "Edit Colors" box (positions
    measured from it), in the app's own style: basic and custom colours on the left, the
    rainbow field, brightness bar, preview and Hue / Sat / Lum / Red / Green / Blue boxes on
    the right. Returns "#RRGGBB", or None if cancelled. The colours are drawn as pictures,
    so dark mode leaves them as they are; custom colours are remembered."""
    owner = parent.winfo_toplevel()
    win = tk.Toplevel(owner)
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(owner)
    result = {"color": None}
    chrome = ClassicWindow(win, title, win.destroy, resizable=False, taskbar=False)
    body = chrome.body
    box = tk.Frame(body, bg=BG, width=445, height=297)
    box.pack()
    box.pack_propagate(False)
    FX, FY, FW, FH = 228, 7, 175, 187  # rainbow field (inside its edge)
    BX, BW = 420, 10  # brightness bar
    keep = []  # Tk forgets pictures nothing holds on to
    SMALL = FONT  # (MS Sans Serif 8: the original's own font)

    def solid(color, w, h):
        img = ImageTk.PhotoImage(Image.new("RGB", (w, h), color))
        keep.append(img)
        return img

    def edge1(c, x0, y0, x1, y1):  # 1 px sunken edge round the area [x0, x1] x [y0, y1]
        c.create_line(x0 - 1, y1 + 1, x0 - 1, y0 - 1, x1 + 2, y0 - 1, fill=EDGE_SHADOW)
        c.create_line(x0 - 1, y1 + 1, x1 + 1, y1 + 1, x1 + 1, y0 - 2, fill=EDGE_LIGHT)

    def edge2(c, x0, y0, x1, y1):  # 2 px sunken edge (the swatches'): grey + black, face + white
        c.create_line(x0 - 2, y1 + 2, x0 - 2, y0 - 2, x1 + 3, y0 - 2, fill=EDGE_SHADOW)
        c.create_line(x0 - 1, y1 + 1, x0 - 1, y0 - 1, x1 + 2, y0 - 1, fill=EDGE_DARK)
        c.create_line(x0 - 1, y1 + 1, x1 + 1, y1 + 1, x1 + 1, y0 - 2, fill=BG)
        c.create_line(x0 - 2, y1 + 2, x1 + 2, y1 + 2, x1 + 2, y0 - 3, fill=EDGE_LIGHT)

    def hls(col):
        return colorsys.rgb_to_hls(*[int(col[i:i + 2], 16) / 255 for i in (1, 3, 5)])

    h, l, s_ = hls(initial)
    state = {"h": h, "l": l, "s": s_, "pick": None}  # pick: ("basic" / "custom", index)
    saved = load_settings().get("custom_colors") or []
    customs = (list(saved) + [None] * 16)[:16]
    state["slot"] = next((i for i, c in enumerate(customs) if c is None), 0)

    # one canvas under everything: swatches, rainbow, bar and preview are drawn on it
    cv = tk.Canvas(box, width=445, height=297, bg=BG, highlightthickness=0)
    cv.place(x=0, y=0)

    def label(text, x, y, under=-1, anchor="nw"):
        tk.Label(box, text=text, bg=BG, font=SMALL, underline=under, padx=0,
                 pady=0).place(x=x, y=y, anchor=anchor)

    # ---- basic and custom colours: 20 x 16 sunken boxes, 25 across and 22 down
    label("Basic colors:", 4, 8, 0)
    label("Custom colors:", 4, 174, 0)
    cells = []  # (kind, index, x, y) of each swatch's colour area (16 x 12)
    for i, col in enumerate(BASIC_COLORS):
        x, y = 10 + (i % 8) * 25, 27 + (i // 8) * 22
        cells.append(("basic", i, x, y))
    for i in range(16):
        x, y = 10 + (i % 8) * 25, 193 + (i // 8) * 22
        cells.append(("custom", i, x, y))
    swatch_items = {}

    def draw_swatch(kind, i, x, y):
        col = BASIC_COLORS[i] if kind == "basic" else (customs[i] or "#FFFFFF")
        if (kind, i) in swatch_items:
            cv.delete(swatch_items[(kind, i)])
        swatch_items[(kind, i)] = cv.create_image(x, y, image=solid(col, 16, 12), anchor="nw")
    for kind, i, x, y in cells:
        draw_swatch(kind, i, x, y)
        edge2(cv, x, y, x + 15, y + 11)
    mark = [cv.create_rectangle(0, 0, 0, 0, outline="#000000", width=1),
            cv.create_rectangle(0, 0, 0, 0, outline="#000000", width=1, dash=(1, 1))]

    # ---- the rainbow field (hue across, saturation down) and the brightness bar
    rainbow = Image.new("RGB", (FW, FH))
    rainbow.putdata([tuple(round(v * 255) for v in colorsys.hls_to_rgb(
        x / (FW - 1), 0.5, 1 - y / (FH - 1))) for y in range(FH) for x in range(FW)])
    img = ImageTk.PhotoImage(rainbow)
    keep.append(img)
    cv.create_image(FX, FY, image=img, anchor="nw")
    edge1(cv, FX, FY, FX + FW - 1, FY + FH - 1)
    edge1(cv, BX, FY, BX + BW - 1, FY + FH - 1)
    bar_item = cv.create_image(BX, FY, anchor="nw")
    arrow = cv.create_polygon(0, 0, 0, 0, 0, 0, fill="#000000")
    cross = [cv.create_line(0, 0, 0, 0, fill="#000000", width=2) for _ in range(4)]

    # ---- preview, and the numbers
    PX, PY, PW, PH = 228, 202, 58, 41
    preview_item = cv.create_image(PX, PY, anchor="nw")
    edge1(cv, PX, PY, PX + PW - 1, PY + PH - 1)
    label("Color|Solid", PX + PW // 2, 246, 0, anchor="n")
    boxes = {}
    for r, (name, text, under) in enumerate((("hue", "Hue:", 1), ("sat", "Sat:", 0),
                                              ("lum", "Lum:", 0))):
        label(text, 318, 203 + r * 24, under, anchor="ne")
        boxes[name] = tk.Entry(box, font=SMALL, relief="sunken", bd=2, bg="white", width=3)
        boxes[name].place(x=321, y=201 + r * 24, width=28, height=20)
    for r, (name, text, under) in enumerate((("red", "Red:", 0), ("green", "Green:", 0),
                                              ("blue", "Blue:", 2))):
        label(text, 398, 203 + r * 24, under, anchor="ne")
        boxes[name] = tk.Entry(box, font=SMALL, relief="sunken", bd=2, bg="white", width=3)
        boxes[name].place(x=401, y=201 + r * 24, width=28, height=20)

    def current():
        rgb = colorsys.hls_to_rgb(state["h"], state["l"], state["s"])
        return "#" + "".join(f"{round(v * 255):02X}" for v in rgb)

    def refresh():
        col = current()
        cx, cy = FX + state["h"] * (FW - 1), FY + (1 - state["s"]) * (FH - 1)
        for item, (x0, y0, x1, y1) in zip(cross, ((-8, 0, -3, 0), (3, 0, 8, 0),
                                                 (0, -8, 0, -3), (0, 3, 0, 8))):
            cv.coords(item, cx + x0, cy + y0, cx + x1, cy + y1)
        strip = Image.new("RGB", (1, FH))
        strip.putdata([tuple(round(v * 255) for v in colorsys.hls_to_rgb(
            state["h"], 1 - y / (FH - 1), state["s"])) for y in range(FH)])
        bar_img = ImageTk.PhotoImage(strip.resize((BW, FH)))
        keep.append(bar_img)
        cv.itemconfig(bar_item, image=bar_img)
        ay = FY + (1 - state["l"]) * (FH - 1)
        cv.coords(arrow, BX + BW + 3, ay, BX + BW + 9, ay - 6, BX + BW + 9, ay + 6)
        cv.itemconfig(preview_item, image=solid(col, PW, PH))
        values = {"hue": round(state["h"] * 240) % 240, "sat": round(state["s"] * 240),
                  "lum": round(state["l"] * 240)}
        values.update(zip(("red", "green", "blue"), (int(col[i:i + 2], 16) for i in (1, 3, 5))))
        for name, val in values.items():
            boxes[name].delete(0, "end")
            boxes[name].insert(0, str(val))
        # the chosen swatch: a black frame and a dotted one round it, like Windows'
        pick = state["pick"]
        spot = next(((x, y) for k, i, x, y in cells if (k, i) == pick), None)
        if spot:
            x, y = spot
            cv.coords(mark[0], x - 3, y - 3, x + 18, y + 14)
            cv.coords(mark[1], x - 5, y - 5, x + 20, y + 16)
        else:
            for m in mark:
                cv.coords(m, 0, 0, 0, 0)

    def set_rgb(col, pick=None):
        state["h"], state["l"], state["s"] = hls(col)
        state["pick"] = pick
        refresh()

    def click(e):
        for kind, i, x, y in cells:
            if x - 2 <= e.x <= x + 17 and y - 2 <= e.y <= y + 13:
                if kind == "custom":
                    state["slot"] = i
                    if customs[i] is None:  # an empty slot: just choose it for "Add"
                        state["pick"] = ("custom", i)
                        refresh()
                        return
                    set_rgb(customs[i], ("custom", i))
                else:
                    set_rgb(BASIC_COLORS[i], ("basic", i))
                return
        if FX <= e.x < FX + FW and FY <= e.y < FY + FH:
            drag_field(e)
        elif BX <= e.x < BX + BW + 12 and FY - 3 <= e.y < FY + FH + 3:
            drag_bar(e)

    def drag_field(e):
        state["h"] = min(max((e.x - FX) / (FW - 1), 0), 1)
        state["s"] = min(max(1 - (e.y - FY) / (FH - 1), 0), 1)
        if state["l"] in (0.0, 1.0):  # black / white has no colour to show: middle brightness
            state["l"] = 0.5
        state["pick"] = None
        refresh()

    def drag_bar(e):
        state["l"] = min(max(1 - (e.y - FY) / (FH - 1), 0), 1)
        state["pick"] = None
        refresh()

    def motion(e):
        if state.get("drag") == "field":
            drag_field(e)
        elif state.get("drag") == "bar":
            drag_bar(e)

    def press(e):
        in_field = FX <= e.x < FX + FW and FY <= e.y < FY + FH
        in_bar = BX <= e.x < BX + BW + 12 and FY - 3 <= e.y < FY + FH + 3
        state["drag"] = "field" if in_field else "bar" if in_bar else None
        click(e)
    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", motion)

    def typed(e=None):  # Enter / leaving a box: take what was typed, if it makes sense
        try:
            if e is not None and e.widget in (boxes["hue"], boxes["sat"], boxes["lum"]):
                state["h"] = min(max(int(boxes["hue"].get()), 0), 239) / 240
                state["s"] = min(max(int(boxes["sat"].get()), 0), 240) / 240
                state["l"] = min(max(int(boxes["lum"].get()), 0), 240) / 240
                state["pick"] = None
                refresh()
                return
            rgb = [min(max(int(boxes[n].get()), 0), 255) for n in ("red", "green", "blue")]
            set_rgb("#" + "".join(f"{v:02X}" for v in rgb))
        except ValueError:
            refresh()  # nonsense typed: show the colour as it was
    for entry in boxes.values():
        entry.bind("<Return>", typed)
        entry.bind("<FocusOut>", typed)

    def add_custom():
        i = state["slot"]
        customs[i] = current()
        save_settings(custom_colors=customs)
        x, y = next((x, y) for k, j, x, y in cells if (k, j) == ("custom", i))
        draw_swatch("custom", i, x, y)
        cv.tag_raise(mark[0]), cv.tag_raise(mark[1])
        state["pick"] = ("custom", i)
        state["slot"] = (i + 1) % 16
        refresh()

    def ok(_=None):
        result["color"] = current()
        win.destroy()

    def button(text, cmd, x, y, w, under=-1, state_="normal"):
        b = xp_button(box, text, cmd)
        b.config(underline=under, state=state_, padx=0, pady=0, font=SMALL)
        b.place(x=x, y=y, width=w, height=23)
        return b
    button("Define Custom Colors >>", None, 5, 243, 211, 0, "disabled")
    button("OK", ok, 5, 269, 66)
    button("Cancel", win.destroy, 77, 269, 66)
    button("Add to Custom Colors", add_custom, 227, 268, 215, 0)
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda e: win.destroy())

    # ---- "What's This?" (the ? button): a note for each part of the box
    notes = {
        "basic": "Click a colour to choose it.",
        "custom": "Colours you've saved. Click one to choose it. To save the chosen colour, "
                  "click an empty box, then click Add to Custom Colors.",
        "field": "Click or drag in the colours to choose one: the shade changes from left to "
                 "right, and the colour gets greyer towards the bottom.",
        "bar": "Click or drag to make the colour lighter (up) or darker (down).",
        "preview": "Shows the colour you've chosen.",
        "hue": "The colour's shade, from 0 (red) round through the rainbow to 239. "
               "Type a number and press Enter.",
        "sat": "How strong the colour is, from 0 (grey) to 240 (full colour). "
               "Type a number and press Enter.",
        "lum": "How light the colour is, from 0 (black) to 240 (white). "
               "Type a number and press Enter.",
        "red": "How much red is in the colour, from 0 to 255. Type a number and press Enter.",
        "green": "How much green is in the colour, from 0 to 255. Type a number and press Enter.",
        "blue": "How much blue is in the colour, from 0 to 255. Type a number and press Enter.",
        "Define Custom Colors >>": "Shows the custom colour controls - they're already shown here.",
        "OK": "Uses the chosen colour for the title bar and closes this box.",
        "Cancel": "Closes this box without changing anything.",
        "Add to Custom Colors": "Saves the chosen colour in the selected Custom colors box, "
                                "so you can pick it again later.",
    }
    by_label = {"Basic colors:": "basic", "Custom colors:": "custom", "Color|Solid": "preview",
                "Hue:": "hue", "Sat:": "sat", "Lum:": "lum", "Red:": "red", "Green:": "green",
                "Blue:": "blue"}

    def whats_this(widget, xr, yr):
        for name, entry in boxes.items():
            if widget is entry:
                return notes[name]
        if widget is cv:  # the drawn parts: which area was clicked?
            x, y = xr - cv.winfo_rootx(), yr - cv.winfo_rooty()
            for kind, i, sx, sy in cells:
                if sx - 3 <= x <= sx + 18 and sy - 3 <= y <= sy + 14:
                    return notes[kind]
            if FX - 1 <= x <= FX + FW and FY - 1 <= y <= FY + FH:
                return notes["field"]
            if BX - 1 <= x <= BX + BW + 12 and FY - 4 <= y <= FY + FH + 4:
                return notes["bar"]
            if PX - 1 <= x <= PX + PW and PY - 1 <= y <= PY + PH:
                return notes["preview"]
            return None
        try:
            text = widget.cget("text")
        except tk.TclError:
            return None
        return notes.get(by_label.get(text, text))
    WhatsThis(win, chrome, whats_this)
    state["pick"] = next((("basic", i) for i, c in enumerate(BASIC_COLORS)
                          if c == initial.upper()), None)
    refresh()

    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    if beside:  # its left edge two thirds across the main window, near the palette setting
        x = owner.winfo_rootx() + owner.winfo_width() * 64 // 100
        y = owner.winfo_rooty() + owner.winfo_height() * 29 // 100
        x = min(x, win.winfo_screenwidth() - w - 4)  # ... but never off the screen
        y = min(y, win.winfo_screenheight() - h - 40)
    else:  # centred over the main window
        x = owner.winfo_rootx() + (owner.winfo_width() - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    win.focus_force()
    win.grab_set()
    win.wait_window()
    return result["color"]


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


class TrackBar(tk.Canvas):
    """Windows 95 / 98's slider ("trackbar"): a thin sunken groove, a raised thumb with a
    point at the bottom, and tick marks under it - with the value shown above the thumb.
    Works like tk.Scale where the app uses it: variable=, from_=, to=, length=, and
    config(state="normal" / "disabled"). It only stops at multiples of step (0, 10, 20 ...
    100), with a tick mark at each: drag the thumb (it jumps stop to stop), click the groove,
    or use the arrow keys / Page Up / Down (one stop), Home / End.
    Everything is drawn in the app's written colours, so the themes recolour it."""

    def __init__(self, parent, variable, from_=0, to=100, length=200, step=10, **_):
        self.var, self.lo, self.hi, self.res = variable, from_, to, step
        self.enabled, self.drag = True, None
        self.L = length
        super().__init__(parent, width=length, height=40, bg=BG, highlightthickness=0,
                         takefocus=1)
        self.X0, self.X1 = 6, length - 7  # where the thumb's point can go
        self.TY = 14  # the thumb's top (the value is written above it)
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<B1-Motion>", self.motion)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        for key, n in (("<Left>", -1), ("<Right>", 1), ("<Down>", -1), ("<Up>", 1),
                       ("<Prior>", 1), ("<Next>", -1)):
            self.bind(key, lambda e, n=n: self.step(n * self.res))
        self.bind("<Home>", lambda e: self.set(self.lo))
        self.bind("<End>", lambda e: self.set(self.hi))
        variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    # tk.Scale's calls the app makes
    def configure(self, cnf=None, **kw):
        kw = {**(cnf or {}), **kw}
        state = kw.pop("state", None)
        kw.pop("fg", None)  # (the value's colour follows state)
        if state is not None:
            self.enabled = state != "disabled"
            self.draw()
        if kw:
            super().configure(**kw)
    config = configure

    def get(self):
        return self.var.get()

    def set(self, value):
        if self.enabled:  # to the nearest stop
            new = min(max(round(value / self.res) * self.res, self.lo), self.hi)
            if new != self.get():
                self.var.set(new)

    def step(self, n):
        self.set(self.get() + n)
        return "break"

    def x_of(self, value):
        return self.X0 + (value - self.lo) * (self.X1 - self.X0) / (self.hi - self.lo)

    def value_at(self, x):
        return self.lo + (x - self.X0) * (self.hi - self.lo) / (self.X1 - self.X0)

    def press(self, e):
        if not self.enabled:
            return
        self.focus_set()
        cx = self.x_of(self.get())
        if abs(e.x - cx) <= 6 and self.TY - 2 <= e.y <= self.TY + 22:  # on the thumb: drag it
            self.drag = e.x - cx
        else:  # on the groove: one stop towards the click, like Windows
            self.step(self.res if e.x > cx else -self.res)

    def motion(self, e):  # dragging: the thumb jumps from stop to stop
        if self.drag is not None and self.enabled:
            self.set(self.value_at(e.x - self.drag))

    def draw(self):
        self.delete("all")
        ty = self.TY
        cx = round(self.x_of(self.get()))
        ink = "#000000" if self.enabled else "#999999"
        # the value, above the thumb - kept fully inside at the ends (100 was cut in half)
        text = str(self.get())
        half = tkfont.Font(font=FONT).measure(text) / 2
        tx = min(max(cx, half + 1), self.L - half - 1)
        if self.enabled:
            self.create_text(tx, 1, text=text, anchor="n", font=FONT, fill=ink)
        else:  # greyed out: engraved, like Windows 98's greyed-out text
            draw_engraved(self, round(tx), 1, text, anchor="n")
        # the groove: sunken, 4 px tall, through the thumb's middle
        x0, x1, gy = self.X0 - 2, self.X1 + 2, ty + 6
        self.create_line(x0, gy + 3, x0, gy, x1, gy, fill=EDGE_SHADOW)  # grey top / left
        self.create_line(x0 + 1, gy + 2, x0 + 1, gy + 1, x1 - 1, gy + 1, fill=EDGE_DARK)
        self.create_line(x0 + 1, gy + 2, x1, gy + 2, fill=BG)
        self.create_line(x0, gy + 3, x1 + 1, gy + 3, fill=EDGE_LIGHT)  # white bottom / right
        self.create_line(x1, gy, x1, gy + 4, fill=EDGE_LIGHT)
        # a tick mark at each stop (1 px boxes, so dark mode lightens them like text)
        for v in range(self.lo, self.hi + 1, self.res):
            tx = round(self.x_of(v))
            self.create_rectangle(tx, ty + 23, tx + 1, ty + 26, fill=ink, outline="")
        # the thumb: 11 px wide, a 16 px body and a 5 px point, raised like a button
        l, r, b = cx - 5, cx + 5, ty + 15
        self.create_polygon(l, ty, r, ty, r, b, cx, b + 5, l, b, fill=BG, outline="")
        self.create_line(l, b, l, ty, r, ty, fill=EDGE_LIGHT)  # white left / top ...
        self.create_line(l, b, cx, b + 5, fill=EDGE_LIGHT)  # ... and left slope
        self.create_line(r - 1, ty + 1, r - 1, b, cx, b + 4, fill=EDGE_SHADOW)  # grey inner
        self.create_line(r, ty, r, b, cx, b + 5, fill=EDGE_DARK)  # black right and slope
        if not self.enabled:  # greyed out: a dotted face, like a disabled Windows thumb
            for yy in range(ty + 2, b, 2):
                for xx in range(l + 2 + (yy // 2) % 2, r - 1, 2):
                    self.create_rectangle(xx, yy, xx + 1, yy + 1, fill=EDGE_LIGHT, outline="")


class FlatScrollbar(tk.Canvas):
    """Vertical scrollbar drawn like Windows 98's: raised 3D arrow buttons with solid black
    triangles (pressed: flat, the triangle shifted), a raised thumb, and a checkered track
    of the face and the light edge colour (in the grey themes; the others, whose schemes
    have a scrollbar colour of their own, a plain track) - the part of the track being
    held down turns dark, like Windows'. Drawn in the theme's own colours.
    Drop-in for tk.Scrollbar: command=widget.yview, and the widget's yscrollcommand=sb.set."""
    CHECKERED = ("98", "xp")  # the themes with Windows 98's checkered track
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

    _checkers = {}  # (width, colours) -> a tall checkered picture, made once

    def checker(self, a, b):
        """A checkered picture of colours a and b, as tall as a screen (drawn from the top,
        the canvas shows as much as it needs)."""
        key = (id(self.tk), self.W, a, b, self.winfo_screenheight())
        if key not in self._checkers:
            h = self.winfo_screenheight()
            ca, cb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
            img = Image.new("RGB", (self.W, h))
            img.putdata([ca if (x + y) % 2 == 0 else cb for y in range(h) for x in range(self.W)])
            self._checkers[key] = ImageTk.PhotoImage(img, master=self)
        return self._checkers[key]

    def raised_box(self, x0, y0, x1, y1):
        """A raised Windows 98 box over [x0, x1) x [y0, y1): face, light inside the top /
        left, shadow inside and dark outside the bottom / right."""
        self.create_rectangle(x0, y0, x1, y1, fill=BG, outline="")
        self.create_line(x0 + 1, y1 - 2, x0 + 1, y0 + 1, x1 - 2, y0 + 1, fill=EDGE_LIGHT)
        self.create_line(x0 + 1, y1 - 2, x1 - 2, y1 - 2, x1 - 2, y0, fill=EDGE_SHADOW)
        self.create_line(x0, y1 - 1, x1 - 1, y1 - 1, x1 - 1, y0 - 1, fill=EDGE_DARK)

    def draw(self):
        self.delete("all")
        W, H, A = self.winfo_width(), self.winfo_height(), self.ARROW
        # the track
        face, light = theme_color(BG), theme_color(EDGE_LIGHT, "edge")
        if THEME in self.CHECKERED:
            self.create_image(0, A, image=self.checker(face, light), anchor="nw")
        else:
            self.create_rectangle(0, A, W, H - A, fill=self.TRACK, outline="")
        y0, y1 = self.thumb_box()
        y0, y1 = round(y0), round(y1)
        if self.held in ("page_up", "page_down"):  # the part held down: dark
            top, bottom = (A, y0) if self.held == "page_up" else (y1, H - A)
            if bottom > top:
                if THEME in self.CHECKERED:  # dark checks from the top of the part, then
                    # the light ones again from its bottom (on the same checks as the rest)
                    dark = self.checker(theme_color(EDGE_DARK, "edge"),
                                        theme_color(EDGE_SHADOW, "edge"))
                    self.create_image(0, top - (top - A) % 2, image=dark, anchor="nw")
                    self.create_image(0, bottom + (bottom - A) % 2,
                                      image=self.checker(face, light), anchor="nw")
                else:
                    self.create_rectangle(0, top, W, bottom, outline="",
                                          fill=theme_color(EDGE_SHADOW, "edge"))
        # the thumb, then the arrow buttons over the ends
        self.raised_box(0, y0, W, y1)
        for part, top in (("up", 0), ("down", H - A)):
            down = self.held == part
            if down:  # pressed: flat, a 1 px shadow round it
                self.create_rectangle(0, top, W, top + A, fill=BG, outline="")
                self.create_line(0, top, W - 1, top, W - 1, top + A - 1, 0, top + A - 1, 0, top,
                                 fill=EDGE_SHADOW)
            else:
                self.raised_box(0, top, W, top + A)
            # a solid black triangle, 7 px wide and 4 tall, in the middle (1 px down / right
            # while pressed, like a pushed button)
            cx, cy = W // 2 + down, top + A // 2 + down
            for row in range(4):
                y = cy - 2 + row if part == "up" else cy + 1 - row
                self.create_rectangle(cx - row, y, cx + row + 1, y + 1, fill="#000000",
                                      outline="")

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
        self.small = tkfont.Font(family=FONT[0], size=FONT[1])
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
                               outline=select_colors()[0] if sel else
                               self.BOX_HOVER if hover else self.BOX_LINE)
        self.canvas.itemconfig(label_bg, fill=select_colors()[0] if sel else "")
        self.canvas.itemconfig(label, fill=select_colors()[1] if sel else "black")

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
        # the app's font everywhere: widgets without a font of their own (the dropdown boxes
        # and their lists) use Tk's default fonts, which are Segoe UI on Windows
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            tkfont.nametofont(name).configure(family=FONT[0], size=FONT[1])
        # The window opens at its smallest size - 26 px taller than before the menu bar (and
        # its line) was added, so everything below it keeps its size. It can be made bigger,
        # not smaller: 540 px is the width the widest page (the Images, Videos and Audio tabs,
        # 533 px) needs, so nothing is ever cut off.
        global CAPTION_ACTIVE
        settings = load_settings()
        self.palette_name, self.custom_color = theme_look(settings)[1:]
        CAPTION_ACTIVE = palette_colors(self.palette_name, self.custom_color)
        self.minsize(540, 718)
        self.center_on_screen(560, 718)
        self.configure(bg=BG)
        # classic navy title bar + 3D border; everything else goes inside chrome.body
        self.chrome = ClassicWindow(self, "Master Converter", self.on_close, min_size=(540, 718))
        self.apply_icon()  # the logo before the title (or the custom one from Settings)
        content = self.chrome.body
        self.files, self.outdir = [], ""
        self.names = []  # custom name for the converted file (None = keep original)
        self.small = tkfont.Font(family=FONT[0], size=FONT[1])

        self.style_ttk()  # lists and dropdowns: classic shapes, same in light and dark

        global SOUNDS_ON, SHOW_HELP
        SOUNDS_ON = load_settings().get("sounds", True)
        SHOW_HELP = load_settings().get("whats_this", True)
        self.brush_icon = brush_icon()  # (the Theme list's Custom item: go to Settings)
        # menu bar under the title bar: Home / Settings / About switch the page below it,
        # Theme opens a list
        self.menubar = MenuBar(content, [
            ("Home", "page", lambda: self.show_panel("home")),
            ("Settings", "page", lambda: self.show_panel("settings")),
            ("Theme", "menu", lambda: [
                (THEME_NAMES[t], lambda t=t: self.pick_theme(t), t == self.theme_choice)
                for t in THEME_NAMES] + [
                None, ("Custom", lambda: self.pick_theme(CUSTOM_THEME),
                       self.theme_choice == CUSTOM_THEME,
                       # in use: a clickable brush that opens Settings, where Custom is edited
                       (self.brush_icon, lambda: self.show_panel("settings")))]),
            ("About", "page", lambda: self.show_panel("about")),
        ])
        self.menubar.pack(fill="x", padx=2, pady=(1, 0))
        # etched line under it, like classic Windows' menu bars - drawn the same way as the
        # line round the Files / Options boxes (a 2 px groove), so it looks the same in both modes
        # (its own face colour: the same as BG in light mode, 2 steps darker in dark mode)
        tk.Frame(content, bg="#EBE8D7", height=2, bd=2, relief="groove").pack(fill="x", padx=2, pady=(1, 0))
        # Home: the converter itself (tabs and their pages); Settings and About take its place
        home = tk.Frame(content, bg=BG)
        self.panels = {"home": home, "settings": self.build_settings(content),
                       "about": self.build_about(content)}
        self.panel = None
        # What's This? (the ? button, on the Home and Settings pages - see show_panel)
        self.help_mode = WhatsThis(self, self.chrome, self.whats_this_note)
        self.show_panel("home")
        # tabs on top, sitting on the raised border around the open page
        self.tabs = ClassicTabs(home, self.show_page)
        self.tabs.pack(fill="x", padx=6, pady=(6, 0))
        area = framed_page(home)
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
        self.qscale = TrackBar(opt, self.quality, from_=0, to=100, length=200)
        self.qscale.grid(row=1, column=1, sticky="w", padx=6)
        self.qnote = EngravedLabel(opt)
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
        self.theme_choice = saved_theme(settings)  # the theme chosen last time (Windows 98 at first)
        self.apply_look(*theme_look(settings, self.theme_choice))
        if settings.get("start_maximized"):
            self.after_idle(self.chrome.toggle_maximize)
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
            return  # skipped: not asked again (Settings > Updates can still install it)
        choice = dialog("Update available",
                        f"Master Converter {version} is available.\nYou have version {APP_VERSION}.",
                        ("Update now", "Not now", "Skip this version"), sound="done")
        if choice == "Update now":
            self.install_update(version, notes, url)
        elif choice == "Skip this version":
            save_settings(skipped_version=version)  # no button, and not asked again
        else:  # Not now (or the box closed): the update button in the title bar, for later -
            self.show_update_button(version, notes, url)  # and asked again next time

    def show_update_button(self, version, notes, url):
        """The update button in the title bar (after "Not now"): install the update from it
        whenever you like. Its box's Not now keeps the button there."""
        def clicked():
            choice = dialog("Update available",
                            f"Master Converter {version} is available.\nYou have version {APP_VERSION}.",
                            ("Update now", "Not now"))
            if choice == "Update now":
                self.install_update(version, notes, url)
        try:
            self.chrome.set_update_button(clicked)
        except tk.TclError:
            pass  # the app was closed while the update check was finishing

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
                            events.put(("frac", (got, total)))
                os.replace(dst + ".part", dst)
                events.put(("done", None))
            except Exception as e:
                events.put(("error", str(e)))

        def poll():
            try:
                while True:
                    kind, value = events.get_nowait()
                    if kind == "frac":
                        got, total = value
                        bar.config(value=got / total * 100)
                        label.config(text=f"Downloading Master Converter {version}...   "
                                          f"{got / 1048576:.0f} of {total / 1048576:.0f} MB")
                    elif kind == "done":
                        label.config(text="Installing the update...")
                        bar.config(value=100)
                        win.update()
                        # remembered for the new version to show once it's running
                        save_settings(just_updated={"version": version, "notes": notes})
                        run_installer_after_exit(dst)
                        self.destroy()
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

    # ---- themes ----
    def pick_theme(self, choice):
        """Theme menu: a built-in theme (its own fixed look), or Custom - your own appearance
        and title bar, as you last left them in Settings."""
        settings = load_settings()
        if choice == CUSTOM_THEME and not settings.get("custom_theme"):
            # never set up: Custom starts as what's on screen now
            save_settings(custom_theme={"appearance": THEME, "palette": self.palette_name,
                                        "custom_color": self.custom_color})
            settings = load_settings()
        self.theme_choice = choice
        save_settings(theme=choice)
        self.apply_look(*theme_look(settings, choice))

    def apply_look(self, appearance, palette, custom_color):
        """Show an appearance (see THEME_COLORS) with a title bar palette. In the Custom
        theme, everything highlighted (selected files, menu items under the mouse, dropdown
        lists, selected text) is in the title bar's colour."""
        global CAPTION_ACTIVE, CUSTOM_SELECT
        self.palette_name, self.custom_color = palette, custom_color
        CAPTION_ACTIVE = palette_colors(palette, custom_color)
        self.chrome._grad = None  # its gradient picture is made again in the new colours
        select = (selection_for(CAPTION_ACTIVE[0]) if self.theme_choice == CUSTOM_THEME
                  else None)
        changed, CUSTOM_SELECT = select != CUSTOM_SELECT, select
        self.set_theme(appearance, force=changed)  # (same appearance: the highlights still change)
        self.chrome.draw()
        if hasattr(self, "palette_var"):  # Settings shows what's in use
            self.palette_var.set(palette)
            self.appearance_var.set(APPEARANCE_NAMES[appearance])
            self.draw_palette_strip()

    def save_custom(self, appearance, palette, custom_color):
        """A change made in Settings: it becomes (and is saved as) the Custom theme."""
        self.theme_choice = CUSTOM_THEME
        save_settings(theme=CUSTOM_THEME, custom_theme={
            "appearance": appearance, "palette": palette, "custom_color": custom_color})
        self.apply_look(appearance, palette, custom_color)

    def set_appearance(self, name):  # Settings > Appearance
        appearance = next(k for k, v in APPEARANCE_NAMES.items() if v == name)
        self.save_custom(appearance, self.palette_name, self.custom_color)

    def set_theme(self, theme, force=False):
        """Switch the whole app to another appearance (see THEME_COLORS). force: colour
        everything again even if it's the same one (the Custom theme's highlight changed)."""
        global THEME, DARK_MODE
        if theme == THEME and not force:
            return
        old, THEME, DARK_MODE = THEME, theme, theme == "dark"
        dark = DARK_MODE  # from here on, every colour given to Tk goes through theme_color()
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
                    ("*TCombobox*Listbox.selectBackground", select_colors()[0]),
                    ("*TCombobox*Listbox.selectForeground", select_colors()[1])):
                self.option_add(pattern, value)
        names = {"disabledforeground": "*disabledForeground", "selectcolor": "*selectColor",
                 "selectbackground": "*selectBackground", "selectforeground": "*selectForeground"}
        for opt, value in system_colors(theme).items():
            if opt in names and not (dark and opt not in ("selectbackground", "selectforeground")):
                self.option_add(names[opt], value)
        widgets = all_widgets(self)
        for w in widgets:  # everything that already exists
            retheme(w, old, theme)
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
            elif isinstance(w, RaisedEdge):
                w.redraw()
            elif isinstance(w, (EngravedLabel, TrackBar)):
                w.draw()
        self.chrome.draw()
        self.color_dropdown_lists()
        self.update_quality_state()  # the ICO size list's height differs between the modes

    def color_dropdown_lists(self):
        """A dropdown makes its list the first time it opens and keeps its colours: colour
        every dropdown's list (making it now if needed) for the current mode."""
        for w in all_widgets(self):
            if isinstance(w, ttk.Combobox):
                popdown = self.tk.call("ttk::combobox::PopdownWindow", w)
                self.tk.call(f"{popdown}.f.l", "configure",
                             "-background", theme_color("#FFFFFF", "box"),
                             "-foreground", theme_color("#000000", "text"),
                             "-selectbackground", select_colors()[0],
                             "-selectforeground", select_colors()[1])

    def style_ttk(self):
        """The details lists and dropdowns are ttk widgets, coloured through styles. Both modes
        use ttk's "alt" look - classic Windows shapes (raised arrow buttons, classic scrollbar)
        drawn by Tk, so unlike Windows' own native look they can be recoloured: the shapes
        stay exactly the same in light and dark, only the colours change."""
        style = ttk.Style(self)
        style.theme_use("alt")
        # (styles don't go through the colour translator: translated here)
        face, box, text, trough, hot, grey = (
            theme_color(BG), theme_color("#FFFFFF", "box"), theme_color("#000000", "text"),
            theme_color("#F7F6F0"), theme_color("#F5F3E8"),
            "#7a7a7a" if DARK_MODE else theme_color("#999999"))
        sel, sel_text = select_colors()
        style.configure(".", background=face, foreground=text, fieldbackground=box,
                        troughcolor=trough, selectbackground=sel,
                        selectforeground=sel_text, arrowcolor=text, font=FONT)
        style.map(".", background=[("active", hot)])
        style.configure("Treeview", background=box, fieldbackground=box, foreground=text,
                        font=FONT)
        style.configure("Treeview.Heading", background=face, foreground=text, font=FONT,
                        relief="raised")
        style.map("Treeview", background=[("selected", sel)],
                  foreground=[("selected", sel_text)])
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

    # ---- Home / Settings / About ----
    def show_panel(self, name):
        if name == self.panel:
            return
        for panel in self.panels.values():
            panel.pack_forget()
        self.panels[name].pack(fill="both", expand=True)
        self.panel = name
        self.menubar.set_current(name.capitalize())
        # the ? (What's This?) button is in the title bar on the Home and Settings pages
        if hasattr(self, "help_mode"):
            self.help_mode.stop()
            self.help_mode.close_note()
            self.chrome.on_help = (self.help_mode.start
                                   if SHOW_HELP and name in ("home", "settings") else None)
            self.chrome.draw()

    MENU_NOTES = {
        "Home": "The converter: the Images, Videos, Audio and GIF Maker tabs.",
        "Settings": "Where the app is installed, sound effects, the app's icon, its theme, and "
                    "how the window opens.",
        "Theme": "Changes the app's look: Windows 98, Ivory, Dark, or your own Custom theme.",
        "About": "About Master Converter.",
    }
    TAB_NOTES = {
        "images": "Convert pictures from one format to another (PNG, JPEG, WEBP, HEIC, ICO, "
                  "PDF and more).",
        "videos": "Convert videos to another format or size, turn them into GIFs, or keep "
                  "just their sound.",
        "voice": "Convert sound files from one format to another, or take the sound out of "
                 "videos.",
        "gif": "Make a GIF from part of a video: choose the part on the timeline and save it.",
    }
    NOUNS = {"images": "picture", "videos": "video", "voice": "sound file"}

    def whats_this_note(self, widget, x_root, y_root):
        """What's This? note for whatever was clicked: the menu bar, then the page's own."""
        for name, btn in self.menubar.labels.items():
            if widget is btn:
                return self.MENU_NOTES[name]
        if self.panel == "settings":
            return self.settings_note(widget, x_root, y_root)
        if self.panel == "home":
            return self.home_note(widget, x_root)
        return None

    def home_note(self, widget, x_root):
        """What's This? on the Home page: the tabs, and every part of each tab."""
        if widget is self.tabs:  # which tab was clicked?
            x = x_root - self.tabs.winfo_rootx()
            key = next((k for k, x0, x1 in self.tabs.boxes if x0 <= x < x1), None)
            return self.TAB_NOTES.get(key)
        page, w = None, widget  # which tab's page it's on
        while w is not None:
            page = next((k for k, p in self.pages.items() if p is w), None)
            if page:
                break
            w = w.master
        if not page:
            return None
        noun = self.NOUNS.get(page, "file")
        panel = {"images": self, "videos": self.video, "voice": self.voice,
                 "gif": self.gif}[page]
        # the file list (either view, and its scrollbar)
        w = widget
        while w is not None and w is not panel:
            if isinstance(w, FlatScrollbar):
                return "Scrolls the list."
            if isinstance(w, (ThumbGrid, DetailsList)):
                return (f"The {noun}s to convert. Click one to select it; Ctrl or Shift + click, "
                        f"or drag a box, to select several. Right-click one to rename, "
                        f"duplicate, find or remove it. Point at one to see its details.")
            w = w.master
        # parts known by what they are
        common = {
            "progress": "Shows how far the conversion has got.",
            "status": "What's happening: how many files there are, how far it has got, or "
                      "what went wrong.",
            "show_btn": "Opens the folder with the new files highlighted, once they're made.",
            "outlabel": "Where the converted files go. At first each one is saved next to its "
                        "original.",
        }
        if page == "gif":
            common.update({
                "status": "What's happening: the video's details, how far making the GIF has "
                          "got, or what went wrong.",
                "progress": "Shows how far making the GIF has got.",
                "show_btn": "Opens the folder with the new GIF highlighted.",
                "outlabel": "Where the GIF goes. At first it's saved next to the video.",
                "screen": "The preview: shows the picture at the playhead, and plays the chosen "
                          "part when you press Play.",
                "play_btn": "Plays the chosen part in the preview. Click again to stop.",
                "strip": "The timeline. Drag the handles at its ends to choose the part that "
                         "becomes the GIF, drag inside the chosen part to move it, and click to "
                         "move the playhead.",
                "range_label": "Where the chosen part starts and ends, and how long it is.",
                "size_label": "About how big the GIF will be with the options below.",
                "pos_label": "Where the playhead is.",
                "file_label": "The video that's open.",
                "open_btn": "Choose a video (or a GIF) to make a GIF from.",
                "clear_btn": "Closes the video.",
                "make_btn": "Saves the chosen part of the video as a GIF.",
            })
        else:
            common["qscale"] = {
                "images": "How much each picture is compressed: higher looks better but makes "
                          "a bigger file. It's only used by formats that compress (JPEG, WEBP, "
                          "AVIF, HEIC...) and is greyed out for the others.",
                "videos": "How much the video is compressed: higher looks better but makes a "
                          "bigger file.",
                "voice": "How much the sound is compressed, in kbps: higher sounds better but "
                         "makes a bigger file. Original keeps each file's own quality.",
            }[page]
            common["qnote"] = ("A note about the quality setting beside it - for example, "
                               "when the chosen format doesn't use it.")
            common["snote"] = "Says why the settings beside it aren't used for this format."
            common["ico_cb"] = ("Which sizes the icon file holds. Windows picks the best one "
                                "for each place it shows an icon, so all sizes is usually best.")
            common["mute_cb"] = "Leaves the sound out of the converted videos."
        for attr, note in common.items():
            if getattr(panel, attr, None) is widget:
                return note
        if widget is self.pages[page]:  # the page's empty background: what the tab is for
            return self.TAB_NOTES[page]
        if getattr(panel, "status", None) in widget.winfo_children():  # the bar at the bottom
            return common["status"]
        # dropdowns, by the setting they change (their labels say the same)
        combo = {
                ("images", "fmt"): "The format the pictures are converted to.",
                ("videos", "fmt"): "The format the videos are converted to. GIF makes moving "
                                   "pictures; MP3 keeps only the sound.",
                ("videos", "size"): "How tall the video is: Original keeps its size; a smaller "
                                    "one makes a smaller file.",
                ("videos", "fps"): "Pictures per second: Original keeps them; fewer makes a "
                                   "smaller file, but less smooth.",
                ("voice", "fmt"): "The format the sound is converted to. For a video, only its "
                                  "sound is kept.",
                ("voice", "bitrate"): "How much the sound is compressed, in kbps: higher sounds "
                                      "better but makes a bigger file. Original keeps each "
                                      "file's own quality.",
                ("voice", "rate"): "How finely the sound is recorded, in Hz: Original keeps it; "
                                   "44100 is CD quality; lower makes a smaller file.",
                ("voice", "channels"): "Stereo (two channels) or Mono (one). Original keeps "
                                       "each file's own.",
                ("gif", "width"): "How wide the GIF is, in pixels: smaller makes a smaller file.",
                ("gif", "fps"): "Pictures per second in the GIF: more is smoother but makes a "
                                "bigger file.",
        }
        if isinstance(widget, ttk.Combobox):
            var = str(widget.cget("textvariable"))
            which = next((k for k, v in vars(panel).items()
                          if isinstance(v, tk.Variable) and str(v) == var), None)
            return combo.get((page, which))
        # buttons, boxes and labels, by their text
        try:
            text = str(widget.cget("text")).strip().rstrip(":")
        except tk.TclError:
            text = ""
        by_text = {
            "Add...": f"Choose {noun}s to add to the list. You can also drag and drop files, or "
                      f"whole folders, onto the window.",
            "Remove": f"Takes the selected {noun}s off the list (the files themselves aren't "
                      f"touched). The Delete key does the same.",
            "Clear": f"Empties the list (the files themselves aren't touched).",
            "Details": "Shows the list as details (name, type, size...). Ctrl + mouse wheel "
                       "over the list switches views too.",
            "Thumbnails": "Shows the list as thumbnails. Ctrl + mouse wheel over the list "
                          "switches views too.",
            "Convert to": combo.get((page, "fmt")),
            "Quality": common.get("qscale"),
            "Icon size": common.get("ico_cb"),
            "Size": combo.get((page, "size")),
            "Frame rate": combo.get((page, "fps")),
            "Sample rate": combo.get((page, "rate")),
            "Channels": combo.get((page, "channels")),
            "Width": combo.get((page, "width")),
            "Save to": common["outlabel"],
            "Browse...": "Choose the folder to save in.",
            "Keep photo info (date, camera, GPS location)":
                "Keeps the details stored in each photo (date taken, camera, GPS location) in "
                "the converted file, for formats that can hold them.",
            "Convert": f"Converts every {noun} in the list. The originals are never changed, "
                       f"and a new file never replaces one that's already there.",
            "Plays": "Whether the GIF plays over and over, or once and then stops.",
            "Loop forever": "The GIF plays over and over.",
            "Play once": "The GIF plays once and then stops on its last picture.",
        }
        if by_text.get(text):
            return by_text[text]
        while widget is not None:  # anything else in a box: the box's note
            if isinstance(widget, tk.LabelFrame):
                return {
                    "Files": f"The {noun}s to convert, and the buttons to add and remove them.",
                    "Options": "How the files are converted.",
                    "Preview": "The video you're making a GIF from, and the timeline to choose "
                               "the part of it.",
                    "GIF options": "How the GIF is made, and where it's saved.",
                }.get(str(widget.cget("text")).strip())
            widget = widget.master
        return None

    SETTINGS_NOTES = {  # What's This? on the Settings page: by a control's text, or its box's
        "Settings": "Settings for Master Converter. Click ? and then any setting to see what "
                    "it does.",
        "App location": "The folder Master Converter is installed in. To move it, uninstall "
                        "it and install it again into another folder.",
        "Open folder": "Opens the folder Master Converter is installed in.",
        "Sound effects": "Plays a short chime when a job is done and a low tone when something "
                         "fails. Untick it for silence.",
        "App icon": "The picture shown at the left of the title bar and on the taskbar.",
        "Choose file...": "Use your own picture (.ico, .png, .jpg ...) as the app's icon.",
        "Reset to default": "Goes back to Master Converter's own icon.",
        "No icon in the title bar": "Hides the icon at the left of the title bar. The taskbar "
                                    "still shows it.",
        "Theme": "The app's colours. A change here is saved as the Custom theme, which you "
                 "can pick again any time from the Theme menu.",
        "Appearance": "The colours of the whole app: Light grey, Ivory or Dark grey. A change "
                      "is saved as the Custom theme.",
        "Color palette": "The colours of the title bar. Choose Custom... to pick any colour. A "
                         "change is saved as the Custom theme.",
        "strip": "Shows the title bar's colours.",
        "Window": "Whether Master Converter opens at its normal size or maximized (filling "
                  "the screen).",
        "Help": "Shows or hides the ? button in the title bar - the one you just used. It "
                "explains whatever you click next.",
        "Show the ? button in the title bar":
            "Shows or hides the ? button in the title bar - the one you just used. Untick it "
            "and it disappears from every window (turn it back on here).",
        "Updates": "Which version of Master Converter you have, and a button to look for a "
                   "newer one.",
        "Check for updates": "Looks for a newer version now. If there is one, you can install "
                             "it straight away - the app restarts by itself.",
        "update_status": "What the last check for updates found.",
    }

    def settings_note(self, widget, x_root, y_root):
        """What's This? note for the part of the Settings page that was clicked."""
        notes = self.SETTINGS_NOTES
        if widget is self.palette_strip:
            return notes["strip"]
        if widget is self.icon_preview:
            return notes["App icon"]
        if widget is self.version_label:
            return notes["Updates"]
        if widget is self.update_status:
            return notes["update_status"]
        if isinstance(widget, ttk.Combobox):
            var = str(widget.cget("textvariable"))
            return notes["Appearance" if var == str(self.appearance_var) else "Color palette"]
        try:
            text = str(widget.cget("text")).strip().rstrip(":")
        except tk.TclError:
            text = ""
        if text in notes:
            return notes[text]
        while widget is not None:  # anything else in a box: the box's note
            if isinstance(widget, tk.LabelFrame):
                return notes.get(str(widget.cget("text")).strip())
            widget = widget.master
        return None

    def build_settings(self, parent):
        page = tk.Frame(parent, bg=BG, padx=12, pady=10)
        tk.Label(page, text="Settings", bg=BG, font=(FONT[0], FONT[1], "bold"),
                 anchor="w").pack(fill="x", pady=(0, 4))

        # where the app is installed
        box = tk.LabelFrame(page, text=" App location ", bg=BG, font=FONT, padx=8, pady=8)
        box.pack(fill="x")
        tk.Label(box, text="Master Converter is installed in:", bg=BG, font=FONT,
                 anchor="w").pack(fill="x")
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x", pady=(4, 0))
        folder = app_folder()
        xp_button(row, "Open folder", lambda: os.startfile(folder)).pack(side="right", padx=(6, 0))
        # shown like Windows 98's greyed-out text box - it can't be changed: sunken (shadow
        # and dark top / left, light and face bottom / right) on the face, its text engraved
        edge = tk.Frame(row, bg=EDGE_LIGHT)  # outer bottom / right
        edge.pack(side="left", fill="x", expand=True)
        inner = edge
        for bg, pad in ((EDGE_SHADOW, (0, 1)), (BG, (1, 0)), (EDGE_DARK, (0, 1)), (BG, (1, 0))):
            f = tk.Frame(inner, bg=bg)  # outer top / left, inner bottom / right, inner top /
            f.pack(fill="both", expand=True, padx=pad, pady=pad)  # left, then the inside
            inner = f
        EngravedLabel(inner, text=folder, anchor="w").pack(fill="x", pady=1)
        EngravedLabel(box, text="To move it, uninstall it and install it again into another "
                      "folder.", anchor="w").pack(fill="x", pady=(4, 0))

        # sound effects on / off
        box = tk.LabelFrame(page, text=" Sound effects ", bg=BG, font=FONT, padx=8, pady=6)
        box.pack(fill="x", pady=(6, 0))
        self.sounds_var = tk.BooleanVar(value=SOUNDS_ON)
        tk.Checkbutton(box, text="Play a sound when a job is done or something fails",
                       variable=self.sounds_var, command=self.toggle_sounds, bg=BG,
                       activebackground=BG, font=FONT).pack(anchor="w")

        # the app's icon
        box = tk.LabelFrame(page, text=" App icon ", bg=BG, font=FONT, padx=8, pady=8)
        box.pack(fill="x", pady=(6, 0))
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x")
        self.icon_preview = tk.Label(row, bg="white", relief="sunken", bd=2)
        self.icon_preview.pack(side="left")
        col = tk.Frame(row, bg=BG)
        col.pack(side="left", padx=(10, 0))
        tk.Label(col, text="Shown in the title bar and on the taskbar.", bg=BG, font=FONT,
                 anchor="w").pack(anchor="w")
        btns = tk.Frame(col, bg=BG)
        btns.pack(anchor="w", pady=(6, 0))
        xp_button(btns, "Choose file...", self.choose_icon).pack(side="left")
        xp_button(btns, "Reset to default", self.reset_icon).pack(side="left", padx=(6, 0))
        self.no_icon_var = tk.BooleanVar(value=load_settings().get("no_icon", False))
        tk.Checkbutton(box, text="No icon in the title bar", variable=self.no_icon_var,
                       command=self.toggle_no_icon, bg=BG, activebackground=BG,
                       font=FONT).pack(anchor="w", pady=(6, 0))
        self.apply_icon()  # fills the preview

        # the theme: the app's colours (light grey, ivory, dark grey ...) and the title bar's,
        # side by side, with a strip showing the title bar (like Windows' Display settings);
        # a change here is saved as the Custom theme
        box = tk.LabelFrame(page, text=" Theme ", bg=BG, font=FONT, padx=8, pady=8)
        box.pack(fill="x", pady=(6, 0))
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x")
        tk.Label(row, text="Appearance:", bg=BG, font=FONT).pack(side="left")
        self.appearance_var = tk.StringVar(value=APPEARANCE_NAMES[THEME])
        cb = ttk.Combobox(row, textvariable=self.appearance_var,
                          values=list(APPEARANCE_NAMES.values()), state="readonly", width=14)
        cb.pack(side="left", padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.set_appearance(self.appearance_var.get()))
        tk.Label(row, text="Color palette:", bg=BG, font=FONT).pack(side="left", padx=(16, 0))
        self.palette_var = tk.StringVar(value=self.palette_name)
        cb = ttk.Combobox(row, textvariable=self.palette_var,
                          values=list(TITLE_PALETTES) + [CUSTOM_PALETTE], state="readonly",
                          width=18)
        cb.pack(side="left", padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self.set_palette(self.palette_var.get()))
        self.palette_strip = tk.Canvas(box, height=14, width=1, highlightthickness=0, bd=2,
                                       relief="sunken")
        self.palette_strip.pack(fill="x", pady=(8, 0))
        self.palette_strip.bind("<Configure>", lambda e: self.draw_palette_strip())
        EngravedLabel(box, text="Changes here are saved as the Custom theme.",
                      anchor="w").pack(fill="x", pady=(6, 0))

        # Window and Help side by side, as two equal boxes
        pair = tk.Frame(page, bg=BG)
        pair.pack(fill="x", pady=(6, 0))
        pair.columnconfigure((0, 1), weight=1, uniform="pair")
        pair.rowconfigure(0, weight=1)

        # how the window opens
        box = tk.LabelFrame(pair, text=" Window ", bg=BG, font=FONT, padx=8, pady=6)
        box.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        tk.Label(box, text="When the app starts:", bg=BG, font=FONT).pack(anchor="w")
        choices = tk.Frame(box, bg=BG)
        choices.pack(anchor="w", pady=(2, 0))
        self.start_var = tk.StringVar(
            value="max" if load_settings().get("start_maximized") else "normal")
        for text, val in (("Normal size", "normal"), ("Maximized", "max")):
            tk.Radiobutton(choices, text=text, variable=self.start_var, value=val, bg=BG,
                           activebackground=BG, font=FONT,
                           command=lambda: save_settings(
                               start_maximized=self.start_var.get() == "max")
                           ).pack(side="left", padx=(0, 10))

        # the ? (What's This?) buttons on or off
        box = tk.LabelFrame(pair, text=" Help ", bg=BG, font=FONT, padx=8, pady=6)
        box.grid(row=0, column=1, sticky="nsew", padx=(3, 0))
        self.help_var = tk.BooleanVar(value=SHOW_HELP)
        tk.Checkbutton(box, text="Show the ? button in the title bar",
                       variable=self.help_var, command=self.toggle_whats_this, bg=BG,
                       activebackground=BG, font=FONT).pack(anchor="w")
        EngravedLabel(box, text="(What's This? - it explains what you click)").pack(
            anchor="w", padx=(22, 0))

        # the version, and checking for a newer one
        box = tk.LabelFrame(page, text=" Updates ", bg=BG, font=FONT, padx=8, pady=8)
        box.pack(fill="x", pady=(6, 0))
        row = tk.Frame(box, bg=BG)
        row.pack(fill="x")
        running_source = parse_version(APP_VERSION) is None
        self.version_label = tk.Label(
            row, bg=BG, font=FONT, anchor="w",
            text="Version: " + ("dev (running from the source code)" if running_source
                                else APP_VERSION))
        self.version_label.pack(side="left")
        self.update_btn = xp_button(row, "Check for updates", self.check_updates_now)
        self.update_btn.pack(side="right")
        self.update_status = EngravedLabel(box, anchor="w")  # (errors: plain red)
        self.update_status.pack(fill="x", pady=(6, 0))
        # wraps only if the box is too narrow for its (one-line) messages
        self.update_status.bind("<Configure>", lambda e: self.update_status.config(
            wraplength=max(e.width - 4, 100)))
        return page

    def toggle_whats_this(self):  # Settings > Help
        global SHOW_HELP
        SHOW_HELP = self.help_var.get()
        save_settings(whats_this=SHOW_HELP)
        self.help_mode.stop()
        self.help_mode.close_note()
        self.chrome.on_help = (self.help_mode.start
                               if SHOW_HELP and self.panel in ("home", "settings") else None)
        self.chrome.draw()

    def check_updates_now(self):
        """Settings > Updates > Check for updates: ask GitHub now (off the main thread) and
        say what was found; a newer version is offered straight away."""
        self.update_btn.config(state="disabled")
        self.update_status.config(text="Checking for updates...", fg="#666666")
        found = queue.Queue()
        threading.Thread(target=lambda: found.put(fetch_latest_release()), daemon=True).start()

        def wait():
            try:
                latest = found.get_nowait()
            except queue.Empty:
                self.after(200, wait)
                return
            self.update_btn.config(state="normal")
            self.update_checked(latest)
        self.after(200, wait)

    def update_checked(self, latest):
        if latest is None:
            self.update_status.config(
                text="Couldn't reach GitHub - check the internet connection and try again.",
                fg="#C00000")
            return
        version, notes, url = latest
        mine = parse_version(APP_VERSION)
        if mine is None:
            self.update_status.config(
                text=f"Newest version: {version}. This copy runs from the source code, so it "
                     f"can't update itself.",
                fg="#666666")
        elif parse_version(version) > mine:
            self.update_status.config(text=f"Version {version} is available.", fg="#666666")
            choice = dialog("Update available",
                            f"Master Converter {version} is available.\n"
                            f"You have version {APP_VERSION}.",
                            ("Update now", "Not now"), sound="done")
            if choice == "Update now":
                self.install_update(version, notes, url)
            else:  # Not now: the update button in the title bar, for later
                self.show_update_button(version, notes, url)
        else:
            self.update_status.config(text=f"You have the newest version ({APP_VERSION}).",
                                      fg="#666666")

    PROFILE_URL = "https://github.com/bocchhii"

    def build_about(self, parent):
        """The About page, all centred like a Windows 98 About box: the logo, the name and
        version, what the app is for, then (below an etched line) who made it, with a link to
        their GitHub profile. It stays in the middle of the page at any window size."""
        page = tk.Frame(parent, bg=BG)
        box = tk.Frame(page, bg=BG)
        box.place(relx=0.5, rely=0.47, anchor="center")

        def etched_line(pady):  # grey over white, like the line under the menu bar
            line = tk.Frame(box, bg=BG, width=400)
            line.pack(pady=pady)
            tk.Frame(line, bg=EDGE_SHADOW, height=1, width=400).pack()
            tk.Frame(line, bg=EDGE_LIGHT, height=1, width=400).pack()
        try:
            logo = Image.open(resource_path("icon.png")).convert("RGBA")
            self._about_logo = ImageTk.PhotoImage(logo.resize((128, 128), Image.LANCZOS))
            tk.Label(box, image=self._about_logo, bg=BG).pack(padx=(0, 21))  # (a touch left)
        except Exception:
            pass  # (no icon file: just the name)
        tk.Label(box, text="Master Converter", bg=BG, font=(FONT[0], 42, "bold"),
                 justify="center").pack(pady=(6, 0))
        version = (f"Version {APP_VERSION}" if parse_version(APP_VERSION)
                   else "Version: dev (running from the source code)")
        EngravedLabel(box, text=version).pack(pady=(2, 0))
        etched_line((14, 14))
        tk.Label(box, text="Do you have an image you want to convert but aren't sure how?\n"
                           "Are you concerned about the safety of online file conversion sites?\n\n"
                           "I designed this tool to solve this issue.",
                 bg=BG, font=(FONT[0], 10, "bold"), justify="center", wraplength=380).pack()
        etched_line((14, 10))
        # who made it, and where to find more of their tools
        EngravedLabel(box, text="Developer: bocchi the old").pack()
        EngravedLabel(box, text="For more future tools, check out my profile:").pack(
            pady=(4, 0))
        link = tk.Label(box, text=self.PROFILE_URL, bg=BG, fg="#0000EE", cursor="hand2",
                        font=(FONT[0], FONT[1], "underline"), justify="center")
        link.pack(pady=(2, 0))
        link.bind("<ButtonRelease-1>", lambda e: self.open_profile())
        return page

    def open_profile(self):
        import webbrowser
        webbrowser.open(self.PROFILE_URL)

    def set_palette(self, name):
        """Settings > Color palette: recolour the title bar (and every message box's); saved
        as the Custom theme. Custom... asks for a colour (color_dialog) each time."""
        color = self.custom_color
        if name == CUSTOM_PALETTE:
            color = color_dialog(self, "Edit Colors", CAPTION_ACTIVE[0], beside=True)
            if not color:  # cancelled: keep the palette there was
                self.palette_var.set(self.palette_name)
                return
            color = custom_palette(color)[0]
        self.save_custom(THEME, name, color)

    def draw_palette_strip(self):
        c = self.palette_strip
        w, h = max(c.winfo_width() - 4, 1), 14
        a, b = ([int(col[i:i + 2], 16) for i in (1, 3, 5)] for col in CAPTION_ACTIVE)
        ramp = Image.new("RGB", (256, 1))
        ramp.putdata([tuple(round(p + (q - p) * i / 255) for p, q in zip(a, b))
                      for i in range(256)])
        self._strip = ImageTk.PhotoImage(ramp.resize((w, h)))
        c.delete("all")
        c.create_image(2, 2, image=self._strip, anchor="nw")

    def toggle_no_icon(self):
        save_settings(no_icon=self.no_icon_var.get())
        self.apply_icon()

    def toggle_sounds(self):
        global SOUNDS_ON
        SOUNDS_ON = self.sounds_var.get()
        save_settings(sounds=SOUNDS_ON)
        play_sound("done")  # a sample when switched on (nothing when off)

    def apply_icon(self):
        """The app's icon - the custom one from Settings if there is one - on the window
        (taskbar, Alt+Tab), before the title, and in the Settings preview."""
        custom = os.path.exists(CUSTOM_ICON)
        path = CUSTOM_ICON if custom else resource_path("icon.png")
        try:
            im = Image.open(path).convert("RGBA")
        except Exception:
            return  # missing / broken icon file: keep what's there
        if custom:
            self._win_icons = [ImageTk.PhotoImage(im.resize((n, n), Image.LANCZOS))
                               for n in (48, 32, 16)]
            self.iconphoto(True, *self._win_icons)
        else:
            self.set_icon()
        if load_settings().get("no_icon"):  # Settings > No icon in the title bar
            self.chrome.icon = None
            self.chrome.draw()
        else:
            self.chrome.set_icon(path)
        if hasattr(self, "icon_preview"):  # (Settings is built after the title bar)
            self._preview_icon = ImageTk.PhotoImage(im.resize((32, 32), Image.LANCZOS))
            self.icon_preview.config(image=self._preview_icon)

    def choose_icon(self):
        path = filedialog.askopenfilename(
            parent=self, title="Choose an icon",
            filetypes=[("Images", "*.ico *.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            im = Image.open(path)
            if im.format == "ICO":  # an icon holds several sizes: use the biggest
                im.size = max(im.info.get("sizes", [im.size]))
            im = im.convert("RGBA")
            side = max(im.size)  # not square: centre it on a transparent square
            square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            square.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
            os.makedirs(os.path.dirname(CUSTOM_ICON), exist_ok=True)
            square.resize((256, 256), Image.LANCZOS).save(CUSTOM_ICON)
        except Exception as e:
            dialog("Master Converter", f"Couldn't use that picture as the icon:\n{e}",
                   sound="error")
            return
        self.apply_icon()

    def reset_icon(self):
        try:
            os.remove(CUSTOM_ICON)
        except OSError:
            pass
        self.apply_icon()

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
        self.quality = tk.IntVar(value=80)  # (the slider stops at 0, 10 ... 100)
        self.qscale = TrackBar(opt, self.quality, from_=0, to=100, length=200)
        self.qscale.grid(row=1, column=1, sticky="w", padx=6)
        self.qnote = EngravedLabel(opt)
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
        self.snote = EngravedLabel(opt)
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
        self.qnote = EngravedLabel(opt)
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
        # a video screen stays black in every theme: #010101 looks the same as black, but the
        # themes' colour tables don't touch it (dark mode lightens black in boxes, as text)
        self.screen = tk.Canvas(box, bg="#010101", relief="sunken", bd=2, highlightthickness=0,
                                height=150)
        self.screen.pack(fill="both", expand=True)
        self.screen.bind("<Configure>", lambda e: self.on_screen_resize())

        bar = tk.Frame(box, bg=BG)
        bar.pack(fill="x", pady=(6, 0))
        self.play_btn = ClassicButton(bar, image=self.icon_play, command=self.toggle_play,
                                      width=30)
        self.play_btn.pack(side="left", fill="y")
        # sunken like the preview screen; the play button stretches to the same height
        self.strip = tk.Canvas(bar, height=self.STRIP_H - 2 * self.STRIP_BD, bg="white",
                               relief="sunken", bd=self.STRIP_BD, highlightthickness=0)
        self._tick_font = tkfont.Font(family="Small Fonts", size=7)  # the classic tiny font
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
        key = (fw, fh, len(self.thumbs), THEME)
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
            c.create_text((ix0 + ix1) // 2, (ty0 + iy1) // 2, font=FONT, fill="#888888",
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
    # Programs started from here (the update's installer, and through it the new version)
    # must start fresh: a PyInstaller app hands its children variables saying "your files
    # are unpacked in my temp folder" - a new Master Converter started by the installer
    # would look for them there after this app had closed and deleted them, and never start.
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if sys.platform == "win32":
        try:  # lets Windows show our icon on the taskbar instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MasterConverter.App")
        except Exception:
            pass
    App().mainloop()
