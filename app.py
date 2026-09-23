from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import io, json, math, re, unicodedata

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup, Comment

st.set_page_config(page_title='NCAAF Multi-Source Edge Scanner', page_icon='🏈', layout='wide')
ET = ZoneInfo('America/New_York')
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153 Safari/537.36'
}
ESPN = 'https://www.espn.com'
SCAN_GROUPS = {'FBS': 80, 'FCS': 90, 'D-II / D-III': 35}
SCAN_AHEAD_DAYS = 7
SELECTED_DIVISIONS = list(SCAN_GROUPS)

st.markdown('''<style>
.block-container{max-width:1500px;padding-top:1rem}
.game-card{background:#202020;border:1px solid #353535;border-radius:16px;padding:18px 22px;margin:10px 0 22px 0}
.teamline{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:1.0rem;font-weight:800;letter-spacing:.02em}
.kick{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:700;margin:8px 0 18px}
.section-title{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900;margin-top:12px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.65}
.pick-name{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900;font-size:1.08rem}
.rule{border-top:1px solid #777;width:36%;margin:22px 0}
.logo{height:42px;max-width:52px;object-fit:contain;vertical-align:middle;margin:0 8px}
.good{color:#32d583}.warn{color:#fdb022}.bad{color:#f97066}.muted{color:#aaa}
.propbox{border:1px solid #3b3b3b;border-radius:12px;padding:12px 14px;margin:8px 0;background:#181818}
.source-ok{color:#32d583}.source-miss{color:#fdb022}.source-bad{color:#f97066}
.stMetric{border:1px solid rgba(128,128,128,.22);padding:9px;border-radius:10px}
</style>''', unsafe_allow_html=True)

# ---------- generic web helpers ----------
@st.cache_data(ttl=900, show_spinner=False)
def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=22)
    r.raise_for_status()
    return r.text


def norm(s):
    s = unicodedata.normalize('NFKD', str(s)).encode('ascii', 'ignore').decode('ascii')
    s = s.lower().replace('&', 'and')
    s = re.sub(r'[^a-z0-9]+', ' ', s).strip()
    aliases = {
        'southern california':'usc', 'southern methodist':'smu', 'texas christian':'tcu',
        'miami fl':'miami', 'miami florida':'miami', 'mississippi':'ole miss',
        'louisiana state':'lsu', 'central florida':'ucf', 'nevada las vegas':'unlv',
        'connecticut':'uconn', 'appalachian state':'app state', 'massachusetts':'umass',
        'hawai i':'hawaii', 'san jose state':'san jose state', 'north carolina state':'nc state',
        'southern mississippi':'southern miss', 'louisiana monroe':'ul monroe',
    }
    return aliases.get(s, s)


def slug(s):
    x = norm(s)
    special = {
        'miami':'miami-fl', 'ole miss':'mississippi', 'lsu':'louisiana-state', 'usc':'southern-california',
        'smu':'southern-methodist', 'tcu':'texas-christian', 'ucf':'central-florida', 'unlv':'nevada-las-vegas',
        'uconn':'connecticut', 'umass':'massachusetts', 'app state':'appalachian-state', 'nc state':'north-carolina-state',
        'hawaii':'hawaii', 'ul monroe':'louisiana-monroe', 'southern miss':'southern-mississippi',
    }
    return special.get(x, x.replace(' ', '-'))


def all_tables(html):
    # Sports-Reference often wraps tables in HTML comments. Unwrap them first.
    soup = BeautifulSoup(html, 'html.parser')
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        txt = str(c)
        if '<table' in txt:
            try:
                c.replace_with(BeautifulSoup(txt, 'html.parser'))
            except Exception:
                pass
    try:
        return pd.read_html(io.StringIO(str(soup)))
    except Exception:
        return []


def flat_cols(df):
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [' '.join([str(x) for x in c if str(x) != 'nan']).strip() for c in d.columns]
    else:
        d.columns = [str(c).strip() for c in d.columns]
    return d


def num(v):
    try:
        s = str(v).replace(',', '').replace('%', '').replace('#', '').strip()
        m = re.search(r'-?\d+(?:\.\d+)?', s)
        return float(m.group()) if m else None
    except Exception:
        return None


def find_col(df, *needles):
    cols = [str(c) for c in df.columns]
    for n in needles:
        nl = n.lower()
        for c in cols:
            if c.lower() == nl:
                return c
    for n in needles:
        nl = n.lower()
        for c in cols:
            if nl in c.lower():
                return c
    return None

# ---------- ESPN schedule + logos only ----------
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
                out.append(json.loads(t)); continue
            except Exception:
                pass
        if 'competitions' in t and 'competitors' in t:
            for p in [m.start() for m in re.finditer(r'\{', t)][:12]:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(t[p:]); out.append(obj); break
                except Exception:
                    pass
    return out


def team_obj(c):
    t = c.get('team') or {}
    logos = t.get('logos') or []
    recs = c.get('records') or []
    rec = recs[0].get('summary', '') if recs and isinstance(recs[0], dict) else ''
    rank = c.get('curatedRank', {}).get('current') if isinstance(c.get('curatedRank'), dict) else None
    return {'id':str(t.get('id','')), 'name':t.get('displayName') or t.get('shortDisplayName') or 'Unknown',
            'abbr':t.get('abbreviation',''), 'logo':logos[0].get('href') if logos and isinstance(logos[0],dict) else None,
            'record':rec, 'rank':rank if isinstance(rank,int) and rank<99 else None}


def parse_games(html, lo, hi, division):
    games = {}
    for blob in json_blobs(html):
        for e in walk(blob):
            try:
                eid = str(e.get('id','')); comp = (e.get('competitions') or [])[0]; cs = comp.get('competitors') or []
                if not eid or len(cs)<2: continue
                dt = datetime.fromisoformat(str(e.get('date')).replace('Z','+00:00')).astimezone(ET)
                if not lo <= dt.date() <= hi or dt <= datetime.now(ET): continue
                by = {x.get('homeAway'):x for x in cs}
                if not {'home','away'} <= set(by): continue
                games[eid]={'id':eid,'date':dt,'home':team_obj(by['home']),'away':team_obj(by['away']),
                            'status':(e.get('status') or {}).get('type',{}).get('description','Scheduled'),
                            'venue':((comp.get('venue') or {}).get('fullName') or ''),'division':division}
            except Exception: continue
    return sorted(games.values(), key=lambda x:x['date'])


def scan_dates(start, days, divisions):
    allg={}; errors=[]
    for i in range(days+1):
        d=start+timedelta(days=i)
        for div in divisions:
            try:
                url=f'{ESPN}/college-football/scoreboard/_/date/{d:%Y%m%d}/group/{SCAN_GROUPS[div]}'
                for g in parse_games(fetch(url),d,d,div):
                    if g['id'] not in allg or allg[g['id']].get('division')=='D-II / D-III': allg[g['id']]=g
            except Exception as e:
                errors.append(f'ESPN schedule {d} {div}: {type(e).__name__}')
    return sorted(allg.values(), key=lambda x:x['date']), errors


def pct(rec):
    m=re.search(r'(\d+)\s*-\s*(\d+)',rec or '')
    if not m:return None
    w,l=map(int,m.groups()); return w/(w+l) if w+l else None


def games_played(rec):
    m=re.search(r'(\d+)\s*-\s*(\d+)',rec or '')
    if not m:return None
    w,l=map(int,m.groups()); return max(1,w+l)

# ---------- CollegeFootballData.com public team page ----------
@st.cache_data(ttl=1800, show_spinner=False)
def cfbd_team(team, season):
    url=f'https://collegefootballdata.com/team/{quote(team)}'
    try:
        html=fetch(url); soup=BeautifulSoup(html,'html.parser'); text=soup.get_text(' ',strip=True)
        tables=[flat_cols(t) for t in all_tables(html)]
        out={'ok':True,'url':url,'source':'CollegeFootballData.com'}
        # Extract useful labeled metrics from table rows where possible.
        wanted=['Predicted Points Added','Success Rate','Explosiveness','Havoc','Points Per Opportunity','Line Yards per Rush','Stuff Rate']
        for t in tables:
            if t.empty: continue
            for _,r in t.iterrows():
                vals=[str(v).strip() for v in r.tolist()]
                joined=' | '.join(vals)
                for key in wanted:
                    if key.lower() in joined.lower():
                        nums=[num(v) for v in vals[1:] if num(v) is not None]
                        if nums:
                            out.setdefault('metrics',{})[key]=nums[:2]
        # Fallback regexes against rendered page text.
        for label, k in [('Predicted Points Added','ppa'),('Success Rate','success'),('Explosiveness','explosiveness')]:
            m=re.search(label+r'.{0,180}?(-?\d+(?:\.\d+)?%?).{1,40}?(-?\d+(?:\.\d+)?%?)',text,re.I)
            if m:
                out[k]=(num(m.group(1)),num(m.group(2)))
        return out
    except Exception as e:
        return {'ok':False,'url':url,'source':'CollegeFootballData.com','error':type(e).__name__}

# ---------- Sports-Reference ----------
@st.cache_data(ttl=1800, show_spinner=False)
def sr_team(team, season):
    sl=slug(team); url=f'https://www.sports-reference.com/cfb/schools/{sl}/{season}.html'
    alt=f'https://www.sports-reference.com/cfb/schools/{sl}/{season}-schedule.html'
    try:
        try: html=fetch(url)
        except Exception: html=fetch(alt); url=alt
        soup=BeautifulSoup(html,'html.parser'); text=soup.get_text(' ',strip=True)
        out={'ok':True,'url':url,'source':'Sports-Reference'}
        patterns={
            'record':r'Record:\s*(\d+\s*-\s*\d+)', 'points_pg':r'Points/G:\s*([\d.]+)',
            'opp_points_pg':r'Opp Pts/G:\s*([\d.]+)', 'srs':r'SRS:\s*(-?[\d.]+)', 'sos':r'SOS:\s*(-?[\d.]+)'
        }
        for k,p in patterns.items():
            m=re.search(p,text,re.I)
            if m: out[k]=m.group(1) if k=='record' else num(m.group(1))
        return out
    except Exception as e:
        return {'ok':False,'url':url,'source':'Sports-Reference','error':type(e).__name__}

@st.cache_data(ttl=1800, show_spinner=False)
def sr_player_tables(season):
    result={}
    urls={
        'passing':f'https://www.sports-reference.com/cfb/years/{season}-passing.html',
        'rushing':f'https://www.sports-reference.com/cfb/years/{season}-rushing.html',
    }
    for kind,url in urls.items():
        try:
            tabs=[flat_cols(t) for t in all_tables(fetch(url))]
            chosen=pd.DataFrame()
            for t in tabs:
                cols=' '.join(t.columns).lower()
                if 'player' in cols and 'team' in cols and ('yds' in cols or 'y/g' in cols): chosen=t; break
            result[kind]={'ok':not chosen.empty,'df':chosen,'url':url}
        except Exception as e:
            result[kind]={'ok':False,'df':pd.DataFrame(),'url':url,'error':type(e).__name__}
    return result

# ---------- Game on Paper ----------
@st.cache_data(ttl=1800, show_spinner=False)
def gop_team(team_id, team, season):
    url=f'https://gameonpaper.com/year/{season}/team/{team_id}'
    try:
        html=fetch(url); soup=BeautifulSoup(html,'html.parser'); text=soup.get_text(' ',strip=True)
        out={'ok':True,'url':url,'source':'Game on Paper'}
        # Team summary metrics; tolerate naming variations.
        rx={
            'net_adj_epa':r'Net Adj EPA/Play[^-\d]{0,20}(-?\d+\.\d+)',
            'off_adj_epa':r'Off Adj EPA/Play[^-\d]{0,20}(-?\d+\.\d+)',
            'def_adj_epa':r'Def Adj EPA/Play[^-\d]{0,20}(-?\d+\.\d+)',
        }
        for k,p in rx.items():
            m=re.search(p,text,re.I)
            if m: out[k]=num(m.group(1))
        # Parse player tables for later matching.
        out['tables']=[flat_cols(t) for t in all_tables(html)]
        return out
    except Exception as e:
        return {'ok':False,'url':url,'source':'Game on Paper','error':type(e).__name__,'tables':[]}

# ---------- Winsipedia ----------
@st.cache_data(ttl=86400, show_spinner=False)
def winsipedia(a,b):
    ua,ub=slug(a),slug(b); url=f'https://www.winsipedia.com/{ua}/vs/{ub}'
    try:
        html=fetch(url); soup=BeautifulSoup(html,'html.parser'); text=' '.join(soup.stripped_strings)
        out={'ok':True,'url':url,'source':'Winsipedia'}
        m=re.search(r'(\d+)\s+WINS\s*\([^)]*\).*?(\d+)\s+WINS\s*\([^)]*\)',text,re.I)
        if m: out['a_wins'],out['b_wins']=int(m.group(1)),int(m.group(2))
        s=re.search(r'CURRENT WIN STREAK\s+(\d+)\s*[•\-]\s*([A-Z .()&\-]+)',text,re.I)
        if s: out['streak_n'],out['streak_team']=int(s.group(1)),s.group(2).strip().title()
        return out
    except Exception as e:
        return {'ok':False,'url':url,'source':'Winsipedia','error':type(e).__name__}

# ---------- BCF Toys FEI ----------
@st.cache_data(ttl=1800, show_spinner=False)
def bcf_table(season):
    url=f'https://bcftoys.com/{season}-fei'
    try:
        tabs=[flat_cols(t) for t in all_tables(fetch(url))]
        df=pd.DataFrame()
        for t in tabs:
            cols=' '.join(t.columns).lower()
            if 'team' in cols and 'fei' in cols and len(t)>20: df=t; break
        if df.empty: return {'ok':False,'url':url,'source':'BCF Toys','df':df,'error':'NoTable'}
        return {'ok':True,'url':url,'source':'BCF Toys','df':df}
    except Exception as e:
        return {'ok':False,'url':url,'source':'BCF Toys','df':pd.DataFrame(),'error':type(e).__name__}


def bcf_team(team, season):
    pack=bcf_table(season)
    if not pack['ok']: return {k:v for k,v in pack.items() if k!='df'}
    d=pack['df']; tc=find_col(d,'Team');
    if not tc: return {'ok':False,'url':pack['url'],'source':'BCF Toys','error':'NoTeamColumn'}
    target=norm(team); row=None
    for _,r in d.iterrows():
        if norm(r.get(tc,''))==target: row=r; break
    if row is None: return {'ok':False,'url':pack['url'],'source':'BCF Toys','error':'TeamNotFound'}
    out={'ok':True,'url':pack['url'],'source':'BCF Toys'}
    for key,cands in {'fei':['FEI'],'ofei':['OFEI'],'dfei':['DFEI'],'sfei':['SFEI'],'record':['Rec']}.items():
        c=find_col(d,*cands)
        if c: out[key]=str(row.get(c,'')) if key=='record' else num(row.get(c))
    return out

# ---------- source bundle / team model ----------
@st.cache_data(ttl=1800, show_spinner=False)
def source_bundle(team_id, team, season):
    return {
        'cfbd':cfbd_team(team,season),
        'sr':sr_team(team,season),
        'gop':gop_team(team_id,team,season),
        'bcf':bcf_team(team,season),
    }


def source_status(src):
    return '✓' if src.get('ok') else '—'


def safe_metric(d,k):
    v=d.get(k)
    return v if isinstance(v,(int,float)) else None


def team_score(g):
    season=g['date'].year; H=source_bundle(g['home']['id'],g['home']['name'],season); A=source_bundle(g['away']['id'],g['away']['name'],season)
    h_wp,a_wp=pct(g['home']['record']),pct(g['away']['record'])
    components=[]
    def add(name, hv, av, weight, scale, higher=True, source=''):
        if hv is None or av is None:return
        raw=(hv-av)/scale
        if not higher: raw=-raw
        val=max(-1.5,min(1.5,raw))*weight
        components.append({'name':name,'home_raw':hv,'away_raw':av,'contrib':val,'source':source})
    if h_wp is not None and a_wp is not None: add('Season win rate',h_wp,a_wp,.22,.30,True,'ESPN schedule')
    # rankings: lower number better
    hr,ar=g['home'].get('rank'),g['away'].get('rank')
    if hr and ar: add('AP rank',hr,ar,.07,15,False,'ESPN')
    elif hr and not ar: components.append({'name':'AP rank','home_raw':hr,'away_raw':'NR','contrib':.06,'source':'ESPN'})
    elif ar and not hr: components.append({'name':'AP rank','home_raw':'NR','away_raw':ar,'contrib':-.06,'source':'ESPN'})

    add('SRS',safe_metric(H['sr'],'srs'),safe_metric(A['sr'],'srs'),.16,12,True,'Sports-Reference')
    hmargin=(safe_metric(H['sr'],'points_pg') or 0)-(safe_metric(H['sr'],'opp_points_pg') or 0) if H['sr'].get('ok') and safe_metric(H['sr'],'points_pg') is not None and safe_metric(H['sr'],'opp_points_pg') is not None else None
    amargin=(safe_metric(A['sr'],'points_pg') or 0)-(safe_metric(A['sr'],'opp_points_pg') or 0) if A['sr'].get('ok') and safe_metric(A['sr'],'points_pg') is not None and safe_metric(A['sr'],'opp_points_pg') is not None else None
    add('Scoring margin',hmargin,amargin,.10,18,True,'Sports-Reference')
    add('Adj EPA/play',safe_metric(H['gop'],'net_adj_epa'),safe_metric(A['gop'],'net_adj_epa'),.17,.35,True,'Game on Paper')
    add('FEI',safe_metric(H['bcf'],'fei'),safe_metric(A['bcf'],'fei'),.18,.80,True,'BCF Toys')
    add('OFEI',safe_metric(H['bcf'],'ofei'),safe_metric(A['bcf'],'ofei'),.07,.55,True,'BCF Toys')
    add('DFEI',safe_metric(H['bcf'],'dfei'),safe_metric(A['bcf'],'dfei'),.07,.55,True,'BCF Toys')

    # CFBD public-page metrics. For PPA/Success, first value is offense and second defense when parsed.
    for key,w,scale in [('ppa',.08,.25),('success',.06,12),('explosiveness',.04,.30)]:
        hv=H['cfbd'].get(key); av=A['cfbd'].get(key)
        if isinstance(hv,tuple) and isinstance(av,tuple) and hv[0] is not None and av[0] is not None:
            # offense-vs-opponent-defense matchup differential on both sides
            hmatch=hv[0]-(av[1] if av[1] is not None else av[0]); amatch=av[0]-(hv[1] if hv[1] is not None else hv[0])
            add('CFBD '+key.upper(),hmatch,amatch,w,scale,True,'CollegeFootballData.com')

    # tiny historical signal only; never a major driver
    wip=winsipedia(g['home']['name'],g['away']['name'])
    if wip.get('ok') and wip.get('a_wins') is not None and wip.get('b_wins') is not None:
        total=wip['a_wins']+wip['b_wins']
        if total>=4:
            add('Head-to-head history',wip['a_wins']/total,wip['b_wins']/total,.025,.35,True,'Winsipedia')

    # Home field is deliberately modest.
    components.append({'name':'Home field','home_raw':'home','away_raw':'road','contrib':.085,'source':'Model'})
    z=sum(c['contrib'] for c in components)
    ph=1/(1+math.exp(-2.15*z))
    # shrink toward 50 when source coverage is thin
    source_names={c['source'] for c in components if c['source'] not in ('Model','ESPN schedule','ESPN')}
    coverage=len(source_names)
    shrink={0:.42,1:.60,2:.74,3:.86,4:.94,5:1.0}.get(coverage,1.0)
    ph=.5+(ph-.5)*shrink
    home_pick=ph>=.5; p=ph if home_pick else 1-ph
    pick=g['home'] if home_pick else g['away']; opp=g['away'] if home_pick else g['home']
    grade=('A+' if p>=.80 else 'A' if p>=.74 else 'B+' if p>=.69 else 'B' if p>=.64 else 'C+' if p>=.60 else 'C' if p>=.57 else 'D' if p>=.53 else 'F')
    # Coverage cap prevents a thin-data slate from becoming an A+ pick.
    order=['F','D','C','C+','B','B+','A','A+']
    cap='A+' if coverage>=4 else 'A' if coverage>=3 else 'B+' if coverage>=2 else 'B'
    if order.index(grade)>order.index(cap): grade=cap

    reasons=[]
    ranked=sorted(components,key=lambda c:abs(c['contrib']),reverse=True)
    for c in ranked:
        favors_home=c['contrib']>0
        if (home_pick and favors_home) or ((not home_pick) and not favors_home):
            if c['name']=='Home field': reasons.append('Home-field advantage supports the pick')
            else: reasons.append(f"{c['source']}: {c['name']} comparison favors {pick['name']}")
        if len(reasons)>=5: break
    return {'pick':pick,'opp':opp,'p':p,'grade':grade,'coverage':coverage,'components':components,'home_sources':H,'away_sources':A,'winsipedia':wip,'reasons':reasons}

# ---------- player comparison / prop targets ----------
def team_match(val, team):
    return norm(val)==norm(team) or norm(val).endswith(norm(team)) or norm(team).endswith(norm(val))


def gop_player_metrics(gop, player):
    target=norm(player)
    for t in gop.get('tables',[]):
        if t.empty: continue
        pc=find_col(t,'Player','Name')
        if not pc: continue
        for _,r in t.iterrows():
            if norm(r.get(pc,''))==target:
                epac=find_col(t,'EPA/play','EPA / play','EPA per play'); src=find_col(t,'SR%','Success Rate','SR')
                return {'epa':num(r.get(epac)) if epac else None,'sr':num(r.get(src)) if src else None}
    return {}


def sr_candidates_for_game(g, model_obj):
    season=g['date'].year; packs=sr_player_tables(season); teams=[g['away'],g['home']]
    candidates=[]
    # Passing
    ppack=packs.get('passing',{}); d=ppack.get('df',pd.DataFrame())
    if not d.empty:
        pc=find_col(d,'Player'); tc=find_col(d,'Team'); gc=find_col(d,'G'); ygc=find_col(d,'Y/G'); yc=find_col(d,'Yds'); tdc=find_col(d,'TD'); ratec=find_col(d,'Rate')
        if pc and tc:
            for _,r in d.iterrows():
                tm=next((t for t in teams if team_match(r.get(tc,''),t['name']) or str(r.get(tc,'')).upper()==t.get('abbr','').upper()),None)
                if not tm: continue
                gp=num(r.get(gc)) if gc else games_played(tm.get('record'))
                ypg=num(r.get(ygc)) if ygc else (num(r.get(yc))/gp if gp and num(r.get(yc)) is not None else None)
                if not ypg: continue
                candidates.append({'player':str(r.get(pc,'')).replace('*','').strip(),'team':tm,'market':'QB Passing Yards','base':ypg,'games':gp or 1,
                                   'td':num(r.get(tdc)) if tdc else None,'eff':num(r.get(ratec)) if ratec else None,'sr_url':ppack.get('url')})
    # Rushing/receiving combined table
    rpack=packs.get('rushing',{}); d=rpack.get('df',pd.DataFrame())
    if not d.empty:
        pc=find_col(d,'Player'); tc=find_col(d,'Team'); gc=find_col(d,'G')
        # use exact prefixed columns when MultiIndex flattened
        rushyg=next((c for c in d.columns if 'rushing' in c.lower() and 'y/g' in c.lower()),None)
        recyg=next((c for c in d.columns if 'receiving' in c.lower() and 'y/g' in c.lower()),None)
        rushyds=next((c for c in d.columns if 'rushing' in c.lower() and re.search(r'\byds\b',c.lower())),None)
        recyds=next((c for c in d.columns if 'receiving' in c.lower() and re.search(r'\byds\b',c.lower())),None)
        if pc and tc:
            for _,r in d.iterrows():
                tm=next((t for t in teams if team_match(r.get(tc,''),t['name']) or str(r.get(tc,'')).upper()==t.get('abbr','').upper()),None)
                if not tm: continue
                gp=num(r.get(gc)) if gc else games_played(tm.get('record'))
                ry=num(r.get(rushyg)) if rushyg else (num(r.get(rushyds))/gp if gp and rushyds and num(r.get(rushyds)) is not None else None)
                wy=num(r.get(recyg)) if recyg else (num(r.get(recyds))/gp if gp and recyds and num(r.get(recyds)) is not None else None)
                if ry and ry>=18: candidates.append({'player':str(r.get(pc,'')).replace('*','').strip(),'team':tm,'market':'RB Rushing Yards','base':ry,'games':gp or 1,'sr_url':rpack.get('url')})
                if wy and wy>=18: candidates.append({'player':str(r.get(pc,'')).replace('*','').strip(),'team':tm,'market':'WR/TE Receiving Yards','base':wy,'games':gp or 1,'sr_url':rpack.get('url')})
    # Best one per market after matchup adjustments.
    out=[]
    for c in candidates:
        tm=c['team']; opp=g['home'] if tm['id']==g['away']['id'] else g['away']
        own=model_obj['home_sources'] if tm['id']==g['home']['id'] else model_obj['away_sources']
        opps=model_obj['away_sources'] if tm['id']==g['home']['id'] else model_obj['home_sources']
        adj=1.0; why=[]
        # BCF defense: larger DFEI is better defense -> reduce projection.
        dfei=safe_metric(opps['bcf'],'dfei')
        if dfei is not None:
            delta=max(-.08,min(.08,-dfei*.07)); adj+=delta; why.append(f"BCF opponent DFEI {'suppresses' if delta<0 else 'raises'} projection")
        # Game on Paper player efficiency if available.
        pm=gop_player_metrics(own['gop'],c['player'])
        if pm.get('epa') is not None:
            delta=max(-.05,min(.05,pm['epa']*.06)); adj+=delta; why.append('Game on Paper player EPA confirms efficiency' if delta>=0 else 'Game on Paper player EPA adds caution')
        # Team offense / opponent defense scoring profile from Sports Reference.
        own_ppg=safe_metric(own['sr'],'points_pg'); opp_opp=safe_metric(opps['sr'],'opp_points_pg')
        if own_ppg is not None and opp_opp is not None:
            delta=max(-.05,min(.05,((own_ppg+opp_opp)/2-28)/100)); adj+=delta; why.append('Sports-Reference scoring environment adjustment')
        proj=max(0.0,c['base']*adj); target=max(.5,round((proj*.90)*2)/2)
        src_count=1 + int(pm.get('epa') is not None) + int(dfei is not None) + int(own_ppg is not None and opp_opp is not None)
        sample=min((c['games'] or 1)/5,1); cushion=(proj-target)/max(target,1)
        conf=.52*sample+.28*min(cushion/.15,1)+.20*min(src_count/4,1)
        grade=('A+' if conf>=.90 else 'A' if conf>=.82 else 'B+' if conf>=.74 else 'B' if conf>=.66 else 'C+' if conf>=.58 else 'C' if conf>=.50 else 'D' if conf>=.42 else 'F')
        c.update({'projection':proj,'target_over':target,'grade':grade,'why':why,'source_count':src_count,'gop':pm})
        out.append(c)
    best=[]
    for market in ['QB Passing Yards','RB Rushing Yards','WR/TE Receiving Yards']:
        rows=[x for x in out if x['market']==market]
        if rows: best.append(max(rows,key=lambda x:(x['grade'] in ('A+','A','B+'),x['projection'])))
    return best


def logo_html(t):
    return f"<img class='logo' src='{t['logo']}'/>" if t.get('logo') else '<span>[LOGO]</span>'


def compare_table(g,m):
    rows=[]
    hs=m['home_sources']; as_=m['away_sources']
    def add(source, metric, av, hv):
        if av is None and hv is None:return
        rows.append({'Source':source,'Metric':metric,g['away']['name']:av if av is not None else '—',g['home']['name']:hv if hv is not None else '—'})
    add('Sports-Reference','SRS',safe_metric(as_['sr'],'srs'),safe_metric(hs['sr'],'srs'))
    add('Sports-Reference','Pts/Game',safe_metric(as_['sr'],'points_pg'),safe_metric(hs['sr'],'points_pg'))
    add('Sports-Reference','Opp Pts/Game',safe_metric(as_['sr'],'opp_points_pg'),safe_metric(hs['sr'],'opp_points_pg'))
    add('Game on Paper','Net Adj EPA/Play',safe_metric(as_['gop'],'net_adj_epa'),safe_metric(hs['gop'],'net_adj_epa'))
    add('BCF Toys','FEI',safe_metric(as_['bcf'],'fei'),safe_metric(hs['bcf'],'fei'))
    add('BCF Toys','OFEI',safe_metric(as_['bcf'],'ofei'),safe_metric(hs['bcf'],'ofei'))
    add('BCF Toys','DFEI',safe_metric(as_['bcf'],'dfei'),safe_metric(hs['bcf'],'dfei'))
    for label,key in [('PPA','ppa'),('Success Rate','success'),('Explosiveness','explosiveness')]:
        av=as_['cfbd'].get(key); hv=hs['cfbd'].get(key)
        if isinstance(av,tuple) or isinstance(hv,tuple):
            add('CollegeFootballData.com',label+' Off',av[0] if isinstance(av,tuple) else None,hv[0] if isinstance(hv,tuple) else None)
            add('CollegeFootballData.com',label+' Def',av[1] if isinstance(av,tuple) and len(av)>1 else None,hv[1] if isinstance(hv,tuple) and len(hv)>1 else None)
    if rows: st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
    else: st.caption('No comparison tables were available from the requested sites for this matchup.')


def source_badges(m):
    checks=[]
    for key,label in [('cfbd','CollegeFootballData'),('sr','Sports-Reference'),('gop','Game on Paper'),('bcf','BCF Toys')]:
        ok=m['home_sources'][key].get('ok') or m['away_sources'][key].get('ok')
        checks.append(f"{'✓' if ok else '—'} {label}")
    checks.append(f"{'✓' if m['winsipedia'].get('ok') else '—'} Winsipedia")
    st.caption(' • '.join(checks))


def prop_cards(g,m):
    st.markdown("<div class='rule'></div><div class='section-title'>⭐ &nbsp;BEST PLAYER PROPS</div>",unsafe_allow_html=True)
    props=sr_candidates_for_game(g,m)
    if not props:
        st.markdown("<div class='mono warn'>PLAYER COMPARISON DATA NOT AVAILABLE — no player or line was fabricated.</div>",unsafe_allow_html=True); return
    for p in props:
        wh='; '.join(p['why'][:3]) if p['why'] else 'Sports-Reference season production provides the base projection.'
        st.markdown(f"<div class='propbox mono'><b>{p['market']}</b><br>Player&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['player']} ({p['team']['abbr']})</b><br>"
                    f"Projection&nbsp;&nbsp; <b>{p['projection']:.1f}</b><br>Model target <b>OVER {p['target_over']:.1f} or lower</b><br>Strength&nbsp;&nbsp;&nbsp;&nbsp; <b>{p['grade']}</b><br>"
                    f"<span class='muted'>Base: {p['base']:.1f}/game • Sources used: {p['source_count']} • {wh}. Target is a model threshold, not a sportsbook line.</span></div>",unsafe_allow_html=True)


def game_card(g):
    m=team_score(g)
    st.markdown("<div class='game-card'>",unsafe_allow_html=True)
    st.markdown(f"<div class='teamline'>{g['away']['name'].upper()} {logo_html(g['away'])} &nbsp;&nbsp;&nbsp; {logo_html(g['home'])} {g['home']['name'].upper()}</div>",unsafe_allow_html=True)
    st.markdown(f"<div class='kick'>{g['date'].strftime('%A • %B %d, %Y • %I:%M %p ET')} &nbsp; • &nbsp; {g['division']}</div>",unsafe_allow_html=True)
    source_badges(m)
    cls='good' if m['grade'] in ('A+','A','B+') else ('warn' if m['grade'] in ('B','C+','C') else 'bad')
    st.markdown("<div class='section-title'>🤖 &nbsp;MONEYLINE MODEL</div>",unsafe_allow_html=True)
    st.markdown(f"<div class='pick-name'>{m['pick']['name']} ML &nbsp; <span class='{cls}'>[{m['grade']}]</span></div><br>",unsafe_allow_html=True)
    st.markdown(f"<div class='mono'>Win Probability&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{m['p']*100:.0f}%</b><br>Model Edge&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>N/A until verified MLs</b><br>Pick Strength&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{m['grade']}</b><br>Independent sources&nbsp;&nbsp; <b>{m['coverage']}</b></div>",unsafe_allow_html=True)
    st.markdown(f"<br><div class='section-title'>WHY {m['pick']['name'].upper()}?</div>",unsafe_allow_html=True)
    st.markdown("<div class='mono'>"+''.join('✓ '+r+'<br>' for r in m['reasons'])+"</div>",unsafe_allow_html=True)
    with st.expander('📊 MULTI-SOURCE TEAM COMPARISON'):
        compare_table(g,m)
        if m['winsipedia'].get('ok'):
            w=m['winsipedia'];
            if w.get('a_wins') is not None: st.write(f"Winsipedia series: **{g['home']['name']} {w['a_wins']} – {w['b_wins']} {g['away']['name']}** (historical context only; low model weight).")
        st.caption('Requested comparison sites are used when their public pages are reachable. Missing/blocked sources are skipped rather than fabricated.')
    prop_cards(g,m)
    st.markdown('</div>',unsafe_allow_html=True)
    with st.expander('💵 VERIFIED MONEYLINE + PLAYER PROP GRADER'):
        x,y=st.columns(2); away=x.number_input(f"{g['away']['abbr'] or 'Away'} ML",value=0,step=5,key='a'+g['id']); home=y.number_input(f"{g['home']['abbr'] or 'Home'} ML",value=0,step=5,key='h'+g['id'])
        if away and home:
            def american(x): x=float(x); return 100/(x+100) if x>0 else (-x)/((-x)+100)
            pa,ph=american(away),american(home); s=pa+ph; pa,ph=pa/s,ph/s; market=ph if m['pick']['id']==g['home']['id'] else pa; edge=m['p']-market
            grade=('A+' if edge>=.10 else 'A' if edge>=.075 else 'B+' if edge>=.055 else 'B' if edge>=.035 else 'C+' if edge>=.020 else 'C' if edge>=.010 else 'D' if edge>=0 else 'F')
            st.metric('Verified-line Model Edge',f'{edge*100:+.1f}%'); st.write(f'Market no-vig probability: **{market*100:.1f}%** • Edge grade: **{grade}**')
        p1,p2,p3=st.columns(3); player=p1.text_input('Player',key='p'+g['id']); market_name=p2.selectbox('Market',['Passing Yards','Rushing Yards','Receiving Yards','Receptions','Passing TDs','Anytime TD'],key='m'+g['id']); line=p3.number_input('Verified sportsbook line',min_value=0.0,value=0.0,step=.5,key='l'+g['id'])
        q1,q2=st.columns(2); proj=q1.number_input('Model projection',min_value=0.0,value=0.0,step=.5,key='pr'+g['id']); side=q2.selectbox('Side',['OVER','UNDER'],key='s'+g['id'])
        if player and line>0 and proj>0:
            raw=(proj-line) if side=='OVER' else (line-proj); rel=raw/max(line,1); strength=('A+' if rel>=.18 else 'A' if rel>=.14 else 'B+' if rel>=.10 else 'B' if rel>=.07 else 'C+' if rel>=.05 else 'C' if rel>=.03 else 'D' if rel>=.015 else 'F')
            st.write(f'**{player} — {side} {line:g} {market_name}** • Projection **{proj:g}** • Edge **{raw:+.1f}** • Strength **{strength}**')

# ---------- UI ----------
st.title('🏈 NCAAF MULTI-SOURCE EDGE SCANNER')
st.caption('Hands-free schedule scan • team + player comparisons • CollegeFootballData.com • Sports-Reference • Game on Paper • Winsipedia • BCF Toys')
st.caption('ESPN is used only for upcoming schedule/team-logo discovery. No API keys are required.')

config_key=(datetime.now(ET).date().isoformat(),SCAN_AHEAD_DAYS,tuple(SELECTED_DIVISIONS))
if st.session_state.get('scan_config')!=config_key or 'games' not in st.session_state:
    with st.spinner('Finding upcoming games, then comparing teams and players across the requested public sites...'):
        gs,errs=scan_dates(datetime.now(ET).date(),SCAN_AHEAD_DAYS,SELECTED_DIVISIONS)
        st.session_state.games=gs; st.session_state.errors=errs; st.session_state.scanned=datetime.now(ET); st.session_state.scan_config=config_key

games=st.session_state.get('games',[])
if not games:
    st.warning('No upcoming games were parsed in the 7-day window. No games, players, or betting lines were fabricated.')
else:
    # Team models are evaluated lazily here; cached source reads keep later cards fast.
    with st.spinner('Scoring the upcoming slate with multi-source comparisons...'):
        modeled=[]
        for g in games:
            try: modeled.append((g,team_score(g)))
            except Exception: modeled.append((g,None))
    anchors=[x for x in modeled if x[1] and x[1]['grade']=='A+']
    strong=[x for x in modeled if x[1] and x[1]['grade'] in ('A','B+')]
    others=[x for x in modeled if not x[1] or x[1]['grade'] not in ('A+','A','B+')]
    c1,c2,c3,c4=st.columns(4); c1.metric('Upcoming Games',len(games)); c2.metric('A+ Anchors',len(anchors)); c3.metric('A / B+ Strong',len(strong)); c4.metric('B to F',len(others))
    st.success(f"Last scan: {st.session_state.scanned.strftime('%m/%d/%y %I:%M %p ET')}")
    st.subheader('⚓ A+ ANCHOR PICKS')
    st.caption('A+ requires both high modeled win probability and broad source coverage. It is not a guarantee.')
    if anchors:
        for g,m in sorted(anchors,key=lambda x:x[1]['p'],reverse=True): game_card(g)
    else: st.info('No A+ picks met the multi-source coverage threshold.')
    st.subheader('💪 A / B+ STRONG PICKS')
    if strong:
        for g,m in sorted(strong,key=lambda x:x[1]['p'],reverse=True): game_card(g)
    else: st.info('No A or B+ picks were found.')
    with st.expander(f'📋 B THROUGH F UPCOMING GAMES ({len(others)})'):
        for g,m in sorted(others,key=lambda x:x[1]['p'] if x[1] else 0,reverse=True):
            if m: game_card(g)
            else: st.write(g['away']['name'],'@',g['home']['name'],'— source comparison failed')

errors=st.session_state.get('errors',[])
if errors:
    with st.expander(f'⚠️ Schedule source warnings ({len(errors)})'):
        st.code('\n'.join(errors[:50]))

st.divider()
st.caption('Data integrity: every external site is optional at runtime. If a public page is blocked, rate-limited, changed, or unavailable, that source is marked missing and its weight is removed. The app does not invent source values, sportsbook lines, players, or edges. Winsipedia history is intentionally low-weight; current-season efficiency sources carry more weight.')
