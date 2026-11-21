#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ENA - Projeção Longo Prazo | painel Acompanhamento de mercado.

Entrada:
  dados/climatempo/ENAs_13meses_MmmAA_revN.xlsx (aba Submercados)
  dados/climatempo/recebimentos.json (opcional; horários de recebimento)
Saídas:
  graficos/ena_longo_prazo.html
  graficos/ena_longo_prazo.json
  inclusão idempotente no menu graficos/index.html e no relatório de impressão.

Usa até CINCO revisões por ordem cronológica do mês/ano e número rev do
NOME do arquivo, independentemente da data de upload/modificação no GitHub.
Os arquivos mais antigos podem permanecer em dados/climatempo; não são usados.
Os dados em % MLT são exibidos sem multiplicação por 100 (126.6 = 126,6%).
"""
from __future__ import annotations
import argparse
from datetime import date, datetime
from html import escape
import json
import math
import re
from pathlib import Path
from typing import Any
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'dados' / 'climatempo'
GRAF = ROOT / 'graficos'
MESES = ['Janeiro','Fevereiro','Março','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro']
ABREV = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
SUBS = ['SE/CO','SUL','NE','NORTE']
VARIANTS = ['VE','LI','LS']
X = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
NAME_RE = re.compile(r'^ENAs_13meses_([A-Za-zçÇ]{3})(\d{2})_rev(\d+)\.xlsx$', re.IGNORECASE)


def normal(text: Any) -> str:
    return unicodedata.normalize('NFKD',str(text or '')).encode('ascii','ignore').decode().lower().strip()


def get_name(path: Path) -> dict[str, Any] | None:
    m=NAME_RE.fullmatch(path.name)
    if not m: return None
    ab=normal(m.group(1))
    mapab={normal(x):i+1 for i,x in enumerate(ABREV)}
    if ab not in mapab: return None
    year=2000+int(m.group(2)); month=mapab[ab]; revision=int(m.group(3))
    return {'path':path, 'year':year, 'month':month, 'rev':revision,
            'label':f'Rev{revision}_{ABREV[month-1]}{year%100:02d}',
            'sort':(year,month,revision)}


def discover(folder:Path, limit:int=5)->list[dict[str,Any]]:
    if not folder.exists(): raise FileNotFoundError(f'Pasta ausente: {folder}. Crie dados/climatempo/ e adicione os XLSX.')
    matches=[v for p in folder.iterdir() if p.is_file() and p.suffix.lower()=='.xlsx' and not p.name.startswith('~$') if (v:=get_name(p))]
    if not matches: raise FileNotFoundError(f'Nenhum arquivo ENAs_13meses_MmmAA_revN.xlsx em {folder}')
    seen=set()
    for f in matches:
        if f['sort'] in seen: raise ValueError(f'Revisão duplicada nos nomes: {f["label"]}. Mantenha apenas um arquivo por revisão.')
        seen.add(f['sort'])
    return sorted(matches,key=lambda d:d['sort'],reverse=True)[:limit]


def col_number(ref:str)->int:
    letters=re.match('[A-Z]+',ref.upper())
    if not letters: raise ValueError(f'Referência inválida: {ref}')
    i=0
    for c in letters.group(0):i=i*26+ord(c)-64
    return i-1


def read_cells(path:Path,sheet_name='Submercados')->dict[int,dict[int,Any]]:
    """Leitor OOXML pequeno, sem pandas/openpyxl; preserva valores armazenados.
    Uma fórmula sem resultado em cache é considerada vazia, nunca zero.
    """
    with ZipFile(path,'r') as z:
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        rels={e.attrib['Id']:e.attrib['Target'] for e in rel}
        target=None
        for s in wb.findall(f'{X}sheets/{X}sheet'):
            if normal(s.attrib.get('name'))==normal(sheet_name):
                target=rels.get(s.attrib.get(R+'id'));break
        if not target:raise ValueError(f'{path.name}: guia "{sheet_name}" não encontrada.')
        if target.startswith('/'):xmlpath=target.lstrip('/')
        elif target.startswith('xl/'):xmlpath=target
        else:xmlpath='xl/'+target
        # resolve caminhos que possam conter segmentos ./
        import posixpath
        xmlpath=posixpath.normpath(xmlpath)
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            root=ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall(f'{X}si'):
                strings.append(''.join(t.text or '' for t in si.iter(f'{X}t')))
        root=ET.fromstring(z.read(xmlpath))
        rows:dict[int,dict[int,Any]]={}
        for c in root.iter(f'{X}c'):
            cref=c.attrib.get('r','')
            mm=re.search(r'\d+',cref)
            if not mm:continue
            ry=int(mm.group())-1;co=col_number(cref)
            typ=c.attrib.get('t','')
            if typ=='inlineStr':
                ins=c.find(f'{X}is')
                val=''.join(t.text or '' for t in ins.iter(f'{X}t')) if ins is not None else None
            else:
                ve=c.find(f'{X}v');v=ve.text if ve is not None else None
                if typ=='s' and v is not None: val=strings[int(v)]
                elif typ in ('str','e','d'):val=v
                elif typ=='b':val=(v=='1')
                elif v is None:val=None
                else:
                    try:val=float(v)
                    except ValueError:val=v
            if val is not None:rows.setdefault(ry,{})[co]=val
        return rows


def month_number(text:Any)->int|None:
    t=normal(text)
    for i,m in enumerate(MESES):
        if t==normal(m):return i+1
    return None


def shift_month(year:int,month:int,offset:int)->date:
    n=year*12+(month-1)+offset
    return date(n//12,n%12+1,1)


def identify_region(text:Any)->str|None:
    x=normal(text)
    if x.startswith('sudeste') or x.startswith('se/co'):return 'SE/CO'
    if x.startswith('sul') and not x.startswith('suleste'):return 'SUL'
    if x.startswith('nordeste'):return 'NE'
    if x.startswith('norte'):return 'NORTE'
    return None


def variant_from_title(text:Any)->str:
    t=normal(text)
    if 'limite superior' in t:return 'LS'
    if 'limite inferior' in t:return 'LI'
    return 'VE'


def parse_revision(info:dict[str,Any])->dict[str,Any]:
    rows=read_cells(info['path'])
    data={scenario:{sub:{} for sub in SUBS} for scenario in VARIANTS}
    found=set()
    for ry,row in sorted(rows.items()):
        titles=[(col,identify_region(v),variant_from_title(v)) for col,v in row.items() if isinstance(v,str) and identify_region(v)]
        # cabeçalho da tabela: 4 submercados na mesma linha
        if len(titles)<4:continue
        for co,sub,scenario in titles:
            if (scenario,sub) in found:raise ValueError(f'{info["path"].name}: cabeçalho {scenario}/{sub} repetido.')
            found.add((scenario,sub))
            headers=rows.get(ry+1,{})
            perccols=[c for c,v in headers.items() if co<c<=co+4 and normal(v).replace(' ','') in ('%mlt','%damlt')]
            pctcol=perccols[0] if perccols else co+2
            previous=0
            for idx in range(13):
                rr=rows.get(ry+2+idx,{})
                month=month_number(rr.get(co))
                dt=shift_month(info['year'],info['month'],idx)
                if month != dt.month:
                    raise ValueError(f'{info["path"].name}: {scenario}/{sub}, linha {ry+3+idx}: '
                                     f'mês {rr.get(co)!r} incompatível com {dt:%m/%Y}.')
                raw=rr.get(pctcol)
                if not isinstance(raw,(int,float)) or not math.isfinite(float(raw)):
                    raise ValueError(f'{info["path"].name}: valor % MLT inválido em {scenario}/{sub}/{dt:%m/%Y}: {raw!r}')
                data[scenario][sub][dt.isoformat()]=round(float(raw),8)
                previous+=1
    needed={(var,sub) for var in VARIANTS for sub in SUBS}
    if found!=needed: raise ValueError(f'{info["path"].name}: títulos faltantes: {sorted(needed-found)}')
    for var in VARIANTS:
        for sub in SUBS:
            if len(data[var][sub])!=13: raise ValueError(f'{info["path"].name}: {var}/{sub} não contém 13 meses.')
    result={k:v for k,v in info.items() if k!='path'}
    result['file']=info['path'].name;result['data']=data
    return result


def read_receipts(folder:Path)->dict[str,str]:
    path=folder/'recebimentos.json'
    if not path.exists():return {}
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict):raise ValueError('recebimentos.json deve ser um objeto de nome da revisão → data/hora')
    return {str(k).lower():str(v) for k,v in data.items()}


def build_payload(revisions:list[dict[str,Any]], receipts:dict[str,str])->dict[str,Any]:
    cur=revisions[0];prev=revisions[1] if len(revisions)>1 else None
    for r in revisions:
        r['recebimento']=receipts.get(r['label'].lower(),receipts.get(r['file'].lower(),''))
    changes={}
    # Comparação sempre Atual - imediatamente Anterior; somente meses comuns.
    for var in VARIANTS:
        changes[var]={}
        for sub in SUBS:
            rows=[]
            for m,cval in cur['data'][var][sub].items():
                pval=prev['data'][var][sub].get(m) if prev else None
                delta=round(cval-pval,8) if pval is not None else None
                rows.append({'mes':m,'atual':cval,'anterior':pval,'delta':delta,
                             'nivel':('CRÍTICO' if abs(delta)>=10 else 'ATENÇÃO' if abs(delta)>=3 else 'NORMAL') if delta is not None else 'SEM COMPARAÇÃO'})
            changes[var][sub]=rows
    return {'titulo':'ENA - Projeção Longo Prazo','revisoes':revisions,'comparacao':changes,
            'atual':cur['label'],'anterior':prev['label'] if prev else None,
            'unidade':'% MLT','metodo':'Atual - Anterior (pontos percentuais)',
            'gerado_em':datetime.now().astimezone().isoformat(timespec='seconds'),
            'thresholds_alerta_pp':{'atencao':3,'critico':10}}


def month_label(iso:str)->str:
    dt=date.fromisoformat(iso)
    return f'{ABREV[dt.month-1]}/{dt.year}'


def make_figures(payload:dict[str,Any])->list[str]:
    import plotly.graph_objects as go
    from plotly.offline import plot
    colors=['#053F62','#147DB5','#5B9BD5','#A4BEE1','#A7A7A7']
    charts=[]
    revs=payload['revisoes'];current=revs[0]
    for sub in SUBS:
        fig=go.Figure()
        # Faixa LI/LS somente da revisão atual.
        low=current['data']['LI'][sub];high=current['data']['LS'][sub]
        xs=list(current['data']['VE'][sub])
        fig.add_trace(go.Scatter(x=xs,y=[low[x] for x in xs],mode='lines',line=dict(color='rgba(48,156,193,0)',width=0),
                                 hoverinfo='skip',showlegend=False,name='Limite inferior'))
        fig.add_trace(go.Scatter(x=xs,y=[high[x] for x in xs],mode='lines',fill='tonexty',
                                 fillcolor='rgba(60,169,203,0.17)',line=dict(color='rgba(48,156,193,0)',width=0),
                                 name='Faixa LI–LS atual',hovertemplate='%{x|%b/%Y}: LS %{y:.1f}% MLT<extra></extra>'))
        for i,rev in enumerate(revs):
            points=rev['data']['VE'][sub]
            xs2=list(points)
            fig.add_trace(go.Scatter(x=xs2,y=[points[d] for d in xs2],name=rev['label'],mode='lines+markers',
                line=dict(color=colors[i%len(colors)],width=3.3 if i==0 else 2.0),
                marker=dict(size=7 if i==0 else 5,symbol='circle' if i==0 else 'square'),
                hovertemplate='%{x|%b/%Y}: %{y:.1f}% MLT<extra>'+rev['label']+'</extra>'))
        fig.add_hline(y=100,line=dict(color='#7C8791',width=1.3,dash='dash'))
        fig.update_layout(title=dict(text=sub,x=0.02,font=dict(size=17,color='#004A60')),
            template='plotly_white',height=445,
            margin=dict(l=55,r=15,t=48,b=110),
            legend=dict(orientation='h',yanchor='top',y=-0.25,xanchor='center',x=.5,font=dict(size=10)),
            hovermode='x unified',
            font=dict(family='Arial, sans-serif',color='#36505D'),
            paper_bgcolor='white',plot_bgcolor='white')
        fig.update_xaxes(type='date',tickvals=xs,
                         ticktext=[f'{ABREV[date.fromisoformat(t).month-1]}/{date.fromisoformat(t).year%100:02d}' for t in xs],
                         tickangle=-40,showgrid=True,gridcolor='#EBF1F4',rangeslider_visible=False)
        fig.update_yaxes(title_text='ENA (% MLT)',gridcolor='#E7EFF3',zeroline=False,ticksuffix='%')
        charts.append(plot(fig,output_type='div',include_plotlyjs=False,config={
            'responsive':True,'displaylogo':False,
            'modeBarButtonsToRemove':['lasso2d','select2d'],
            'toImageButtonOptions':{'format':'png','filename':'ENA_'+sub.replace('/','_'), 'height':650,'width':930,'scale':2}}))
    return charts


def html_document(payload:dict[str,Any])->str:
    from plotly.offline import get_plotlyjs
    plots=make_figures(payload)
    meta=payload['revisoes'];cur=meta[0]
    lines=[]
    for i,r in enumerate(meta):
        lines.append(f'<div class="revline"><span class="dot dot{i}"></span><strong>{escape(r["label"])}</strong></div>')
    # Segurança ao embutir JSON no HTML.
    data=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    graph_html=''.join(f'<article class="plot-card">{p}</article>' for p in plots)
    return '''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>ENA - Projeção Longo Prazo</title><style>
    :root{color-scheme:light;font-family:Arial,Segoe UI,sans-serif;color:#233D4A;background:#EEF3F5}*{box-sizing:border-box}body{margin:0}
    main{max-width:1580px;margin:0 auto;padding:17px 20px 45px}header{background:white;border-radius:13px;padding:22px 25px;border-top:5px solid #007F81;margin-bottom:16px;box-shadow:0 4px 18px #20414b12}
    h1{font-size:25px;color:#006E75;margin:0 0 6px}h2{margin:0;font-size:18px;color:#006870}.subtitle{color:#657985;margin:0 0 10px;font-size:13px}
    .revisions{display:flex;flex-wrap:wrap;gap:8px 17px;margin:12px 0 3px}.revline{font-size:12px;color:#4E6571}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#053F62;margin-right:6px}.dot1{background:#147DB5}.dot2{background:#5B9BD5}.dot3{background:#A4BEE1}.dot4{background:#A7A7A7}
    .muted{color:#637A87;font-size:12px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:18px}.plot-card{background:#fff;border-radius:12px;box-shadow:0 3px 15px #24556212;min-width:0;padding:8px 8px 4px}
    .panel{background:white;border-radius:12px;padding:18px 20px;box-shadow:0 3px 15px #24556212;margin-bottom:15px}.panel-head{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:space-between;margin-bottom:12px}
    .filters{display:flex;align-items:center;gap:9px;flex-wrap:wrap}select,button{padding:8px 11px;background:white;border:1px solid #C7D9DF;border-radius:8px;font:inherit;font-size:13px;color:#23404d}button{background:#007D80;color:white;border-color:#007D80;font-weight:700;cursor:pointer}select:focus,button:focus{outline:2px solid #65BFC1;outline-offset:1px}
    .table-wrap{overflow-x:auto;max-width:100%}table{width:100%;border-collapse:collapse;font-size:12px;min-width:690px}th{background:#005A6D;color:#fff;padding:11px;text-align:left;position:sticky;top:0}td{padding:9px 11px;border-bottom:1px solid #E3EBEF;white-space:nowrap}tr:nth-child(even){background:#F7FAFB}td.num{text-align:right;font-variant-numeric:tabular-nums}.pos{color:#067FAA;background:#E3F4FC;font-weight:700}.neg{color:#970E24;background:#FDEAF0;font-weight:700}.neutral{color:#5D7380}.level{font-weight:700;font-size:11px}.alert{color:#B06300}.critical{color:#AE2431}
    .chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}.chip{padding:7px 10px;background:#EBF5F6;border-radius:9px;font-size:12px;color:#00646C}
    @media(max-width:900px){.grid{grid-template-columns:1fr}main{padding:8px}.plot-card{padding:0}header,.panel{padding:14px}h1{font-size:21px}}
    @media print{main{max-width:none}.grid{grid-template-columns:1fr 1fr}.filters,button{display:none!important}.plot-card,.panel,header{box-shadow:none;border:1px solid #ddd}.table-wrap{overflow:visible}}
    </style><script>''' + get_plotlyjs() + '''</script></head><body><main>
    <header><h1>ENA - Projeção Longo Prazo</h1>
    <p class="subtitle">Revisão atual versus até quatro revisões anteriores · VE (% MLT) e faixa LI–LS da revisão atual</p>
    <div class="revisions">''' + ''.join(lines) + '''</div>
    <p class="muted">Revisões ordenadas por mês/ano e número rev do nome do arquivo. A revisão mais recente é ''' + escape(cur['label']) + '''. Diferenças calculadas somente para meses comuns às duas últimas revisões.</p>
    </header><section class="grid" id="ena-lp-plots">''' + graph_html + '''</section>
    <section class="panel"><div class="panel-head"><div><h2>Variações da revisão atual contra a anterior</h2><p class="subtitle">Diferença = Atual − Anterior · em pontos percentuais (pp)</p></div>
    <div class="filters"><label for="scenario">Cenário:</label><select id="scenario"><option value="VE">Valor esperado (VE)</option><option value="LI">Limite inferior (LI)</option><option value="LS">Limite superior (LS)</option></select>
    <label for="submarket">Submercado:</label><select id="submarket"><option value="TODOS">Todos</option><option value="SE/CO">SE/CO</option><option value="SUL">Sul</option><option value="NE">Nordeste</option><option value="NORTE">Norte</option></select>
    <label for="variation">Exibir:</label><select id="variation"><option value="TODOS">Todos os meses</option><option value="RELEVANTES">|Δ| ≥ 3 pp</option></select><button id="download" type="button">Exportar CSV</button></div></div>
    <div id="ena-chips" class="chips"></div><div class="table-wrap"><table id="ena-lp-table"><thead><tr><th>Submercado</th><th>Mês</th><th>Anterior (% MLT)</th><th>Atual (% MLT)</th><th>Δ (pp)</th><th>Nível</th></tr></thead><tbody id="table-body"></tbody></table></div>
    <p class="muted">Destaques de apresentação: atenção a partir de 3 pp, crítico a partir de 10 pp (limites de visualização ajustáveis no código). Meses sem sobreposição aparecem sem variação; não são considerados zero.</p>
    </section></main><script id="ena-payload" type="application/json">''' + data + '''</script><script>
    (function(){const DATA=JSON.parse(document.getElementById('ena-payload').textContent);const $=id=>document.getElementById(id);
    const monthNames=['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
    const dateLabel=s=>{const d=s.split('-');return monthNames[Number(d[1])-1]+'/'+d[0];};
    const fmt=n=>n==null?'—':Number(n).toLocaleString('pt-BR',{minimumFractionDigits:1,maximumFractionDigits:1});
    function filteredRows(){const scen=$('scenario').value;const market=$('submarket').value;const rel=$('variation').value;let rows=[];
      ['SE/CO','SUL','NE','NORTE'].forEach(sub=>{if(market!=='TODOS'&&market!==sub)return;
        DATA.comparacao[scen][sub].forEach(r=>{if(rel==='RELEVANTES'&&(r.delta===null||Math.abs(r.delta)<3))return;rows.push({...r,sub});});});
      return rows.sort((a,b)=>a.mes.localeCompare(b.mes)||a.sub.localeCompare(b.sub));}
    function draw(){const rows=filteredRows();const tbody=$('table-body');tbody.textContent='';let countUp=0,countDown=0;
      for(const r of rows){const tr=document.createElement('tr');
        const delta=r.delta===null?'—':(r.delta>0?'+':'')+fmt(r.delta)+' pp';if(r.delta>0)countUp++;else if(r.delta<0)countDown++;
        const vals=[r.sub,dateLabel(r.mes),fmt(r.anterior),fmt(r.atual),delta,r.nivel];
        vals.forEach((s,i)=>{let cell=document.createElement('td');cell.textContent=s;if(i>=2&&i<=4)cell.classList.add('num');
          if(i===4)cell.classList.add(r.delta===null?'neutral':r.delta>0?'pos':r.delta<0?'neg':'neutral');
          if(i===5)cell.classList.add('level',r.nivel==='CRÍTICO'?'critical':r.nivel==='ATENÇÃO'?'alert':'');tr.appendChild(cell);});tbody.appendChild(tr);}
      if(!rows.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=6;td.textContent='Nenhuma linha disponível para os filtros selecionados.';tr.appendChild(td);tbody.appendChild(tr);}
      $('ena-chips').innerHTML='<span class="chip">'+rows.length+' registros</span><span class="chip">'+countUp+' aumentos</span><span class="chip">'+countDown+' reduções</span>';
    }
    function exportCsv(){const rows=filteredRows();const header=['Submercado','Mes','Cenario','Anterior_pct_MLT','Atual_pct_MLT','Delta_pp','Nivel'];
      const quote=x=>'"'+String(x??'').replace(/"/g,'""')+'"';const body=rows.map(r=>[r.sub,dateLabel(r.mes),$('scenario').value,r.anterior,r.atual,r.delta,r.nivel].map(quote).join(';'));
      const csv='\\uFEFF'+[header.map(quote).join(';'),...body].join('\\r\\n');const blob=new Blob([csv],{type:'text/csv;charset=utf-8'});const u=URL.createObjectURL(blob);
      const a=document.createElement('a');a.href=u;a.download='ENA_Longo_Prazo_'+DATA.atual+'_vs_'+(DATA.anterior||'sem_anterior')+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
    function adjustParentIframe(){
      try {const fr=window.frameElement;if(!fr)return;const h=Math.max(1500,document.documentElement.scrollHeight+35);
        if(Math.abs(fr.getBoundingClientRect().height-h)>35)fr.style.height=h+'px';} catch(_){}
    }
    ['scenario','submarket','variation'].forEach(id=>$(id).addEventListener('change',()=>{draw();adjustParentIframe();}));
    $('download').addEventListener('click',exportCsv);draw();
    window.addEventListener('load',()=>setTimeout(adjustParentIframe,1000));
    window.addEventListener('resize',()=>setTimeout(adjustParentIframe,180));
    setTimeout(adjustParentIframe,2500);
    })();
    </script></body></html>'''


def inject_index():
    path=GRAF/'index.html'
    if not path.exists():raise FileNotFoundError('Falta graficos/index.html; execute após os geradores anteriores do painel.')
    html=path.read_text(encoding='utf-8')
    button='<button role="tab" id="botao-ena-lp" aria-selected="false" aria-controls="aba-ena-lp" onclick="selecionar(\'ena-lp\')">ENA - Projeção Longo Prazo</button>'
    panel='<section class="aba" id="aba-ena-lp" role="tabpanel" aria-labelledby="botao-ena-lp"><div class="molde"><iframe id="quadro-ena-lp" title="ENA - Projeção Longo Prazo" src="ena_longo_prazo.html" loading="lazy" style="height:1900px"></iframe></div></section>'
    if 'id="botao-ena-lp"' not in html:
        if '</nav>' not in html:raise ValueError('Menu </nav> não encontrado em graficos/index.html')
        html=html.replace('</nav>',button+'\n</nav>',1)
    if 'id="aba-ena-lp"' not in html:
        if '</main>' not in html:raise ValueError('Área </main> não encontrada em graficos/index.html')
        html=html.replace('</main>',panel+'\n</main>',1)
    path.write_text(html,encoding='utf-8')


def inject_print():
    path=GRAF/'impressao.html'
    if not path.exists():return
    txt=path.read_text(encoding='utf-8')
    module="['ena-lp','ENA - Projeção Longo Prazo','ena_longo_prazo.html']"
    if module in txt:return
    anchors=["['mre','Ajuste MRE','ajuste_mre.html']", "['decomp','Projeções Decomp','decomp.html']"]
    for anchor in anchors:
        if anchor in txt:
            txt=txt.replace(anchor,anchor+',\n '+module,1)
            txt=txt.replace('Newave, Decomp e Ajuste MRE','Newave, Decomp, Ajuste MRE e ENA Longo Prazo')
            txt=txt.replace("decomp:1}[id]", "decomp:1,'ena-lp':4}[id]")
            path.write_text(txt,encoding='utf-8')
            return
    print('Aviso: módulo ENA LP não pôde ser integrado ao relatório de impressão (lista não identificada).')


def main():
    global GRAF
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--diretorio',default=str(DATA),help='Diretório com os XLSX de revisão')
    parser.add_argument('--graficos',default=str(GRAF),help='Pasta de saída (padrão: graficos)')
    parser.add_argument('--sem-menu',action='store_true',help='Gerar prévia sem modificar index/impressao')
    args=parser.parse_args()
    GRAF=Path(args.graficos).resolve()
    folder=Path(args.diretorio).resolve()
    files=discover(folder)
    print('Revisões selecionadas, da mais recente à mais antiga:')
    for i,f in enumerate(files): print(f' {i+1}. {f["label"]} -> {f["path"].name}')
    revisions=[parse_revision(x) for x in files]
    payload=build_payload(revisions,read_receipts(folder))
    GRAF.mkdir(parents=True,exist_ok=True)
    (GRAF/'ena_longo_prazo.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    (GRAF/'ena_longo_prazo.html').write_text(html_document(payload),encoding='utf-8')
    if not args.sem_menu:
        inject_index();inject_print()
    print('ENA LP: gerados ena_longo_prazo.html e ena_longo_prazo.json; '+str(len(revisions))+' revisão(ões).')
    if len(revisions)==1:print('Aviso: somente uma revisão; não há comparações até que outra planilha seja adicionada.')
    else:
        sub='SE/CO';r=payload['comparacao']['VE'][sub][0]
        print(f'ENA LP: {sub} {month_label(r["mes"])} - variação da revisão atual: {r["delta"]} pp')

if __name__=='__main__':main()
