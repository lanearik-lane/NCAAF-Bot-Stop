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


@st.cache_data(ttl=900, show_spinner=False)
def schedule_pack(season: int, start_iso: str, days: int):
    start = datetime.fromisoformat(start_iso).date()
    end = start + timedelta(days=days)
    url = f"https://www.sports-reference.com/cfb/years/{season}-schedule.html"
    html, err = request_text(url, 9)
    if not html:
        return {"games": [], "error": err, "url": url}
    tabs = tables_from_html(html)
    sched = None
    for t in tabs:
        if col(t, "Date") and (col(t, "Winner") or col(t, "Visitor")) and len(t) > 50:
            sched = t
            break
    if sched is None:
        return {"games": [], "error": "Schedule table not found", "url": url}

    dc = col(sched, "Date")
    tc = col(sched, "Time")
    weekc = col(sched, "Wk")
    firstc = col(sched, "Visitor", "Winner")
    secondc = col(sched, "Home", "Loser")
    if not firstc or not secondc:
        return {"games": [], "error": "Team columns not found", "url": url}

    # The unnamed location column is normally between the two teams and contains @ or N.
    locc = None
    for c in sched.columns:
        vals = set(str(x).strip() for x in sched[c].dropna().head(1000).tolist())
        if "@" in vals or "N" in vals:
            locc = c
            break

    games = []
    for idx, row in sched.iterrows():
        raw_date = str(row.get(dc, "")).strip()
        if not raw_date or raw_date.lower() == "date":
            continue
        try:
            gd = pd.to_datetime(raw_date, errors="raise").date()
        except Exception:
            continue
        if not (start <= gd <= end):
            continue
        a = clean_team(row.get(firstc, ""))
        b = clean_team(row.get(secondc, ""))
        if not a or not b or a.lower() in ("winner", "visitor") or b.lower() in ("loser", "home"):
            continue
        marker = str(row.get(locc, "")).strip() if locc else "@"
        neutral = marker.upper() == "N"
        # Future schedule rows are Visitor @ Home. For neutral games we retain listed order.
        away, home = a, b
        raw_time = str(row.get(tc, "TBD")).strip() if tc else "TBD"
        if raw_time.lower() in ("nan", "", "time"):
            raw_time = "TBD"
        if raw_time.upper() == "TBD":
            kickoff = datetime(gd.year, gd.month, gd.day, 12, 0, tzinfo=ET)
        else:
            try:
                parsed = pd.to_datetime(f"{gd.isoformat()} {raw_time}", errors="raise")
                kickoff = datetime(parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, tzinfo=ET)
            except Exception:
                kickoff = datetime(gd.year, gd.month, gd.day, 12, 0, tzinfo=ET)
        games.append({
            "id": f"sr-{gd.isoformat()}-{idx}",
            "date": kickoff,
            "date_label": gd.strftime("%A • %B %d, %Y"),
            "time_label": raw_time,
            "away": {"name": away, "key": norm(away), "id": None, "abbr": "", "logo": None},
            "home": {"name": home, "key": norm(home), "id": None, "abbr": "", "logo": None},
            "neutral": neutral,
            "week": int(number(row.get(weekc))) if weekc and number(row.get(weekc)) is not None else None,
            "schedule_source": "Sports-Reference",
        })
    games.sort(key=lambda g: g["date"])
    return {"games": games, "error": None, "url": url}


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
        # Prefer a yards column that belongs to the requested category when columns are flattened.
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
            rows.append({"player": player, "team": team, "team_key": norm(team), "kind": kind, "games": gp or 1, "ypg": ypg, "yards": yds, "td": number(r.get(tdc)) if tdc else None})
        if rows:
            break
    return rows


@st.cache_data(ttl=1800, show_spinner=False)
def core_pack(season: int):
    urls = {
        "standings": f"https://www.sports-reference.com/cfb/years/{season}-standings.html",
        "bcf": f"https://bcftoys.com/{season}-fei",
        "espn_teams": "https://www.espn.com/college-football/teams",
        "passing": f"https://www.sports-reference.com/cfb/years/{season}-passing.html",
        "rushing": f"https://www.sports-reference.com/cfb/years/{season}-rushing.html",
        "receiving": f"https://www.sports-reference.com/cfb/years/{season}-receiving.html",
    }
    raw = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(request_text, u, 8): k for k, u in urls.items()}
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
        "standings": parse_standings(raw.get("standings")),
        "bcf": parse_bcf(raw.get("bcf")),
        "teams": parse_espn_teams(raw.get("espn_teams")),
        "players": {
            "passing": parse_player_table(raw.get("passing"), "passing"),
            "rushing": parse_player_table(raw.get("rushing"), "rushing"),
            "receiving": parse_player_table(raw.get("receiving"), "receiving"),
        },
        "errors": errors,
        "urls": urls,
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

    add("Win rate", hs.get("pct"), as_.get("pct"), .20, .30, "Sports-Reference")
    add("SRS", hs.get("srs"), as_.get("srs"), .25, 12, "Sports-Reference")
    add("SOS", hs.get("sos"), as_.get("sos"), .07, 8, "Sports-Reference")
    if hs.get("off") is not None and hs.get("def") is not None and as_.get("off") is not None and as_.get("def") is not None:
        add("Scoring margin", hs["off"] - hs["def"], as_["off"] - as_["def"], .15, 16, "Sports-Reference")
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


def prop_grade(sample: float, cushion: float, matchup: float):
    score = .45 * min(sample / 4.0, 1.0) + .35 * min(max(cushion, 0) / .12, 1.0) + .20 * min(max(matchup, 0), 1.0)
    return "A+" if score >= .90 else "A" if score >= .82 else "B+" if score >= .74 else "B" if score >= .66 else "C+" if score >= .58 else "C" if score >= .50 else "D" if score >= .42 else "F"


def player_props(game, model, core):
    team_keys = {norm(game["away"]["name"]): game["away"], norm(game["home"]["name"]): game["home"]}
    candidates = []
    mapping = {"passing": "QB Passing Yards", "rushing": "RB Rushing Yards", "receiving": "WR/TE Receiving Yards"}
    for kind, market in mapping.items():
        for r in core["players"].get(kind, []):
            tm = None
            if r["team_key"] in team_keys:
                tm = team_keys[r["team_key"]]
            else:
                for tk, obj in team_keys.items():
                    if len(tk) >= 4 and (tk in r["team_key"] or r["team_key"] in tk):
                        tm = obj
                        break
            if not tm:
                continue
            opp = game["home"] if tm is game["away"] else game["away"]
            opp_bcf = lookup(core["bcf"], opp["name"])
            opp_sr = lookup(core["standings"], opp["name"])
            adj = 1.0
            matchup_strength = .5
            dfei = opp_bcf.get("dfei")
            if dfei is not None:
                # better defensive FEI reduces projection
                delta = max(-.08, min(.08, -dfei * .06))
                adj += delta
                matchup_strength = .75 if delta > 0 else .35
            opp_def = opp_sr.get("def")
            if opp_def is not None:
                delta = max(-.05, min(.05, (opp_def - 27.0) / 100.0))
                adj += delta
                matchup_strength = max(matchup_strength, .7 if delta > 0 else .4)
            proj = max(0.0, r["ypg"] * adj)
            target = max(.5, round(proj * .90 * 2) / 2)
            cushion = (proj - target) / max(target, 1)
            grade = prop_grade(r.get("games", 1), cushion, matchup_strength)
            candidates.append({**r, "team_obj": tm, "opp": opp, "market": market, "projection": proj, "target": target, "grade": grade})
    best = []
    for market in mapping.values():
        rows = [x for x in candidates if x["market"] == market]
        if rows:
            rows.sort(key=lambda x: (GRADE_ORDER.index(x["grade"]), x["projection"]), reverse=True)
            best.append(rows[0])
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
        for p in props:
            st.markdown(
                f"<div class='prop mono'><b>{p['market']}</b><br>Player&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['player']} ({p['team']})</b><br>Projection&nbsp;&nbsp; <b>{p['projection']:.1f}</b><br>Model target <b>OVER {p['target']:.1f} or lower</b><br>Strength&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['grade']}</b><br><span class='muted'>Base: {p['ypg']:.1f}/game • target is a model threshold, not a sportsbook line</span></div>",
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
    st.caption(f"Schedule source: {sp['url']}")
    if sp.get("error"):
        st.code(sp["error"])
    st.stop()

st.success(f"Found {len(sp['games'])} upcoming games from Sports-Reference. Building the model board now...")

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
    rest = [x for x in modeled if x[1]["grade"] not in ("A+", "A", "B+")]
    anchors.sort(key=lambda x: x[1]["p"], reverse=True)
    strong.sort(key=lambda x: x[1]["p"], reverse=True)
    rest.sort(key=lambda x: x[1]["p"], reverse=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Upcoming Games", len(modeled))
    c2.metric("A+ Anchors", len(anchors))
    c3.metric("A / B+ Strong", len(strong))
    c4.metric("Player Markets", sum(len(x[2]) for x in modeled))

    st.subheader("⚓ A+ ANCHOR PICKS")
    if anchors:
        for g, m, pp in anchors:
            render_card(g, m, pp)
    else:
        st.info("No A+ matchup met the current model and source-coverage threshold.")

    st.subheader("💪 A / B+ STRONG PICKS")
    if strong:
        for g, m, pp in strong:
            render_card(g, m, pp)
    else:
        st.info("No A or B+ picks are available right now.")

    with st.expander(f"📋 B THROUGH F UPCOMING GAMES ({len(rest)})"):
        for g, m, pp in rest:
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
    st.write("**Schedule:** Sports-Reference", "✅" if sp["games"] else "❌")
    status = {
        "Sports-Reference standings": bool(core["standings"]),
        "BCF Toys FEI": bool(core["bcf"]),
        "ESPN team logos": bool(core["teams"]),
        "Sports-Reference passing": bool(core["players"]["passing"]),
        "Sports-Reference rushing": bool(core["players"]["rushing"]),
        "Sports-Reference receiving": bool(core["players"]["receiving"]),
    }
    for k, ok in status.items():
        st.write(f"{'✅' if ok else '⚠️'} {k}")
    if core["errors"]:
        st.caption("Blocked/unavailable sources are skipped. They no longer prevent the board from loading.")
        st.code("\n".join(f"{k}: {v}" for k, v in core["errors"].items()))

st.divider()
st.caption("Data integrity: the app does not invent games, players, sportsbook lines, or source values. A+ is the model's highest confidence tier, not a guaranteed winner. Player 'Model target' values are projection thresholds, not sportsbook lines.")
