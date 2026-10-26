# Flathub Publishing Instructions

This document provides step-by-step instructions for publishing TV Remote to Flathub.

## Prerequisites

1. A GitHub account
2. A Flathub account (sign up at https://flathub.org)
3. Your app passing validation checks

## Step 1: App ID Verification

The application ID has been updated to `io.github.tecix.TvRemote`, which follows Flathub's reverse DNS naming requirements for GitHub-hosted projects.

## Step 2: Add Screenshots

Take screenshots of your application and save them in the `screenshots/` directory:

```bash
# Take a screenshot of the main window
gnome-screenshot -w -f screenshots/main-window.png

# Take a screenshot of the device dialog
gnome-screenshot -w -f screenshots/device-dialog.png
```

Requirements:
- PNG format, at least 620px wide
- Show the main interface and key features
- Light and dark variants are appreciated

Then push them to your GitHub repository so the URLs in the metainfo.xml work.

## Step 3: Validate Your Metainfo

Install the appstream validator:

```bash
sudo dnf install appstream  # Fedora
sudo apt install appstream  # Ubuntu/Debian
```

Validate your metainfo file:

```bash
appstreamcli validate data/io.github.tecix.TvRemote.metainfo.xml
```

Fix any reported issues before proceeding.

## Step 4: Test Your Flatpak Build

```bash
# Clean build
rm -rf build-dir .flatpak-builder
./build-and-run.sh

# Test the built app
flatpak run io.github.tecix.TvRemote
```

## Step 5: Fork the Flathub Repository

1. Go to https://github.com/flathub/flathub
2. Fork the repository to your account

## Step 6: Create Your App Submission

1. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/flathub.git
   cd flathub
   ```

2. Create a new branch for your app:
   ```bash
   git checkout -b new-pr
   ```

3. Copy your manifest to the root directory (Flathub requires the manifest in the repo root, not in a subdirectory):
   ```bash
   cp /path/to/gnome-tv-remote/flatpak/io.github.tecix.TvRemote.yml io.github.tecix.TvRemote.yml
   ```

4. **Important**: Modify the manifest for Flathub:
   - Ensure `app-id:` is `io.github.tecix.TvRemote`
   - Change the source from `type: dir` to `type: git` pointing to your GitHub repo:
   
   ```yaml
   - name: android-tv-remote
     buildsystem: simple
     build-commands:
       - pip3 install --prefix=/app --no-deps --no-build-isolation .
       - install -D data/io.github.tecix.TvRemote.desktop /app/share/applications/io.github.tecix.TvRemote.desktop
       - install -D data/io.github.tecix.TvRemote.metainfo.xml /app/share/metainfo/io.github.tecix.TvRemote.metainfo.xml
       - install -D data/icons/hicolor/scalable/apps/io.github.tecix.TvRemote.svg /app/share/icons/hicolor/scalable/apps/io.github.tecix.TvRemote.svg
       - install -D data/io.github.tecix.TvRemote.gschema.xml /app/share/glib-2.0/schemas/io.github.tecix.TvRemote.gschema.xml
       - glib-compile-schemas /app/share/glib-2.0/schemas
     sources:
       - type: git
         url: https://github.com/tecix/gnome-tv-remote.git
         tag: v1.0.5  # Use a release tag
   ```

5. Commit and push:
   ```bash
   git add .
   git commit -m "Add io.github.tecix.TvRemote"
   git push origin new-pr
   ```

6. Open a Pull Request on https://github.com/flathub/flathub

## Step 7: Respond to Review

The Flathub team will review your submission. Common feedback includes:

- App ID format issues (use `io.github.tecix.TvRemote`)
- Missing or invalid metainfo fields
- Permission concerns
- Screenshot requirements

Address any feedback promptly to get your app published.

## Step 8: After Acceptance

Once accepted, your app will be automatically built and published to Flathub. Users can then install it with:

```bash
flatpak install flathub io.github.tecix.TvRemote
```

## Updating Your App

For future updates:

1. Create a new release/tag on your GitHub repository
2. Update the manifest in your Flathub repository with the new tag
3. The Flathub buildbot will automatically rebuild and publish

## Resources

- [Flathub App Submission](https://github.com/flathub/flathub/wiki/App-Submission)
- [Flatpak Manifest Documentation](https://docs.flatpak.org/en/latest/manifests.html)
- [AppStream Metainfo Guidelines](https://www.freedesktop.org/software/appstream/docs/chap-Metadata.html)
- [Flathub Quality Guidelines](https://docs.flathub.org/docs/for-app-authors/metainfo-guidelines/)

## Local AirPlay files and transcoding

The GNOME 49 manifest uses the Freedesktop 25.08 base. Use that runtime's
`/usr/bin/ffmpeg` and `ffprobe`; do not bundle a second FFmpeg or request host
execution permissions. The old `org.freedesktop.Platform.ffmpeg-full` extension
is not needed for this runtime generation. The runtime declares
`org.freedesktop.Platform.codecs-extra` (branch `25.08-extra`) and adds its
libraries to the loader path. Ensure the matching extension is available when
validating H.264 software encoding; a user may have masked extra codecs.
See the [Flatpak extension documentation](https://docs.flatpak.org/en/latest/extension.html).

The supervisor verified the installed 25.08 runtime with `codecs-extra`:
`ffmpeg`, `ffprobe`, `libx264`, `h264_vaapi`, and `aac` are present; `h264_qsv`
is absent. The sender therefore tries full VAAPI decode, rotation and scaling
(`transpose_vaapi`, `scale_vaapi`) plus `h264_vaapi` inside Flatpak, then
`libx264`, then `libopenh264` if available. Encoder initialization failures
before the first three segments automatically retry with the next encoder.
The existing `--device=dri` permission enables hardware access. No additional
filesystem permission is needed: the GTK file chooser uses the document portal.

From source, FFmpeg tools come from `/usr/lib/jellyfin-ffmpeg/` when installed
(the tested Intel QSV build), otherwise from `PATH`. Source runs prefer full QSV
decode/rotation/scaling/encoding and fall back to software. No media is converted
on the TV. A missing tool or exhausted encoder fallbacks appears in the dialog.
The GNOME 49 Flatpak itself was not rebuilt during this change; GNOME 49 is not
installed on the development laptop. Validate the exact release runtime before
publishing rather than assuming a host encoder is available in the sandbox.

Choose **Play a local file…** in AirPlay. The sender probes the selected file,
reads `/tinytv/caps` (404 uses legacy defaults), and sends compatible files
unchanged over an HTTP Range server bound to the laptop's route to the TV.
Other files become H.264 High level 4.1 or below, at most 1920×1080 and 30 fps,
with applied rotation and AAC-LC stereo 48 kHz. Portrait videos remain portrait.
The literal `index.m3u8` URL uses two-second MPEG-TS segments and an EVENT
playlist ending with `EXT-X-ENDLIST`. Playback begins after three segments
(or completion of a shorter clip). Conversion progress appears in the dialog;
seeking is disabled while conversion is in progress. Full output remains cached;
seeking after conversion also requires the receiver to report a seekable duration.
This TV reported duration zero for the EVENT streams during the smoke test. Stop, another play request, closing the dialog,
or exiting removes the temporary session under `$XDG_CACHE_HOME/tv-remote`.
Keep the laptop awake and reachable on the LAN during playback.

Offline checks (with the application's Python dependencies installed):

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tests/manual/convert_clips.py [encoder] [output-directory]` converts eight seconds
of each reference clip without touching the TV and verifies output metadata.
Hardware recipes need a session where `/dev/dri` is visible.

Validation on the development laptop: the supervisor ran both full QSV and full
VAAPI conversion pipelines successfully on the Pixel and XiaoYing clips. Source
AirPlay smoke tests exercised the automatic QSV-to-libx264 fallback because GPU
devices are hidden in the agent sandbox: both clips reported PLAYING with an
advancing position and no receiver error. Converted Pixel frames were visually
verified upright; the physical panel was not independently checked in this test.
