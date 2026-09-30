# Master Converter

A free, offline Windows app for converting **images, videos and sound** - and making **GIFs** -
with a classic Windows XP look. Everything runs on your own PC: no accounts, no ads.
(The only time it goes online is to check this page for a newer version - see *Updates* below.)

## Features

### Images
- Convert between **PNG, JPEG, WEBP, AVIF, HEIC, GIF, BMP, TIFF, ICO, PDF, TGA, PPM, JPEG 2000,
  DDS, PCX, SVG** and **RAW pixel data**.
- Opens **camera RAW photos** (Canon CR2/CR3, Nikon NEF, Sony ARW, DNG, Fujifilm RAF, Olympus ORF,
  Panasonic RW2, ...) and **SVG drawings**, as well as all of the above.
- Quality slider for JPEG / WEBP / AVIF / HEIC; keeps photo info (date, camera, GPS) if you want.
- Rename or duplicate files before converting (right-click).

### Videos
- Convert to **MP4, WEBM, MKV, MOV, AVI, GIF**, or keep just the sound as **MP3**.
- Quality, size (4K ... 360p), frame rate, and "remove sound" options.
- Live progress per file, and a Cancel button.

### Audio
- Convert sound between **MP3, WAV, M4A (AAC), FLAC, OGG, OPUS, AIFF, WMA** - or take the sound
  out of a video.
- Quality (**Original** keeps each file's own quality, copying it untouched when the format
  doesn't change), sample rate, and stereo / mono.

### GIF Maker
- Open a video (or a GIF), pick the part you want on a Movie Maker-style timeline, preview it,
  and save it as a GIF that **loops forever or plays once**.
- Shows the expected file size before you make it.

### Everywhere
- Drag & drop files or whole folders.
- Two views of your files: **thumbnails** or **details** - switch with the button or
  **Ctrl + mouse wheel**.
- Select several files with Ctrl / Shift / dragging; hover a file for its details.
- "Show converted files" opens the folder with the new files highlighted.
- Never overwrites anything: a name that's taken gets `_converted1`, `_converted2`, ...
- **Dark mode**: the small moon / sun button at the right end of the tabs switches the whole app
  between light and dark, and it remembers your choice.

---

## Download and install (for users)

1. Go to the [**Releases**](../../releases/latest) page and download **MasterConverter-Setup.exe**.
2. Run it. If Windows shows **"Windows protected your PC"**, click **More info -> Run anyway**
   (see the note below).
3. Click **Next -> Install -> Finish**, then open **Master Converter** from the Start menu.

To uninstall: **Settings -> Apps -> Installed apps -> Master Converter -> Uninstall**.

### Updates
When the app starts, it checks this project's Releases page for a newer version. If there is one,
it offers **Update now**, **Remind me later** or **Skip this version**. *Update now* downloads and
installs the new version by itself, restarts the app and shows what's new. The check only reads
the public list of releases - nothing about you or your files is sent. To turn it off, set
`"check_updates": false` in `%APPDATA%\Master Converter\settings.json`.

> **About the "Windows protected your PC" warning:** Windows shows it for any program from an
> unknown publisher. It doesn't mean the app is unsafe - the app just isn't digitally signed
> (that needs a paid code-signing certificate).

---

## Run from the source code

Needs [Python](https://www.python.org/downloads/) 3.10 or newer (tick **Add python.exe to PATH**
when installing).

```
pip install -r requirements.txt
python master_converter.py
```

## Build the installer yourself

**On your own PC:** install [Inno Setup](https://jrsoftware.org/isdl.php) (free), then double-click
**`build.bat`**. It makes `dist\MasterConverter.exe` and the installer
`installer_output\MasterConverter-Setup.exe`.

**With GitHub (automatic):** push a tag such as `v1.0.0` (or create a release with that tag on
GitHub). The workflow in `.github/workflows/release.yml` builds the installer in the cloud and
attaches it to the release - watch the **Actions** tab; it takes about 5 minutes.
For a new version, publish a new tag: `v1.0.1`, `v1.1.0`, ... The tag's number becomes the app's
version, and every installed copy offers the update the next time it starts; the release's
description is what users see as "what's new" after updating. (Builds made with `build.bat` or run
from the source code have no version, so they never check for updates.)

## What's in this repository

| File | What it is |
|---|---|
| `master_converter.py` | the whole app |
| `requirements.txt` | the Python packages it needs |
| `icon.ico` / `icon.png` | the app icon (replace both, keeping the names, to change it) |
| `build.bat` | one click: builds the .exe and the installer on your PC |
| `installer.iss` | the recipe for the installer (used by Inno Setup) |
| `.github/workflows/release.yml` | builds the installer on GitHub for every release tag |

## Built with

- [Pillow](https://python-pillow.org/) and [pillow-heif](https://github.com/bigcat88/pillow_heif) - images, HEIC
- [FFmpeg](https://ffmpeg.org/) via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) - video, sound, GIFs
- [LibRaw](https://www.libraw.org/) via [rawpy](https://github.com/letmaik/rawpy) - camera RAW photos
- [resvg](https://github.com/linebender/resvg) via [resvg_py](https://github.com/baseplate-admin/resvg-py) - SVG drawings
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) - drag & drop
- [PyInstaller](https://pyinstaller.org/) and [Inno Setup](https://jrsoftware.org/isinfo.php) - the .exe and the installer

Each of these has its own license. Note that the FFmpeg build bundled by imageio-ffmpeg is
licensed under the GPL, which applies when you distribute the built app.
