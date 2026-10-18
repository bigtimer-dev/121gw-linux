"""Application entry point."""

from __future__ import annotations

import sys

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __version__
from .lcd import CSS
from .sessions import SessionStore
from .settings import Settings

APP_ID = "io.github.bigtimer_dev.Meter121GW"


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.add_main_option("demo", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Use a simulated 121GW instead of Bluetooth", None)
        self.demo = False
        self.link = None
        self.window = None
        GLib.set_application_name("121GW")

    def do_handle_local_options(self, options) -> int:
        self.demo = options.contains("demo")
        if self.demo:
            # separate instance id so a demo can run next to the real app
            self.set_application_id(APP_ID + ".Demo")
        return -1

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb, accels in (("quit", lambda *_: self.quit(), ["<Ctrl>q"]),
                                 ("about", lambda *_: self._about(), [])):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self) -> None:
        if self.window is None:
            from .window import MainWindow
            settings = Settings()
            if self.demo:
                from .demo import DemoLink
                self.link = DemoLink(settings)
            else:
                from .bluez import BluezLink
                self.link = BluezLink(settings)
            self.window = MainWindow(self, self.link, settings, SessionStore())
            if self.demo:
                self.window.set_title("121GW (demo)")
            self.link.start()
        self.window.present()

    def do_shutdown(self) -> None:
        if self.link:
            self.link.shutdown()
        Adw.Application.do_shutdown(self)

    def _about(self) -> None:
        about = Adw.AboutDialog(
            application_name="121GW",
            application_icon=APP_ID,
            version=__version__,
            developer_name="121GW Linux port",
            comments="Linux companion for the EEVblog 121GW multimeter: live LCD, remote keys, trend logging, "
                     "sessions, CSV/graph export and continuity beeper.\n\nPorted from the EEVblog app-121gw and "
                     "the evotronix iOS / Windows / Android / Chrome ports.",
            license_type=Gtk.License.MIT_X11,
            website="https://gitlab.com/eevblog/app-121gw",
        )
        about.add_link("evotronix ports", "https://github.com/evotronix")
        about.present(self.window)


def main() -> int:
    return App().run(sys.argv)
