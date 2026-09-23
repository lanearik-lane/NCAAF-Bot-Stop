from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json, re, math
from urllib.parse import urljoin
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title='NCAAF Edge Scanner', page_icon='🏈', layout='wide')
ET=ZoneInfo('America/New_York')
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153 Safari/537.36'}
ESPN='https://www.espn.com'

st.markdown('''<style>.block-container{max-width:1450px;padding-top:1.2rem}.hero{padding:18px;border:1px solid #30363d;border-radius:18px}.pick{font-size:1.45rem;font-weight:850}.good{color:#19c37d}.bad{color:#ff5c5c}.small{opacity:.72}.stMetric{border:1px solid rgba(128,128,128,.25);padding:10px;border-radius:12px}</style>''',unsafe_allow_html=True)

@st.cache_data(ttl=600,show_spinner=False)
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
        # ESPN hydration payloads may contain a JSON object after an assignment.
        if 'competitions' in t and 'competitors' in t:
            starts=[m.start() for m in re.finditer(r'\{',t)]
            for p in starts[:8]:
                try:
                    dec=json.JSONDecoder(); obj,_=dec.raw_decode(t[p:]); out.append(obj); break
                except: pass
    return out

def team_obj(c):
    t=c.get('team') or {}; logos=t.get('logos') or []
    recs=c.get('records') or []
    rec=''
    if recs and isinstance(recs[0],dict): rec=recs[0].get('summary','')
    rank=c.get('curatedRank',{}).get('current') if isinstance(c.get('curatedRank'),dict) else None
    return {'id':str(t.get('id','')),'name':t.get('displayName') or t.get('shortDisplayName') or 'Unknown','abbr':t.get('abbreviation',''),
            'logo':logos[0].get('href') if logos and isinstance(logos[0],dict) else None,'record':rec,'rank':rank if isinstance(rank,int) and rank<99 else None}

def parse_games(html,lo,hi):
    games={}
    for blob in json_blobs(html):
        for e in walk(blob):
            try:
                eid=str(e.get('id','')); comp=(e.get('competitions') or [])[0]; cs=comp.get('competitors') or []
                if not eid or len(cs)<2: continue
                dt=datetime.fromisoformat(str(e.get('date')).replace('Z','+00:00')).astimezone(ET)
                if not lo<=dt.date()<=hi: continue
                by={x.get('homeAway'):x for x in cs}
                if not {'home','away'}<=set(by): continue
                games[eid]={'id':eid,'date':dt,'home':team_obj(by['home']),'away':team_obj(by['away']),
                    'status':(e.get('status') or {}).get('type',{}).get('description','Scheduled'),
                    'venue':((comp.get('venue') or {}).get('fullName') or ''),'link':f'{ESPN}/college-football/game/_/gameId/{eid}'}
            except: continue
    return sorted(games.values(),key=lambda x:x['date'])

def scan_dates(start,days):
    allg={}; errors=[]
    # ESPN date pages are ordinary public webpages; no API/key is used.
    for i in range(days):
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
    # Transparent baseline: record strength + AP-rank signal + modest home field.
    def rank_bonus(r): return 0 if not r else (26-r)/25*.32
    score=(hp-ap)*1.75 + rank_bonus(g['home']['rank'])-rank_bonus(g['away']['rank']) + .13
    ph=1/(1+math.exp(-score)); home=ph>=.5; p=ph if home else 1-ph
    pick=g['home'] if home else g['away']; opp=g['away'] if home else g['home']
    grade='A+' if p>=.78 else 'A' if p>=.70 else 'B+' if p>=.64 else 'B' if p>=.58 else 'PASS'
    reasons=[]
    if (hp if home else ap)>(ap if home else hp): reasons.append('Better verified season win rate')
    pr,orr=pick['rank'],opp['rank']
    if pr and (not orr or pr<orr): reasons.append('AP ranking signal favors the pick')
    reasons.append('Home-field adjustment favors the pick' if home else 'Road profile is strong enough to overcome home field')
    return {'pick':pick,'opp':opp,'p':p,'grade':grade,'reasons':reasons}

def american_to_prob(x):
    x=float(x); return 100/(x+100) if x>0 else (-x)/((-x)+100)

def novig(a,b):
    pa,pb=american_to_prob(a),american_to_prob(b); s=pa+pb; return pa/s,pb/s

def grade_edge(edge):
    return 'A+' if edge>=.10 else 'A' if edge>=.07 else 'B+' if edge>=.045 else 'B' if edge>=.025 else 'PASS'

def game_card(g):
    m=model(g); st.markdown(f"### {g['away']['name']}  @  {g['home']['name']}")
    c1,c2=st.columns(2)
    for col,t in [(c1,g['away']),(c2,g['home'])]:
        with col:
            if t['logo']: st.image(t['logo'],width=68)
            st.write(f"**{t['name']}**  {t['record'] or 'Record N/A'}" + (f" • AP #{t['rank']}" if t['rank'] else ''))
    st.caption(g['date'].strftime('%A • %B %d, %Y • %I:%M %p ET') + (f" • {g['venue']}" if g['venue'] else ''))
    st.markdown('#### 🤖 MONEYLINE MODEL')
    if not m: st.warning('PASS — not enough verified record data.'); return
    st.markdown(f"<div class='pick'>{m['pick']['name']} ML</div>",unsafe_allow_html=True)
    a,b,c=st.columns(3); a.metric('Model Win Probability',f"{m['p']*100:.1f}%"); b.metric('Pick Strength',m['grade']); c.metric('Market Edge','N/A')
    st.markdown('**WHY THIS MONEYLINE?**'); [st.write('✓ '+r) for r in m['reasons']]
    with st.expander('💵 Add current moneylines → calculate true no-vig edge'):
        x,y=st.columns(2); away=x.number_input(f"{g['away']['abbr'] or 'Away'} ML",value=0,step=5,key='a'+g['id']); home=y.number_input(f"{g['home']['abbr'] or 'Home'} ML",value=0,step=5,key='h'+g['id'])
        if away and home:
            pa,ph=novig(away,home); market=ph if m['pick']['id']==g['home']['id'] else pa; edge=m['p']-market
            st.metric('Verified-line Model Edge',f'{edge*100:+.1f}%'); st.write(f"Market no-vig probability: **{market*100:.1f}%** • Edge grade: **{grade_edge(edge)}**")
    st.markdown('#### ⭐ BEST PLAYER PROPS')
    st.info('Automatic sportsbook prop lines are not fabricated. Enter a real line below and the app will grade it against your projection.')
    with st.expander('Player prop grader'):
        p1,p2,p3=st.columns(3); player=p1.text_input('Player',key='p'+g['id']); market=p2.selectbox('Market',['Passing Yards','Rushing Yards','Receiving Yards','Receptions','Passing TDs','Anytime TD'],key='m'+g['id']); line=p3.number_input('Sportsbook line',min_value=0.0,value=0.0,step=.5,key='l'+g['id'])
        q1,q2=st.columns(2); proj=q1.number_input('Your/model projection',min_value=0.0,value=0.0,step=.5,key='pr'+g['id']); side=q2.selectbox('Side',['OVER','UNDER'],key='s'+g['id'])
        if player and line>0 and proj>0:
            raw=(proj-line) if side=='OVER' else (line-proj); rel=raw/max(line,1); strength='A+' if rel>=.18 else 'A' if rel>=.12 else 'B+' if rel>=.08 else 'B' if rel>=.04 else 'PASS'
            st.write(f"**{player} — {side} {line:g} {market}**")
            st.write(f"Projection: **{proj:g}** • Projection edge: **{raw:+.1f}** • Strength: **{strength}**")
    st.markdown('#### 🚫 PASS'); st.caption('Any market without enough verified data or a real line stays PASS.')
    st.caption('Source: ESPN public scoreboard page • No API key • '+datetime.now(ET).strftime('Updated %m/%d/%y %I:%M %p ET'))

st.title('🏈 NCAAF EDGE SCANNER')
st.caption('Upcoming college-football scanner • real team logos • transparent moneyline model • no API keys')
with st.sidebar:
    st.header('Scanner Controls'); start=st.date_input('Start date',datetime.now(ET).date()); days=st.slider('Scan upcoming days',1,14,7)
    ming=st.selectbox('Minimum model strength',['ALL','B','B+','A','A+']); st.caption('Public ESPN pages are scanned by date. Website markup can change, so the app fails closed instead of inventing data.')
if st.button('🔍 SCAN UPCOMING GAMES',type='primary',use_container_width=True):
    with st.spinner('Scanning upcoming public scoreboard pages...'):
        gs,errs=scan_dates(start,days); st.session_state.games=gs; st.session_state.errors=errs; st.session_state.scanned=datetime.now(ET)

games=st.session_state.get('games',[])
if 'scanned' not in st.session_state: st.info('Select a date range and press **SCAN UPCOMING GAMES**.')
elif not games:
    st.error('No games could be parsed from the public pages. ESPN may have changed its page markup or blocked the request. No games or picks were fabricated.')
    if st.session_state.get('errors'): st.caption('Scan diagnostics: '+', '.join(st.session_state.errors[:5]))
else:
    ranks={'PASS':0,'B':1,'B+':2,'A':3,'A+':4}; th=0 if ming=='ALL' else ranks[ming]
    rows=[]; qualified=[]
    for g in games:
        m=model(g)
        if m and ranks[m['grade']]>=th:
            qualified.append((g,m)); rows.append({'Kickoff':g['date'].strftime('%a %m/%d %I:%M %p ET'),'Matchup':g['away']['abbr']+' @ '+g['home']['abbr'],'Pick':m['pick']['name']+' ML','Win %':round(m['p']*100,1),'Strength':m['grade']})
    st.success(f'Verified {len(games)} upcoming games • {len(qualified)} meet the current model filter')
    if rows:
        st.markdown('### 🔥 TOP MONEYLINE PICKS'); st.dataframe(pd.DataFrame(rows).sort_values('Win %',ascending=False).head(10),hide_index=True,use_container_width=True)
    st.markdown('### 🎟️ MATCHUP CARDS')
    for g,m in qualified:
        with st.container(border=True): game_card(g)

with st.expander('Model & data rules'):
    st.write('The automatic model uses verified season records, AP ranking when present on the scoreboard payload, and a modest home-field adjustment. It does not call a paid or key-based API. Market edge is only calculated after real moneylines are supplied, because a win-probability advantage is not the same thing as sportsbook edge.')
    st.write('Player props are never invented. Without a reliably accessible public sportsbook prop line, the app provides a prop grader: enter the real line and a projection, and it calculates the projection edge and strength grade.')
st.divider(); st.caption('Grades describe model signal strength, not certainty. Public website access/markup can change.')
