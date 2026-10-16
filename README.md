# PhotoBouche

Photobooth and live slideshow on two Raspberry Pi 4s, with an optional scream trigger.

People pose, or scream, in the big mouth, and a Canon EOS 5D Mark II takes the picture. The shot
appears within about 2 s on the **control screen** next to the booth and on the **slideshow**.
The slideshow then goes back to a mixed rotation of shots, making-of photos and clips, with
sound.

## Taking a photo

| How | When it works | What it needs |
|---|---|---|
| **The camera's remote** (or its shutter button) | Always, in every mode | Nothing from PhotoBouche: the camera fires, and PhotoBouche picks up the file. |
| **A scream** | When scream mode is on (key **S** on the control screen, or `scream_mode` in the config) | A USB mic named in `[camera] mic`. The shutter fires about 0.25 s into a scream loud enough to pass the SHOOT line, once per scream, then waits about 3 s. |
| **Space** on the control screen, or `photobouche shoot` | Always | For staff and for tests. |

The remote is the normal way to run the booth. The scream trigger is an extra you can turn on
and off during the evening; with no mic configured, PhotoBouche is a plain remote-controlled
photobooth. Every shot counts the same, whatever fired it, and also starts the ~3 s pause
before a scream can fire again.

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
| `photobouche camera` | booth | Owns the 5D. Downloads every shot (remote, scream or Space), files the CR2 in `archive/` and writes a 1920px JPEG to `display/`. `--fake` works without a camera. |
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

Photos and clips from the shared drive **never go in git**: the repo is public. `media/`,
`external_data/` and every image and video extension, in any letter case, are in `.gitignore`.
Put one folder per person in `external_data/`, then:

```sh
photobouche prepare external_data --dry-run              # review every decision first
photobouche prepare external_data media/prepared         # do it
deploy/push-media.sh media/prepared raspi4-1.local       # rsync to the booth (adds, never deletes)
```

What `prepare` does:

- **Duplicates.** Many people post the same material, often forwarded through WhatsApp,
  which re-compresses, shrinks, renames (`IMG-…-WA…`) and strips the metadata. Files are
  therefore matched on what they show: a 32×32 thumbnail for photos; duration plus three
  frames for clips, or the same name and duration for a clip exported twice.
  - **Which copy is kept:** the one with real camera metadata (capture time, camera model),
    then the largest, then the one not named like a WhatsApp file.
  - **Which files can match:** a look-alike only counts if it comes from another person's
    folder. Within one folder, look-alikes are bursts (two 5D frames in the same second), and
    only byte-identical files are dropped.
- **Photos** become upright 1920px JPEGs, from JPEG, PNG, TIFF, CR2 or DNG. Thumbnails below
  800px are dropped. HEIC is ignored: export it as JPEG first.
- **Every clip is re-encoded,** even one that would play, so the Pi never meets a surprise:
  - **Format:** 8-bit H.264 High@4.0 and BT.709, at most 30 fps and 8 Mb/s, with AAC stereo
    and the index up front for streaming.
  - **Picture:** rotation is baked in and the frame fits inside 1920×1080. Portrait clips fit
    in too, because 1080×1920 is past the Pi 4 decoder's limit.
  - **HDR:** iPhone HDR (HLG or Dolby Vision) is converted to standard colour.
  - **Long clips** are cut into equal parts of at most 45 s (`--max-clip`).
- **Names** become lowercase ASCII, prefixed with the folders they came from:
  `Zoé/IMG 1.png` → `zoe_img_1.jpg`.
- **Re-runs** only redo what changed.

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
