#!/usr/bin/env python3
"""
hpctl-gui - GTK4/libadwaita front end

Requires hpctl2.py in the same directory; all PipeWire logic lives there and
this file is only presentation. That separation is deliberate - the CLI stays
the testable surface, and the GUI is a thin skin over it.

    python3 hpctl_gui.py

Needs python3-gobject, gtk4, libadwaita.
"""

import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib, Gio
except (ImportError, ValueError) as e:
    print(f"error: missing GTK4/libadwaita bindings ({e})", file=sys.stderr)
    print("Fedora/Bazzite: rpm-ostree install python3-gobject gtk4 libadwaita",
          file=sys.stderr)
    print("or run inside a toolbox: toolbox create; toolbox enter", file=sys.stderr)
    sys.exit(1)

try:
    import hpctl2 as core
except ImportError:
    print("error: hpctl2.py must be in the same directory", file=sys.stderr)
    sys.exit(1)

try:
    import hpctl3 as v3
except ImportError:
    print("error: hpctl3.py must be in the same directory", file=sys.stderr)
    sys.exit(1)

# v0.3 keeps one permanent sink and expresses mode as mixer gains, so the
# questions this UI used to ask - which sink is default, does the EQ sink
# exist - no longer describe reality. These helpers answer the equivalent
# question against the new graph.

def chain_present(objs):
    return core.resolve(objs, v3.SINK) is not None


def eq_controls(objs, index):
    """Control names for one band. The EQ is per-side now, and shared by
    both the direct and spatial paths."""
    node = core.find_control_node(objs, f"eqL{index}:Gain")
    if node is None:
        return None, []
    return node, [f"eqL{index}:Gain", f"eqR{index}:Gain"]


# ---------------------------------------------------------------- heuristics

# Sinks that are almost never what a user means by "my headphones".
# The GoXLR case is instructive: its mixer channels are indistinguishable from
# real outputs at the ALSA level, so they have to be recognised by name.
DEPRIORITISE = (
    "hdmi", "spdif", "iec958", "displayport",
    "goxlr", "line1", "line2", "line3", "line4", "line5",
    "monitor", "null", "dummy",
)

HEADPHONE_HINTS = ("headphone", "headset", "hd ", "hifiman", "sennheiser",
                   "beyerdynamic", "audeze", "turtle beach", "steelseries",
                   "hyperx", "logitech", "corsair", "astro", "arctis")


def score_sink(sink):
    """
    Rank an output by how likely it is to be the user's headphones.

    Returns (score, reason). Higher is better; negative means hide by default.
    This is unavoidably heuristic - there is no reliable way to ask PipeWire
    'is this a headphone' - so the UI must always allow overriding it.
    """
    name = (sink["name"] + " " + sink["desc"]).lower()
    score, reasons = 0, []

    if not sink["hardware"]:
        return -100, "virtual"

    for bad in DEPRIORITISE:
        if bad in name:
            return -50, "not a headphone output"

    if "usb" in name:
        score += 30
        reasons.append("usb audio")
    if any(h in name for h in HEADPHONE_HINTS):
        score += 40
        reasons.append("headphone device")
    if "analog-stereo" in name or "analog_stereo" in name:
        score += 20
        reasons.append("stereo analog")
    if "pci" in name:
        score -= 10

    return score, ", ".join(reasons) or "generic output"


def block_scroll(widget):
    """
    Stop the scroll wheel from changing a slider.

    Sliders inside a scrolling list are a trap: the wheel gesture the user
    means as 'scroll the page' lands on whichever control is under the pointer.
    A capture-phase controller sees the event before the scale does and stops
    it, so the wheel scrolls the page as intended and the value only changes
    on a deliberate drag or arrow key.
    """
    ctrl = Gtk.EventControllerScroll.new(
        Gtk.EventControllerScrollFlags.BOTH_AXES)
    ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    ctrl.connect("scroll", lambda *_: True)  # True = stop here
    widget.add_controller(ctrl)
    return widget


def get_volume(node_id):
    """Read a sink's volume as a float, or None."""
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
    vol = max(0.0, min(1.5, vol))
    core.run(["wpctl", "set-volume", str(node_id), f"{vol:.3f}"])


def carry_volume(from_id, to_id):
    """
    Copy volume from the outgoing sink to the incoming one.

    Every sink keeps its own level, and the desktop slider only ever controls
    the current default. Without this, switching modes jumps the volume to
    whatever that sink was last left at - which reads as the app randomly
    changing loudness, and also quietly invalidates any A/B comparison the
    user thinks they are making.
    """
    if from_id is None or to_id is None or from_id == to_id:
        return
    v = get_volume(from_id)
    if v is not None:
        set_volume(to_id, v)


def rank_sinks(sinks):
    scored = []
    for s in sinks:
        sc, why = score_sink(s)
        s = dict(s)
        s["score"], s["why"] = sc, why
        scored.append(s)
    scored.sort(key=lambda x: -x["score"])
    return scored


# ---------------------------------------------------------------- window

class Window(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title="Headphones",
                         default_width=520, default_height=760)

        self.state = core.load_state()
        self.sinks = []
        self.show_all_devices = False
        self._updating = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh)
        toolbar.add_top_bar(header)

        self.banner = Adw.Banner(revealed=False)
        toolbar.add_top_bar(self.banner)

        self.page = Adw.PreferencesPage()
        toolbar.set_content(self.page)
        self.set_content(toolbar)

        self._build_modes()
        self._build_status()
        self._build_device()
        self._build_eq()
        self._build_spatial()

        self.refresh()

        # The default sink can change from outside this window - GNOME's Sound
        # panel, the top bar, another tool. Poll cheaply so the UI does not sit
        # there showing a mode the system left behind.
        self._last_seen = None
        self._last_volume = None
        GLib.timeout_add_seconds(2, self._poll)

    def _poll(self):
        if not self.is_visible():
            return True

        def work():
            objs = core.pw_dump()
            # Fingerprint everything the window displays, not just the default
            # sink - otherwise a device appearing, a chain loading, or a sink
            # vanishing leaves the UI showing something that is no longer true.
            sinks = tuple(sorted(s["name"] for s in core.list_sinks(objs)))
            cur = core.current_default(objs)
            return (
                cur,
                sinks,
                chain_present(objs),
                chain_present(objs) and core.sofa_works(objs),
                core.resolve(objs, cur) if cur else None,
            )

        def done(fingerprint):
            if fingerprint != self._last_seen:
                prev = self._last_seen[0] if self._last_seen else None
                new = fingerprint[0]
                # A switch made from GNOME's panel or the top bar never goes
                # through our own code, so nothing carried the level over and
                # the user gets a loudness jump. Apply the last known volume
                # to whatever the system moved to.
                if prev and new and prev != new and self._last_volume is not None:
                    objs = core.pw_dump()
                    nid = core.resolve(objs, new)
                    if nid is not None:
                        set_volume(nid, self._last_volume)
                self._last_seen = fingerprint
                self.refresh()
            # Track the current level so the next switch has something to carry.
            cur_id = fingerprint[4]
            if cur_id is not None:
                v = get_volume(cur_id)
                if v is not None:
                    self._last_volume = v

        threading.Thread(
            target=lambda: self._poll_thread(work, done), daemon=True).start()
        return True

    def _poll_thread(self, work, done):
        try:
            result = work()
        except Exception:  # noqa: BLE001 - polling must never raise
            return
        GLib.idle_add(lambda: (done(result), False)[1])

    # ------------------------------------------------------------ modes

    def _build_modes(self):
        g = Adw.PreferencesGroup(
            title="Mode",
            description="Processing changes the signal. Pure does not.")
        self.mode_rows = {}
        self.mode_checks = {}

        specs = [
            ("pure", "Pure", "Untouched audio, straight to the device"),
            ("eq", "Custom EQ", "Parametric equaliser, transparent processing"),
            ("spatial", "Spatial", "Surround via HRTF — 48 kHz, processed"),
        ]
        first = None
        for key, title, sub in specs:
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            check = Gtk.CheckButton()
            if first is None:
                first = check
            else:
                check.set_group(first)
            check.connect("toggled", self._on_mode_toggled, key)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            g.add(row)
            self.mode_rows[key] = row
            self.mode_checks[key] = check

        self.page.add(g)

    def _on_mode_toggled(self, check, key):
        if self._updating or not check.get_active():
            return
        self._run(lambda: self._switch(key))

    def _switch(self, key):
        """
        Hand the switch to v0.3.

        The old path picked a sink per mode and moved the system default. That
        is what made the desktop panel go stale and the volume jump, so none of
        it survives: EQ and Spatial are now gain changes inside one permanent
        sink, and only Pure moves the default. carry_volume is gone with it -
        there is no longer a second sink to carry a level to.
        """
        objs = core.pw_dump()
        if key != "pure" and not chain_present(objs):
            raise RuntimeError("Audio chain not loaded — run "
                               "'python3 hpctl3.py install' first.")

        if key == "pure" and not self.state.get("hw_sink"):
            ranked = rank_sinks(core.list_sinks(objs))
            if not ranked or ranked[0]["score"] < 0:
                raise RuntimeError("No suitable output found. "
                                   "Pick one under Output Device.")
            self.state["hw_sink"] = ranked[0]["name"]
            core.save_state(self.state)

        msg = v3.set_mode(key, makeup=self.state.get("makeup", 0.5))
        self.state = core.load_state()

        if key in ("eq", "spatial"):
            # The EQ now sits after the mixer, so it applies in Spatial too.
            # Re-push on entry because sliders moved while the chain was out
            # of the path were saved but never sent.
            self._resync_eq()

        return msg

    def _resync_eq(self):
        objs = core.pw_dump()
        node, _ = eq_controls(objs, 0)
        if node is None:
            return
        vals = {}
        for i, b in enumerate(v3.bands_for(self.state)):
            vals[f"eqL{i}:Gain"] = b["gain"]
            vals[f"eqR{i}:Gain"] = b["gain"]
        v3.push_many(node, vals)

    # ------------------------------------------------------------ status

    def _build_status(self):
        g = Adw.PreferencesGroup(title="Now Playing Through")
        self.row_output = Adw.ActionRow(title="Output", subtitle="—")
        self.row_format = Adw.ActionRow(title="Format", subtitle="—")
        self.row_purity = Adw.ActionRow(title="Signal", subtitle="—")
        for r in (self.row_output, self.row_format, self.row_purity):
            g.add(r)
        self.page.add(g)

    # ------------------------------------------------------------ device

    def _build_device(self):
        self.dev_group = Adw.PreferencesGroup(
            title="Output Device",
            description="Where audio goes in Pure mode")

        self.dev_expander = Adw.ExpanderRow(
            title="Available outputs", subtitle="—")
        self.dev_group.add(self.dev_expander)

        toggle_row = Adw.ActionRow(
            title="Show all outputs",
            subtitle="Includes HDMI, S/PDIF and mixer channels")
        self.sw_all = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.sw_all.connect("state-set", self._on_show_all)
        toggle_row.add_suffix(self.sw_all)
        self.dev_group.add(toggle_row)

        self.page.add(self.dev_group)
        self._dev_rows = []

    def _on_show_all(self, sw, active):
        self.show_all_devices = active
        GLib.idle_add(self._populate_devices)
        return False

    def _populate_devices(self):
        for r in self._dev_rows:
            self.dev_expander.remove(r)
        self._dev_rows = []

        ranked = rank_sinks(self.sinks)
        shown = [s for s in ranked
                 if self.show_all_devices or s["score"] > 0]
        if not shown:
            shown = ranked[:1]

        for s in shown:
            row = Adw.ActionRow(title=s["desc"], subtitle=s["why"])
            btn = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_pick_device, s["name"])
            row.add_suffix(btn)
            self.dev_expander.add_row(row)
            self._dev_rows.append(row)

        hidden = len(ranked) - len(shown)
        self.dev_expander.set_subtitle(
            f"{len(shown)} shown" + (f", {hidden} hidden" if hidden else ""))
        return False

    def _on_pick_device(self, _btn, name):
        def work():
            objs = core.pw_dump()
            nid = core.resolve(objs, name)
            if nid is None:
                raise RuntimeError("Device disappeared")
            prev = core.current_default(objs)
            prev_id = core.resolve(objs, prev) if prev else None
            carry_volume(prev_id, nid)
            core.set_default_sink(nid)
            core.migrate_streams(nid, name, objs)
            self.state["hw_sink"] = name
            self.state["mode"] = "pure"
            core.save_state(self.state)
            return "Output changed"
        self._run(work)

    # ------------------------------------------------------------ eq

    def _build_eq(self):
        self.eq_group = Adw.PreferencesGroup(
            title="Equaliser",
            description="Applies in Custom EQ mode only. "
                        "Spatial and Pure bypass it entirely.")

        self.eq_scales = []
        for i, band in enumerate(v3.bands_for(self.state)):
            label = (f"{band['freq']} Hz" if band["freq"] < 1000
                     else f"{band['freq'] // 1000} kHz")
            row = Adw.ActionRow(title=label)
            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, -12, 12, 0.5)
            scale.set_value(band["gain"])
            scale.set_hexpand(True)
            scale.set_size_request(240, -1)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
            scale.connect("value-changed", self._on_band, i)
            block_scroll(scale)
            row.add_suffix(scale)
            self.eq_group.add(row)
            self.eq_scales.append(scale)

        reset = Adw.ActionRow(title="Reset to flat")
        btn = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", self._on_eq_reset)
        reset.add_suffix(btn)
        self.eq_group.add(reset)

        self.page.add(self.eq_group)

    def _on_band(self, scale, idx):
        if self._updating:
            return
        gain = round(scale.get_value(), 1)
        v3.bands_for(self.state)[idx]["gain"] = gain
        core.save_state(self.state)
        # Debounced push: coalesce drag events so we are not hammering pw-cli.
        if getattr(self, "_eq_timer", None):
            GLib.source_remove(self._eq_timer)
        self._eq_timer = GLib.timeout_add(60, self._push_band, idx, gain)

    def _push_band(self, idx, gain):
        self._eq_timer = None

        def work():
            objs = core.pw_dump()
            node, names = eq_controls(objs, idx)
            if node is None:
                return None  # chain not loaded; value is saved for later
            v3.push_many(node, {n: gain for n in names})
            return None
        threading.Thread(target=self._quiet, args=(work,), daemon=True).start()
        return False

    def _on_eq_reset(self, _btn):
        self._updating = True
        for i, s in enumerate(self.eq_scales):
            s.set_value(0)
            v3.bands_for(self.state)[i]["gain"] = 0.0
        self._updating = False
        core.save_state(self.state)

        def work():
            objs = core.pw_dump()
            node, _ = eq_controls(objs, 0)
            if node:
                for i in range(len(v3.bands_for(self.state))):
                    v3.push_many(node, {f"eqL{i}:Gain": 0.0,
                                        f"eqR{i}:Gain": 0.0})
            return "Equaliser flat"
        self._run(work)

    # ------------------------------------------------------------ spatial

    def _build_spatial(self):
        g = Adw.PreferencesGroup(
            title="Spatial Audio",
            description="Loudness is reduced to leave headroom for eight "
                        "summed channels. Raise it if surround feels thin; "
                        "lower it if loud scenes distort.")

        self.row_sofa = Adw.ActionRow(
            title="HRTF profile",
            subtitle=self.state.get("sofa") or "none selected")
        pick = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        pick.add_css_class("flat")
        pick.connect("clicked", self._on_pick_sofa)
        self.row_sofa.add_suffix(pick)
        g.add(self.row_sofa)

        row = Adw.ActionRow(
            title="Output level",
            subtitle="Ceiling, not gain — affects loud passages most")
        self.spatial_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.2, 1.0, 0.01)
        self.spatial_scale.set_value(self.state.get("makeup", 0.63))
        self.spatial_scale.set_size_request(240, -1)
        self.spatial_scale.set_draw_value(True)
        self.spatial_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.spatial_scale.add_mark(0.255, Gtk.PositionType.BOTTOM, "safe")
        self.spatial_scale.add_mark(0.63, Gtk.PositionType.BOTTOM, None)
        block_scroll(self.spatial_scale)
        row.add_suffix(self.spatial_scale)
        g.add(row)

        apply_row = Adw.ActionRow(
            title="Set up processing",
            subtitle="Writes configs and restarts PipeWire (~3 s of silence)")
        self.btn_apply = Gtk.Button(label="Set Up", valign=Gtk.Align.CENTER)
        self.btn_apply.add_css_class("suggested-action")
        self.btn_apply.connect("clicked", self._on_apply)
        apply_row.add_suffix(self.btn_apply)
        g.add(apply_row)

        rm = Adw.ActionRow(title="Remove processing",
                           subtitle="Restores your device and deletes configs")
        b = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
        b.add_css_class("destructive-action")
        b.connect("clicked", self._on_remove)
        rm.add_suffix(b)
        g.add(rm)

        self.page.add(g)

    def _on_pick_sofa(self, _btn):
        dlg = Gtk.FileDialog(title="Choose a SOFA HRTF file")
        filt = Gtk.FileFilter()
        filt.set_name("SOFA files")
        filt.add_pattern("*.sofa")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        dlg.set_filters(store)

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            if f:
                self.state["sofa"] = f.get_path()
                core.save_state(self.state)
                self.row_sofa.set_subtitle(f.get_path())
        dlg.open(self, None, done)

    def _on_apply(self, _btn):
        makeup = round(self.spatial_scale.get_value(), 2)
        self.state["makeup"] = makeup
        sofa = self.state.get("sofa")
        core.save_state(self.state)

        def work():
            core.CONF_DIR.mkdir(parents=True, exist_ok=True)
            objs = core.pw_dump()
            self.state = core.remember_hw_sink(self.state, objs)
            (core.CONF_DIR / f"{core.PREFIX}eq.conf").write_text(
                core.eq_config(v3.bands_for(self.state, "eq")))
            if sofa and os.path.exists(sofa):
                (core.CONF_DIR / f"{core.PREFIX}spatial.conf").write_text(
                    core.spatial_config(sofa, makeup=makeup))
            core.save_state(self.state)
            core.restart_pipewire()

            objs = core.pw_dump()
            ok_eq = chain_present(objs)
            ok_sp = ok_eq and core.sofa_works(objs)
            if not ok_eq:
                raise RuntimeError("EQ chain failed to load — check "
                                   "journalctl --user -u pipewire")
            if sofa and not ok_sp:
                raise RuntimeError("Spatial chain failed to load — the HRTF "
                                   "file may be unreadable")
            return "Ready" + ("" if sofa else " (no HRTF chosen, EQ only)")
        self._run(work)

    def _on_remove(self, _btn):
        def work():
            objs = core.pw_dump()
            if self.state.get("hw_sink"):
                nid = core.resolve(objs, self.state["hw_sink"])
                if nid:
                    core.set_default_sink(nid)
            n = 0
            for f in core.CONF_DIR.glob(f"{core.PREFIX}*.conf"):
                f.unlink()
                n += 1
            if n:
                core.restart_pipewire()
            self.state["mode"] = "pure"
            core.save_state(self.state)
            return "Processing removed"
        self._run(work)

    # ------------------------------------------------------------ refresh

    def refresh(self):
        def work():
            objs = core.pw_dump()
            cur = core.current_default(objs)
            self.sinks = core.list_sinks(objs)
            caps = core.device_capabilities()
            sofa = core.sofa_works(objs)
            eq_ok = chain_present(objs)
            sp_ok = eq_ok and core.sofa_works(objs)
            return ("refresh", cur, caps, sofa, eq_ok, sp_ok)

        def done(result):
            _, cur, caps, sofa, eq_ok, sp_ok = result
            self._updating = True

            mode = self.state.get("mode", "pure")
            if cur and cur != v3.SINK:
                # something outside this app moved the default away
                mode = "pure"
            self.state["mode"] = mode
            if mode in self.mode_checks:
                self.mode_checks[mode].set_active(True)

            self.mode_rows["eq"].set_sensitive(eq_ok)
            self.mode_rows["spatial"].set_sensitive(sp_ok)
            if not eq_ok:
                self.mode_rows["eq"].set_subtitle("Not set up yet")
            if not sp_ok:
                self.mode_rows["spatial"].set_subtitle(
                    "Not set up yet" if sofa
                    else "HRTF support unavailable on this system")

            pretty = cur or "unknown"
            for s in self.sinks:
                if s["name"] == cur:
                    pretty = s["desc"]
                    break
            if cur == v3.SINK:
                # Both processed modes share one sink now, so the sink name no
                # longer says which is active. The mode does.
                pretty = f"Headphones ({'Spatial' if mode == 'spatial' else 'EQ'})"
            self.row_output.set_subtitle(pretty)

            hw = next((c for c in caps if c["rates"]), None)
            if mode == "spatial":
                self.row_format.set_subtitle("48 kHz (fixed by HRTF)")
                self.row_purity.set_subtitle("Processed — not bit-perfect")
            elif hw:
                rs = ", ".join(f"{r/1000:g} kHz" for r in hw["rates"])
                bs = "/".join(str(b) for b in hw["bits"])
                self.row_format.set_subtitle(f"{rs} · {bs}-bit available")
                self.row_purity.set_subtitle(
                    "Bit-perfect where the source rate is supported"
                    if mode == "pure" else "Transparent float processing")
            else:
                self.row_format.set_subtitle("unknown")

            msg = None
            if hw and 44100 not in hw["rates"]:
                msg = ("This device cannot accept 44.1 kHz, so most music is "
                       "resampled. That is a hardware limit.")
            if not sofa:
                msg = "HRTF support not detected — Spatial mode unavailable."
            if msg:
                self.banner.set_title(msg)
                self.banner.set_revealed(True)
            else:
                self.banner.set_revealed(False)

            # Make it visually obvious when the equaliser is not in the path,
            # rather than leaving live-looking sliders that do nothing.
            if mode == "eq":
                self.eq_group.set_description(
                    "Active — changes apply immediately.")
                self.eq_group.remove_css_class("dim-label")
            else:
                self.eq_group.set_description(
                    f"Saved, but not in the signal path — {mode} mode "
                    "bypasses the equaliser.")
                self.eq_group.add_css_class("dim-label")

            self._populate_devices()
            self._updating = False

        self._run(work, on_done=done, quiet=True)

    # ------------------------------------------------------------ threading

    def _run(self, fn, on_done=None, quiet=False):
        """Run blocking work off the main loop; report errors as a banner."""
        def thread():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 - surfaced to the user
                GLib.idle_add(self._error, str(e))
                return
            GLib.idle_add(self._finish, result, on_done, quiet)
        threading.Thread(target=thread, daemon=True).start()

    def _quiet(self, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(self._error, str(e))

    def _finish(self, result, on_done, quiet):
        if on_done:
            on_done(result)
        elif not quiet:
            GLib.timeout_add(300, lambda: (self.refresh(), False)[1])
        return False

    def _error(self, msg):
        self.banner.set_title(msg)
        self.banner.set_revealed(True)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.hpctl.Headphones")

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
