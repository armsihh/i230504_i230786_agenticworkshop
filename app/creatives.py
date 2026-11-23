"""Ad creatives as SVG banners, drawn from the ad copy. No image model: free and instant."""
import re
import textwrap
from html import escape as _escape

SIZES = {"square": (1080, 1080), "story": (1080, 1920), "landscape": (1200, 628)}
THEMES = {
    "bold":    {"bg": "#111827", "fg": "#ffffff", "accent": "#f59e0b"},
    "fresh":   {"bg": "#ecfdf5", "fg": "#064e3b", "accent": "#10b981"},
    "minimal": {"bg": "#ffffff", "fg": "#111827", "accent": "#2563eb"},
    "warm":    {"bg": "#fff7ed", "fg": "#7c2d12", "accent": "#ea580c"},
}

# Characters XML 1.0 forbids: control characters, lone surrogates, U+FFFE/U+FFFF
_XML_INVALID = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\ud800-\\udfff\\ufffe\\uffff]")


def escape(text: str) -> str:
    """HTML-escape and drop characters XML forbids (one of them makes the whole SVG unreadable)."""
    return _escape(_XML_INVALID.sub("", text))


def render_svg(headline: str, body: str, cta: str, size: str = "square", theme: str = "bold") -> str:
    w, h = SIZES.get(size, SIZES["square"])
    t = THEMES.get(theme, THEMES["bold"])
    pad = w * 0.08
    head_px, body_px = round(w * 0.075), round(w * 0.038)
    head_lines = textwrap.wrap(headline, width=max(10, int((w - 2 * pad) / (head_px * 0.55))))[:3]
    body_lines = textwrap.wrap(body, width=max(15, int((w - 2 * pad) / (body_px * 0.52))))[:4]

    y = h * 0.30
    parts = [f'<rect width="{w}" height="{h}" fill="{t["bg"]}"/>',
             f'<rect x="{pad}" y="{h * 0.2}" width="{w * 0.12}" height="{w * 0.012}" fill="{t["accent"]}"/>']
    for line in head_lines:
        y += head_px * 1.15
        parts.append(f'<text x="{pad}" y="{y:.0f}" font-size="{head_px}" font-weight="800" '
                     f'fill="{t["fg"]}">{escape(line)}</text>')
    y += body_px
    for line in body_lines:
        y += body_px * 1.4
        parts.append(f'<text x="{pad}" y="{y:.0f}" font-size="{body_px}" fill="{t["fg"]}" '
                     f'opacity="0.85">{escape(line)}</text>')
    bw, bh = w * 0.5, body_px * 2.4
    by = h - pad - bh
    parts.append(f'<rect x="{pad}" y="{by:.0f}" width="{bw:.0f}" height="{bh:.0f}" rx="{bh / 2:.0f}" '
                 f'fill="{t["accent"]}"/>')
    parts.append(f'<text x="{pad + bw / 2:.0f}" y="{by + bh * 0.63:.0f}" font-size="{body_px}" font-weight="700" '
                 f'text-anchor="middle" fill="#ffffff">{escape(cta)}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'font-family="Inter, Helvetica, Arial, sans-serif">{"".join(parts)}</svg>')
