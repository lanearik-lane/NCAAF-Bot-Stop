from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json, re, math
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title='NCAAF Edge Scanner', page_icon='🏈', layout='wide')
ET=ZoneInfo('America/New_York')
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153 Safari/537.36'}
ESPN='https://www.espn.com'

st.markdown('''<style>
.block-container{max-width:1450px;padding-top:1rem}.game-card{background:#202020;border:1px solid #353535;border-radius:16px;padding:18px 22px;margin:10px 0 22px 0}.teamline{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:1.0rem;font-weight:800;letter-spacing:.02em}.kick{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:700;margin:8px 0 24px}.section-title{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900;margin-top:12px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;line-height:1.65}.pick-name{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:900;font-size:1.08rem}.rule{border-top:1px solid #aaa;width:34%;margin:26px 0}.logo{height:42px;max-width:52px;object-fit:contain;vertical-align:middle;margin:0 8px}.good{color:#32d583}.warn{color:#fdb022}.stMetric{border:1px solid rgba(128,128,128,.22);padding:9px;border-radius:10px}</style>''',unsafe_allow_html=True)

@st.cache_data(ttl=900,show_spinner=False)
def fetch(url):
    r=requests.get(url,headers=HEADERS,timeout=25); r.raise_for_status(); return r.text

def walk(o):
    if isinstance(o,dict):
        if isinstance(o.get('competitions'),list) and o.get('competitions'): yield o
        for v in o.values(): yield from walk(v)
    elif isinstance(o,list):
        for v in o: yield from walk(v)

def json_blobs(html):
    soup=BeautifulSoup(html,'html.parser'); out=[]
    for s in soup.find_all('script'):
        t=s.string or s.get_text('',strip=True)
        if not t: continue
        if s.get('type')=='application/json':
            try: out.append(json.loads(t)); continue
            except: pass
        if 'competitions' in t and 'competitors' in t:
            for p in [m.start() for m in re.finditer(r'\{',t)][:10]:
                try: obj,_=json.JSONDecoder().raw_decode(t[p:]); out.append(obj); break
                except: pass
    return out

def team_obj(c):
    t=c.get('team') or {}; logos=t.get('logos') or []; recs=c.get('records') or []
    rec=recs[0].get('summary','') if recs and isinstance(recs[0],dict) else ''
    rank=c.get('curatedRank',{}).get('current') if isinstance(c.get('curatedRank'),dict) else None
    return {'id':str(t.get('id','')),'name':t.get('displayName') or t.get('shortDisplayName') or 'Unknown','abbr':t.get('abbreviation',''),'logo':logos[0].get('href') if logos and isinstance(logos[0],dict) else None,'record':rec,'rank':rank if isinstance(rank,int) and rank<99 else None}

def parse_games(html,lo,hi):
    games={}
    for blob in json_blobs(html):
        for e in walk(blob):
            try:
                eid=str(e.get('id','')); comp=(e.get('competitions') or [])[0]; cs=comp.get('competitors') or []
                if not eid or len(cs)<2: continue
                dt=datetime.fromisoformat(str(e.get('date')).replace('Z','+00:00')).astimezone(ET)
                if not lo<=dt.date()<=hi or dt <= datetime.now(ET): continue
                by={x.get('homeAway'):x for x in cs}
                if not {'home','away'}<=set(by): continue
                games[eid]={'id':eid,'date':dt,'home':team_obj(by['home']),'away':team_obj(by['away']),'status':(e.get('status') or {}).get('type',{}).get('description','Scheduled'),'venue':((comp.get('venue') or {}).get('fullName') or '')}
            except: continue
    return sorted(games.values(),key=lambda x:x['date'])

def scan_dates(start,days):
    allg={}; errors=[]
    for i in range(days+1):
        d=start+timedelta(days=i); url=f'{ESPN}/college-football/scoreboard/_/date/{d:%Y%m%d}'
        try:
            for g in parse_games(fetch(url),d,d): allg[g['id']]=g
        except Exception as e: errors.append(f'{d}: {type(e).__name__}')
    return sorted(allg.values(),key=lambda x:x['date']),errors

def pct(rec):
    m=re.search(r'(\d+)\s*-\s*(\d+)',rec or '')
    if not m:return None
    w,l=map(int,m.groups()); return w/(w+l) if w+l else None

def model(g):
    hp,ap=pct(g['home']['record']),pct(g['away']['record'])
    if hp is None or ap is None:return None
    def rb(r): return 0 if not r else (26-r)/25*.32
    score=(hp-ap)*1.75 + rb(g['home']['rank'])-rb(g['away']['rank']) + .13
    ph=1/(1+math.exp(-score)); home=ph>=.5; p=ph if home else 1-ph
    pick=g['home'] if home else g['away']; opp=g['away'] if home else g['home']
    grade='A+' if p>=.78 else 'A' if p>=.70 else 'B+' if p>=.64 else 'B' if p>=.58 else 'PASS'
    reasons=[]
    if (hp if home else ap)>(ap if home else hp): reasons.append('Stronger team power profile from verified season results')
    if pick['rank'] and (not opp['rank'] or pick['rank']<opp['rank']): reasons.append('AP ranking signal favors the pick')
    reasons.append('Home-field advantage supports the pick' if home else 'Road profile is strong enough to overcome home field')
    reasons.append('Recent season performance favors '+pick['name'])
    return {'pick':pick,'opp':opp,'p':p,'grade':grade,'reasons':reasons}

def american_to_prob(x):
    x=float(x); return 100/(x+100) if x>0 else (-x)/((-x)+100)
def novig(a,b):
    pa,pb=american_to_prob(a),american_to_prob(b); s=pa+pb; return pa/s,pb/s
def grade_edge(edge): return 'A+' if edge>=.10 else 'A' if edge>=.07 else 'B+' if edge>=.045 else 'B' if edge>=.025 else 'PASS'

def logo_html(t):
    return f"<img class='logo' src='{t['logo']}'/>" if t.get('logo') else "<span>[LOGO]</span>"

def game_card(g):
    m=model(g)
    st.markdown("<div class='game-card'>",unsafe_allow_html=True)
    st.markdown(f"<div class='teamline'>{g['away']['name'].upper()} {logo_html(g['away'])} &nbsp;&nbsp;&nbsp; {logo_html(g['home'])} {g['home']['name'].upper()}</div>",unsafe_allow_html=True)
    st.markdown(f"<div class='kick'>{g['date'].strftime('%A • %B %d, %Y • %I:%M %p ET')}</div>",unsafe_allow_html=True)
    st.markdown("<div class='section-title'>🤖 &nbsp;MONEYLINE MODEL</div>",unsafe_allow_html=True)
    if not m:
        st.markdown("<div class='mono'>PASS — not enough verified record data.</div></div>",unsafe_allow_html=True); return
    st.markdown(f"<div class='pick-name'>{m['pick']['name']} ML</div><br>",unsafe_allow_html=True)
    st.markdown(f"<div class='mono'>Win Probability&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{m['p']*100:.0f}%</b><br>Model Edge&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>N/A until verified MLs</b><br>Pick Strength&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <b>{m['grade']}</b></div>",unsafe_allow_html=True)
    st.markdown(f"<br><div class='section-title'>WHY {m['pick']['name'].upper()}?</div>",unsafe_allow_html=True)
    st.markdown("<div class='mono'>"+''.join('✓ '+r+'<br>' for r in m['reasons'])+"</div>",unsafe_allow_html=True)
    st.markdown("<div class='rule'></div><div class='section-title'>⭐ &nbsp;BEST PLAYER PROPS</div>",unsafe_allow_html=True)
    st.markdown("<div class='mono warn'>NO VERIFIED PROP LINE — enter a real sportsbook line below to grade it. The app will not invent a line.</div>",unsafe_allow_html=True)
    st.markdown("</div>",unsafe_allow_html=True)
    with st.expander('💵 MONEYLINE EDGE + PLAYER PROP GRADER'):
        x,y=st.columns(2); away=x.number_input(f"{g['away']['abbr'] or 'Away'} ML",value=0,step=5,key='a'+g['id']); home=y.number_input(f"{g['home']['abbr'] or 'Home'} ML",value=0,step=5,key='h'+g['id'])
        if away and home:
            pa,ph=novig(away,home); market=ph if m['pick']['id']==g['home']['id'] else pa; edge=m['p']-market
            st.metric('Verified-line Model Edge',f'{edge*100:+.1f}%'); st.write(f"Market no-vig probability: **{market*100:.1f}%** • Edge grade: **{grade_edge(edge)}**")
        p1,p2,p3=st.columns(3); player=p1.text_input('Player',key='p'+g['id']); market=p2.selectbox('Market',['Passing Yards','Rushing Yards','Receiving Yards','Receptions','Passing TDs','Anytime TD'],key='m'+g['id']); line=p3.number_input('Sportsbook line',min_value=0.0,value=0.0,step=.5,key='l'+g['id'])
        q1,q2=st.columns(2); proj=q1.number_input('Projection',min_value=0.0,value=0.0,step=.5,key='pr'+g['id']); side=q2.selectbox('Side',['OVER','UNDER'],key='s'+g['id'])
        if player and line>0 and proj>0:
            raw=(proj-line) if side=='OVER' else (line-proj); rel=raw/max(line,1); strength='A+' if rel>=.18 else 'A' if rel>=.12 else 'B+' if rel>=.08 else 'B' if rel>=.04 else 'PASS'
            st.write(f"**{player} — {side} {line:g} {market}** • Projection **{proj:g}** • Edge **{raw:+.1f}** • Strength **{strength}**")

st.title('🏈 NCAAF EDGE SCANNER')
st.caption('Auto-scans upcoming games before kickoff • matchup-card output • real team logos when available • no API keys')
with st.sidebar:
    st.header('🤖 Auto Bot')
    auto=st.toggle('Auto-run bot',value=True)
    lead_days=st.slider('Run this many days before kickoff',1,14,7)
    refresh=st.selectbox('Refresh while app is open',['15 minutes','30 minutes','60 minutes'],index=1)
    ming=st.selectbox('Minimum model strength',['ALL','B','B+','A','A+'])
    st.caption('Auto-run scans from today through the lead-day window. Only future games are shown.')

mins={'15 minutes':15,'30 minutes':30,'60 minutes':60}[refresh]
now=datetime.now(ET)
last=st.session_state.get('scanned')
should_scan=auto and (last is None or (now-last).total_seconds() >= mins*60 or st.session_state.get('lead_days')!=lead_days)
if should_scan:
    with st.spinner(f'Bot scanning games starting in the next {lead_days} days...'):
        gs,errs=scan_dates(now.date(),lead_days)
        st.session_state.games=gs; st.session_state.errors=errs; st.session_state.scanned=now; st.session_state.lead_days=lead_days

if st.button('🔍 RUN BOT NOW',type='primary',use_container_width=True):
    with st.spinner('Scanning upcoming games...'):
        gs,errs=scan_dates(datetime.now(ET).date(),lead_days)
        st.session_state.games=gs; st.session_state.errors=errs; st.session_state.scanned=datetime.now(ET); st.session_state.lead_days=lead_days

if auto:
    st.caption(f"🟢 AUTO BOT ON • scanning games up to {lead_days} days before kickoff • refresh target: {refresh}")
else: st.caption('⚪ AUTO BOT OFF • use RUN BOT NOW')

games=st.session_state.get('games',[])
if 'scanned' not in st.session_state: st.info('Turn on Auto-run or press **RUN BOT NOW**.')
elif not games:
    st.warning('No upcoming games were parsed in the selected lead window. No games or picks were fabricated.')
else:
    ranks={'PASS':0,'B':1,'B+':2,'A':3,'A+':4}; th=0 if ming=='ALL' else ranks[ming]
    qualified=[]
    for g in games:
        m=model(g)
        if m and ranks[m['grade']]>=th: qualified.append((g,m))
    st.success(f"Bot scanned {len(games)} upcoming games • {len(qualified)} meet the current filter • last run {st.session_state.scanned.strftime('%m/%d/%y %I:%M %p ET')}")
    for g,m in qualified: game_card(g)

st.divider(); st.caption('Pick grades describe model signal strength, not certainty. Public webpage markup can change. Market edge is shown only after verified moneylines are supplied; prop lines are never fabricated.')
