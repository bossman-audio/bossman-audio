#!/usr/bin/env python3
"""
hpctl - headphone control, v0.2

Changes from v0.1, all found by running it on real hardware:

  * mode pure no longer guesses. It remembers the hardware sink you were on
    before switching into a processed mode, and refuses rather than picking
    an arbitrary device off the front of the list. (v0.1 sent audio to HDMI.)
  * Everything is keyed on node.name, not node id. IDs change across restarts;
    names don't.
  * Rate parsing separates Playback from Capture. v0.1 merged them and
    reported a headset mic's 16kHz as a playback capability.
  * SOFA support is detected empirically - write config, look for the node -
    rather than by probing hardcoded plugin paths. Fedora compiles it into
    the main filter-chain module, so path probing gave a false negative.
  * Stream migration tries wpctl move, then falls back to pw-metadata.
  * Devices with duplicate descriptions are disambiguated in listings.

    ./hpctl.py status
    ./hpctl.py devices
    ./hpctl.py mode pure [--device <name-fragment>]
    ./hpctl.py mode eq | spatial
    ./hpctl.py eq <freq> <gain> | show | reset
    ./hpctl.py install [--sofa FILE] [--makeup 0.5]
    ./hpctl.py uninstall
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONF_DIR = Path.home() / ".config" / "pipewire" / "pipewire.conf.d"
STATE_FILE = Path.home() / ".config" / "hpctl" / "state.json"
PREFIX = "hpctl-"

EQ_SINK = "effect_input.hpctl_eq"
SPATIAL_SINK = "effect_input.hpctl_spatial"
OUR_SINKS = {EQ_SINK, SPATIAL_SINK}

DEFAULT_BANDS = [
    {"freq": f, "gain": 0.0, "q": 1.0}
    for f in (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
]


# ---------------------------------------------------------------- utils

def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


# ---------------------------------------------------------------- pipewire

def pw_dump():
    rc, out, err = run(["pw-dump"])
    if rc != 0:
        die(f"pw-dump failed ({err.strip()}) - is PipeWire running?")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        die("could not parse pw-dump output")


def nodes_by_name(objs):
    """Map node.name -> id. Names are stable across restarts; ids are not."""
    out = {}
    for o in objs:
        props = (o.get("info") or {}).get("props") or {}
        n = props.get("node.name")
        if n:
            out[n] = o["id"]
    return out


def resolve(objs, node_name):
    return nodes_by_name(objs).get(node_name)


def find_control_node(objs, control_name):
    """
    Locate the node exposing a named filter control.

    The controls sit deeper in the Props array than an indexed lookup finds,
    so this searches the serialised object. Slower but correct, and it runs
    once per operation rather than in a hot path.
    """
    for o in objs:
        try:
            if control_name in json.dumps(o):
                return o["id"]
        except (TypeError, ValueError):
            continue
    return None


def list_sinks(objs, include_ours=False):
    sinks = []
    for o in objs:
        props = (o.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name", "")
        if not include_ours and name in OUR_SINKS:
            continue
        sinks.append({
            "id": o["id"],
            "name": name,
            "desc": props.get("node.description", name),
            "hardware": name.startswith("alsa_output."),
        })
    return sinks


def disambiguate(sinks):
    """Append a distinguishing fragment where descriptions collide."""
    seen = {}
    for s in sinks:
        seen.setdefault(s["desc"], []).append(s)
    for desc, group in seen.items():
        if len(group) < 2:
            continue
        for s in group:
            m = re.search(r"(usb-[^.]*?)(?:-\d\d)?\.", s["name"])
            hint = m.group(1)[-14:] if m else s["name"][-14:]
            s["desc"] = f"{desc}  ({hint})"
    return sinks


def current_default(objs):
    """Return the node.name of the current default sink, via pw metadata."""
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Metadata":
            continue
        if (o.get("props") or {}).get("metadata.name") != "default":
            continue
        for entry in o.get("metadata") or []:
            if entry.get("key") == "default.audio.sink":
                v = entry.get("value")
                if isinstance(v, dict):
                    return v.get("name")
                if isinstance(v, str):
                    try:
                        return json.loads(v).get("name")
                    except (json.JSONDecodeError, AttributeError):
                        return v
    return None


def set_default_sink(node_id):
    rc, _, err = run(["wpctl", "set-default", str(node_id)])
    if rc != 0:
        die(f"could not set default sink: {err.strip()}")


def migrate_streams(target_id, target_name, objs):
    """
    Move already-playing streams to the new sink.

    Setting the default only affects streams created afterwards. Without this
    a mode switch strands existing audio on the old sink, which presents to
    the user as 'surround only works in some apps'.
    """
    streams = []
    for o in objs:
        props = (o.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        if props.get("node.name", "").startswith("effect_output."):
            continue
        streams.append(o["id"])

    if not streams:
        return 0, "none playing"

    moved = 0
    method = "wpctl"
    for sid in streams:
        rc, _, _ = run(["wpctl", "move", str(sid), str(target_id)])
        if rc == 0:
            moved += 1

    if moved == 0:
        # wpctl move is not present on every WirePlumber version; the metadata
        # route works wherever pw-metadata does.
        method = "pw-metadata"
        for sid in streams:
            rc, _, _ = run(["pw-metadata", str(sid),
                            "target.object", f'"{target_name}"'])
            if rc == 0:
                moved += 1

    return moved, method


def sofa_works(objs=None):
    """Empirical: is a sofa spatializer node actually alive?"""
    objs = objs or pw_dump()
    for o in objs:
        props = (o.get("info") or {}).get("props") or {}
        if "spatializer" in (props.get("node.name") or ""):
            return True
    return False


# ---------------------------------------------------------------- hardware

def device_capabilities():
    """
    Parse ALSA stream descriptors, keeping Playback and Capture separate.

    v0.1 merged them, so a headset whose mic runs at 16kHz appeared to have
    16kHz as a playback rate. Section tracking fixes that.
    """
    devices = []
    for path in sorted(Path("/proc/asound").glob("card*/stream0")):
        try:
            text = path.read_text()
        except OSError:
            continue
        if not text.strip():
            continue

        name = text.splitlines()[0].split(" at ")[0].strip()
        section = None
        play_rates, play_bits = set(), set()

        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("Playback:"):
                section = "play"
                continue
            if line.startswith("Capture:"):
                section = "capture"
                continue
            if section != "play":
                continue
            if line.startswith("Rates:"):
                for r in line.split(":", 1)[1].split(","):
                    r = r.strip()
                    if r.isdigit():
                        play_rates.add(int(r))
            elif line.startswith("Bits:"):
                b = line.split(":", 1)[1].strip()
                if b.isdigit():
                    play_bits.add(int(b))

        devices.append({
            "name": name,
            "rates": sorted(play_rates),
            "bits": sorted(play_bits),
        })
    return devices


# ---------------------------------------------------------------- config gen

def eq_config(bands):
    nodes, links = [], []
    for i, b in enumerate(bands):
        nodes.append(
            f'          {{ type = builtin name = band_{i} label = bq_peaking\n'
            f'            control = {{ "Freq" = {b["freq"]} '
            f'"Q" = {b["q"]} "Gain" = {b["gain"]} }} }}'
        )
        if i:
            links.append(
                f'          {{ output = "band_{i-1}:Out" input = "band_{i}:In" }}'
            )
    nl = chr(10)
    return f"""# Generated by hpctl. Edits will be overwritten.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.description = "Headphones (EQ)"
      media.name       = "Headphones (EQ)"
      filter.graph = {{
        nodes = [
{nl.join(nodes)}
        ]
        links = [
{nl.join(links)}
        ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.name   = "{EQ_SINK}"
        media.class = Audio/Sink
      }}
      playback.props = {{
        node.name    = "effect_output.hpctl_eq"
        node.passive = true
      }}
    }}
  }}
]
"""


def spatial_config(sofa_path, makeup=0.5, spatial_lfe=False):
    """
    7.1 -> binaural.

    makeup: the common 0.255 (-12dB) is worst-case protection against eight
    channels summing hot, and it is the main reason virtual surround sounds
    thin next to plain stereo. 0.5 (-6dB) is a starting point; tune by ear and
    add a limiter before going higher.

    spatial_lfe: LFE is non-directional below ~100Hz, so convolving it adds
    phase smearing for no perceptual gain. Default is to mix it flat.
    """
    positions = [
        ("spFL", 30.0, 0.0), ("spFR", 330.0, 0.0),
        ("spFC", 0.0, 0.0), ("spLFE", 0.0, -60.0),
        ("spRL", 150.0, 0.0), ("spRR", 210.0, 0.0),
        ("spSL", 90.0, 0.0), ("spSR", 270.0, 0.0),
    ]
    nodes, links, inputs = [], [], []
    slot = 1
    for name, az, el in positions:
        if name == "spLFE" and not spatial_lfe:
            nodes.append(f'          {{ type = builtin label = copy name = {name} }}')
            links.append(f'          {{ output = "{name}:Out" input = "mixL:In {slot}" }}')
            links.append(f'          {{ output = "{name}:Out" input = "mixR:In {slot}" }}')
            inputs.append(f'"{name}:In"')
        else:
            nodes.append(
                f'          {{ type = sofa label = spatializer name = {name}\n'
                f'            config = {{ filename = "{sofa_path}" }}\n'
                f'            control = {{ "Azimuth" = {az} "Elevation" = {el} '
                f'"Radius" = 3.0 }} }}'
            )
            links.append(f'          {{ output = "{name}:Out L" input = "mixL:In {slot}" }}')
            links.append(f'          {{ output = "{name}:Out R" input = "mixR:In {slot}" }}')
            inputs.append(f'"{name}:In"')
        slot += 1

    nl = chr(10)
    return f"""# Generated by hpctl. Edits will be overwritten.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.description = "Headphones (Spatial)"
      media.name       = "Headphones (Spatial)"
      filter.graph = {{
        nodes = [
{nl.join(nodes)}
          {{ type = builtin label = mixer name = mixL }}
          {{ type = builtin label = mixer name = mixR }}
        ]
        links = [
{nl.join(links)}
        ]
        inputs  = [ {" ".join(inputs)} ]
        outputs = [ "mixL:Out" "mixR:Out" ]
      }}
      capture.props = {{
        node.name      = "{SPATIAL_SINK}"
        media.class    = Audio/Sink
        audio.channels = 8
        audio.position = [ FL FR FC LFE RL RR SL SR ]
      }}
      playback.props = {{
        node.name      = "effect_output.hpctl_spatial"
        node.passive   = true
        audio.channels = 2
        audio.position = [ FL FR ]
        channelmix.max-volume = {makeup}
      }}
    }}
  }}
]
"""


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            s.setdefault("bands", [dict(b) for b in DEFAULT_BANDS])
            s.setdefault("mode", "pure")
            s.setdefault("hw_sink", "")
            s.setdefault("sofa", "")
            import profiles
            if profiles.migrate(s):
                save_state(s)
            return s
        except (json.JSONDecodeError, OSError):
            pass
    return {"mode": "pure", "bands": [dict(b) for b in DEFAULT_BANDS],
            "sofa": "", "hw_sink": ""}


def save_state(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2))


def restart_pipewire():
    run(["systemctl", "--user", "restart",
         "pipewire", "pipewire-pulse", "wireplumber"], timeout=20)
    time.sleep(2)


def remember_hw_sink(st, objs):
    """
    Record the hardware sink currently in use, so pure mode can return to it.

    This is the fix for v0.1's worst bug: without it, pure mode fell back to
    the first sink in the list, which on a machine with HDMI audio meant the
    monitor.
    """
    cur = current_default(objs)
    if cur and cur not in OUR_SINKS and cur.startswith("alsa_output."):
        st["hw_sink"] = cur
    return st


# ---------------------------------------------------------------- commands

def cmd_status(args):
    st = load_state()
    objs = pw_dump()
    cur = current_default(objs)

    print(f"mode:      {st['mode']}")
    print(f"output:    {cur or 'unknown'}")
    print(f"remembered:{st['hw_sink'] or ' (none yet)'}")
    print(f"sofa:      {'working' if sofa_works(objs) else 'not detected'}")
    print()
    print("playback capabilities:")
    for d in device_capabilities():
        if not d["rates"]:
            continue
        rs = ", ".join(f"{r/1000:g}k" for r in d["rates"])
        bs = "/".join(str(b) for b in d["bits"]) or "?"
        print(f"  {d['name']}")
        print(f"    rates: {rs}   bits: {bs}")
        if 44100 not in d["rates"]:
            print("    note: no 44.1kHz - most music will be resampled "
                  "(hardware limit)")
    print()
    confs = sorted(CONF_DIR.glob(f"{PREFIX}*.conf"))
    print("configs:   " + (", ".join(c.name for c in confs) if confs else "none"))


def cmd_devices(args):
    objs = pw_dump()
    sinks = disambiguate(list_sinks(objs, include_ours=True))
    for s in sinks:
        tag = "hw " if s["hardware"] else "   "
        star = "*" if s["name"] == current_default(objs) else " "
        print(f" {star}{tag}{s['desc']}")
        print(f"      {s['name']}")


def cmd_install(args):
    for tool in ("pw-dump", "pw-cli", "wpctl"):
        if not shutil.which(tool):
            die(f"missing '{tool}'")

    st = load_state()
    objs = pw_dump()
    st = remember_hw_sink(st, objs)
    CONF_DIR.mkdir(parents=True, exist_ok=True)

    (CONF_DIR / f"{PREFIX}eq.conf").write_text(eq_config(st["bands"]))
    print(f"wrote {PREFIX}eq.conf")

    if args.sofa:
        sofa = os.path.abspath(os.path.expanduser(args.sofa))
        if not os.path.exists(sofa):
            die(f"sofa file not found: {sofa}")
        st["sofa"] = sofa
        (CONF_DIR / f"{PREFIX}spatial.conf").write_text(
            spatial_config(sofa, makeup=args.makeup))
        print(f"wrote {PREFIX}spatial.conf  (makeup gain {args.makeup})")

    save_state(st)
    restart_pipewire()

    # Empirical verification - the only check that survives packaging differences.
    objs = pw_dump()
    ok_eq = resolve(objs, EQ_SINK) is not None
    print(f"  eq sink:      {'loaded' if ok_eq else 'FAILED TO LOAD'}")
    if args.sofa:
        ok_sp = resolve(objs, SPATIAL_SINK) is not None
        print(f"  spatial sink: {'loaded' if ok_sp else 'FAILED TO LOAD'}")
        if not ok_sp:
            warn("check: journalctl --user -u pipewire -n 30 --no-pager")


def cmd_uninstall(args):
    st = load_state()
    objs = pw_dump()

    # Return to hardware before removing the sinks out from under the user.
    if st.get("hw_sink"):
        hid = resolve(objs, st["hw_sink"])
        if hid:
            set_default_sink(hid)
            print(f"restored {st['hw_sink']}")

    n = 0
    for f in CONF_DIR.glob(f"{PREFIX}*.conf"):
        f.unlink()
        print(f"removed {f.name}")
        n += 1
    if n:
        restart_pipewire()
    st["mode"] = "pure"
    save_state(st)


def cmd_mode(args):
    st = load_state()
    objs = pw_dump()

    if args.name != "pure":
        st = remember_hw_sink(st, objs)

    if args.name == "pure":
        target_name = None
        if args.device:
            cands = [s for s in list_sinks(objs)
                     if s["hardware"] and args.device.lower() in
                     (s["desc"] + s["name"]).lower()]
            if len(cands) > 1:
                print("ambiguous - matches:")
                for c in disambiguate(cands):
                    print(f"   {c['desc']}")
                die("be more specific")
            if not cands:
                die(f"no hardware sink matching '{args.device}'")
            target_name = cands[0]["name"]
        elif st.get("hw_sink"):
            target_name = st["hw_sink"]
        else:
            hw = [s for s in list_sinks(objs) if s["hardware"]]
            print("no remembered device. available hardware outputs:")
            for s in disambiguate(hw):
                print(f"   {s['desc']}")
            die("pick one with: mode pure --device <fragment>")

        target = resolve(objs, target_name)
        if target is None:
            die(f"remembered device '{target_name}' is not present")
    else:
        sink = EQ_SINK if args.name == "eq" else SPATIAL_SINK
        target = resolve(objs, sink)
        target_name = sink
        if target is None:
            die(f"{args.name} sink not loaded - run install first")

    set_default_sink(target)
    moved, method = migrate_streams(target, target_name, objs)
    st["mode"] = args.name
    save_state(st)

    print(f"mode: {args.name} -> {target_name}")
    print(f"      streams moved: {moved} ({method})")
    if args.name == "spatial":
        print("      48kHz, processed - not bit-perfect")
    if args.name == "pure":
        caps = device_capabilities()
        for d in caps:
            if d["rates"] and 44100 not in d["rates"]:
                print("      note: device lacks 44.1kHz; music will resample")
                break


def cmd_eq(args):
    st = load_state()

    if args.freq == "show":
        for b in st["bands"]:
            n = int(abs(b["gain"]) * 2)
            bar = ("+" if b["gain"] >= 0 else "-") * n
            print(f"  {b['freq']:>6} Hz  {b['gain']:+5.1f} dB  {bar}")
        return

    if args.freq == "reset":
        st["bands"] = [dict(b) for b in DEFAULT_BANDS]
        save_state(st)
        objs = pw_dump()
        node = find_control_node(objs, "band_0:Gain")
        if node:
            for i in range(len(st["bands"])):
                push(node, f"band_{i}:Gain", 0.0)
        print("flat")
        return

    try:
        freq = int(args.freq)
        gain = float(args.gain)
    except (TypeError, ValueError):
        die("usage: eq <freq> <gain> | eq show | eq reset")

    idx = min(range(len(st["bands"])),
              key=lambda i: abs(st["bands"][i]["freq"] - freq))
    st["bands"][idx]["gain"] = max(-24.0, min(24.0, gain))
    save_state(st)

    objs = pw_dump()
    node = find_control_node(objs, f"band_{idx}:Gain")
    if node is None:
        die("eq chain not loaded - run install, then mode eq")
    push(node, f"band_{idx}:Gain", st["bands"][idx]["gain"])
    print(f"  {st['bands'][idx]['freq']} Hz -> "
          f"{st['bands'][idx]['gain']:+.1f} dB")


def push(node_id, control, value):
    rc, _, err = run(["pw-cli", "s", str(node_id), "Props",
                      f'{{ params = [ "{control}" {value} ] }}'])
    if rc != 0:
        die(f"could not set {control}: {err.strip()}")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="hpctl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("devices").set_defaults(fn=cmd_devices)

    p = sub.add_parser("install")
    p.add_argument("--sofa")
    p.add_argument("--makeup", type=float, default=0.5,
                   help="spatial output gain ceiling (default 0.5)")
    p.set_defaults(fn=cmd_install)

    sub.add_parser("uninstall").set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("mode")
    p.add_argument("name", choices=["pure", "eq", "spatial"])
    p.add_argument("--device", help="hardware sink name fragment (pure mode)")
    p.set_defaults(fn=cmd_mode)

    p = sub.add_parser("eq")
    p.add_argument("freq")
    p.add_argument("gain", nargs="?")
    p.set_defaults(fn=cmd_eq)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
