#!/usr/bin/env python3
"""
macro_tape.py — 毎朝5分で見るマクロ指標を1枚のHTMLにまとめる。

依存: 標準ライブラリのみ (Python 3.9+)
使い方:
    python3 macro_tape.py                 # macro_tape.html を生成
    python3 macro_tape.py --open          # 生成してブラウザで開く
    python3 macro_tape.py --out ~/tape.html

データ元:
    FRED  https://fred.stlouisfed.org/  (fredgraph.csv エンドポイント / APIキー不要)
    Stooq https://stooq.com/            (金スポット)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import math
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

UA = "Mozilla/5.0 (macro_tape/1.0)"
JST = dt.timezone(dt.timedelta(hours=9), "JST")
TIMEOUT = 25

FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
ATLANTA_URL = "https://www.atlantafed.org/cenfis/market-probability-tracker"


# ────────────────────────────────────────────────────────── データ取得

@dataclass
class Series:
    key: str
    label: str
    note: str = ""
    unit: str = ""
    dec: int = 2
    group: str = "rates"
    transform: str = "level"      # level | yoy_pct | mom_diff | qoq_saar
    scale: float = 1.0
    fred_id: str | None = None
    stooq: str | None = None
    derived: tuple[str, str] | None = None   # (系列A, 系列B) → A - B
    dates: list[dt.date] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return len(self.values) > 0 and not self.error


def _get(url: str, attempts: int = 3) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:                  # 一時的な失敗は待って再試行
            last = e
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise last if last else RuntimeError("取得失敗")


def fetch_fred(fred_id: str, start: dt.date) -> tuple[list[dt.date], list[float]]:
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={fred_id}&cosd={start.isoformat()}"
    )
    rows = list(csv.reader(io.StringIO(_get(url))))
    if not rows:
        raise ValueError("空のレスポンス")
    dates: list[dt.date] = []
    vals: list[float] = []
    for row in rows[1:]:                       # 1行目はヘッダー
        if len(row) < 2:
            continue
        raw = row[1].strip()
        if raw in ("", ".", "NA"):             # FREDの欠損表記
            continue
        try:
            dates.append(dt.date.fromisoformat(row[0].strip()))
            vals.append(float(raw))
        except ValueError:
            continue
    if not vals:
        raise ValueError("有効な観測値なし")
    return dates, vals


def fetch_stooq(symbol: str, start: dt.date) -> tuple[list[dt.date], list[float]]:
    url = (
        f"https://stooq.com/q/d/l/?s={symbol}&i=d"
        f"&d1={start.strftime('%Y%m%d')}&d2={dt.date.today().strftime('%Y%m%d')}"
    )
    rows = list(csv.reader(io.StringIO(_get(url))))
    if len(rows) < 2 or rows[0][0].lower() != "date":
        raise ValueError("Stooqが想定外の応答を返しました")
    close_ix = rows[0].index("Close")
    dates: list[dt.date] = []
    vals: list[float] = []
    for row in rows[1:]:
        try:
            dates.append(dt.date.fromisoformat(row[0]))
            vals.append(float(row[close_ix]))
        except (ValueError, IndexError):
            continue
    if not vals:
        raise ValueError("有効な観測値なし")
    return dates, vals


def load(s: Series, start: dt.date) -> Series:
    try:
        if s.fred_id:
            s.dates, s.values = fetch_fred(s.fred_id, start)
        elif s.stooq:
            s.dates, s.values = fetch_stooq(s.stooq, start)
        if s.scale != 1.0:
            s.values = [v * s.scale for v in s.values]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        s.error = f"取得失敗: {type(e).__name__}"
    except Exception as e:                      # パース失敗など
        s.error = f"取得失敗: {e}"
    return s


def align_difference(a: Series, b: Series) -> tuple[list[dt.date], list[float]]:
    """日付を突き合わせて A - B を作る。"""
    mb = dict(zip(b.dates, b.values))
    dates, vals = [], []
    for d, v in zip(a.dates, a.values):
        if d in mb:
            dates.append(d)
            vals.append(v - mb[d])
    return dates, vals


# ────────────────────────────────────────────────────────── 指標の計算

def value_at_offset(s: Series, days: int) -> float | None:
    """days日前に最も近い（それ以前の）観測値。"""
    if not s.ok:
        return None
    target = s.dates[-1] - dt.timedelta(days=days)
    pick = None
    for d, v in zip(s.dates, s.values):
        if d <= target:
            pick = v
        else:
            break
    return pick


def prev_obs(s: Series) -> float | None:
    return s.values[-2] if s.ok and len(s.values) >= 2 else None


def percentile_1y(s: Series) -> float | None:
    """直近1年レンジの中での位置 (0-100)。"""
    if not s.ok:
        return None
    cutoff = s.dates[-1] - dt.timedelta(days=365)
    window = [v for d, v in zip(s.dates, s.values) if d >= cutoff]
    if len(window) < 20:
        return None
    lo, hi = min(window), max(window)
    if math.isclose(hi, lo):
        return 50.0
    return (s.values[-1] - lo) / (hi - lo) * 100


def yoy_pct(s: Series) -> float | None:
    base = value_at_offset(s, 365)
    if base is None or base == 0:
        return None
    return (s.values[-1] / base - 1) * 100


def mom_diff(s: Series) -> float | None:
    p = prev_obs(s)
    return None if p is None else s.values[-1] - p


def qoq_saar(s: Series) -> float | None:
    p = prev_obs(s)
    if p is None or p <= 0:
        return None
    return ((s.values[-1] / p) ** 4 - 1) * 100


# ────────────────────────────────────────────────────────── 系列定義

def build_registry() -> dict[str, Series]:
    defs = [
        # ── 金利・インフレ期待
        Series("DGS10", "米10年 名目利回り", "リスク資産のバリュエーションの基準線",
               unit="%", group="rates", fred_id="DGS10"),
        Series("DFII10", "米10年 実質金利 (TIPS)", "金・グロース株の主因",
               unit="%", group="rates", fred_id="DFII10"),
        Series("T10YIE", "米10年 期待インフレ率 (BEI)", "名目 − 実質。インフレ懸念の温度",
               unit="%", group="rates", fred_id="T10YIE"),
        Series("DGS2", "米2年 利回り", "政策金利の織り込みを最も強く反映",
               unit="%", group="rates", fred_id="DGS2"),
        Series("T10Y2Y", "2-10年 スプレッド", "解消のしかたを見る（ベア/ブル・スティープ）",
               unit="%", group="rates", fred_id="T10Y2Y"),

        # ── 政策
        Series("DFF", "実効FF金利", "現在の政策金利の実勢",
               unit="%", group="policy", fred_id="DFF"),
        Series("DGS1", "米1年 利回り", "", unit="%", group="hidden", fred_id="DGS1"),
        Series("IMPLIED", "利下げ織り込み 目安", "1年債 − 実効FF。マイナスが深いほど利下げを織り込み",
               unit="%", group="policy", derived=("DGS1", "DFF")),

        # ── リスク・クレジット
        Series("BAMLH0A0HYM2", "HY クレジットスプレッド (OAS)", "景気の体温計。拡大は最優先の警戒シグナル",
               unit="%", group="risk", fred_id="BAMLH0A0HYM2"),
        Series("BAMLC0A0CM", "IG クレジットスプレッド (OAS)", "投資適格まで波及したら本物",
               unit="%", group="risk", fred_id="BAMLC0A0CM"),
        Series("VIXCLS", "VIX", "20 / 30 が目安の閾値", group="risk", fred_id="VIXCLS"),

        # ── 通貨・コモディティ・株
        Series("DTWEXBGS", "ドル指数 (広義・名目)", "DXYの代替。金利差要因かリスクオフかを金利と併読",
               group="fx", fred_id="DTWEXBGS"),
        Series("XAUUSD", "金 (ドル建て)", "実質金利と逆相関が基本。同時上昇は信認低下のサイン",
               unit="$", group="fx", stooq="xauusd"),
        Series("DCOILWTICO", "WTI原油", "供給要因か需要要因かで意味が反転",
               unit="$", group="fx", fred_id="DCOILWTICO"),
        Series("SP500", "S&P 500", "", dec=0, group="fx", fred_id="SP500"),

        # ── マクロ（イベント）
        Series("ICSA", "新規失業保険申請件数", "週次。雇用統計より速い先行指標",
               unit="千件", dec=0, scale=0.001, group="macro", transform="mom_diff"),
        Series("PAYEMS", "非農業部門雇用者数", "前月差＝雇用統計のヘッドライン",
               unit="千人", dec=0, group="macro", transform="mom_diff"),
        Series("UNRATE", "失業率", "前月差。%ポイント", unit="pt", dec=1,
               group="macro", transform="mom_diff"),
        Series("CPIAUCSL", "CPI (総合)", "前年比", unit="%", group="macro", transform="yoy_pct"),
        Series("CPILFESL", "CPI (コア)", "前年比", unit="%", group="macro", transform="yoy_pct"),
        Series("PCEPILFE", "コアPCE デフレーター", "FRBが最重視。前年比", unit="%",
               group="macro", transform="yoy_pct"),
        Series("GDPC1", "実質GDP", "前期比年率", unit="%", group="macro", transform="qoq_saar"),
    ]
    for s in defs:
        if s.fred_id is None and s.derived is None and s.stooq is None:
            s.fred_id = s.key
    return {s.key: s for s in defs}


# ────────────────────────────────────────────────────────── レジーム判定

def regime_read(reg: dict[str, Series]) -> tuple[str, str]:
    def chg(key: str, days: int = 7) -> float | None:
        s = reg.get(key)
        if s is None or not s.ok:
            return None
        base = value_at_offset(s, days)
        return None if base is None else s.values[-1] - base

    y, d, c = chg("DGS10"), chg("DTWEXBGS"), chg("BAMLH0A0HYM2")
    if None in (y, d, c):
        return "判定不可", "レジーム判定に必要な系列が揃っていません。"

    if c > 0.30:
        return "クレジット主導のストレス", (
            "HYスプレッドが週内で大きく拡大しています。株価より先に動くことが多い系列なので、"
            "他が落ち着いて見えても優先して確認する局面です。"
        )
    if y > 0.15 and d > 0:
        return "引き締め警戒（金利上昇・ドル高）", (
            "名目金利とドルが同時に上昇。実質金利と期待インフレのどちらが押し上げているかで、"
            "成長期待の改善か、インフレ再燃かが分かれます。"
        )
    if y < -0.15 and d > 0 and c > 0:
        return "質への逃避", (
            "金利低下・ドル高・スプレッド拡大が揃った形。景気減速を織り込む典型的な組み合わせです。"
        )
    if y < -0.10 and d < 0 and c < 0:
        return "緩和期待のリスクオン", (
            "金利低下・ドル安・スプレッド縮小。利下げ織り込みが進む局面で出やすい並びです。"
        )
    return "中立レンジ", "主要3系列（10年金利・ドル・HYスプレッド）に週次で目立った偏りはありません。"


# ────────────────────────────────────────────────────────── HTML描画

PAPER, INK, MUTED, RULE = "#EBEAE4", "#1A1E1B", "#6E7169", "#C8C7BE"
UP, DOWN, FLAG = "#B3421E", "#2C6E7F", "#9A7B10"


def sparkline(s: Series, days: int = 120, w: int = 104, h: int = 24) -> str:
    if not s.ok:
        return ""
    cutoff = s.dates[-1] - dt.timedelta(days=days)
    pts = [(d, v) for d, v in zip(s.dates, s.values) if d >= cutoff]
    if len(pts) < 3:
        return ""
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    pad = 3
    step = (w - 2) / (len(vals) - 1)
    coords = [
        (1 + i * step, pad + (h - 2 * pad) * (1 - (v - lo) / span))
        for i, v in enumerate(vals)
    ]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    ex, ey = coords[-1]
    tone = UP if vals[-1] >= vals[0] else DOWN
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline points="{path}" fill="none" stroke="{tone}" '
        f'stroke-width="1.1" stroke-linejoin="round"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="1.9" fill="{tone}"/></svg>'
    )


def range_bar(p: float | None) -> str:
    if p is None:
        return '<span class="dim">—</span>'
    return (
        f'<span class="rng" title="直近1年レンジ内 {p:.0f}%">'
        f'<span class="rng-tick" style="left:{max(0.0, min(100.0, p)):.1f}%"></span></span>'
    )


def fmt(v: float | None, dec: int, unit: str = "", signed: bool = False) -> str:
    if v is None:
        return '<span class="dim">—</span>'
    body = f"{v:+,.{dec}f}" if signed else f"{v:,.{dec}f}"
    if unit == "$":
        return f"${body}"
    return f"{body}{unit}"


def signed_text(v: float, dec: int, unit: str = "") -> tuple[str, str]:
    """表示文字列と色クラス。丸めて0になる値は符号を付けず中立色にする。"""
    r = round(v, dec)
    if r == 0:
        return fmt(0.0, dec, unit), "flat"
    return fmt(r, dec, unit, signed=True), ("up" if r > 0 else "down")


def delta_cell(v: float | None, dec: int, unit: str = "", slot: str = "",
               kicker: str = "") -> str:
    body, cls = ("—", "dim") if v is None else signed_text(v, dec, unit)
    return f'<td class="num {slot} {cls}" data-k="{kicker}">{body}</td>'


def market_row(s: Series) -> str:
    if not s.error and not s.ok:
        s.error = "データなし"
    if s.error:
        return (
            f'<tr class="row err"><th scope="row"><span class="lbl">{s.label}</span>'
            f'<span class="note">{s.error}。ネットワークまたは系列IDを確認してください。</span></th>'
            f'<td class="num dim" colspan="6">—</td></tr>'
        )
    d1 = None if prev_obs(s) is None else s.values[-1] - prev_obs(s)
    b7, b30 = value_at_offset(s, 7), value_at_offset(s, 30)
    w1 = None if b7 is None else s.values[-1] - b7
    m1 = None if b30 is None else s.values[-1] - b30
    note = f'<span class="note">{s.note}</span>' if s.note else ""
    return (
        f'<tr class="row"><th scope="row"><span class="lbl">{s.label}</span>{note}</th>'
        f'<td class="num lead">{fmt(s.values[-1], s.dec, s.unit)}</td>'
        f'{delta_cell(d1, s.dec, slot="d1", kicker="前日")}'
        f'{delta_cell(w1, s.dec, slot="w1", kicker="1週")}'
        f'{delta_cell(m1, s.dec, slot="m1", kicker="1月")}'
        f'<td class="bar">{range_bar(percentile_1y(s))}</td>'
        f'<td class="sp">{sparkline(s)}</td></tr>'
    )


def macro_row(s: Series) -> str:
    if not s.error and not s.ok:
        s.error = "データなし"
    if s.error:
        return (
            f'<tr class="row err"><th scope="row"><span class="lbl">{s.label}</span>'
            f'<span class="note">{s.error}</span></th>'
            f'<td class="num dim" colspan="3">—</td></tr>'
        )
    calc = {"yoy_pct": yoy_pct, "mom_diff": mom_diff, "qoq_saar": qoq_saar}
    v = calc.get(s.transform, lambda _s: _s.values[-1])(s)
    kind = {"yoy_pct": "前年比", "mom_diff": "前回差", "qoq_saar": "前期比年率"}[s.transform]
    text, cls = ('<span class="dim">—</span>', "flat") if v is None \
        else signed_text(v, s.dec, s.unit)
    note = f'<span class="note">{s.note}</span>' if s.note else ""
    return (
        f'<tr class="row"><th scope="row"><span class="lbl">{s.label}</span>{note}</th>'
        f'<td class="num lead {cls}">{text}</td>'
        f'<td class="num kind">{kind}</td>'
        f'<td class="num asof">{s.dates[-1].isoformat()} 時点</td></tr>'
    )


def section(title: str, subtitle: str, rows: str, head: list[str]) -> str:
    ths = "".join(f'<th class="col">{h}</th>' for h in head)
    return f"""
  <section class="block">
    <div class="eyebrow"><h2>{title}</h2><p>{subtitle}</p></div>
    <table><thead><tr><th class="col left">指標</th>{ths}</tr></thead>
    <tbody>{rows}</tbody></table>
  </section>"""


def render(reg: dict[str, Series]) -> str:
    now = dt.datetime.now(JST)
    label, body = regime_read(reg)
    mk_head = ["最新", "前日", "1週", "1月", "1年レンジ", "120日"]
    mc_head = ["変化", "基準", "更新"]

    rates = "".join(market_row(reg[k]) for k in
                    ("DGS10", "DFII10", "T10YIE", "DGS2", "T10Y2Y"))
    policy = "".join(market_row(reg[k]) for k in ("DFF", "IMPLIED"))
    risk = "".join(market_row(reg[k]) for k in
                   ("BAMLH0A0HYM2", "BAMLC0A0CM", "VIXCLS"))
    fx = "".join(market_row(reg[k]) for k in
                 ("DTWEXBGS", "XAUUSD", "DCOILWTICO", "SP500"))
    macro = "".join(macro_row(reg[k]) for k in
                    ("ICSA", "PAYEMS", "UNRATE", "CPIAUCSL", "CPILFESL",
                     "PCEPILFE", "GDPC1"))

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="{PAPER}">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Macro Tape">
<title>マクロ日次テープ — {now:%Y-%m-%d}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper:{PAPER}; --ink:{INK}; --muted:{MUTED}; --rule:{RULE};
    --up:{UP}; --down:{DOWN}; --flag:{FLAG};
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--paper); color:var(--ink);
    font-family:"Noto Sans JP","IBM Plex Sans Condensed",sans-serif;
    font-size:14px; line-height:1.5;
    -webkit-font-smoothing:antialiased;
  }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:36px 24px 72px; }}

  /* ── ヘッダー：テープの見出し */
  header {{ border-bottom:2px solid var(--ink); padding-bottom:14px; }}
  .masthead {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px 16px; flex-wrap:wrap; }}
  h1 {{
    font-family:"IBM Plex Sans Condensed",sans-serif; font-weight:700;
    font-size:clamp(24px,4vw,34px); letter-spacing:.14em; margin:0; text-transform:uppercase;
  }}
  .tag {{ margin:4px 0 0; font-size:12px; color:var(--muted); letter-spacing:.08em; }}
  .stamp {{ font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted); letter-spacing:.06em; }}

  .regime {{ margin-top:18px; display:flex; gap:14px; align-items:flex-start; }}
  .regime .pin {{ width:9px; height:9px; margin-top:7px; background:var(--flag); flex:none; }}
  .regime h3 {{
    margin:0; font-family:"IBM Plex Sans Condensed",sans-serif;
    font-size:17px; letter-spacing:.03em;
  }}
  .regime p {{ margin:3px 0 0; color:var(--muted); font-size:13px; max-width:64ch; }}

  .links {{ margin-top:16px; display:flex; gap:8px; flex-wrap:wrap; }}
  .links a {{
    font-family:"IBM Plex Mono",monospace; font-size:11.5px; letter-spacing:.04em;
    text-decoration:none; color:var(--ink); border:1px solid var(--rule);
    padding:5px 10px; background:transparent; transition:background .15s,border-color .15s;
  }}
  .links a:hover {{ background:var(--ink); color:var(--paper); border-color:var(--ink); }}
  .links a:focus-visible {{ outline:2px solid var(--flag); outline-offset:2px; }}

  /* ── セクション */
  .block {{ margin-top:38px; }}
  .eyebrow {{ display:flex; align-items:baseline; gap:22px; border-bottom:1px solid var(--ink); padding-bottom:7px; flex-wrap:wrap; }}
  .eyebrow h2 {{
    font-family:"IBM Plex Sans Condensed",sans-serif; font-weight:600;
    font-size:13px; letter-spacing:.2em; text-transform:uppercase; margin:0; white-space:nowrap;
  }}
  .eyebrow p {{ margin:0 0 0 10px; font-size:12px; color:var(--muted); }}

  table {{ width:100%; border-collapse:collapse; }}
  thead th {{
    font-family:"IBM Plex Mono",monospace; font-weight:500; font-size:10.5px;
    letter-spacing:.09em; color:var(--muted); text-transform:uppercase;
    text-align:right; padding:9px 8px 7px; border-bottom:1px solid var(--rule); white-space:nowrap;
  }}
  thead th.left {{ text-align:left; }}
  tbody th {{ text-align:left; font-weight:400; padding:11px 8px 11px 0; vertical-align:top; }}
  .lbl {{ display:block; font-weight:500; font-size:14px; }}
  .note {{ display:block; font-size:11.5px; color:var(--muted); margin-top:2px; max-width:46ch; }}
  tbody td {{ padding:11px 8px; border-bottom:1px solid var(--rule); vertical-align:middle; }}
  tbody th {{ border-bottom:1px solid var(--rule); }}
  .row:hover td, .row:hover th {{ background:rgba(26,30,27,.035); }}
  .num {{ font-family:"IBM Plex Mono",monospace; text-align:right; font-size:13px; white-space:nowrap; font-variant-numeric:tabular-nums; }}
  .lead {{ font-size:16px; font-weight:600; }}
  .up {{ color:var(--up); }} .down {{ color:var(--down); }} .flat {{ color:var(--muted); }}
  .dim {{ color:var(--muted); }}
  .kind, .asof {{ font-size:11px; color:var(--muted); }}
  .err .lbl {{ color:var(--muted); }}

  /* ── 1年レンジ内の位置 */
  .bar {{ width:92px; }}
  .rng {{ position:relative; display:block; height:3px; background:var(--rule); }}
  .rng-tick {{ position:absolute; top:-3px; width:2px; height:9px; background:var(--ink); transform:translateX(-1px); }}
  .sp {{ width:112px; }}
  .spark {{ display:block; }}

  footer {{ margin-top:44px; padding-top:14px; border-top:1px solid var(--rule); font-size:11.5px; color:var(--muted); }}
  footer p {{ margin:5px 0; max-width:74ch; }}

  @media (max-width:720px) {{
    .wrap {{ padding:24px 16px 56px; }}
    thead, .bar {{ display:none; }}
    table, tbody, tbody tr, tbody th, tbody td {{ display:block; }}

    tbody tr.row {{
      display:grid; align-items:center; column-gap:12px;
      grid-template-columns:repeat(3, minmax(0,1fr)) 92px;
      grid-template-areas:"lbl lbl lbl lbl" "val val val sp" "d1 w1 m1 sp";
      padding:14px 0; border-bottom:1px solid var(--rule);
    }}
    tbody tr.row:last-child {{ border-bottom:none; }}
    tbody th {{ grid-area:lbl; padding:0 0 8px; border:none; }}
    .note {{ font-size:11px; max-width:none; }}
    tbody td, tbody td.num {{ border:none; padding:0; text-align:left; }}
    .lead {{ grid-area:val; font-size:22px; padding-bottom:8px; }}
    .d1 {{ grid-area:d1; }} .w1 {{ grid-area:w1; }} .m1 {{ grid-area:m1; }}
    .d1::before, .w1::before, .m1::before {{
      content:attr(data-k); display:block; font-size:9.5px; letter-spacing:.1em;
      color:var(--muted); margin-bottom:1px;
    }}
    .sp {{ grid-area:sp; justify-self:end; }}
    .spark {{ width:92px; height:34px; }}
    .kind, .asof {{ display:inline-block; margin-right:12px; }}

    /* イベント行は3列構成 */
    .block:last-of-type tbody tr.row {{
      grid-template-columns:1fr auto;
      grid-template-areas:"lbl lbl" "val val" "kind asof";
    }}
    .block:last-of-type .kind {{ grid-area:kind; }}
    .block:last-of-type .asof {{ grid-area:asof; justify-self:end; }}
  }}
  @media (prefers-reduced-motion:reduce) {{ * {{ transition:none !important; }} }}
</style></head>
<body><div class="wrap">

<header>
  <div class="masthead">
    <div><h1>Macro Tape</h1><p class="tag">米マクロ・毎朝の確認用</p></div>
    <span class="stamp">生成 {now:%Y-%m-%d %H:%M} JST</span>
  </div>
  <div class="regime">
    <span class="pin"></span>
    <div><h3>{label}</h3><p>{body}</p></div>
  </div>
  <div class="links">
    <a href="{FEDWATCH_URL}" target="_blank" rel="noopener">CME FedWatch（利上げ確率）</a>
    <a href="{ATLANTA_URL}" target="_blank" rel="noopener">Atlanta Fed 確率トラッカー</a>
    <a href="https://www.bls.gov/schedule/news_release/" target="_blank" rel="noopener">BLS 発表カレンダー</a>
    <a href="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm" target="_blank" rel="noopener">FOMC 日程</a>
  </div>
</header>

{section("金利・インフレ期待", "名目 = 実質 + 期待インフレ。どちらが動かしたかで意味が変わる", rates, mk_head)}
{section("政策金利", "指標発表への反応は、実数値ではなく織り込みとの差で決まる", policy, mk_head)}
{section("リスク・クレジット", "株価より先に動く。毎朝ここだけは飛ばさない", risk, mk_head)}
{section("通貨・コモディティ・株", "金利と併読して初めて解釈できる", fx, mk_head)}
{section("マクロ（イベント）", "日次では動かない。発表日に確認する", macro, mc_head)}

<footer>
  <p>データ: FRED（セントルイス連銀）/ Stooq（金スポット）。FREDの日次系列は1営業日程度遅れて公開されます。</p>
  <p>ドル指数は広義名目ドル指数 DTWEXBGS。ICEのDXYとは構成通貨・ウェイトが異なります。</p>
  <p>「利下げ織り込み 目安」は1年債利回り − 実効FF金利による簡易プロキシです。正式な確率分布はFedWatchを参照してください。</p>
  <p>本ページは指標の集約表示のみを行うもので、投資助言ではありません。</p>
</footer>

</div></body></html>"""


# ────────────────────────────────────────────────────────── 実行

def collect(reg: dict[str, Series], start: dt.date) -> None:
    targets = [s for s in reg.values() if s.derived is None]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda s: load(s, start), targets))
    for s in reg.values():
        if s.derived:
            a, b = reg[s.derived[0]], reg[s.derived[1]]
            if a.ok and b.ok:
                s.dates, s.values = align_difference(a, b)
                if not s.values:
                    s.error = "日付が突き合いません"
            else:
                s.error = "元系列の取得失敗"


def main() -> int:
    ap = argparse.ArgumentParser(description="マクロ指標を1枚のHTMLにまとめる")
    ap.add_argument("--out", default="macro_tape.html", help="出力先HTML")
    ap.add_argument("--years", type=int, default=3, help="取得する履歴年数")
    ap.add_argument("--open", action="store_true", help="生成後にブラウザで開く")
    args = ap.parse_args()

    start = dt.date.today() - dt.timedelta(days=365 * args.years + 30)
    reg = build_registry()

    print("取得中 …", file=sys.stderr)
    collect(reg, start)

    failed = [s.label for s in reg.values() if s.group != "hidden" and not s.ok]
    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(reg), encoding="utf-8")

    print(f"生成: {out}", file=sys.stderr)
    if failed:
        print(f"取得できなかった系列: {', '.join(failed)}", file=sys.stderr)
    if args.open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
