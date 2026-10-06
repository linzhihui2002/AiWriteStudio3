"""Persistent, book-wide setting-object colours; no colours enter author text."""
from __future__ import annotations

import colorsys
import re

LEGACY = {
    "highlight-1": {"light": "#3d5a80", "dark": "#86a9d1", "paper": "#34506f"},
    "highlight-2": {"light": "#9c4034", "dark": "#d98679", "paper": "#8a3a2f"},
    "highlight-3": {"light": "#3f7357", "dark": "#85b79a", "paper": "#3a6449"},
    "highlight-4": {"light": "#8a6417", "dark": "#d8b268", "paper": "#7a5713"},
    "highlight-5": {"light": "#6b4b7a", "dark": "#b391c4", "paper": "#5c4168"},
}
EMPTY_COLORS = {"light": "", "dark": "", "paper": ""}


def _luminance(rgb):
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * w for v, w in zip(values, (.2126, .7152, .0722)))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in rgb)


def _theme_color(hue, saturation, target):
    lo, hi = 0., 1.
    for _ in range(24):
        lightness = (lo + hi) / 2
        rgb = colorsys.hls_to_rgb(hue, lightness, saturation)
        if _luminance(rgb) < target:
            lo = lightness
        else:
            hi = lightness
    return _hex(colorsys.hls_to_rgb(hue, (lo + hi) / 2, saturation))


def colors_for(color):
    if color in LEGACY:
        return dict(LEGACY[color])
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(color)):
        raise ValueError("请选择有效颜色")
    rgb = tuple(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))
    hue, _, saturation = colorsys.rgb_to_hls(*rgb)
    # Use the chosen hue, with readable foreground luminance in each theme.
    saturation = max(.35, saturation)
    return {"light": _theme_color(hue, saturation, .105),
            "dark": _theme_color(hue, saturation, .49),
            "paper": _theme_color(hue, saturation, .085)}


def allocate(meta):
    """Never recycle a slot, including inactive objects and manual choices."""
    used = {theme: set() for theme in EMPTY_COLORS}
    for item in meta.get("entities", {}).values():
        for key in ("auto_colors", "highlight_colors"):
            for theme, color in (item.get(key) or {}).items():
                if theme in used and color:
                    used[theme].add(color.lower())
    for value in (meta.get("highlights") or {}).values():
        color = value.get("color") if isinstance(value, dict) else value
        try:
            for theme, color in colors_for(color).items():
                used[theme].add(color)
        except (ValueError, TypeError):
            pass
    slot = int(meta.get("next_color", 0))
    while True:
        hue = (slot * .3819660112501051 + .59) % 1
        saturation = (.68, .82, .52)[(slot // 24) % 3]
        colors = {"light": _theme_color(hue, saturation, .105),
                  "dark": _theme_color(hue, saturation, .49),
                  "paper": _theme_color(hue, saturation, .085)}
        slot += 1
        if all(color not in used[theme] for theme, color in colors.items()):
            meta["next_color"] = slot
            return colors


def apply_mode(entity, mode, color=""):
    entity["highlight_mode"] = mode
    if mode == "off":
        entity.update(highlight="", highlight_colors=dict(EMPTY_COLORS))
    elif mode == "auto":
        colors = dict(entity["auto_colors"])
        entity.update(highlight=colors["light"], highlight_colors=colors)
    else:
        colors = colors_for(color)
        entity.update(highlight=color.lower() if color.startswith("#") else color,
                      highlight_colors=colors)


def palette():
    return [*LEGACY]
