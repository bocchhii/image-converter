# Image Converter

A small offline tool that converts images between formats (PNG, JPEG, WEBP, HEIC, ...).

## What's in this folder

| File | What it is |
|---|---|
| `image_converter.py` | the app itself |
| `icon.ico` / `icon.png` | the app icon (replace these to change it) |
| `build.bat` | one click: builds `ImageConverter.exe` and the `Setup` file |
| `installer.iss` | the recipe for the Setup file (used by Inno Setup) |
| `.github/workflows/release.yml` | builds and publishes a download page automatically |

---

## Option A - build the Setup file on your own PC (10 minutes)

1. Install **Python** from https://www.python.org/downloads/ (tick **Add python.exe to PATH**).
2. Install **Inno Setup** (free) from https://jrsoftware.org/isdl.php.
3. Double-click **`build.bat`**.
4. When it says DONE, your installer is at **`installer_output\ImageConverter-Setup.exe`**.
5. Send that one file to people (Google Drive, Dropbox, WeTransfer, ...).

## Option B - get a real download link with GitHub (recommended)

This builds the Setup file for you in the cloud and gives you a permanent link.

1. Make a free account at https://github.com and create a **new repository** (e.g. `image-converter`).
2. Upload everything in this folder to it (including the hidden `.github` folder).
3. Go to the repository -> **Releases** -> **Create a new release**, type the tag **`v1.0.0`**
   and publish it (or push the tag from git).
4. Wait ~5 minutes (watch the **Actions** tab). The Setup file appears on the release page.
5. Send people this link (replace the names):
   `https://github.com/YOUR-USERNAME/image-converter/releases/latest`

For a new version, change the code, then publish a new release with the tag `v1.0.1`, `v1.1.0`, etc.

---

## What your users do

1. Open your link and download **ImageConverter-Setup.exe**.
2. Double-click it. If Windows shows **"Windows protected your PC"**, click
   **More info -> Run anyway** (see the note below).
3. Click **Next -> Install -> Finish**.
4. Open **Image Converter** from the Start menu (or the desktop shortcut if they ticked it).

To uninstall: **Settings -> Apps -> Installed apps -> Image Converter -> Uninstall**.

### About the "Windows protected your PC" warning
Windows shows it for any program from an unknown publisher. It doesn't mean the app is unsafe -
the app is simply not digitally signed. Removing the warning needs a paid code-signing
certificate (or a service such as Azure Trusted Signing). For sharing with friends, telling them
to click **More info -> Run anyway** is normal.

## Changing the icon
The icon is already your own design. To change it, replace `icon.ico` and `icon.png` (keep the names) and build again.
Tip: this very app can convert any picture to **ICO** for you.
