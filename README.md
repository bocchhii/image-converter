# Image Converter

A simple Windows app that converts images between formats. It runs entirely offline, so your files never leave your computer.

## Download

Get the latest installer from the [Releases page](https://github.com/bocchhii/image-converter/releases/latest) and download `ImageConverter-Setup.exe`.

## Installation

1. Download `ImageConverter-Setup.exe` from the Releases page.
2. Run the installer and follow the steps.
3. If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**. This warning appears because the app is not code-signed.
4. Launch **Image Converter** from the Start menu.

To uninstall, go to Settings > Apps > Installed apps > Image Converter.

## Features

- Convert to PNG, JPEG, WEBP, HEIC, AVIF, BMP, GIF, TIFF, ICO, PDF, TGA, PPM, JPEG 2000, DDS and PCX
- Open most common image formats, including HEIC photos from iPhones
- Batch conversion of many files at once
- Drag and drop files or folders
- Thumbnail previews with file name, type and details on hover
- Adjustable quality for lossy formats
- Choose an output folder, or save next to the originals
- Right-click to rename, duplicate or remove an image
- Original files are never modified or overwritten

## Usage

1. Add images by dragging them into the window or clicking **Add...**
2. Choose a format in **Convert to**.
3. Set the quality (applies to JPEG, WEBP and other lossy formats).
4. Optionally choose an output folder with **Browse...**
5. Click **Convert**.

### Shortcuts

| Action | Result |
|---|---|
| Click | Select an image |
| Ctrl + click | Add or remove an image from the selection |
| Shift + click | Select a range |
| Click and drag | Select multiple images with a selection box |
| Right-click | Rename, duplicate or remove |
| Delete | Remove the selected images from the list |

Renaming only changes the name of the converted file. Nothing is saved until you click **Convert**.

## Notes

- Converting transparent images to JPEG gives them a white background.
- Only the first frame of animated GIFs is converted.
- Camera RAW files and SVG are not supported.

## Building from source

Requires Python 3.

```
pip install pillow pillow-heif tkinterdnd2
python image_converter.py
```

To build the installer, install [Inno Setup](https://jrsoftware.org/isdl.php) and run `build.bat`.
