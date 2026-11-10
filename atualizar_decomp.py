"""Painel Projeções Decomp: lê dados/bd_rodada_decomp.xlsx e históricos locais.
Gera gráficos/decomp.html e integra botão em gráficos/index.html, sem coletar web.
Requer pandas e plotly (dependências existentes do projeto) e openpyxl para ler XLSX.
"""
from __future__ import annotations
import json, re, calendar
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from plotly.offline import get_plotlyjs

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'dados'
PLOTS = ROOT / 'graficos'
INPUT = DATA / 'bd_rodada_decomp.xlsx'
OUT = PLOTS / 'decomp.html'
INDEX = PLOTS / 'index.html'
MONTHS = {'jan':1,'fev':2,'mar':3,'abr':4,'mai':5,'jun':6,'jul':7,'ago':8,'set':9,'out':10,'nov':11,'dez':12}
SUBS = {'SE/CO':'SE/CO','SUL':'SUL','NE':'NE','NORDESTE':'NE','N':'NORTE','NORTE':'NORTE','SIN':'SIN'}


def month_key(x):
    if isinstance(x, (datetime, pd.Timestamp)): return x.strftime('%Y-%m')
    s = str(x).strip().lower()
    m = re.match(r'([a-zç]{3})[/\-](\d{4})$', s)
    if m and m.group(1) in MONTHS: return f'{m.group(2)}-{MONTHS[m.group(1)]:02d}'
    t = pd.to_datetime(x, errors='coerce', dayfirst=True)
    if pd.isna(t): raise ValueError(f'Data Time não reconhecida: {x!r}')
    return t.strftime('%Y-%m')


def identify(label):
    label = ' '.join(str(label).split())
    checks = [
        (r'^PLD\s+(SE/CO|SUL|NE|NORDESTE|N|NORTE)\s+(.+)$','PLD'),
        (r'^ENA\s+(SE/CO|SUL|NE|NORDESTE|N|NORTE|SIN)\s+%\s+(.+)$','ENA'),
        (r'^EARf\s+(SE/CO|SUL|NE|NORDESTE|N|NORTE|SIN)\s+(.+)$','EAR'),
        (r'^GSF\s+%\s+(.+)$','GSF'),
    ]
    for pat,var in checks:
        m = re.match(pat,label,re.I)
        if m:
            if var=='GSF': return (var,'SIN',m.group(1))
            return (var,SUBS[m.group(1).upper()],m.group(2))
    return None


def number(s):
    if isinstance(s, str):
        s=s.strip()
        if ',' in s: s=s.replace('.','').replace(',','.')
    return pd.to_numeric(s,errors='coerce')


def read_projection():
    if not INPUT.exists(): raise FileNotFoundError(f'Arquivo obrigatório ausente: {INPUT}')
    df = pd.read_excel(INPUT, engine='openpyxl')
    required={'Time','Variável','Valor'}
    if not required.issubset(df.columns): raise ValueError(f'Colunas esperadas: {required}; recebidas {df.columns.tolist()}')
    result={}; unknown=set(); scenarios=set(); conflicts=[]
    for _,r in df.iterrows():
        p=identify(r['Variável'])
        if p is None:
            unknown.add(str(r['Variável']));continue
        var,sub,sc=p;mon=month_key(r['Time']); v=number(r['Valor'])
        if pd.isna(v):continue
        key=f'{var}|{sub}|{sc}'
        series=result.setdefault(key,{})
        if mon in series and abs(series[mon]-float(v))>0.00001: conflicts.append((key,mon))
        series[mon]=float(v);scenarios.add(sc)
    if conflicts: raise ValueError(f'Valores conflitantes na mesma variável/cenário/mês: {conflicts[:8]}')
    if not result: raise ValueError('Nenhuma variável PLD, GSF, ENA ou EARf reconhecida.')
    return result,sorted(scenarios,key=str.lower),sorted(unknown)


def read_history():
    out={}
    def store(var,sub,mon,v):
        if pd.notna(v):out.setdefault(f'{var}|{sub}',{})[mon]=round(float(v),6)
    p=DATA/'pld_historico_long.csv'
    if p.exists():
        d=pd.read_csv(p,sep=';',decimal=',',low_memory=False)
        d['data']=pd.to_datetime(d['data'],errors='coerce')
        d['pld']=pd.to_numeric(d['pld'],errors='coerce')
        d=d.dropna(subset=['data','pld'])
        d['mes']=d['data'].dt.strftime('%Y-%m')
        # Cada registro horário tem o mesmo peso. Médias só usam horas observadas.
        for (sub,mon),g in d.groupby(['submercado','mes']):
            sub=SUBS.get(str(sub).upper())
            if sub in ('SE/CO','SUL','NE','NORTE'):store('PLD',sub,mon,g['pld'].mean())
    p=DATA/'ena_mensal.csv'
    if p.exists():
        d=pd.read_csv(p,sep=';',decimal=',')
        for _,r in d.iterrows():
            mon=f'{int(r["ano"]):04d}-{int(r["mes"]):02d}'
            for sub,c in [('SE/CO','enasepp'),('SUL','enasupp'),('NE','enanepp'),('NORTE','enanpp'),('SIN','enasinpp')]:
                store('ENA',sub,mon,number(r[c]))
    p=DATA/'ear_mensal.csv'
    if p.exists():
        d=pd.read_csv(p,sep=';',decimal=',')
        for _,r in d.iterrows():
            mon=f'{int(r["ano"]):04d}-{int(r["mes"]):02d}'
            for sub,c in [('SE/CO','earse'),('SUL','earsu'),('NE','earne'),('NORTE','earn'),('SIN','earsin')]:
                store('EAR',sub,mon,number(r[c]))
    # GSF histórico não está disponível no dataset, não criar realizado artificial.
    return out


def inject_index():
    if not INDEX.exists():return
    txt=INDEX.read_text(encoding='utf-8')
    btn='<button role="tab" id="botao-decomp" aria-selected="false" aria-controls="aba-decomp" onclick="selecionar(\'decomp\')">Projeções Decomp</button>'
    sec='<section class="aba" id="aba-decomp" role="tabpanel" aria-labelledby="botao-decomp"><div class="molde"><iframe id="quadro-decomp" title="Projeções Decomp" src="decomp.html" loading="lazy" style="height:4200px"></iframe></div></section>'
    if 'id="botao-decomp"' not in txt:
        if 'id="botao-newave"' in txt:
            txt=re.sub(r'(<button\s+role="tab"\s+id="botao-newave"[^>]*>.*?</button>)',r'\1\n'+btn,txt,count=1,flags=re.S)
        else:txt=txt.replace('</nav>',btn+'\n</nav>',1)
    if 'id="aba-decomp"' not in txt: txt=txt.replace('</main>',sec+'\n</main>',1)
    if 'id="botao-decomp"' not in txt or 'id="aba-decomp"' not in txt: raise ValueError('Falha ao integrar o menu Decomp')
    INDEX.write_text(txt,encoding='utf-8')

HTML_HEAD='''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Projeções Decomp — Acompanhamento de mercado</title>
<style>
:root{--teal:#007e80;--ink:#193345;--muted:#758399;--line:#dae4ed;--bg:#f7fafc}*{box-sizing:border-box}body{margin:0;background:#fff;font-family:"Century Gothic",CenturyGothic,Arial,sans-serif;color:var(--ink)}main{max-width:1480px;margin:0 auto;padding:10px 14px 24px}.intro{margin:4px 5px 17px;font-size:13px;color:#64748b;line-height:1.7}.section{border:1px solid var(--line);border-radius:15px;overflow:hidden;margin-bottom:22px;background:white}.bar{background:var(--teal);color:white;text-align:center;padding:13px;font-size:18px;font-weight:800}.sub{padding:10px 15px 4px;font-size:12px;color:#768397;text-align:center}.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:13px 16px 7px}.controls .grp{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.grp strong{font-size:12px;white-space:nowrap}.pill{border:1px solid #cbd9e2;border-radius:20px;background:#f8fafc;padding:7px 12px;cursor:pointer;font-size:12px;color:#536579;font-family:inherit}.pill.on{border-color:#77b7bc;background:#e8f5f4;color:#076c6e;font-weight:700}.view{display:flex;align-items:center;gap:8px;margin-left:auto}.view select{font:inherit;font-size:12px;border:1px solid #cbd9e2;border-radius:9px;padding:8px;color:#193345;background:white}.export{border:1px solid var(--teal);color:var(--teal);background:#fff;border-radius:9px;padding:8px 12px;cursor:pointer;font:inherit;font-size:12px;font-weight:700}.export:hover{background:#e8f5f4}.graph{width:100%;height:510px}.graph.small{height:390px}.subgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px;padding:10px 14px 16px}.minipanel{border:1px solid var(--line);border-radius:12px;overflow:hidden}.minihead{text-align:center;color:white;background:#007e80;font-weight:700;padding:9px;font-size:13px}.hint{padding:3px 17px 14px;color:#718096;font-size:11px;line-height:1.65}.historical{color:#708092;font-size:12px;padding:5px 18px 10px}.legendkey{color:#64748b}.status{font-size:11px;color:#64748b;padding:4px 16px 8px}.summary-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(370px,1fr));gap:14px;padding:14px}.year-card{border:1px solid var(--line);border-radius:13px;overflow:hidden;background:#fff}.year-head{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:13px 15px;background:#eef8f7;border-bottom:1px solid #cce6e5}.year-head strong{font-size:19px;color:#007e80}.year-head span{font-size:11px;color:#506878;text-align:center;grid-column:2}.year-head::after{content:"";grid-column:3}.sum-table th:nth-child(3),.sum-table td:nth-child(3){text-align:center;white-space:nowrap}.year-content{padding:9px 13px 14px;overflow-x:auto}.summary-var{padding:8px 0 10px;border-bottom:1px solid #e4ebf0}.summary-var:last-child{border-bottom:0}.summary-var h3{font-size:13px;color:#0b6f72;margin:2px 0 8px}.sum-table{width:100%;min-width:590px;border-collapse:collapse;font-size:11px;table-layout:auto}.sum-table th{background:#f2f6fa;color:#5c7082;font-weight:700;text-align:left}.sum-table th,.sum-table td{padding:6px 5px;border-bottom:1px solid #edf1f5;overflow-wrap:anywhere}.sum-table td:last-child,.sum-table th:last-child{text-align:right;font-weight:700}.sum-table td:nth-child(2){color:#526778}.sum-table th:nth-child(4),.sum-table td:nth-child(4){text-align:center}.series-legend{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;column-gap:9px;row-gap:7px;padding:13px 14px 4px;min-height:38px}.series-legend .legend-item{display:inline-flex;gap:7px;align-items:center;cursor:pointer;border:1px solid #d6e1e9;border-radius:17px;background:#fff;color:#43576a;font:inherit;font-size:11px;padding:6px 10px;white-space:normal;text-align:left}.series-legend .legend-item:hover{border-color:#007e80}.series-legend .legend-item[aria-pressed="false"]{opacity:.45;text-decoration:line-through}.legend-stroke{display:inline-block;min-width:23px;width:23px;border-top:3px solid var(--series-color)}.legend-stroke.historic{border-top-style:dashed}.scenario-label{display:inline-flex;align-items:center;gap:5px}.scenario-dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex:none}.summary-empty{padding:20px;color:#64748b;font-size:13px}.summary-info{padding:0 16px 12px;font-size:11px;color:#687a8a;line-height:1.55}.warning{background:#fff8e9;color:#855917;border:1px solid #e8d6a8;border-radius:7px;padding:9px 13px;margin:8px 16px;font-size:12px}@media(max-width:850px){.summary-cards{grid-template-columns:1fr;padding:9px}.subgrid{grid-template-columns:1fr}.graph{height:440px}.controls{gap:8px}.view{margin-left:0}.pill{padding:6px 9px}.section{margin-bottom:15px}}
</style><script>'''

HTML_BODY='''</script></head><body><main><section class="section" id="panel-summary"><div class="bar">Projeções Decomp - Resumo</div><p class="sub">Comparativo por ano, cenário, variável e submercado</p><div class="controls" id="summary-filters"></div><div class="summary-cards" id="summary-cards"></div><div class="summary-info">A coluna de média projetada considera somente os meses da rodada. Quando a projeção não abrange todos os 12 meses, a coluna de ano fechado combina histórico anterior à projeção e meses projetados, e só apresenta média quando há 12 meses disponíveis; caso contrário, mostra —. Se todos os 12 meses do ano forem projetados, essa coluna é omitida. PLD e GSF: ponderação por horas; ENA: por dias. EARf: valor de setembro ou do mês projetado mais próximo, identificado na tabela.</div></section><div id="sections"></div></main><script>
const DATA=__DATA__;
const MESES=['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
const VARIABLES=['PLD','GSF','ENA','EAR'];
const OPTIONS={PLD:['SE/CO','SUL','NE','NORTE'],GSF:['SIN'],ENA:['SIN','SE/CO','SUL','NE','NORTE'],EAR:['SIN','SE/CO','SUL','NE','NORTE']};
const NAMES={PLD:'PLD (R$/MWh)',GSF:'GSF (%)',ENA:'ENA (% MLT)',EAR:'EARf — Níveis dos reservatórios (%)'};
const COLORS=['#ec7900','#2364ee','#8d50c4','#008f85','#b33b52','#799398','#b3bdc6','#b0bd99'];
const SC_COLORS=['#2364ee','#ec7900','#7c4ac7','#139485','#b33b52'];
const YEARS=Array.from(new Set([...Object.values(DATA.historico).flatMap(x=>Object.keys(x).map(y=>Number(y.slice(0,4)))),...Object.values(DATA.projecoes).flatMap(x=>Object.keys(x).map(y=>Number(y.slice(0,4))))])).sort((a,b)=>a-b);
const FORECAST_YEARS=Array.from(new Set(Object.values(DATA.projecoes).flatMap(x=>Object.keys(x).map(y=>Number(y.slice(0,4)))))).sort();
const DEFAULT_YEARS=[...new Set([YEARS.at(-3),...FORECAST_YEARS])].filter(Boolean);
const SELECTED={};const TABS={};const SUMMARY_SELECTED=new Set(['li','ve','ls'].filter(sc=>DATA.cenarios.includes(sc)));const SUMMARY_SUBS=new Set(['SE/CO']);
for(const k of VARIABLES){SELECTED[k]={sc:new Set(DATA.cenarios.filter(c=>c==='ve').length?['ve']:[DATA.cenarios[0]]),years:new Set(DEFAULT_YEARS),subs:new Set(k==='PLD'?['SE/CO']:OPTIONS[k]),view:'mensal',source:'ambos'};}
function escapeHtml(s){return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
function num(n,d=2){return n==null||!Number.isFinite(n)?'—':n.toLocaleString('pt-BR',{maximumFractionDigits:d,minimumFractionDigits:d})}
function last(a){return a.length?a[a.length-1]:null}
function summaryAverage(k,year,entries){let vals=entries.map(([m,v])=>{let month=Number(m.slice(5,7));let days=new Date(Date.UTC(year,month,0)).getUTCDate();return {v,days}});if(!vals.length)return null;let weights=vals.map(o=>k==='EAR'?1:(k==='PLD'||k==='GSF'?o.days*24:o.days));let total=weights.reduce((a,b)=>a+b,0);return {value:vals.reduce((a,o,i)=>a+o.v*weights[i],0)/total,count:vals.length};}
function projMonths(k,sub,sc,year){let byMonth=DATA.projecoes[k+'|'+sub+'|'+sc]||{};return Object.entries(byMonth).filter(([m,v])=>m.startsWith(year+'-')&&Number.isFinite(v)).sort((a,b)=>a[0].localeCompare(b[0]));}
function fmtMonth(ym){return MESES[Number(ym.slice(5))-1].toLowerCase()+'/'+ym.slice(2,4)}
function monthSpan(entries){return entries.length?fmtMonth(entries[0][0])+' a '+fmtMonth(entries[entries.length-1][0]):'—'}
function summarySeries(k,sub,sc,year){let entries=projMonths(k,sub,sc,year);
 if(!entries.length)return null;
 if(k==='EAR'){let chosen=[...entries].sort((a,b)=>Math.abs(Number(a[0].slice(5))-9)-Math.abs(Number(b[0].slice(5))-9)||a[0].localeCompare(b[0]))[0];return {value:chosen[1],count:1,reference:fmtMonth(chosen[0]),span:monthSpan(entries)};}
 let p=summaryAverage(k,year,entries);
 // Só identifica como ano fechado quando existem 12 meses combinando histórico anterior à projeção e previsões.
 let combined=baseline(k,sub,year,sc,'ambos');let total=annual(k,year,combined.values);
 return {...p,span:monthSpan(entries),fullYear:total&&total.count===12?total.value:null};}
function renderSummary(){
 let filters=document.getElementById('summary-filters');
 let subOptions=['SIN','SE/CO','SUL','NE','NORTE'];
 let fmtSub=s=>s==='NE'?'Nordeste':s==='NORTE'?'Norte':s==='SUL'?'Sul':s;
 filters.innerHTML='<div class="grp"><strong>Cenários:</strong>'+DATA.cenarios.map(sc=>`<button type="button" aria-pressed="${SUMMARY_SELECTED.has(sc)}" class="pill ${SUMMARY_SELECTED.has(sc)?'on':''}" data-summary-sc="${escapeHtml(sc)}">${escapeHtml(sc)}</button>`).join('')+'</div>'+
 '<div class="grp"><strong>Submercados:</strong>'+subOptions.map(sub=>`<button type="button" aria-pressed="${SUMMARY_SUBS.has(sub)}" class="pill ${SUMMARY_SUBS.has(sub)?'on':''}" data-summary-sub="${sub}">${fmtSub(sub)}</button>`).join('')+'</div>';
 filters.querySelectorAll('[data-summary-sc]').forEach(b=>b.addEventListener('click',()=>{let sc=b.dataset.summarySc;if(SUMMARY_SELECTED.has(sc))SUMMARY_SELECTED.delete(sc);else SUMMARY_SELECTED.add(sc);renderSummary();adjustHeight()}));
 filters.querySelectorAll('[data-summary-sub]').forEach(b=>b.addEventListener('click',()=>{let sub=b.dataset.summarySub;if(SUMMARY_SUBS.has(sub))SUMMARY_SUBS.delete(sub);else SUMMARY_SUBS.add(sub);renderSummary();adjustHeight()}));
 let cards=document.getElementById('summary-cards');let scenarios=[...SUMMARY_SELECTED];
 if(!scenarios.length){cards.innerHTML='<div class="summary-empty">Selecione ao menos um cenário para visualizar as médias projetadas.</div>';return;}
 cards.innerHTML=FORECAST_YEARS.map(year=>{
  // O intervalo do card acompanha os cenários e os submercados atualmente selecionados.
  let selectedMonths=[];
  for(let k of VARIABLES){let subs=k==='GSF'?['SIN']:OPTIONS[k].filter(s=>SUMMARY_SUBS.has(s));for(let sc of scenarios)for(let sub of subs)selectedMonths.push(...projMonths(k,sub,sc,year).map(row=>row[0]));}
  selectedMonths=[...new Set(selectedMonths)].sort();let yearSpan=selectedMonths.length?fmtMonth(selectedMonths[0])+' a '+fmtMonth(selectedMonths[selectedMonths.length-1]):'—';
  let sections=VARIABLES.map(k=>{
   let subs=k==='GSF'?['SIN']:OPTIONS[k].filter(s=>SUMMARY_SUBS.has(s));
   // Esconde a coluna do ano fechado quando todos os produtos/cenários exibidos têm os 12 meses de projeção.
   let records=[];
   for(let sc of scenarios)for(let sub of subs){let v=summarySeries(k,sub,sc,year);if(v)records.push({sc,sub,v,projCount:projMonths(k,sub,sc,year).length});}
   const showFullYear=k!=='EAR'&&records.some(r=>r.projCount<12);
   let rows=records.map(({sc,sub,v})=>{let color=SC_COLORS[Math.max(0,DATA.cenarios.indexOf(sc))%SC_COLORS.length];let projAverageLabel=k==='EAR'?num(v.value)+' ('+v.reference+')':num(v.value);return `<tr><td>${fmtSub(sub)}</td><td><span class="scenario-label"><i class="scenario-dot" style="background:${color}"></i>${escapeHtml(sc)}</span></td><td>${v.span}</td><td>${projAverageLabel}</td>${showFullYear?`<td>${v.fullYear===null?'—':num(v.fullYear)}</td>`:''}</tr>`});
   let colTitle=k==='EAR'?'Valor (mês ref.)':`Média (${yearSpan})`;
   return rows.length?`<section class="summary-var"><h3>${NAMES[k]}</h3><table class="sum-table"><thead><tr><th>Submercado</th><th>Cenário</th><th>Meses de projeção</th><th>${colTitle}</th>${showFullYear?'<th>Média ano fechado (histórico + projeções)</th>':''}</tr></thead><tbody>${rows.join('')}</tbody></table></section>`:'';
  }).filter(Boolean).join('');
  return `<article class="year-card"><div class="year-head"><strong>${year}</strong><span>Meses de projeção: ${yearSpan}</span></div><div class="year-content">${sections||'<p class="summary-empty">Sem projeções para a combinação de filtros selecionada.</p>'}</div></article>`;
 }).join('');adjustHeight();}
function opts(k){let z=SELECTED[k];return '<div class="controls"><div class="grp"><strong>Cenários:</strong>'+DATA.cenarios.map(sc=>`<button class="pill ${z.sc.has(sc)?'on':''}" data-kind="sc" data-var="${k}" data-id="${escapeHtml(sc)}">${escapeHtml(sc)}</button>`).join('')+'</div>'+(k==='PLD'?'<div class="grp"><strong>Submercados:</strong>'+OPTIONS[k].map(s=>`<button class="pill ${z.subs.has(s)?'on':''}" data-kind="sub" data-var="${k}" data-id="${s}">${s==='NE'?'Nordeste':s==='NORTE'?'Norte':s==='SUL'?'Sul':s}</button>`).join('')+'</div>':'')+`<div class="view"><strong>Dados:</strong><button type="button" aria-pressed="${z.source!=='projecoes'}" class="pill ${z.source!=='projecoes'?'on':''}" data-kind="source-historico" data-var="${k}">Histórico</button><button type="button" aria-pressed="${z.source!=='historico'}" class="pill ${z.source!=='historico'?'on':''}" data-kind="source-projecoes" data-var="${k}">Projeções</button><select aria-label="Visualização ${k}" data-kind="view" data-var="${k}"><option value="mensal" ${z.view==='mensal'?'selected':''}>Mensal + Ano</option><option value="anual" ${z.view==='anual'?'selected':''}>Consolidado anual</option></select><button class="export" data-kind="export" data-var="${k}">Exportar Excel</button></div></div><div class="controls" style="padding-top:2px"><div class="grp"><strong>Anos:</strong>${YEARS.map(y=>`<button class="pill ${z.years.has(y)?'on':''}" data-kind="year" data-var="${k}" data-id="${y}">${y}</button>`).join('')}</div></div>`}
function buildUI(){document.getElementById('sections').innerHTML=VARIABLES.map(k=>`<section class="section" id="panel-${k}"><div class="bar">${NAMES[k]}</div><div class="sub">${k==='PLD'?'Preços mensais por submercado e cenário':k==='GSF'?'Projeção do GSF — sem divisão por submercados':'Projeção do SIN e dos quatro submercados'}</div><div id="filters-${k}"></div><div id="plots-${k}"></div><div class="status" id="status-${k}"></div><div class="hint">Fonte: históricos / projeções</div></section>`).join('');
 for(const k of VARIABLES){document.getElementById('plots-'+k).innerHTML=(k==='ENA'||k==='EAR')?`<div id="chart-${k}-SIN" class="graph"></div><div class="subgrid">${['SE/CO','SUL','NE','NORTE'].map(s=>`<div class="minipanel"><div class="minihead">${s==='NE'?'Nordeste':s==='NORTE'?'Norte':s==='SUL'?'Sul':s}</div><div class="graph small" id="chart-${k}-${s.replace('/','')}"></div></div>`).join('')}</div>`:'<div id="chart-'+k+'-single" class="graph"></div>';}
 document.querySelectorAll('button[data-kind]').forEach(b=>b.addEventListener('click',handleClick));document.querySelectorAll('select[data-kind]').forEach(b=>b.addEventListener('change',handleSelect));redrawAll();}
function handleClick(e){const b=e.currentTarget,k=b.dataset.var,type=b.dataset.kind,id=b.dataset.id,z=SELECTED[k];if(type==='export'){exportExcel(k);return;}if(type==='source-historico'||type==='source-projecoes'){let h=z.source!=='projecoes',p=z.source!=='historico';if(type==='source-historico'){if(h&&p)h=false;else if(!h)h=true;}else{if(h&&p)p=false;else if(!p)p=true;}z.source=h&&p?'ambos':h?'historico':'projecoes';refreshFilters(k);draw(k);return;}let set=type==='sc'?z.sc:type==='sub'?z.subs:z.years;let v=type==='year'?Number(id):id;if(set.has(v))set.delete(v);else set.add(v);refreshFilters(k);draw(k);}
function handleSelect(e){let b=e.currentTarget,k=b.dataset.var;SELECTED[k][b.dataset.kind]=b.value;refreshFilters(k);draw(k)}
function refreshFilters(k){let el=document.getElementById('filters-'+k);el.innerHTML=opts(k);el.querySelectorAll('button[data-kind]').forEach(b=>b.addEventListener('click',handleClick));el.querySelectorAll('select[data-kind]').forEach(b=>b.addEventListener('change',handleSelect));}
// A visualização da origem não mistura cotação projetada com realizado quando o usuário exige apenas uma fonte.
function baseline(k,sub,y,sc,mode){let h=DATA.historico[k+'|'+sub]||{};let f=DATA.projecoes[k+'|'+sub+'|'+sc]||{};
 let months=Object.keys(f).sort(),first=months[0]||'9999-99';let values=[],sources=[];
 for(let i=1;i<=12;i++){let ym=`${y}-${String(i).padStart(2,'0')}`,hasF=Object.hasOwn(f,ym),hasH=Object.hasOwn(h,ym);
  if(mode==='projecoes'&&hasF){values.push(f[ym]);sources.push('Projeção · '+sc)}
  else if(mode==='historico'&&hasH){values.push(h[ym]);sources.push('Histórico')}
  else if(mode==='ambos'&&hasF){values.push(f[ym]);sources.push('Projeção · '+sc)}
  else if(mode==='ambos'&&hasH&&ym<first){values.push(h[ym]);sources.push('Histórico')}
  else{values.push(null);sources.push(null)}
 }return {values,sources};}
function annual(varName,year,values){let valid=values.map((v,i)=>({v,i})).filter(p=>p.v!==null&&Number.isFinite(p.v));if(!valid.length)return null;
 if(varName==='EAR'){let z=last(valid);return {value:z.v,count:valid.length,method:'Posição no último mês disponível ('+MESES[z.i]+')'}}
 let weights=valid.map(p=>new Date(Date.UTC(year,p.i+1,0)).getUTCDate()*(varName==='PLD'||varName==='GSF'?24:1));let sum=weights.reduce((a,b)=>a+b,0);return {value:valid.reduce((a,p,j)=>a+p.v*weights[j],0)/sum,count:valid.length,method:(varName==='PLD'||varName==='GSF'?'Ponderação por horas':'Ponderação por dias')};}
function seriesData(k,sub){const arr=[],z=SELECTED[k];let ylist=[...z.years].sort((a,b)=>a-b),scs=[...z.sc];
 for(let y of ylist){let historic=DATA.historico[k+'|'+sub]||{};
  let hasHistoric=Object.keys(historic).some(m=>m.startsWith(y+'-'));
  let projected=scs.filter(sc=>Object.keys(DATA.projecoes[k+'|'+sub+'|'+sc]||{}).some(m=>m.startsWith(y+'-')));
  if(z.source==='historico'){if(!hasHistoric)continue;let b=baseline(k,sub,y,'','historico');arr.push({year:y,scenario:null,sub,...b,annual:annual(k,y,b.values)});continue;}
  if(z.source==='ambos'&&projected.length===0&&hasHistoric){let b=baseline(k,sub,y,'','historico');arr.push({year:y,scenario:null,sub,...b,annual:annual(k,y,b.values)});continue;}
  for(let sc of projected){let b=baseline(k,sub,y,sc,z.source);if(!b.values.some(v=>v!==null))continue;
   arr.push({year:y,scenario:sc,sub,...b,annual:annual(k,y,b.values)});}
 }return arr;}
function traces(k,sub){let rows=seriesData(k,sub),z=SELECTED[k],out=[];for(const row of rows){let scIndex=DATA.cenarios.indexOf(row.scenario);let color=row.scenario?SC_COLORS[Math.max(0,scIndex)%SC_COLORS.length]:COLORS[Math.max(0,YEARS.indexOf(row.year))%COLORS.length];let origins=new Set(row.sources.filter(Boolean).map(v=>v.startsWith('Projeção')?'Projeção':'Histórico'));let originLabel=origins.size===2?'Histórico + Projeção':origins.has('Projeção')?'Projeção':'Histórico';let label=(k==='PLD'?sub+' · ':'')+row.year+' · '+originLabel+(row.scenario?' · Cenário: '+row.scenario:'');let custom=MESES.map((m,i)=>[row.sources[i]||'Sem dado',row.annual?.count||0]);let xs=z.view==='mensal'?MESES.map((_,i)=>i):[row.year];let ys=z.view==='mensal'?row.values:[row.annual?.value??null];let customs=z.view==='mensal'?custom:[[originLabel+' · '+(row.annual?.count===12?'Ano completo':`Ano parcial (${row.annual?.count||0}/12 meses)`),row.annual?.count||0]];
 out.push({x:xs,y:ys,customdata:customs,name:label,mode:z.view==='anual'?'markers':'lines+markers',line:{color,width:row.scenario?3:2,dash:row.scenario?'solid':'dot',shape:'spline',smoothing:.6},marker:{size:z.view==='mensal'?6:10},hovertemplate:`<b>${escapeHtml(label)}</b><br>${z.view==='mensal'?'Mês: %{x}':'Ano: %{x}'}<br>Valor: %{y:.2f}<br>%{customdata[0]}<extra></extra>`,connectgaps:false});
 if(z.view==='mensal'&&row.annual){out.push({x:[null,12],y:[null,row.annual.value],mode:'markers',name:label+' — anual',showlegend:false,marker:{color,size:11,symbol:'diamond'},customdata:[null,[row.annual.count===12?'Ano completo':`Ano parcial (${row.annual.count}/12 meses)`,row.annual.count]],hovertemplate:`<b>${escapeHtml(label)}</b><br>Consolidado: %{y:.2f}<br>%{customdata[0]}<extra></extra>`})}
 }return out;}
function plotLayout(k,sub,small){let isAnnual=SELECTED[k].view==='anual';return {height:small?390:510,margin:{l:small?65:78,r:small?16:35,t:24,b:small?87:98},paper_bgcolor:'#fff',plot_bgcolor:'#fff',font:{family:'Century Gothic,Arial,sans-serif',size:12,color:'#496076'},hovermode:'closest',showlegend:false,xaxis:{title:{text:isAnnual?'Ano':'Mês / Ano',standoff:15},type:isAnnual?'linear':'linear',tickmode:'array',tickvals:isAnnual?[...SELECTED[k].years].sort((a,b)=>a-b):Array.from({length:13},(_,i)=>i),ticktext:isAnnual?[...SELECTED[k].years].sort((a,b)=>a-b).map(String):[...MESES,'Ano'],range:isAnnual?undefined:[-.3,12.5],showline:true,linecolor:'#91a6ba',showgrid:false,zeroline:false},yaxis:{title:{text:k==='PLD'?'R$/MWh':k==='ENA'?'% MLT':'%',standoff:10},showline:true,linecolor:'#91a6ba',showgrid:true,gridcolor:'#ecf1f6',zeroline:false,automargin:true},hoverlabel:{bgcolor:'#fff',font:{color:'#172e40'}}};}
function draw(k){const subs=(k==='PLD'?[...SELECTED[k].subs]:OPTIONS[k]);let targets=(k==='ENA'||k==='EAR')?subs:['single'];let plotPromises=[];let count=0;
 for(let sub of targets){let effective=sub==='single'?(k==='PLD'?null:'SIN'):sub;let id=(k==='ENA'||k==='EAR')?'chart-'+k+'-'+sub.replace('/',''):'chart-'+k+'-single';let t=k==='PLD'?subs.flatMap(s=>traces(k,s)):traces(k,effective);if(t.length)count+=t.filter(x=>x.showlegend!==false).length;
 // Legenda externa visível antes do gráfico: não é cortada pelo container do Plotly.
 let plotNode=document.getElementById(id), legendId='legend-'+id;
 let legend=document.getElementById(legendId);
 if(!legend){legend=document.createElement('div');legend.id=legendId;legend.className='series-legend';legend.setAttribute('aria-label','Legenda de cenários e anos');plotNode.parentElement.insertBefore(legend,plotNode)}
 legend.innerHTML=t.map((tr,i)=>tr.showlegend===false?'':`<button type="button" class="legend-item" data-trace-index="${i}" aria-pressed="true" title="Clique para ocultar ou exibir esta série"><span class="legend-stroke ${tr.name.includes('Histórico')?'historic':''}" style="--series-color:${tr.line?.color||tr.marker?.color||'#5d6e83'}"></span>${escapeHtml(tr.name)}</button>`).join('');
 legend.querySelectorAll('button').forEach(btn=>btn.addEventListener('click',()=>{let visible=btn.getAttribute('aria-pressed')!=='true';btn.setAttribute('aria-pressed',String(visible));Plotly.restyle(id,{visible:visible?true:'legendonly'},[Number(btn.dataset.traceIndex)]);adjustHeight()}));
 let layout=plotLayout(k,sub,k==='ENA'||k==='EAR'?sub!=='SIN':false);if(t.length===0){layout.annotations=[{text:'Sem dados para a seleção',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{color:'#91a0b3'}}]}
 if(k==='PLD'&&sub==='single'){plotPromises.push(Plotly.react(id,t,layout,{responsive:true,displaylogo:false}));}else{plotPromises.push(Plotly.react(id,t,layout,{responsive:true,displaylogo:false}))}
 }
 document.getElementById('status-'+k).textContent=count?`${count} séries exibidas · ${SELECTED[k].view==='mensal'?'Visão mensal + consolidado anual':'Consolidado anual'} · ${SELECTED[k].source==='ambos'?'Histórico + projeções':SELECTED[k].source==='historico'?'Só histórico':'Só projeções'}`:'Nenhuma série disponível para os filtros selecionados';Promise.all(plotPromises).then(adjustHeight);}
function redrawAll(){for(const k of VARIABLES){refreshFilters(k);draw(k)}}
function adjustHeight(){if(window.parent!==window)window.parent.postMessage({tipo:'altura-painel-ons',id:'decomp',altura:document.documentElement.scrollHeight+30},'*');}
window.addEventListener('resize',adjustHeight);window.addEventListener('load',adjustHeight);
if(window.ResizeObserver)new ResizeObserver(adjustHeight).observe(document.body);
window.addEventListener('message',e=>{if(e.data?.tipo==='ativar-painel-plotly'){document.querySelectorAll('.js-plotly-plot').forEach(el=>Plotly.Plots.resize(el));adjustHeight()}});
// XLSX genuíno, gerado offline: OpenXML + ZIP armazenado sem dependências de CDN.
function xmlEsc(v){return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&apos;')}
function colName(n){let s='';for(n++;n;n=Math.floor((n-1)/26))s=String.fromCharCode(65+(n-1)%26)+s;return s}
function sheetXML(rows){let body=rows.map((row,i)=>'<row r="'+(i+1)+'">'+row.map((v,j)=>{if(v===null||v===undefined||v==='')return '';let ref=colName(j)+(i+1);return typeof v==='number'&&Number.isFinite(v)?`<c r="${ref}"><v>${v}</v></c>`:`<c r="${ref}" t="inlineStr"><is><t>${xmlEsc(v)}</t></is></c>`}).join('')+'</row>').join('');return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetData>${body}</sheetData></worksheet>`}
const enc=new TextEncoder();const CRC_TABLE=Array.from({length:256},(_,n)=>{let c=n;for(let i=0;i<8;i++)c=c&1?0xedb88320^(c>>>1):c>>>1;return c>>>0});
function crc32(d){let c=0xffffffff;for(const x of d)c=CRC_TABLE[(c^x)&255]^(c>>>8);return (c^0xffffffff)>>>0}
function zipFiles(files){let local=[],central=[],offset=0;const u16=(dv,p,v)=>dv.setUint16(p,v,true),u32=(dv,p,v)=>dv.setUint32(p,v>>>0,true);
 for(let [name,txt] of files){let n=enc.encode(name),d=enc.encode(txt),crc=crc32(d);let a=new Uint8Array(30+n.length+d.length),v=new DataView(a.buffer);u32(v,0,0x04034b50);u16(v,4,20);u16(v,6,0x0800);u32(v,14,crc);u32(v,18,d.length);u32(v,22,d.length);u16(v,26,n.length);a.set(n,30);a.set(d,30+n.length);local.push(a);
 let b=new Uint8Array(46+n.length),w=new DataView(b.buffer);u32(w,0,0x02014b50);u16(w,4,20);u16(w,6,20);u16(w,8,0x0800);u32(w,16,crc);u32(w,20,d.length);u32(w,24,d.length);u16(w,28,n.length);u32(w,42,offset);b.set(n,46);central.push(b);offset+=a.length}
 let clen=central.reduce((n,a)=>n+a.length,0),all=new Uint8Array(offset+clen+22),cur=0;for(let a of [...local,...central]){all.set(a,cur);cur+=a.length}let e=new DataView(all.buffer);u32(e,cur,0x06054b50);u16(e,cur+8,files.length);u16(e,cur+10,files.length);u32(e,cur+12,clen);u32(e,cur+16,offset);return all}
function xlsx(sheets){const xmlHeader='<?xml version="1.0" encoding="UTF-8" standalone="yes"?>';let files=[['[Content_Types].xml',xmlHeader+'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'+sheets.map((_,i)=>`<Override PartName="/xl/worksheets/sheet${i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`).join('')+'</Types>'],['_rels/.rels',xmlHeader+'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'],['xl/workbook.xml',xmlHeader+'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+sheets.map((s,i)=>`<sheet name="${xmlEsc(s.name)}" sheetId="${i+1}" r:id="rId${i+1}"/>`).join('')+'</sheets></workbook>'],['xl/_rels/workbook.xml.rels',xmlHeader+'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+sheets.map((s,i)=>`<Relationship Id="rId${i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet${i+1}.xml"/>`).join('')+'</Relationships>']];for(let i=0;i<sheets.length;i++)files.push([`xl/worksheets/sheet${i+1}.xml`,sheetXML(sheets[i].rows)]);return new Blob([zipFiles(files)],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})}
function exportExcel(k){let rows=[['Variável','Submercado','Ano','Mês','Cenário','Valor','Origem','Consolidado anual','Meses considerados','Metodologia']];for(let sub of (k==='GSF'?['SIN']:k==='PLD'?[...SELECTED[k].subs]:OPTIONS[k]))for(let s of seriesData(k,sub)){for(let i=0;i<12;i++)if(s.values[i]!==null)rows.push([k,sub,s.year,MESES[i],s.sc||'Histórico',s.values[i],s.sources[i],null,null,null]);if(s.annual)rows.push([k,sub,s.year,'ANO',s.sc||'Histórico',s.annual.value,s.annual.count===12?'Ano completo':'Ano parcial',s.annual.value,s.annual.count,s.annual.method]);}
 let metadata=[['Projeções Decomp — Metodologia'],['Variável',k],['Cenários',Array.from(SELECTED[k].sc).join(', ')],['Anos',Array.from(SELECTED[k].years).join(', ')],['Visão',SELECTED[k].view],['Origem selecionada',SELECTED[k].source],['Histórico PLD','CCEE — média de registros horários por mês'],['Histórico ENA','ONS — média mensal em % MLT'],['Histórico EAR','ONS — posição de fim de mês em %'],['Histórico GSF','Não disponível nos arquivos fornecidos'],['Prioridade temporal','Apenas histórico, apenas projeções ou histórico anterior à projeção, conforme filtro'],['Consolidado PLD/GSF','Média ponderada por horas calendário dos meses disponíveis'],['Consolidado ENA','Média ponderada por dias calendário dos meses disponíveis'],['Consolidado EAR','Último valor mensal disponível no ano'],['Ano parcial','Indicado quando houver menos de 12 meses'],['Fonte projeções','dados/bd_rodada_decomp.xlsx']];let blob=xlsx([{name:'Dados e consolidado',rows},{name:'Metodologia',rows:metadata}]);let url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='projecoes_decomp_'+k.toLowerCase()+'.xlsx';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1200)}
buildUI();renderSummary();
</script></body></html>'''


def main():
    projections,scenarios,unknown=read_projection()
    history=read_history()
    payload={'projecoes':projections,'historico':history,'cenarios':scenarios,'gerado':datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat()}
    data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('</','<\\/')
    PLOTS.mkdir(exist_ok=True)
    rendered=HTML_HEAD+get_plotlyjs()+HTML_BODY.replace('__DATA__',data)
    OUT.write_text(rendered,encoding='utf-8')
    inject_index()
    print('DECOMP cenários:',scenarios,'séries:',len(projections),'históricos:',len(history),'meses:',len(set(m for v in projections.values() for m in v)))
    print('Variáveis ignoradas por não fazerem parte deste módulo:',len(unknown))
    print('Página:',OUT)
if __name__=='__main__':main()
