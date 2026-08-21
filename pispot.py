import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import strftime

try:
    _ver = version("textual")
except PackageNotFoundError:
    raise SystemExit(
        "Textual is not installed.\n"
        "Run: sudo pip3 install --break-system-packages textual"
    )
if int(_ver.partition(".")[0]) < 1:
    raise SystemExit(
        f"Textual {_ver} (Debian's python3-textual) is too old.\n"
        "Run: sudo apt remove python3-textual"
        " && sudo pip3 install --break-system-packages textual"
    )

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Switch,
)
from textual import work

PROFILE = "Hotspot"
SYSCTL_CONF = Path("/etc/sysctl.d/99-pispot.conf")
NEIGH_STATES = {"REACHABLE", "STALE", "DELAY", "PROBE"}
BANDS = [("2.4 GHz (b/g/n)", "bg"), ("5 GHz (a/n/ac)", "a")]
CHANNELS = {
    "bg": [("Auto", "")] + [(str(c), str(c)) for c in range(1, 14)],
    "a": [("Auto", "")]
    + [(str(c), str(c)) for c in (36, 40, 44, 48, 149, 153, 157, 161, 165)],
}


def _run(*args):
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{args[0]} exited {proc.returncode}")
    return proc.stdout


def wifi_ifaces():
    out = _run("nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status")
    return [line.partition(":")[0] for line in out.splitlines() if line.endswith(":wifi")]


def load_profile(name=PROFILE):
    fields = (
        "802-11-wireless.ssid",
        "802-11-wireless.band",
        "802-11-wireless.channel",
        "802-11-wireless.hidden",
        "802-11-wireless-security.psk",
    )
    try:
        out = _run("nmcli", "-t", "-f", ",".join(fields), "connection", "show", name)
    except RuntimeError:
        return None
    vals = {}
    for line in out.splitlines():
        key, _, val = line.partition(":")
        vals[key] = val
    return {
        "ssid": vals.get(fields[0], ""),
        "band": vals.get(fields[1]) or "bg",
        "channel": vals.get(fields[2], ""),
        "hidden": vals.get(fields[3]) == "yes",
        "psk": vals.get(fields[4], ""),
    }


def is_active(name=PROFILE):
    try:
        out = _run("nmcli", "-t", "-f", "GENERAL.STATE", "connection", "show", name)
    except RuntimeError:
        return False
    return out.strip().endswith(":activated")


def active_ip(iface):
    try:
        out = _run("nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", iface)
    except RuntimeError:
        return ""
    for line in out.splitlines():
        val = line.partition(":")[2]
        if val:
            return val.split("/")[0]
    return ""


def ensure_forwarding():
    conf = "net.ipv4.ip_forward=1\n"
    try:
        if SYSCTL_CONF.read_text() == conf:
            return
        SYSCTL_CONF.write_text(conf)
        _run("sysctl", "-w", "net.ipv4.ip_forward=1")
    except OSError:
        pass


def apply(cfg, iface=None, name=PROFILE):
    if load_profile(name) is None:
        args = [
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "con-name",
            name,
            "autoconnect",
            "yes",
            "ssid",
            cfg["ssid"],
            "802-11-wireless.mode",
            "ap",
            "ipv4.method",
            "shared",
        ]
        if iface:
            args += ["ifname", iface]
        _run(*args)
    _run(
        "nmcli",
        "connection",
        "modify",
        name,
        "802-11-wireless.ssid",
        cfg["ssid"],
        "802-11-wireless.band",
        cfg["band"],
        "802-11-wireless.channel",
        cfg["channel"],
        "802-11-wireless.hidden",
        "yes" if cfg["hidden"] else "no",
        "802-11-wireless-security.psk",
        cfg["psk"],
        "802-11-wireless-security.key-mgmt",
        "wpa-psk",
        "ipv4.never-default",
        "yes",
        "ipv4.route-metric",
        "700",
        "ipv4.dns-priority",
        "200",
    )
    ensure_forwarding()
    _run("nmcli", "connection", "up", name)


def start(name=PROFILE):
    _run("nmcli", "connection", "up", name)


def stop(name=PROFILE):
    _run("nmcli", "connection", "down", name)


def clients(iface):
    sig = {}
    if shutil.which("iw"):
        try:
            out = _run("iw", "dev", iface, "station", "dump")
        except (RuntimeError, OSError):
            out = ""
        mac = None
        for line in out.splitlines():
            tok = line.split()
            if not tok:
                continue
            if tok[0] == "Station":
                mac = tok[1].lower()
            elif tok[0] == "signal:" and mac:
                sig[mac] = f"{tok[1]} dBm"
    hosts = []
    try:
        out = _run("ip", "neigh", "show", "dev", iface)
    except (RuntimeError, OSError):
        out = ""
    for line in out.splitlines():
        tok = line.split()
        if "lladdr" not in tok:
            continue
        i = tok.index("lladdr")
        ip_addr, mac, state = tok[0], tok[i + 1], tok[-1]
        if ":" in ip_addr or state not in NEIGH_STATES:
            continue
        hosts.append((ip_addr, mac.upper(), sig.get(mac.lower(), "?")))
    return sorted(hosts)


class PiSpotApp(App):
    TITLE = "PiSpot TUI"
    CSS = """
    #main { height: 1fr; }
    #config { width: 42%; padding: 1 2; border-right: solid $accent; }
    #monitor { width: 58%; padding: 1 2; }
    #config Label { margin-top: 1; }
    #row { height: 3; }
    #eye { width: 8; margin-left: 1; margin-top: 0; }
    #hrow { height: auto; margin-top: 1; }
    #hrow Label { margin-right: 1; }
    Select, Input { margin-bottom: 1; }
    Button { margin-top: 1; width: 100%; }
    #refreshed { margin-top: 1; text-align: center; color: $text-muted; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "toggle", "Start/Stop"),
        ("r", "refresh_clients", "Refresh Clients"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield Vertical(
                Label("", id="status"),
                Label("SSID"),
                Input(id="ssid"),
                Label("Password"),
                Horizontal(Input(id="psk", password=True), Button("Show", id="eye"), id="row"),
                Label("Interface"),
                Select([], prompt="Interface", id="iface"),
                Label("Band"),
                Select(BANDS, prompt="Band", id="band"),
                Label("Channel"),
                Select(CHANNELS["bg"], prompt="Channel", id="chan"),
                Horizontal(Label("Hidden network"), Switch(id="hidden"), id="hrow"),
                Button("APPLY & RESTART HOTSPOT", variant="success", id="apply"),
                Button("STOP HOTSPOT", variant="error", id="stop"),
                id="config",
            )
            yield Vertical(
                DataTable(id="clients"),
                Label("", id="refreshed"),
                id="monitor",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#clients", DataTable).add_columns("IP Address", "MAC Address", "Signal")
        self._ifaces = []
        self.set_interval(5, self.refresh_all)
        self.init_load()

    def _iface(self):
        sel = self.query_one("#iface", Select)
        if isinstance(sel.value, str):
            return sel.value
        return self._ifaces[0] if self._ifaces else "wlan0"

    def _fill_config(self, ifaces, prof):
        iface_sel = self.query_one("#iface", Select)
        iface_sel.set_options([(i, i) for i in ifaces] or [("wlan0", "wlan0")])
        if ifaces:
            iface_sel.value = ifaces[0]
        if prof:
            self.query_one("#ssid", Input).value = prof["ssid"]
            self.query_one("#psk", Input).value = prof["psk"]
            band = self.query_one("#band", Select)
            band.value = prof["band"]
            opts = list(CHANNELS.get(prof["band"], CHANNELS["bg"]))
            if prof["channel"] and all(v != prof["channel"] for _, v in opts):
                opts.insert(0, (f"{prof['channel']} (current)", prof["channel"]))
            chan = self.query_one("#chan", Select)
            chan.set_options(opts)
            chan.value = prof["channel"]
            self.query_one("#hidden", Switch).value = prof["hidden"]

    def _update_panes(self, state, ip, rows, stamp):
        dot, word = ("[green]●[/]", "ACTIVE") if state else ("[red]●[/]", "DOWN")
        self.query_one("#status", Label).update(f"{dot} {word}" + (f" ({ip})" if ip else ""))
        table = self.query_one("#clients", DataTable)
        table.clear()
        for row in rows:
            table.add_row(*row)
        self.query_one("#refreshed", Label).update(
            f"Last refreshed: {stamp} — {len(rows)} client(s)"
        )

    def _err(self, msg):
        self.notify(msg, severity="error", timeout=10)

    @work(thread=True, exclusive=True)
    def init_load(self):
        self._ifaces = wifi_ifaces()
        prof = load_profile()
        self.call_from_thread(self._fill_config, self._ifaces, prof)
        self.call_from_thread(self.refresh_all)

    @work(thread=True, exclusive=True)
    def refresh_all(self):
        iface = self._iface()
        state = is_active()
        ip = active_ip(iface) if state else ""
        rows = clients(iface) if state else []
        self.call_from_thread(self._update_panes, state, ip, rows, strftime("%H:%M:%S"))

    @work(thread=True, exclusive=True)
    def action_toggle(self):
        try:
            if is_active():
                stop()
                msg = "Hotspot stopped"
            else:
                start()
                msg = "Hotspot started"
        except RuntimeError as exc:
            self.call_from_thread(self._err, str(exc))
            return
        self.call_from_thread(self.notify, msg)
        self.call_from_thread(self.refresh_all)

    @work(thread=True, exclusive=True)
    def action_stop_hotspot(self):
        try:
            stop()
        except RuntimeError as exc:
            self.call_from_thread(self._err, str(exc))
            return
        self.call_from_thread(self.notify, "Hotspot stopped")
        self.call_from_thread(self.refresh_all)

    @work(thread=True, exclusive=True)
    def apply_worker(self, cfg, iface):
        try:
            apply(cfg, iface)
        except RuntimeError as exc:
            self.call_from_thread(self._err, str(exc))
            return
        self.call_from_thread(self.notify, "Settings applied — hotspot restarted")
        self.call_from_thread(self.refresh_all)

    def do_apply(self):
        band = self.query_one("#band", Select).value
        chan = self.query_one("#chan", Select).value
        cfg = {
            "ssid": self.query_one("#ssid", Input).value.strip(),
            "psk": self.query_one("#psk", Input).value,
            "band": band if isinstance(band, str) else "bg",
            "channel": chan if isinstance(chan, str) else "",
            "hidden": self.query_one("#hidden", Switch).value,
        }
        if not cfg["ssid"]:
            self.notify("SSID is required", severity="error")
            return
        if len(cfg["psk"]) < 8:
            self.notify("Password must be at least 8 characters (WPA)", severity="error")
            return
        self.apply_worker(cfg, self._iface())

    def action_refresh_clients(self):
        self.refresh_all()

    def on_select_changed(self, event):
        if event.select.id != "band":
            return
        chan = self.query_one("#chan", Select)
        current = chan.value if isinstance(chan.value, str) else ""
        opts = CHANNELS.get(str(event.value), CHANNELS["bg"])
        chan.set_options(opts)
        if any(v == current for _, v in opts):
            chan.value = current

    def on_button_pressed(self, event):
        btn = event.button.id
        if btn == "apply":
            self.do_apply()
        elif btn == "stop":
            self.action_stop_hotspot()
        elif btn == "eye":
            psk = self.query_one("#psk", Input)
            psk.password = not psk.password
            self.query_one("#eye", Button).label = "Hide" if psk.password else "Show"


if __name__ == "__main__":
    PiSpotApp().run()
