"""Detail-window renderer.

The tray does NOT build HTML - it points the WebView2 host at a file this
worker wrote (that is the original design, and it is why a theme switch never
re-scans anything). Contract with tray/webview2-host.ps1:

  * the file lives at AppHome/detail.html
  * it may declare its own window size with  data-w='1080' data-h='640'
    (regex-matched out of the raw text, so keep those two attributes adjacent
    and single-quoted)

Three themes, matching the tray's Theme submenu:
  surfacer - dark board, big numbers (default)
  phosphor - monospace terminal face
  mini     - narrow strip that can live in a screen corner
"""

from __future__ import annotations

import html

from .util import fmt_age, fmt_tokens, fmt_usd

TONE = {
    "relaxed": "#3ddc84",
    "caution": "#f2c14e",
    "warning": "#ff9142",
    "blocked": "#ff5a50",
    "unknown": "#7a828c",
    "degraded": "#f2c14e",
}

ACCENT = {"claude-code": "#e08257", "fable": "#f2ad62", "codex": "#37c9a3"}

DAYS = ["월", "화", "수", "목", "금", "토", "일"]


def esc(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def pct_text(value) -> str:
    if value is None:
        return "--"
    if value >= 10:
        return "%.0f%%" % value
    return "%.1f%%" % value


def window_value(provider, name, short=False):
    """Cost for Claude Code, tokens for Codex - each provider's honest unit."""
    windows = provider.get("windows") or {}
    bucket = windows.get(name)
    if not bucket:
        return "--"
    if provider["id"] == "claude-code":
        return fmt_usd(bucket.get("cost"))
    return fmt_tokens(bucket.get("total"))


def window_sub(provider, name):
    windows = provider.get("windows") or {}
    bucket = windows.get(name)
    if not bucket:
        return ""
    if provider["id"] == "claude-code":
        return "%s tok · %d msg" % (fmt_tokens(bucket.get("tokens")), bucket.get("msgs") or 0)
    return "fresh %s · out %s" % (fmt_tokens(bucket.get("fresh")),
                                  fmt_tokens(bucket.get("output")))


# ── pieces ──────────────────────────────────────────────────────────────────

def bar(used, tone, height=10):
    colour = TONE.get(tone, TONE["unknown"])
    if used is None:
        return ("<div class='bar' style='height:%dpx'><div class='bar-unknown'>"
                "</div></div>" % height)
    return ("<div class='bar' style='height:%dpx'><div class='bar-fill' "
            "style='width:%.1f%%;background:%s'></div></div>"
            % (height, max(0.0, used), colour))


def sparkline(trend, accent, unit):
    """Daily trend. The peak day is called out, because a bar chart with no
    scale answers 'busier than yesterday?' but never 'busier than what?'."""
    if not trend:
        return ""
    peak = max(value for _, value in trend) or 1.0
    peak_day = max(trend, key=lambda row: row[1])[0]
    cells = []
    ticks = []
    for index, (day, value) in enumerate(trend):
        last = index == len(trend) - 1
        label = "%s · %s" % (day, fmt_usd(value) if unit == "$" else fmt_tokens(value))
        cells.append(
            "<div class='spark-col' title='%s'><div class='spark-bar' style="
            "'height:%.0f%%;background:%s;opacity:%s'></div></div>"
            % (esc(label), max(3.0, value / peak * 100), accent, "1" if last else "0.62"))
        ticks.append("<i>%s</i>" % (day[-2:] if (last or index % 3 == 0) else ""))
    top = fmt_usd(peak) if unit == "$" else fmt_tokens(peak)
    return ("<div class='spark-head'><span>최근 %d일</span><span>최대 %s / %s</span></div>"
            "<div class='spark'>%s</div><div class='spark-ax'>%s</div>"
            % (len(trend), esc(top), esc(peak_day[5:]), "".join(cells), "".join(ticks)))


def heatmap(heat, accent, title):
    if not heat:
        return ""
    grid, seen = heat["grid"], heat["seen"]
    peak = max((max(row) for row in grid), default=0) or 1.0
    rows = []
    for day in range(7):
        cells = []
        for hour in range(24):
            value = grid[day][hour]
            if not seen[day][hour]:
                cells.append("<i class='hc none' title='미관찰'></i>")
                continue
            if value <= 0:
                cells.append("<i class='hc zero' title='%s %02d시 · 0'></i>"
                             % (DAYS[day], hour))
                continue
            level = value / peak
            alpha = 0.25 + 0.75 * min(1.0, level ** 0.5)
            label = "%s %02d시 · %s" % (
                DAYS[day], hour,
                fmt_usd(value) if heat["unit"] == "$" else fmt_tokens(value))
            cells.append("<i class='hc' style='background:%s;opacity:%.2f' title='%s'></i>"
                         % (accent, alpha, esc(label)))
        rows.append("<div class='hrow'><span class='hday'>%s</span>%s</div>"
                    % (DAYS[day], "".join(cells)))
    ruler = "".join("<i class='hc tick'>%s</i>" % (str(h) if h % 6 == 0 else "")
                    for h in range(24))
    return (
        "<div class='heat'><div class='heat-title'>%s</div>%s"
        "<div class='hrow ruler'><span class='hday'></span>%s</div></div>"
        % (esc(title), "".join(rows), ruler))


def limit_row(limit, hero=False):
    tone = limit.get("bar_tone") or "unknown"
    colour = TONE.get(tone, TONE["unknown"])
    badge = ("<span class='badge measured'>실측</span>" if limit.get("measured")
             else "<span class='badge budget'>예산 기준</span>")
    if hero:
        return (
            "<div class='hero'>"
            "<div class='hero-num' style='color:%s'>%s</div>"
            "<div class='hero-meta'><div class='hero-label'>%s %s</div>"
            "<div class='hero-sub'>%s</div></div>"
            "<div class='hero-reset'><b>%s</b><span>리셋</span></div>"
            "</div>%s"
            % (colour, pct_text(limit.get("used")), esc(limit.get("label")), badge,
               esc(limit.get("sub")), esc(limit.get("reset_text")),
               bar(limit.get("used"), tone, 12)))
    return (
        "<div class='lim'><div class='lim-top'><span>%s %s</span>"
        "<span class='lim-val' style='color:%s'>%s</span></div>%s"
        "<div class='lim-sub'>%s / %s · 리셋 %s</div></div>"
        % (esc(limit.get("label")), badge, colour, pct_text(limit.get("used")),
           bar(limit.get("used"), tone, 6), esc(limit.get("primary_text")),
           esc(limit.get("budget_text")), esc(limit.get("reset_text"))))


def stat_tiles(provider):
    tiles = []
    for name, label in (("today", "오늘"), ("week", "7일"), ("day30", "30일")):
        tiles.append("<div class='tile'><span class='tile-l'>%s</span>"
                     "<b>%s</b><span class='tile-s'>%s</span></div>"
                     % (label, esc(window_value(provider, name)),
                        esc(window_sub(provider, name))))
    return "<div class='tiles'>%s</div>" % "".join(tiles)


def model_chips(provider):
    chips = []
    for model in (provider.get("models") or [])[:3]:
        value = (fmt_usd(model["cost"]) if model.get("cost") is not None
                 else fmt_tokens(model["tokens"]))
        chips.append("<span class='chip'>%s <b>%s</b></span>"
                     % (esc(model["name"]), esc(value)))
    if not chips:
        return ""
    return "<div class='chips'><span class='chips-l'>7일 모델</span>%s</div>" % "".join(chips)


def cost_split(provider):
    """Claude's seven-day total, plan proxy, and Fable credit equivalent."""
    groups = provider.get("cost_split") or {}
    if not groups:
        return ""
    chips = []
    for key in ("all", "subscription", "fable"):
        group = groups.get(key)
        if not group:
            continue
        chips.append("<span class='chip %s'>%s <b>%s</b></span>" % (
            esc(key), esc(group.get("label")), esc(fmt_usd(group.get("cost")))))
    return ("<div class='chips cost-split'><span class='chips-l'>최근 7일 환산</span>"
            "%s</div>" % "".join(chips))


def provider_card(provider):
    accent = ACCENT.get(provider["id"], "#8b95a1")
    limits = provider.get("limits") or []
    if not limits:
        body = ("<div class='empty'>사용률 없음<br><span>%s</span></div>"
                % esc(provider.get("note")))
    else:
        extra = "".join(limit_row(l) for l in limits[1:])
        body = limit_row(limits[0], hero=True) + extra
        if provider.get("status") != "ok":
            body += "<div class='metric-note'>%s</div>" % esc(provider.get("note"))
    meta = []
    if provider.get("plan"):
        meta.append("plan %s" % provider["plan"])
    if provider.get("last_ms"):
        meta.append("마지막 %s 전" % fmt_age(provider["last_ms"]))
    burn = provider.get("burn") or {}
    if burn.get("cost_per_hour"):
        prefix = "구독 최근 1h" if provider["id"] == "claude-code" else "최근 1h"
        meta.append("%s %s/h" % (prefix, fmt_usd(burn["cost_per_hour"])))
    if provider.get("projection") is not None:
        meta.append("이 속도면 블록 끝 %s" % pct_text(provider["projection"]))
    return (
        "<section class='card' style='--accent:%s'>"
        "<header><h2>%s</h2><span class='meta'>%s</span></header>%s</section>"
        % (accent, esc(provider["label"]), esc(" · ".join(meta)), body))


# ── themes ──────────────────────────────────────────────────────────────────

SURFACER_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0c0f}
#stage{background:#0a0c0f;color:#e6e9ee;
 font-family:'Pretendard','Segoe UI Variable','Segoe UI','Malgun Gothic',sans-serif;
 font-size:13px;padding:14px 16px;display:flex;flex-direction:column;gap:10px}
.top{display:flex;align-items:baseline;gap:10px}
.top h1{font-size:15px;letter-spacing:.14em;font-weight:700;color:#f4f6f8}
.top .scope{color:#69737f;font-size:11px}
.top .stamp{margin-left:auto;color:#69737f;font-size:11px;font-variant-numeric:tabular-nums}
.banner{border-radius:9px;padding:9px 13px;font-size:14px;font-weight:600;
 border:1px solid rgba(255,255,255,.07);background:#12161c;display:flex;gap:9px;align-items:center}
.banner i{width:8px;height:8px;border-radius:50%;flex:none}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;flex:1;min-height:0}
.card{background:#11151a;border:1px solid #1d232b;border-top:2px solid var(--accent);
 border-radius:11px;padding:10px 14px 11px;display:flex;flex-direction:column;gap:6px}
.card header{display:flex;align-items:baseline;gap:8px}
.card h2{font-size:12px;letter-spacing:.16em;font-weight:700;color:var(--accent)}
.card .meta{margin-left:auto;color:#69737f;font-size:10.5px;text-align:right}
.hero{display:flex;align-items:flex-end;gap:12px}
.hero-num{font-size:46px;line-height:.94;font-weight:800;font-variant-numeric:tabular-nums}
.hero-meta{padding-bottom:3px}
.hero-label{font-size:12px;color:#c7ced7;font-weight:600}
.hero-sub{font-size:11px;color:#7b8590;margin-top:2px;font-variant-numeric:tabular-nums}
.hero-reset{margin-left:auto;text-align:right;padding-bottom:3px}
.hero-reset b{display:block;font-size:15px;font-variant-numeric:tabular-nums;white-space:nowrap}
.hero-reset span{font-size:10px;color:#69737f;letter-spacing:.1em}
.bar{width:100%;background:#1b212a;border-radius:999px;overflow:hidden}
.bar-fill{height:100%;border-radius:999px}
.bar-unknown{height:100%;background:repeating-linear-gradient(90deg,#252c36 0 6px,#1b212a 6px 12px)}
.lim{margin-top:1px}
.lim-top{display:flex;font-size:11px;color:#a8b1bb;margin-bottom:4px;align-items:center}
.lim-val{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums}
.lim-sub{font-size:10.5px;color:#69737f;margin-top:3px;font-variant-numeric:tabular-nums}
.badge{font-size:9px;letter-spacing:.06em;padding:1px 5px;border-radius:4px;margin-left:5px;
 vertical-align:1px;font-weight:700}
.badge.measured{background:rgba(61,220,132,.14);color:#3ddc84}
.badge.budget{background:rgba(255,255,255,.07);color:#8b95a1}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:1px}
.tile{background:#0d1116;border:1px solid #1b212a;border-radius:8px;padding:5px 9px}
.tile-l{display:block;font-size:10px;color:#69737f;letter-spacing:.08em}
.tile b{display:block;font-size:17px;font-weight:700;margin:1px 0;font-variant-numeric:tabular-nums}
.tile-s{display:block;font-size:9.5px;color:#69737f;font-variant-numeric:tabular-nums}
.chips{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.chips-l{font-size:10px;color:#69737f;letter-spacing:.08em}
.chip{font-size:10.5px;color:#a8b1bb;background:#0d1116;border:1px solid #1b212a;
 border-radius:999px;padding:2px 8px}
.chip b{color:#e6e9ee}
.cost-split .fable b{color:var(--accent)}
.spark-head{display:flex;font-size:9.5px;color:#69737f;margin-top:auto;justify-content:space-between;letter-spacing:.06em}
.spark{display:flex;align-items:flex-end;gap:3px;height:26px;margin-top:2px}
.spark-ax{display:flex;gap:3px;margin-top:2px}
.spark-ax i{flex:1;font-style:normal;font-size:8.5px;color:#4e5761;text-align:center}
.spark-col{flex:1;height:100%;display:flex;align-items:flex-end}
.spark-bar{width:100%;border-radius:2px 2px 0 0}
.empty{color:#69737f;font-size:13px;padding:24px 0;text-align:center}
.empty span{font-size:11px;color:#4e5761}
.metric-note{font-size:10px;color:#f2c14e;margin-top:5px}
.heats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:auto}
.heat{background:#11151a;border:1px solid #1d232b;border-radius:11px;padding:10px 12px 8px}
.heat-title{font-size:10px;color:#69737f;letter-spacing:.1em;margin-bottom:6px}
.hrow{display:flex;align-items:center;gap:2px;margin-bottom:2px}
.hday{width:16px;font-size:9px;color:#5b646e;flex:none}
.hc{width:100%;height:11px;border-radius:2px;background:#161b22;display:block}
.hc.zero{background:#151a21}
.hc.none{background:transparent;box-shadow:inset 0 0 0 1px #171d24}
.ruler .hc{height:auto;background:none;font-size:8px;color:#4e5761;font-style:normal;text-align:left}
.foot{color:#4e5761;font-size:10px;text-align:center}
"""

PHOSPHOR_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#04070a}
#stage{background:#04070a;color:#7ef7c0;
 font-family:'Cascadia Mono','Consolas','D2Coding',monospace;font-size:13px;
 padding:14px 18px;display:flex;flex-direction:column;gap:9px;
 text-shadow:0 0 6px rgba(70,255,180,.22)}
body:after{content:'';position:fixed;inset:0;pointer-events:none;
 background:repeating-linear-gradient(180deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px)}
.top{display:flex;gap:10px;align-items:baseline;border-bottom:1px solid #123a2e}
.top h1{font-size:14px;letter-spacing:.2em}
.top .scope,.top .stamp{color:#2f7f63;font-size:11px}
.top .stamp{margin-left:auto}
.banner{border:1px solid #17513e;padding:7px 11px;font-size:13px;display:flex;gap:9px;align-items:center}
.banner i{width:7px;height:7px;flex:none}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;flex:1;min-height:0}
.card{border:1px solid #164536;padding:8px 12px;display:flex;flex-direction:column;gap:6px}
.card header{display:flex;gap:8px;align-items:baseline}
.card h2{font-size:12px;letter-spacing:.2em;color:var(--accent)}
.card .meta{margin-left:auto;color:#2f7f63;font-size:10px;text-align:right}
.hero{display:flex;align-items:flex-end;gap:10px}
.hero-num{font-size:40px;line-height:1;font-weight:700}
.hero-label{font-size:12px}
.hero-sub{font-size:11px;color:#3d9c7b}
.hero-reset{margin-left:auto;text-align:right}
.hero-reset b{display:block;font-size:14px;white-space:nowrap}
.hero-reset span{font-size:9px;color:#2f7f63}
.bar{width:100%;background:#0b1a15;border:1px solid #164536}
.bar-fill{height:100%}
.bar-unknown{height:100%;background:repeating-linear-gradient(90deg,#16382c 0 4px,transparent 4px 8px)}
.lim-top{display:flex;font-size:11px;margin-bottom:3px}
.lim-val{margin-left:auto}
.lim-sub{font-size:10px;color:#2f7f63;margin-top:2px}
.badge{font-size:9px;margin-left:5px;color:#2f7f63}
.badge.measured{color:#7ef7c0}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.tile{border:1px dashed #164536;padding:4px 8px}
.tile-l{display:block;font-size:9px;color:#2f7f63}
.tile b{display:block;font-size:16px}
.tile-s{display:block;font-size:9px;color:#2f7f63}
.chips{display:flex;gap:8px;font-size:10px;color:#3d9c7b;flex-wrap:wrap}
.chip b{color:#7ef7c0}
.cost-split .fable b{color:var(--accent)}
.spark-head{display:flex;font-size:9px;color:#2f7f63;margin-top:auto;justify-content:space-between}
.spark{display:flex;align-items:flex-end;gap:2px;height:24px;margin-top:2px}
.spark-ax{display:flex;gap:2px;margin-top:2px}
.spark-ax i{flex:1;font-style:normal;font-size:8.5px;color:#2f7f63;text-align:center}
.spark-col{flex:1;height:100%;display:flex;align-items:flex-end}
.spark-bar{width:100%}
.empty{padding:22px 0;text-align:center;color:#2f7f63}
.metric-note{font-size:10px;color:#f2c14e;margin-top:5px}
.heats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:auto}
.heat{border:1px solid #164536;padding:8px 10px}
.heat-title{font-size:9px;color:#2f7f63;letter-spacing:.14em;margin-bottom:5px}
.hrow{display:flex;align-items:center;gap:2px;margin-bottom:2px}
.hday{width:15px;font-size:9px;color:#2f7f63;flex:none}
.hc{width:100%;height:10px;background:#0b1a15;display:block}
.hc.zero{background:#0a1512}
.hc.none{background:transparent;box-shadow:inset 0 0 0 1px #10352a}
.ruler .hc{height:auto;background:none;font-size:8px;color:#2f7f63;font-style:normal}
.foot{color:#2f7f63;font-size:10px;text-align:center}
"""

MINI_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0c0f}
#stage{background:#0a0c0f;color:#e6e9ee;
 font-family:'Pretendard','Segoe UI','Malgun Gothic',sans-serif;font-size:12px;padding:9px 11px}
.mtop{display:flex;align-items:center;gap:7px;margin-bottom:7px}
.mtop b{font-size:11px;letter-spacing:.14em;color:#c7ced7}
.mtop span{margin-left:auto;color:#5b646e;font-size:10px;font-variant-numeric:tabular-nums}
.mrow{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.mtag{width:58px;font-size:10px;color:#8b95a1;letter-spacing:.04em;flex:none}
.mbar{flex:1;height:9px;background:#1b212a;border-radius:999px;overflow:hidden}
.mbar i{display:block;height:100%;border-radius:999px}
.mbar .unk{background:repeating-linear-gradient(90deg,#252c36 0 5px,#1b212a 5px 10px)}
.mval{width:46px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums;flex:none}
.mrs{width:52px;text-align:right;color:#69737f;font-size:10px;flex:none;
 font-variant-numeric:tabular-nums}
.mfoot{color:#5b646e;font-size:10px;border-top:1px solid #1b212a;padding-top:6px}
"""


def screen_scale() -> float:
    """Display scale factor (144 DPI -> 1.5).

    Read from the registry rather than asked of Windows: this worker is a
    DPI-unaware process, and GetDpiForSystem answers a flat 96 for those. The
    tray uses the same key as its own fallback.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\Desktop\WindowMetrics") as key:
            dpi = int(winreg.QueryValueEx(key, "AppliedDPI")[0])
        return max(1.0, min(2.5, dpi / 96.0))
    except Exception:
        return 1.0


def _shell(title, css, body, width, height):
    """Wrap a fixed-size layout in a stage that scales itself to the viewport.

    The window is asked for `width x height` DEVICE pixels (data-w/data-h, which
    webview2-host.ps1 regex-matches out of this file), sized up by the display
    scale so the board keeps its intended physical size on a 144 DPI screen.
    WebView2 then reports a CSS viewport of its own choosing, so the stage
    measures what it actually got and scales to fit - correct at any DPI, and
    no layout is ever clipped.
    """
    scale = screen_scale()
    # The fit must survive being laid out while the window is MINIMIZED or parked
    # off-screen: innerWidth is 0 there, and a naive scale(0) leaves a window that
    # stays blank forever once restored (the host only re-navigates when its own
    # dirty flag is set, which it is not after a successful navigate). So clamp a
    # non-positive scale to 1 and recompute on every event that can change the
    # viewport - resize, restore, and a slow timer as the backstop.
    fit = (
        "(function(){var w=%d,h=%d,e=document.getElementById('stage');"
        "function f(){var s=Math.min(innerWidth/w,innerHeight/h);"
        "if(!(s>0)||!isFinite(s))s=1;"
        "e.style.transform='scale('+s+')';"
        "e.style.left=((innerWidth-w*s)/2)+'px';"
        "e.style.top=((innerHeight-h*s)/2)+'px';}"
        "addEventListener('resize',f);addEventListener('pageshow',f);"
        "document.addEventListener('visibilitychange',f);"
        "if(window.ResizeObserver){new ResizeObserver(f).observe("
        "document.documentElement);}"
        "setInterval(f,2000);f();})();" % (width, height)
    )
    stage_css = (
        "html,body{width:100%%;height:100%%;margin:0;overflow:hidden}"
        "#stage{position:absolute;left:0;top:0;width:%dpx;height:%dpx;"
        "transform-origin:0 0}" % (width, height)
    )
    return (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='color-scheme' content='dark'><title>%s</title>"
        "<style>%s%s</style></head>"
        "<body data-w='%d' data-h='%d'><div id='stage'>%s</div>"
        "<script>%s</script></body></html>"
        % (esc(title), css, stage_css, round(width * scale), round(height * scale),
           body, fit))


def _board(snap, css, theme):
    banner = snap["gui_model"]["banner"]
    colour = TONE.get(banner.get("tone"), TONE["unknown"])
    providers = snap["providers"]
    cards = "".join(provider_card(p) for p in providers)
    body = (
        "<div class='top'><h1>q_console</h1>"
        "<span class='scope'>현재 로그인 계정의 공식 Usage</span>"
        "<span class='stamp'>%s · %s 전</span></div>"
        "<div class='banner'><i style='background:%s'></i>%s</div>"
        "<div class='cards'>%s</div>"
        "<div class='foot'>Claude Code 전체 · Fable · Codex 주간 사용률만 표시 · "
        "금액/토큰 환산 없음 · 조회 실패는 -- 로 표시</div>"
        % (esc(snap["generated_stamp"]), esc(fmt_age(snap["generated_at_ms"])),
           colour, esc(banner.get("text")), cards))
    return _shell("q_console / %s" % theme, css, body, 960, 260)


# The mini strip is 400 px wide: every row has to name itself in ~7 characters,
# so provider and window collapse to codes rather than wrapping onto two lines.
MINI_TAG = {"claude-code": "Claude", "fable": "Fable", "codex": "Codex"}
MINI_KEY = {"week": ""}


def _mini(snap):
    rows = []
    for provider in snap["providers"]:
        limits = provider.get("limits") or []
        code = MINI_TAG.get(provider["id"], provider["label"][:2])
        if not limits:
            rows.append(
                "<div class='mrow'><span class='mtag'>%s</span>"
                "<div class='mbar'><i class='unk' style='width:100%%'></i></div>"
                "<span class='mval'>--</span><span class='mrs'>기록없음</span></div>"
                % esc(code))
            continue
        for limit in limits:
            tone = TONE.get(limit.get("bar_tone"), TONE["unknown"])
            used = limit.get("used")
            fill = ("<i class='unk' style='width:100%'></i>" if used is None else
                    "<i style='width:%.1f%%;background:%s'></i>" % (max(0.0, used), tone))
            tag = "%s %s" % (code, MINI_KEY.get(limit.get("key"), limit.get("key") or ""))
            reset = ("롤링" if limit.get("reset_in") is None
                     else limit.get("reset_text"))
            rows.append(
                "<div class='mrow'><span class='mtag'>%s</span>"
                "<div class='mbar'>%s</div>"
                "<span class='mval' style='color:%s'>%s</span>"
                "<span class='mrs'>%s</span></div>"
                % (esc(tag), fill, tone, pct_text(used), esc(reset)))
    banner = snap["gui_model"]["banner"]
    body = (
        "<div class='mtop'><b>q_console</b><span>%s · %s 전</span></div>%s"
        "<div class='mfoot'>%s</div>"
        % (esc(snap["generated_stamp"]), esc(fmt_age(snap["generated_at_ms"])),
           "".join(rows), esc(banner.get("text"))))
    height = 34 + 25 * len(rows) + 34
    return _shell("q_console / mini", MINI_CSS, body, 400, height)


def render(snap: dict, theme: str) -> str:
    if theme == "mini":
        return _mini(snap)
    if theme == "phosphor":
        return _board(snap, PHOSPHOR_CSS, "phosphor")
    return _board(snap, SURFACER_CSS, "surfacer")


# ── overlay strip ───────────────────────────────────────────────────────────
# A one-line always-on-top HUD with exactly three account percentages:
# Claude Code, Fable, and Codex (with the ChatGPT mark).
# Written to overlay.html on every refresh; the tray's overlay mode shows this
# document instead of the board. Icons instead of provider names - the whole
# point is to cost as few pixels as possible.

ICON_CLAUDE = (
    "<svg class='oi' viewBox='0 0 16 16'><g stroke='#e08257' stroke-width='2.1'"
    " stroke-linecap='round'><path d='M8 1.5v13M1.5 8h13M3.4 3.4l9.2 9.2"
    "M12.6 3.4l-9.2 9.2'/></g></svg>")          # 8-spoke spark
# Official OpenAI monoblossom used by ChatGPT, embedded so the overlay stays
# fully offline: https://learn.chatgpt.com/assets/OpenAI-black-monoblossom.svg
ICON_CODEX = (
    "<svg class='oi' viewBox='118.557 119.958 484.139 479.818' "
    "aria-label='ChatGPT'><path d='M304.246 294.611V249.028C304.246 245.189 "
    "305.687 242.309 309.044 240.392L400.692 187.612C413.167 180.415 428.042 "
    "177.058 443.394 177.058C500.971 177.058 537.44 221.682 537.44 "
    "269.182C537.44 272.54 537.44 276.379 536.959 280.218L441.954 "
    "224.558C436.197 221.201 430.437 221.201 424.68 224.558L304.246 "
    "294.611ZM518.245 472.145V363.224C518.245 356.505 515.364 351.707 "
    "509.608 348.349L389.174 278.296L428.519 255.743C431.877 253.826 "
    "434.757 253.826 438.115 255.743L529.762 308.523C556.154 323.879 "
    "573.905 356.505 573.905 388.171C573.905 424.636 552.315 458.225 "
    "518.245 472.141V472.145ZM275.937 376.182L236.592 353.152C233.235 "
    "351.235 231.794 348.354 231.794 344.515V238.956C231.794 187.617 "
    "271.139 148.749 324.4 148.749C344.555 148.749 363.264 155.468 "
    "379.102 167.463L284.578 222.164C278.822 225.521 275.942 230.319 "
    "275.942 237.039V376.186L275.937 376.182ZM360.626 425.122L304.246 "
    "393.455V326.283L360.626 294.616L417.002 326.283V393.455L360.626 "
    "425.122ZM396.852 570.989C376.698 570.989 357.989 564.27 342.151 "
    "552.276L436.674 497.574C442.431 494.217 445.311 489.419 445.311 "
    "482.699V343.552L485.138 366.582C488.495 368.499 489.936 371.379 "
    "489.936 375.219V480.778C489.936 532.117 450.109 570.985 396.852 "
    "570.985V570.989ZM283.134 463.99L191.486 411.211C165.094 395.854 "
    "147.343 363.229 147.343 331.562C147.343 294.616 169.415 261.509 "
    "203.48 247.593V356.991C203.48 363.71 206.361 368.508 212.117 "
    "371.866L332.074 441.437L292.729 463.99C289.372 465.907 286.491 "
    "465.907 283.134 463.99ZM277.859 542.68C223.639 542.68 183.813 "
    "501.895 183.813 451.514C183.813 447.675 184.294 443.836 184.771 "
    "439.997L279.295 494.698C285.051 498.056 290.812 498.056 296.568 "
    "494.698L417.002 425.127V470.71C417.002 474.549 415.562 477.429 "
    "412.204 479.346L320.557 532.126C308.081 539.323 293.206 542.68 "
    "277.854 542.68H277.859ZM396.852 599.776C454.911 599.776 503.37 "
    "558.513 514.41 503.812C568.149 489.896 602.696 439.515 602.696 "
    "388.176C602.696 354.587 588.303 321.962 562.392 298.45C564.791 "
    "288.373 566.231 278.296 566.231 268.224C566.231 199.611 510.571 "
    "148.267 446.274 148.267C433.322 148.267 420.846 150.184 408.37 "
    "154.505C386.775 133.392 357.026 119.958 324.4 119.958C266.342 "
    "119.958 217.883 161.22 206.843 215.921C153.104 229.837 118.557 "
    "280.218 118.557 331.557C118.557 365.146 132.95 397.771 158.861 "
    "421.283C156.462 431.36 155.022 441.437 155.022 451.51C155.022 "
    "520.123 210.682 571.466 274.978 571.466C287.931 571.466 300.407 "
    "569.549 312.883 565.228C334.473 586.341 364.222 599.776 396.852 "
    "599.776Z' fill='#37c9a3'/></svg>")

OVERLAY_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0c0f13;cursor:move;user-select:none;-webkit-user-select:none;touch-action:none}
#stage{display:flex;align-items:center;gap:10px;padding:0 12px;
 background:#0c0f13;border-top:1px solid #232a33;color:#e6e9ee;
 font-family:'Pretendard','Segoe UI','Malgun Gothic',sans-serif;font-size:12.5px;
 white-space:nowrap;overflow:hidden}
.seg{display:flex;align-items:center;gap:6px}
.oi{width:13px;height:13px;flex:none}
.k{font-size:9.5px;color:#69737f;letter-spacing:.04em}
.v{font-weight:700;font-variant-numeric:tabular-nums}
.r{font-size:10px;color:#7b8590;font-variant-numeric:tabular-nums}
.div{width:1px;height:14px;background:#232a33}
.age{margin-left:auto;font-size:9.5px;color:#4e5761;font-variant-numeric:tabular-nums}
"""

OVERLAY_DRAG_SCRIPT = (
    "(function(){var active=false,w=window.chrome&&window.chrome.webview;"
    "if(!w)return;document.addEventListener('dragstart',function(e){e.preventDefault();});"
    "document.addEventListener('pointerdown',function(e){if(e.button!==0)return;"
    "active=true;e.preventDefault();try{document.body.setPointerCapture(e.pointerId);}catch(_){}"
    "w.postMessage('q_console:drag-start');});"
    "document.addEventListener('pointermove',function(e){if(active){e.preventDefault();"
    "w.postMessage('q_console:drag-move');}});"
    "function end(e){if(!active)return;active=false;e.preventDefault();"
    "w.postMessage('q_console:drag-end');}"
    "document.addEventListener('pointerup',end);document.addEventListener('pointercancel',end);})();"
)


def render_overlay(snap: dict) -> str:
    by_id = {provider.get("id"): provider for provider in snap.get("providers") or []}

    def metric(provider_id, label):
        provider = by_id.get(provider_id) or {}
        limits = provider.get("limits") or []
        limit = limits[0] if limits else {}
        tone = TONE.get(limit.get("bar_tone"), TONE["unknown"])
        value = pct_text(limit.get("used"))
        reset = limit.get("reset_text") or "--"
        html_value = ("<span class='k'>%s</span>"
                      "<span class='v' style='color:%s'>%s</span>"
                      "<span class='r'>(%s)</span>"
                      % (esc(label), tone, esc(value), esc(reset)))
        return html_value, value, reset

    claude, claude_value, claude_reset = metric("claude-code", "Claude")
    fable, fable_value, fable_reset = metric("fable", "Fable")
    codex, codex_value, codex_reset = metric("codex", "Codex")
    segments = [
        "<div class='seg'>%s%s%s</div>" % (ICON_CLAUDE, claude, fable),
        "<div class='seg'>%s%s</div>" % (ICON_CODEX, codex),
    ]
    width = (24 + 38 + len(claude_value) * 7 + len(fable_value) * 7 +
             len(codex_value) * 7 + 6 * (len("Claude") + len("Fable") +
                                         len("Codex") + len(claude_reset) +
                                         len(fable_reset) + len(codex_reset)) + 62)
    body = "<div class='div'></div>".join(segments)
    stamp = "<span class='age'>%s</span>" % esc(snap.get("generated_stamp", "")[-5:])
    width += 44
    return _shell("q_console overlay", OVERLAY_CSS,
                  body + stamp + "<script>%s</script>" % OVERLAY_DRAG_SCRIPT,
                  max(320, min(700, width)), 32)
