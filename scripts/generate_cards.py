#!/usr/bin/env python3
"""Render GitHub profile stat cards as self-contained SVGs.

Queries the GitHub GraphQL API and writes three cards to --out:

  stats.svg      headline totals, led by a hero commit figure
  languages.svg  part-to-whole stacked bar of language bytes
  activity.svg   contribution heatmap plus streak figures

Needs a token in GITHUB_TOKEN (public_repo is enough). No third-party
service is involved at render or at display time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com/graphql"

# --- design tokens -----------------------------------------------------------
# Single dark surface: these render inside an <img>, which cannot see the
# page's colour scheme, so the card carries its own background either way.
SURFACE = "#0d1117"
BORDER = "#30363d"
INK = "#e6edf3"      # primary text
INK_MUTED = "#8b949e"  # secondary text
ACCENT = "#7a7adb"     # profile accent

# Categorical slots, validated for a dark surface (adjacent-pair CVD dE 8.4,
# normal-vision 19.3). Fixed order, never cycled; an overflow folds into OTHER.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500",
          "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER = "#6e7681"

# Sequential ramp for the heatmap: one hue, monotonically lighter -> darker.
HEAT = ["#161b22", "#2b2a63", "#453f9e", "#6a5fd0", "#9d95f5"]

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif")


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def comma(n: int) -> str:
    return f"{n:,}"


def graphql(query: str, variables: dict, token: str) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API, data=body,
        headers={"Authorization": f"bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "profile-cards"})
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    if "errors" in payload:
        raise SystemExit("GraphQL error: " +
                         "; ".join(e.get("message", "?") for e in payload["errors"]))
    return payload["data"]


# --- data --------------------------------------------------------------------

PROFILE_Q = """
query($login:String!){
  user(login:$login){
    name login createdAt
    followers{totalCount}
    following{totalCount}
    starredRepositories{totalCount}
    repositories(ownerAffiliations:OWNER, privacy:PUBLIC){totalCount}
    repositoriesContributedTo(contributionTypes:[COMMIT,PULL_REQUEST,ISSUE,REPOSITORY]){totalCount}
    pullRequests{totalCount}
    issues{totalCount}
    langs:repositories(first:100, ownerAffiliations:OWNER, isFork:false,
                       orderBy:{field:PUSHED_AT, direction:DESC}){
      nodes{ languages(first:10, orderBy:{field:SIZE, direction:DESC}){
        edges{ size node{ name } } } }
    }
  }
}"""

YEAR_Q = """
query($login:String!,$from:DateTime!,$to:DateTime!){
  user(login:$login){
    contributionsCollection(from:$from,to:$to){
      totalCommitContributions
      totalPullRequestReviewContributions
      contributionCalendar{ weeks{ contributionDays{ date contributionCount } } }
    }
  }
}"""


def fetch(login: str, token: str) -> dict:
    prof = graphql(PROFILE_Q, {"login": login}, token)["user"]
    if not prof:
        raise SystemExit(f"No such user: {login}")

    created = dt.datetime.fromisoformat(prof["createdAt"].replace("Z", "+00:00"))
    now = dt.datetime.now(dt.timezone.utc)

    commits = 0
    reviews = 0
    days: dict[str, int] = {}
    year = created.year
    while year <= now.year:
        frm = max(created, dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc))
        to = min(now, dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc))
        cc = graphql(YEAR_Q, {"login": login,
                              "from": frm.isoformat().replace("+00:00", "Z"),
                              "to": to.isoformat().replace("+00:00", "Z")},
                     token)["user"]["contributionsCollection"]
        commits += cc["totalCommitContributions"]
        reviews += cc["totalPullRequestReviewContributions"]
        for w in cc["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                days[d["date"]] = d["contributionCount"]
        year += 1

    langs: dict[str, int] = {}
    for repo in prof["langs"]["nodes"]:
        for e in repo["languages"]["edges"]:
            langs[e["node"]["name"]] = langs.get(e["node"]["name"], 0) + e["size"]

    return {
        "name": prof["name"] or prof["login"],
        "login": prof["login"],
        "created": created,
        "followers": prof["followers"]["totalCount"],
        "starred": prof["starredRepositories"]["totalCount"],
        "repos": prof["repositories"]["totalCount"],
        "contributed": prof["repositoriesContributedTo"]["totalCount"],
        "prs": prof["pullRequests"]["totalCount"],
        "issues": prof["issues"]["totalCount"],
        "commits": commits,
        "reviews": reviews,
        "days": days,
        "langs": langs,
    }


def streaks(days: dict[str, int]) -> tuple[int, int, int, str]:
    """Current streak, longest streak, best single day, that day's date."""
    if not days:
        return 0, 0, 0, ""
    today = dt.date.today()
    dates = sorted(d for d in days if dt.date.fromisoformat(d) <= today)
    best = cur = longest = 0
    prev = None
    for d in dates:
        day = dt.date.fromisoformat(d)
        if days[d] > 0:
            cur = cur + 1 if prev and (day - prev).days == 1 else 1
            longest = max(longest, cur)
            prev = day
        else:
            cur = 0
            prev = None
    # Current streak: walk back from today (an empty today is not yet a break).
    run = 0
    probe = today
    if days.get(probe.isoformat(), 0) == 0:
        probe -= dt.timedelta(days=1)
    while days.get(probe.isoformat(), 0) > 0:
        run += 1
        probe -= dt.timedelta(days=1)
    peak_day = max(dates, key=lambda d: days[d])
    return run, longest, days[peak_day], peak_day


# --- drawing helpers ---------------------------------------------------------

def card(width: int, height: int, title: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" \
viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">
<style>
  .t{{font-family:{FONT};}}
  .title{{font-size:13px;font-weight:600;fill:{ACCENT};}}
  .hero{{font-size:46px;font-weight:700;fill:{INK};}}
  .val{{font-size:19px;font-weight:600;fill:{INK};}}
  .lab{{font-size:11px;fill:{INK_MUTED};letter-spacing:.02em;}}
  .leg{{font-size:11px;fill:{INK};}}
  .pct{{font-size:11px;fill:{INK_MUTED};}}
</style>
<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12"
      fill="{SURFACE}" stroke="{BORDER}"/>
<g class="t">
  <rect x="20" y="20" width="3" height="13" rx="1.5" fill="{ACCENT}"/>
  <text x="31" y="31" class="title">{esc(title)}</text>
  {body}
</g>
</svg>
"""


def tile(x: int, y: int, value: str, label: str) -> str:
    return (f'<text x="{x}" y="{y}" class="val">{esc(value)}</text>'
            f'<text x="{x}" y="{y + 16}" class="lab">{esc(label)}</text>')


# --- cards -------------------------------------------------------------------

def card_stats(d: dict) -> str:
    years = (dt.datetime.now(dt.timezone.utc) - d["created"]).days // 365
    b = [
        f'<text x="20" y="96" class="hero">{comma(d["commits"])}</text>',
        f'<text x="20" y="115" class="lab">Total commits &#183; {years} years on GitHub</text>',
    ]
    stats = [(comma(d["prs"]), "Pull requests"),
             (comma(d["reviews"]), "Reviews"),
             (comma(d["issues"]), "Issues"),
             (comma(d["repos"]), "Repositories"),
             (comma(d["contributed"]), "Contributed to"),
             (comma(d["followers"]), "Followers")]
    for i, (v, l) in enumerate(stats):
        b.append(tile(20 + (i % 3) * 153, 158 + (i // 3) * 46, v, l))
    return card(480, 250, "GitHub stats", "\n  ".join(b))


def card_languages(d: dict) -> str:
    total = sum(d["langs"].values()) or 1
    ranked = sorted(d["langs"].items(), key=lambda kv: -kv[1])
    keep = [(n, v) for n, v in ranked if v / total >= 0.01]
    top = keep[:6]
    rest = total - sum(v for _, v in top)
    rows = [(n, v / total, SERIES[i]) for i, (n, v) in enumerate(top)]
    if rest:
        rows.append(("Other", rest / total, OTHER))

    x, bar_y, bar_w, bar_h, gap = 20, 56, 440, 22, 2
    b, cursor = [], float(x)
    n = len(rows)
    for i, (_, frac, color) in enumerate(rows):
        w = max(frac * (bar_w - gap * (n - 1)), 2.0)
        # Round only the outer ends so the bar reads as one track.
        r = 4
        if i == 0 or i == n - 1:
            b.append(f'<path d="{_pill(cursor, bar_y, w, bar_h, r, i == 0, i == n - 1)}" fill="{color}"/>')
        else:
            b.append(f'<rect x="{cursor:.1f}" y="{bar_y}" width="{w:.1f}" '
                     f'height="{bar_h}" fill="{color}"/>')
        cursor += w + gap

    for i, (name, frac, color) in enumerate(rows):
        col, row = i % 2, i // 2
        lx, ly = 20 + col * 220, 108 + row * 22
        b.append(f'<rect x="{lx}" y="{ly - 8}" width="9" height="9" rx="2" fill="{color}"/>')
        b.append(f'<text x="{lx + 15}" y="{ly}" class="leg">{esc(name)}</text>')
        b.append(f'<text x="{lx + 205}" y="{ly}" class="pct" text-anchor="end">'
                 f'{frac * 100:.1f}%</text>')

    # Match the stats card's height: inline images in the README are
    # baseline-aligned, so a shorter card would hang lower than its neighbour.
    height = max(108 + ((len(rows) + 1) // 2) * 22 + 14, 250)
    return card(480, height, "Languages", "\n  ".join(b))


def _pill(x: float, y: int, w: float, h: int, r: int, left: bool, right: bool) -> str:
    """Rect path with rounded corners on the outer end(s) only."""
    lr = r if left else 0
    rr = r if right else 0
    return (f"M{x + lr:.1f},{y} H{x + w - rr:.1f} "
            f"{f'a{rr},{rr} 0 0 1 {rr},{rr}' if rr else ''} V{y + h - rr} "
            f"{f'a{rr},{rr} 0 0 1 -{rr},{rr}' if rr else ''} H{x + lr:.1f} "
            f"{f'a{lr},{lr} 0 0 1 -{lr},-{lr}' if lr else ''} V{y + lr} "
            f"{f'a{lr},{lr} 0 0 1 {lr},-{lr}' if lr else ''} Z")


def card_activity(d: dict, width: int = 864) -> str:
    """Full-width activity card: 52-week heatmap, month rule, streak figures."""
    cur, longest, peak, _ = streaks(d["days"])
    today = dt.date.today()
    weeks = 52
    start = today - dt.timedelta(days=today.weekday() + 1 + 7 * (weeks - 1))

    # Only the drawn window feeds the scale. Thresholds taken over all years
    # would push almost every recent day into the top bucket.
    window = {}
    for i in range((today - start).days + 1):
        day = start + dt.timedelta(days=i)
        window[day] = d["days"].get(day.isoformat(), 0)
    live = sorted(v for v in window.values() if v > 0)
    year_peak = max(window.values()) if window else 0
    cuts = [live[int(len(live) * q)] for q in (0.25, 0.5, 0.75)] if live else [1, 1, 1]

    pad = 20
    step = (width - pad * 2) / weeks
    cell = step - 2.4
    top = 68
    b = []

    # Month rule: label a column when its month differs from the one before.
    seen = None
    for w in range(weeks):
        m = (start + dt.timedelta(days=w * 7)).strftime("%b")
        if m != seen:
            b.append(f'<text x="{pad + w * step:.1f}" y="{top - 8}" class="lab">{m}</text>')
            seen = m

    for w in range(weeks):
        for dow in range(7):
            day = start + dt.timedelta(days=w * 7 + dow)
            if day > today:
                continue
            c = window.get(day, 0)
            if c == 0:
                fill = HEAT[0]
            elif c <= cuts[0]:
                fill = HEAT[1]
            elif c <= cuts[1]:
                fill = HEAT[2]
            elif c <= cuts[2]:
                fill = HEAT[3]
            else:
                fill = HEAT[4]
            b.append(f'<rect x="{pad + w * step:.2f}" y="{top + dow * step:.2f}" '
                     f'width="{cell:.2f}" height="{cell:.2f}" rx="2" fill="{fill}"/>')

    grid_bottom = top + 7 * step
    b.append(f'<text x="{pad}" y="{grid_bottom + 20:.0f}" class="lab">'
             f'Past 12 months &#183; peak {comma(year_peak)} in a day</text>')
    tiles = [(comma(cur), "Current streak"),
             (comma(longest), "Longest streak"),
             (comma(peak), "Best day, all time"),
             (comma(d["contributed"]), "Repositories contributed to")]
    ty = int(grid_bottom + 58)
    col = (width - pad * 2) / len(tiles)
    for i, (v, l) in enumerate(tiles):
        b.append(tile(int(pad + i * col), ty, v, l))
    return card(width, ty + 34, "Contribution activity", "\n  ".join(b))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--out", default=".")
    ap.add_argument("--ignore", default="",
                    help="comma-separated languages to exclude (markup, config, etc.)")
    ap.add_argument("--data", help="read a cached JSON dump instead of the API")
    ap.add_argument("--dump", help="write the fetched data here")
    a = ap.parse_args()

    if a.data:
        raw = json.load(open(a.data))
        raw["created"] = dt.datetime.fromisoformat(raw["created"])
        d = raw
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            sys.exit("GITHUB_TOKEN is not set")
        d = fetch(a.user, token)

    if a.dump:
        out = dict(d)
        out["created"] = d["created"].isoformat()
        json.dump(out, open(a.dump, "w"), indent=1)

    skip = {x.strip().lower() for x in a.ignore.split(",") if x.strip()}
    if skip:
        d["langs"] = {k: v for k, v in d["langs"].items() if k.lower() not in skip}

    os.makedirs(a.out, exist_ok=True)
    for name, svg in (("stats.svg", card_stats(d)),
                      ("langs.svg", card_languages(d)),
                      ("activity.svg", card_activity(d))):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as f:
            f.write(svg)
        print(f"wrote {os.path.join(a.out, name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
