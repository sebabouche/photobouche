# PhotoBouche

Scream-triggered photobooth and live slideshow on two Raspberry Pi 4s.

People scream into the big mouth. A Canon EOS 5D Mark II fires, either from its remote or by
itself when the scream is loud enough. The shot appears within about 2 s on the **control
screen** next to the booth, which shows the latest shot plus an equalizer of the mic, and on the
**slideshow**. The slideshow then goes back to a mixed rotation of shots, making-of photos and
clips, with sound.

```
 USB mic ─▶ BOOTH Pi (raspi4-1)                                 SLIDESHOW Pi (raspi4-2)
 5D Mk II ─USB─▶ photobouche camera   tether, develop, scream trigger
                 photobouche booth    HTTP: /api/changes, /media ────▶ photobouche screen --profile show
                 photobouche screen --profile booth                    (mpv, sound, cache)
                 (pygame: last shot + equalizer)
```

## Commands

| Command | Where | What |
|---|---|---|
| `photobouche camera` | booth | Owns the 5D. Downloads every shot, files the CR2 in `archive/` and writes a 1920px JPEG to `display/`. Fires the shutter on a scream. `--fake` works without a camera. |
| `photobouche booth` | booth | HTTP server for the slideshow (long-poll feed + files with Range). |
| `photobouche screen --profile booth` | booth | Control screen: latest shot, equalizer, SHOOT line. |
| `photobouche screen --profile show` | slideshow | The show. `--windowed` for a laptop. |
| `photobouche mics` / `listen <mic>` | booth | Pick the mic by name; watch floor, level and centroid to tune `trigger_db`. |
| `photobouche shoot` | booth | Fire the camera from a terminal. |
| `photobouche doctor` | both | Checks mpv, gphoto2, camera, mic, data folder, booth reachability. |
| `photobouche prepare <src> [dest]` | laptop | Makes making-of material Pi-friendly (see below). |
| `photobouche check-stream` | slideshow | Measures, for every clip on the booth, download speed against the clip's bitrate. |

### Control screen keys

| Key | Action |
|---|---|
| **X** | Hide the shot on screen. It moves to `rejected/` (never deleted) and leaves the slideshow within a second. |
| **S** | Turn scream mode on or off. |
| **M** | Switch between latest and cycle. |
| **+ / −** | Move the trigger threshold by 1 dB. |
| **Space** | Fire the camera. |
| **Esc** | Quit. |

## Camera (5D Mk II)

- **Quality: RAW + S1 JPEG.** The hook uses whichever file arrives first. Even with RAW alone, it
  never decodes the raw data: it reads the full-size JPEG the camera embeds in every CR2.
- Set auto power-off to **Disable** and set the clock (archive folders are by date).
- Use **manual focus**, or a fixed AF point, for repeatable shots of the mouth.
- The daemon sets `capturetarget` to the memory card, so the CF card keeps a copy of
  everything.
- Darktable must **not** run: only one program can hold the camera.

## Booth Pi setup (Pi OS with desktop)

```sh
sudo apt install python3-venv libportaudio2 gphoto2 exiv2
# The desktop grabs cameras for the file manager; that blocks gphoto2 ("Could not claim the USB device").
systemctl --user mask --now gvfs-gphoto2-volume-monitor
sudo mkdir -p /srv/photobouche /etc/photobouche && sudo chown pi: /srv/photobouche
git clone https://github.com/sebabouche/photobouche.git ~/photobouche && cd ~/photobouche
python3 -m venv .venv && .venv/bin/pip install -e '.[booth]'
sudo cp config.example.toml /etc/photobouche/config.toml   # set [camera] mic
cp deploy/photobouche-*.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now photobouche-camera photobouche-booth
sudo loginctl enable-linger pi
cp deploy/photobouche-screen-booth.desktop ~/.config/autostart/
.venv/bin/photobouche doctor
```

Making-of photos and clips go to `/srv/photobouche/making_of/` and `/srv/photobouche/clips/`
on the booth Pi (see "Making-of media" below). They appear in the show within a second, with no
restart.

## Slideshow Pi setup (Pi OS with desktop)

```sh
sudo apt install python3-venv mpv
git clone https://github.com/sebabouche/photobouche.git ~/photobouche && cd ~/photobouche
python3 -m venv .venv && .venv/bin/pip install -e .
sudo mkdir -p /etc/photobouche && sudo cp config.example.toml /etc/photobouche/config.toml  # set booth_url
cp deploy/photobouche-screen-show.desktop ~/.config/autostart/
.venv/bin/photobouche doctor
```

When the booth is unreachable, the show keeps going with the images already cached (up to
`cache_mb`) and the slideshow Pi's own `local_clips`.

## Making-of media

Photos and clips from the shared drive **never go in git**: the repo is public, and `media/`
plus every image and video extension are in `.gitignore`. Drop them in `media/` or anywhere
else, then run:

```sh
photobouche prepare media/shared media/prepared --transcode   # 1920px JPEGs; clips → H.264 ≤1080p
deploy/push-media.sh media/prepared raspi4-1.local             # rsync to the booth (adds, never deletes)
```

`prepare` turns any image (JPEG, PNG, TIFF, CR2) into an upright 1920px JPEG. That keeps the
slideshow Pi's memory and SD cache small. It names files after their folders, so two
`IMG_0001.jpg` from different folders don't collide. It skips files that are already up to
date.

Clips are probed with ffprobe. Anything other than H.264 at 1080p or below is reported, and
with `--transcode` it is re-encoded by ffmpeg. HEIC photos from phones are ignored: export them
as JPEG first.

Then, on the slideshow Pi, check that the booth's data actually plays well from there:

```sh
photobouche check-stream     # per clip: Mb/s off the booth vs the clip's bitrate; want ×3 or more
photobouche screen --profile show --windowed -v   # watch: clips play with sound, new shots cut in
```

Below ×1.5 the clip will stall: use Ethernet between the Pis, or transcode it lighter.

## On a laptop, without the Pis

```sh
poetry install --extras booth            # plus `brew install mpv` for the show
photobouche camera --root ./demo --fake --mic "MacBook"
photobouche booth  --root ./demo
photobouche screen --profile booth --root ./demo --windowed
photobouche screen --profile show  --booth-url http://localhost:8765 --windowed
```

macOS: the terminal needs microphone permission. Without it, the mic records silence with no
error, and the control screen says "micro muet".

## Development

```sh
poetry run pytest
poetry run ruff check .
```

`src/photobouche/audio/` is copied from treek 0.20.5 (scream detector and mic input), with
equalizer bands and `active_duration` added. It isn't imported from treek, so the two projects
can change independently.
