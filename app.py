from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import io
import math
import re
import unicodedata

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup, Comment

st.set_page_config(page_title="NCAAF Edge Scanner - Fast Load", page_icon="🏈", layout="wide")
ET = ZoneInfo("America/New_York")
SEASON = datetime.now(ET).year
LOOKAHEAD_DAYS = 7
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

st.markdown(
    """
<style>
.block-container{max-width:1500px;padding-top:1rem}
.card{border:1px solid #3a3a3a;border-radius:16px;padding:18px 20px;margin:10px 0 18px;background:#181818}
.teamline{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900;font-size:1.05rem}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.6}
.logo{height:40px;max-width:48px;vertical-align:middle;object-fit:contain;margin:0 8px}
.good{color:#32d583}.warn{color:#fdb022}.bad{color:#f97066}.muted{color:#aaa}
.badge{display:inline-block;border:1px solid #555;border-radius:999px;padding:4px 9px;margin:5px 4px 8px 0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:800}
.prop{border:1px solid #343434;border-radius:12px;padding:12px 14px;margin:7px 0;background:#121212}
.suggested{border:2px solid #32d583;border-radius:14px;padding:14px 16px;margin:10px 0 14px;background:#102119}
.timebar{border-left:4px solid #32d583;padding:8px 12px;margin:22px 0 8px;background:#111820;border-radius:8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900}
.source{font-size:.9rem;color:#aaa}
</style>
""",
    unsafe_allow_html=True,
)

GRADE_ORDER = ["F", "D", "C", "C+", "B", "B+", "A", "A+"]


def norm(value: object) -> str:
    s = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("&", "and")
    s = re.sub(r"\(\d+\)", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    aliases = {
        "southern california": "usc",
        "southern methodist": "smu",
        "texas christian": "tcu",
        "central florida": "ucf",
        "nevada las vegas": "unlv",
        "connecticut": "uconn",
        "massachusetts": "umass",
        "north carolina state": "nc state",
        "louisiana state": "lsu",
        "mississippi": "ole miss",
        "miami florida": "miami fl",
        "miami fl": "miami fl",
        "southern mississippi": "southern miss",
        "louisiana monroe": "ul monroe",
        "brigham young": "byu",
        "texas el paso": "utep",
        "alabama birmingham": "uab",
    }
    return aliases.get(s, s)


def sr_slug(team: str) -> str:
    n = norm(team)
    special = {
        "usc": "southern-california",
        "smu": "southern-methodist",
        "tcu": "texas-christian",
        "ucf": "central-florida",
        "unlv": "nevada-las-vegas",
        "uconn": "connecticut",
        "umass": "massachusetts",
        "nc state": "north-carolina-state",
        "lsu": "louisiana-state",
        "ole miss": "mississippi",
        "miami fl": "miami-fl",
        "southern miss": "southern-mississippi",
        "ul monroe": "louisiana-monroe",
        "byu": "brigham-young",
        "utep": "texas-el-paso",
        "uab": "alabama-birmingham",
    }
    return special.get(n, n.replace(" ", "-"))


def number(v):
    try:
        m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", "").replace("%", ""))
        return float(m.group()) if m else None
    except Exception:
        return None


def flatten(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [" ".join(str(x) for x in col if str(x) != "nan").strip() for col in d.columns]
    else:
        d.columns = [str(c).strip() for c in d.columns]
    return d


def tables_from_html(html: str) -> list[pd.DataFrame]:
    soup = BeautifulSoup(html, "html.parser")
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        txt = str(c)
        if "<table" in txt:
            try:
                c.replace_with(BeautifulSoup(txt, "html.parser"))
            except Exception:
                pass
    try:
        return [flatten(t) for t in pd.read_html(io.StringIO(str(soup)))]
    except Exception:
        return []


def col(df: pd.DataFrame, *names: str):
    cols = list(df.columns)
    for n in names:
        for c in cols:
            if str(c).strip().lower() == n.lower():
                return c
    for n in names:
        for c in cols:
            if n.lower() in str(c).lower():
                return c
    return None


def clean_team(value: object) -> str:
    s = str(value).replace("\xa0", " ").strip()
    s = re.sub(r"^\(\d+\)\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def request_text(url: str, timeout: float = 8.0) -> tuple[str | None, str | None]:
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            if len(r.text) < 200:
                raise RuntimeError("empty response")
            return r.text, None
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return None, last


def cbs_team_from_cell(cell):
    link = None
    for a in cell.find_all("a", href=True):
        href = a.get("href", "")
        if "/college-football/teams/" in href:
            link = a
            break
    if link:
        name = " ".join(link.stripped_strings).strip()
        href = link.get("href", "")
        m = re.search(r"/college-football/teams/([^/]+)/", href)
        code = m.group(1).upper() if m else ""
    else:
        name = " ".join(cell.stripped_strings).strip()
        code = ""
    name = re.sub(r"^\d+\s+", "", name).strip()
    img = cell.find("img")
    logo = None
    if img:
        logo = img.get("data-src") or img.get("src")
        if logo and logo.startswith("//"):
            logo = "https:" + logo
    return name, code, logo


def parse_cbs_schedule_html(html: str, start, end, source_label: str):
    """Parse CBS schedule pages across both table and div/card layouts.

    CBS currently renders the schedule content server-side, but the exact DOM
    structure differs between variants.  Some pages use semantic tables while
    others use nested div/card markup.  This parser intentionally does *not*
    depend on CBS CSS class names.  It identifies dates, team-page links and
    kickoff times from the visible HTML and then reconstructs game rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    games = []

    date_re = re.compile(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2}),\s+(\d{4})",
        re.I,
    )
    time_re = re.compile(r"\b(TBD|\d{1,2}:\d{2}\s*(?:a\.?m\.?|p\.?m\.?))\b", re.I)
    team_href_re = re.compile(r"/college-football/teams/([^/]+)/", re.I)

    def clean_text(x):
        return re.sub(r"\s+", " ", str(x or "")).strip()

    def parse_date_any(text):
        m = date_re.search(clean_text(text))
        if not m:
            return None
        try:
            return datetime.strptime(
                f"{m.group(1)}, {m.group(2)} {m.group(3)}, {m.group(4)}",
                "%A, %B %d, %Y",
            ).date()
        except Exception:
            return None

    def parse_kickoff(gd, raw_time):
        raw_time = clean_text(raw_time or "TBD")
        if raw_time.upper() == "TBD":
            return datetime(gd.year, gd.month, gd.day, 12, 0, tzinfo=ET), "TBD"
        normalized = raw_time.upper().replace("A.M.", "AM").replace("P.M.", "PM").replace("A.M", "AM").replace("P.M", "PM")
        try:
            parsed = datetime.strptime(f"{gd.isoformat()} {normalized}", "%Y-%m-%d %I:%M %p")
            label = parsed.strftime("%I:%M %p").lstrip("0")
            return parsed.replace(tzinfo=ET), label
        except Exception:
            return datetime(gd.year, gd.month, gd.day, 12, 0, tzinfo=ET), raw_time.upper()

    def team_from_anchor(a):
        href = a.get("href", "") if a else ""
        m = team_href_re.search(href)
        code = m.group(1).upper() if m else ""
        # Team labels can include a ranking number in a sibling element; using
        # only the anchor's own text avoids swallowing TV/ticket text.
        name = clean_team(clean_text(" ".join(a.stripped_strings) if a else ""))
        name = re.sub(r"^#?\d+\s+", "", name).strip()
        logo = None
        scope = a.parent if a and a.parent else a
        if scope:
            img = scope.find("img")
            if img:
                logo = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if logo and logo.startswith("//"):
                    logo = "https:" + logo
        return name, code, logo

    def previous_game_date(node):
        """Find the closest date heading before a game row/card.

        CBS sometimes splits a heading across nested spans.  We therefore keep
        a short rolling window of previous text nodes and look for a complete
        date in the reconstructed text instead of requiring one exact text node.
        """
        buf = []
        try:
            previous = node.find_all_previous(string=True, limit=450)
        except Exception:
            previous = []
        for raw in previous:
            txt = clean_text(raw)
            if not txt:
                continue
            buf.append(txt)
            if len(buf) > 12:
                buf.pop(0)
            # We are walking backwards; reverse the rolling buffer back into
            # document order before matching.
            joined = " ".join(reversed(buf))
            gd = parse_date_any(joined)
            if gd is not None:
                return gd
        return None

    def game_from_container(container, forced_date=None):
        anchors = []
        seen = set()
        for a in container.find_all("a", href=True):
            href = a.get("href", "")
            if not team_href_re.search(href):
                continue
            name, code, logo = team_from_anchor(a)
            key = code or norm(name)
            if not name or key in seen:
                continue
            seen.add(key)
            anchors.append((a, name, code, logo))
        if len(anchors) < 2:
            return None

        text = clean_text(container.get_text(" ", strip=True))
        mt = time_re.search(text)
        if not mt:
            return None

        gd = forced_date or previous_game_date(container)
        if gd is None or not (start <= gd <= end):
            return None

        _, away_name, away_code, away_logo = anchors[0]
        _, home_name, home_code, home_logo = anchors[1]
        if not away_name or not home_name or norm(away_name) == norm(home_name):
            return None

        kickoff, time_label = parse_kickoff(gd, mt.group(1))
        return {
            "id": f"cbs-{gd.isoformat()}-{away_code or norm(away_name)}-{home_code or norm(home_name)}",
            "date": kickoff,
            "date_label": gd.strftime("%A • %B %d, %Y"),
            "time_label": time_label,
            "away": {"name": away_name, "key": norm(away_name), "id": None, "abbr": away_code, "cbs_code": away_code, "logo": away_logo},
            "home": {"name": home_name, "key": norm(home_name), "id": None, "abbr": home_code, "cbs_code": home_code, "logo": home_logo},
            "neutral": False,
            "week": None,
            "schedule_source": source_label,
        }

    # PASS 1: semantic tables, when CBS supplies them.
    for table in soup.find_all("table"):
        gd = None
        # Check nearby prior text for the date associated with this table.
        for prev in table.find_all_previous(string=True, limit=300):
            gd = parse_date_any(prev)
            if gd is not None:
                break
        if gd is None:
            gd = previous_game_date(table)
        if gd is None or not (start <= gd <= end):
            continue
        for tr in table.find_all("tr"):
            game = game_from_container(tr, gd)
            if game:
                games.append(game)

    # PASS 2: generic row/card parser.  This is the important fallback for the
    # current CBS schedule layout where game rows can be nested divs rather than
    # real <table>/<tr> elements.
    if True:
        candidate_nodes = []
        seen_nodes = set()
        for a in soup.find_all("a", href=team_href_re):
            # Ascend until we find the smallest ancestor that contains at least
            # two distinct team links plus a kickoff time.
            node = a
            chosen = None
            for _ in range(10):
                node = getattr(node, "parent", None)
                if node is None or getattr(node, "name", None) in {"body", "html"}:
                    break
                team_links = node.find_all("a", href=team_href_re)
                unique_codes = []
                for ta in team_links:
                    mm = team_href_re.search(ta.get("href", ""))
                    code = mm.group(1).upper() if mm else norm(clean_text(ta.get_text(" ", strip=True)))
                    if code and code not in unique_codes:
                        unique_codes.append(code)
                if len(unique_codes) >= 2 and time_re.search(clean_text(node.get_text(" ", strip=True))):
                    chosen = node
                    break
            if chosen is not None and id(chosen) not in seen_nodes:
                seen_nodes.add(id(chosen))
                candidate_nodes.append(chosen)

        for node in candidate_nodes:
            game = game_from_container(node)
            if game:
                games.append(game)

    # PASS 3: section-based extraction.  If the DOM nesting is unusual, locate
    # compact date-bearing elements and inspect following siblings for game rows.
    if not games:
        date_tags = []
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "div", "span", "p"]):
            txt = clean_text(tag.get_text(" ", strip=True))
            if len(txt) > 110:
                continue
            gd = parse_date_any(txt)
            if gd is not None and start <= gd <= end:
                date_tags.append((tag, gd))
        for tag, gd in date_tags:
            # Search a bounded number of following elements.  Stop when another
            # date heading appears so games are not assigned to the wrong day.
            checked = 0
            for nxt in tag.find_all_next(["tr", "div", "li"], limit=250):
                checked += 1
                txt = clean_text(nxt.get_text(" ", strip=True))
                other_date = parse_date_any(txt) if len(txt) < 110 else None
                if other_date is not None and other_date != gd:
                    break
                game = game_from_container(nxt, gd)
                if game:
                    games.append(game)
                if checked >= 250:
                    break

    # PASS 4: document-stream parser.  This ignores layout entirely and walks
    # the HTML in source order.  Once a date is seen, the next two CBS team
    # links followed by a kickoff time are treated as one matchup.  This is
    # resilient to CBS switching between tables, CSS grids and card markup.
    try:
        from bs4 import NavigableString, Tag
        current_date = None
        pending_teams = []
        rolling_text = []
        stream_games = []
        skip_anchor_ids = set()

        for node in soup.descendants:
            if isinstance(node, Tag) and node.name == "a" and node.get("href"):
                if team_href_re.search(node.get("href", "")):
                    # Ignore nav/team-menu links until a schedule date has been
                    # encountered in the document stream.
                    if current_date is not None:
                        name, code, logo = team_from_anchor(node)
                        key = code or norm(name)
                        if name and (not pending_teams or (pending_teams[-1][1] or norm(pending_teams[-1][0])) != key):
                            pending_teams.append((name, code, logo))
                            # Keep only the most recent pair; this protects
                            # against an unexpected extra team link in a row.
                            if len(pending_teams) > 2:
                                pending_teams = pending_teams[-2:]
                    skip_anchor_ids.add(id(node))
                continue

            if isinstance(node, NavigableString):
                # Skip text that belongs to a team anchor already handled above.
                par = getattr(node, "parent", None)
                if par is not None:
                    a_par = par if getattr(par, "name", None) == "a" else par.find_parent("a")
                    if a_par is not None and team_href_re.search(a_par.get("href", "") or ""):
                        continue
                txt = clean_text(node)
                if not txt:
                    continue
                rolling_text.append(txt)
                if len(rolling_text) > 8:
                    rolling_text.pop(0)
                joined = " ".join(rolling_text)
                gd = parse_date_any(joined)
                if gd is not None:
                    current_date = gd if start <= gd <= end else None
                    pending_teams = []
                    rolling_text = [txt]
                    continue
                if current_date is None:
                    continue
                mt = time_re.search(txt)
                if mt and len(pending_teams) >= 2:
                    away_name, away_code, away_logo = pending_teams[-2]
                    home_name, home_code, home_logo = pending_teams[-1]
                    kickoff, time_label = parse_kickoff(current_date, mt.group(1))
                    stream_games.append({
                        "id": f"cbs-{current_date.isoformat()}-{away_code or norm(away_name)}-{home_code or norm(home_name)}",
                        "date": kickoff,
                        "date_label": current_date.strftime("%A • %B %d, %Y"),
                        "time_label": time_label,
                        "away": {"name": away_name, "key": norm(away_name), "id": None, "abbr": away_code, "cbs_code": away_code, "logo": away_logo},
                        "home": {"name": home_name, "key": norm(home_name), "id": None, "abbr": home_code, "cbs_code": home_code, "logo": home_logo},
                        "neutral": False,
                        "week": None,
                        "schedule_source": source_label,
                    })
                    pending_teams = []
        games.extend(stream_games)
    except Exception:
        pass

    # De-duplicate responsive/mobile copies and nested-container duplicates.
    dedup = {}
    for g in games:
        key = (g["date"].date().isoformat(), norm(g["away"]["name"]), norm(g["home"]["name"]))
        dedup[key] = g
    return sorted(dedup.values(), key=lambda g: g["date"])

def current_cbs_week(html: str | None):
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.stripped_strings)
    m = re.search(r"College Football Schedule\s*-?\s*Week\s*(\d+)", text, re.I)
    if not m:
        m = re.search(r"\bWeek\s+(\d+)\b", text, re.I)
    return int(m.group(1)) if m else None


def sportsref_schedule_fallback(season: int, start, end):
    url = f"https://www.sports-reference.com/cfb/years/{season}-schedule.html"
    html, err = request_text(url, 7)
    if not html:
        return [], err, url
    tabs = tables_from_html(html)
    sched = None
    for t in tabs:
        if col(t, "Date") and (col(t, "Winner") or col(t, "Visitor")) and len(t) > 20:
            sched = t
            break
    if sched is None:
        return [], "Schedule table not found", url
    dc = col(sched, "Date")
    tc = col(sched, "Time")
    weekc = col(sched, "Wk")
    firstc = col(sched, "Visitor", "Winner")
    secondc = col(sched, "Home", "Loser")
    if not firstc or not secondc:
        return [], "Team columns not found", url
    locc = None
    for c in sched.columns:
        vals = set(str(x).strip() for x in sched[c].dropna().head(1000).tolist())
        if "@" in vals or "N" in vals:
            locc = c
            break
    games = []
    for idx, row in sched.iterrows():
        raw_date = str(row.get(dc, "")).strip()
        try:
            gd = pd.to_datetime(raw_date, errors="raise").date()
        except Exception:
            continue
        if not (start <= gd <= end):
            continue
        away = clean_team(row.get(firstc, ""))
        home = clean_team(row.get(secondc, ""))
        if not away or not home:
            continue
        marker = str(row.get(locc, "")).strip() if locc else "@"
        neutral = marker.upper() == "N"
        raw_time = str(row.get(tc, "TBD")).strip() if tc else "TBD"
        if raw_time.lower() in ("nan", "", "time"):
            raw_time = "TBD"
        try:
            parsed = pd.to_datetime(f"{gd.isoformat()} {raw_time}", errors="raise") if raw_time.upper() != "TBD" else None
            kickoff = datetime(parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, tzinfo=ET) if parsed is not None else datetime(gd.year, gd.month, gd.day, 12, 0, tzinfo=ET)
        except Exception:
            kickoff = datetime(gd.year, gd.month, gd.day, 12, 0, tzinfo=ET)
        games.append({
            "id": f"sr-{gd.isoformat()}-{idx}",
            "date": kickoff,
            "date_label": gd.strftime("%A • %B %d, %Y"),
            "time_label": raw_time,
            "away": {"name": away, "key": norm(away), "id": None, "abbr": "", "cbs_code": "", "logo": None},
            "home": {"name": home, "key": norm(home), "id": None, "abbr": "", "cbs_code": "", "logo": None},
            "neutral": neutral,
            "week": int(number(row.get(weekc))) if weekc and number(row.get(weekc)) is not None else None,
            "schedule_source": "Sports-Reference fallback",
        })
    return games, None, url


@st.cache_data(ttl=900, show_spinner=False)
def schedule_pack(season: int, start_iso: str, days: int):
    start = datetime.fromisoformat(start_iso).date()
    end = start + timedelta(days=days)
    errors = {}
    urls = []
    games = []

    # CBS is the primary schedule source because Sports-Reference often returns 403 on Streamlit Cloud.
    base_urls = {
        "CBS FBS": "https://www.cbssports.com/college-football/schedule/",
        "CBS FCS": "https://www.cbssports.com/college-football/schedule/FCS/",
    }
    base_html = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(request_text, u, 8): label for label, u in base_urls.items()}
        for fut in as_completed(futs):
            label = futs[fut]
            html, err = fut.result()
            base_html[label] = html
            urls.append(base_urls[label])
            if err:
                errors[label] = err
            if html:
                parsed = parse_cbs_schedule_html(html, start, end, label)
                games.extend(parsed)
                if not parsed:
                    errors[label] = f"Page loaded ({len(html):,} chars) but 0 games matched {start} through {end}."

    # Also fetch the following CBS week so a 7-day Wednesday/Sunday scan includes next week's games.
    next_jobs = {}
    for label, html in base_html.items():
        wk = current_cbs_week(html)
        if wk is None:
            continue
        group = "FCS" if "FCS" in label else "FBS"
        for w in {wk, wk + 1}:
            u = f"https://www.cbssports.com/college-football/schedule/{group}/{season}/regular/{w}/"
            next_jobs[f"{label} Week {w}"] = u
    if next_jobs:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(request_text, u, 8): (label, u) for label, u in next_jobs.items()}
            for fut in as_completed(futs):
                label, u = futs[fut]
                html, err = fut.result()
                urls.append(u)
                if err:
                    errors[label] = err
                if html:
                    parsed = parse_cbs_schedule_html(html, start, end, label)
                    games.extend(parsed)
                    if not parsed and label not in errors:
                        errors[label] = f"Page loaded ({len(html):,} chars) but 0 games matched {start} through {end}."

    # De-duplicate CBS results from base and week pages.
    dedup = {}
    for g in games:
        key = (g["date"].date().isoformat(), norm(g["away"]["name"]), norm(g["home"]["name"]))
        dedup[key] = g
    games = list(dedup.values())

    # Last-resort fallback only. A 403 here no longer stops the app.
    if not games:
        sr_games, sr_err, sr_url = sportsref_schedule_fallback(season, start, end)
        urls.append(sr_url)
        if sr_err:
            errors["Sports-Reference fallback"] = sr_err
        games.extend(sr_games)

    games.sort(key=lambda g: g["date"])
    primary = "CBS Sports" if any(g.get("schedule_source", "").startswith("CBS") for g in games) else (games[0]["schedule_source"] if games else "None")
    return {"games": games, "error": None if games else "All schedule sources failed", "errors": errors, "urls": urls, "url": urls[0] if urls else "", "source": primary}

def parse_standings(html: str | None):
    out = {}
    if not html:
        return out
    for t in tables_from_html(html):
        sc = col(t, "School")
        srsc = col(t, "SRS")
        if not sc or not srsc or len(t) < 40:
            continue
        confc = col(t, "Conf")
        # Avoid conference W/L by taking the first matching Overall W/L/Pct when flattened.
        wcands = [c for c in t.columns if str(c).lower().endswith(" w") or str(c).lower() == "w"]
        lcands = [c for c in t.columns if str(c).lower().endswith(" l") or str(c).lower() == "l"]
        pctcands = [c for c in t.columns if "pct" in str(c).lower()]
        offcands = [c for c in t.columns if "points per game" in str(c).lower() and str(c).lower().endswith(" off")]
        defcands = [c for c in t.columns if "points per game" in str(c).lower() and str(c).lower().endswith(" def")]
        sosc = col(t, "SOS")
        apc = col(t, "AP Rank", "AP Curr")
        for _, r in t.iterrows():
            team = clean_team(r.get(sc, ""))
            if not team or team.lower() == "school":
                continue
            w = number(r.get(wcands[0])) if wcands else None
            l = number(r.get(lcands[0])) if lcands else None
            pctv = number(r.get(pctcands[0])) if pctcands else None
            if pctv is None and w is not None and l is not None and w + l > 0:
                pctv = w / (w + l)
            elif pctv is not None and pctv > 1:
                pctv /= 100.0
            out[norm(team)] = {
                "team": team,
                "conf": str(r.get(confc, "")).strip() if confc else "",
                "w": w,
                "l": l,
                "pct": pctv,
                "srs": number(r.get(srsc)),
                "sos": number(r.get(sosc)) if sosc else None,
                "off": number(r.get(offcands[0])) if offcands else None,
                "def": number(r.get(defcands[0])) if defcands else None,
                "ap": number(r.get(apc)) if apc else None,
            }
        if out:
            break
    return out


def parse_bcf(html: str | None):
    out = {}
    if not html:
        return out
    for t in tables_from_html(html):
        tc = col(t, "Team")
        feic = col(t, "FEI")
        if not tc or not feic or len(t) < 20:
            continue
        ofeic = col(t, "OFEI")
        dfeic = col(t, "DFEI")
        for _, r in t.iterrows():
            team = clean_team(r.get(tc, ""))
            if not team or team.lower() == "team":
                continue
            out[norm(team)] = {
                "fei": number(r.get(feic)),
                "ofei": number(r.get(ofeic)) if ofeic else None,
                "dfei": number(r.get(dfeic)) if dfeic else None,
            }
        if out:
            break
    return out


def parse_espn_teams(html: str | None):
    out = {}
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r"/college-football/team/_/id/(\d+)/", href)
        if not m:
            continue
        team_id = m.group(1)
        text = " ".join(a.stripped_strings).strip()
        if not text:
            continue
        n = norm(text)
        if n and len(n) > 2:
            out[n] = {
                "id": team_id,
                "abbr": "",
                "logo": f"https://a.espncdn.com/i/teamlogos/ncaa/500/{team_id}.png",
            }
    return out


def parse_player_table(html: str | None, kind: str):
    rows = []
    if not html:
        return rows
    for t in tables_from_html(html):
        pc = col(t, "Player")
        schoolc = col(t, "School", "Team")
        if not pc or not schoolc or len(t) < 20:
            continue
        gc = col(t, "G")
        ygc = col(t, "Y/G", "Yds/G", "Yards Per Game")
        yc = col(t, "Yds", "Yards")
        tdc = col(t, "TD")
        recc = col(t, "Rec", "Receptions") if kind == "receiving" else None
        if kind in ("rushing", "receiving"):
            scoped_y = [c for c in t.columns if kind in str(c).lower() and re.search(r"\byds\b|yards", str(c).lower())]
            scoped_yg = [c for c in t.columns if kind in str(c).lower() and ("y/g" in str(c).lower() or "per game" in str(c).lower())]
            if scoped_y:
                yc = scoped_y[0]
            if scoped_yg:
                ygc = scoped_yg[0]
        for _, r in t.iterrows():
            player = str(r.get(pc, "")).replace("*", "").strip()
            team = clean_team(r.get(schoolc, ""))
            if not player or not team or player.lower() == "player":
                continue
            gp = number(r.get(gc)) if gc else None
            ypg = number(r.get(ygc)) if ygc else None
            yds = number(r.get(yc)) if yc else None
            if ypg is None and gp and yds is not None:
                ypg = yds / gp
            if ypg is None or ypg <= 0:
                continue
            rec = number(r.get(recc)) if recc else None
            rec_pg = rec / gp if rec is not None and gp else None
            rows.append({
                "player": player,
                "team": team,
                "team_key": norm(team),
                "kind": kind,
                "games": gp or 1,
                "ypg": ypg,
                "yards": yds,
                "td": number(r.get(tdc)) if tdc else None,
                "receptions": rec,
                "receptions_pg": rec_pg,
            })
        if rows:
            break
    return rows

def parse_cbs_team_stats(off_htmls, def_htmls):
    out = {}

    def ingest(html, defense=False):
        if not html:
            return
        for t in tables_from_html(html):
            tc = col(t, "Team")
            if not tc or len(t) < 5:
                continue
            ppg = None
            total = None
            passing = None
            rushing = None
            for c in t.columns:
                cl = str(c).lower()
                if ppg is None and ("pts/g" in cl or "points per game" in cl):
                    ppg = c
                if total is None and "total yards per game" in cl:
                    total = c
                if passing is None and "passing yards per game" in cl:
                    passing = c
                if rushing is None and "rushing yards per game" in cl:
                    rushing = c
            if ppg is None:
                continue
            for _, r in t.iterrows():
                team = clean_team(r.get(tc, ""))
                if not team or team.lower() == "team":
                    continue
                k = norm(team)
                d = out.setdefault(k, {"team": team, "conf": "", "w": None, "l": None, "pct": None, "srs": None, "sos": None, "off": None, "def": None, "source": "CBS Sports"})
                if defense:
                    d["def"] = number(r.get(ppg))
                    d["def_total_yards"] = number(r.get(total)) if total else None
                    d["def_pass_yards"] = number(r.get(passing)) if passing else None
                    d["def_rush_yards"] = number(r.get(rushing)) if rushing else None
                else:
                    d["off"] = number(r.get(ppg))
                    d["total_yards"] = number(r.get(total)) if total else None
                    d["pass_yards"] = number(r.get(passing)) if passing else None
                    d["rush_yards"] = number(r.get(rushing)) if rushing else None
            # each page normally contains one main table
            break

    for html in off_htmls:
        ingest(html, False)
    for html in def_htmls:
        ingest(html, True)
    return out


def parse_cbs_player_table(htmls, kind: str):
    rows = []
    seen = set()
    pos_default = {"passing": "QB", "rushing": "RB", "receiving": "WR"}.get(kind, "")
    for html in htmls:
        if not html:
            continue
        for t in tables_from_html(html):
            pc = col(t, "Player")
            if not pc or len(t) < 5:
                continue
            gc = col(t, "GP", "Games played", "G")
            ygc = None
            yc = None
            tdc = None
            recc = None
            for c in t.columns:
                cl = str(c).lower()
                if ygc is None and ("yds/g" in cl or "yards per game" in cl):
                    ygc = c
                if yc is None and (cl == "yds" or " yards" in cl) and "per game" not in cl and "yds/g" not in cl:
                    yc = c
                if tdc is None and (cl == "td" or "touchdown" in cl):
                    tdc = c
                if kind == "receiving" and recc is None and (cl == "rec" or "receptions" in cl):
                    recc = c
            if ygc is None:
                continue
            for _, r in t.iterrows():
                raw = str(r.get(pc, "")).replace("*", " ").strip()
                if not raw or raw.lower() == "player":
                    continue
                parts = re.split(r"\s+(QB|RB|WR|TE|ATH|FB)\s+", raw)
                player = raw
                pos = pos_default
                team_code = ""
                if len(parts) >= 3:
                    pos = parts[-2]
                    tail = parts[-1].strip()
                    mcode = re.search(r"([A-Z0-9]{2,10})\s*$", tail)
                    if mcode:
                        team_code = mcode.group(1).upper()
                    candidate = parts[-3].strip()
                    candidate = re.sub(r"^[A-Z0-9]{2,10}\s+", "", candidate).strip()
                    if candidate:
                        player = candidate
                if len(player) < 2:
                    continue
                gp = number(r.get(gc)) if gc else None
                ypg = number(r.get(ygc))
                yds = number(r.get(yc)) if yc else None
                if ypg is None and gp and yds is not None:
                    ypg = yds / gp
                if ypg is None or ypg <= 0:
                    continue
                rec = number(r.get(recc)) if recc else None
                rec_pg = rec / gp if rec is not None and gp else None
                key = (player.lower(), team_code, kind)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "player": player,
                    "team": team_code or "Unknown",
                    "team_code": team_code,
                    "team_key": norm(team_code),
                    "kind": kind,
                    "position": pos,
                    "games": gp or 1,
                    "ypg": ypg,
                    "yards": yds,
                    "td": number(r.get(tdc)) if tdc else None,
                    "receptions": rec,
                    "receptions_pg": rec_pg,
                    "source": "CBS Sports",
                })
            break
    return rows

def merge_team_maps(primary, fallback):
    out = {k: dict(v) for k, v in fallback.items()}
    for k, v in primary.items():
        base = out.setdefault(k, {})
        for fk, fv in v.items():
            if fv is not None and fv != "":
                base[fk] = fv
        if "source" not in base:
            base["source"] = "Sports-Reference"
    return out


@st.cache_data(ttl=1800, show_spinner=False)
def core_pack(season: int):
    urls = {
        "standings": f"https://www.sports-reference.com/cfb/years/{season}-standings.html",
        "bcf": f"https://bcftoys.com/{season}-fei",
        "espn_teams": "https://www.espn.com/college-football/teams",
        "sr_passing": f"https://www.sports-reference.com/cfb/years/{season}-passing.html",
        "sr_rushing": f"https://www.sports-reference.com/cfb/years/{season}-rushing.html",
        "sr_receiving": f"https://www.sports-reference.com/cfb/years/{season}-receiving.html",
    }
    # CBS is the main public-page fallback when Sports-Reference blocks cloud-hosted apps.
    for pageno in range(1, 4):
        urls[f"cbs_off_{pageno}"] = f"https://www.cbssports.com/college-football/stats/team/team/total/all-conf/?page={pageno}&seasonType=regular"
        urls[f"cbs_def_{pageno}"] = f"https://www.cbssports.com/college-football/stats/team/opponent/total/all-conf/?page={pageno}&seasonType=regular"
        for kind in ("passing", "rushing", "receiving"):
            urls[f"cbs_{kind}_{pageno}"] = f"https://www.cbssports.com/college-football/stats/player/{kind}/all-conf/all/?page={pageno}"
    raw = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(request_text, u, 7): k for k, u in urls.items()}
        for f in as_completed(futs):
            k = futs[f]
            try:
                html, err = f.result()
            except Exception as e:
                html, err = None, f"{type(e).__name__}: {e}"
            raw[k] = html
            if err:
                errors[k] = err

    sr_stats = parse_standings(raw.get("standings"))
    for v in sr_stats.values():
        v["source"] = "Sports-Reference"
    cbs_stats = parse_cbs_team_stats(
        [raw.get(f"cbs_off_{i}") for i in range(1, 4)],
        [raw.get(f"cbs_def_{i}") for i in range(1, 4)],
    )
    team_stats = merge_team_maps(sr_stats, cbs_stats)

    players = {}
    for kind in ("passing", "rushing", "receiving"):
        sr_rows = parse_player_table(raw.get(f"sr_{kind}"), kind)
        for r in sr_rows:
            r["source"] = "Sports-Reference"
            r["team_code"] = ""
        cbs_rows = parse_cbs_player_table([raw.get(f"cbs_{kind}_{i}") for i in range(1, 4)], kind)
        # Prefer Sports-Reference rows if available, but supplement with CBS players.
        combined = sr_rows[:]
        existing = {(r["player"].lower(), r.get("team_key", "")) for r in combined}
        for r in cbs_rows:
            key = (r["player"].lower(), r.get("team_key", ""))
            if key not in existing:
                combined.append(r)
        players[kind] = combined

    return {
        "standings": team_stats,
        "bcf": parse_bcf(raw.get("bcf")),
        "teams": parse_espn_teams(raw.get("espn_teams")),
        "players": players,
        "errors": errors,
        "urls": urls,
        "cbs_team_stats": bool(cbs_stats),
    }


def lookup(map_: dict, team: str):
    k = norm(team)
    if k in map_:
        return map_[k]
    # conservative fuzzy fallback for common school naming differences
    for mk, v in map_.items():
        if len(k) >= 5 and (k in mk or mk in k):
            return v
    return {}


def apply_team_meta(game, core):
    for side in ("away", "home"):
        t = game[side]
        meta = lookup(core["teams"], t["name"])
        if meta:
            t.update(meta)
        stt = lookup(core["standings"], t["name"])
        t["stats"] = stt
        t["conf"] = stt.get("conf", "") if stt else ""
    return game


def grade_from_prob(p: float, coverage: int):
    g = "A+" if p >= .80 else "A" if p >= .74 else "B+" if p >= .69 else "B" if p >= .64 else "C+" if p >= .60 else "C" if p >= .57 else "D" if p >= .53 else "F"
    cap = "A+" if coverage >= 3 else "A" if coverage >= 2 else "B+" if coverage >= 1 else "C"
    return GRADE_ORDER[min(GRADE_ORDER.index(g), GRADE_ORDER.index(cap))]


def baseline_model(game, core):
    hs = lookup(core["standings"], game["home"]["name"])
    as_ = lookup(core["standings"], game["away"]["name"])
    hb = lookup(core["bcf"], game["home"]["name"])
    ab = lookup(core["bcf"], game["away"]["name"])
    components = []

    def add(label, hv, av, weight, scale, source, higher=True):
        if hv is None or av is None:
            return
        diff = (hv - av) / scale
        if not higher:
            diff = -diff
        contrib = max(-1.5, min(1.5, diff)) * weight
        components.append({"label": label, "contrib": contrib, "source": source, "home": hv, "away": av})

    add("Win rate", hs.get("pct"), as_.get("pct"), .20, .30, hs.get("source", "Team stats"))
    add("SRS", hs.get("srs"), as_.get("srs"), .25, 12, hs.get("source", "Team stats"))
    add("SOS", hs.get("sos"), as_.get("sos"), .07, 8, hs.get("source", "Team stats"))
    if hs.get("off") is not None and hs.get("def") is not None and as_.get("off") is not None and as_.get("def") is not None:
        add("Scoring margin", hs["off"] - hs["def"], as_["off"] - as_["def"], .15, 16, hs.get("source", "Team stats"))
    add("FEI", hb.get("fei"), ab.get("fei"), .18, .75, "BCF Toys")
    add("OFEI", hb.get("ofei"), ab.get("ofei"), .07, .55, "BCF Toys")
    add("DFEI", hb.get("dfei"), ab.get("dfei"), .05, .55, "BCF Toys")
    # modest home field, none for neutral games
    if not game.get("neutral"):
        components.append({"label": "Home field", "contrib": .075, "source": "Model", "home": 1, "away": 0})

    z = sum(x["contrib"] for x in components)
    ph = 1 / (1 + math.exp(-2.0 * z))
    sources = {x["source"] for x in components if x["source"] != "Model"}
    coverage = len(sources)
    shrink = {0: .40, 1: .68, 2: .88}.get(coverage, 1.0)
    ph = .5 + (ph - .5) * shrink
    home_pick = ph >= .5
    p = ph if home_pick else 1 - ph
    pick = game["home"] if home_pick else game["away"]
    opp = game["away"] if home_pick else game["home"]
    reasons = []
    for c in sorted(components, key=lambda x: abs(x["contrib"]), reverse=True):
        favors_home = c["contrib"] > 0
        if (home_pick and favors_home) or ((not home_pick) and not favors_home):
            reasons.append("Home-field advantage" if c["label"] == "Home field" else f"{c['source']}: {c['label']} favors {pick['name']}")
        if len(reasons) >= 4:
            break
    return {"pick": pick, "opp": opp, "p": p, "grade": grade_from_prob(p, coverage), "coverage": coverage, "components": components, "reasons": reasons, "home_sr": hs, "away_sr": as_, "home_bcf": hb, "away_bcf": ab}


def prop_grade(sample: float, cushion: float, matchup: float, probability: float | None = None):
    sample_score = min(max(sample, 0) / 4.0, 1.0)
    cushion_score = min(max(cushion, 0) / .14, 1.0)
    matchup_score = min(max(matchup, 0), 1.0)
    score = .42 * sample_score + .36 * cushion_score + .22 * matchup_score
    if probability is not None:
        prob_score = min(max((probability - .30) / .45, 0), 1.0)
        score = .34 * sample_score + .22 * cushion_score + .16 * matchup_score + .28 * prob_score
    grade = "A+" if score >= .90 else "A" if score >= .82 else "B+" if score >= .74 else "B" if score >= .66 else "C+" if score >= .58 else "C" if score >= .50 else "D" if score >= .42 else "F"
    return grade, score


def _match_player_to_game(r, game, team_keys):
    tm = None
    if r.get("team_code"):
        for obj in team_keys.values():
            if obj.get("cbs_code") and obj.get("cbs_code").upper() == r.get("team_code", "").upper():
                tm = obj
                break
    if tm is None and r.get("team_key") in team_keys:
        tm = team_keys[r["team_key"]]
    if tm is None:
        for tk, obj in team_keys.items():
            rk = r.get("team_key", "")
            if len(tk) >= 4 and rk and (tk in rk or rk in tk):
                tm = obj
                break
    return tm


def _matchup_adjustment(opp, core, kind: str):
    opp_bcf = lookup(core["bcf"], opp["name"])
    opp_sr = lookup(core["standings"], opp["name"])
    adj = 1.0
    matchup_strength = .5
    dfei = opp_bcf.get("dfei")
    if dfei is not None:
        delta = max(-.08, min(.08, -dfei * .06))
        adj += delta
        matchup_strength = .75 if delta > 0 else .35
    opp_def = opp_sr.get("def")
    if opp_def is not None:
        delta = max(-.05, min(.05, (opp_def - 27.0) / 100.0))
        adj += delta
        matchup_strength = max(matchup_strength, .70 if delta > 0 else .40)
    # If CBS exposes split defensive yardage, use the category-specific number conservatively.
    split = opp_sr.get("def_rush_yards") if kind == "rushing" else opp_sr.get("def_pass_yards")
    if split is not None:
        baseline = 155.0 if kind == "rushing" else 225.0
        scale = 500.0 if kind == "rushing" else 700.0
        delta = max(-.04, min(.04, (split - baseline) / scale))
        adj += delta
        matchup_strength = max(matchup_strength, .72 if delta > 0 else .42)
    return max(.84, min(1.16, adj)), matchup_strength


def player_props(game, model, core):
    """Return best prop candidate in the four requested markets for a game.

    Markets: Anytime TD, rushing yards, receptions, receiving yards.  Suggested
    yard/reception numbers are model thresholds, not sportsbook lines.
    """
    team_keys = {norm(game["away"]["name"]): game["away"], norm(game["home"]["name"]): game["home"]}
    candidates = []

    # Rushing yards candidates.
    for r in core["players"].get("rushing", []):
        tm = _match_player_to_game(r, game, team_keys)
        if not tm:
            continue
        opp = game["home"] if tm is game["away"] else game["away"]
        adj, matchup_strength = _matchup_adjustment(opp, core, "rushing")
        proj = max(0.0, r["ypg"] * adj)
        target = max(.5, round((proj * .89) * 2) / 2)
        cushion = (proj - target) / max(target, 1)
        grade, score = prop_grade(r.get("games", 1), cushion, matchup_strength)
        candidates.append({**r, "team_obj": tm, "opp": opp, "market": "Rushing Yards", "projection": proj, "target": target, "grade": grade, "prop_score": score, "suggestion": f"OVER {target:.1f} rushing yards"})

    # Receiving yards + receptions candidates.
    for r in core["players"].get("receiving", []):
        tm = _match_player_to_game(r, game, team_keys)
        if not tm:
            continue
        opp = game["home"] if tm is game["away"] else game["away"]
        adj, matchup_strength = _matchup_adjustment(opp, core, "receiving")
        proj = max(0.0, r["ypg"] * adj)
        target = max(.5, round((proj * .89) * 2) / 2)
        cushion = (proj - target) / max(target, 1)
        grade, score = prop_grade(r.get("games", 1), cushion, matchup_strength)
        candidates.append({**r, "team_obj": tm, "opp": opp, "market": "Receiving Yards", "projection": proj, "target": target, "grade": grade, "prop_score": score, "suggestion": f"OVER {target:.1f} receiving yards"})

        rec_pg = r.get("receptions_pg")
        if rec_pg is not None and rec_pg > 0:
            rec_proj = max(.1, rec_pg * (0.98 + (adj - 1.0) * .55))
            # Typical reception markets are half-number lines.  Use the highest
            # half-line that still leaves a modest projection cushion.
            rec_target = max(.5, math.floor(rec_proj * .88 * 2) / 2)
            rec_cushion = (rec_proj - rec_target) / max(rec_target, .5)
            rec_grade, rec_score = prop_grade(r.get("games", 1), rec_cushion, matchup_strength)
            candidates.append({**r, "team_obj": tm, "opp": opp, "market": "Receptions", "projection": rec_proj, "target": rec_target, "grade": rec_grade, "prop_score": rec_score, "suggestion": f"OVER {rec_target:.1f} receptions"})

    # Anytime TD candidates: combine rushing + receiving TD rates for the same player.
    td_map = {}
    for kind in ("rushing", "receiving"):
        for r in core["players"].get(kind, []):
            tm = _match_player_to_game(r, game, team_keys)
            if not tm:
                continue
            gp = max(float(r.get("games") or 1), 1.0)
            td = float(r.get("td") or 0)
            if td <= 0:
                continue
            # Key on team + normalized player so rush and receiving TDs can combine.
            key = (norm(tm["name"]), norm(r["player"]))
            item = td_map.setdefault(key, {"player": r["player"], "team_obj": tm, "games": gp, "td_total": 0.0, "source_rows": []})
            item["games"] = max(item["games"], gp)
            item["td_total"] += td
            item["source_rows"].append(r)
    for item in td_map.values():
        tm = item["team_obj"]
        opp = game["home"] if tm is game["away"] else game["away"]
        adj, matchup_strength = _matchup_adjustment(opp, core, "rushing")
        td_rate = item["td_total"] / max(item["games"], 1.0)
        # Blend scoring rate toward a conservative prior because early-season TD samples are noisy.
        shrunk_rate = .72 * td_rate + .28 * .35
        lam = max(.05, shrunk_rate * (0.97 + (adj - 1.0) * .8))
        td_prob = 1.0 - math.exp(-lam)
        cushion = max(0.0, td_prob - .50)
        grade, score = prop_grade(item["games"], cushion, matchup_strength, probability=td_prob)
        candidates.append({
            "player": item["player"],
            "team": tm.get("cbs_code") or tm["name"],
            "team_obj": tm,
            "opp": opp,
            "market": "Anytime TD",
            "projection": td_prob * 100.0,
            "target": None,
            "grade": grade,
            "prop_score": score,
            "games": item["games"],
            "ypg": td_rate,
            "td_probability": td_prob,
            "suggestion": "ANYTIME TD — YES",
        })

    best = []
    for market in ("Anytime TD", "Rushing Yards", "Receptions", "Receiving Yards"):
        rows = [x for x in candidates if x["market"] == market]
        if rows:
            rows.sort(key=lambda x: (GRADE_ORDER.index(x["grade"]), x.get("prop_score", 0), x.get("projection", 0)), reverse=True)
            best.append(rows[0])
    best.sort(key=lambda x: (GRADE_ORDER.index(x["grade"]), x.get("prop_score", 0)), reverse=True)
    return best

def deep_urls(game, season):
    a = game["away"]
    h = game["home"]
    out = {
        "winsipedia": f"https://www.winsipedia.com/{sr_slug(h['name'])}/vs/{sr_slug(a['name'])}",
        "cfbd_home": f"https://collegefootballdata.com/team/{quote(h['name'])}",
        "cfbd_away": f"https://collegefootballdata.com/team/{quote(a['name'])}",
    }
    if h.get("id"):
        out["gop_home"] = f"https://gameonpaper.com/year/{season}/team/{h['id']}"
    if a.get("id"):
        out["gop_away"] = f"https://gameonpaper.com/year/{season}/team/{a['id']}"
    return out


def gop_metrics(html):
    if not html:
        return {}
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    out = {}
    for key, pat in {
        "adj_epa": r"Adj EPA/Play\s*([+\-]?\d+(?:\.\d+)?)",
        "epa": r"EPA/Play\s*([+\-]?\d+(?:\.\d+)?)",
        "success": r"Success %\s*([+\-]?\d+(?:\.\d+)?)%",
    }.items():
        m = re.search(pat, text, re.I)
        if m:
            out[key] = number(m.group(1))
    return out


def cfbd_metrics(html):
    if not html:
        return {}
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    out = {}
    for key, label in {"ppa": "Predicted Points Added", "success": "Success Rate", "explosiveness": "Explosiveness"}.items():
        m = re.search(label + r".{0,120}?([+\-]?\d+(?:\.\d+)?%?)", text, re.I)
        if m:
            out[key] = number(m.group(1))
    return out


@st.cache_data(ttl=1800, show_spinner=False)
def deep_verify(home_name: str, away_name: str, home_id: str | None, away_id: str | None, season: int):
    game_stub = {"home": {"name": home_name, "id": home_id}, "away": {"name": away_name, "id": away_id}}
    urls = deep_urls(game_stub, season)
    raw = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=min(5, len(urls))) as ex:
        futs = {ex.submit(request_text, u, 7): k for k, u in urls.items()}
        for f in as_completed(futs):
            k = futs[f]
            try:
                html, err = f.result()
            except Exception as e:
                html, err = None, f"{type(e).__name__}: {e}"
            raw[k] = html
            if err:
                errors[k] = err
    return {
        "urls": urls,
        "errors": errors,
        "gop_home": gop_metrics(raw.get("gop_home")),
        "gop_away": gop_metrics(raw.get("gop_away")),
        "cfbd_home": cfbd_metrics(raw.get("cfbd_home")),
        "cfbd_away": cfbd_metrics(raw.get("cfbd_away")),
        "winsipedia_ok": bool(raw.get("winsipedia")),
    }


def logo_html(team):
    if team.get("logo"):
        return f"<img class='logo' src='{team['logo']}'/>"
    return ""


def weekly_status(now):
    wd = now.weekday()
    if wd == 2:
        return "EARLY MODEL", "Wednesday moneyline board"
    if wd == 3:
        return "UPDATED", "Thursday matchup refresh"
    if wd == 4:
        return "PROP UPGRADE", "Friday player-prop upgrade"
    if wd == 5:
        return "FINAL PREGAME", "Saturday final pregame board"
    return "NEXT SLATE BUILD", "Building the next slate"


def render_card(game, model, props):
    grade = model["grade"]
    color = "good" if grade in ("A+", "A", "B+") else "warn" if grade in ("B", "C+", "C") else "bad"
    kick = f"{game['date_label']} • {game['time_label']} ET" if game["time_label"] != "TBD" else f"{game['date_label']} • TIME TBD"
    st.markdown("<div class='card'>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='teamline'>{game['away']['name'].upper()} {logo_html(game['away'])} &nbsp;&nbsp; @ &nbsp;&nbsp; {logo_html(game['home'])} {game['home']['name'].upper()}</div>",
        unsafe_allow_html=True,
    )
    confs = " • ".join(x for x in [game["away"].get("conf", ""), game["home"].get("conf", "")] if x)
    st.markdown(f"<div class='mono muted'>{kick}{' • ' + confs if confs else ''}</div>", unsafe_allow_html=True)
    st.markdown("<div class='mono'><b>🤖 MONEYLINE MODEL</b></div>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='mono'><b>{model['pick']['name']} ML</b><br>Win Probability&nbsp;&nbsp; <b>{model['p']*100:.0f}%</b><br>Pick Strength&nbsp;&nbsp;&nbsp; <b class='{color}'>{grade}</b><br>Model Edge&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>N/A until verified MLs</b></div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='mono'><br><b>WHY {model['pick']['name'].upper()}?</b><br>" + "".join(f"✓ {r}<br>" for r in model["reasons"]) + "</div>", unsafe_allow_html=True)
    st.markdown("<div class='mono'><br><b>⭐ BEST PLAYER PROPS</b></div>", unsafe_allow_html=True)
    if props:
        suggested = props[0]
        if suggested["market"] == "Anytime TD":
            detail = f"Projected TD probability <b>{suggested['td_probability']*100:.0f}%</b>"
        else:
            detail = f"Projection <b>{suggested['projection']:.1f}</b> • Model target <b>{suggested['suggestion']}</b>"
        st.markdown(
            f"<div class='suggested mono'><b>🎯 SUGGESTED PROP</b><br><b>{suggested['player']} — {suggested['suggestion']}</b><br>{detail}<br>Strength <b>{suggested['grade']}</b></div>",
            unsafe_allow_html=True,
        )
        for p in props:
            if p["market"] == "Anytime TD":
                body = f"Projected TD probability&nbsp;&nbsp; <b>{p['td_probability']*100:.0f}%</b><br>Suggested prop&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>ANYTIME TD — YES</b><br>Strength&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['grade']}</b><br><span class='muted'>Scoring rate: {p['ypg']:.2f} TD/game • model probability, not sportsbook odds</span>"
            else:
                unit = "receptions" if p["market"] == "Receptions" else "yards"
                body = f"Projection&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['projection']:.1f} {unit}</b><br>Suggested prop&nbsp;&nbsp; <b>{p['suggestion']}</b><br>Strength&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['grade']}</b><br><span class='muted'>Suggested number is a model threshold, not a sportsbook line</span>"
            st.markdown(
                f"<div class='prop mono'><b>{p['market'].upper()}</b><br>Player&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['player']} ({p['team']})</b><br>{body}</div>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown("<div class='mono warn'>Player data unavailable for this matchup. No prop was fabricated.</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


# ---------------------- UI / fast path ----------------------
st.title("🏈 NCAAF EDGE SCANNER")
st.caption("Fast-load hands-free build • no API keys • public webpages only")
now = datetime.now(ET)
phase, phase_desc = weekly_status(now)
st.markdown(f"<span class='badge'>{phase}</span> <span class='muted'>{phase_desc}</span>", unsafe_allow_html=True)

with st.spinner("Loading the upcoming schedule..."):
    sp = schedule_pack(SEASON, now.date().isoformat(), LOOKAHEAD_DAYS)

if not sp["games"]:
    st.error("The schedule source did not load. The app stopped instead of hanging or inventing games.")
    st.caption("CBS Sports is primary. If its page loads but markup changes, the parser diagnostic below will say so. Sports-Reference is only a last-resort fallback and may return 403 on Streamlit Cloud.")
    if sp.get("errors"):
        st.code("\n".join(f"{k}: {v}" for k, v in sp["errors"].items()))
    st.stop()

st.success(f"Found {len(sp['games'])} upcoming games from {sp.get('source', 'public schedule sources')}. Building the model board now...")

with st.spinner("Loading standings, FEI ratings, logos, and player leader tables in parallel..."):
    core = core_pack(SEASON)

games = [apply_team_meta(dict(g), core) for g in sp["games"]]
modeled = []
for g in games:
    try:
        m = baseline_model(g, core)
        pp = player_props(g, m, core)
        modeled.append((g, m, pp))
    except Exception:
        continue

if not modeled:
    st.error("Games loaded, but the comparison tables were unavailable. Check Source Health below.")
else:
    anchors = [x for x in modeled if x[1]["grade"] == "A+"]
    strong = [x for x in modeled if x[1]["grade"] in ("A", "B+")]
    anchors.sort(key=lambda x: x[1]["p"], reverse=True)
    strong.sort(key=lambda x: x[1]["p"], reverse=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Upcoming Games", len(modeled))
    c2.metric("A+ Anchors", len(anchors))
    c3.metric("A / B+ Strong", len(strong))
    c4.metric("Prop Candidates", sum(len(x[2]) for x in modeled))

    st.subheader("🔥 STRONGEST MONEYLINE PICKS")
    top_picks = (anchors + strong)[:10]
    if top_picks:
        summary_rows = []
        for g, m, pp in top_picks:
            best_prop = pp[0] if pp else None
            summary_rows.append({
                "Kickoff (ET)": g["date"].strftime("%a %m/%d %I:%M %p").replace(" 0", " "),
                "Game": f"{g['away']['name']} @ {g['home']['name']}",
                "ML Pick": m["pick"]["name"],
                "ML Tier": m["grade"],
                "Win %": f"{m['p']*100:.0f}%",
                "Suggested Prop": f"{best_prop['player']} — {best_prop['suggestion']}" if best_prop else "—",
                "Prop Tier": best_prop["grade"] if best_prop else "—",
            })
        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)
    else:
        st.info("No A+, A, or B+ moneyline picks are available right now.")

    st.subheader("🕒 ALL GAMES BY KICKOFF TIME")
    chronological = sorted(modeled, key=lambda x: (x[0].get("date") or datetime.max.replace(tzinfo=ET), -x[1]["p"]))
    last_bucket = None
    for g, m, pp in chronological:
        d = g.get("date")
        if g.get("time_label") == "TBD":
            bucket = (g.get("date_label", "Upcoming"), "TIME TBD")
        else:
            bucket = (g.get("date_label", "Upcoming"), g.get("time_label", "TBD"))
        if bucket != last_bucket:
            st.markdown(f"<div class='timebar'>🕒 {bucket[0]} — {bucket[1]} ET</div>", unsafe_allow_html=True)
            last_bucket = bucket
        render_card(g, m, pp)

    # Deep verification happens after the visible board, so slower sites cannot prevent results from appearing.
    verify_targets = (anchors + strong)[:4]
    if verify_targets:
        st.subheader("🔎 DEEP SOURCE VERIFICATION — TOP PICKS")
        st.caption("Loaded after the main board so CollegeFootballData, Game on Paper, or Winsipedia cannot block the search results.")
        for g, m, _ in verify_targets:
            with st.spinner(f"Deep-checking {g['away']['name']} @ {g['home']['name']}..."):
                dv = deep_verify(g["home"]["name"], g["away"]["name"], g["home"].get("id"), g["away"].get("id"), SEASON)
            cfd_ok = bool(dv["cfbd_home"] or dv["cfbd_away"])
            gop_ok = bool(dv["gop_home"] or dv["gop_away"])
            win_ok = dv["winsipedia_ok"]
            st.markdown(f"**{g['away']['name']} @ {g['home']['name']} — {m['pick']['name']} ML [{m['grade']}]**")
            st.caption(f"{'✓' if cfd_ok else '—'} CollegeFootballData • {'✓' if gop_ok else '—'} Game on Paper • {'✓' if win_ok else '—'} Winsipedia • ✓ Sports-Reference • {'✓' if core['bcf'] else '—'} BCF Toys")
            rows = []
            if dv["gop_away"] or dv["gop_home"]:
                rows.append({"Source": "Game on Paper", "Metric": "Adj EPA/Play", g["away"]["name"]: dv["gop_away"].get("adj_epa", "—"), g["home"]["name"]: dv["gop_home"].get("adj_epa", "—")})
            if dv["cfbd_away"] or dv["cfbd_home"]:
                rows.append({"Source": "CollegeFootballData", "Metric": "PPA", g["away"]["name"]: dv["cfbd_away"].get("ppa", "—"), g["home"]["name"]: dv["cfbd_home"].get("ppa", "—")})
                rows.append({"Source": "CollegeFootballData", "Metric": "Success Rate", g["away"]["name"]: dv["cfbd_away"].get("success", "—"), g["home"]["name"]: dv["cfbd_home"].get("success", "—")})
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

with st.expander("🩺 SOURCE HEALTH"):
    st.write(f"**Schedule:** {sp.get('source', 'Public web fallback')}", "✅" if sp["games"] else "❌")
    status = {
        "Team comparison stats (CBS / Sports-Reference fallback)": bool(core["standings"]),
        "BCF Toys FEI": bool(core["bcf"]),
        "ESPN team logos": bool(core["teams"]),
        "Passing leaders (CBS / Sports-Reference fallback)": bool(core["players"]["passing"]),
        "Rushing leaders (CBS / Sports-Reference fallback)": bool(core["players"]["rushing"]),
        "Receiving leaders (CBS / Sports-Reference fallback)": bool(core["players"]["receiving"]),
    }
    for k, ok in status.items():
        st.write(f"{'✅' if ok else '⚠️'} {k}")
    if core["errors"]:
        st.caption("Blocked/unavailable sources are skipped. They no longer prevent the board from loading.")
        st.code("\n".join(f"{k}: {v}" for k, v in core["errors"].items()))

st.divider()
st.caption("Data integrity: the app does not invent games, players, sportsbook lines, or source values. A+ is the model's highest confidence tier, not a guaranteed winner. Suggested player-prop numbers are model thresholds, not sportsbook lines. Anytime TD is a model probability, not a quoted sportsbook price.")
