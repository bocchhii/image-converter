"""
Image Converter - runs 100% locally.
Setup:   pip install pillow pillow-heif tkinterdnd2
Run:     python image_converter.py
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

from PIL import Image, ImageOps, ImageTk

try:  # adds HEIC/HEIF (iPhone photos) read + write support
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

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
NO_ALPHA = {"JPEG", "BMP", "PPM", "PDF", "PCX"}
LOSSY = {"JPEG", "WEBP", "AVIF", "HEIF", "JPEG2000"}

INPUT_EXTS = sorted(Image.registered_extensions().keys())

# XP-ish palette
BG, BLUE, DARK = "#ECE9D8", "#245EDC", "#0A246A"
FONT = ("Tahoma", 9)


def convert_one(src, dst, fmt, quality):
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        opts = {}
        if fmt in NO_ALPHA:
            if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
        if fmt in LOSSY:
            opts["quality"] = quality
        if fmt == "ICO":
            im = im.convert("RGBA")
            im.thumbnail((256, 256))
        im.save(dst, fmt, **opts)


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("Image Converter")
        self.set_icon()
        self.geometry("520x560")
        self.minsize(480, 520)
        self.configure(bg=BG)
        self.files, self.outdir = [], ""
        self.names = []  # custom name for the converted file (None = keep original)
        self.small = tkfont.Font(family="Tahoma", size=8)

        style = ttk.Style(self)
        style.theme_use("winnative" if "winnative" in style.theme_names() else "clam")

        self.blend_titlebar(BG, "#000000")

        body = tk.Frame(self, bg=BG, padx=10, pady=8)
        body.pack(fill="both", expand=True)

        # File list
        box = tk.LabelFrame(body, text=" Files ", bg=BG, font=FONT, padx=6, pady=6)
        box.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(box, bg="white", relief="sunken", bd=2,
                                highlightthickness=0, height=200)
        self.sb = tk.Scrollbar(box, command=self.canvas.yview)
        self.sb_shown = False  # only packed when the pictures don't fit
        self.canvas.config(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.thumbs, self.selected, self.last_click = [], set(), None
        self._photos, self._redraw_job = [], None
        self.rects, self.press, self.dragging = [], None, False
        self.base_sel, self.ctrl, self.region_h = set(), False, 0
        self._band_img = None
        self.tip, self.tip_job, self.tip_pos = None, None, (0, 0)
        self.tip_index, self.tip_watch = None, None
        self.hover = None  # index of the picture under the cursor
        self.canvas.bind("<Configure>", lambda e: self.schedule_redraw())
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Motion>", self.on_hover)
        self.canvas.bind("<Leave>", self.on_leave)
        self.canvas.bind("<Button-3>", self.on_right_click)
        if sys.platform == "darwin":  # right-click is Button-2 on macOS
            self.canvas.bind("<Button-2>", self.on_right_click)
        self.can_scroll = False
        self.bind("<Delete>", self.on_delete_key)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        btns = tk.Frame(box, bg=BG)
        btns.pack(side="left", fill="y", padx=(6, 0))
        self.btn(btns, "Add...", self.add_files).pack(fill="x", pady=2)
        self.btn(btns, "Remove", self.remove_files).pack(fill="x", pady=2)
        self.btn(btns, "Clear", self.clear_files).pack(fill="x", pady=2)

        # Options
        opt = tk.LabelFrame(body, text=" Options ", bg=BG, font=FONT, padx=6, pady=6)
        opt.pack(fill="x", pady=8)
        tk.Label(opt, text="Convert to:", bg=BG, font=FONT).grid(row=0, column=0, sticky="w")
        self.fmt = tk.StringVar(value="PNG")
        cb = ttk.Combobox(opt, textvariable=self.fmt, values=list(OUT_FORMATS),
                          state="readonly", width=14)
        cb.grid(row=0, column=1, sticky="w", padx=6, pady=2)

        tk.Label(opt, text="Quality:", bg=BG, font=FONT).grid(row=1, column=0, sticky="w")
        self.quality = tk.IntVar(value=100)
        tk.Scale(opt, from_=1, to=100, orient="horizontal", variable=self.quality,
                 bg=BG, font=FONT, length=200, highlightthickness=0).grid(row=1, column=1, sticky="w", padx=6)

        tk.Label(opt, text="Save to:", bg=BG, font=FONT).grid(row=2, column=0, sticky="w")
        self.outlabel = tk.Label(opt, text="(same folder as original)", bg="white",
                                 font=FONT, relief="sunken", bd=2, anchor="w", width=32)
        self.outlabel.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        self.btn(opt, "Browse...", self.pick_outdir).grid(row=2, column=2)

        # Convert + progress
        self.btn(body, "Convert", self.run, bold=True).pack(fill="x", pady=(0, 6), ipady=4)
        self.progress = ttk.Progressbar(body, mode="determinate")
        self.progress.pack(fill="x")
        self.status = tk.Label(body, text="Ready.", bg=BG, font=FONT, anchor="w")
        self.status.pack(fill="x", pady=(4, 0))

        # Drag & drop (works anywhere over the window)
        if HAS_DND:
            for w in (self, body, box, self.canvas):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
        self.redraw()

    def set_icon(self):
        try:
            if sys.platform == "win32":
                self.iconbitmap(resource_path("icon.ico"))
            else:
                self._icon = ImageTk.PhotoImage(Image.open(resource_path("icon.png")))
                self.iconphoto(True, self._icon)
        except Exception:
            pass  # missing icon file: keep the default one

    def blend_titlebar(self, color_hex, text_hex="#FFFFFF"):
        """Windows 11: recolor the native title bar (caption, text, border)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.update()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())

            def colorref(h):  # Windows wants 0x00BBGGRR
                h = h.lstrip("#")
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return ctypes.c_int((b << 16) | (g << 8) | r)

            # 35 = caption color, 36 = caption text color, 34 = border color
            for attr, col in ((35, color_hex), (36, text_hex), (34, color_hex)):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(colorref(col)), ctypes.sizeof(ctypes.c_int))
        except Exception:
            pass  # older Windows: keep the default title bar

    def btn(self, parent, text, cmd, bold=False):
        return tk.Button(parent, text=text, command=cmd, bg=BG, relief="raised", bd=3,
                         font=("Tahoma", 9, "bold" if bold else "normal"),
                         activebackground="#F5F3E8", padx=8)

    # ---- adding files (button or drag & drop) ----
    def add_files(self):
        exts = " ".join("*" + e for e in INPUT_EXTS)
        paths = filedialog.askopenfilenames(
            title="Select images",
            filetypes=[("Images", exts), ("All files", "*.*")])
        self.add_paths(paths)

    def on_drop(self, event):
        self.add_paths(self.tk.splitlist(event.data))
        return event.action

    def add_paths(self, paths):
        valid = tuple(INPUT_EXTS)
        found, skipped = [], 0
        for p in paths:
            if os.path.isdir(p):  # dropped a folder: take the images inside it
                found += [os.path.join(p, f) for f in sorted(os.listdir(p))]
            else:
                found.append(p)
        for p in found:
            if os.path.isdir(p):
                continue
            if not p.lower().endswith(valid):
                skipped += 1
            elif p not in self.files:
                self.files.append(p)
                self.thumbs.append(self.make_thumb(p))
                self.names.append(None)
        msg = f"{len(self.files)} file(s) selected."
        if skipped:
            msg += f" Skipped {skipped} unsupported."
        self.status.config(text=msg)
        self.redraw()

    def make_thumb(self, path):
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((320, 320))
                return im.convert("RGBA")
        except Exception:
            return Image.new("RGBA", (64, 64), "#CCCCCC")

    # --- thumbnail grid: 4 columns, cell size follows the window width ---
    COLS, GAP, TEXT_H = 4, 6, 34

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

    def fit(self, text, max_px):
        """Shorten text with '...' so it fits in max_px."""
        if self.small.measure(text) <= max_px:
            return text
        while len(text) > 1 and self.small.measure(text + "...") > max_px:
            text = text[:-1]
        return text + "..."

    def redraw(self):
        self._redraw_job = None
        self.hide_tip()
        if self.hover is not None and self.hover >= len(self.thumbs):
            self.hover = None
        self.canvas.delete("all")
        self._photos, self.rects = [], []
        if not self.thumbs:
            hint = ("Drag & drop images here\nor click Add..." if HAS_DND
                    else "Click Add... to choose images")
            self.canvas.create_text(self.canvas.winfo_width() // 2,
                                    max(self.canvas.winfo_height(), 100) // 2,
                                    text=hint, font=FONT, fill="#888888", justify="center")
        for i, th in enumerate(self.thumbs):
            x, y, c, ch = self.cell_xy(i)
            sel = i in self.selected
            self.rects.append(self.canvas.create_rectangle(
                x, y, x + c, y + ch, **self.cell_style(sel, i == self.hover)))
            img = th.copy()
            img.thumbnail((c - 8, c - 8), Image.BILINEAR)
            photo = ImageTk.PhotoImage(img)
            self._photos.append(photo)  # keep a reference or Tk drops it
            self.canvas.create_image(x + c // 2, y + c // 2, image=photo)

            src_name = os.path.basename(self.files[i])
            ext = os.path.splitext(src_name)[1]
            name = (self.names[i] + ext) if self.names[i] else src_name
            kind = ext.lstrip(".").upper() or "FILE"
            self.canvas.create_text(x + c // 2, y + c + 9, font=self.small, fill="black",
                                    text=self.fit(name, c - 8))
            self.canvas.create_text(x + c // 2, y + c + 23, font=self.small, fill="#666666",
                                    text=f"{kind} file")
        view_w, view_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        content_h = self.content_height(view_w)
        self.can_scroll = bool(self.thumbs) and content_h > view_h
        # region at least as big as the view, so an empty/short list can't be scrolled
        self.region_h = max(content_h, view_h)
        self.canvas.config(scrollregion=(0, 0, view_w, self.region_h))
        if not self.can_scroll:
            self.canvas.yview_moveto(0)
        self.update_scrollbar()

    def on_wheel(self, event):
        self.hide_tip()
        if self.can_scroll:
            self.canvas.yview_scroll(-1 * (event.delta // 120), "units")
            self.update_hover(event.x, event.y)

    def content_height(self, width):
        c = max(48, (max(width, 100) - self.GAP * (self.COLS + 1)) // self.COLS)
        rows = -(-len(self.thumbs) // self.COLS)
        return self.GAP + rows * (c + self.TEXT_H + self.GAP)

    def update_scrollbar(self):
        """Show the scrollbar only when the pictures are taller than the box."""
        view_h = self.canvas.winfo_height()
        if view_h <= 1:  # window not laid out yet
            return
        # judge by the full width (scrollbar hidden) so it can't flicker on/off
        full_w = self.canvas.winfo_width() + (self.sb.winfo_reqwidth() if self.sb_shown else 0)
        need = bool(self.thumbs) and self.content_height(full_w) > view_h
        if need == self.sb_shown:
            return
        self.sb_shown = need
        if need:
            self.sb.pack(side="left", fill="y", after=self.canvas)
        else:
            self.sb.pack_forget()
            self.canvas.yview_moveto(0)

    def cell_style(self, sel, hover=False):
        # selected: blue shade + thick blue border; hover: same shade, thin grey border
        return dict(width=3 if sel else 1, outline=BLUE if sel else "#BBBBBB",
                    fill="#DCE8FF" if (sel or hover) else "#F7F7F7")

    def refresh_selection(self):
        """Restyle the cell boxes only (fast: no picture redraw)."""
        for i, r in enumerate(self.rects):
            self.canvas.itemconfig(r, **self.cell_style(i in self.selected, i == self.hover))

    def hit_test(self, x, y):
        for i in range(len(self.thumbs)):
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
                self.selected.clear()
        elif self.ctrl:  # Ctrl: toggle
            self.selected ^= {hit}
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
        for i in range(len(self.thumbs)):
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

    # ---- hover tooltip: appears after the mouse rests ~1s on a picture ----
    TIP_DELAY = 1000

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

    @staticmethod
    def fmt_size(n):
        n = float(n)
        for unit in ("bytes", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{int(n)} bytes" if unit == "bytes" else f"{n:.2f} {unit}"
            n /= 1024

    def file_info(self, path):
        ext = os.path.splitext(path)[1].lstrip(".").upper() or "FILE"
        lines = [f"Item type: {ext} File"]
        try:
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

    def show_tip(self, i, pos):
        self.tip_job = None
        if i >= len(self.files):
            return
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        tk.Label(tip, text="\n".join(self.file_info(self.files[i])), justify="left",
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

    # ---- right-click menu: Rename / Duplicate / Remove ----
    def out_base(self, i):
        """Name (without extension) the converted file will get."""
        return self.names[i] or os.path.splitext(os.path.basename(self.files[i]))[0]

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

        menu = tk.Menu(self, tearoff=0, font=FONT, bg=BG, fg="black", bd=1,
                       activebackground="#316AC5", activeforeground="white")
        menu.add_command(label="Rename", command=lambda: self.rename_item(hit))
        menu.add_command(label="Duplicate", command=lambda: self.duplicate_item(hit))
        menu.add_separator()
        menu.add_command(label="Remove", command=self.remove_files)  # same as the button
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def rename_item(self, i):
        """Renames the converted file only - the original on disk is never touched."""
        win = tk.Toplevel(self)
        win.title("Rename")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self)
        tk.Label(win, text="New name for the converted file\n(the file type is added automatically):",
                 bg=BG, font=FONT, justify="left").pack(anchor="w", padx=12, pady=(12, 4))
        var = tk.StringVar(value=self.out_base(i))
        entry = tk.Entry(win, textvariable=var, font=FONT, width=38, relief="sunken", bd=2)
        entry.pack(padx=12)
        err = tk.Label(win, text="", bg=BG, fg="#C00000", font=FONT)
        err.pack(anchor="w", padx=12)

        def ok(_=None):
            name = var.get().strip().rstrip(".")
            if not name:
                err.config(text="The name can't be empty.")
            elif any(ch in name for ch in '\\/:*?"<>|'):
                err.config(text='A name can\'t contain any of:  \\ / : * ? " < > |')
            else:
                self.names[i] = name
                self.status.config(text=f"Will be saved as \"{name}\".")
                win.destroy()
                self.redraw()

        row = tk.Frame(win, bg=BG)
        row.pack(pady=(4, 12))
        self.btn(row, "OK", ok).pack(side="left", padx=4)
        self.btn(row, "Cancel", win.destroy).pack(side="left", padx=4)
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()  # centre over the main window
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{x}+{y}")
        entry.focus_set()
        entry.select_range(0, "end")
        win.grab_set()

    def duplicate_item(self, i):
        """Adds a copy of the picture right after it. Nothing is saved until Convert."""
        taken = {self.out_base(k) for k in range(len(self.files))}
        base = self.out_base(i)
        name, n = f"{base} - Copy", 2
        while name in taken:
            name = f"{base} - Copy ({n})"
            n += 1
        self.files.insert(i + 1, self.files[i])
        self.thumbs.insert(i + 1, self.thumbs[i])
        self.names.insert(i + 1, name)
        self.selected = {i + 1}  # select the new copy
        self.last_click = i + 1
        self.status.config(text=f"Duplicated. {len(self.files)} file(s) selected.")
        self.redraw()

    def on_delete_key(self, event):
        """Delete key = same as the Remove button (ignored while typing in a text box)."""
        try:
            if isinstance(self.focus_get(), (tk.Entry, ttk.Entry)):
                return
        except KeyError:
            pass
        if self.selected:
            self.hide_tip()
            self.remove_files()

    def remove_files(self):
        keep = [i for i in range(len(self.files)) if i not in self.selected]
        self.files = [self.files[i] for i in keep]
        self.thumbs = [self.thumbs[i] for i in keep]
        self.names = [self.names[i] for i in keep]
        self.selected.clear()
        self.status.config(text=f"{len(self.files)} file(s) selected.")
        self.redraw()

    def clear_files(self):
        self.files.clear()
        self.thumbs.clear()
        self.names.clear()
        self.selected.clear()
        self.status.config(text="Ready.")
        self.redraw()

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.outdir = d
            self.outlabel.config(text=d)

    def run(self):
        if not self.files:
            messagebox.showinfo("Image Converter", "Add some files first.")
            return
        label = self.fmt.get()
        fmt, ext = OUT_FORMATS[label]
        q = self.quality.get()
        self.progress.config(maximum=len(self.files), value=0)
        ok, errors = 0, []
        for i, src in enumerate(self.files, 1):
            folder = self.outdir or os.path.dirname(src)
            base = self.names[i - 1] or os.path.splitext(os.path.basename(src))[0]
            dst = os.path.join(folder, base + ext)
            n = 1
            while os.path.exists(dst):  # never overwrite
                dst = os.path.join(folder, f"{base}_converted{n}{ext}")
                n += 1
            self.status.config(text=f"Converting {os.path.basename(src)}...")
            self.update()
            try:
                convert_one(src, dst, fmt, q)
                ok += 1
            except Exception as e:
                errors.append(f"{os.path.basename(src)}: {e}")
            self.progress.config(value=i)
        self.status.config(text=f"Done. {ok} converted, {len(errors)} failed.")
        if errors:
            messagebox.showwarning("Some files failed", "\n".join(errors[:10]))
        else:
            messagebox.showinfo("Image Converter", f"Converted {ok} file(s) to {label}.")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:  # lets Windows show our icon on the taskbar instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ImageConverter.App")
        except Exception:
            pass
    App().mainloop()
