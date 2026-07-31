# Bossman Audio

Headphone audio control for Linux (PipeWire). Three modes, one switch, no plugin rack.

*(The CLI and internals are called `hpctl` — that's the same thing.
Bossman Audio is the project; `hpctl` is the tool it ships.)*

---

Hi, welcome to Bossman Audio — and thank you for choosing it for your
audio!

If you've come from Windows, you probably had vendor software handling
surround sound, EQ, and hi-res for you. On Linux those capabilities have
always existed, but scattered and hidden. This app brings them back with
simplicity and day-to-day use in mind: seamlessly switch between **Pure**
audio, **Custom EQ**, and **Spatial** surround for your listening
pleasure.

This is a work in progress, and development continues toward one goal: a
simple but sophisticated audio utility that anyone can use, whatever
they're listening to. Thank you for trying my program — I truly hope you
enjoy it!

*Yours truly, Bossman*

**P.S. — Please read this document before using the app.** It's short, and
a couple of things in it (especially the volume-wheel note just below)
genuinely change whether your audio works right. Five minutes here saves
an hour of confusion later.

---

| Mode | What it does | Signal path |
|---|---|---|
| **Pure** | Bit-perfect playback — nothing touches your audio | app → DAC |
| **Custom EQ** | 10-band parametric EQ, transparent float32 | app → EQ → DAC |
| **Spatial** | Real HRTF surround for games and film | app → 7.1 → HRTF → EQ → DAC |

Each mode keeps its **own EQ curve** and its **own volume level**. Switching is
instant — no device disappearing from your sound menu, no volume jumps, no
audio dropouts.

> **Status: early development.** This works and is in daily use on the
> machine it was built on, but it has been tested on exactly one system so
> far (Bazzite / GNOME / USB wireless headset). Expect sharp edges. Bug
> reports are very welcome — that's how it gets less sharp.

## ⚠️ Important: headset volume wheels/dials

**If your headset has its own volume wheel or dial: set it to maximum and
stop using it. Control volume with your desktop only** (keyboard keys,
top-bar slider, or the sound settings).

Why this matters: a headset wheel is a *second* volume stage that your
desktop cannot see. If it sits below max, it silently caps your loudness
while the on-screen slider claims 100% — the classic "Linux audio is quiet
and nothing explains why" experience. hpctl holds the hardware at full in
its processed modes precisely so the slider on screen always tells the
truth. Turning the wheel fights that (you'll hear brief dips as hpctl
corrects it). Wheel at max, desktop for volume — one control, one truth.

## Requirements

- PipeWire (with `pw-dump`, `pw-cli`, `pw-metadata`, `wpctl`, `pactl` on PATH — standard on any recent distro)
- Python 3.11+
- For the GUI: GTK4 + libadwaita Python bindings (preinstalled on GNOME distros)
- For Spatial mode:
  - the SOFA filter-chain module — on Fedora/Bazzite:
    `rpm-ostree install pipewire-module-filter-chain-sofa` then reboot
    (on non-atomic Fedora: `dnf install pipewire-module-filter-chain-sofa`)
  - an HRTF file in SOFA format — the free
    [SADIE II database](https://www.york.ac.uk/sadie-project/database.html)
    works well (D1 is a good default)

Pure and Custom EQ work without the SOFA pieces.

## Install

```bash
git clone https://github.com/bossman-audio/bossman-audio.git
cd bossman-audio
python3 hpctl3.py check          # what's present, what's missing
python3 hpctl3.py devices        # find your output's node.name
python3 hpctl3.py install --output <your-node-name> --sofa /path/to/your.sofa
```

`install` writes one PipeWire config, restarts PipeWire (brief dropout,
one time), and creates a sink called **"Headphones (Processed)"**.

Optional but recommended — the volume-pin service (enforces the wheel rule
automatically):

```bash
mkdir -p ~/.config/systemd/user
cp hpctl-sync.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hpctl-sync.service
```

## Use

**GUI:**

```bash
python3 hpctl_gui.py
```

**CLI:**

```bash
python3 hpctl3.py mode eq|spatial|pure   # switch mode
python3 hpctl3.py cycle                  # rotate modes (bind this to a hotkey!)
python3 hpctl3.py eq show                # current mode's EQ curve
python3 hpctl3.py eq 1 6                 # band 1 to +6 dB, applied live
python3 hpctl3.py level 0.7              # spatial loudness (0.1–1.0)
python3 hpctl3.py status                 # what's actually going on
python3 hpctl3.py uninstall              # remove everything, restore defaults
```

Tip: bind `python3 /path/to/hpctl/hpctl3.py cycle` to a keyboard shortcut
(GNOME Settings → Keyboard → Custom Shortcuts) for one-key mode switching
with a desktop notification.

## How your sound menu will look

- **"Headphones (Processed)"** — the EQ/Spatial sink. When this is the
  default, hpctl is in the path.
- **Your actual device** (e.g. "Analog Output — …") — Pure mode routes
  straight here. Untouched, bit-perfect.

A notification shows the mode on every switch. Mode switching between EQ
and Spatial changes nothing device-visible at all — that's by design; it's
what keeps the sound menu stable and truthful.

## Design rules this project learned the hard way

- **Switching modes never transfers volume.** Every mode keeps its own
  level. Clever cross-mode volume math produced path-dependent loudness
  and was deleted.
- **Any code touching one gain stage must account for the other.** There
  are always at least two volume stages (chain + hardware) in series.
- **Detect capabilities from your own graph**, never from what happens to
  be loaded on the system.
- **PipeWire node IDs change on every restart.** Resolve by name, always.

## Known limitations

- The mode label in the sound menu can't change per-mode (renaming a
  PipeWire sink requires rebuilding it — the exact churn this design
  avoids). A GNOME Quick Settings toggle showing the current mode is the
  planned fix.
- Spatial runs at 48 kHz (HRTF convolution is rate-fixed). Pure follows
  the source rate.
- Touching a headset wheel in processed modes causes a brief (~100 ms)
  volume dip while the pin corrects it. Rule above avoids it entirely.
- Tested on one machine. See Status.

## Roadmap

- GNOME Quick Settings extension (glanceable mode + one-tap switching)
- AutoEQ integration: fetch the correction curve for your exact headphone model
- Per-headphone profiles with hotplug detection
- Proper packaging (RPM / possibly a Bazzite `ujust` recipe)

## License

MIT — see LICENSE.
