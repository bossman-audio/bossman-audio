#!/usr/bin/env python3
"""
Device profiles for hpctl.

hpctl was built for one headset on one machine. Everything it remembers -
EQ curves, per-mode volumes, whether to pin the hardware sink - is stored
once, globally, on the quiet assumption that there is only ever one output
worth caring about.

That assumption breaks the moment you plug something in. On this laptop,
inserting a 3.5mm plug does not merely change the default sink: ALSA
switches profile and the speaker sink *ceases to exist*. The chain is left
pointed at a target that is gone, and the replacement arrives at whatever
volume it happened to be left at.

A profile is the set of things that should have been per-device all along:

  type            headphones or speakers - decides capability, not just label
  spatial_enabled HRTF only works when each ear hears one channel. Through
                  speakers both ears hear both channels, the binaural cues
                  collide, and the result is worse than untreated stereo.
                  So Spatial is not merely unhelpful on speakers, it is
                  wrong, and the profile refuses it rather than offering it.
  volume_pin      pinning hardware to 100% exists to defeat a headset's
                  physical wheel - a second gain stage the desktop cannot
                  see. Laptop speakers have no wheel, so pinning them buys
                  nothing and costs digital headroom.
  eq_profiles     one curve per mode, per device. Small speakers want a
                  very different curve from a closed-back headset, and
                  sharing one between them means neither is right.
  volumes         per-mode level memory, per device.

Nothing here touches PipeWire. This module owns the *decisions*; hpctl3
owns the graph. Keeping that line clean is what lets the profile layer be
tested without audio hardware present.
"""

import re

# Fallback used when a device has no curve of its own yet. Deliberately flat:
# a new device should sound like itself until the listener says otherwise.
FLAT_BANDS = [
    {"freq": 31, "gain": 0.0, "q": 1.0},
    {"freq": 62, "gain": 0.0, "q": 1.0},
    {"freq": 125, "gain": 0.0, "q": 1.0},
    {"freq": 250, "gain": 0.0, "q": 1.0},
    {"freq": 500, "gain": 0.0, "q": 1.0},
    {"freq": 1000, "gain": 0.0, "q": 1.0},
    {"freq": 2000, "gain": 0.0, "q": 1.0},
    {"freq": 4000, "gain": 0.0, "q": 1.0},
    {"freq": 8000, "gain": 0.0, "q": 1.0},
    {"freq": 16000, "gain": 0.0, "q": 1.0},
]

TYPE_HEADPHONES = "headphones"
TYPE_SPEAKERS = "speakers"

# Substrings that identify an output as speakers. Everything else is assumed
# to be headphones - see guess_type() for why the default leans that way.
_SPEAKER_HINTS = ("speaker", "hdmi", "displayport", "iec958", "spdif", "lineout")

# Substrings that positively identify headphones. Checked first, because
# "USB Headset Speaker" contains both and the headphone reading is correct.
_HEADPHONE_HINTS = ("headphone", "headset", "earphone", "iem")


def guess_type(node_name):
    """
    Guess whether an output is headphones or speakers from its node name.

    The guess is biased toward headphones, and that asymmetry is deliberate.
    Being wrong about speakers costs a little digital headroom, which almost
    nobody will notice. Being wrong the other way means no volume pin on a
    headset that has a wheel - which reproduces exactly the "Linux audio is
    quiet and nothing explains why" problem hpctl exists to solve.

    Cheap to correct, expensive to get backwards. So we lean.
    """
    name = (node_name or "").lower()
    for hint in _HEADPHONE_HINTS:
        if hint in name:
            return TYPE_HEADPHONES
    for hint in _SPEAKER_HINTS:
        if hint in name:
            return TYPE_SPEAKERS
    return TYPE_HEADPHONES


def label_for(node_name):
    """
    A human-readable name for a node, for notifications and the GUI.

    PipeWire node names are accurate and unreadable:
        alsa_output.pci-0000_80_1f.3-platform-skl_hda_dsp_generic.HiFi__Headphones__sink

    The useful part is nearly always the profile segment near the end, so we
    take that and tidy it. If the shape is unfamiliar we return the raw name
    rather than mangling it - a long correct label beats a short wrong one.
    """
    if not node_name:
        return "unknown output"
    m = re.search(r"\.HiFi__([A-Za-z0-9_]+?)__sink$", node_name)
    if m:
        return m.group(1).replace("_", " ").strip()
    m = re.search(r"\.([A-Za-z0-9_\-]+)$", node_name)
    if m:
        return m.group(1).replace("_", " ").replace("-", " ").strip()
    return node_name


def default_profile(node_name):
    """Build a fresh profile for a device we have never seen before."""
    kind = guess_type(node_name)
    return {
        "label": label_for(node_name),
        "type": kind,
        # Speakers cannot use HRTF at all, so the flag is not a preference
        # there - it is a statement of fact. Headphones get it enabled and
        # the SOFA check decides availability separately.
        "spatial_enabled": kind == TYPE_HEADPHONES,
        # See guess_type(): pin by default, because the failure mode of not
        # pinning is the one users cannot diagnose.
        "volume_pin": kind == TYPE_HEADPHONES,
        "eq_profiles": {},
        "volumes": {},
    }


def _devices(st):
    """The devices dict, created on first access."""
    return st.setdefault("devices", {})


def migrate(st):
    """
    Fold legacy single-device state into a profile, once.

    Before profiles, EQ curves and pure_level lived at the top level of
    state.json and implicitly belonged to whatever hw_sink happened to be.
    That is still true - we just have to say so explicitly, and attribute
    the existing settings to the device they were actually tuned for.

    The top-level keys are left in place rather than deleted. They cost
    nothing, they keep older builds of hpctl working against the same state
    file, and they serve as the fallback for a device with no profile yet.

    Returns True if anything changed, so the caller knows whether to save.
    """
    hw = st.get("hw_sink")
    if not hw:
        return False
    devs = _devices(st)
    if hw in devs:
        return False

    prof = default_profile(hw)

    legacy_eq = st.get("eq_profiles")
    if isinstance(legacy_eq, dict) and legacy_eq:
        prof["eq_profiles"] = {k: [dict(b) for b in v] for k, v in legacy_eq.items()}
    elif st.get("bands"):
        prof["eq_profiles"] = {"eq": [dict(b) for b in st["bands"]]}

    if st.get("pure_level") is not None:
        prof["volumes"]["pure"] = st["pure_level"]

    devs[hw] = prof
    return True


def get_profile(st, node_name, create=True):
    """
    The profile for a device, created on first sight if allowed.

    Returns None for an unknown device when create is False, which is how
    callers ask "have we met?" without side effects.
    """
    if not node_name:
        return None
    devs = _devices(st)
    prof = devs.get(node_name)
    if prof is None and create:
        prof = default_profile(node_name)
        devs[node_name] = prof
    return prof


def is_known(st, node_name):
    """Whether we already have a profile for this device."""
    return bool(node_name) and node_name in st.get("devices", {})


def bands_for(st, node_name, mode, fallback=None):
    """
    The EQ curve for one device in one mode.

    Falls back to the caller's supplied bands, then to flat. A device with
    no curve yet should sound like itself, not like the last thing plugged in.
    """
    prof = get_profile(st, node_name)
    if prof:
        bands = prof.get("eq_profiles", {}).get(mode)
        if bands:
            return [dict(b) for b in bands]
    if fallback:
        return [dict(b) for b in fallback]
    return [dict(b) for b in FLAT_BANDS]


def set_bands(st, node_name, mode, bands):
    """Store the EQ curve for one device in one mode."""
    prof = get_profile(st, node_name)
    if prof is None:
        return
    prof.setdefault("eq_profiles", {})[mode] = [dict(b) for b in bands]


def volume_for(st, node_name, mode, default=None):
    """Remembered level for one device in one mode, or default if none."""
    prof = get_profile(st, node_name)
    if prof:
        vol = prof.get("volumes", {}).get(mode)
        if vol is not None:
            return vol
    return default


def set_volume_for(st, node_name, mode, vol):
    """Remember a level for one device in one mode."""
    prof = get_profile(st, node_name)
    if prof is None:
        return
    prof.setdefault("volumes", {})[mode] = round(float(vol), 3)


def should_pin(st, node_name):
    """
    Whether to hold this device's hardware volume at 100%.

    True for a headset with a wheel the desktop cannot see. False for
    laptop speakers, where pinning means the app attenuates digitally and
    throws away headroom for no benefit.
    """
    prof = get_profile(st, node_name)
    return bool(prof and prof.get("volume_pin", True))


def spatial_allowed(st, node_name):
    """
    Whether Spatial may be offered on this device at all.

    Distinct from whether Spatial is *available* - that also needs a SOFA
    file and a built graph, which hpctl3 checks. This answers the prior
    question of whether it would even be correct here.
    """
    prof = get_profile(st, node_name)
    return bool(prof and prof.get("spatial_enabled", True))


def active_device(st):
    """The device profiles are currently applied to."""
    return st.get("active_device") or st.get("hw_sink") or ""


def set_active_device(st, node_name):
    """
    Record which device is now active.

    Returns True if this is a change, so the caller knows whether to
    reapply EQ, volume and pin behaviour.
    """
    if not node_name or st.get("active_device") == node_name:
        return False
    st["active_device"] = node_name
    return True


def describe(st, node_name):
    """One line for a notification when a device becomes active."""
    prof = get_profile(st, node_name, create=False)
    if prof is None:
        guessed = guess_type(node_name)
        return f"{label_for(node_name)} - new device, treating as {guessed}"
    return f"{prof.get('label') or label_for(node_name)} ({prof.get('type', '?')})"


def summary(st):
    """Every known device, for `hpctl3.py profile list` and the GUI."""
    out = []
    active = active_device(st)
    for name, prof in sorted(st.get("devices", {}).items()):
        out.append({
            "node": name,
            "label": prof.get("label") or label_for(name),
            "type": prof.get("type", "?"),
            "spatial_enabled": prof.get("spatial_enabled", True),
            "volume_pin": prof.get("volume_pin", True),
            "modes_tuned": sorted(prof.get("eq_profiles", {}).keys()),
            "active": name == active,
        })
    return out
