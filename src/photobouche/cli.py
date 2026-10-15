"""photobouche — one command per role.

Booth Pi:      photobouche camera · photobouche booth · photobouche screen --profile booth
Slideshow Pi:  photobouche screen --profile show
Anywhere:      photobouche doctor · mics · listen · shoot
Media:         photobouche prepare (laptop) · photobouche check-stream (slideshow Pi)
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import time
from pathlib import Path

import typer
from rich.console import Console

from photobouche import config as config_mod

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
console = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="TOML file (default /etc/photobouche/config.toml).")
RootOpt = typer.Option(None, "--root", help="Data folder, overriding [paths] root.")


def _setup(config: Path | None, root: Path | None, verbose: bool = False) -> config_mod.Config:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return config_mod.load(config, root=root)


@app.command()
def camera(
    config: Path | None = ConfigOpt,
    root: Path | None = RootOpt,
    fake: bool = typer.Option(False, help="No 5D: make up a picture on each trigger."),
    mic: str | None = typer.Option(None, help="Microphone name, overriding [camera] mic."),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Own the camera: download and develop every shot, fire on screams."""
    from photobouche.camera.daemon import run_daemon

    cfg = _setup(config, root, verbose)
    with contextlib.suppress(KeyboardInterrupt):
        run_daemon(cfg, fake=fake or cfg.camera.fake, mic=mic)


@app.command()
def booth(
    config: Path | None = ConfigOpt,
    root: Path | None = RootOpt,
    port: int | None = typer.Option(None),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Serve the collections to the slideshow over HTTP."""
    from photobouche.booth.agent import BoothServer

    cfg = _setup(config, root, verbose)
    p = cfg.paths
    collections = {"live": p.display, "making_of": p.making_of, "clips": p.clips}
    for folder in collections.values():
        folder.mkdir(parents=True, exist_ok=True)
    server = BoothServer(collections, p.root / "index.json", cfg.booth.host, port or cfg.booth.port)
    console.print(f"booth serving {p.root} on port {server.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


@app.command()
def screen(
    profile: str = typer.Option(..., help="`booth` (control screen) or `show` (slideshow)."),
    config: Path | None = ConfigOpt,
    root: Path | None = RootOpt,
    booth_url: str | None = typer.Option(None, help="Override [show] booth_url."),
    windowed: bool = typer.Option(False, help="Not fullscreen, for working on a laptop."),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Run a screen."""
    cfg = _setup(config, root, verbose)
    if profile == "booth":
        from photobouche.screen.booth_view import run_booth_view

        cfg.booth_screen.fullscreen = not windowed
        run_booth_view(cfg)
    elif profile == "show":
        from photobouche.screen.show import Show

        if booth_url:
            cfg.show.booth_url = booth_url
        cfg.show.fullscreen = not windowed
        with contextlib.suppress(KeyboardInterrupt):
            Show(cfg).run()
    else:
        raise typer.BadParameter("profile is `booth` or `show`")


@app.command()
def shoot(config: Path | None = ConfigOpt, root: Path | None = RootOpt) -> None:
    """Fire the camera through the running `photobouche camera`."""
    import json
    import socket

    cfg = _setup(config, root)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(cfg.paths.socket))
        s.sendall((json.dumps({"cmd": "shoot"}) + "\n").encode())
    console.print("fired")


@app.command()
def prepare(
    src: Path = typer.Argument(..., help="Folder of making-of photos and clips (any depth)."),
    dest: Path = typer.Argument(Path("media/prepared"), help="Output: making_of/ and clips/."),
    transcode: bool = typer.Option(False, help="Re-encode clips the Pi may struggle with (ffmpeg)."),
) -> None:
    """Make making-of material Pi-friendly before pushing it to the booth."""
    from photobouche.media import prepare as run

    _setup(None, None)
    counts = run(src, dest, do_transcode=transcode)
    console.print(
        f"{counts['images']} images, {counts['clips']} clips written to {dest} "
        f"({counts['skipped']} up to date, {counts['ignored']} ignored, "
        f"{counts['problems']} with problems)"
    )
    console.print(f"next: deploy/push-media.sh {dest} <booth host>")


@app.command("check-stream")
def check_stream(
    config: Path | None = ConfigOpt,
    booth_url: str | None = typer.Option(None, help="Override [show] booth_url."),
    sample_mb: int = typer.Option(16, help="MB downloaded per clip."),
) -> None:
    """On the slideshow Pi: is the booth fast enough for every clip?"""
    from photobouche.media import check_stream as run

    cfg = _setup(config, None)
    url = booth_url or cfg.show.booth_url
    results = run(url, sample_mb)
    if not results:
        console.print(f"no clips on {url}")
    worst = None
    for r in results:
        detail = ""
        if r.info:
            detail = f"{r.info.codec} {r.info.width}x{r.info.height}"
            if r.info.problems:
                detail += f" [yellow]({', '.join(r.info.problems)})[/]"
        head = r.headroom
        verdict = (
            "[dim]bitrate unknown (install ffmpeg)[/]"
            if head is None
            else f"[{'green' if head >= 3 else 'yellow' if head >= 1.5 else 'red'}]x{head:.1f}[/]"
        )
        need = f"{r.needed_mbps:.1f}" if r.needed_mbps else "?"
        console.print(f"{r.name}: {r.mbps:.0f} Mb/s for {need} Mb/s {verdict}  {detail}")
        if head is not None:
            worst = head if worst is None else min(worst, head)
    if worst is not None and worst < 1.5:
        console.print("[red]some clips stream too slowly: use Ethernet, or transcode them lighter[/]")
        raise typer.Exit(1)


@app.command()
def mics() -> None:
    """List microphones, to pick one by name for [camera] mic."""
    from photobouche.audio.live import list_microphones

    for m in list_microphones():
        mark = "*" if m["default"] else " "
        console.print(f"{mark} {m['name']}  ({m['channels']} ch, {m['samplerate']} Hz)")


@app.command()
def listen(
    mic: str = typer.Argument(..., help="Microphone name."),
    trigger_db: float = typer.Option(15.0),
) -> None:
    """Show what the scream detector hears: tune trigger_db on site with this."""
    from photobouche.audio.detector import DetectorConfig
    from photobouche.audio.live import LiveAudioSource

    src = LiveAudioSource(
        mic, config=DetectorConfig(trigger_db=trigger_db, release_db=max(trigger_db - 7, 1))
    )
    with src:
        try:
            while True:
                time.sleep(0.1)
                for s in src.poll():
                    console.print(f"[bold red]SCREAM[/] {s.duration:.2f}s loudness {s.loudness:.2f}")
                d = src.detector
                bar = "#" * int(d.level * 40)
                console.print(
                    f"{bar:<40} floor {d.floor_db:6.1f} dB  now {d.last_db:6.1f} dB  "
                    f"centroid {d.last_centroid:5.0f} Hz  {'ON' if d.screaming else ''}",
                    highlight=False,
                )
        except KeyboardInterrupt:
            pass


@app.command()
def doctor(config: Path | None = ConfigOpt, root: Path | None = RootOpt) -> None:
    """Check what this Pi needs for its role."""
    cfg = _setup(config, root)
    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= passed
        console.print(f"[{'green' if passed else 'red'}]{'✓' if passed else '✗'}[/] {label}"
                      + (f"  [dim]{hint}[/]" if not passed and hint else ""))

    check("mpv (slideshow)", shutil.which("mpv") is not None, "sudo apt install mpv")
    try:
        import gphoto2  # noqa: F401

        check("python-gphoto2 (booth)", True)
        out = subprocess.run(["gphoto2", "--auto-detect"], capture_output=True, text=True)
        check("camera detected", "usb:" in out.stdout, "plug the 5D in and switch it on")
    except ImportError:
        check("python-gphoto2 (booth)", False, "pip install 'photobouche[booth]'")
    except FileNotFoundError:
        check("gphoto2 CLI", False, "sudo apt install gphoto2")
    try:
        import pygame  # noqa: F401

        check("pygame-ce (control screen)", True)
    except ImportError:
        check("pygame-ce (control screen)", False, "pip install 'photobouche[booth]'")
    if cfg.camera.mic:
        try:
            from photobouche.audio.live import resolve_microphone

            resolve_microphone(cfg.camera.mic)
            check(f"microphone {cfg.camera.mic!r}", True)
        except Exception as exc:
            check(f"microphone {cfg.camera.mic!r}", False, str(exc))
    else:
        check("microphone configured", False, "set [camera] mic (see `photobouche mics`)")
    check(f"data folder {cfg.paths.root}", cfg.paths.root.is_dir(), "create it, owned by the pi user")

    import urllib.request

    try:
        with urllib.request.urlopen(cfg.show.booth_url + "/api/health", timeout=3) as r:
            check(f"booth reachable at {cfg.show.booth_url}", r.status == 200)
    except OSError as exc:
        check(f"booth reachable at {cfg.show.booth_url}", False, str(exc))
    raise typer.Exit(0 if ok else 1)
