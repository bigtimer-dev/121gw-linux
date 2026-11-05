"""The "Companion" look: near-black, warm grey text and the meter's yellow as the one accent colour, after zombu4's
121GW Companion (ui/index.html of his companion branch). Applied over libadwaita's dark style; "System" leaves the
desktop's own light/dark and accent colour."""

from __future__ import annotations

from gi.repository import Adw, Gdk, Gtk

BG, SURFACE, RAISED, RULE, RULE_HI = "#0c0c0b", "#141412", "#1b1b18", "#262622", "#3a3a34"
INK, DIM, FAINT = "#e9e7df", "#8d8a80", "#55534c"
ACC, ACC_DIM = "#f2c200", "#7a6200"
ACCENT_RGB = (0xF2 / 255, 0xC2 / 255, 0x00 / 255)

CSS = f"""
:root {{
  --window-bg-color: {BG}; --window-fg-color: {INK};
  --view-bg-color: {BG}; --view-fg-color: {INK};
  --headerbar-bg-color: {BG}; --headerbar-fg-color: {INK}; --headerbar-backdrop-color: {BG};
  --headerbar-border-color: {RULE}; --headerbar-shade-color: {RULE};
  --sidebar-bg-color: {SURFACE}; --secondary-sidebar-bg-color: {SURFACE};
  --card-bg-color: {SURFACE}; --card-fg-color: {INK}; --card-shade-color: {RULE};
  --dialog-bg-color: {SURFACE}; --dialog-fg-color: {INK};
  --popover-bg-color: {RAISED}; --popover-fg-color: {INK};
  --thumbnail-bg-color: {RAISED};
  --accent-bg-color: {ACC}; --accent-fg-color: {BG}; --accent-color: {ACC};
  --border-color: {RULE_HI};
}}
.card, list.boxed-list, .boxed-list {{ border: 1px solid {RULE}; box-shadow: none; }}
.boxed-list > row {{ border-color: {RULE}; }}
headerbar {{ box-shadow: inset 0 -1px {RULE}; }}
viewswitcher button:checked {{ color: {ACC}; }}
viewswitcher button:checked label {{ color: {ACC}; }}
.title-2, .title-3, .title-4 {{ letter-spacing: 0.04em; }}
.heading {{ text-transform: uppercase; letter-spacing: 0.14em; font-size: 9pt; color: {DIM}; }}
preferencesgroup > box > box > .heading {{ color: {DIM}; }}
.dim-label {{ color: {DIM}; opacity: 1; }}
button.suggested-action {{ background-color: transparent; color: {ACC}; box-shadow: inset 0 0 0 1px {ACC}; }}
button.suggested-action:hover {{ background-color: {ACC}; color: {BG}; }}
button.destructive-action {{ background-color: transparent; color: #ff6b5a; box-shadow: inset 0 0 0 1px #ff6b5a; }}
button.destructive-action:hover {{ background-color: #ff6b5a; color: {BG}; }}
progressbar > trough > progress, levelbar > trough > block.filled {{ background-color: {ACC}; }}
levelbar > trough > block.empty {{ background-color: {RULE}; }}
banner > revealer > widget {{ background-color: alpha({ACC}, 0.14); color: {INK}; }}
.lcd {{
  background-color: #11110f; color: {INK}; border: 1px solid {RULE_HI}; box-shadow: none; border-radius: 4px;
}}
.lcd.dimmed {{ background-color: {BG}; }}
.lcd .ann {{ color: alpha({INK}, 0.12); }}
.lcd .ann.on {{ color: {ACC}; }}
.lcd .sign.off {{ color: alpha({INK}, 0.08); }}
.lcd .mode-name, .lcd .subtitle, .lcd .lcdstatus {{ color: {DIM}; }}
.lcd .tag {{ color: {FAINT}; }}
.lcd .tag.on {{ background-color: {ACC}; color: {BG}; }}
.lcd separator {{ background-color: {RULE_HI}; }}
.meter-key.pressed {{ background-color: alpha({ACC}, 0.35); }}
.stat-tile {{ background-color: {SURFACE}; border: 1px solid {RULE}; border-radius: 2px; }}
.stat-title {{ text-transform: uppercase; letter-spacing: 0.12em; font-size: 8pt; color: {DIM}; opacity: 1; }}
.stat-value {{ color: {INK}; }}
.accent-text {{ color: {ACC}; }}
.card-file-protected {{ color: {DIM}; }}
"""

_provider: Gtk.CssProvider | None = None
active = False


def apply(name: str) -> None:
    """name 'companion' or 'system'."""
    global _provider, active
    display = Gdk.Display.get_default()
    if _provider is not None:
        Gtk.StyleContext.remove_provider_for_display(display, _provider)
        _provider = None
    active = name == "companion"
    manager = Adw.StyleManager.get_default()
    if active:
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        _provider = Gtk.CssProvider()
        _provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    else:
        manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
