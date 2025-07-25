# -*- encoding: utf-8 -*-
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2017 Marek Marczykowski-Górecki
#                               <marmarek@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation; either version 2.1 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with this program; if not, see <http://www.gnu.org/licenses/>.

"""GUI/AUDIO daemon launcher tool"""

import os
import signal
import subprocess
import fcntl
import asyncio
import logging
import re
import functools
import sys
import xcffib
import xcffib.xproto  # pylint: disable=unused-import

import qubesadmin
import qubesadmin.events
import qubesadmin.exc
import qubesadmin.tools
import qubesadmin.vm
from qubesadmin.tools import xcffibhelpers

# pylint: disable=wrong-import-position,wrong-import-order
from Xlib import X, XK
from Xlib.display import Display
from Xlib.error import DisplayConnectionError

GUI_DAEMON_PATH = "/usr/bin/qubes-guid"
PACAT_DAEMON_PATH = "/usr/bin/pacat-simple-vchan"
QUBES_ICON_DIR = "/usr/share/icons/hicolor/128x128/devices"


def validator_key_sequence(sequence: str) -> bool:
    """xside.c key sequence validation is not case sensitive and supports more
    choices than Global Config's limited choices, so we replicate it here"""
    if not isinstance(sequence, str):
        return False
    if sequence.lower() in ["none", "disable"]:
        return True
    while "-" in sequence:
        modifier, sequence = sequence.split("-", 1)
        if not modifier.lower() in [
            "shift",
            "ctrl",
            "alt",
            "mod1",
            "mod2",
            "mod3",
            "mod4",
        ]:
            return False
    try:
        display = Display()
        result = display.keysym_to_keycode(XK.string_to_keysym(sequence))
        display.close()
        return result != X.NoSymbol
    except DisplayConnectionError:
        return False


def validator_trayicon_mode(mode: str) -> bool:
    """xside.c tray mode validation is replicated here"""
    if not isinstance(mode, str):
        return False
    if mode in ["bg", "border1", "border2", "tint"]:
        return True
    if mode.startswith("tint"):
        if mode[4:] in ["+border1", "+border2", "+saturation50", "+whitehack"]:
            return True
    return False


def validator_color(color: str) -> bool:
    """xside.c `parse_color` validation code is replicated here"""
    if not isinstance(color, str):
        return False
    if re.match(r"^0[xX](?:[0-9a-fA-F]{3}){1,2}$", color.strip()):
        # Technically `parse_color` could parse values such as `0x0`
        # Xlib.xobject.colormap conventions & standards are different.
        return True
    try:
        display = Display()
        result = display.screen().default_colormap.alloc_named_color(color)
        display.close()
        if result is not None:
            return True
    except DisplayConnectionError:
        return False
    return False


GUI_DAEMON_OPTIONS = [
    ("allow_fullscreen", "bool", (lambda x: isinstance(x, bool))),
    ("override_redirect_protection", "bool", (lambda x: isinstance(x, bool))),
    ("override_redirect", "str", (lambda x: x in ["allow", "disable"])),
    ("allow_utf8_titles", "bool", (lambda x: isinstance(x, bool))),
    ("secure_copy_sequence", "str", validator_key_sequence),
    ("secure_paste_sequence", "str", validator_key_sequence),
    ("windows_count_limit", "int", (lambda x: isinstance(x, int) and x > 0)),
    ("trayicon_mode", "str", validator_trayicon_mode),
    ("window_background_color", "str", validator_color),
    ("startup_timeout", "int", (lambda x: isinstance(x, int) and x >= 0)),
    (
        "max_clipboard_size",
        "int",
        (lambda x: isinstance(x, int) and 256 <= x <= 256000),
    ),
    (
        "events_max_delay",
        "int",
        (lambda x: isinstance(x, int) and 0 <= x <= 5000),
    ),
]

formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
handler.setFormatter(formatter)

log = logging.getLogger("qvm-start-daemon")
log.addHandler(handler)

if "XDG_RUNTIME_DIR" in os.environ:
    pidfile_path = os.path.join(
        os.environ["XDG_RUNTIME_DIR"], "qvm-start-daemon.pid"
    )
else:
    pidfile_path = os.path.join(
        os.environ.get("HOME", "/"), ".qvm-start-daemon.pid"
    )

parser = qubesadmin.tools.QubesArgumentParser(
    description="start GUI or AUDIO for qube(s)", vmname_nargs="*"
)
parser.add_argument(
    "--debug", "-d", action="store_true", help="Show debug messages"
)
parser.add_argument(
    "--watch",
    action="store_true",
    help="Keep watching for further domain startups",
)
parser.add_argument(
    "--force-stubdomain",
    action="store_true",
    help="Start GUI to stubdomain-emulated VGA,"
    " even if gui-agent is running in the VM",
)
parser.add_argument(
    "--pidfile",
    action="store",
    default=pidfile_path,
    help="Pidfile path to create in --watch mode",
)
parser.add_argument(
    "--notify-monitor-layout",
    action="store_true",
    help="Notify running instance in --watch mode"
    " about changed monitor layout",
)
parser.add_argument(
    "--kde",
    action="store_true",
    help="Set KDE specific arguments to gui-daemon.",
)
# Add it for the help only
parser.add_argument(
    "--force",
    action="store_true",
    default=False,
    help="Force running daemon without enabled services"
    " 'guivm' or 'audiovm'",
)


def retrieve_gui_daemon_options(vm, guivm):
    """
    Construct a list of GUI daemon options based on VM features.

    This checks 'gui-*' features on the VM, and if they're absent,
    'gui-default-*' features on the GuiVM.
    """

    options = {}

    for name, kind, validator in GUI_DAEMON_OPTIONS:
        feature = "gui-" + name.replace("_", "-")
        feature_value = vm.features.get(feature, None)
        if feature_value is None:
            feature = "gui-default-" + name.replace("_", "-")
            feature_value = guivm.features.get(feature, None)
        if feature_value is None:
            continue

        if kind == "bool":
            value = bool(feature_value)
        elif kind == "int":
            value = int(feature_value)
        elif kind == "str":
            value = feature_value
        else:
            assert False, kind

        if not validator(value):
            message = (
                f"{vm.name}: Ignoring invalid feature:\n"
                f"{feature}={feature_value}"
            )
            log.error(message)
            if not sys.stdout.isatty():
                subprocess.run(
                    [
                        "notify-send",
                        "-a",
                        "Qubes GUI Daemon",
                        "--icon",
                        "dialog-warning",
                        message,
                    ],
                    check=False,
                )
            continue

        options[name] = value
    return options


def serialize_gui_daemon_options(options):
    """
    Prepare configuration file content for GUI daemon. Currently, uses libconfig
    format.
    """

    lines = [
        "# Auto-generated file, do not edit!",
        "",
        "global: {",
    ]
    for name, kind, validator in GUI_DAEMON_OPTIONS:
        if name in options:
            value = options[name]
            if kind == "bool":
                serialized = "true" if value else "false"
            elif kind == "int":
                serialized = str(value)
            elif kind == "str":
                serialized = escape_config_string(value)
            else:
                assert False, kind

            if not validator(value):
                message = f"Ignoring invalid GUI property:\n{name}={value}"
                log.error(message)
                if not sys.stdout.isatty():
                    subprocess.run(
                        [
                            "notify-send",
                            "-a",
                            "Qubes GUI Daemon",
                            "--icon",
                            "dialog-warning",
                            message,
                        ],
                        check=False,
                    )
                continue

            lines.append("  {} = {};".format(name, serialized))
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
UNPRINTABLE_CHARACTER_RE = re.compile(r"[\x00-\x1F\x7F]")


def escape_config_string(value):
    """
    Convert a string to libconfig format.

    Format specification:
    http://www.hyperrealm.com/libconfig/libconfig_manual.html#String-Values

    See dump_string() for python-libconf:
    https://github.com/Grk0/python-libconf/blob/master/libconf.py
    """

    assert not NON_ASCII_RE.match(
        value
    ), "expected an ASCII string: {!r}".format(value)

    value = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\f", r"\f")
        .replace("\n", r"\n")
        .replace("\r", r"\r")
        .replace("\t", r"\t")
    )
    value = UNPRINTABLE_CHARACTER_RE.sub(
        lambda m: r"\x{:02x}".format(ord(m.group(0))), value
    )
    return '"' + value + '"'


# "LVDS connected 1024x768+0+0 (normal left inverted right) 304mm x 228mm"
REGEX_OUTPUT = re.compile(
    r"""(?x)                           # ignore whitespace
        ^                              # start of string
        (?P<output>[A-Za-z0-9\-]*)[ ]  # LVDS VGA etc
        (?P<connect>(dis)?connected)   # dis/connected
        ([ ]
        (?P<primary>(primary)?)[ ]?
        ((                             # a group
           (?P<width>\d+)x             # either 1024x768+0+0
           (?P<height>\d+)[+]
           (?P<x>\d+)[+]
           (?P<y>\d+)
         )|[\D])                       # or not a digit
        ([ ]\(.*\))?[ ]?               # ignore options
        (                              #  304mm x 228mm
           (?P<width_mm>\d+)mm[ ]x[ ]
           (?P<height_mm>\d+)mm
        )?
        .*                             # ignore rest of line
        )?                             # anything after (dis)connect is optional
        """
)


class KeyboardLayout:
    """Class to store and parse X Keyboard layout data"""

    # pylint: disable=too-few-public-methods
    def __init__(self, binary_string):
        split_string = binary_string.split(b"\0")
        self.languages = split_string[2].decode().split(",")
        self.variants = split_string[3].decode().split(",")
        self.options = split_string[4].decode()
        self.options = ",".join(
            opt for opt in self.options.split(",") if not opt.startswith("grp:")
        )

    def get_property(self, layout_num):
        """Return the selected keyboard layout as formatted for keyboard_layout
        property."""
        return "+".join(
            [
                self.languages[layout_num],
                self.variants[layout_num],
                self.options,
            ]
        )


class XWatcher:
    """Watch and react for X events related to the keyboard layout changes."""

    def __init__(self, conn, app):
        self.app = app
        self.current_vm = self.app.domains[self.app.local_name]

        self.conn = conn
        self.ext = self.initialize_extension()

        # get root window
        self.setup = self.conn.get_setup()
        self.root = self.setup.roots[0].root

        # atoms (strings) of events we need to watch
        # keyboard layout was switched
        self.atom_xklavier = (
            self.conn.core.InternAtom(
                False,
                len("XKLAVIER_ALLOW_SECONDARY"),
                "XKLAVIER_ALLOW_SECONDARY",
            )
            .reply()
            .atom
        )
        # keyboard layout was changed
        self.atom_xkb_rules = (
            self.conn.core.InternAtom(
                False, len("_XKB_RULES_NAMES"), "_XKB_RULES_NAMES"
            )
            .reply()
            .atom
        )

        self.conn.core.ChangeWindowAttributesChecked(
            self.root,
            xcffib.xproto.CW.EventMask,
            [xcffib.xproto.EventMask.PropertyChange],
        )
        self.conn.flush()

        # initialize state
        self.keyboard_layout = KeyboardLayout(self.get_keyboard_layout())
        self.selected_layout = self.get_selected_layout()

    def initialize_extension(self):
        """Initialize XKB extension (not supported by xcffib by default"""
        ext = self.conn(xcffibhelpers.key)
        ext.UseExtension()
        return ext

    def get_keyboard_layout(self):
        """Check what is current keyboard layout definition"""
        property_cookie = self.conn.core.GetProperty(
            False,  # delete
            self.root,  # window
            self.atom_xkb_rules,
            xcffib.xproto.Atom.STRING,
            0,
            1000,
        )
        prop_reply = property_cookie.reply()
        return prop_reply.value.buf()

    def get_selected_layout(self):
        """Check which keyboard layout is currently selected"""
        state_reply = self.ext.GetState().reply()
        return state_reply.lockedGroup[0]

    def update_keyboard_layout(self):
        """Update current vm's keyboard_layout property"""
        new_property = self.keyboard_layout.get_property(self.selected_layout)

        current_property = self.current_vm.keyboard_layout

        if new_property != current_property:
            self.current_vm.keyboard_layout = new_property

    def event_reader(self, callback):
        """Poll for X events related to keyboard layout"""
        try:
            for event in iter(self.conn.poll_for_event, None):
                if isinstance(event, xcffib.xproto.PropertyNotifyEvent):
                    if event.atom == self.atom_xklavier:
                        self.selected_layout = self.get_selected_layout()
                    elif event.atom == self.atom_xkb_rules:
                        self.keyboard_layout = KeyboardLayout(
                            self.get_keyboard_layout()
                        )
                    else:
                        continue

                    self.update_keyboard_layout()
        except xcffib.ConnectionException:
            callback()


def get_monitor_layout():
    """Get list of monitors and their size/position"""
    outputs = []

    with subprocess.Popen(["xrandr", "-q"], stdout=subprocess.PIPE) as proc:
        for line in proc.stdout:
            line = line.decode()
            if not line.startswith("Screen") and not line.startswith(" "):
                match = REGEX_OUTPUT.match(line)
                if not match:
                    logging.warning("Invalid output from xrandr: %r", line)
                    continue
                output_params = match.groupdict()
                if output_params["width"]:
                    phys_size = ""
                    if output_params["width_mm"] and int(
                        output_params["width_mm"]
                    ):
                        # don't provide real values for privacy reasons - see
                        # #1951 for details
                        dpi = (
                            int(output_params["width"])
                            * 254
                            // int(output_params["width_mm"])
                            // 10
                        )
                        if dpi > 300:
                            dpi = 300
                        elif dpi > 200:
                            dpi = 200
                        elif dpi > 150:
                            dpi = 150
                        else:
                            dpi = 96
                        # now calculate dimensions based on approximate DPI
                        phys_size = " {} {}".format(
                            int(output_params["width"]) * 254 // dpi // 10,
                            int(output_params["height"]) * 254 // dpi // 10,
                        )
                    outputs.append(
                        "%s %s %s %s%s\n"
                        % (
                            output_params["width"],
                            output_params["height"],
                            output_params["x"],
                            output_params["y"],
                            phys_size,
                        )
                    )
    return outputs


class DAEMONLauncher:
    """Launch GUI/AUDIO daemon for VMs"""

    def __init__(
        self,
        app: qubesadmin.app.QubesBase,
        enabled_services,
        vm_names=None,
        kde=False,
    ):
        """Initialize DAEMONLauncher.

        :param app: :py:class:`qubesadmin.Qubes` instance
        :param vm_names: VM names to watch for, or None if watching for all
        :param kde: add KDE-specific arguments for guid
        """
        self.app = app
        self.enabled_services = enabled_services
        self.started_processes = {}
        self.vm_names = vm_names
        self.kde = kde

        # cache XID values when the VM was still running -
        # for cleanup purpose
        self.xid_cache = {}

    async def send_monitor_layout(self, vm, layout=None, startup=False):
        """Send monitor layout to a given VM

        This function is a coroutine.

        :param vm: VM to which send monitor layout
        :param layout: monitor layout to send; if None, fetch it from
            local X server.
        :param startup:
        :return: None
        """
        if (
            vm.features.check_with_template("no-monitor-layout", False)
            or not vm.is_running()
            or getattr(vm, "is_preload", False)
        ):
            return

        if layout is None:
            layout = get_monitor_layout()
            if not layout:
                return

        vm.log.info("Sending monitor layout")

        if not startup:
            with open(self.guid_pidfile(vm.xid), encoding="ascii") as pidfile:
                pid = int(pidfile.read())
            os.kill(pid, signal.SIGHUP)
            try:
                with open(
                    self.guid_pidfile(vm.stubdom_xid), encoding="ascii"
                ) as pidfile:
                    pid = int(pidfile.read())
                os.kill(pid, signal.SIGHUP)
            except FileNotFoundError:
                pass

        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                functools.partial(
                    vm.run_service_for_stdio,
                    "qubes.SetMonitorLayout",
                    input="".join(layout).encode(),
                    autostart=False,
                ),
            )
        except subprocess.CalledProcessError as e:
            vm.log.warning("Failed to send monitor layout: %s", e.stderr)

    def send_monitor_layout_all(self):
        """Send monitor layout to all (running) VMs"""
        monitor_layout = get_monitor_layout()
        for vm in self.app.domains:
            if (
                getattr(vm, "guivm", None) != vm.app.local_name
                or vm.klass == "AdminVM"
                or not vm.is_running()
                or not vm.features.check_with_template("gui", True)
                or getattr(vm, "is_preload", False)
            ):
                continue
            asyncio.ensure_future(
                self.send_monitor_layout(vm, monitor_layout)
            )

    @staticmethod
    def kde_guid_args(vm):
        """Return KDE-specific arguments for gui-daemon, if applicable"""

        guid_cmd = []
        # native decoration plugins is used, so adjust window properties
        # accordingly
        guid_cmd += ["-T"]  # prefix window titles with VM name
        # get owner of X11 session
        session_owner = None
        for line in subprocess.check_output(["xhost"]).splitlines():
            if line == b"SI:localuser:root":
                pass
            elif line.startswith(b"SI:localuser:"):
                session_owner = line.split(b":")[2].decode()
        if session_owner is not None:
            data_dir = os.path.expanduser(
                "~{}/.local/share".format(session_owner)
            )
        else:
            # fallback to current user
            data_dir = os.path.expanduser("~/.local/share")

        guid_cmd += [
            "-p",
            "_KDE_NET_WM_COLOR_SCHEME=s:{}".format(
                os.path.join(data_dir, "qubes-kde", vm.label.name + ".colors")
            ),
        ]
        return guid_cmd

    def common_guid_args(self, vm):
        """Common qubes-guid arguments for PV(H), HVM and Stubdomain"""

        guid_cmd = [
            GUI_DAEMON_PATH,
            "-N",
            vm.name,
            "-c",
            vm.label.color,
            "-i",
            os.path.join(QUBES_ICON_DIR, vm.label.icon) + ".png",
            "-l",
            str(vm.label.index),
        ]

        if vm.debug:
            guid_cmd += ["-v", "-v"]
            #       elif not verbose:
        else:
            guid_cmd += ["-q"]

        if vm.features.check_with_template("rpc-clipboard", False):
            guid_cmd.extend(["-Q"])

        guivm = self.app.domains[vm.guivm]
        options = retrieve_gui_daemon_options(vm, guivm)
        config = serialize_gui_daemon_options(options)
        config_path = self.guid_config_file(vm.xid)
        self.write_guid_config(config_path, config)
        guid_cmd.extend(["-C", config_path])
        if self.kde:
            guid_cmd.extend(self.kde_guid_args(vm))
        return guid_cmd

    @staticmethod
    def write_guid_config(config_path, config):
        """Write guid configuration to a file"""
        with open(config_path, "w", encoding="ascii") as config_file:
            config_file.write(config)

    @staticmethod
    def guid_pidfile(xid):
        """Helper function to construct a GUI pidfile path"""
        return "/var/run/qubes/guid-running.{}".format(xid)

    @staticmethod
    def guid_config_file(xid):
        """Helper function to construct a GUI configuration file path"""
        return "/var/run/qubes/guid-conf.{}".format(xid)

    @staticmethod
    def pacat_pidfile(xid):
        """Helper function to construct an AUDIO pidfile path"""
        return "/var/run/qubes/pacat.{}".format(xid)

    @staticmethod
    def pacat_domid(vm):
        """Determine target domid for an AUDIO daemon"""
        xid = (
            vm.stubdom_xid
            if vm.features.check_with_template("audio-model", False)
            and vm.virt_mode == "hvm"
            else vm.xid
        )
        return xid

    async def start_gui_for_vm(self, vm, monitor_layout=None):
        """Start GUI daemon (qubes-guid) connected directly to a VM

        This function is a coroutine.

        :param vm: VM for which start GUI daemon
        :param monitor_layout: monitor layout to send; if None, fetch it from
            local X server.
        """
        if getattr(vm, "is_preload", False):
            return
        guid_cmd = self.common_guid_args(vm)
        guid_cmd.extend(["-d", str(vm.xid)])

        if vm.virt_mode == "hvm":
            guid_cmd.extend(["-n"])

            stubdom_guid_pidfile = self.guid_pidfile(vm.stubdom_xid)
            if not vm.debug and os.path.exists(stubdom_guid_pidfile):
                # Terminate stubdom guid once "real" gui agent connects
                with open(
                    stubdom_guid_pidfile, "r", encoding="ascii"
                ) as pidfile:
                    stubdom_guid_pid = pidfile.read().strip()
                guid_cmd += ["-K", stubdom_guid_pid]

        vm.log.info("Starting GUI")

        await asyncio.create_subprocess_exec(*guid_cmd)

        await self.send_monitor_layout(vm, layout=monitor_layout, startup=True)

    async def start_gui_for_stubdomain(self, vm, force=False):
        """Start GUI daemon (qubes-guid) connected to a stubdomain

        This function is a coroutine.
        """
        if getattr(vm, "is_preload", False):
            return
        want_stubdom = force
        if not want_stubdom and vm.features.check_with_template(
            "gui-emulated", False
        ):
            want_stubdom = True
        # if no 'gui' or 'gui-emulated' feature set at all, use emulated GUI
        if (
            not want_stubdom
            and vm.features.check_with_template("gui", None) is None
            and vm.features.check_with_template("gui-emulated", None) is None
        ):
            want_stubdom = True
        if not want_stubdom and vm.debug:
            want_stubdom = True
        if not want_stubdom:
            return
        if os.path.exists(self.guid_pidfile(vm.stubdom_xid)):
            return
        vm.log.info("Starting GUI (stubdomain)")
        guid_cmd = self.common_guid_args(vm)
        guid_cmd.extend(["-d", str(vm.stubdom_xid), "-t", str(vm.xid)])

        await asyncio.create_subprocess_exec(*guid_cmd)

    async def start_audio_for_vm(self, vm):
        """Start AUDIO daemon (pacat-simple-vchan) connected directly to a VM

        This function is a coroutine.

        :param vm: VM for which start AUDIO daemon
        """
        if getattr(vm, "is_preload", False):
            return
        pacat_cmd = [
            PACAT_DAEMON_PATH,
            "-l",
            str(self.pacat_domid(vm)),
            vm.name,
        ]
        vm.log.info("Starting AUDIO")

        await asyncio.create_subprocess_exec(*pacat_cmd)

    async def start_gui(self, vm, force_stubdom=False, monitor_layout=None):
        """Start GUI daemon regardless of start event.

        This function is a coroutine.

        :param vm: VM for which GUI daemon should be started
        :param force_stubdom: Force GUI daemon for stubdomain, even if the
            one for target AppVM is running.
        :param monitor_layout: monitor layout configuration
        """
        if getattr(vm, "is_preload", False):
            return
        guivm = getattr(vm, "guivm", None)
        if guivm != vm.app.local_name:
            vm.log.info("GUI connected to {}. Skipping.".format(guivm))
            return

        if vm.virt_mode == "hvm":
            await self.start_gui_for_stubdomain(vm, force=force_stubdom)

        if not vm.features.check_with_template("gui", True):
            return

        if not os.path.exists(self.guid_pidfile(vm.xid)):
            await self.start_gui_for_vm(vm, monitor_layout=monitor_layout)

    async def start_audio(self, vm):
        """Start AUDIO daemon regardless of start event.

        This function is a coroutine.

        :param vm: VM for which AUDIO daemon should be started
        """
        if getattr(vm, "is_preload", False):
            return
        audiovm = getattr(vm, "audiovm", None)
        if audiovm != vm.app.local_name:
            vm.log.info("AUDIO connected to {}. Skipping.".format(audiovm))
            return

        if not vm.features.check_with_template("audio", True):
            return

        xid = self.pacat_domid(vm)
        if not os.path.exists(self.pacat_pidfile(xid)):
            await self.start_audio_for_vm(vm)
        else:
            vm.log.info("AUDIO process exists. Skipping.")

    def on_domain_spawn(self, vm, _event, **kwargs):
        """Handler of 'domain-spawn' event, starts GUI daemon for stubdomain"""

        if not self.is_watched(vm) or getattr(vm, "is_preload", False):
            return

        try:
            if (
                "guivm" in self.enabled_services
                and getattr(vm, "guivm", None) != vm.app.local_name
            ):
                return
            if not vm.features.check_with_template(
                "gui", True
            ) and not vm.features.check_with_template("gui-emulated", True):
                return
            if (
                vm.virt_mode == "hvm"
                and kwargs.get("start_guid", "True") == "True"
            ):
                asyncio.ensure_future(self.start_gui_for_stubdomain(vm))
        except qubesadmin.exc.QubesException as e:
            vm.log.warning("Failed to start GUI for %s: %s", vm.name, str(e))

    def on_domain_start(self, vm, _event, **kwargs):
        """Handler of 'domain-start' event, starts GUI/AUDIO daemon for
        actual VM"""

        if not self.is_watched(vm) or getattr(vm, "is_preload", False):
            return

        self.xid_cache[vm.name] = vm.xid, vm.stubdom_xid

        try:
            if (
                "guivm" in self.enabled_services
                and getattr(vm, "guivm", None) == vm.app.local_name
                and vm.features.check_with_template("gui", True)
                and kwargs.get("start_guid", "True") == "True"
            ):
                asyncio.ensure_future(self.start_gui_for_vm(vm))
        except qubesadmin.exc.QubesException as e:
            vm.log.warning("Failed to start GUI for %s: %s", vm.name, str(e))

        try:
            if (
                "audiovm" in self.enabled_services
                and getattr(vm, "audiovm", None) == vm.app.local_name
                and vm.features.check_with_template("audio", True)
                and kwargs.get("start_audio", "True") == "True"
            ):
                asyncio.ensure_future(self.start_audio_for_vm(vm))
        except qubesadmin.exc.QubesException as e:
            vm.log.warning("Failed to start AUDIO for %s: %s", vm.name, str(e))

    def on_connection_established(self, _subject, _event, **_kwargs):
        """Handler of 'connection-established' event, used to launch GUI/AUDIO
        daemon for domains started before this tool."""
        monitor_layout = get_monitor_layout()
        self.app.domains.clear_cache()
        for vm in self.app.domains:
            try:
                if (
                    vm.klass == "AdminVM"
                    or not self.is_watched(vm)
                    or getattr(vm, "is_preload", False)
                ):
                    continue

                power_state = vm.get_power_state()
                if power_state == "Running":
                    if "guivm" in self.enabled_services:
                        asyncio.ensure_future(
                            self.start_gui(vm, monitor_layout=monitor_layout)
                        )
                    if "audiovm" in self.enabled_services:
                        asyncio.ensure_future(self.start_audio(vm))
                    self.xid_cache[vm.name] = vm.xid, vm.stubdom_xid
                elif power_state == "Transient":
                    # it is still starting, we'll get 'domain-start'
                    # event when fully started
                    if (
                        "guivm" in self.enabled_services
                        and vm.virt_mode == "hvm"
                    ):
                        asyncio.ensure_future(self.start_gui_for_stubdomain(vm))
            except qubesadmin.exc.QubesDaemonCommunicationError as e:
                vm.log.warning("Failed to handle %s: %s", vm.name, str(e))

    def on_domain_stopped(self, vm, _event, **_kwargs):
        """Handler of 'domain-stopped' event, cleans up"""

        if not self.is_watched(vm):
            return

        # read XID from cache, as stopped domain reports it already as -1
        try:
            xid, stubdom_xid = self.xid_cache[vm.name]
            del self.xid_cache[vm.name]
        except KeyError:
            return
        if xid != -1:
            self.cleanup_guid(xid)
            self.cleanup_pacat_process(xid)
        if stubdom_xid != -1:
            self.cleanup_guid(stubdom_xid)
            self.cleanup_pacat_process(stubdom_xid)

    def on_property_preload_set(self, vm, _event, **_kwargs):
        """Handler of 'property-reset:is_preload' event, used to launch
        GUI/AUDIO daemon after preload is marked as used."""
        if (
            not vm.klass == "DispVM"
            or not self.is_watched(vm)
            or getattr(vm, "is_preload", False)
            or not vm.is_running()
        ):
            return
        monitor_layout = get_monitor_layout()
        try:
            if "guivm" in self.enabled_services:
                asyncio.ensure_future(
                    self.start_gui(vm, monitor_layout=monitor_layout)
                )
            if "audiovm" in self.enabled_services:
                asyncio.ensure_future(self.start_audio(vm))
            self.xid_cache[vm.name] = vm.xid, vm.stubdom_xid
        except qubesadmin.exc.QubesDaemonCommunicationError as e:
            vm.log.warning("Failed to handle %s: %s", vm.name, str(e))

    def on_property_audiovm_set(self, vm, event, **kwargs):
        """Handler for catching event related to dynamic AudioVM set/unset"""
        if vm.name not in self.xid_cache:
            try:
                self.xid_cache[vm.name] = vm.xid, vm.stubdom_xid
            except qubesadmin.exc.QubesDaemonAccessError as e:
                log.error("vm.name: failed to determine XID: %s", str(e))
                return
        xid, stubdom_xid = self.xid_cache[vm.name]
        # We ensure that on_property_audiovm_set is really called with
        # newvalue as kwarg to not fallback all the times to vm.audiovm
        # in order to have newvalue=None as requested.
        if "newvalue" in kwargs:  # pylint: disable=consider-using-get
            newvalue = kwargs["newvalue"]
        else:
            newvalue = str(getattr(vm, "audiovm", None))
        if newvalue != self.app.local_name:
            if xid != -1:
                self.cleanup_pacat_process(xid)
            if stubdom_xid != -1:
                self.cleanup_pacat_process(stubdom_xid)
            try:
                if not os.path.exists(self.guid_pidfile(xid)):
                    del self.xid_cache[vm.name]
            except KeyError:
                return
        if (
            event in ["property-set:audiovm", "property-reset:audiovm"]
            and newvalue == self.app.local_name
            and vm.get_power_state() == "Running"
        ):
            asyncio.ensure_future(self.start_audio(vm))

    def cleanup_guid(self, xid):
        """
        Clean up after qubes-guid.

        Removes the auto-generated configuration file, if any.
        """

        config_path = self.guid_config_file(xid)
        if os.path.exists(config_path):
            os.unlink(config_path)

    def cleanup_pacat_process(self, xid):
        """
        Clean up after pacat-simple-vchan.

        Removes the auto-generated configuration file, if any.
        """
        config_file = self.pacat_pidfile(xid)
        if not os.path.exists(config_file):
            return
        try:
            with open(config_file, encoding="ascii") as fd:
                pid = int(fd.readline())
                os.kill(pid, signal.SIGTERM)
                log.info(
                    "Sent SIGTERM signal to pacat-simple-vchan process %d", pid
                )
        except OSError:
            log.error(
                "Failed to send SIGTERM signal for the"
                " pacat-simple-vchan with xid of %s",
                xid,
            )
        os.unlink(config_file)

    def register_events(self, events):
        """Register domain startup events in app.events dispatcher"""
        events.add_handler("domain-spawn", self.on_domain_spawn)
        events.add_handler("domain-start", self.on_domain_start)
        events.add_handler(
            "connection-established", self.on_connection_established
        )
        events.add_handler("domain-stopped", self.on_domain_stopped)
        events.add_handler(
            "property-reset:is_preload", self.on_property_preload_set
        )

        for event in [
            "property-set:audiovm",
            "property-pre-set:audiovm",
            "property-pre-reset:audiovm",
            "property-reset:audiovm",
        ]:
            events.add_handler(event, self.on_property_audiovm_set)

    def is_watched(self, vm):
        """
        Should we watch this VM for changes
        """

        if self.app.local_name == vm.name:
            return False
        if self.vm_names is None:
            return True
        return vm.name in self.vm_names


def main():
    """Main function of qvm-start-daemon tool"""

    only_if_service_enabled = ["guivm", "audiovm"]
    enabled_services = [
        service
        for service in only_if_service_enabled
        if os.path.exists(f"/var/run/qubes-service/{service}")
    ]

    if "--force" in sys.argv or os.path.exists("/etc/qubes-release"):
        enabled_services = only_if_service_enabled

    if not enabled_services:
        log.info("None of 'audiovm' nor 'guivm' service is enabled")
        log.info(parser.format_help())
        return

    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    if args.watch and args.notify_monitor_layout:
        parser.error("--watch cannot be used with --notify-monitor-layout")

    if args.all_domains:
        vm_names = None
    else:
        vm_names = [vm.name for vm in args.domains]

    launcher = DAEMONLauncher(
        app=args.app,
        enabled_services=enabled_services,
        vm_names=vm_names,
        kde=args.kde,
    )

    if args.watch:
        # Start qubes running before we started qvm-start-daemon
        for qube in launcher.app.domains:
            if qube.name == "dom0":
                continue

        fd = os.open(args.pidfile, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "r+") as lock_f:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    pid = int(lock_f.read().strip())
                except ValueError:
                    pid = "unknown"
                log.error(
                    "Another daemon launcher process (with PID %d)"
                    " is already running",
                    pid,
                )
                sys.exit(1)
            print(os.getpid(), file=lock_f)
            lock_f.flush()
            lock_f.truncate()
            loop = asyncio.get_event_loop()
            # pylint: disable=no-member
            events = qubesadmin.events.EventsDispatcher(args.app)
            # pylint: enable=no-member
            launcher.register_events(events)

            events_listener = asyncio.ensure_future(events.listen_for_events())

            for signame in ("SIGINT", "SIGTERM"):
                loop.add_signal_handler(
                    getattr(signal, signame), events_listener.cancel
                )  # pylint: disable=no-member

            loop.add_signal_handler(
                signal.SIGHUP, launcher.send_monitor_layout_all
            )

            if "guivm" in enabled_services:
                conn = xcffib.connect()
                x_watcher = XWatcher(conn, args.app)
                x_fd = conn.get_file_descriptor()
                loop.add_reader(
                    x_fd, x_watcher.event_reader, events_listener.cancel
                )
                x_watcher.update_keyboard_layout()

            try:
                loop.run_until_complete(events_listener)
            except asyncio.CancelledError:
                pass

            if "guivm" in enabled_services:
                loop.remove_reader(x_fd)

            loop.stop()
            loop.run_forever()
            loop.close()
    elif args.notify_monitor_layout:
        try:
            with open(pidfile_path, "r", encoding="ascii") as pidfile:
                pid = int(pidfile.read().strip())
            os.kill(pid, signal.SIGHUP)
        except (FileNotFoundError, ValueError) as e:
            parser.error(f"Cannot open pidfile {pidfile_path}: {str(e)}")
    else:
        loop = asyncio.get_event_loop()
        tasks = []
        for vm in args.domains:
            if vm.is_running():
                if "guivm" in enabled_services:
                    tasks.append(
                        asyncio.ensure_future(
                            launcher.start_gui(
                                vm, force_stubdom=args.force_stubdomain
                            )
                        )
                    )
                if "audiovm" in enabled_services:
                    tasks.append(
                        asyncio.ensure_future(launcher.start_audio(vm))
                    )
        if tasks:
            loop.run_until_complete(asyncio.wait(tasks))
        loop.stop()
        loop.run_forever()
        loop.close()


if __name__ == "__main__":
    main()
