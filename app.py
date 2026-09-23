from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import io, json, math, re

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title='NCAAF Edge Scanner', page_icon='🏈', layout='wide')
ET = ZoneInfo('America/New_York')
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153 Safari/537.36'
}
ESPN = 'https://www.espn.com'

# ESPN public scoreboard groups. This keeps the app API-key free.
SCAN_GROUPS = {
    'FBS': 80,
    'FCS': 90,
    'D-II / D-III': 35,
}

# Hands-free defaults: no scan controls are shown to the user.
SCAN_AHEAD_DAYS = 7
SELECTED_DIVISIONS = list(SCAN_GROUPS)

st.markdown('''<style>
.block-container{max-width:1450px;padding-top:1rem}
.game-card{background:#202020;border:1px solid #353535;border-radius:16px;padding:18px 22px;margin:10px 0 22px 0}
.teamline{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:1.0rem;font-weight:800;letter-spacing:.02em}
.kick{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:700;margin:8px 0 24px}
.section-title{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900;margin-top:12px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.65}
.pick-name{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900;font-size:1.08rem}
.rule{border-top:1px solid #aaa;width:34%;margin:26px 0}
.logo{height:42px;max-width:52px;object-fit:contain;vertical-align:middle;margin:0 8px}
.good{color:#32d583}.warn{color:#fdb022}.bad{color:#f97066}.muted{color:#aaa}
.propbox{border:1px solid #3b3b3b;border-radius:12px;padding:12px 14px;margin:8px 0;background:#181818}
.stMetric{border:1px solid rgba(128,128,128,.22);padding:9px;border-radius:10px}
</style>''', unsafe_allow_html=True)

@st.cache_data(ttl=900, show_spinner=False)
def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def walk(o):
    if isinstance(o, dict):
        if isinstance(o.get('competitions'), list) and o.get('competitions'):
            yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def json_blobs(html):
    soup = BeautifulSoup(html, 'html.parser')
    out = []
    for s in soup.find_all('script'):
        t = s.string or s.get_text('', strip=True)
        if not t:
            continue
        if s.get('type') == 'application/json':
            try:
                out.append(json.loads(t))
                continue
            except Exception:
                pass
        if 'competitions' in t and 'competitors' in t:
            for p in [m.start() for m in re.finditer(r'\{', t)][:12]:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(t[p:])
                    out.append(obj)
                    break
                except Exception:
                    pass
    return out


def team_obj(c):
    t = c.get('team') or {}
    logos = t.get('logos') or []
    recs = c.get('records') or []
    rec = recs[0].get('summary', '') if recs and isinstance(recs[0], dict) else ''
    rank = c.get('curatedRank', {}).get('current') if isinstance(c.get('curatedRank'), dict) else None
    return {
        'id': str(t.get('id', '')),
        'name': t.get('displayName') or t.get('shortDisplayName') or 'Unknown',
        'abbr': t.get('abbreviation', ''),
        'logo': logos[0].get('href') if logos and isinstance(logos[0], dict) else None,
        'record': rec,
        'rank': rank if isinstance(rank, int) and rank < 99 else None,
    }


def parse_games(html, lo, hi, division):
    games = {}
    for blob in json_blobs(html):
        for e in walk(blob):
            try:
                eid = str(e.get('id', ''))
                comp = (e.get('competitions') or [])[0]
                cs = comp.get('competitors') or []
                if not eid or len(cs) < 2:
                    continue
                dt = datetime.fromisoformat(str(e.get('date')).replace('Z', '+00:00')).astimezone(ET)
                if not lo <= dt.date() <= hi or dt <= datetime.now(ET):
                    continue
                by = {x.get('homeAway'): x for x in cs}
                if not {'home', 'away'} <= set(by):
                    continue
                games[eid] = {
                    'id': eid,
                    'date': dt,
                    'home': team_obj(by['home']),
                    'away': team_obj(by['away']),
                    'status': (e.get('status') or {}).get('type', {}).get('description', 'Scheduled'),
                    'venue': ((comp.get('venue') or {}).get('fullName') or ''),
                    'division': division,
                }
            except Exception:
                continue
    return sorted(games.values(), key=lambda x: x['date'])


def scan_dates(start, days, divisions):
    allg = {}
    errors = []
    for i in range(days + 1):
        d = start + timedelta(days=i)
        for div in divisions:
            group = SCAN_GROUPS[div]
            url = f'{ESPN}/college-football/scoreboard/_/date/{d:%Y%m%d}/group/{group}'
            try:
                for g in parse_games(fetch(url), d, d, div):
                    # Prefer the more specific division label on duplicates.
                    if g['id'] not in allg or allg[g['id']].get('division') == 'D-II / D-III':
                        allg[g['id']] = g
            except Exception as e:
                errors.append(f'{d} • {div}: {type(e).__name__}')
    return sorted(allg.values(), key=lambda x: x['date']), errors


def pct(rec):
    m = re.search(r'(\d+)\s*-\s*(\d+)', rec or '')
    if not m:
        return None
    w, l = map(int, m.groups())
    return w / (w + l) if w + l else None


def games_played(rec):
    m = re.search(r'(\d+)\s*-\s*(\d+)', rec or '')
    if not m:
        return None
    w, l = map(int, m.groups())
    return max(1, w + l)


def model(g):
    hp, ap = pct(g['home']['record']), pct(g['away']['record'])
    if hp is None or ap is None:
        return None

    def rb(r):
        return 0 if not r else (26 - r) / 25 * .32

    score = (hp - ap) * 1.75 + rb(g['home']['rank']) - rb(g['away']['rank']) + .13
    ph = 1 / (1 + math.exp(-score))
    home = ph >= .5
    p = ph if home else 1 - ph
    pick = g['home'] if home else g['away']
    opp = g['away'] if home else g['home']
    grade = 'A+' if p >= .78 else 'A' if p >= .70 else 'B+' if p >= .64 else 'B' if p >= .58 else 'PASS'
    reasons = []
    if (hp if home else ap) > (ap if home else hp):
        reasons.append('Stronger team power profile from verified season results')
    if pick['rank'] and (not opp['rank'] or pick['rank'] < opp['rank']):
        reasons.append('AP ranking signal favors the pick')
    reasons.append('Home-field advantage supports the pick' if home else 'Road profile is strong enough to overcome home field')
    reasons.append('Recent season performance favors ' + pick['name'])
    return {'pick': pick, 'opp': opp, 'p': p, 'grade': grade, 'reasons': reasons}


def pick_tier(m):
    """Human-readable model tier. 'Anchor' means highest model-strength bucket, not a guarantee."""
    if not m:
        return 'PASS'
    if m['grade'] == 'A+':
        return 'ANCHOR'
    if m['grade'] == 'A':
        return 'STRONG'
    if m['grade'] in ('B+', 'B'):
        return 'LEAN'
    return 'PASS'


def american_to_prob(x):
    x = float(x)
    return 100 / (x + 100) if x > 0 else (-x) / ((-x) + 100)


def novig(a, b):
    pa, pb = american_to_prob(a), american_to_prob(b)
    s = pa + pb
    return pa / s, pb / s


def grade_edge(edge):
    return 'A+' if edge >= .10 else 'A' if edge >= .07 else 'B+' if edge >= .045 else 'B' if edge >= .025 else 'PASS'


def logo_html(t):
    return f"<img class='logo' src='{t['logo']}'/>" if t.get('logo') else "<span>[LOGO]</span>"

# ---------- PLAYER STAT / PROP TARGET SCANNER ----------

def clean_num(v):
    try:
        return float(str(v).replace(',', '').strip())
    except Exception:
        return None


def split_player_team(raw, known_abbrs):
    s = str(raw).strip()
    # ESPN tables commonly render Name+team abbreviation in one cell.
    for ab in sorted([a for a in known_abbrs if a], key=len, reverse=True):
        if s.upper().endswith(ab.upper()) and len(s) > len(ab):
            return s[:-len(ab)].strip(), ab.upper()
    return s, ''


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_stat_table(stat, season, group):
    # Public ESPN HTML pages; no API key or hidden API request is used.
    url = f'{ESPN}/college-football/stats/player/_/stat/{stat}/season/{season}/group/{group}'
    html = fetch(url)
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return pd.DataFrame()
    if len(tables) < 2:
        return pd.DataFrame()

    name_tbl = None
    stat_tbl = None
    for t in tables:
        cols = [str(c).upper() for c in t.columns]
        if 'NAME' in cols and name_tbl is None:
            name_tbl = t.copy()
        if 'YDS' in cols and stat_tbl is None:
            stat_tbl = t.copy()
    if name_tbl is None or stat_tbl is None:
        return pd.DataFrame()

    # ESPN often separates rank/name and stat columns into parallel tables.
    n = min(len(name_tbl), len(stat_tbl))
    if n == 0:
        return pd.DataFrame()
    name_tbl = name_tbl.iloc[:n].reset_index(drop=True)
    stat_tbl = stat_tbl.iloc[:n].reset_index(drop=True)
    out = pd.concat([name_tbl, stat_tbl], axis=1)
    out = out.loc[:, ~out.columns.duplicated()]
    return out


def all_stat_tables_for_division(division, season):
    group = SCAN_GROUPS[division]
    data = {}
    for stat in ('passing', 'rushing', 'receiving'):
        try:
            data[stat] = fetch_stat_table(stat, season, group)
        except Exception:
            data[stat] = pd.DataFrame()
    return data


def player_candidates(g, season):
    """Return projection-based prop candidates. These are NOT claimed to be sportsbook lines."""
    division = g['division']
    stat_tables = all_stat_tables_for_division(division, season)
    teams = [g['away'], g['home']]
    abbrs = {t['abbr'].upper() for t in teams if t.get('abbr')}
    candidates = []

    def add_from(stat, market, limit=1):
        df = stat_tables.get(stat, pd.DataFrame())
        if df.empty or 'Name' not in df.columns or 'YDS' not in df.columns:
            return
        rows = []
        for _, r in df.iterrows():
            player, ab = split_player_team(r.get('Name', ''), abbrs)
            if ab not in abbrs:
                continue
            team = next((t for t in teams if t.get('abbr', '').upper() == ab), None)
            if not team:
                continue
            yds = clean_num(r.get('YDS'))
            if yds is None:
                continue
            gp = games_played(team.get('record'))
            if not gp:
                continue
            per_game = yds / gp
            td = clean_num(r.get('TD')) or 0.0
            # Small, transparent matchup adjustment derived only from opponent record/rank.
            opp = g['home'] if team['id'] == g['away']['id'] else g['away']
            opp_wp = pct(opp.get('record'))
            adjustment = 1.0
            if opp_wp is not None:
                adjustment += (0.50 - opp_wp) * 0.14
            if opp.get('rank'):
                adjustment -= max(0.0, (26 - opp['rank']) / 25 * 0.05)
            projection = max(0.0, per_game * adjustment)
            # Target line gives the user a usable threshold without pretending it is a book line.
            target_over = max(0.5, round((projection * .90) * 2) / 2)
            score = projection * (1 + min(td / max(gp, 1), 1) * .08)
            rows.append({
                'player': player,
                'team': ab,
                'market': market,
                'projection': projection,
                'target_over': target_over,
                'season_total': yds,
                'games': gp,
                'score': score,
            })
        rows.sort(key=lambda x: x['score'], reverse=True)
        candidates.extend(rows[:limit])

    add_from('passing', 'QB Passing Yards', 1)
    add_from('rushing', 'RB Rushing Yards', 1)
    add_from('receiving', 'WR/TE Receiving Yards', 1)
    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates[:3]


def prop_strength(c):
    # Strength reflects stability of the season sample + distance between average and threshold.
    sample = min(c['games'] / 5, 1)
    cushion = (c['projection'] - c['target_over']) / max(c['target_over'], 1)
    x = .65 * sample + .35 * min(cushion / .15, 1)
    return 'A' if x >= .88 else 'B+' if x >= .72 else 'B' if x >= .55 else 'C'


def prop_cards(g):
    season = g['date'].year
    try:
        props = player_candidates(g, season)
    except Exception:
        props = []
    st.markdown("<div class='rule'></div><div class='section-title'>⭐ &nbsp;BEST PLAYER PROPS</div>", unsafe_allow_html=True)
    if not props:
        st.markdown(
            "<div class='mono warn'>PLAYER STAT DATA NOT AVAILABLE YET — the bot did not fabricate a player or line. "
            "Use the manual verified-line grader below when a sportsbook posts props.</div>", unsafe_allow_html=True)
        return

    for p in props:
        strength = prop_strength(p)
        st.markdown(
            f"<div class='propbox mono'><b>{p['market']}</b><br>"
            f"Player&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['player']} ({p['team']})</b><br>"
            f"Projection&nbsp;&nbsp; <b>{p['projection']:.1f}</b><br>"
            f"Model target <b>OVER {p['target_over']:.1f} or lower</b><br>"
            f"Strength&nbsp;&nbsp;&nbsp;&nbsp; <b>{strength}</b><br>"
            f"<span class='muted'>Season sample: {p['games']} team games • {p['season_total']:.0f} yards. "
            f"Target is a model threshold, not a sportsbook line.</span></div>",
            unsafe_allow_html=True,
        )


def game_card(g):
    m = model(g)
    st.markdown("<div class='game-card'>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='teamline'>{g['away']['name'].upper()} {logo_html(g['away'])} &nbsp;&nbsp;&nbsp; "
        f"{logo_html(g['home'])} {g['home']['name'].upper()}</div>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='kick'>{g['date'].strftime('%A • %B %d, %Y • %I:%M %p ET')}"
        f" &nbsp; • &nbsp; {g['division']}</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>🤖 &nbsp;MONEYLINE MODEL</div>", unsafe_allow_html=True)
    if not m:
        st.markdown("<div class='mono'>PASS — not enough verified record data.</div>", unsafe_allow_html=True)
        prop_cards(g)
        st.markdown("</div>", unsafe_allow_html=True)
        return

    tier = pick_tier(m)
    tier_class = 'good' if tier in ('ANCHOR', 'STRONG') else ('warn' if tier == 'LEAN' else 'bad')
    st.markdown(f"<div class='pick-name'>{m['pick']['name']} ML &nbsp; <span class='{tier_class}'>[{tier}]</span></div><br>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='mono'>Win Probability&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{m['p']*100:.0f}%</b><br>"
        f"Model Edge&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>N/A until verified MLs</b><br>"
        f"Pick Strength&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{m['grade']}</b><br>"
        f"Pick Tier&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{tier}</b></div>", unsafe_allow_html=True)
    st.markdown(f"<br><div class='section-title'>WHY {m['pick']['name'].upper()}?</div>", unsafe_allow_html=True)
    st.markdown("<div class='mono'>" + ''.join('✓ ' + r + '<br>' for r in m['reasons']) + "</div>", unsafe_allow_html=True)
    prop_cards(g)
    st.markdown("</div>", unsafe_allow_html=True)

    with st.expander('💵 VERIFIED MONEYLINE + PLAYER PROP GRADER'):
        x, y = st.columns(2)
        away = x.number_input(f"{g['away']['abbr'] or 'Away'} ML", value=0, step=5, key='a' + g['id'])
        home = y.number_input(f"{g['home']['abbr'] or 'Home'} ML", value=0, step=5, key='h' + g['id'])
        if away and home:
            pa, ph = novig(away, home)
            market_prob = ph if m['pick']['id'] == g['home']['id'] else pa
            edge = m['p'] - market_prob
            st.metric('Verified-line Model Edge', f'{edge*100:+.1f}%')
            st.write(f"Market no-vig probability: **{market_prob*100:.1f}%** • Edge grade: **{grade_edge(edge)}**")

        p1, p2, p3 = st.columns(3)
        player = p1.text_input('Player', key='p' + g['id'])
        market_name = p2.selectbox('Market', ['Passing Yards', 'Rushing Yards', 'Receiving Yards', 'Receptions', 'Passing TDs', 'Anytime TD'], key='m' + g['id'])
        line = p3.number_input('Verified sportsbook line', min_value=0.0, value=0.0, step=.5, key='l' + g['id'])
        q1, q2 = st.columns(2)
        proj = q1.number_input('Your/model projection', min_value=0.0, value=0.0, step=.5, key='pr' + g['id'])
        side = q2.selectbox('Side', ['OVER', 'UNDER'], key='s' + g['id'])
        if player and line > 0 and proj > 0:
            raw = (proj - line) if side == 'OVER' else (line - proj)
            rel = raw / max(line, 1)
            strength = 'A+' if rel >= .18 else 'A' if rel >= .12 else 'B+' if rel >= .08 else 'B' if rel >= .04 else 'PASS'
            st.write(f"**{player} — {side} {line:g} {market_name}** • Projection **{proj:g}** • Edge **{raw:+.1f}** • Strength **{strength}**")


st.title('🏈 NCAAF EDGE SCANNER')
st.caption('Hands-free upcoming-game scan • all conference pools • moneyline model • player-prop targets • no API keys')

# The app scans automatically when it opens. There are no scan controls or search buttons.
# Cached public-page reads reduce traffic; a normal Streamlit rerun refreshes stale data after cache TTL.
def run_hands_free_scan():
    config_key = (datetime.now(ET).date().isoformat(), SCAN_AHEAD_DAYS, tuple(SELECTED_DIVISIONS))
    if st.session_state.get('scan_config') != config_key or 'games' not in st.session_state:
        with st.spinner(f'Automatically searching all conference pools for upcoming games in the next {SCAN_AHEAD_DAYS} days, plus player prop candidates...'):
            gs, errs = scan_dates(datetime.now(ET).date(), SCAN_AHEAD_DAYS, SELECTED_DIVISIONS)
            st.session_state.games = gs
            st.session_state.errors = errs
            st.session_state.scanned = datetime.now(ET)
            st.session_state.scan_config = config_key

run_hands_free_scan()

st.info(
    f'Hands-free mode is ON: the app scans FBS, FCS, and D-II/D-III automatically for the next '
    f'{SCAN_AHEAD_DAYS} days when the app opens. No scan settings or search button are required.'
)

games = st.session_state.get('games', [])
if 'scanned' not in st.session_state:
    st.info('The automatic search has not completed yet.')
elif not games:
    st.warning('No upcoming games were parsed in the hands-free window. No games, players, or lines were fabricated.')
else:
    modeled = [(g, model(g)) for g in games]
    anchors = [(g, m) for g, m in modeled if m and pick_tier(m) == 'ANCHOR']
    strong = [(g, m) for g, m in modeled if m and pick_tier(m) == 'STRONG']
    leans = [(g, m) for g, m in modeled if m and pick_tier(m) == 'LEAN']
    passes = [(g, m) for g, m in modeled if not m or pick_tier(m) == 'PASS']

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('Upcoming Games', len(games))
    c2.metric('Anchor Picks', len(anchors))
    c3.metric('Strong Picks', len(strong))
    c4.metric('Other / Pass', len(leans) + len(passes))
    st.success(f"Last scan: {st.session_state.scanned.strftime('%m/%d/%y %I:%M %p ET')}")

    st.subheader('⚓ ANCHOR PICKS')
    st.caption('Highest model-strength moneyline bucket (A+). “Anchor” is a model tier, not a guarantee.')
    if anchors:
        for g, _ in sorted(anchors, key=lambda x: x[1]['p'], reverse=True):
            game_card(g)
    else:
        st.warning('No A+ anchor-level moneyline picks were found in the current upcoming slate.')

    st.subheader('💪 STRONG PICKS')
    st.caption('A-grade moneyline picks that clear the model’s strong-pick threshold.')
    if strong:
        for g, _ in sorted(strong, key=lambda x: x[1]['p'], reverse=True):
            game_card(g)
    else:
        st.info('No A-grade strong picks were found in the current upcoming slate.')

    with st.expander(f'📋 OTHER UPCOMING GAMES ({len(leans) + len(passes)})'):
        for g, _ in leans + passes:
            game_card(g)

errors = st.session_state.get('errors', [])
if errors:
    with st.expander(f'⚠️ Source warnings ({len(errors)})'):
        st.write('Some public pages did not load. The scanner kept the games it could verify.')
        st.code('\n'.join(errors[:50]))

st.divider()
st.caption(
    'Data integrity: the app reads free public ESPN webpages and does not require an API key. '
    'Player “model target” lines are calculated betting thresholds, not claimed sportsbook lines. '
    'A sportsbook line is treated as verified only when entered in the grader. '
    'Anchor/Strong labels reflect model strength only and do not guarantee an outcome.'
)
