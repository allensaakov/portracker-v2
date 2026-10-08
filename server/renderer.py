"""
renderer.py
Server-side Pillow renderer — same visual logic as the Pi's render_display.py
but takes pre-fetched data as arguments instead of reading files.

Called by main.py to produce the 800x480 PNG that the Pi downloads.
"""

import math, os, subprocess, shutil
from datetime import datetime, date, timedelta
import holidays
from zoneinfo import ZoneInfo
import pytz

from PIL import Image, ImageDraw, ImageFont

W, H    = 800, 480
BLACK   = 0
WHITE   = 255

_earn_page_counter = 0
_exdiv_page_counter = 0

def _find_font_dir() -> str:
    # Bundled fonts (works everywhere)
    bundled = os.path.join(os.path.dirname(__file__), "fonts") + "/"
    if os.path.exists(bundled + "DejaVuSansMono.ttf"):
        return bundled
    # Pi fallback
    pi_path = "/usr/share/fonts/truetype/dejavu/"
    if os.path.exists(pi_path + "DejaVuSansMono.ttf"):
        return pi_path
    return ""

def _font(name, size):
    font_dir = _find_font_dir()
    try:
        return ImageFont.truetype(font_dir + name, size)
    except OSError:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            return ImageFont.load_default()
        
MARGIN       = 8
TOP_BAR_H    = 56
BOTTOM_BAR_H = 44
SIDE_W       = 130

TRACKED_SLUGS = [
    slug.strip()
    for slug in os.getenv("PORTFOLIOS", "").split(",")
    if slug.strip()
]

SLUG_LABELS = {
    slug: slug.replace("_", " ").title()
    for slug in TRACKED_SLUGS
}


LINE_STYLES   = ["solid", "dashed", "dash-dot"]


def _rect(draw, xy, width=1, fill=None):
    draw.rectangle(xy, outline=BLACK, width=width, fill=fill)


def _text(draw, xy, s, fnt, anchor=None):
    draw.text(xy, s, font=fnt, fill=BLACK, anchor=anchor)


def _tw(draw, s, fnt):
    bb = draw.textbbox((0, 0), s, font=fnt)
    return bb[2] - bb[0]


def _draw_line(draw, points, style, width=2):
    if not points or len(points) < 2:
        return
    if style == "solid":
        draw.line(points, fill=BLACK, width=width, joint="curve")
        return

    DASH_ON = 12
    DASH_OFF = 8
    DOT_R = width // 2 + 1

    if style == "dashed":
        pattern = [("line", DASH_ON), ("gap", DASH_OFF)]
    elif style == "dash-dot":
        pattern = [("line", DASH_ON), ("gap", 6), ("dot", DOT_R * 2), ("gap", 6)]
    else:
        pattern = [("line", DASH_ON), ("gap", DASH_OFF)]

    import math as _math
    pat_idx = 0
    pat_rem = pattern[0][1]
    seg_i   = 0
    seg_px  = list(points[0])

    while seg_i < len(points) - 1:
        p0 = seg_px
        p1 = list(points[seg_i + 1])
        seg_len = _math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if seg_len == 0:
            seg_i += 1
            seg_px = p1
            continue

        dx = (p1[0] - p0[0]) / seg_len
        dy = (p1[1] - p0[1]) / seg_len
        walked = 0.0

        while walked < seg_len:
            kind, _ = pattern[pat_idx]
            step = min(pat_rem, seg_len - walked)
            ex = p0[0] + dx * (walked + step)
            ey = p0[1] + dy * (walked + step)

            if kind == "line":
                draw.line(
                    [(int(p0[0] + dx * walked), int(p0[1] + dy * walked)),
                     (int(ex), int(ey))],
                    fill=BLACK, width=width
                )
            elif kind == "dot":
                mx = int(p0[0] + dx * (walked + step / 2))
                my = int(p0[1] + dy * (walked + step / 2))
                draw.ellipse((mx - DOT_R, my - DOT_R, mx + DOT_R, my + DOT_R), fill=BLACK)

            walked  += step
            pat_rem -= step
            if pat_rem <= 0:
                pat_idx = (pat_idx + 1) % len(pattern)
                pat_rem = pattern[pat_idx][1]

        seg_i += 1
        seg_px = p1


def is_market_open() -> bool:
    nyse_holidays = holidays.NYSE()
    now = datetime.now()
    if (now.weekday() >= 5) or now in nyse_holidays: 
        return False
    market_open  = now.replace(hour=8, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close


def render_display(portfolios: dict, indices: list, history: dict,
                   events: list, mode: str) -> Image.Image:
    """
    portfolios: {slug: {total_value, daily_gain, daily_pct, positions}}
    indices:    [{symbol, price, change_pct, change_pct_30d, change_pct_ytd}]
    history:    {slug: [(datetime, pct_gain, dollar_value), ...]}
    events:     [{symbol, kind, date, detail}]
    mode:       daily | monthly | ytd
    """

    global _earn_page_counter, _exdiv_page_counter
    F_TINY   = _font("DejaVuSansMono.ttf",      13)
    F_SMALL  = _font("DejaVuSansMono.ttf",      15)
    F_SMALLB = _font("DejaVuSansMono-Bold.ttf", 15)
    F_MED    = _font("DejaVuSansMono-Bold.ttf", 18)
    F_LABEL  = _font("DejaVuSans-Bold.ttf",     13)

    img  = Image.new("L", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    _rect(draw, (0, 0, W - 1, H - 1), width=2)

    # ── Top bar ───────────────────────────────────────────────────────────────
    top_y0, top_y1 = MARGIN, MARGIN + TOP_BAR_H
    idx_x0, idx_x1 = MARGIN, 420
    ts_x0,  ts_x1  = idx_x1 + 8, W - MARGIN

    _rect(draw, (idx_x0, top_y0, idx_x1, top_y1), width=2)
    _rect(draw, (ts_x0,  top_y0, ts_x1,  top_y1), width=2)

    # Indices
    # idx_mode_label = {"daily": "DLY", "monthly": "30D", "ytd": "YTD"}.get(mode, "DLY")
    # _text(draw, (idx_x0 + 4, top_y0 + 4), idx_mode_label, F_TINY)
    col_w = (idx_x1 - idx_x0) // len(indices)

    for i, idx in enumerate(indices):
        sym = idx["symbol"]
        price = idx.get("price")
        chg = idx.get("change_pct")

        col_x = idx_x0 + i * col_w + col_w // 2
        _text(draw, (col_x, top_y0 + 10), sym, F_SMALLB, anchor="mt")
        if price is not None:
            _text(draw, (col_x, top_y0 + 25), f"{price:,.2f}", F_TINY, anchor="mt")
        else:
            _text(draw, (col_x, top_y0 + 25), "--", F_TINY, anchor="mt")

        if chg is not None:
            sign = "+" if chg >= 0 else ""
            _text(draw, (col_x, top_y0 + 40), f"{sign}{chg:.2f}%", F_TINY, anchor="mt")
        else:
            _text(draw, (col_x, top_y0 + 40), "--", F_TINY, anchor="mt")

    # Timestamp + market status
    now_str = datetime.now(ZoneInfo("America/Chicago")).strftime("%b %-d  %-I:%M %p")
    # _text(draw, (ts_x0 + 10, top_y0 + 4),  "UPDATED", F_LABEL)
    _text(draw, (ts_x0 + 10, top_y0 + 21), now_str,   F_SMALLB)

    market_live = is_market_open()
    status_cx   = ts_x0 + 10 + _tw(draw, now_str, F_SMALLB) + 18
    status_cy   = top_y0 + 29
    r = 5
    if market_live:
        draw.ellipse((status_cx-r, status_cy-r, status_cx+r, status_cy+r), fill=BLACK)
        _text(draw, (status_cx + r + 6, status_cy), "LIVE",   F_TINY, anchor="lm")
    else:
        draw.ellipse((status_cx-r, status_cy-r, status_cx+r, status_cy+r), outline=BLACK, width=2)
        _text(draw, (status_cx + r + 6, status_cy), "CLOSED", F_TINY, anchor="lm")

    badge_label = mode.upper()
    bx1 = ts_x1 - 10
    bx0 = bx1 - 60
    by0 = top_y0 + 19
    by1 = by0 + 18
    _rect(draw, (bx0, by0, bx1, by1), width=1)
    _text(draw, ((bx0 + bx1) // 2, (by0 + by1) // 2), badge_label, F_TINY, anchor="mm")

    # ── Bottom bar (ex-dividends) ─────────────────────────────────────────────────
    bot_y1 = H - MARGIN
    bot_y0 = bot_y1 - BOTTOM_BAR_H
    _rect(draw, (MARGIN, bot_y0, W - MARGIN, bot_y1), width=2)
    _text(draw, (MARGIN + 10, bot_y0 + 6), "EX-DIV", F_LABEL)

    exdiv_events  = events.get("exdiv", [])
    EXDIV_SLOTS   = 3
    ex_total_pages = max(1, math.ceil(len(exdiv_events) / EXDIV_SLOTS))
    ex_page        = _exdiv_page_counter % ex_total_pages
    ex_page_events = exdiv_events[ex_page * EXDIV_SLOTS:(ex_page + 1) * EXDIV_SLOTS]

    if ex_page_events:
        parts    = [f"{e['symbol']} {e['detail']} {e['date'].strftime('%-m/%-d')}"
                    for e in ex_page_events]
        div_line = "  \u2022  ".join(parts)
    else:
        div_line = "No ex-dividends this month"

    _text(draw, (MARGIN + 10, bot_y0 + 23), div_line, F_SMALL)

    if ex_total_pages > 1:
        _text(draw, (W - MARGIN - 10, bot_y1 - 8),
              f"{ex_page + 1} of {ex_total_pages}", F_TINY, anchor="rb")

    _exdiv_page_counter += 1

    # ── Middle row ────────────────────────────────────────────────────────────
    mid_y0 = top_y1 + 8
    mid_y1 = bot_y0 - 8

    left_x0  = MARGIN
    left_x1  = left_x0 + SIDE_W
    right_x1 = W - MARGIN
    right_x0 = right_x1 - SIDE_W
    chart_x0 = left_x1 + 8
    chart_x1 = right_x0 - 8

    # ── Left sidebar: gains ───────────────────────────────────────────────────
    _rect(draw, (left_x0, mid_y0, left_x1, mid_y1), width=2)
    gains_label = {"daily": "GAINS", "monthly": "30D", "ytd": "YTD"}.get(mode, "GAINS")
    _text(draw, (left_x0 + 8, mid_y0 + 6), gains_label, F_LABEL)

    slugs  = TRACKED_SLUGS
    py     = mid_y0 + 28
    row_h  = (mid_y1 - mid_y0 - 28) // max(len(slugs), 1)

    for i, (slug, style) in enumerate(zip(slugs, LINE_STYLES)):
        pf_data = portfolios.get(slug, {})
        label   = SLUG_LABELS.get(slug, slug)
        pts     = history.get(slug, [])

        if mode == "daily":
            pct    = pf_data.get("daily_pct", 0.0)
            dollar = pf_data.get("daily_gain", 0.0)
        else:
            if pts:
                base_val   = pts[0][2]
                curr_val   = pts[-1][2]
                dollar     = curr_val - base_val
                pct        = (dollar / base_val * 100) if base_val else 0.0
            else:
                pct = dollar = 0.0

        if pct is None or dollar is None:
            pct_str, dollar_str = "--", "--"
        else:
            pct_str    = f"{'+' if pct >= 0 else ''}{pct:.2f}%"
            dollar_str = f"{'+' if dollar >= 0 else '-'}${abs(dollar):,.0f}"

        _text(draw, (left_x0 + 8, py), label, F_SMALLB)
        _text(draw, (left_x0 + 8, py + 17), pct_str,    F_MED)
        _text(draw, (left_x0 + 8, py + 37), dollar_str, F_TINY)

        swatch_y = py + 56
        swatch   = [(left_x0 + 10, swatch_y), (left_x0 + 48, swatch_y)]
        # _draw_line(draw, [(left_x0 + 8, swatch_y), (left_x1 - 8, swatch_y)], style, width=2)
        _draw_line(draw, swatch, style, width=1)

        if i < len(slugs) - 1:
            sep_y = py + row_h - 8
            draw.line((left_x0 + 8, sep_y, left_x1 - 8, sep_y), fill=BLACK, width=1)
        py += row_h

    # ── Right sidebar: earnings/dividends ─────────────────────────────────────
    

    _rect(draw, (right_x0, mid_y0, right_x1, mid_y1), width=2)
    _text(draw, (right_x0 + 8, mid_y0 + 6), "EARN/DIV", F_LABEL)

    # Combine earnings + dividend payment events
    sidebar_events = events.get("earn", []) + events.get("div", [])
    sidebar_events.sort(key=lambda e: e["date"])

    SLOTS       = 4
    EV_H        = (mid_y1 - mid_y0 - 28 - 16) // SLOTS
    total_pages = max(1, math.ceil(len(sidebar_events) / SLOTS))
    earn_page   = _earn_page_counter % total_pages
    page_events = sidebar_events[earn_page * SLOTS:(earn_page + 1) * SLOTS]

    if page_events:
        ey = mid_y0 + 26
        for j, ev in enumerate(page_events):
            _text(draw, (right_x0 + 8, ey),      ev["symbol"], F_SMALLB)
            _text(draw, (right_x0 + 8, ey + 16), ev["kind"],   F_TINY)
            _text(draw, (right_x0 + 8, ey + 30), ev["detail"], F_TINY)
            if j < len(page_events) - 1:
                draw.line((right_x0 + 8, ey + EV_H - 4,
                           right_x1 - 8, ey + EV_H - 4), fill=BLACK, width=1)
            ey += EV_H
    else:
        _text(draw, (right_x0 + 8, mid_y0 + 30), "None", F_TINY)

    if total_pages > 1:
        _text(draw, (right_x1 - 8, mid_y1 - 8),
              f"{earn_page + 1} of {total_pages}", F_TINY, anchor="rb")

    _earn_page_counter += 1

    # ── Center chart ──────────────────────────────────────────────────────────
    chart_pad_l, chart_pad_r = 44, 10
    chart_pad_t, chart_pad_b = 20, 24
    cx0 = chart_x0 + chart_pad_l
    cx1 = chart_x1 - chart_pad_r
    cy0 = mid_y0   + chart_pad_t
    cy1 = mid_y1   - chart_pad_b

    chart_title = {
        "daily":   "PERFORMANCE \u2014 DAILY (%)",
        "monthly": "GROWTH \u2014 30D ($100 peg)",
        "ytd":     "GROWTH \u2014 YTD ($100 peg)",
    }.get(mode, "PERFORMANCE")
    _text(draw, (chart_x0 + 8, mid_y0 + -2), chart_title, F_LABEL)

    today = date.today()
    if mode == "daily":
        x_labels = ("8:30", "12:00", "3:00")
    elif mode == "monthly":
        start = today - timedelta(days=30)
        x_labels = (start.strftime("%-m/%-d"),
                    (start + timedelta(days=15)).strftime("%-m/%-d"),
                    today.strftime("%-m/%-d"))
    else:
        x_labels = ("Jan 1", today.strftime("%b"), today.strftime("%-m/%-d"))

    _text(draw, (cx0, cy1 + 4),               x_labels[0], F_TINY, anchor="la")
    _text(draw, ((cx0 + cx1) // 2, cy1 + 4),  x_labels[1], F_TINY, anchor="ma")
    _text(draw, (cx1, cy1 + 4),               x_labels[2], F_TINY, anchor="ra")

    # Build chart series
    series_data = []
    for slug in slugs:
        pts = history.get(slug, [])
        print(pts)
        if not pts:
            continue
        if mode == "daily":
            base = pts[0][1]   # pct_gain of anchor point = 0.0
            normalized = [(ts, pct - base) for ts, pct, _ in pts]
            series_data.append((slug, normalized))
        else:  # monthly/ytd
            
            # Convert cumulative TWR into a $100 growth index
            # and rebase the selected period to exactly $100.
            
            twr_points = [
                (ts, pct)
                for ts, pct, _ in pts
                if pct is not None
            ]
            if not twr_points:
                continue
            starting_twr = twr_points[0][1]
            normalized = [
                (
                    ts,
                    100.0 * (1.0 + cumulative_twr)
                    / (1.0 + starting_twr)
                )
                for ts, cumulative_twr in twr_points
            ]
            series_data.append((slug, normalized))

    if series_data:
        all_vals = [v for _, s in series_data for _, v in s]
        y_min, y_max = min(all_vals), max(all_vals)
        pad = max(0.5, (y_max - y_min) * 0.12)
        y_min -= pad
        y_max += pad
    else:
        y_min, y_max = (99.0, 101.0) if mode != "daily" else (-1.0, 1.0)

    if series_data:
        all_times = [t for _, s in series_data for t, _ in s]
        t_min  = min(all_times)
        if mode == "daily":
            # Force x-axis to span full trading day (8:30am–3:00pm CT)
            ct  = pytz.timezone("America/Chicago")
            now = datetime.now(ct)
            t_min  = now.replace(hour=8, minute=30, second=0, microsecond=0)
            t_max  = now.replace(hour=15, minute=0,  second=0, microsecond=0)

            # Convert to naive UTC to match DB timestamps
            t_min = t_min.astimezone(pytz.utc).replace(tzinfo=None)
            t_max = t_max.astimezone(pytz.utc).replace(tzinfo=None)
        else:
            t_max = max(all_times)
        t_span = (t_max - t_min).total_seconds() or 1.0
    else:
        t_min  = datetime.now()
        t_span = 1.0

    def to_px(ts, val):
        x = cx0 + (cx1 - cx0) * ((ts - t_min).total_seconds() / t_span)
        y = cy1 - (cy1 - cy0) * ((val - y_min) / (y_max - y_min))
        x = max(cx0, min(cx1, x))
        y = max(cy0, min(cy1, y))
        return (x, y)

    for frac in (0.25, 0.5, 0.75):
        gx = cx0 + (cx1 - cx0) * frac
        y  = cy0
        while y < cy1:
            draw.line((gx, y, gx, min(y + 3, cy1)), fill=BLACK, width=1)
            y += 7

    baseline = 0.0 if mode == "daily" else 100.0
    if y_min <= baseline <= y_max:
        bzy = cy1 - (cy1 - cy0) * ((baseline - y_min) / (y_max - y_min))
        draw.line((cx0, bzy, cx1, bzy), fill=BLACK, width=1)
        _text(draw, (cx0 - 4, bzy),
              "0%" if mode == "daily" else "$100", F_TINY, anchor="rm")

    if mode == "daily":
        _text(draw, (cx0 - 4, cy0), f"{y_max:+.1f}%", F_TINY, anchor="rm")
        _text(draw, (cx0 - 4, cy1), f"{y_min:+.1f}%", F_TINY, anchor="rm")
    else:
        _text(draw, (cx0 - 4, cy0), f"${y_max:.1f}", F_TINY, anchor="rm")
        _text(draw, (cx0 - 4, cy1), f"${y_min:.1f}", F_TINY, anchor="rm")

    for (slug, pts), style in zip(series_data, LINE_STYLES):
        pixel_pts = [to_px(ts, v) for ts, v in pts]
        if len(pixel_pts) > 200:
            step = max(1, len(pixel_pts) // 200)
            pixel_pts = pixel_pts[::step]
        _draw_line(draw, pixel_pts, style)

    if not series_data:
        _text(draw, ((cx0 + cx1) // 2, (cy0 + cy1) // 2),
              "No history yet", F_TINY, anchor="mm")

    return img.convert("1")