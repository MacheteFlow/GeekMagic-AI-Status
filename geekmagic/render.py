"""Renders the 240x240 frames shown on the SmallTV-Ultra.

Layout, top to bottom:
    provider   small, muted tint
    model      large, centred, wraps or shrinks to fit
    status     medium
    bars       usage of the 5-hour and, if available, the weekly window

The background colour encodes the state, so it reads at a glance from across
the room without having to read the text at all.

A note on caching: the filename is derived from everything visible on the
frame, so usage percentages are rounded to steps (5% by default). Without that,
every single percentage point would produce a new file and we would rewrite the
ESP8266's flash memory continuously.
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

SIZE = 240

# Bumping this invalidates every frame already uploaded to the device. Needed
# whenever the artwork changes without the data behind it changing, since the
# filename is derived from the data alone.
RENDER_VERSION = 7

# Rounding step for the usage percentages, in points. At 1 the bars show the
# real figure. See the note in frame_key about what that costs.
USAGE_BUCKET = 1

# The mark drawn beside the status word.
#
# "check" is drawn with lines rather than typed, because the obvious character
# for it (U+2713) is not in Consolas and would come out as an empty box. The
# rest are plain ASCII, present in every monospace font.
#
# working cycles through the four, which is what makes the spinner: the frames
# become an animated GIF, so the device loops it by itself and nothing is
# rewritten while it spins.
STATUS_GLYPHS: dict[str, tuple[str, ...]] = {
    "idle": ("check",),
    "waiting": ("?",),
    "working": ("/", "-", "\\", "|"),
    "error": ("×",),
}

# Milliseconds per spinner frame.
SPINNER_MS = 130


@dataclass(frozen=True)
class Style:
    bg: tuple[int, int, int]
    fg: tuple[int, int, int]
    dim: tuple[int, int, int]
    track: tuple[int, int, int]


# Tones picked for a TFT IPS panel: saturated but not blown out, text always
# readable against the background.
STYLES: dict[str, Style] = {
    "working": Style((214, 106, 16), (255, 255, 255), (255, 226, 190), (138, 66, 6)),
    "waiting": Style((186, 32, 32), (255, 255, 255), (255, 208, 208), (116, 14, 14)),
    "idle": Style((126, 191, 74), (255, 255, 255), (232, 250, 214), (76, 124, 40)),
    "error": Style((90, 26, 120), (255, 255, 255), (228, 200, 245), (54, 12, 74)),
}
DEFAULT_STYLE = STYLES["idle"]

# Monospace fonts in order of preference. The first one that exists wins.
FONT_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "fonts", "PixelOperatorMono.ttf"),
    r"C:\Windows\Fonts\consolab.ttf",
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\lucon.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]

_font_cache: dict[tuple[str, int], object] = {}


def _font_path(override: str | None = None) -> str | None:
    for path in ([override] if override else []) + FONT_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def _font(size: int, override: str | None = None):
    path = _font_path(override)
    if path is None:
        return ImageFont.load_default()
    key = (path, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, size)
    return _font_cache[key]


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _cap_box(draw: ImageDraw.ImageDraw, font) -> tuple[int, int]:
    """Where a capital letter actually sits, as (offset from the draw origin,
    height).

    PIL places text by the top of its line box, not by the top of the letters,
    so anything drawn by hand next to a word has to account for that gap or it
    ends up sitting lower than the text it belongs to.
    """
    _, top, _, bottom = draw.textbbox((0, 0), "M", font=font)
    return top, bottom - top


def _wrap(text: str, max_chars: int) -> list[str]:
    """Break on the separators typical of model names ('-', '_', '.', space)."""
    if len(text) <= max_chars:
        return [text]
    lines, current, token = [], "", ""
    for ch in text:
        token += ch
        if ch in "-_. /":
            if len(current) + len(token) > max_chars and current:
                lines.append(current)
                current = token
            else:
                current += token
            token = ""
    if len(current) + len(token) > max_chars and current:
        lines.append(current)
        current = token
    else:
        current += token
    if current:
        lines.append(current)
    out: list[str] = []
    for line in lines:  # hard cut if a single token is still too long
        while len(line) > max_chars:
            out.append(line[:max_chars])
            line = line[max_chars:]
        out.append(line)
    return [l for l in out if l]


def _fit_block(draw, text, max_w, max_h, sizes, font_override, min_single=14):
    """Decide how to lay the text out inside the given box.

    At comparable legibility a single line reads far better than two larger
    ones, so wrapping is avoided while the type stays at least `min_single`.
    Below that threshold it is better to break the line and use a bigger face.
    """
    sizes = list(sizes)
    # Best size found for each possible number of lines.
    per_lines: dict[int, tuple] = {}
    for size in sizes:
        font = _font(size, font_override)
        char_w = max(1, _text_size(draw, "M", font)[0])
        lines = _wrap(text, max(1, max_w // char_w))
        line_h = int(size * 1.25)
        widest = max((_text_size(draw, l, font)[0] for l in lines), default=0)
        if widest <= max_w and line_h * len(lines) <= max_h:
            n = len(lines)
            if n not in per_lines or size > per_lines[n][3]:
                per_lines[n] = (lines, font, line_h, size)

    if not per_lines:
        size = min(sizes)
        font = _font(size, font_override)
        char_w = max(1, _text_size(draw, "M", font)[0])
        return _wrap(text, max(1, max_w // char_w)), font, int(size * 1.25)

    single = per_lines.get(1)
    if single and single[3] >= min_single:
        return single[:3]
    return max(per_lines.values(), key=lambda c: c[3])[:3]


# ---------------------------------------------------------------- usage bars


def bucket(pct: float | None, step: int = USAGE_BUCKET) -> int | None:
    """Snap to the nearest step, so the set of cached frames stays finite.

    Nearest rather than down: rounding 49% to 45% is a visible error for no
    benefit, while 50% is off by one and the cache converges just the same.
    """
    if pct is None:
        return None
    pct = max(0.0, min(100.0, float(pct)))
    return min(100, int(round(pct / step)) * step)


def _draw_bar(draw, y: int, label: str, pct: int, style: Style, font_override) -> None:
    font = _font(13, font_override)
    pad = 10
    lbl_w, lbl_h = _text_size(draw, label, font)
    val = f"{pct}%"
    val_w, _ = _text_size(draw, val, font)

    bar_x0 = pad + lbl_w + 6
    bar_x1 = SIZE - pad - val_w - 6
    bar_h = 11
    if bar_x1 - bar_x0 < 20:  # not enough room: show the number alone
        draw.text((pad, y), f"{label} {val}", font=font, fill=style.dim)
        return

    text_y = y + (bar_h - lbl_h) // 2 - 1
    draw.text((pad, text_y), label, font=font, fill=style.dim)
    draw.text((SIZE - pad - val_w, text_y), val, font=font, fill=style.fg)

    draw.rounded_rectangle([bar_x0, y, bar_x1, y + bar_h], radius=3, fill=style.track)
    filled = int((bar_x1 - bar_x0) * pct / 100)
    if filled > 0:
        draw.rounded_rectangle(
            [bar_x0, y, bar_x0 + max(filled, 4), y + bar_h], radius=3, fill=style.fg
        )


def _draw_check(draw, x: int, y: int, height: int, colour, weight: int) -> None:
    """A tick drawn to match the surrounding text rather than typed.

    Sized from the cap height of the status word and stroked at the same weight,
    so it reads as part of the same lettering even though no glyph exists for it.
    """
    # Inset from the cap box so the stroke's own thickness does not spill past
    # the letters it sits next to.
    draw.line(
        [
            (x, y + height * 0.52),
            (x + height * 0.38, y + height * 0.90),
            (x + height * 0.90, y + height * 0.10),
        ],
        fill=colour, width=weight, joint="curve",
    )


def _usage_buckets(usage: dict | None) -> dict[str, int]:
    """Pull out the already-rounded percentages, skipping absent windows."""
    out: dict[str, int] = {}
    if not usage:
        return out
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key) or {}
        value = bucket(window.get("used_percentage", window.get("pct")))
        if value is not None:
            out[label] = value
    return out


# ---------------------------------------------------------------- frame


def render(
    provider: str,
    model: str,
    status: str,
    usage: dict | None = None,
    font_override: str | None = None,
) -> Image.Image:
    """A single still image: the first frame of the sequence."""
    return render_frames(provider, model, status, usage, font_override)[0]


def render_frames(
    provider: str,
    model: str,
    status: str,
    usage: dict | None = None,
    font_override: str | None = None,
) -> list[Image.Image]:
    """One image per glyph position: a single frame, or four for the spinner."""
    glyphs = STATUS_GLYPHS.get(status.lower(), ())
    if not glyphs:
        return [_render_one(provider, model, status, usage, font_override, None)]
    return [
        _render_one(provider, model, status, usage, font_override, glyph)
        for glyph in glyphs
    ]


def _render_one(
    provider: str,
    model: str,
    status: str,
    usage: dict | None,
    font_override: str | None,
    glyph: str | None,
) -> Image.Image:
    style = STYLES.get(status.lower(), DEFAULT_STYLE)
    img = Image.new("RGB", (SIZE, SIZE), style.bg)
    draw = ImageDraw.Draw(img)

    bars = _usage_buckets(usage)
    inner_w = SIZE - 20

    # --- bars at the bottom: they claim their space first, text gets the rest
    bar_rows = list(bars.items())
    bars_h = len(bar_rows) * 18
    bars_top = SIZE - 8 - bars_h if bar_rows else SIZE
    y = bars_top
    for label, pct in bar_rows:
        _draw_bar(draw, y, label, pct, style, font_override)
        y += 18

    # --- provider, at the top
    top_used = 14
    if provider:
        p_lines, p_font, p_lh = _fit_block(
            draw, provider, inner_w, 38, range(11, 19), font_override
        )
        y = top_used
        for line in p_lines:
            w, _ = _text_size(draw, line, p_font)
            draw.text(((SIZE - w) // 2, y), line, font=p_font, fill=style.dim)
            y += p_lh
        top_used = y

    # --- status, just above the bars, with its mark beside it
    #
    # The mark is laid out as a fixed-width slot whether or not this frame has
    # one, so the word does not shift as the spinner turns.
    s_lines, s_font, s_lh = _fit_block(
        draw, status.upper(), inner_w, 32, range(14, 25), font_override
    )
    char_w, _ = _text_size(draw, "M", s_font)
    cap_top, cap_h = _cap_box(draw, s_font)
    slot = char_w * 2 if glyph else 0     # one space plus one character
    s_h = s_lh * len(s_lines)
    gap = 12 if bar_rows else 20
    y = bars_top - gap - s_h
    status_top = y
    for index, line in enumerate(s_lines):
        w, _ = _text_size(draw, line, s_font)
        left = (SIZE - w - slot) // 2
        draw.text((left, y), line, font=s_font, fill=style.fg)
        # Only the last line carries the mark, so a wrapped word keeps it at the end.
        if glyph and index == len(s_lines) - 1:
            gx = left + w + char_w
            if glyph == "check":
                # Aligned to the cap box of the word beside it, so it sits on
                # the same baseline instead of hanging below.
                weight = max(2, round(cap_h / 6))
                _draw_check(draw, gx, y + cap_top, cap_h, style.fg, weight)
            else:
                draw.text((gx, y), glyph, font=s_font, fill=style.fg)
        y += s_lh

    # --- model, centred in whatever space is left
    avail_h = max(28, status_top - top_used - 10)
    m_lines, m_font, m_lh = _fit_block(
        draw, model or "?", inner_w, avail_h, range(12, 41), font_override
    )
    y = top_used + (avail_h - m_lh * len(m_lines)) // 2
    for line in m_lines:
        w, _ = _text_size(draw, line, m_font)
        draw.text(((SIZE - w) // 2, y), line, font=m_font, fill=style.fg)
        y += m_lh

    return img


def is_animated(status: str) -> bool:
    return len(STATUS_GLYPHS.get(status.lower(), ())) > 1


def render_jpeg(provider, model, status, usage=None, font_override=None,
                quality: int = 88) -> bytes:
    buf = io.BytesIO()
    render(provider, model, status, usage, font_override).save(
        buf, format="JPEG", quality=quality, optimize=True
    )
    return buf.getvalue()


def render_gif(provider, model, status, usage=None, font_override=None) -> bytes:
    """An animated GIF of the whole sequence, looping forever.

    The device plays GIFs on its own, so the spinner costs exactly one upload
    however long it turns. Driving the animation by pushing a frame every tenth
    of a second would write to the flash thousands of times an hour.
    """
    frames = render_frames(provider, model, status, usage, font_override)
    # A flat palette keeps this small: the artwork is a handful of solid colours.
    converted = [f.convert("P", palette=Image.ADAPTIVE, colors=32) for f in frames]
    buf = io.BytesIO()
    converted[0].save(
        buf, format="GIF", save_all=True, append_images=converted[1:],
        duration=SPINNER_MS, loop=0, optimize=True, disposal=1,
    )
    return buf.getvalue()


def render_bytes(provider, model, status, usage=None, font_override=None,
                 quality: int = 88) -> bytes:
    """Whatever this status needs: an animated GIF, or a still JPEG."""
    if is_animated(status):
        return render_gif(provider, model, status, usage, font_override)
    return render_jpeg(provider, model, status, usage, font_override, quality)


def frame_key(provider: str, model: str, status: str, usage: dict | None = None) -> str:
    """Deterministic filename: same contents, same file, so no rewrites.

    With USAGE_BUCKET at 1 the bars carry the real figure, which means a new
    file each time a percentage moves. That is the trade the exact numbers cost:
    a handful of uploads an hour instead of almost none. max_cached_frames keeps
    the pile on the device bounded.
    """
    bars = _usage_buckets(usage)
    parts = [str(RENDER_VERSION), provider, model, status]
    parts += [f"{k}{v}" for k, v in sorted(bars.items())]
    raw = "|".join(parts).lower()
    suffix = ".gif" if is_animated(status) else ".jpg"
    return "ai_" + hashlib.sha1(raw.encode()).hexdigest()[:10] + suffix
