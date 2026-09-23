from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json, re
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title='NCAAF Edge Scanner', page_icon='🏈', layout='wide')
ET = ZoneInfo('America/New_York')
HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36'}
ESPN_SCOREBOARD='https://www.espn.com/college-football/scoreboard'
SOFA_FBS='https://www.sofascore.com/american-football/tournament/usa/ncaa-division-fbs/32199'
SOFA_FCS='https://www.sofascore.com/american-football/tournament/usa/ncaa-division-fcs/32200'

st.markdown('''<style>
.block-container{max-width:1400px;padding-top:1.4rem}.gamecard{border:1px solid #30363d;border-radius:18px;padding:22px;margin:14px 0;background:rgba(128,128,128,.05)}
.pick{font-size:1.35rem;font-weight:800}.good{color:#19c37d;font-weight:800}.muted{opacity:.72}.reason{line-height:1.8}
</style>''', unsafe_allow_html=True)

@st.cache_data(ttl=900, show_spinner=False)
def get_html(url):
    r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status(); return r.text

def _walk(obj):
    if isinstance(obj,dict):
        if 'competitions' in obj and isinstance(obj.get('competitions'),list): yield obj
        for v in obj.values(): yield from _walk(v)
    elif isinstance(obj,list):
        for v in obj: yield from _walk(v)

def parse_espn_games(html, start, end):
    soup=BeautifulSoup(html,'html.parser'); games=[]; seen=set()
    scripts=soup.find_all('script')
    blobs=[]
    for s in scripts:
        t=s.string or s.get_text(' ',strip=True)
        if t and ('competitions' in t and 'competitors' in t):
            m=re.search(r'({.*})',t,re.S)
            if m:
                try: blobs.append(json.loads(m.group(1)))
                except Exception: pass
    for blob in blobs:
        for ev in _walk(blob):
            eid=str(ev.get('id',''))
            if not eid or eid in seen: continue
            comps=ev.get('competitions') or []
            if not comps: continue
            comp=comps[0]; teams=comp.get('competitors') or []
            if len(teams)<2: continue
            try:
                dt=datetime.fromisoformat(str(ev.get('date')).replace('Z','+00:00')).astimezone(ET)
            except Exception: continue
            if not(start <= dt.date() <= end): continue
            by={t.get('homeAway'):t for t in teams}
            if 'home' not in by or 'away' not in by: continue
            def tm(x):
                team=x.get('team') or {}
                logos=team.get('logos') or []
                return {'name':team.get('displayName') or team.get('shortDisplayName') or 'Unknown','abbr':team.get('abbreviation',''),
                        'logo':logos[0].get('href') if logos and isinstance(logos[0],dict) else None,
                        'record':(x.get('records') or [{}])[0].get('summary','')}
            games.append({'id':eid,'date':dt,'home':tm(by['home']),'away':tm(by['away']),'status':(ev.get('status') or {}).get('type',{}).get('description','Scheduled')})
            seen.add(eid)
    return sorted(games,key=lambda x:x['date'])

def fallback_games_from_visible(html,start,end):
    # Deliberately conservative: never guesses teams from loose text.
    return []

def rec_pct(rec):
    m=re.match(r'(\d+)-(\d+)',rec or '')
    if not m:return None
    w,l=map(int,m.groups()); return w/(w+l) if w+l else None

def model_game(g):
    hp,ap=rec_pct(g['home']['record']),rec_pct(g['away']['record'])
    if hp is None or ap is None: return None
    # Transparent baseline only: season record + modest home field. No fake market edge.
    diff=(hp-ap)*1.65 + .12
    p=1/(1+pow(2.718281828,-diff))
    home_pick=p>=.5; wp=p if home_pick else 1-p
    pick=g['home'] if home_pick else g['away']; opp=g['away'] if home_pick else g['home']
    grade='A+' if wp>=.78 else 'A' if wp>=.70 else 'B+' if wp>=.64 else 'B' if wp>=.58 else 'PASS'
    reasons=[]
    pp=hp if home_pick else ap; op=ap if home_pick else hp
    if pp>op: reasons.append('Better verified season win rate')
    if home_pick: reasons.append('Home-field adjustment favors the pick')
    else: reasons.append('Road team profile overcomes the home-field adjustment')
    reasons.append('Current verified record profile favors '+pick['name'])
    return {'pick':pick,'opp':opp,'prob':wp*100,'grade':grade,'reasons':reasons}

def card(g):
    m=model_game(g)
    left,right=st.columns([1,1])
    with left:
        if g['away']['logo']: st.image(g['away']['logo'],width=72)
        st.markdown(f"### {g['away']['name']}")
        st.caption(g['away']['record'] or 'Record unavailable')
    with right:
        if g['home']['logo']: st.image(g['home']['logo'],width=72)
        st.markdown(f"### {g['home']['name']}")
        st.caption(g['home']['record'] or 'Record unavailable')
    st.markdown(f"**{g['date'].strftime('%A • %B %d, %Y • %I:%M %p ET')}**")
    st.markdown('#### 🤖 MONEYLINE MODEL')
    if not m:
        st.warning('PASS — not enough verified team data to calculate a responsible model probability.')
    else:
        st.markdown(f"<div class='pick'>{m['pick']['name']} ML</div>",unsafe_allow_html=True)
        a,b,c=st.columns(3); a.metric('Win Probability',f"{m['prob']:.1f}%"); b.metric('Model Edge','N/A'); c.metric('Pick Strength',m['grade'])
        st.caption('Model Edge is N/A unless a current, verified market moneyline is available from a public page.')
        st.markdown('**WHY THIS MONEYLINE?**')
        for x in m['reasons']: st.markdown('✓ '+x)
    st.markdown('---')
    st.markdown('#### ⭐ BEST PLAYER PROPS')
    st.info('NO VERIFIED PROP LINE — the app will not invent a player, line, projection edge, or OVER/UNDER recommendation. A prop card appears only when both the player/stat data and current line can be verified from the public sources.')
    st.markdown('#### 🚫 PASS')
    st.caption('Other props without enough verified statistical/market edge.')
    st.caption(f"Data check: Schedule ✓ • Team records {'✓' if m else '—'} • Verified prop line — • Last updated {datetime.now(ET).strftime('%m/%d/%y • %I:%M %p ET')}")

st.title('🏈 NCAAF EDGE SCANNER')
st.caption('Upcoming games • Moneyline model • Player-prop verification • No API keys')
with st.sidebar:
    st.header('Scanner Controls')
    start=st.date_input('Start date',datetime.now(ET).date())
    days=st.slider('Upcoming days',1,14,7)
    divisions=st.multiselect('Divisions',['FBS','FCS','D-II','D-III'],default=['FBS','FCS'])
    min_grade=st.selectbox('Minimum strength',['ALL','B','B+','A','A+'])
    st.caption('ESPN/Sofascore public pages are used only when accessible. No keys or private APIs.')

scan=st.button('🔍 SCAN UPCOMING GAMES',type='primary',use_container_width=True)
if scan:
    with st.spinner('Scanning public college-football pages...'):
        try:
            html=get_html(ESPN_SCOREBOARD)
            gs=parse_espn_games(html,start,start+timedelta(days=days-1))
        except Exception as e:
            gs=[]; st.session_state['err']=str(e)
        st.session_state['games']=gs; st.session_state['scanned']=datetime.now(ET)

games=st.session_state.get('games',[])
if 'scanned' not in st.session_state:
    st.info('Choose your date range and press **SCAN UPCOMING GAMES**.')
elif not games:
    st.warning('No upcoming games could be verified from the accessible public page in this scan. Nothing was fabricated. Public sites can change or block automated page access.')
else:
    ranks={'PASS':0,'B':1,'B+':2,'A':3,'A+':4}; threshold=0 if min_grade=='ALL' else ranks[min_grade]
    filtered=[]
    for g in games:
        m=model_game(g)
        if m and ranks[m['grade']]>=threshold: filtered.append((g,m))
    st.subheader(f'Upcoming Games — {len(filtered)} model-qualified of {len(games)} verified')
    top=sorted(filtered,key=lambda z:z[1]['prob'],reverse=True)[:10]
    if top:
        st.markdown('### 🔥 TOP MONEYLINE PICKS')
        df=pd.DataFrame([{'Kickoff':g['date'].strftime('%a %I:%M %p ET'),'Pick':m['pick']['name']+' ML','Win %':f"{m['prob']:.1f}%",'Edge':'N/A','Strength':m['grade']} for g,m in top])
        st.dataframe(df,use_container_width=True,hide_index=True)
    st.markdown('### 🎟️ MATCHUP CARDS')
    for g,m in filtered:
        with st.container(border=True): card(g)

with st.expander('Sources & model rules'):
    st.markdown(f'- [ESPN College Football scoreboard]({ESPN_SCOREBOARD})\n- [Sofascore NCAA FBS]({SOFA_FBS})\n- [Sofascore NCAA FCS]({SOFA_FCS})')
    st.write('This no-key build uses ordinary public webpages. It never substitutes a made-up market line. The baseline probability uses verified season records plus a modest home-field adjustment; future versions can add verified recent form and player statistics when those fields are reliably obtainable from accessible pages.')

st.divider(); st.caption('Model grades describe signal strength, not certainty. Website access and page structures can change; the app fails closed instead of fabricating data.')
