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
 # O ONS pode publicar datas ISO ou no padrão brasileiro. Datas com barra
 # (01/11/2026) precisam de dayfirst=True; ISO (2026-11-01) usa yearfirst.
 originais=raw['datahora'].astype('string').str.strip()
 iso=originais.str.match(r'^\d{4}[-/]',na=False)
 datas=pd.Series(pd.NaT,index=raw.index,dtype='datetime64[ns]')
 for mask,prioridade in ((iso,'iso'),(~iso,'br')):
  if not mask.any():continue
  datas.loc[mask]=pd.to_datetime(originais.loc[mask],dayfirst=(prioridade=='br'),
                                   errors='coerce',format='mixed')
 raw['datahora']=datas
 if getattr(raw['datahora'].dt,'tz',None) is not None:
  raw['datahora']=raw['datahora'].dt.tz_localize(None)
 raw=raw.loc[raw['datahora'].notna() & (raw['datahora'].dt.year==int(year))].copy()
 if raw.empty:raise ValueError(f'ONS {year}: nenhuma data válida após interpretação das datas')
 ultimo_bruto=raw['datahora'].max()
 print(f'ONS {year} diagnóstico: última data/hora encontrada no CSV recebido: {ultimo_bruto:%d/%m/%Y %H:%M}')
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
 # Diagnóstico enxuto: preserva a regra de exigir 4 subsistemas completos,
 # mas explica em quais datas/variáveis foram perdidas horas recentes.
 ultima_data=raw['datahora'].max().normalize()
 limite_diag=ultima_data-pd.Timedelta(days=4)
 recentes=raw.loc[raw['datahora']>=limite_diag].copy()
 validos=recentes.dropna(subset=mandatory).drop_duplicates(['datahora','subsistema'],keep='last')
 for data_diag,g in recentes.groupby(recentes['datahora'].dt.date,sort=True):
  gv=validos.loc[validos['datahora'].dt.date==data_diag]
  sub_por_hora=gv.groupby('datahora')['subsistema'].nunique()
  horas_completas=int(sub_por_hora.eq(4).sum())
  faltantes={m:int(g[m].isna().sum()) for m in mandatory if g[m].isna().any()}
  print(f'ONS {year} diagnóstico {data_diag:%d/%m/%Y}: '
        f'{g["datahora"].nunique()} horas recebidas; '
        f'{horas_completas}/24 horas com os 4 subsistemas válidos; '
        f'campos ausentes={faltantes or "nenhum"}')
 raw=raw.dropna(subset=mandatory)
 raw=raw.drop_duplicates(['datahora','subsistema'],keep='last')
 complete=raw.groupby('datahora')['subsistema'].nunique()
 raw=raw[raw['datahora'].isin(complete[complete==4].index)]
 raw=raw.sort_values(['datahora','subsistema'])
 if not raw.empty:
  print(f'ONS {year} diagnóstico: última hora completa de todos os subsistemas: '
        f'{raw["datahora"].max():%d/%m/%Y %H:%M}')
  por_dia=raw.groupby(raw['datahora'].dt.date)['datahora'].nunique()
  dias_completos=por_dia[por_dia==24]
  if not dias_completos.empty:
   print(f'ONS {year} diagnóstico: último dia completo elegível aos cards: '
         f'{dias_completos.index.max():%d/%m/%Y}')
  else:print(f'ONS {year} AVISO: nenhuma data com 24 horas completas para os cards')
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
 # Consolidação mensal do SIN baseada apenas em dias completos (24 horas).
 # Pequeno agregado para todas as séries e anos: evita transferir décadas de dados horários.
 monthly=[]
 if daily:
  dm=pd.DataFrame(daily,columns=['dia']+METRICS)
  dm['ano']=dm['dia'].str[:4].astype(int);dm['mes']=dm['dia'].str[5:7].astype(int)
  agg=dm.groupby(['ano','mes'],sort=True)[METRICS].mean().reset_index()
  monthly=[[int(row['ano']),int(row['mes'])]+[round(float(row[k]),2) for k in METRICS] for _,row in agg.iterrows()]
 (PUBLIC_DIR/'resumo.json').write_text(json.dumps({'years':published,'daily':daily,'monthly':monthly},separators=(',',':')),encoding='utf8')
 return published,daily

def integrate_index():
 '''Finaliza o index depois de ENA, Mercado e Operação, sempre reaplicando a ordem.'''
 idx=GRAF/'index.html'
 if not idx.exists(): return
 txt=idx.read_text(encoding='utf8')
 if 'id="botao-operacao"' not in txt:
  button='<button role="tab" id="botao-operacao" aria-selected="false" aria-controls="aba-operacao" onclick="selecionar(\'operacao\')">Operação</button>\n'
  panel='<section class="aba" id="aba-operacao" role="tabpanel" aria-labelledby="botao-operacao"><div class="molde"><iframe id="quadro-operacao" title="Operação do SIN" src="operacao.html" loading="eager" style="height:3450px"></iframe></div></section>\n'
  txt=txt.replace('</nav>',button+'</nav>',1).replace('</main>',panel+'</main>',1)
 for old,new in (
  ("['ear','ena','pld','mercado']", "['ear','ena','pld','mercado','operacao']"),
  ("['ena','ear','pld','mercado']", "['ena','ear','pld','mercado','operacao']"),
  ("['ena','pld','mercado']", "['ena','pld','mercado','operacao']"),
  ("['ear','ena','pld']", "['ear','ena','pld','operacao']"),
  ("['ena','ear','pld']", "['ena','ear','pld','operacao']"),
  ("['ena','pld']", "['ena','pld','operacao']"),
 ):
  txt=txt.replace(old,new)
 import re
 ordem=['operacao','ena','ear','pld','mercado']
 # Reorder without changing the underlying tab management code.
 padrao_botao=re.compile(r'<button\s+role="tab"\s+id="botao-(operacao|ena|ear|pld|mercado)"[^>]*>.*?</button>',re.S)
 botoes={m.group(1):m.group(0) for m in padrao_botao.finditer(txt)}
 if set(botoes)!=set(ordem):
  raise RuntimeError(f'Botões incompletos no index.html: {list(botoes)}')
 txt=padrao_botao.sub('',txt)
 txt=txt.replace('</nav>', '\n'.join(botoes[k] for k in ordem)+'\n</nav>',1)
 txt=txt.replace('>Operação do SIN</button>', '>Operação</button>')
 # Sections have only one closing section tag, no nested sections.
 padrao_aba=re.compile(r'<section class="aba(?: ativa)?" id="aba-(operacao|ena|ear|pld|mercado)".*?</section>',re.S)
 abas={m.group(1):m.group(0) for m in padrao_aba.finditer(txt)}
 if set(abas)!=set(ordem):
  raise RuntimeError(f'Abas incompletas no index.html: {list(abas)}')
 txt=padrao_aba.sub('',txt)
 partes=[]
 for k in ordem:
  sec=abas[k].replace('class="aba ativa"','class="aba"')
  if k=='operacao':
   sec=sec.replace('class="aba"','class="aba ativa"',1).replace('loading="lazy"','loading="eager"')
  partes.append(sec)
 txt=txt.replace('</main>','\n'.join(partes)+'\n</main>',1)
 for k in ordem:
  txt=txt.replace(botoes[k],botoes[k].replace('aria-selected="true"','aria-selected="false"'))
 # Update aria-selected for exactly the first tab.
 txt=txt.replace('id="botao-operacao" aria-selected="false"','id="botao-operacao" aria-selected="true"')
 txt=re.sub(r'<p class="subtexto">.*?</p>\s*','',txt,count=1,flags=re.S)
 date_str='—'
 path=PUBLIC_DIR/'resumo.json'
 if path.exists():
  try:
   daily=json.loads(path.read_text(encoding='utf8')).get('daily',[])
   if daily: date_str=datetime.strptime(daily[-1][0],'%Y-%m-%d').strftime('%d/%m/%Y')
  except (ValueError,KeyError,IndexError,TypeError): pass
 txt=txt.replace('Fonte: ONS / CCEE &nbsp;', 'Fonte: ONS / CCEE / Mercado &nbsp;')
 if ' · Operação ' not in txt:
  txt=txt.replace('Dados atualizados até:',f'Dados atualizados até: Operação {date_str} ·',1)
 else:
  txt=re.sub(r'Operação [\d/—]+ ·',f'Operação {date_str} ·',txt,count=1)
 # Ensure default first tab as Operation; query string still controls any other tab.
 txt=txt.replace('const visao = new URLSearchParams', 'const visao = new URLSearchParams')
 # Print button and printing support. No external Python server or browser extension needed.
 if 'id="imprimir-dashboard"' not in txt:
  txt=txt.replace('</nav>', '''</nav><div class="barra-impresso"><button id="imprimir-dashboard" type="button" onclick="imprimirDashboardCompleto()" title="Imprimir todos os módulos em um único PDF">Imprimir dashboard em PDF</button></div>''',1)
  txt=txt.replace('</style>', '''
  .barra-impresso{display:flex;justify-content:flex-end;margin:8px 0 2px}
  #imprimir-dashboard{background:#007E80;color:#fff;border-color:#007E80;padding:9px 18px;font-size:12px}
  @media print {.abas,.barra-impresso{display:none!important}}
</style>''',1)
 if 'async function imprimirDashboardCompleto' not in txt:
  txt=txt.replace('</body>', '<script>'+PRINT_JS+'</script></body>',1)
 idx.write_text(txt,encoding='utf8')

PRINT_JS = r'''
async function imprimirDashboardCompleto(){
  // A janela deve abrir diretamente no clique; o navegador controla o destino do PDF.
  const janela=window.open('','_blank');
  if(!janela){alert('Permita pop-ups deste site para abrir a impressão do dashboard.');return;}
  janela.document.open();janela.document.write(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Acompanhamento de mercado — Relatório</title>
    <style>@page{size:A4 landscape;margin:10mm}body{font-family:Arial,sans-serif;color:#253345;margin:0}
    .report-header{border-bottom:3px solid #007E80;padding:10px 0;margin-bottom:10px}
    .report-title{font-size:22px;font-weight:bold;color:#007E80;margin:0 0 6px}
    .pdf-modulo{display:block;break-before:page;margin-bottom:30px;}
    img.chart-export{display:block;max-width:100%;height:auto;object-fit:contain;margin:auto}
    @media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}.pdf-modulo{page-break-before:always}}
    </style></head><body><header class="report-header"><h1 class="report-title">Acompanhamento de mercado</h1><p>${document.querySelector('.fonte-atualizacao')?.textContent||''}</p></header><div id="pdf-status">Preparando todas as abas e gráficos para impressão…</div><div id="pdf-content"></div></body></html>`);janela.document.close();
  const status=janela.document.getElementById('pdf-status');
  const nomes=[['operacao','Operação do SIN'],['ena','ENA'],['ear','EAR'],['pld','PLD'],['mercado','Preço de Mercado']];
  const original=document.querySelector('.aba.ativa');
  const impressaoStyle=document.createElement('style');impressaoStyle.textContent='.aba{display:block!important;position:absolute!important;left:-30000px!important;top:0!important;width:1350px!important;visibility:visible!important}';document.head.appendChild(impressaoStyle);
  try{
    for(const [id,titulo] of nomes){
      status.textContent='Preparando '+titulo+'…';
      const frame=document.getElementById('quadro-'+id);
      frame.loading='eager';
      if(!frame.contentDocument||frame.contentDocument.readyState!=='complete'){
        await Promise.race([new Promise(resolve=>frame.addEventListener('load',resolve,{once:true})),new Promise(resolve=>setTimeout(resolve,12000))]);
      }
      const doc=frame.contentDocument;
      if(!doc){throw Error('Não foi possível ler o módulo '+titulo+'. Abra pelo endereço do GitHub Pages.')}
      const alvoEsperado={operacao:6,ena:5,ear:5,pld:5,mercado:1}[id]||1;
      for(let n=0;n<24;n++){
        const prontos=[...doc.querySelectorAll('.js-plotly-plot')].filter(el=>el._fullLayout).length;
        if(prontos>=alvoEsperado)break;
        await new Promise(resolve=>setTimeout(resolve,350));
      }
      const origem=doc.querySelector('main')||doc.body;
      const copia=origem.cloneNode(true);
      // Convert Plotly canvases to SVG / image before cloning into the print window.
      const plots=[...origem.querySelectorAll('.js-plotly-plot')].filter(el=>el._fullLayout);
      for(let i=0;i<plots.length;i++){
        const plot=plots[i];
        try{
          const limite=plot.closest('.painel, .grafico-box')?.clientWidth||1000;
          const width=Math.min(1250,Math.max(660,limite));
          const height=Math.min(490,Math.max(290,plot.clientHeight||430));
          const url=await frame.contentWindow.Plotly.toImage(plot,{format:'svg',width,height});
          const originalNodes=[...origem.querySelectorAll('.js-plotly-plot')];
          const index=originalNodes.indexOf(plot);
          const nodes=[...copia.querySelectorAll('.js-plotly-plot')];
          if(index>=0&&nodes[index]){
            const img=janela.document.createElement('img');img.src=url;img.alt='Gráfico '+(i+1)+' de '+titulo;img.className='chart-export';
            nodes[index].replaceWith(img);
          }
        }catch(e){console.warn('Exportação de gráfico',titulo,i,e)}
      }
      // Each module uses its own independent stylesheet. Shadow DOM prevents CSS collisions.
      const div=janela.document.createElement('section');div.className='pdf-modulo';
      const shadow=div.attachShadow({mode:'open'});
      const styles=[...doc.querySelectorAll('head style')].map(x=>x.textContent).join('\n');
      const st=janela.document.createElement('style');
      st.textContent=':host{display:block}img.chart-export{display:block;max-width:100%;height:auto;object-fit:contain;margin:8px auto}'+styles+'\n@media print {.js-plotly-plot{display:none!important}.painel,.grafico-box,.card{break-inside:avoid} .area,.graficos-sub{gap:9px!important} .fonte-dados{padding-bottom:8px}}';
      shadow.appendChild(st);
      const h=janela.document.createElement('h2');h.textContent=titulo;h.style.cssText='font-family:Arial;color:#007E80;font-size:20px;margin:12px 0';shadow.appendChild(h);
      // importNode ensures all cloned elements belong to this print document.
      shadow.appendChild(janela.document.importNode(copia,true));
      janela.document.getElementById('pdf-content').appendChild(div);
    }
    status.remove();
    await new Promise(resolve=>setTimeout(resolve,700));
    janela.focus();janela.print();
  }catch(e){status.textContent='Falha ao preparar o relatório: '+e.message;console.error(e)}
  finally{impressaoStyle.remove();}
}
'''

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
.referencia{font-size:10px;color:#7a8796;margin-top:9px;line-height:1.5}.calendario-card{margin-top:7px}.calendario-card[hidden]{display:none!important}
.controls{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:13px 18px}.controls label{font-size:13px;font-weight:700;color:#536579}.controls input{font:inherit;padding:8px;border:1px solid #c5d7df;border-radius:8px;color:#243547}
.graficos-sub{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding-bottom:6px}
.grafico-box{min-width:0;overflow:hidden;border:1px solid #e0e8ee;border-radius:14px;background:#fff}
.grafico-box.sin{grid-column:1 / -1}
.grafico-box .barra{font-size:16px;padding:12px}.chart{width:100%;height:470px}.sin .chart{height:560px}
.legenda{display:flex;align-items:center;justify-content:center;gap:16px;flex-wrap:wrap;padding:9px 14px 15px;font-size:12px}
.itemleg{display:flex;gap:7px;align-items:center;white-space:nowrap}.bolinha{width:25px;height:0;border-top:4px solid;border-radius:2px}
.itemleg:last-child .bolinha{border-top-width:3px}
.mensal-controles{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;padding:14px 18px 6px}.mensal-controles strong{font-size:12px;color:#506377}.chips{display:flex;flex-wrap:wrap;gap:6px}.chip-filtro{font:inherit;font-size:11px;font-weight:600;background:#fff;border:1px solid #cbd7df;border-radius:7px;padding:6px 9px;color:#344054;cursor:pointer}.chip-filtro[aria-pressed='true']{background:#e5f4f2;border-color:#007E80;color:#006d74}.chip-ano{display:inline-flex;align-items:center;gap:6px}.ponto-ano{width:13px;height:4px;border-radius:3px;display:inline-block}.mensal-plot{width:100%;height:540px}.nota{padding:7px 18px 16px;color:#66768a;font-size:11px;line-height:1.6}.status{font-size:12px;color:#6b7888}
.aviso{padding:0 18px 10px;color:#8a530d;font-size:11px}
@media(max-width:1150px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:750px){.grid{grid-template-columns:repeat(2,minmax(0,1fr));padding:10px;gap:8px}.valor{font-size:20px}.graficos-sub{grid-template-columns:1fr}.grafico-box.sin{grid-column:auto}.chart{height:360px}.sin .chart{height:420px}}
@media(max-width:430px){.grid{grid-template-columns:1fr}}
</style></head><body><main>
<section class="painel"><div class="barra">Operação do SIN — Resumo</div><div class="grid" id="cards"></div><p class="nota">Média das 24 horas do último dia completo. Δ em MWmed e variação relativa (%) em relação ao próprio valor de referência.</p></section>
<section class="painel"><div class="barra">Operação do SIN — Balanço Horário</div>
<div class="controls"><label for="data">Selecione a data: <input type="date" id="data"></label><span class="status" id="status">Carregando…</span></div></section>
<div class="graficos-sub" id="graficos-sub">
<section class="grafico-box sin"><div class="barra">SIN</div><div id="chart-sin" class="chart"></div><p class="aviso" id="aviso-sin"></p></section>
<section class="grafico-box"><div class="barra">Sudeste / Centro-Oeste (SE/CO)</div><div id="chart-se" class="chart"></div><p class="aviso" id="aviso-se"></p></section>
<section class="grafico-box"><div class="barra">Sul (S)</div><div id="chart-s" class="chart"></div><p class="aviso" id="aviso-s"></p></section>
<section class="grafico-box"><div class="barra">Nordeste (NE)</div><div id="chart-ne" class="chart"></div><p class="aviso" id="aviso-ne"></p></section>
<section class="grafico-box"><div class="barra">Norte (N)</div><div id="chart-n" class="chart"></div><p class="aviso" id="aviso-n"></p></section>
</div>
<section class="painel"><div class="barra">Operação do SIN — Mensal</div>
<div class="mensal-controles"><strong>Recursos:</strong><div class="chips" id="filtro-recursos"></div></div>
<div class="mensal-controles"><strong>Anos:</strong><div class="chips" id="filtro-anos"></div></div>
<div id="chart-mensal" class="mensal-plot"></div><p class="nota">SIN: média mensal das 24 horas dos dias completos. O ponto «Ano» é a média dos dias completos daquele ano, sem ligação com dezembro. As cores representam anos; os traçados distinguem recursos. Fonte: ONS.</p></section>
<section class="painel"><p class="nota">Clique nas legendas dos gráficos para mostrar ou ocultar fontes. Fonte: ONS — Balanço de Energia nos Subsistemas. Valores em MWmed. As áreas representam Térmica, Eólica, Solar, Hidráulica e Intercâmbio líquido (quando ativado); a Carga é mostrada como linha para evitar dupla contagem. O SIN é a soma das quatro regiões. A convenção do intercâmbio é invertida somente na visualização, sem modificar o valor original do ONS armazenado.</p></section>
</main><script>
const KEYS=['termica','eolica','solar','hidraulica','intercambio','carga'];
const LABELS=['Térmica','Eólica','Solar','Hidráulica','Intercâmbio','Carga'];
const CORES=['#E18B51','#6CB7D2','#EACB59','#2B9C89','#9987BB','#23344D'];
const REGIOES=[['sin','SIN'],['se','SE'],['s','S'],['ne','NE'],['n','N']];
const PERIODOS=[['dia','Dia anterior'],['semana','Semana anterior'],['mes','Mês anterior'],['ano','Ano anterior'],['data','Escolher data…']];
const fmt=(n,dp=0)=>n==null||!Number.isFinite(n)?'—':n.toLocaleString('pt-BR',{minimumFractionDigits:dp,maximumFractionDigits:dp});
let DAILY=[], MONTHLY=[], YEARS=[], CACHE={}, ANNUAL={}, state=[3,0,1,2,5].map(metric=>({periodo:'mes',metric,data:null}));
const REC_SELECTED=new Set([5]);let ANOS_SELECTED=new Set();
const MESES=['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
const DASH=['solid','dash','dot','dashdot','longdash','longdashdot'];
const COR_ANO=(ano)=>`hsl(${Math.round(((ano-2000)*137.508)%360)}, 66%, 43%)`;
const msDay=86400000;
function addMonths(iso,n){let [y,m,d]=iso.split('-').map(Number);m+=n;while(m<=0){m+=12;y--}while(m>12){m-=12;y++};d=Math.min(d,new Date(Date.UTC(y,m,0)).getUTCDate());return `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`}
function refDate(day,p){if(p==='mes')return addMonths(day,-1);if(p==='ano')return addMonths(day,-12);let d=new Date(day+'T12:00:00Z');d=new Date(d.getTime()-(p==='semana'?7:1)*msDay);return d.toISOString().slice(0,10)}
function prior(day){let lo=0,hi=DAILY.length-1,found=null;while(lo<=hi){let mid=Math.floor((lo+hi)/2);if(DAILY[mid][0]<=day){found=DAILY[mid];lo=mid+1}else hi=mid-1}return found}
function renderCards(){let box=document.getElementById('cards');if(!DAILY.length){box.innerHTML='<p>Sem histórico diário consolidado disponível.</p>';return}
 const latest=DAILY[DAILY.length-1];box.innerHTML=state.map((s,i)=>{
  const defaultDate=refDate(latest[0],'mes');
  const alvo=s.periodo==='data'?(s.data||defaultDate):refDate(latest[0],s.periodo);
  let old=prior(alvo);let cur=latest[1+s.metric],before=old?old[1+s.metric]:null;let diff=old?cur-before:null;
  let pp=old&&Number.isFinite(before)&&before!==0?(cur/before-1)*100:null;
  let klass=diff==null?'':diff>=0?'positivo':'negativo';let sel=PERIODOS.map(([v,l])=>`<option value="${v}" ${s.periodo===v?'selected':''}>${l}</option>`).join('');
  return `<article class="card"><h3>${LABELS[s.metric]}</h3><select class="select" data-card="${i}" aria-label="Comparar ${LABELS[s.metric]}">${sel}</select>
  <input type="date" class="select calendario-card" data-card-data="${i}" min="${DAILY[0][0]}" max="${latest[0]}" value="${s.data||defaultDate}" ${s.periodo==='data'?'':'hidden'} aria-label="Data de comparação ${LABELS[s.metric]}">
  <div class="valor">${fmt(cur)} <span class="unidade">MWmed</span></div><div class="linha"><span>Referência ${old?old[0].split('-').reverse().join('/'):'—'}</span><strong>${fmt(before)} MWmed</strong></div>
  <div class="linha"><span>Δ (MWmed)</span><span class="delta ${klass}">${diff==null?'—':(diff>=0?'+':'')+fmt(diff)}</span></div><div class="linha"><span>Δ (%)</span><span class="delta ${pp==null?'':pp>=0?'positivo':'negativo'}">${pp==null?'—':(pp>=0?'+':'')+fmt(pp,2)}${pp==null?'':'%'}</span></div></article>`
 }).join('');box.querySelectorAll('select[data-card]').forEach(el=>el.addEventListener('change',()=>{state[Number(el.dataset.card)].periodo=el.value;renderCards()}));
 box.querySelectorAll('input[data-card-data]').forEach(el=>el.addEventListener('change',()=>{state[Number(el.dataset.cardData)].data=el.value;renderCards()}))}
async function yearData(y){if(CACHE[y])return CACHE[y];const r=await fetch(`operacao_sin/${y}.json`);if(!r.ok)throw Error('Dados anuais indisponíveis: '+y);CACHE[y]=await r.json();return CACHE[y]}
function renderLegend(){} // cada Plotly possui sua própria legenda interativa
function regionalData(doc,id,pos,end){
 if(id==='sin'&&doc.sin)return doc.sin.slice(pos,end);
 if(!doc.s)return null;
 if(id==='sin'){
  return Array.from({length:end-pos},(_,j)=>KEYS.map((_,m)=>['SE','S','NE','N'].reduce((acc,sm)=>acc+(doc.s[sm][pos+j][m]||0),0)))
 }
 const sub={'se':'SE','s':'S','ne':'NE','n':'N'}[id];return doc.s[sub]?doc.s[sub].slice(pos,end):null
}
// Os arquivos persistidos preservam a convenção de sinal original do ONS.
// A apresentação usa a convenção invertida de intercâmbio solicitada no dashboard.
const valorParaGrafico=(metric,valor)=>metric===4?-valor:valor;
function chartTraces(xs,rows){return KEYS.map((k,i)=>({
 type:'scatter',mode:'lines',uid:KEYS[i],name:LABELS[i],x:xs,y:rows.map(v=>valorParaGrafico(i,v[i])),
 visible:i===4?'legendonly':true,
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
  const traces=chartTraces(xs,rows); // Carga = rows[i][5], valor original do ONS, nunca soma das fontes.
  await Plotly.react(target,traces,{height:id==='sin'?560:470,showlegend:true,margin:{t:24,b:100,l:84,r:18},paper_bgcolor:'#fff',plot_bgcolor:'#fff',
   xaxis:{title:'Hora',type:'date',tickformat:'%H:%M',dtick:7200000,showgrid:false,showline:true,linecolor:'#94a3b8'},
   yaxis:{title:'Energia MWm',tickformat:',.0f',gridcolor:'#edf1f5',zeroline:true,zerolinecolor:'#94a3b8'},
   legend:{orientation:'h',x:.5,xanchor:'center',y:-.28,yanchor:'top',font:{size:10},itemclick:'toggle',itemdoubleclick:'toggleothers'},font:{family:'Century Gothic,Arial',color:'#344054',size:12},hovermode:'x unified',hoverlabel:{bgcolor:'#f7f9fc'},uirevision:'operacao-'+id},{responsive:true,displaylogo:false});
  const residual=rows.map(v=>v[5]-v.slice(0,5).reduce((a,z,i)=>a+valorParaGrafico(i,z),0));const maxres=Math.max(...residual.map(Math.abs));
  aviso.textContent=maxres>1000?`Diferença máxima entre carga e geração + intercâmbio: ${fmt(maxres)} MWmed (dados ONS sem ajuste).`:'';
 }));
 status.textContent=`${xs.length} horas • ${day.split('-').reverse().join('/')}`;
}catch(e){status.textContent='Não foi possível carregar '+day+': '+e.message;console.error(e)}}
function renderMensalFiltros(){
 const r=document.getElementById('filtro-recursos');
 r.innerHTML=LABELS.map((t,i)=>`<button type="button" class="chip-filtro" data-recurso="${i}" aria-pressed="${REC_SELECTED.has(i)}">${t}</button>`).join('');
 r.querySelectorAll('button').forEach(b=>b.onclick=()=>{let k=Number(b.dataset.recurso);if(REC_SELECTED.has(k)){if(REC_SELECTED.size>1)REC_SELECTED.delete(k)}else REC_SELECTED.add(k);renderMensalFiltros();renderMensal()});
 const anos=[...new Set(MONTHLY.map(r=>r[0]))].sort((a,b)=>a-b);
 const a=document.getElementById('filtro-anos');
 a.innerHTML=anos.map(y=>`<button type="button" class="chip-filtro chip-ano" data-ano="${y}" aria-pressed="${ANOS_SELECTED.has(y)}"><span class="ponto-ano" style="background:${COR_ANO(y)}"></span>${y}</button>`).join('');
 a.querySelectorAll('button').forEach(b=>b.onclick=()=>{let y=Number(b.dataset.ano);if(ANOS_SELECTED.has(y)){if(ANOS_SELECTED.size>1)ANOS_SELECTED.delete(y)}else ANOS_SELECTED.add(y);renderMensalFiltros();renderMensal()});
}
// Média anual ponderada pelo número de dias completos: evita atribuir o
// mesmo peso a fevereiro e a meses com 31 dias. O ano corrente é parcial.
function montarMediasAnuais(){
 const soma={}, contagem={};
 for(const linha of DAILY){
  const ano=Number(linha[0].slice(0,4));
  if(!soma[ano]){soma[ano]=Array(KEYS.length).fill(0);contagem[ano]=Array(KEYS.length).fill(0)}
  for(let k=0;k<KEYS.length;k++){
   const valor=Number(linha[1+k]);
   if(Number.isFinite(valor)){soma[ano][k]+=valor;contagem[ano][k]++}
  }
 }
 ANNUAL={};
 for(const ano of Object.keys(soma))ANNUAL[ano]=soma[ano].map((v,k)=>contagem[ano][k]?v/contagem[ano][k]:null);
}
function renderMensal(){
 const traces=[];
 for(const ano of [...ANOS_SELECTED].sort((a,b)=>a-b)){
  const items=MONTHLY.filter(r=>r[0]===ano).sort((a,b)=>a[1]-b[1]);
  for(const k of [...REC_SELECTED].sort((a,b)=>a-b)){
   const cor=COR_ANO(ano), grupo=`${ano}-${KEYS[k]}`;
   // Apenas os 12 meses ficam ligados por linhas.
   traces.push({type:'scatter',mode:'lines+markers',name:`${ano} · ${LABELS[k]}`,
    legendgroup:grupo,x:items.map(r=>r[1]),y:items.map(r=>valorParaGrafico(k,r[2+k])),
    line:{color:cor,width:ano===Math.max(...ANOS_SELECTED)?3:2,dash:DASH[k],shape:'spline',smoothing:.55},
    marker:{size:5,color:cor},
    hovertemplate:`<b>${ano} · ${LABELS[k]}</b><br>Mês: %{x}<br>%{y:,.1f} MWm<extra></extra>`});
   // Ponto 13 = média anual isolada: não liga dezembro ao ano por uma linha.
   const media=ANNUAL[ano]?.[k];
   if(media!=null){
    traces.push({type:'scatter',mode:'markers',showlegend:false,legendgroup:grupo,
     name:`${ano} · ${LABELS[k]} — Ano`,x:[13],y:[valorParaGrafico(k,media)],
     marker:{color:cor,symbol:'diamond',size:12,line:{color:'#fff',width:1.2}},
     hovertemplate:`<b>${ano} · ${LABELS[k]}</b><br>Média anual: %{y:,.1f} MWm<extra></extra>`});
   }
  }
 }
 Plotly.react('chart-mensal',traces,{height:540,showlegend:true,hovermode:'closest',paper_bgcolor:'#fff',plot_bgcolor:'#fff',
 margin:{t:16,l:92,r:24,b:95},font:{family:'Century Gothic,Arial',size:12,color:'#344054'},
 xaxis:{title:'Mês',range:[.7,13.35],tickmode:'array',tickvals:[1,2,3,4,5,6,7,8,9,10,11,12,13],ticktext:[...MESES,'Ano'],showline:true,linecolor:'#94a3b8',showgrid:false},
 yaxis:{title:'Energia (MWm)',showline:true,linecolor:'#94a3b8',gridcolor:'#e7edf2',tickformat:',.0f'},
 legend:{orientation:'h',x:.5,xanchor:'center',y:-.27,yanchor:'top',font:{size:10},groupclick:'togglegroup'}},{responsive:true,displaylogo:false});
}
async function main(){renderLegend();try{
 const r=await fetch('operacao_sin/resumo.json');if(!r.ok)throw Error('Resumo não encontrado');const o=await r.json();DAILY=o.daily||[];MONTHLY=o.monthly||[];YEARS=o.years||[];montarMediasAnuais();
 const ys=[...new Set(MONTHLY.map(r=>r[0]))].sort((a,b)=>a-b);ANOS_SELECTED=new Set(ys.slice(-2));if(!ANOS_SELECTED.size&&ys.length)ANOS_SELECTED.add(ys[ys.length-1]);renderMensalFiltros();if(MONTHLY.length)renderMensal();
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
