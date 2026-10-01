# Master Converter

Ever needed to convert a file but didn't trust those online converter sites? That's why I made
this. Master Converter is a small Windows app that converts your images, videos and audio, and
makes GIFs, all on your own PC. No accounts, no ads, and your files never leave your computer.

It looks and feels like Windows 98, because why not.

## What it can do

- **Images:** convert between all the usual formats (PNG, JPEG, WEBP, HEIC, and lots more). It
  even opens camera RAW photos.
- **Videos:** convert to MP4, WEBM, MKV and others, change the size or quality, or remove the sound.
- **Audio:** convert between MP3, WAV, FLAC and others, or pull the sound out of a video.
- **GIF Maker:** pick a part of a video and turn it into a GIF.

Just drag your files in, pick a format and hit Convert. It never overwrites your originals. You
can also switch between a few themes from the Theme menu.

## Install

1. Download **MasterConverter-Setup.exe** from the [Releases](../../releases/latest) page.
2. Run it. If Windows says **"Windows protected your PC"**, click **More info → Run anyway**.
   That warning shows up for any app that isn't signed with a paid certificate. It doesn't mean
   the app is unsafe.
3. Click through the installer and open Master Converter from the Start menu.

The app tells you when there's a new version and can update itself. Checking for updates is the
only time it goes online.

## Run it from the code

You'll need [Python](https://www.python.org/downloads/) 3.10 or newer.

```
pip install -r requirements.txt
python master_converter.py
```

To build the installer yourself, install [Inno Setup](https://jrsoftware.org/isdl.php) and run
`build.bat`. New releases are built automatically on GitHub when a version tag (like `v1.2.0`) is
pushed.

## Thanks to

- [Pillow](https://python-pillow.org/) and [pillow-heif](https://github.com/bigcat88/pillow_heif) for images
- [FFmpeg](https://ffmpeg.org/) (through [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)) for video, audio and GIFs
- [rawpy](https://github.com/letmaik/rawpy) for camera RAW photos
- [resvg_py](https://github.com/baseplate-admin/resvg-py) for SVG files
- [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) for drag and drop
- [PyInstaller](https://pyinstaller.org/) and [Inno Setup](https://jrsoftware.org/isinfo.php) for the app and installer

Each of these has its own license. The FFmpeg build that comes with imageio-ffmpeg is under the
GPL, which applies if you share the built app.

Made by **bocchi the old**. For more tools, check out [my GitHub profile](https://github.com/bocchhii).
