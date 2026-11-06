"""The "Companion" look: near-black, warm grey text and the meter's yellow as the one accent colour, after zombu4's
121GW Companion (ui/index.html of his companion branch). Applied over libadwaita's dark style; "System" leaves the
desktop's own light/dark and accent colour."""

from __future__ import annotations

from gi.repository import Adw, Gdk, Gtk

BG, SURFACE, RAISED, RULE, RULE_HI = "#0c0c0b", "#141412", "#1b1b18", "#262622", "#3a3a34"
INK, DIM, FAINT = "#e9e7df", "#8d8a80", "#55534c"
ACC, ACC_DIM = "#f2c200", "#7a6200"
ACCENT_RGB = (0xF2 / 255, 0xC2 / 255, 0x00 / 255)

# "purple": the same minimal theme in violet, on a black with a hint of violet
PURPLE = {ACC: "#b48cff", BG: "#0c0a10", SURFACE: "#15121c", RAISED: "#1c1825", INK: "#ece8f6", DIM: "#8f88a3",
          FAINT: "#565066", "#ff7a6b": "#ff7a9c"}
THEMES = ("companion", "purple", "system")


def _rgb(hexcolor: str) -> tuple[float, float, float]:
    return tuple(int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))

CSS = f"""
:root {{
  --window-bg-color: {BG}; --window-fg-color: {INK};
  --view-bg-color: {BG}; --view-fg-color: {INK};
  --headerbar-bg-color: {BG}; --headerbar-fg-color: {INK}; --headerbar-backdrop-color: {BG};
  --headerbar-border-color: transparent; --headerbar-shade-color: transparent;
  --sidebar-bg-color: {BG}; --secondary-sidebar-bg-color: {BG};
  --card-bg-color: transparent; --card-fg-color: {INK}; --card-shade-color: transparent;
  --dialog-bg-color: {SURFACE}; --dialog-fg-color: {INK};
  --popover-bg-color: {SURFACE}; --popover-fg-color: {INK};
  --thumbnail-bg-color: {SURFACE};
  --accent-bg-color: {ACC}; --accent-fg-color: {BG}; --accent-color: {ACC};
  --border-color: alpha(white, 0.06);
}}
window, popover, dialog {{ font-family: "Adwaita Sans", "Inter", sans-serif; }}

/* no boxes: content sits on the background, rows split by hairlines */
.card, list.boxed-list, .boxed-list {{ background: none; border: none; box-shadow: none; border-radius: 0; }}
.boxed-list > row {{ background: none; border-color: alpha(white, 0.05); border-radius: 0; }}
.boxed-list > row:hover {{ background-color: alpha(white, 0.025); }}
popover > contents {{ border-radius: 12px; border: 1px solid alpha(white, 0.06); box-shadow: 0 10px 30px alpha(black, 0.5);
  padding: 4px; }}
headerbar {{ box-shadow: none; }}
viewswitcher button {{ background: none; box-shadow: none; color: {FAINT}; transition: color 150ms ease; }}
viewswitcher button:hover {{ color: {DIM}; background: none; }}
viewswitcher button:checked {{ background: none; color: {INK}; }}
viewswitcher button:checked label, viewswitcher button:checked image {{ color: {INK}; }}

/* type */
.title-1, .title-2, .title-3, .title-4 {{ font-weight: 600; letter-spacing: -0.01em; }}
.heading, preferencesgroup > box > box > .heading {{ font-weight: 500; font-size: 9pt; color: {DIM}; }}
.dim-label {{ color: {DIM}; opacity: 1; }}
.card-title {{ font-size: 9pt; font-weight: 500; color: {DIM}; opacity: 1; }}
.caption {{ font-size: 8.5pt; }}

/* buttons: text only until hovered */
button {{ border-radius: 8px; box-shadow: none; transition: background-color 120ms ease, color 120ms ease; }}
button:not(.flat):not(.suggested-action):not(.destructive-action) {{ background-color: alpha(white, 0.04); }}
button:hover {{ background-color: alpha(white, 0.07); }}
button.pill {{ border-radius: 999px; padding: 8px 26px; font-weight: 500; }}
button.suggested-action {{ background: none; color: {ACC}; box-shadow: inset 0 0 0 1px alpha({ACC}, 0.5); }}
button.suggested-action:hover {{ background-color: alpha({ACC}, 0.10); }}
button.destructive-action {{ background: none; color: #ff7a6b; box-shadow: inset 0 0 0 1px alpha(#ff7a6b, 0.45); }}
button.destructive-action:hover {{ background-color: alpha(#ff7a6b, 0.10); }}
progressbar > trough, levelbar > trough {{ border-radius: 999px; min-height: 3px; background-color: alpha(white, 0.06); }}
progressbar > trough > progress, levelbar > trough > block.filled {{ background-color: {INK}; border-radius: 999px; }}
levelbar > trough > block.empty {{ background-color: alpha(white, 0.06); }}
banner > revealer > widget {{ background: none; color: {ACC}; }}

/* status line */
.chip {{ font-size: 8.5pt; font-weight: 500; color: {FAINT}; font-feature-settings: "tnum"; }}
.chip.live {{ color: {DIM}; }}
.chip.error {{ color: #ff7a6b; }}
.device-name {{ font-size: 8.5pt; font-weight: 500; color: {DIM}; }}

/* the readout: one big thin number */
.lcd {{ background: none; border: none; box-shadow: none; padding: 18px 4px 10px 4px; color: {INK}; }}
.lcd.dimmed {{ background: none; opacity: 0.5; }}
.lcd .digits {{ font-family: "Adwaita Sans", "Inter", sans-serif; font-weight: 200; font-size: 72pt;
  letter-spacing: -0.03em; font-feature-settings: "tnum"; color: {INK}; }}
.lcd .sign {{ font-family: "Adwaita Sans", sans-serif; font-weight: 200; font-size: 50pt; }}
.lcd .sign.off {{ color: transparent; }}
.lcd .unit {{ font-family: "Adwaita Sans", sans-serif; font-weight: 300; font-size: 24pt; color: {DIM}; }}
.lcd .subreading {{ font-family: "Adwaita Sans", sans-serif; font-weight: 300; font-size: 18pt;
  font-feature-settings: "tnum"; color: {INK}; }}
.lcd .ann, .lcd .tag {{ font-family: "Adwaita Sans", sans-serif; font-weight: 600; font-size: 8pt;
  letter-spacing: 0.06em; color: transparent; background: none; padding: 0 2px; }}
.lcd .ann.on, .lcd .tag.on {{ color: {ACC}; background: none; }}
.lcd .mode-name, .lcd .subtitle, .lcd .lcdstatus {{ font-family: "Adwaita Sans", sans-serif; font-weight: 500;
  font-size: 8.5pt; color: {DIM}; }}
.lcd separator {{ background: none; min-height: 0; }}

/* keys: a word each; the ▾ appears on hover */
.key-cell {{ border-radius: 10px; transition: background-color 120ms ease; }}
.key-cell:hover {{ background-color: alpha(white, 0.04); }}
.key-cell button, .key-cell button:hover, .key-cell button:not(.flat):not(.suggested-action):not(.destructive-action) {{
  background: none; box-shadow: none; border: none; }}
.key-cell .meter-key {{ min-height: 46px; font-weight: 500; font-size: 10pt; letter-spacing: 0.04em; color: {INK}; }}
.key-cell .meter-key.pressed {{ color: {ACC}; }}
.key-cell .meter-key .arrow {{ font-size: 8pt; font-weight: 400; color: {FAINT}; opacity: 1; }}
.key-cell.active .meter-key .arrow {{ color: {ACC}; }}
.key-cell .meter-key-menu > button {{ min-width: 22px; min-height: 46px; padding: 0 2px; opacity: 0;
  color: {DIM}; transition: opacity 120ms ease; }}
.key-cell:hover .meter-key-menu > button, .key-cell .meter-key-menu > button:checked,
.key-cell .meter-key-menu > button:focus {{ opacity: 1; }}
.meter-key.active {{ box-shadow: none; }}
popover .key-action {{ border-radius: 8px; padding: 6px 8px; background: none; }}
popover .key-action:hover {{ background-color: alpha(white, 0.05); }}
popover list.navigation-sidebar {{ background: none; }}
popover list.navigation-sidebar > row {{ border-radius: 8px; padding: 3px 8px; min-height: 28px; }}
popover list.navigation-sidebar > row:hover {{ background-color: alpha(white, 0.05); }}
popover separator {{ background-color: alpha(white, 0.05); }}

/* statistics: label over number, nothing else */
.stat-tile {{ background: none; border: none; padding: 4px 2px; }}
.stat-title {{ text-transform: none; letter-spacing: 0.02em; font-size: 8pt; font-weight: 500; color: {FAINT}; opacity: 1; }}
.stat-value {{ font-family: "Adwaita Sans", sans-serif; font-weight: 300; font-size: 14pt; color: {INK};
  font-feature-settings: "tnum"; }}
.mono {{ font-family: "Adwaita Sans", sans-serif; font-feature-settings: "tnum"; }}
.accent-text {{ color: {ACC}; }}
.card-file-protected {{ color: {DIM}; }}
"""

_provider: Gtk.CssProvider | None = None
active = False


def apply(name: str) -> None:
    """name 'companion' (yellow), 'purple' or 'system'."""
    global _provider, active, ACCENT_RGB
    display = Gdk.Display.get_default()
    if _provider is not None:
        Gtk.StyleContext.remove_provider_for_display(display, _provider)
        _provider = None
    active = name in ("companion", "purple")
    css = CSS
    if name == "purple":
        for old, new in PURPLE.items():
            css = css.replace(old, new)
    ACCENT_RGB = _rgb(PURPLE[ACC] if name == "purple" else ACC)
    manager = Adw.StyleManager.get_default()
    if active:
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        _provider = Gtk.CssProvider()
        _provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    else:
        manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
