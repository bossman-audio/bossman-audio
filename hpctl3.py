#!/usr/bin/env python3
"""
hpctl - headphone control, v0.3

Restructure. v0.2 used one sink per mode and switched the system default to
change mode. Three symptoms came out of that single decision:

  * the GNOME sound panel and taskbar showed stale state until you navigated
    away and back - they are PulseAudio clients, and a sink that vanishes and
    reappears across a PipeWire restart invalidates their cached device list
  * volume jumped on every mode change, because each sink carries its own
    level and the desktop slider only ever controls the current default
  * EQ and Spatial were mutually exclusive, being separate sinks

v0.3 uses ONE permanent sink holding both signal paths. Mode is a set of
mixer gains pushed to the live node - no config rewrite, no restart, no
default-sink change. The desktop never sees anything change, so it cannot go
stale; volume never moves because the sink never moves; and the EQ sits after
the mixer, so it applies to both paths.

Pure remains a real default-sink change. Bit-perfect means not passing
through our sink at all, so there is no way around that - and it is the one
case where the desktop *should* show a different device.

    ./hpctl3.py install [--sofa FILE]
    ./hpctl3.py status
    ./hpctl3.py mode eq | spatial | pure [--device FRAGMENT]
    ./hpctl3.py eq <freq> <gain> | show | reset
    ./hpctl3.py monitor
    ./hpctl3.py uninstall
"""

import argparse
import copy
import json
import re
import subprocess
import sys
import time
from pathlib import Path
import profiles
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import hpctl2 as core
except ImportError:
    print("error: hpctl2.py must be in the same directory", file=sys.stderr)
    sys.exit(1)


CONF = core.CONF_DIR / "hpctl-unified.conf"
SINK = "effect_input.hpctl"
SINK_OUT = "effect_output.hpctl"

MODE_EQ = "eq"
MODE_SPATIAL = "spatial"
MODE_PURE = "pure"

# Graph input order. Must match audio.position below.
CHANNELS = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]

# Virtual speaker placement for the spatial path, degrees.
# LFE is deliberately absent: it is non-directional below ~100Hz, so
# convolving it buys phase smearing and nothing else.
SPEAKERS = [
    ("FL", 30.0, 0.0), ("FR", 330.0, 0.0),
    ("FC", 0.0, 0.0),
    ("RL", 150.0, 0.0), ("RR", 210.0, 0.0),
    ("SL", 90.0, 0.0), ("SR", 270.0, 0.0),
]

# Mixer input slots on the final per-side mixer.
SLOT_DIRECT = 1     # FL (or FR) straight through
SLOT_SPATIAL = 2    # summed output of the spatial path
SLOT_CENTRE = 3
SLOT_REAR = 4
SLOT_SIDE = 5
SLOT_LFE = 6

# -0.707 = -3dB, the conventional downmix coefficient for folding
# centre/rear/side into stereo without the sum running hot.
DOWNMIX = 0.707
LFE_MIX = 0.5


# ------------------------------------------------------------------ config

def unified_config(bands, sofa_path=None, makeup=0.5, mode=MODE_EQ, target=None):
    """
    Build the whole graph: direct path, spatial path, shared EQ.

    Written once at install time. Everything after that is a control push.
    """
    nodes, links = [], []
    inputs = []

    # tee every input channel - graph inputs can only feed one filter port,
    # but a filter OUTPUT may fan out to several
    for ch in CHANNELS:
        nodes.append(f'          {{ type = builtin label = copy name = cp{ch} }}')
        inputs.append(f'"cp{ch}:In"')

    spatial = bool(sofa_path)
    if spatial:
        for name, az, el in SPEAKERS:
            nodes.append(
                f'          {{ type = sofa label = spatializer name = sp{name}\n'
                f'            config = {{ filename = "{sofa_path}" }}\n'
                f'            control = {{ "Azimuth" = {az} "Elevation" = {el} '
                f'"Radius" = 3.0 }} }}'
            )
            links.append(f'          {{ output = "cp{name}:Out" input = "sp{name}:In" }}')

        nodes.append('          { type = builtin label = mixer name = mixSpL }')
        nodes.append('          { type = builtin label = mixer name = mixSpR }')
        for i, (name, _, _) in enumerate(SPEAKERS, start=1):
            links.append(f'          {{ output = "sp{name}:Out L" input = "mixSpL:In {i}" }}')
            links.append(f'          {{ output = "sp{name}:Out R" input = "mixSpR:In {i}" }}')
        # LFE rides the spatial bus flat, in the last free slot
        lfe_slot = len(SPEAKERS) + 1
        links.append(f'          {{ output = "cpLFE:Out" input = "mixSpL:In {lfe_slot}" }}')
        links.append(f'          {{ output = "cpLFE:Out" input = "mixSpR:In {lfe_slot}" }}')

    # final per-side mixers - the mode switch lives here
    g = mode_gains(mode, makeup, spatial)
    for side in ("L", "R"):
        ctl = " ".join(
            f'"Gain {slot}" = {g[f"mix{side}:Gain {slot}"]}'
            for slot in (SLOT_DIRECT, SLOT_SPATIAL, SLOT_CENTRE,
                         SLOT_REAR, SLOT_SIDE, SLOT_LFE)
        )
        nodes.append(
            f'          {{ type = builtin label = mixer name = mix{side}\n'
            f'            control = {{ {ctl} }} }}'
        )

    links.append(f'          {{ output = "cpFL:Out" input = "mixL:In {SLOT_DIRECT}" }}')
    links.append(f'          {{ output = "cpFR:Out" input = "mixR:In {SLOT_DIRECT}" }}')
    if spatial:
        links.append(f'          {{ output = "mixSpL:Out" input = "mixL:In {SLOT_SPATIAL}" }}')
        links.append(f'          {{ output = "mixSpR:Out" input = "mixR:In {SLOT_SPATIAL}" }}')

    # 7.1 -> stereo fold for the direct path. Without this, EQ mode silently
    # drops centre, rears and LFE for anything outputting surround.
    links.append(f'          {{ output = "cpFC:Out"  input = "mixL:In {SLOT_CENTRE}" }}')
    links.append(f'          {{ output = "cpFC:Out"  input = "mixR:In {SLOT_CENTRE}" }}')
    links.append(f'          {{ output = "cpRL:Out"  input = "mixL:In {SLOT_REAR}" }}')
    links.append(f'          {{ output = "cpRR:Out"  input = "mixR:In {SLOT_REAR}" }}')
    links.append(f'          {{ output = "cpSL:Out"  input = "mixL:In {SLOT_SIDE}" }}')
    links.append(f'          {{ output = "cpSR:Out"  input = "mixR:In {SLOT_SIDE}" }}')
    links.append(f'          {{ output = "cpLFE:Out" input = "mixL:In {SLOT_LFE}" }}')
    links.append(f'          {{ output = "cpLFE:Out" input = "mixR:In {SLOT_LFE}" }}')

    # shared EQ, after the mixer so it applies to both paths
    for side in ("L", "R"):
        prev = f"mix{side}:Out"
        for i, b in enumerate(bands):
            nodes.append(
                f'          {{ type = builtin label = bq_peaking name = eq{side}{i}\n'
                f'            control = {{ "Freq" = {b["freq"]} "Q" = {b.get("q", 1.0)} '
                f'"Gain" = {b["gain"]} }} }}'
            )
            links.append(f'          {{ output = "{prev}" input = "eq{side}{i}:In" }}')
            prev = f"eq{side}{i}:Out"

    last = len(bands) - 1
    outputs = f'"eqL{last}:Out" "eqR{last}:Out"'

    # Pin the output to a named device. Left passive, the chain attaches to
    # whatever WirePlumber considers the default - and since our own sink IS
    # the default, that choice is ambiguous. On a machine with HDMI, S/PDIF
    # and a GoXLR's several loopback sinks in the list, "ambiguous" eventually
    # means the audio comes out somewhere you did not ask for.
    target_line = f'\n        target.object  = "{target}"' if target else ""

    nl = chr(10)
    return f"""# Generated by hpctl v0.3. Edits will be overwritten.
# One permanent sink. Mode is a mixer gain, not a different device.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.description = "Headphones (Processed)"
      media.name       = "Headphones (Processed)"
      filter.graph = {{
        nodes = [
{nl.join(nodes)}
        ]
        links = [
{nl.join(links)}
        ]
        inputs  = [ {" ".join(inputs)} ]
        outputs = [ {outputs} ]
      }}
      capture.props = {{
        node.name      = "{SINK}"
        media.class    = Audio/Sink
        audio.channels = 8
        audio.position = [ {" ".join(CHANNELS)} ]
      }}
      playback.props = {{
        node.name      = "{SINK_OUT}"
        node.passive   = true
        audio.channels = 2
        audio.position = [ FL FR ]{target_line}
      }}
    }}
  }}
]
"""


def mode_gains(mode, makeup=0.5, spatial_available=True):
    """Control values that define a mode. This is the whole mode switch."""
    if mode == MODE_SPATIAL and spatial_available:
        direct, spat, sur, lfe = 0.0, makeup, 0.0, 0.0
    else:  # EQ, and the fallback when no SOFA is loaded
        direct, spat, sur, lfe = 1.0, 0.0, DOWNMIX, LFE_MIX

    out = {}
    for side in ("L", "R"):
        out[f"mix{side}:Gain {SLOT_DIRECT}"] = direct
        out[f"mix{side}:Gain {SLOT_SPATIAL}"] = spat
        out[f"mix{side}:Gain {SLOT_CENTRE}"] = sur
        out[f"mix{side}:Gain {SLOT_REAR}"] = sur
        out[f"mix{side}:Gain {SLOT_SIDE}"] = sur
        out[f"mix{side}:Gain {SLOT_LFE}"] = lfe
    return out


# ------------------------------------------------------------------ control

def bands_for(st, mode=None):
    """
    The band set belonging to a mode.

    One EQ chain serves both paths, but the same curve does not suit both:
    HRTF convolution colours the sound, so a correction tuned for stereo is
    usually wrong under spatialisation. Keeping a set per mode and pushing it
    on entry gives independent curves without a second filter chain.
    """
    mode = mode or st.get("mode", MODE_EQ)
    key = MODE_SPATIAL if mode == MODE_SPATIAL else MODE_EQ
    profs = st.setdefault("eq_profiles", {})
    if key not in profs:
        legacy = st.get("bands")
        base = legacy if legacy else core.DEFAULT_BANDS
        profs[key] = copy.deepcopy(list(base))
    return profs[key]


def eq_values(bands):
    vals = {}
    for i, b in enumerate(bands):
        for side in ("L", "R"):
            vals[f"eq{side}{i}:Freq"] = b["freq"]
            vals[f"eq{side}{i}:Gain"] = b["gain"]
            vals[f"eq{side}{i}:Q"] = b.get("q", 1.0)
    return vals


def push_many(node_id, values):
    """Set several controls in one call - fewer round trips, less tearing."""
    if not values:
        return True
    parts = " ".join(f'"{k}" {v}' for k, v in values.items())
    rc, _, err = core.run(["pw-cli", "s", str(node_id), "Props",
                           f"{{ params = [ {parts} ] }}"])
    if rc != 0:
        core.warn(f"control push failed: {err.strip()}")
    return rc == 0


def get_volume(node_id):
    rc, out, _ = core.run(["wpctl", "get-volume", str(node_id)])
    if rc != 0:
        return None
    for tok in out.split():
        try:
            return float(tok)
        except ValueError:
            continue
    return None


def set_volume(node_id, vol):
    core.run(["wpctl", "set-volume", str(node_id), f"{max(0.0, min(1.0, vol)):.3f}"])


def pin_hardware_volume(objs, st, chain_id=None):
    """
    Earlier versions tried to be kind across mode switches: fold the hardware
    level into the chain on the way in, save it, restore it on the way out.
    Each rule was locally sensible and together they made loudness depend on
    the ROUTE taken between modes - the same mode came up loud or quiet based
    on where you had been before, with no input from the user. Path-dependent
    volume is worse than a one-time calibration, so all of it is gone.

    The whole model now: processed modes pin hardware to 100% and your volume
    is the chain sink; Pure leaves hardware entirely alone and your volume is
    the hardware sink. Each mode keeps its own level. Switching never
    transfers volume in either direction.
    """
    hw = st.get("hw_sink")
    if not hw:
        return None
    if not profiles.should_pin(st, hw):
        return None
    nid = core.resolve(objs, hw)
    if nid is None:
        return None
    current = get_volume(nid)
    if current is not None and current < 0.999:
        set_volume(nid, 1.0)
        return current
    return None

def sink_node_id(objs=None):
    objs = objs or core.pw_dump()
    return core.resolve(objs, SINK)


def control_node_id(objs=None):
    """The node exposing the graph controls (the capture side)."""
    objs = objs or core.pw_dump()
    nid = core.find_control_node(objs, "mixL:Gain 1")
    return nid if nid is not None else sink_node_id(objs)


def set_mode(mode, makeup=0.5, fade_ms=120, device=None):
    """
    Switch mode.

    EQ <-> Spatial never touches the default sink; it ramps mixer gains on the
    live node. Pure is the exception and does move the default, because
    bit-perfect means bypassing our sink entirely.
    """
    st = core.load_state()
    objs = core.pw_dump()

    if mode == MODE_PURE:
        hw = st.get("hw_sink")
        if device:
            hw = _match_device(objs, device)
        if not hw:
            core.die("no hardware sink remembered; pass --device")
        nid = core.resolve(objs, hw)
        if nid is None:
            core.die(f"hardware sink '{hw}' not present")
        set_default_both_layers(nid, hw)
        pure_level = st.get("pure_level")
        if pure_level is not None:
            set_volume(nid, pure_level)
        moved, how = core.migrate_streams(nid, hw, objs)
        st.pop("hw_volume_saved", None)   # legacy fold/restore residue
        st["mode"] = MODE_PURE
        core.save_state(st)
        _notify(MODE_PURE)
        return f"pure -> {hw} ({moved} stream(s) moved via {how})"

    # make sure our sink is the default before an in-graph switch
    our_id = sink_node_id(objs)
    if our_id is None:
        core.die("hpctl sink not loaded - run 'install' first")

    if core.current_default(objs) != SINK:
        core.remember_hw_sink(st, objs)
        set_default_both_layers(our_id, SINK)
        core.migrate_streams(our_id, SINK, objs)

    st.pop("hw_volume_saved", None)       # legacy fold/restore residue
    was = pin_hardware_volume(objs, st)
    if was is not None:
        # The level found on the hardware sink is the level Pure was using -
        # nothing else writes there. Remember it as Pure's own, so returning
        # to Pure lands where the listener left it instead of at the pinned
        # maximum. Per-mode memory, not cross-mode transfer: no arithmetic
        # between stages, no path dependence - the one-way street that made
        # yesterday's fold/restore machinery collapse stays closed.
        st["pure_level"] = round(was, 3)
        core.warn(f"hardware pinned to 100%; Pure will return at {was:.0%}")

    cid = control_node_id(objs)
    spatial_ok = st.get("sofa") and spatial_ready(objs)
    if mode == MODE_SPATIAL and not spatial_ok:
        core.die("spatial unavailable: no SOFA file loaded")

    target = mode_gains(mode, makeup, bool(spatial_ok))

    if fade_ms > 0:
        current = mode_gains(st.get("mode", MODE_EQ), makeup, bool(spatial_ok))
        steps = max(2, int(fade_ms / 20))
        for s in range(1, steps + 1):
            t = s / steps
            blend = {k: round(current.get(k, 0.0) * (1 - t) + v * t, 4)
                     for k, v in target.items()}
            push_many(cid, blend)
            time.sleep(0.02)
    else:
        push_many(cid, target)

    st["mode"] = mode
    push_many(cid, eq_values(bands_for(st, mode)))
    core.save_state(st)
    _notify(mode)
    return f"{mode} (in-graph, EQ curve for {mode})"


def _match_device(objs, fragment):
    frag = fragment.lower()
    for s in core.list_sinks(objs):
        if frag in s["name"].lower() or frag in s["desc"].lower():
            return s["name"]
    return None


def set_band(index, freq=None, gain=None, q=None):
    """Push one EQ band. Applies to both L and R, and to both paths."""
    st = core.load_state()
    bands = bands_for(st)
    if not 0 <= index < len(bands):
        core.die(f"band must be 0..{len(bands)-1}")
    if freq is not None:
        bands[index]["freq"] = freq
    if gain is not None:
        bands[index]["gain"] = gain
    if q is not None:
        bands[index]["q"] = q
    core.save_state(st)

    cid = control_node_id()
    if cid is None:
        return "saved (sink not loaded)"
    vals = {}
    for side in ("L", "R"):
        vals[f"eq{side}{index}:Freq"] = bands[index]["freq"]
        vals[f"eq{side}{index}:Gain"] = bands[index]["gain"]
        vals[f"eq{side}{index}:Q"] = bands[index].get("q", 1.0)
    push_many(cid, vals)
    return (f"[{st.get('mode', MODE_EQ)}] band {index}: "
            f"{bands[index]['freq']}Hz {bands[index]['gain']:+.1f}dB")


# ------------------------------------------------------------------ monitor

def monitor(on_change):
    """
    Event-driven desktop sync, replacing the 2-second poll.

    `pactl subscribe` reports changes at the PulseAudio layer - which is the
    layer GNOME's panel and the taskbar actually live on. Watching it means
    the app reacts to desktop-side changes immediately instead of up to two
    seconds later, and there is no poll racing the user's input.
    """
    proc = subprocess.Popen(
        ["pactl", "subscribe"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("Event "):
                continue
            if any(k in line for k in ("sink", "server", "change")):
                on_change(line)
    finally:
        proc.terminate()


# ------------------------------------------------------------------ commands

def spatial_ready(objs=None):
    """
    Did OUR graph's spatial path actually build?

    The old check looked for any node named "spatializer" - which matched a
    stray leftover config, not our graph. Removing that stray revealed the
    dependency: spatial "support" vanished while the spatial path kept
    working, because the check had been detecting the wrong object all
    along. The spatial mixer's gain controls only exist on the unified sink
    when its SOFA nodes loaded, so their presence is the question actually
    being asked.
    """
    objs = objs or core.pw_dump()
    return core.find_control_node(objs, "mixSpL:Gain") is not None


def cmd_install(args):
    st = core.load_state()
    if args.sofa:
        p = Path(args.sofa).expanduser().resolve()
        if not p.is_file():
            core.die(f"not a file: {p}")
        st["sofa"] = str(p)
    bands = bands_for(st, MODE_EQ)
    st.setdefault("makeup", args.makeup)

    core.CONF_DIR.mkdir(parents=True, exist_ok=True)
    target = args.output or st.get("hw_sink")
    if target:
        st["hw_sink"] = target
    CONF.write_text(unified_config(bands, st.get("sofa"),
                                   st["makeup"], st.get("mode", MODE_EQ),
                                   target))
    core.save_state(st)
    print(f"wrote {CONF}")
    print(f"output pinned to {target or '(floating - pass --output)'}")

    # Other filter-chain configs load alongside ours and quietly burn CPU.
    # Report them rather than deleting - they may not be ours to remove.
    strays = []
    for other in core.CONF_DIR.glob("*.conf"):
        if other == CONF:
            continue
        try:
            if "filter-chain" in other.read_text():
                strays.append(other.name)
        except OSError:
            continue
    if strays:
        print("\n  ! other filter-chain configs are also loaded:")
        for s in strays:
            print(f"      {core.CONF_DIR / s}")
        print("    remove them unless you want them running too")

    # remove the v0.2 per-mode configs so both do not load at once
    removed = []
    for old in core.CONF_DIR.glob(f"{core.PREFIX}*.conf"):
        if old != CONF:
            old.unlink()
            removed.append(old.name)
    if removed:
        print(f"removed v0.2 configs: {', '.join(removed)}")

    core.restart_pipewire()
    time.sleep(1.5)
    objs = core.pw_dump()
    if sink_node_id(objs) is None:
        core.die("sink did not appear - check: "
                 "journalctl --user -u pipewire -n 50")
    print("sink 'Headphones' is up")
    if st.get("sofa"):
        print("spatial:", "available" if spatial_ready(objs) else "SOFA FAILED")
    return 0


def cmd_status(args):
    objs = core.pw_dump()
    st = core.load_state()
    nid = sink_node_id(objs)
    print(f"mode           {st.get('mode', '?')}")
    print(f"sink           {'up' if nid else 'NOT LOADED'}"
          f"{f' (id {nid})' if nid else ''}")
    print(f"default sink   {core.current_default(objs)}")
    print(f"hardware sink  {st.get('hw_sink', '(not remembered)')}")
    print(f"sofa           {st.get('sofa', '(none)')}")
    if nid:
        print(f"spatial        "
              f"{'ok' if spatial_ready(objs) else 'unavailable'}")
    return 0


def cmd_mode(args):
    print(set_mode(args.name, makeup=core.load_state().get("makeup", 0.5),
                   fade_ms=0 if args.no_fade else 120, device=args.device))
    return 0


def cmd_eq(args):
    st = core.load_state()
    bands = bands_for(st)
    if args.action == "show":
        print(f"  curve for {st.get('mode', MODE_EQ)} mode")
        for i, b in enumerate(bands):
            print(f"  {i:>2}  {b['freq']:>6.0f} Hz  {b['gain']:>+6.1f} dB")
        return 0
    if args.action == "reset":
        for i in range(len(bands)):
            set_band(i, gain=0.0)
        print("all bands flat")
        return 0
    idx = int(args.action)
    print(set_band(idx, gain=args.gain))
    return 0


def cmd_cycle(args):
    """
    Rotate EQ -> Spatial -> Pure -> EQ.

    Built for a keyboard shortcut. Losing the three entries in the sound panel
    cost a genuinely convenient switcher; one keypress is a better replacement
    than the panel was, and it does not require modes to masquerade as devices.
    """
    st = core.load_state()
    order = [MODE_EQ, MODE_SPATIAL, MODE_PURE]
    if not (st.get("sofa") and spatial_ready()):
        order.remove(MODE_SPATIAL)
    try:
        nxt = order[(order.index(st.get("mode", MODE_EQ)) + 1) % len(order)]
    except ValueError:
        nxt = order[0]
    msg = set_mode(nxt, makeup=st.get("makeup", 0.5), fade_ms=120)
    print(msg)
    return 0


def cmd_level(args):
    """Tune the spatial makeup gain live, without reinstalling."""
    st = core.load_state()
    if args.value is None:
        print(f"spatial level {st.get('makeup', 0.5):.2f}")
        return 0
    val = max(0.1, min(1.0, args.value))
    st["makeup"] = val
    core.save_state(st)
    if st.get("mode") == MODE_SPATIAL:
        cid = control_node_id()
        if cid:
            push_many(cid, mode_gains(MODE_SPATIAL, val, True))
    print(f"spatial level -> {val:.2f}")
    if val > 0.7:
        core.warn("above ~0.7 an eight-channel sum can clip; "
                  "add a limiter before going higher")
    return 0


def cmd_sync(args):
    """
    Keep the headset dial and the desktop slider agreeing.

    Two gain stages sit in series: the dial (hardware sink, absolute position)
    and GNOME's slider (our chain sink). Total loudness is their product, and
    the desktop can only see one of them.

    The first attempt at this treated a dial reading as a relative gesture and
    multiplied it into the chain volume on every event. Since the reading was
    always below unity, every event shrank the volume - including events from
    turning the dial UP - and it ratcheted to nothing. The reading is an
    absolute position, so adopt it as one: the dial sets the chain volume
    outright, and the hardware goes back to unity. Monotonic with dial
    position, so up is up, and no repeated application can compound.

    Rails below are not optional. A control loop driving an audio path needs
    to fail safe whether or not the model behind it is right.
    """
    st = core.load_state()
    hw_name = st.get("hw_sink")
    if not hw_name:
        core.die("no hardware sink remembered - run a mode command first")

    objs = core.pw_dump()
    hw_id = core.resolve(objs, hw_name)
    chain_id = sink_node_id(objs)
    if hw_id is None or chain_id is None:
        core.die("could not resolve both sinks; is the chain installed?")

    FLOOR = 0.10          # never drive the chain below this
    STUCK = 5             # writes while the dial sits still = feedback
    ECHO = 0.35           # ignore the event our own write generates
    SETTLE = 2.5          # window in which a repeat reading is the device
    DEADBAND = 0.05       # wheel steps are ~12%, so this only filters noise

    last_write = [0.0]
    prev_hw = [None]
    adopted = [None]      # the wheel position we last acted on
    stuck = [0]

    if args.dry_run:
        print("DRY RUN - reporting only, changing nothing\n")
    print(f"hardware {hw_id}  chain {chain_id}")
    print("turn the headset dial; ctrl-c to stop\n")

    if not args.dry_run:
        set_volume(hw_id, 1.0)

    def on_event(line):
        # Which sink the event came from is the whole trick. Reading volumes
        # alone cannot distinguish the wheel moving from the desktop slider
        # moving, and that ambiguity is why a wheel already at its top endstop
        # could never restore volume the keyboard had taken away.
        m = re.search(r"#(\d+)", line)
        if not m or int(m.group(1)) != hw_id:
            return

        now = time.time()
        if now - last_write[0] < ECHO:
            return

        hw = get_volume(hw_id)
        chain = get_volume(chain_id)
        if hw is None or chain is None:
            return

        # Handing the hardware sink back to unity does not go unanswered: the
        # headset re-asserts its physical position shortly afterwards, past
        # the echo window. Treated as a fresh turn, that re-assertion gets
        # adopted again and the volume appears to move on its own. A repeat of
        # the position we just acted on, arriving while things are still
        # settling, is the device talking to itself.
        if (adopted[0] is not None
                and abs(hw - adopted[0]) < 0.02
                and now - last_write[0] < SETTLE):
            return

        new = max(FLOOR, min(1.0, hw))
        if abs(new - chain) < DEADBAND:
            return

        if prev_hw[0] is not None and abs(hw - prev_hw[0]) < 0.02:
            stuck[0] += 1
        else:
            stuck[0] = 0
        if stuck[0] >= STUCK:
            print(f"\n  aborting: {STUCK} writes with the dial unmoved - "
                  f"that is a feedback loop, not you")
            print(f"  restore with:  wpctl set-volume {chain_id} 1.0")
            raise KeyboardInterrupt

        print(f"  wheel {hw:.0%}  ->  volume {chain:.0%} -> {new:.0%}")
        if not args.dry_run:
            set_volume(chain_id, new)
            set_volume(hw_id, 1.0)
        prev_hw[0] = hw
        adopted[0] = hw
        last_write[0] = now

    monitor(on_event)
    return 0


_MODE_TITLES = {MODE_EQ: "Custom EQ", MODE_SPATIAL: "Spatial", MODE_PURE: "Pure"}


def _notify(mode):
    """
    Flash the mode on screen at every switch.

    The unified sink means the desktop's device label can never say which
    mode is active - node.description is fixed at sink creation, and the
    rebuild a rename requires is exactly the device churn the design exists
    to avoid. A transient notification is the honest alternative: the name
    stays stable, the switch is still visible, whoever made it.
    """
    core.run(["notify-send", "-t", "1500", "-i", "audio-headphones",
              "Headphones", _MODE_TITLES.get(mode, mode)])


def set_default_both_layers(node_id, node_name):
    """
    Change the default sink at both layers.

    wpctl talks to WirePlumber, which is authoritative for PipeWire - but
    GNOME's volume keys and sound panel are PulseAudio clients, and on some
    setups the change never reaches them: the keys keep driving the previous
    sink, which in Pure mode is a chain carrying no audio, so the volume
    appears dead. pactl generates the exact events those clients subscribe
    to. Belt and braces: set both.
    """
    core.set_default_sink(node_id)
    core.run(["pactl", "set-default-sink", node_name])


def cmd_pin(args):
    """
    Hold the hardware sink at unity so the desktop slider tells the truth.

    Two absolute gain stages sit in series - the headset wheel and our chain -
    and GNOME can only see one of them. Total loudness is their product, so
    the number on screen is only honest when the hardware stays at 1.0.

    Bidirectional sync was tried and abandoned. Adopting the wheel's position
    meant writing to the same value the wheel owns, and the two controllers
    took turns overwriting each other: a steady turn downwards produced
    readings alternating up and down, because each reset raced the device's
    re-assertion. Filters on top of that race traded one artefact for another
    without removing the cause.

    This direction has no race to lose. Nothing is read from the hardware and
    fed anywhere; the value is simply held. The wheel stops doing anything,
    which is a real cost, and the volume stops lying, which is worth more.
    """
    st = core.load_state()
    hw_name = st.get("hw_sink")
    if not hw_name:
        core.die("no hardware sink remembered - run a mode command first")

    objs = core.pw_dump()
    hw_id = core.resolve(objs, hw_name)
    if hw_id is None:
        core.die(f"hardware sink '{hw_name}' not present")

    print(f"watching hardware sink {hw_id}")
    print("pins at 100% only while the chain is in the path;")
    print("in Pure mode the hardware volume is left to you")
    print("ctrl-c to stop\n")

    if core.current_default(objs) == SINK:
        set_volume(hw_id, 1.0)
    else:
        print("  pure mode at startup - not pinning")
    last = [0.0]
    current = [hw_id]
    checked = [time.time()]
    routed = [True]
    routed_at = [0.0]

    def chain_in_path():
        """
        Is our chain actually carrying the audio?

        Pinning the hardware sink is only correct while the chain sits in
        front of it and owns the volume. In Pure mode the hardware sink IS
        the default, so the desktop slider drives it directly - and pinning
        then means every attempt to turn the volume down is immediately
        forced back to maximum. Unconditional pinning is not a tuning bug,
        it is a control fighting the user at full output.
        """
        now = time.time()
        if now - routed_at[0] < 1.0:
            return routed[0]
        try:
            routed[0] = core.current_default(core.pw_dump()) == v3_sink_name()
        except Exception:
            routed[0] = False
        routed_at[0] = now
        return routed[0]

    def v3_sink_name():
        return SINK

    def resolve_now():
        """
        Re-find the sink by name.

        A wireless headset that sleeps, drops, or gets power-cycled comes back
        under a new node id. Holding the id from startup means quietly
        watching a node that no longer exists - the pin stops working and the
        headset returns at whatever level it feels like, which is the exact
        failure this command exists to prevent. Names survive reconnects;
        ids do not.
        """
        nid = core.resolve(core.pw_dump(), hw_name)
        if nid is not None and nid != current[0]:
            print(f"  sink returned as {nid} (was {current[0]})")
            current[0] = nid
            if chain_in_path():
                set_volume(nid, 1.0)
        checked[0] = time.time()
        return nid

    def on_event(line):
        now = time.time()
        m = re.search(r"#(\d+)", line)
        if not m:
            return
        sid = int(m.group(1))

        if sid != current[0]:
            if now - checked[0] > 3.0 and get_volume(current[0]) is None:
                resolve_now()
            return

        if not chain_in_path():
            if args.verbose:
                print("  pure mode - leaving hardware volume alone")
            return

        if now - last[0] < 0.25:
            return
        v = get_volume(current[0])
        if v is None:
            resolve_now()
            return
        if v >= 0.995:
            return
        if args.verbose:
            print(f"  re-pinning ({v:.0%} -> 100%)")
        set_volume(current[0], 1.0)
        last[0] = now

    monitor(on_event)
    return 0


def cmd_monitor(args):
    print("watching desktop audio events (ctrl-c to stop)\n")
    monitor(lambda line: print(f"  {time.strftime('%H:%M:%S')}  {line}"))
    return 0


def cmd_uninstall(args):
    if CONF.exists():
        CONF.unlink()
        print(f"removed {CONF}")
    st = core.load_state()
    hw = st.get("hw_sink")
    core.restart_pipewire()
    if hw:
        time.sleep(1.0)
        objs = core.pw_dump()
        nid = core.resolve(objs, hw)
        if nid:
            core.set_default_sink(nid)
            print(f"default sink -> {hw}")
    return 0


def main():
    p = argparse.ArgumentParser(prog="hpctl3")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("install")
    i.add_argument("--sofa")
    i.add_argument("--makeup", type=float, default=0.5)
    i.add_argument("--output", help="hardware sink node.name to pin output to")
    i.set_defaults(fn=cmd_install)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    m = sub.add_parser("mode")
    m.add_argument("name", choices=[MODE_EQ, MODE_SPATIAL, MODE_PURE])
    m.add_argument("--device")
    m.add_argument("--no-fade", action="store_true")
    m.set_defaults(fn=cmd_mode)

    e = sub.add_parser("eq")
    e.add_argument("action", help="band index, 'show', or 'reset'")
    e.add_argument("gain", type=float, nargs="?")
    e.set_defaults(fn=cmd_eq)

    sub.add_parser("cycle").set_defaults(fn=cmd_cycle)

    lv = sub.add_parser("level")
    lv.add_argument("value", type=float, nargs="?")
    lv.set_defaults(fn=cmd_level)

    pn = sub.add_parser("pin")
    pn.add_argument("--verbose", action="store_true")
    pn.set_defaults(fn=cmd_pin)

    sy = sub.add_parser("sync")
    sy.add_argument("--dry-run", action="store_true",
                    help="report what it would do without changing anything")
    sy.set_defaults(fn=cmd_sync)   # deprecated: races the wheel, see cmd_pin
    sub.add_parser("monitor").set_defaults(fn=cmd_monitor)
    sub.add_parser("uninstall").set_defaults(fn=cmd_uninstall)

    args = p.parse_args()
    try:
        sys.exit(args.fn(args) or 0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
