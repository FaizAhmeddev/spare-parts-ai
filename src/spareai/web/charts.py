"""Tiny dependency-free SVG charts (themable through CSS variables,
native hover tooltips through <title>). No JS / CDN needed, so the
dashboard works on an air-gapped plant network."""
from __future__ import annotations

from html import escape


def _fmt(v: float, kind: str) -> str:
    if kind == "money":
        if abs(v) >= 1e6:
            return f"${v / 1e6:.1f}M"
        if abs(v) >= 1e3:
            return f"${v / 1e3:.0f}k"
        return f"${v:,.0f}"
    if kind == "pct":
        return f"{v:.0%}"
    return f"{v:,.0f}"


def hbar(items: list[tuple[str, float]], kind: str = "num", width: int = 520,
         bar_h: int = 22, gap: int = 8, label_w: int = 150, color_var: str = "--series-1") -> str:
    """Horizontal bar chart. items = [(label, value)]."""
    if not items:
        return '<p class="muted">No data</p>'
    vmax = max(v for _, v in items) or 1
    plot_w = width - label_w - 70
    h = len(items) * (bar_h + gap)
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" role="img" '
           f'aria-label="bar chart" preserveAspectRatio="xMinYMin meet">']
    for i, (lab, v) in enumerate(items):
        y = i * (bar_h + gap)
        w = max(plot_w * v / vmax, 2 if v > 0 else 0)
        out.append(f'<g class="bar"><title>{escape(str(lab))}: {_fmt(v, kind)}</title>'
                   f'<rect x="0" y="{y}" width="{width}" height="{bar_h + gap}" fill="transparent"/>'
                   f'<text x="{label_w - 8}" y="{y + bar_h * 0.7}" text-anchor="end" class="axis">{escape(str(lab))}</text>'
                   f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" '
                   f'style="fill:var({color_var})"/>'
                   f'<text x="{label_w + w + 6:.1f}" y="{y + bar_h * 0.7}" class="val">{_fmt(v, kind)}</text></g>')
    out.append("</svg>")
    return "".join(out)


def demand_chart(hist: list[tuple[str, float]], fc: list[tuple[str, float, float]],
                 width: int = 860, height: int = 240) -> str:
    """Monthly history (bars) + forecast (unplanned bars stacked with planned).

    hist: [(month, qty)] ; fc: [(month, unplanned, planned)]
    """
    months = [m for m, _ in hist] + [m for m, _, _ in fc]
    if not months:
        return '<p class="muted">No demand history</p>'
    vals = [q for _, q in hist] + [u + p for _, u, p in fc]
    vmax = max(max(vals), 1)
    left, bottom, top = 36, 28, 10
    pw, ph = width - left - 10, height - bottom - top
    step = pw / len(months)
    bw = max(step - 2, 1)
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="demand history and forecast">']
    for frac in (0, 0.5, 1):
        y = top + ph * (1 - frac)
        out.append(f'<line x1="{left}" x2="{width - 10}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="axis">{vmax * frac:.0f}</text>')
    split_x = left + step * len(hist)
    out.append(f'<line x1="{split_x:.1f}" x2="{split_x:.1f}" y1="{top}" y2="{top + ph}" class="split"/>'
               f'<text x="{split_x + 4:.1f}" y="{top + 10}" class="axis">forecast →</text>')
    for i, (m, q) in enumerate(hist):
        x = left + i * step
        hgt = ph * q / vmax
        out.append(f'<g><title>{m}: {q:.0f} issued</title><rect x="{x:.1f}" y="{top}" width="{step:.1f}" '
                   f'height="{ph}" fill="transparent"/><rect x="{x + 1:.1f}" y="{top + ph - hgt:.1f}" '
                   f'width="{bw:.1f}" height="{hgt:.1f}" rx="2" style="fill:var(--series-1)"/></g>')
    for j, (m, u, p) in enumerate(fc):
        x = left + (len(hist) + j) * step
        hu, hp = ph * u / vmax, ph * p / vmax
        out.append(f'<g><title>{m}: forecast {u:.1f} unplanned + {p:.0f} planned (PM)</title>'
                   f'<rect x="{x:.1f}" y="{top}" width="{step:.1f}" height="{ph}" fill="transparent"/>'
                   f'<rect x="{x + 1:.1f}" y="{top + ph - hu:.1f}" width="{bw:.1f}" height="{hu:.1f}" rx="2" '
                   f'style="fill:var(--series-2)"/>'
                   f'<rect x="{x + 1:.1f}" y="{top + ph - hu - hp:.1f}" width="{bw:.1f}" height="{max(hp - 1, 0):.1f}" '
                   f'rx="2" style="fill:var(--series-3)"/></g>')
    for i, m in enumerate(months):
        if i % 6 == 0:
            out.append(f'<text x="{left + i * step:.1f}" y="{height - 8}" class="axis">{m}</text>')
    out.append("</svg>")
    return "".join(out)



def year_bars(used: list[tuple[str, float]], required: tuple[str, float],
              width: int = 640, height: int = 280) -> str:
    """3 yearly 'used' bars + 1 'required next 12 months' bar, value on top."""
    items = [(lab, v, "--series-1") for lab, v in used] + [(required[0], required[1], "--series-2")]
    vmax = max(max(v for _, v, _ in items), 1)
    top, bottom, left = 30, 56, 10
    ph = height - top - bottom
    slot = (width - left * 2) / len(items)
    bw = min(slot * 0.55, 90)
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="pieces used per year and required next year">',
           f'<line x1="{left}" x2="{width - left}" y1="{top + ph}" y2="{top + ph}" class="grid"/>']
    for i, (lab, v, col) in enumerate(items):
        cx = left + slot * i + slot / 2
        hgt = ph * v / vmax
        y = top + ph - hgt
        name = "Required" if i == len(items) - 1 else "Used"
        out.append(f'<g><title>{name} {escape(lab)}: {v:,.0f} pieces</title>'
                   f'<rect x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{ph}" fill="transparent"/>'
                   f'<rect x="{cx - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(hgt, 1):.1f}" rx="4" style="fill:var({col})"/>'
                   f'<text x="{cx:.1f}" y="{y - 8:.1f}" text-anchor="middle" class="val" style="font-size:15px;font-weight:600">{v:,.0f}</text>'
                   f'<text x="{cx:.1f}" y="{top + ph + 20}" text-anchor="middle" class="axis" style="font-size:12px">{name}</text>'
                   f'<text x="{cx:.1f}" y="{top + ph + 36}" text-anchor="middle" class="axis" style="font-size:11px">{escape(lab)}</text></g>')
    out.append("</svg>")
    return "".join(out)