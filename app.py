import os, math, requests
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st

st.set_page_config(page_title='NCAAF Moneyline Edge Bot', page_icon='🏈', layout='wide')

CFBD_BASE='https://api.collegefootballdata.com'
ODDS_BASE='https://api.the-odds-api.com/v4'

DIVISIONS={'FBS':'fbs','FCS':'fcs','Division II':'ii','Division III':'iii'}

@st.cache_data(ttl=1800)
def cfbd_get(path, params, key):
    r=requests.get(CFBD_BASE+path, params=params, headers={'Authorization':f'Bearer {key}'}, timeout=25)
    r.raise_for_status(); return r.json()

@st.cache_data(ttl=900)
def odds_get(sport, key):
    url=f'{ODDS_BASE}/sports/{sport}/odds/'
    p={'apiKey':key,'regions':'us','markets':'h2h','oddsFormat':'american','dateFormat':'iso'}
    r=requests.get(url, params=p, timeout=25); r.raise_for_status(); return r.json()

def american_implied(o):
    o=float(o)
    return 100/(o+100) if o>0 else (-o)/((-o)+100)

def elo_prob(home_elo, away_elo, hfa=55):
    return 1/(1+10**(-((home_elo+hfa)-away_elo)/400))

def no_vig(p1,p2):
    s=p1+p2
    return (p1/s,p2/s) if s else (0.5,0.5)

def logo_for(team_name, teams):
    x=teams.get(team_name,{})
    return x.get('logos',[None])[0] if x.get('logos') else None

@st.cache_data(ttl=86400)
def team_map(key, classification):
    data=cfbd_get('/teams', {'classification':classification}, key)
    return {t['school']:t for t in data}

@st.cache_data(ttl=1800)
def elo_map(key, year, week=None):
    p={'year':year};
    if week: p['week']=week
    data=cfbd_get('/ratings/elo',p,key)
    return {x['team']:x.get('elo') for x in data if x.get('elo') is not None}

@st.cache_data(ttl=1800)
def games(key, year, week, classification):
    return cfbd_get('/games', {'year':year,'week':week,'seasonType':'regular','classification':classification}, key)

def consensus_odds(event):
    out={}
    for b in event.get('bookmakers',[]):
        for m in b.get('markets',[]):
            if m.get('key')=='h2h':
                for x in m.get('outcomes',[]): out.setdefault(x['name'],[]).append(x['price'])
    return {k: round(sum(v)/len(v)) for k,v in out.items() if v}

def match_odds(game, events):
    h=game.get('homeTeam'); a=game.get('awayTeam')
    for e in events:
        names={e.get('home_team'),e.get('away_team')}
        if h in names and a in names: return consensus_odds(e)
    return {}

def pick_game(g, elos, odds):
    h,a=g['homeTeam'],g['awayTeam']; he=elos.get(h,1500); ae=elos.get(a,1500)
    hp=elo_prob(he,ae); ap=1-hp
    ho=odds.get(h); ao=odds.get(a)
    hm=am=None
    if ho is not None and ao is not None:
        hm,am=no_vig(american_implied(ho),american_implied(ao))
    # Blend independent Elo with market consensus when available; market gets 35% weight.
    hmodel=.65*hp+.35*hm if hm is not None else hp
    amodel=1-hmodel
    pick=h if hmodel>=amodel else a
    prob=max(hmodel,amodel)
    market=hm if pick==h else am
    edge=(prob-market)*100 if market is not None else None
    elo_gap=(he+55-ae) if pick==h else (ae-(he+55))
    reasons=[]
    reasons.append(f"Elo matchup advantage: {elo_gap:+.0f} rating points after home-field adjustment.")
    if market is not None: reasons.append(f"Model win probability {prob:.1%} vs no-vig market {market:.1%} ({edge:+.1f}% edge).")
    else: reasons.append(f"Model win probability: {prob:.1%}; live moneyline not available from the configured odds feed.")
    if g.get('neutralSite'): reasons.append('Neutral-site game: home-field boost should be treated cautiously.')
    return pick,prob,edge,' '.join(reasons)

st.title('🏈 NCAAF Moneyline Edge Bot')
st.caption('FBS • FCS • Division II • Division III | Free-data-first Streamlit dashboard')

with st.sidebar:
    st.header('Bot Controls')
    cfbd_key=st.text_input('CFBD API key', value=os.getenv('CFBD_API_KEY',''), type='password')
    odds_key=st.text_input('The Odds API key (optional)', value=os.getenv('ODDS_API_KEY',''), type='password')
    now=datetime.now(ZoneInfo('America/New_York'))
    year=st.number_input('Season',2020,now.year+1,now.year)
    week=st.number_input('Week',1,20,4)
    division=st.selectbox('Division',list(DIVISIONS))
    min_prob=st.slider('Minimum model win probability',50,95,58)/100
    min_edge=st.slider('Minimum edge % (when odds exist)',-5.0,20.0,2.0,.5)
    st.caption('API keys belong in Streamlit Secrets or environment variables for deployment.')

st.info(f"Last refresh target: {now.strftime('%A, %B %d, %Y • %I:%M %p ET')}")

if not cfbd_key:
    st.warning('Enter a free CollegeFootballData API key in the sidebar to load schedules, teams and Elo ratings.')
    st.stop()

try:
    classification=DIVISIONS[division]
    gm=games(cfbd_key,int(year),int(week),classification)
    teams=team_map(cfbd_key,classification)
    elos=elo_map(cfbd_key,int(year),int(week))
    odds_events=[]
    if odds_key:
        sports=['americanfootball_ncaaf']
        if division=='FCS': sports=['americanfootball_ncaaf_fcs']
        if division in ('FBS','FCS'):
            try: odds_events=odds_get(sports[0],odds_key)
            except Exception as e: st.warning(f'Odds feed unavailable: {e}')

    rows=[]
    for g in gm:
        if g.get('completed'): continue
        odds=match_odds(g,odds_events)
        pick,prob,edge,why=pick_game(g,elos,odds)
        if prob<min_prob: continue
        if edge is not None and edge<min_edge: continue
        dt=g.get('startDate')
        try:
            kickoff=datetime.fromisoformat(dt.replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York'))
            dtxt=kickoff.strftime('%a %b %d • %I:%M %p ET')
        except: dtxt=dt or 'TBD'
        rows.append({'Date / Time':dtxt,'Away':g['awayTeam'],'Home':g['homeTeam'],'Pick':pick,
                     'Model Win %':prob*100,'Edge %':edge,'Moneyline':odds.get(pick),'Why':why,
                     'Logo':logo_for(pick,teams)})
    rows=sorted(rows,key=lambda x:(x['Edge %'] if x['Edge %'] is not None else -999,x['Model Win %']),reverse=True)

    c1,c2,c3=st.columns(3)
    c1.metric('Qualified picks',len(rows)); c2.metric('Division',division); c3.metric('Week',int(week))
    st.subheader('Moneyline Bot Picks')
    if not rows: st.warning('No games passed the current probability/edge filters. Lower the thresholds or check another week.')
    for r in rows:
        with st.container(border=True):
            a,b=st.columns([1,8])
            with a:
                if r['Logo']: st.image(r['Logo'],width=72)
            with b:
                edge_txt='N/A' if r['Edge %'] is None else f"{r['Edge %']:+.1f}%"
                ml='N/A' if r['Moneyline'] is None else f"{r['Moneyline']:+d}"
                st.markdown(f"### {r['Pick']} ML  ·  {r['Model Win %']:.1f}% model  ·  Edge {edge_txt}")
                st.write(f"**{r['Away']} @ {r['Home']}** — {r['Date / Time']} — Consensus ML: **{ml}**")
                st.write(r['Why'])

    if rows:
        df=pd.DataFrame(rows).drop(columns=['Logo'])
        st.download_button('Download picks CSV',df.to_csv(index=False),f'ncaaf_moneyline_week_{week}.csv','text/csv')
        with st.expander('Table view'):
            st.dataframe(df,use_container_width=True,hide_index=True,column_config={'Model Win %':st.column_config.NumberColumn(format='%.1f%%'),'Edge %':st.column_config.NumberColumn(format='%+.1f%%')})

    st.divider()
    st.caption('Model: Elo win probability with a configurable home-field adjustment, blended 65/35 with the no-vig consensus market when current odds are available. Edge = model probability − no-vig market probability. Predictions are estimates, not guarantees.')
except requests.HTTPError as e:
    st.error(f'API request failed: {e}. Check your key, quota, season/week, and endpoint access.')
except Exception as e:
    st.exception(e)
