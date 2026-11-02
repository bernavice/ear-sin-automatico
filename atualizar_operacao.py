#!/usr/bin/env python3
# Operação do SIN — ONS Balanço de Energia nos Subsistemas (2000+)
# Fonte: https://dados.ons.org.br/dataset/balanco-energia-subsistema
# Dados por hora: térmica, eólica, fotovoltaica, hidráulica, intercâmbio e carga (MWmed)
from __future__ import annotations
import argparse
import calendar
import csv
import gzip
import json
import math
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
import pandas as pd
import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'dados'
GRAF = ROOT / 'graficos'
SIN_DIR = DATA / 'operacao_sin'
PUBLIC_DIR = GRAF / 'operacao_sin'
BASE = 'https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/balanco_energia_subsistema_ho/'
SUBS = ['SE','S','NE','N']  # SE/CO, Sul, Nordeste, Norte
METRICS = ['termica','eolica','solar','hidraulica','intercambio','carga']
COLS = ['datahora'] + METRICS
# aliases normalized to alphanumeric for ONS historical field variations
ALIASES = {
 'datahora': ['din_instante','din_referencia','din_instante_brasilia','dat_hora','datahora','data_hora','data_hora_medicao','data_hora_referencia','dt_referencia'],
 'subsistema': ['id_subsistema','nom_subsistema','sig_subsistema','nom_submercado','id_submercado','subsistema'],
 'termica': ['val_gertermica','val_geracao_termica','val_geracao_termeletrica','val_geracao_termoeletrica','val_geracao_term','geracao_termica'],
 'eolica': ['val_gereolica','val_geracao_eolica','geracao_eolica'],
 'solar': ['val_gersolar','val_geracao_fotovoltaica','val_geracao_solar','geracao_fotovoltaica','geracao_solar'],
 'hidraulica': ['val_gerhidraulica','val_geracao_hidraulica','val_geracao_hidreletrica','val_geracao_hidroeletrica','geracao_hidraulica'],
 'intercambio': ['val_intercambio','val_intercambio_liquido','val_intercambio_energia','intercambio'],
 'carga': ['val_carga','val_carga_verificada','carga'],
}

def key(s):
 s = unicodedata.normalize('NFKD',str(s)).encode('ascii','ignore').decode('ascii').lower()
 return re.sub('[^a-z0-9]','',s)

def pick_columns(columns):
 normalized = {key(c):c for c in columns}
 out={}
 for name,options in ALIASES.items():
  for option in options:
   if key(option) in normalized:
    out[name] = normalized[key(option)]; break
 if not {'datahora','subsistema','termica','hidraulica','carga'}.issubset(out):
  raise ValueError(f'Colunas desconhecidas do ONS: {list(columns)}; identificadas: {out}. Revisar dicionário do balanço.')
 return out

def numeric(series):
 # Handles 1.234,56; 1234.56; 1234,56; numeric
 if pd.api.types.is_numeric_dtype(series):return pd.to_numeric(series,errors='coerce')
 st=series.astype(str).str.strip().str.replace(' ','',regex=False)
 both=st.str.contains(',',regex=False)&st.str.contains('.',regex=False)
 st=st.where(~both,st.str.replace('.','',regex=False))
 st=st.str.replace(',','.',regex=False)
 return pd.to_numeric(st,errors='coerce')

def read_source_bytes(blob:bytes):
 # ONS published utf-8 CSV, historical files vary with delim / decimal
 txt = blob[:10000].decode('utf-8-sig',errors='replace')
 first=txt.splitlines()[0]
 sep=';' if first.count(';')>=first.count(',') else ','
 return pd.read_csv(BytesIO(blob),sep=sep,encoding='utf-8-sig',low_memory=False,on_bad_lines='warn')

def normalize_ons(frame, year):
 """Keep the ONS values per subsystem; never infer regional trade from SIN balances."""
 m=pick_columns(frame.columns)
 raw=pd.DataFrame({k:frame[c] for k,c in m.items()})
 raw['datahora']=pd.to_datetime(raw['datahora'],dayfirst=False,errors='coerce',format='mixed')
 if getattr(raw['datahora'].dt,'tz',None) is not None:
  raw['datahora']=raw['datahora'].dt.tz_localize(None)
 raw=raw.loc[raw['datahora'].notna() & (raw['datahora'].dt.year==int(year))].copy()
 subs=raw['subsistema'].astype(str).map(key)
 mapa={'se':'SE','seco':'SE','sudeste':'SE','sudestecentrooeste':'SE',
       's':'S','sul':'S','ne':'NE','nordeste':'NE','n':'N','norte':'N'}
 raw['subsistema']=subs.map(mapa)
 raw=raw[raw['subsistema'].isin(SUBS)].copy()
 if raw.empty: raise ValueError('Nenhum subsistema reconhecido nas linhas ONS')
 for k in METRICS:
  if k in raw: raw[k]=numeric(raw[k])
  elif k in ('solar','eolica') and (year<2015 if k=='solar' else year<2005):raw[k]=0.0
  else:raise ValueError(f'ONS {year}: coluna obrigatória ausente: {k}')
 # The ONS dictionary allows null for generation. Only assume zero in the very early
 # years when these technologies were not represented; never zero-fill modern records.
 if year<2015: raw['solar']=raw['solar'].fillna(0)
 if year<2005: raw['eolica']=raw['eolica'].fillna(0)
 mandatory=METRICS
 raw=raw.dropna(subset=mandatory)
 raw=raw.drop_duplicates(['datahora','subsistema'],keep='last')
 complete=raw.groupby('datahora')['subsistema'].nunique()
 raw=raw[raw['datahora'].isin(complete[complete==4].index)]
 raw=raw.sort_values(['datahora','subsistema'])
 raw['datahora']=raw['datahora'].dt.strftime('%Y-%m-%dT%H:%M:%S')
 print(f'ONS {year}: {len(raw)//4} horas completas com quatro subsistemas')
 return raw[['datahora','subsistema']+METRICS]

def build_sin(sub):
 if sub.empty:return pd.DataFrame(columns=['datahora']+METRICS)
 # Hourly sum of all four ONS subsystems. Interchange may be negative.
 return sub.groupby('datahora',as_index=False)[METRICS].sum().sort_values('datahora')

def file_for(year):return SIN_DIR/f'operacao_sin_{year}.csv.gz'
def file_sub_for(year):return SIN_DIR/f'operacao_subsistemas_{year}.csv.gz'

def save_frame(df,path):
 path.parent.mkdir(parents=True,exist_ok=True)
 df.to_csv(path,index=False,compression={'method':'gzip','compresslevel':9},float_format='%.2f')
 return path

def load_frame(path,sub=False):
 if not path.exists():return pd.DataFrame(columns=['datahora']+(['subsistema'] if sub else [])+METRICS)
 df=pd.read_csv(path,compression='gzip')
 for k in METRICS:df[k]=pd.to_numeric(df[k],errors='coerce')
 return df

def load_year(year):return load_frame(file_for(year))
def load_sub_year(year):return load_frame(file_sub_for(year),sub=True)

def fetch_year(year,timeout=85):
 url=BASE+f'BALANCO_ENERGIA_SUBSISTEMA_{year}.csv'
 r=requests.get(url,timeout=timeout,headers={'User-Agent':'AcompanhamentoMercadoSIN/1.0 (ONS open data; daily refresh)'})
 r.raise_for_status()
 if len(r.content)<500:raise ValueError('Resposta ONS vazia ou incompleta')
 return normalize_ons(read_source_bytes(r.content),year)

def update(years,offline=False):
 results=[]
 current=datetime.now().year
 for y in years:
  subpath=file_sub_for(y)
  # Revisit the current year daily; previous year in January and February.
  should_fetch=not subpath.exists() or y==current or (y==current-1 and datetime.now().month<=2)
  if offline:should_fetch=False
  if should_fetch:
   try:
    new=fetch_year(y)
    if new.empty:raise ValueError('Arquivo do ONS não produziu horas completas')
    old=load_sub_year(y)
    if not old.empty:
     new=pd.concat([old,new],ignore_index=True).drop_duplicates(['datahora','subsistema'],keep='last')
    new=new.sort_values(['datahora','subsistema'])
    save_frame(new,subpath)
    save_frame(build_sin(new),file_for(y))
    print(f'ONS balanço {y}: {len(new)//4} horas com dados dos 4 subsistemas')
   except Exception as e:
    print(f'AVISO ONS balanço {y}: {type(e).__name__}: {str(e)[:250]}')
  if subpath.exists() or file_for(y).exists():results.append(y)
 return results

def make_assets(years):
 """Publish four subsystems by year; SIN is derived on client, avoiding duplicated JSON."""
 PUBLIC_DIR.mkdir(parents=True,exist_ok=True)
 daily=[]; published=[]
 for year in years:
  sub=load_sub_year(year)
  if not sub.empty:
   sub['ts']=pd.to_datetime(sub['datahora'],errors='coerce')
   sub=sub.dropna(subset=['ts']).sort_values(['ts','subsistema'])
   counts=sub.groupby('ts')['subsistema'].nunique()
   sub=sub[sub['ts'].isin(counts[counts==4].index)]
   if sub.empty:continue
   ts=pd.DatetimeIndex(sorted(sub['ts'].unique()))
   rows={}
   for sm in SUBS:
    v=sub[sub['subsistema']==sm].set_index('ts').reindex(ts)[METRICS]
    rows[sm]=[[round(float(x),1) for x in line] for line in v.to_numpy()]
   encoded={'ts':ts.strftime('%Y-%m-%dT%H:%M').tolist(),'s':rows}
   sin=build_sin(sub)
  else:
   # Backward compatibility: legacy SIN cache displays SIN; other charts correctly say unavailable.
   sin=load_year(year)
   if sin.empty:continue
   sin['ts']=pd.to_datetime(sin['datahora'],errors='coerce')
   sin=sin.dropna(subset=['ts']).sort_values('ts')
   encoded={'ts':sin['ts'].dt.strftime('%Y-%m-%dT%H:%M').tolist(),
            'sin':[[round(float(x),1) for x in line] for line in sin[METRICS].to_numpy()]}
  (PUBLIC_DIR/f'{year}.json').write_text(json.dumps(encoded,separators=(',',':'),ensure_ascii=False),encoding='utf8')
  published.append(year)
  sin['ts']=pd.to_datetime(sin['datahora'],errors='coerce')
  # Complete days only; incomplete periods never enter the five cards.
  byday=sin.groupby(sin['ts'].dt.strftime('%Y-%m-%d'))[METRICS].agg(['mean','count'])
  for d,v in byday.iterrows():
   if any(int(v[(m,'count')])!=24 for m in METRICS):continue
   daily.append([d]+[round(float(v[(m,'mean')]),2) for m in METRICS])
 daily.sort(key=lambda r:r[0])
 (PUBLIC_DIR/'resumo.json').write_text(json.dumps({'years':published,'daily':daily},separators=(',',':')),encoding='utf8')
 return published,daily

def integrate_index():
 idx=GRAF/'index.html'
 if not idx.exists():return
 txt=idx.read_text(encoding='utf8')
 if 'id="botao-operacao"' in txt:return
 button='<button role="tab" id="botao-operacao" aria-selected="false" aria-controls="aba-operacao" onclick="selecionar(\'operacao\')">Operação do SIN</button>\n'
 panel='<section class="aba" id="aba-operacao" role="tabpanel" aria-labelledby="botao-operacao"><div class="molde"><iframe id="quadro-operacao" title="Operação do SIN" src="operacao.html" loading="lazy" style="height:2500px"></iframe></div></section>\n'
 txt=txt.replace('</nav>',button+'</nav>',1).replace('</main>',panel+'</main>',1)
 # extend the existing selection logic, including resize + message handler
 for old,new in (
  ("['ear','ena','pld','mercado']","['ear','ena','pld','mercado','operacao']"),
  ("['ena','ear','pld','mercado']","['ena','ear','pld','mercado','operacao']"),
  ("['ena','pld','mercado']","['ena','pld','mercado','operacao']"),
  ("['ear','ena','pld']","['ear','ena','pld','operacao']"),
  ("['ena','ear','pld']","['ena','ear','pld','operacao']"),
  ("['ena','pld']","['ena','pld','operacao']"),
 ):
  txt=txt.replace(old,new)
 idx.write_text(txt,encoding='utf8')

HTML_TEMPLATE = r'''<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Operação do SIN — Acompanhamento de mercado</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:'Century Gothic',CenturyGothic,Arial,sans-serif;margin:0;background:#fff;color:#243547}
main{padding:0 12px 24px}.painel{border:1px solid #e0e8ee;border-radius:14px;margin-bottom:16px;overflow:hidden;background:#fff}
.barra{background:#007E80;color:#fff;text-align:center;font-weight:700;padding:13px;font-size:18px}
.grid{padding:17px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}
.card{min-width:0;border:1px solid #dce7eb;background:#f8fbfc;border-radius:12px;padding:12px}
.card h3{font-size:14px;margin:0 0 10px;color:#006f75}.select{font-family:inherit;border:1px solid #cbd8e3;border-radius:7px;padding:6px 8px;background:#fff;color:#243547;max-width:100%;width:100%;font-size:12px}
.valor{font-size:23px;font-weight:700;color:#173b4f;white-space:nowrap;padding-top:12px}.unidade{font-size:11px;color:#69788a}
.linha{display:flex;justify-content:space-between;font-size:11px;color:#647487;gap:6px;padding-top:9px;border-top:1px solid #e2e8ee;margin-top:9px}.delta{font-size:13px;font-weight:700;white-space:nowrap}.positivo{color:#087f77}.negativo{color:#c44752}
.referencia{font-size:10px;color:#7a8796;margin-top:9px;line-height:1.5}
.controls{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:13px 18px}.controls label{font-size:13px;font-weight:700;color:#536579}.controls input{font:inherit;padding:8px;border:1px solid #c5d7df;border-radius:8px;color:#243547}
.graficos-sub{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding-bottom:6px}
.grafico-box{min-width:0;overflow:hidden;border:1px solid #e0e8ee;border-radius:14px;background:#fff}
.grafico-box.sin{grid-column:1 / -1}
.grafico-box .barra{font-size:16px;padding:12px}.chart{width:100%;height:425px}.sin .chart{height:530px}
.legenda{display:flex;align-items:center;justify-content:center;gap:16px;flex-wrap:wrap;padding:9px 14px 15px;font-size:12px}
.itemleg{display:flex;gap:7px;align-items:center;white-space:nowrap}.bolinha{width:25px;height:0;border-top:4px solid;border-radius:2px}
.itemleg:last-child .bolinha{border-top-width:3px}
.nota{padding:7px 18px 16px;color:#66768a;font-size:11px;line-height:1.6}.status{font-size:12px;color:#6b7888}
.aviso{padding:0 18px 10px;color:#8a530d;font-size:11px}
@media(max-width:1150px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:750px){.grid{grid-template-columns:repeat(2,minmax(0,1fr));padding:10px;gap:8px}.valor{font-size:20px}.graficos-sub{grid-template-columns:1fr}.grafico-box.sin{grid-column:auto}.chart{height:360px}.sin .chart{height:420px}}
@media(max-width:430px){.grid{grid-template-columns:1fr}}
</style></head><body><main>
<section class="painel"><div class="barra">Operação do SIN — Resumo</div><div class="grid" id="cards"></div><p class="nota">Média das 24 horas do último dia completo. Δ em MWmed e p.p. da participação na carga; para Carga, p.p. do índice com referência = 100.</p></section>
<section class="painel"><div class="barra">Operação do SIN — Balanço Horário</div>
<div class="controls"><label for="data">Selecione a data: <input type="date" id="data"></label><span class="status" id="status">Carregando…</span></div></section>
<div class="graficos-sub" id="graficos-sub">
<section class="grafico-box sin"><div class="barra">SIN</div><div id="chart-sin" class="chart"></div><p class="aviso" id="aviso-sin"></p></section>
<section class="grafico-box"><div class="barra">Sudeste / Centro-Oeste (SE/CO)</div><div id="chart-se" class="chart"></div><p class="aviso" id="aviso-se"></p></section>
<section class="grafico-box"><div class="barra">Sul (S)</div><div id="chart-s" class="chart"></div><p class="aviso" id="aviso-s"></p></section>
<section class="grafico-box"><div class="barra">Nordeste (NE)</div><div id="chart-ne" class="chart"></div><p class="aviso" id="aviso-ne"></p></section>
<section class="grafico-box"><div class="barra">Norte (N)</div><div id="chart-n" class="chart"></div><p class="aviso" id="aviso-n"></p></section>
</div>
<section class="painel"><div class="legenda" id="legenda"></div><p class="nota">Fonte: ONS — Balanço de Energia nos Subsistemas. Valores em MWmed. As áreas representam Térmica, Eólica, Solar, Hidráulica e Intercâmbio líquido (positivo ou negativo); a Carga é mostrada como linha para evitar dupla contagem. O SIN é a soma das quatro regiões. O sinal do intercâmbio é mantido conforme publicado pelo ONS, sem ajuste artificial para fechar a carga.</p></section>
</main><script>
const KEYS=['termica','eolica','solar','hidraulica','intercambio','carga'];
const LABELS=['Térmica','Eólica','Solar','Hidráulica','Intercâmbio','Carga'];
const CORES=['#E18B51','#6CB7D2','#EACB59','#2B9C89','#9987BB','#23344D'];
const REGIOES=[['sin','SIN'],['se','SE'],['s','S'],['ne','NE'],['n','N']];
const PERIODOS=[['dia','Dia anterior'],['semana','Semana anterior'],['mes','Mês anterior'],['ano','Ano anterior']];
const fmt=(n,dp=0)=>n==null||!Number.isFinite(n)?'—':n.toLocaleString('pt-BR',{minimumFractionDigits:dp,maximumFractionDigits:dp});
let DAILY=[], YEARS=[], CACHE={}, state=[3,0,1,2,5].map(metric=>({periodo:'mes',metric}));
const msDay=86400000;
function addMonths(iso,n){let [y,m,d]=iso.split('-').map(Number);m+=n;while(m<=0){m+=12;y--}while(m>12){m-=12;y++};d=Math.min(d,new Date(Date.UTC(y,m,0)).getUTCDate());return `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`}
function refDate(day,p){if(p==='mes')return addMonths(day,-1);if(p==='ano')return addMonths(day,-12);let d=new Date(day+'T12:00:00Z');d=new Date(d.getTime()-(p==='semana'?7:1)*msDay);return d.toISOString().slice(0,10)}
function prior(day){let lo=0,hi=DAILY.length-1,found=null;while(lo<=hi){let mid=Math.floor((lo+hi)/2);if(DAILY[mid][0]<=day){found=DAILY[mid];lo=mid+1}else hi=mid-1}return found}
function renderCards(){let box=document.getElementById('cards');if(!DAILY.length){box.innerHTML='<p>Sem histórico diário consolidado disponível.</p>';return}
 const latest=DAILY[DAILY.length-1];box.innerHTML=state.map((s,i)=>{
  let old=prior(refDate(latest[0],s.periodo));let cur=latest[1+s.metric],before=old?old[1+s.metric]:null;let diff=old?cur-before:null;
  let cLoad=latest[6],pLoad=old?old[6]:null;
  let pp=old&&pLoad>0&&cLoad>0?(s.metric===5?(cur/before-1)*100:(cur/cLoad-before/pLoad)*100):null;
  let klass=diff==null?'':diff>=0?'positivo':'negativo';let sel=PERIODOS.map(([v,l])=>`<option value="${v}" ${s.periodo===v?'selected':''}>${l}</option>`).join('');
  return `<article class="card"><h3>${LABELS[s.metric]}</h3><select class="select" data-card="${i}" aria-label="Comparar ${LABELS[s.metric]}">${sel}</select>
  <div class="valor">${fmt(cur)} <span class="unidade">MWmed</span></div><div class="linha"><span>Referência ${old?old[0].split('-').reverse().join('/'):'—'}</span><strong>${fmt(before)} MWmed</strong></div>
  <div class="linha"><span>Δ (MWmed)</span><span class="delta ${klass}">${diff==null?'—':(diff>=0?'+':'')+fmt(diff)}</span></div><div class="linha"><span>Δ (p.p.)</span><span class="delta ${pp==null?'':pp>=0?'positivo':'negativo'}">${pp==null?'—':(pp>=0?'+':'')+fmt(pp,2)}</span></div></article>`
 }).join('');box.querySelectorAll('select').forEach(s=>s.addEventListener('change',()=>{state[Number(s.dataset.card)].periodo=s.value;renderCards()}))}
async function yearData(y){if(CACHE[y])return CACHE[y];const r=await fetch(`operacao_sin/${y}.json`);if(!r.ok)throw Error('Dados anuais indisponíveis: '+y);CACHE[y]=await r.json();return CACHE[y]}
function renderLegend(){document.getElementById('legenda').innerHTML=LABELS.map((s,i)=>`<span class="itemleg"><span class="bolinha" style="border-color:${CORES[i]}"></span>${s}</span>`).join('')}
function regionalData(doc,id,pos,end){
 if(id==='sin'&&doc.sin)return doc.sin.slice(pos,end);
 if(!doc.s)return null;
 if(id==='sin'){
  return Array.from({length:end-pos},(_,j)=>KEYS.map((_,m)=>['SE','S','NE','N'].reduce((acc,sm)=>acc+(doc.s[sm][pos+j][m]||0),0)))
 }
 const sub={'se':'SE','s':'S','ne':'NE','n':'N'}[id];return doc.s[sub]?doc.s[sub].slice(pos,end):null
}
function chartTraces(xs,rows){return KEYS.map((k,i)=>({
 type:'scatter',mode:'lines',name:LABELS[i],x:xs,y:rows.map(v=>v[i]),
 line:{color:CORES[i],width:i===5?3:0.85,shape:'linear'},
 ...(i===5?{}:{stackgroup:'geracao'}),
 hovertemplate:`<b>${LABELS[i]}</b><br>%{x|%d/%m/%Y %H:%M}<br>%{y:,.1f} MWmed<extra></extra>`
}))}
async function plotDay(day){const status=document.getElementById('status');status.textContent='Carregando '+day+'…';try{
 const doc=await yearData(Number(day.slice(0,4)));
 const pos=doc.ts.findIndex(t=>t.slice(0,10)===day);
 if(pos<0){status.textContent='Sem dados para '+day;REGIOES.forEach(([id])=>Plotly.purge('chart-'+id));return}
 let end=pos;while(end<doc.ts.length&&doc.ts[end].slice(0,10)===day)end++;
 const xs=doc.ts.slice(pos,end);
 await Promise.all(REGIOES.map(async ([id,title])=>{
  const rows=regionalData(doc,id,pos,end);const target='chart-'+id;const aviso=document.getElementById('aviso-'+id);
  if(!rows){Plotly.purge(target);document.getElementById(target).textContent='Histórico regional ainda não disponível para este ano. Aguardando coleta dos quatro subsistemas.';aviso.textContent='';return}
  const traces=chartTraces(xs,rows);
  await Plotly.react(target,traces,{height:id==='sin'?530:425,showlegend:false,margin:{t:24,b:55,l:83,r:17},paper_bgcolor:'#fff',plot_bgcolor:'#fff',
   xaxis:{title:'Hora',type:'date',tickformat:'%H:%M',dtick:7200000,showgrid:false,showline:true,linecolor:'#94a3b8'},
   yaxis:{title:'Potência média (MWmed)',tickformat:',.0f',gridcolor:'#edf1f5',zeroline:true,zerolinecolor:'#94a3b8'},
   font:{family:'Century Gothic,Arial',color:'#344054',size:12},hovermode:'x unified',hoverlabel:{bgcolor:'#f7f9fc'},uirevision:'operacao-'+id},{responsive:true,displaylogo:false});
  const residual=rows.map(v=>v[5]-v.slice(0,5).reduce((a,z)=>a+z,0));const maxres=Math.max(...residual.map(Math.abs));
  aviso.textContent=maxres>1000?`Diferença máxima entre carga e geração + intercâmbio: ${fmt(maxres)} MWmed (dados ONS sem ajuste).`:'';
 }));
 status.textContent=`${xs.length} horas • ${day.split('-').reverse().join('/')}`;
}catch(e){status.textContent='Não foi possível carregar '+day+': '+e.message;console.error(e)}}
async function main(){renderLegend();try{
 const r=await fetch('operacao_sin/resumo.json');if(!r.ok)throw Error('Resumo não encontrado');const o=await r.json();DAILY=o.daily||[];YEARS=o.years||[];
 if(!DAILY.length){document.getElementById('status').textContent='Aguardando primeira coleta do ONS';renderCards();return}
 renderCards();const input=document.getElementById('data');input.min=DAILY[0][0];input.max=DAILY[DAILY.length-1][0];input.value=DAILY[DAILY.length-1][0];input.addEventListener('change',()=>plotDay(input.value));await plotDay(input.value)
}catch(e){document.getElementById('status').textContent='Histórico indisponível: '+e.message;renderCards()}}
main();
function enviarAltura(){if(window.parent!==window)window.parent.postMessage({tipo:'altura-painel-ons',id:'operacao',altura:document.body.scrollHeight+18},'*')}
window.addEventListener('load',enviarAltura);if(window.ResizeObserver)new ResizeObserver(enviarAltura).observe(document.body);
</script></body></html>'''
def make_html():
 GRAF.mkdir(parents=True,exist_ok=True)
 (GRAF/'operacao.html').write_text(HTML_TEMPLATE,encoding='utf8')
 integrate_index()


def main():
 p=argparse.ArgumentParser()
 p.add_argument('--offline',action='store_true',help='Usar apenas dados persistidos, sem requisitar ONS')
 p.add_argument('--years',nargs='*',type=int,help='Anos a processar (padrão 2000–ano atual)')
 args=p.parse_args()
 years=args.years if args.years is not None else list(range(2000,datetime.now().year+1))
 available=update(years,args.offline)
 avail,daily=make_assets(available)
 make_html()
 print(f'Operação SIN: {len(avail)} anos armazenados, {len(daily)} dias completos, HTML graficos/operacao.html')
 if not avail:print('ATENÇÃO: Sem dados baixados; HTML abre informando que aguarda a primeira coleta.')

if __name__=='__main__':main()
