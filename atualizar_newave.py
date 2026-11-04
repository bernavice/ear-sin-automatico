#!/usr/bin/env python3
"""Painel Projeções Newave (2.000 séries) — integração GitHub Pages.

Entrada: dados/Produtos Novo.xlsx (ou dados/Produto Novo.xlsx)
Realizados: dados/pld_historico_long.csv e dados/ear_diaria.csv
Saídas: graficos/newave.html, dados/newave_resumo.json e atualização do index.html.

Leitura do XLSX via ZIP/XML nativo para não precisar instalar bibliotecas Excel
adicionais. Processamento dos cenários via NumPy. Não altera a planilha original.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from plotly.offline import get_plotlyjs

ROOT = Path(__file__).resolve().parent
DADOS = ROOT / 'dados'
GRAFICOS = ROOT / 'graficos'
ARQUIVOS_XLSX = [DADOS / 'Produtos Novo.xlsx', DADOS / 'Produto Novo.xlsx', ROOT / 'Produtos Novo.xlsx', ROOT / 'Produto Novo.xlsx']
PLD = DADOS / 'pld_historico_long.csv'
EAR = DADOS / 'ear_diaria.csv'
MERCADO_DIARIO = DADOS / 'precos_mercado_diario.csv'
PAGINA = GRAFICOS / 'newave.html'
RESUMO = DADOS / 'newave_resumo.json'
INDEX = GRAFICOS / 'index.html'
VARIAVEIS = {'cmarg001':'SE/CO', 'cmarg002':'Sul', 'cmarg003':'Nordeste', 'cmarg004':'Norte', 'GSF':'GSF', 'ear_sin':'EAR SIN'}
SUBMERCADOS = ['SE/CO','Sul','Nordeste','Norte']
CORES = {'SE/CO':'#2563EB','Sul':'#D97706','Nordeste':'#DC2626','Norte':'#0D9488'}
MESES = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
NSR = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
RELS = '{http://schemas.openxmlformats.org/package/2006/relationships}'


def excel_col(s):
    n=0
    for c in s:
        n=n*26+ord(c)-64
    return n-1


def periodo(texto):
    s=str(texto).lower().strip().replace('ç','c')
    m=re.fullmatch(r'([a-z]{3})[/\-](\d{2}|\d{4})',s)
    pt={'jan':1,'fev':2,'mar':3,'abr':4,'mai':5,'jun':6,'jul':7,'ago':8,'set':9,'out':10,'nov':11,'dez':12}
    if not m or m.group(1) not in pt:
        raise ValueError(f'Cabeçalho de mês não reconhecido: {texto!r}')
    ano=int(m.group(2))
    return (ano+2000 if ano<100 else ano,pt[m.group(1)])


def ler_xlsx(caminho):
    """Lê somente A:BI; ignora fórmulas/quadros auxiliares além dos 60 meses.

    Cabeçalho B:BI: meses; A2:A2001: identificadores da série. Suporta
    valores numericamente armazenados, compartilhados ou cacheados de fórmula.
    """
    resultados={}
    with zipfile.ZipFile(caminho) as z:
        compartilhadas=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            r=ET.fromstring(z.read('xl/sharedStrings.xml'))
            compartilhadas=[''.join(el.itertext()) for el in r.findall(NS+'si')]
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        relroot=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        rels={x.get('Id'):x.get('Target') for x in relroot.findall(RELS+'Relationship')}
        folhas={x.get('name'):rels.get(x.get(NSR+'id')) for x in wb.find(NS+'sheets')}
        ausentes=set(VARIAVEIS)-set(folhas)
        if ausentes:
            raise ValueError('Faltam guias obrigatórias: '+', '.join(sorted(ausentes)))
        base_periodos=None
        for aba in VARIAVEIS:
            target=folhas[aba]
            target=target.lstrip('/').replace('../','')
            if not target.startswith('xl/'):target='xl/'+target
            if target not in z.namelist():
                raise ValueError(f'Arquivo interno da guia {aba} ausente: {target}')
            cols=[]; series=[]; ids=[]
            with z.open(target) as stream:
                for _,row in ET.iterparse(stream,events=('end',)):
                    if row.tag != NS+'row':continue
                    linha={}
                    for cell in row.findall(NS+'c'):
                        match=re.match(r'([A-Z]+)',cell.get('r',''))
                        if not match:continue
                        ix=excel_col(match.group(1))
                        if ix>120:continue # planilha pode conter quadros auxiliares à direita
                        v=cell.find(NS+'v')
                        inline=cell.find(NS+'is')
                        val=v.text if v is not None else (''.join(inline.itertext()) if inline is not None else '')
                        if cell.get('t')=='s' and val!='':val=compartilhadas[int(val)]
                        linha[ix]=val
                    if row.get('r')=='1':
                        cols=[]
                        for c in range(1,200):
                            if c not in linha or not str(linha[c]).strip():break
                            try:cols.append(periodo(linha[c]))
                            except ValueError:break
                        if len(cols)<12:raise ValueError(f'{aba}: cabeçalho de meses muito curto: {cols}')
                        if len(set(cols))!=len(cols):raise ValueError(f'{aba}: meses duplicados')
                        if base_periodos is None:base_periodos=cols
                        elif cols!=base_periodos:raise ValueError(f'{aba}: meses diferentes da guia principal')
                    elif row.get('r') and int(row.get('r'))>1:
                        if not cols:raise ValueError(f'{aba}: cabeçalho não encontrado')
                        try:ident=int(float(linha.get(0,'')))
                        except (ValueError,TypeError):continue
                        valores=[]
                        for c in range(1,len(cols)+1):
                            try:valores.append(float(linha.get(c,'')))
                            except (ValueError,TypeError):valores.append(np.nan)
                        ids.append(ident);series.append(valores)
                    row.clear()
            if len(ids)<100:
                raise ValueError(f'{aba}: foram reconhecidas apenas {len(ids)} séries')
            if len(set(ids))!=len(ids):raise ValueError(f'{aba}: IDs das séries duplicados')
            ordem=np.argsort(ids)
            resultados[aba]={'ids':np.asarray(ids)[ordem], 'valores':np.array(series,dtype=float)[ordem]}
            print(f'Newave {aba}: {len(ids)} séries × {len(cols)} meses; {np.isfinite(resultados[aba]["valores"]).sum()} valores válidos')
        for name in list(resultados):
            if not np.array_equal(resultados[name]['ids'],resultados['cmarg001']['ids']):
                raise ValueError('IDs das séries diferem entre guias; não é seguro consolidar anos.')
        return base_periodos,{VARIAVEIS[k]:v['valores'] for k,v in resultados.items()},len(resultados['cmarg001']['ids'])


def ler_preco_realizado():
    """Média das horas disponíveis por submercado, somente meses completos."""
    if not PLD.exists():raise FileNotFoundError(f'PLD histórico ausente: {PLD}')
    cnt=defaultdict(int);som=defaultdict(float);dias=defaultdict(set);horas=defaultdict(set)
    mapa={'SE/CO':'SE/CO','SUL':'Sul','NE':'Nordeste','N':'Norte'}
    with PLD.open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f,delimiter=';'):
            dt=row['data'][:10];sub=mapa.get(row.get('submercado'))
            if not sub or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',dt):continue
            ano,mes=map(int,dt.split('-')[:2]);k=(ano,mes,sub)
            try:valor=float(row['pld'].replace(',','.'));hora=int(row['hora'])
            except (ValueError,KeyError):continue
            if not np.isfinite(valor) or not 0<=hora<=23:continue
            cnt[k]+=1;som[k]+=valor;dias[k].add(dt);horas[k].add((dt,hora))
    realizado={}
    hoje=date.today()
    for (ano,mes,sub),n in cnt.items():
        esperado=calendar.monthrange(ano,mes)[1]*24
        if (ano,mes)>=(hoje.year,hoje.month):continue
        if n==esperado and len(horas[(ano,mes,sub)])==esperado:
            realizado[(ano,mes,sub)]=som[(ano,mes,sub)]/n
    return realizado


def ler_ear_realizado():
    if not EAR.exists():raise FileNotFoundError(f'EAR histórico ausente: {EAR}')
    ultimo={}
    with EAR.open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f,delimiter=';'):
            try:
                d=date.fromisoformat(row['data'][:10]);v=float(row['earsin'].replace(',','.'))
            except (KeyError,ValueError):continue
            if np.isfinite(v) and 0<=v<=105:
                ultimo[(d.year,d.month)]=(d,v)
    realizado={}
    hoje=date.today()
    for (ano,mes),(d,valor) in ultimo.items():
        if (ano,mes)>=(hoje.year,hoje.month):continue
        # EAR mensal usa último dia do mês; não preencher mês incompleto
        if d.day==calendar.monthrange(ano,mes)[1]:realizado[(ano,mes)]=valor
    return realizado



def ler_precos_mercado_anuais(anos):
    """Preço mais recente de cada produto anual SE/CO do módulo Mercado.

    Utiliza a média diária ponderada por volume já calculada por
    atualizar_mercado.py. Não confunde data da negociação com ano de entrega.
    A base acompanhada atualmente não contém contratos anuais dos outros
    submercados; para eles não é estimado preço de mercado.
    """
    if not MERCADO_DIARIO.is_file():
        print(f'Newave: base diária de Mercado não encontrada: {MERCADO_DIARIO}; sem comparação de mercado.')
        return {}
    anos_esperados={str(ano) for ano in anos}
    resultado={}
    with MERCADO_DIARIO.open(encoding='utf-8-sig', newline='') as f:
        leitor=csv.DictReader(f)
        exigidas={'APELIDO','DIA','PRECO_MEDIO'}
        if not exigidas.issubset(set(leitor.fieldnames or [])):
            print(f'Newave: cabeçalho inesperado na base de Mercado: {leitor.fieldnames}; sem comparação de mercado.')
            return {}
        for row in leitor:
            ano=str(row.get('APELIDO','')).strip()
            dia=str(row.get('DIA','')).strip()[:10]
            if ano not in anos_esperados:continue
            try:
                data=date.fromisoformat(dia)
                preco=float(str(row['PRECO_MEDIO']).strip().replace(',','.'))
            except (TypeError,ValueError,KeyError):continue
            if not np.isfinite(preco) or preco<=0 or data>date.today():continue
            anterior=resultado.get(ano)
            if anterior is None or dia>anterior['data']:
                resultado[ano]={'preco':round(preco,3),'data':dia,
                                'submercado':'SE/CO',
                                'produto':f'FEN - SE CON ANU JAN/{ano[-2:]} DEZ/{ano[-2:]} - Preço Fixo'}
    print('Newave: preços anuais de mercado identificados:',
          ', '.join(f'{ano}: R$ {d["preco"]:.2f}/MWh em {d["data"]}' for ano,d in sorted(resultado.items())) or 'nenhum')
    return resultado


def risco(a,tipo):
    vals=np.asarray(a,dtype=float)
    vals=np.sort(vals[np.isfinite(vals)])
    if vals.size==0:return {'media':None,'cvar':None,'epar':None,'n':0}
    n=max(1,int(math.ceil(vals.size*.05)))
    # Preço: risco de ALTA (maiores 5%); GSF / EAR: risco de BAIXA (menores 5%).
    cima=tipo=='preco'
    return {'media':round(float(vals.mean()),3),
            'cvar':round(float(vals[-n:].mean() if cima else vals[:n].mean()),3),
            'epar':round(float(vals[:n].mean() if cima else vals[-n:].mean()),3),'n':int(vals.size)}


def arr_json(a):
    return [None if not np.isfinite(v) else round(float(v),3) for v in a]


def consolidar(periodos,mat,nseries):
    precos=ler_preco_realizado();ears=ler_ear_realizado()
    # Qual é o último mês com PLD completo nos quatro submercados E EAR de fechamento?
    completos={p for p in ears if all((p[0],p[1],s) in precos for s in SUBMERCADOS)}
    disponiveis=sorted(set(periodos).intersection(completos))
    if not disponiveis:
        raise ValueError('Sem mês completo comum de PLD e EAR para substituir nas projeções.')
    ultimo=disponiveis[-1]
    # Se não houver algum mês intermediário realizado, deixar os valores projetados,
    # mas não usá-lo como se tivesse sido efetivamente realizado.
    referencias={}
    for j,(ano,mes) in enumerate(periodos):
        if (ano,mes)>ultimo:continue
        if (ano,mes) in completos:
            for s in SUBMERCADOS:
                mat[s][:,j]=precos[(ano,mes,s)]
            mat['EAR SIN'][:,j]=ears[(ano,mes)]
            referencias[f'{ano}-{mes:02}']={'pld':{s:round(precos[(ano,mes,s)],2) for s in SUBMERCADOS},'ear':round(ears[(ano,mes)],2)}
    # GSF -1 em todas as séries nos meses realizados é placeholder do arquivo.
    # Não inventar GSF realizado ou tratar -1 como uma perda de 100%.
    gsf=mat['GSF'].copy()
    meses_nao_informados=[]
    for j,(ano,mes) in enumerate(periodos):
        if (ano,mes)<=ultimo and np.all(np.isclose(gsf[:,j],-1.0,equal_nan=False)):
            gsf[:,j]=np.nan
            meses_nao_informados.append(f'{ano}-{mes:02}')
    mat['GSF']=gsf*100.0 # arquivo usa fração; ex.: -0,08 = -8%
    anos=sorted({a for a,_ in periodos})
    mercado=ler_precos_mercado_anuais(anos)
    medias_mensais={};resumos_anuais={}; caixas={}
    for nome,values in mat.items():
        tipo='preco' if nome in SUBMERCADOS else 'baixa'
        medias_mensais[nome]=[risco(values[:,j],tipo) for j in range(len(periodos))]
        resumos_anuais[nome]={};caixas[nome]={}
        for a in anos:
            ids=[i for i,(ano,mes) in enumerate(periodos) if ano==a]
            if not ids:continue
            if nome=='EAR SIN':
                idx=next((i for i,(ano,mes) in enumerate(periodos) if ano==a and mes==9),None)
                if idx is None:continue
                vals=values[:,idx]
                pcount=int(np.isfinite(vals).sum())
                parcial=False
                nome_periodo='Setembro'
            else:
                bloco=values[:,ids]
                contagem=np.isfinite(bloco).sum(axis=1)
                vals=np.full(values.shape[0],np.nan)
                validos=contagem>0
                vals[validos]=np.nanmean(bloco[validos,:],axis=1)
                parcial=not bool(np.all(np.isfinite(bloco)))
                nome_periodo='Ano' if not parcial else 'Período disponível'
                pcount=int(validos.sum())
            out=risco(vals,tipo)
            out['parcial']=parcial
            out['periodo']=nome_periodo
            resumos_anuais[nome][str(a)]=out
            caixas[nome][str(a)]=arr_json(vals[np.isfinite(vals)])
    return {
        'periodos': [{'ano':a,'mes':m,'rotulo':f'{MESES[m-1]}/{str(a)[-2:]}','realizado':f'{a}-{m:02}' in referencias} for a,m in periodos],
        'anos':anos,'quantidade_series':nseries,
        'newave_referencia':f'{MESES[ultimo[1]%12]}/{ultimo[0]+(1 if ultimo[1]==12 else 0)}',
        'ultimo_realizado':f'{ultimo[0]}-{ultimo[1]:02}',
        'referencias_realizadas':referencias,
        'gsf_mes_sem_realizado':meses_nao_informados,
        'mensal':medias_mensais,'anual':resumos_anuais,'caixas':caixas,
        'mercado_anual':mercado,
    }


def html_espera():
    return '''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font-family:Century Gothic,Arial,sans-serif;color:#344054;margin:0}.barra{background:#007E80;color:#fff;padding:18px;text-align:center;font-weight:bold;border-radius:12px}.card{background:#f8fafb;border:1px solid #dce5ea;border-radius:12px;padding:28px;margin:20px 12px}</style><main><div class="barra">Projeções Newave</div><div class="card"><b>Aguardando planilha</b><p>Envie <code>dados/Produtos Novo.xlsx</code> ao repositório e execute a atualização do GitHub Actions. Os demais módulos permanecem funcionando normalmente.</p></div></main></html>'''


HTML_TEMPLATE = r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Projeções Newave · Acompanhamento de mercado</title>
<style>
:root{--verde:#007E80;--borda:#e1e8ef;--texto:#263b4d;--suave:#687b8e}
*{box-sizing:border-box}body{font-family:'Century Gothic',CenturyGothic,Arial,sans-serif;color:var(--texto);background:#fff;margin:0}
main{max-width:1520px;margin:auto;padding:4px 10px 25px}
.section{border:1px solid var(--borda);border-radius:14px;margin:0 0 18px;overflow:hidden;background:#fff}
.bar{background:var(--verde);color:#fff;text-align:center;font-size:17px;font-weight:700;padding:13px 12px}
.desc{margin:13px 19px 4px;color:var(--suave);font-size:12px;line-height:1.65}
.info{display:flex;gap:12px;flex-wrap:wrap;margin:14px 16px 12px}
.pill{border:1px solid #dce8e8;border-radius:9px;padding:11px 14px;background:#f5faf9;min-width:170px;flex:1}
.pill small{color:#64788a;display:block;font-size:11px;margin-bottom:5px}.pill b{color:#007E80;font-size:19px}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;padding:14px}
.card{border:1px solid var(--borda);border-radius:13px;background:#f9fbfc;padding:13px;min-width:0}
.year{font-weight:800;font-size:20px;color:#007E80;margin:0 0 8px}.card table{border-collapse:collapse;width:100%;font-size:11px;font-variant-numeric:tabular-nums}
.card th{text-align:right;color:#687586;padding:5px 3px;white-space:nowrap;font-size:10px}.card th:first-child,.card td:first-child{text-align:left}
.card td{text-align:right;border-top:1px solid #e3eaf0;padding:7px 3px;white-space:nowrap}.card td:first-child{font-weight:700;color:#344054}
.card td.market-price{font-weight:700;color:#007E80}.card .market-date{font-size:10px;color:#687b8e;margin-top:8px}
.card .partial{font-size:10px;color:#9a621a;margin-top:8px}
.toolbar{display:flex;gap:13px;align-items:center;flex-wrap:wrap;padding:13px 18px 5px;font-size:12px;color:#425367}
.toolbar select{font:inherit;background:#fff;border:1px solid #cfdae4;border-radius:8px;padding:8px 12px;min-width:120px;color:#253a4a}
.chart{height:510px;width:100%}.chart.short{height:420px}
.box-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;padding:0 12px 12px}
.boxitem{border:1px solid var(--borda);border-radius:11px;overflow:hidden}.boxitem h3{text-align:center;color:#284659;margin:11px 0 0;font-size:14px}
.legend{padding:10px 17px;font-size:11px;color:#6d7d8f;line-height:1.65}
.caveat{background:#f8fafb;border-top:1px solid #edf1f4;padding:10px 17px;font-size:11px;line-height:1.7;color:#607185}
@media(max-width:1150px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:700px){.cards,.box-grid{grid-template-columns:1fr}.chart{height:480px}.card table{font-size:11px}.info{gap:5px} .pill{min-width:120px}}
</style></head><body><main>
<section class="section"><div class="bar">Projeções Newave — Resumo</div>
<div class="info"><div class="pill"><small>Referência do estudo</small><b id="refnewave"></b></div>
<div class="pill"><small>Cenários</small><b id="nseries"></b></div><div class="pill"><small>Realizados até</small><b id="realizado"></b></div></div>

<div id="year-cards" class="cards"></div>
</section>
<section class="section"><div class="bar">Preços — Projeções mensais e consolidado anual</div>
<div class="toolbar"><label for="select-ano-preco">Ano</label><select id="select-ano-preco"></select><span>Quatro submercados · barras agrupadas · último ponto = média anual</span></div>
<div id="precos-chart" class="chart"></div></section>
<section class="section"><div class="bar">GSF — Evolução mensal e consolidado anual</div>
<div class="toolbar"><label for="select-ano-gsf">Ano</label><select id="select-ano-gsf"></select><span>CVaR: 5% menores valores · EPaR: 5% maiores valores</span></div>
<div id="gsf-chart" class="chart"></div></section>
<section class="section"><div class="bar">Armazenamento do SIN — Projeção mensal</div>
<div class="toolbar"><label for="select-ano-ear">Ano</label><select id="select-ano-ear"></select><span>Média · CVaR 95% (menores níveis) · EPaR 95% (maiores níveis)</span></div>
<div id="ear-chart" class="chart"></div></section>
<section class="section"><div class="bar">Distribuições anuais dos preços — Boxplots por submercado</div>
<div class="box-grid"><div class="boxitem"><h3>SE/CO</h3><div id="box-se" class="chart short"></div></div><div class="boxitem"><h3>Sul</h3><div id="box-sul" class="chart short"></div></div>
<div class="boxitem"><h3>Nordeste</h3><div id="box-ne" class="chart short"></div></div><div class="boxitem"><h3>Norte</h3><div id="box-n" class="chart short"></div></div></div></section>
<section class="section"><div class="bar">GSF — Boxplot anual</div><div id="box-gsf" class="chart"></div></section>
<section class="section"><div class="bar">Armazenamento do SIN — Boxplot de setembro</div><div id="box-ear" class="chart"></div>
<div class="caveat">EAR: os valores das distribuições correspondem a setembro de cada ano (último mês do período seco de SE/CO). Para o ano já realizado, todos os cenários coincidem com o valor observado.</div></section>
<div class="caveat">Fontes: Processamento do Newave · PLD CCEE e EAR ONS provenientes do histórico do painel.</div>
</main><script>__PLOTLY__</script><script>
const D=__DADOS__;
const nomes={'SE/CO':'SE/CO','Sul':'Sul','Nordeste':'Nordeste','Norte':'Norte','GSF':'GSF','EAR SIN':'EAR SIN'};
const meses=['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
const cores={'SE/CO':'#2563EB','Sul':'#D97706','Nordeste':'#DC2626','Norte':'#0D9488'};
const config={responsive:true,displaylogo:false,modeBarButtonsToRemove:['lasso2d','select2d']};
const layout0={font:{family:'Century Gothic,CenturyGothic,Arial,sans-serif',color:'#344054',size:11},paper_bgcolor:'#fff',plot_bgcolor:'#fff',margin:{l:92,r:22,t:24,b:90},hovermode:'closest',legend:{orientation:'h',x:.5,xanchor:'center',y:-.23,yanchor:'top'},xaxis:{showgrid:false,showline:true,linecolor:'#61758a'},yaxis:{gridcolor:'#e9eef2',zeroline:false,title:{standoff:15}}};
function fmt(x,p=2){return x==null||!Number.isFinite(Number(x))?'—':Number(x).toLocaleString('pt-BR',{minimumFractionDigits:p,maximumFractionDigits:p});}
function unit(k){return k==='GSF'||k==='EAR SIN'?'%':'R$/MWh'}
function info(k,year){return D.anual[k]?.[String(year)]||null}
function renderCards(){const el=document.getElementById('year-cards');const vars=['SE/CO','Sul','Nordeste','Norte','GSF','EAR SIN'];
 el.innerHTML=D.anos.map(y=>{const mk=D.mercado_anual?.[String(y)]||null;
 let linhas=vars.map(k=>{let d=info(k,y);if(!d)return '';
 const precoMercado=k==='SE/CO'&&mk?fmt(mk.preco):'—';
 const mercadoTitle=k==='SE/CO'&&mk?` title="Último preço médio diário do contrato anual SE/CO em ${mk.data.slice(8,10)}/${mk.data.slice(5,7)}/${mk.data.slice(0,4)}"`:'';
 return `<tr><td>${k}${k==='EAR SIN'?' · Set':''}</td><td>${fmt(d.media)}</td><td>${fmt(d.cvar)}</td><td>${fmt(d.epar)}</td><td class="${k==='SE/CO'&&mk?'market-price':''}"${mercadoTitle}>${precoMercado}</td></tr>`}).join('');
 const dataMercado=mk?`<div class="market-date">Mercado SE/CO · contrato anual · ${mk.data.slice(8,10)}/${mk.data.slice(5,7)}/${mk.data.slice(0,4)}</div>`:'';
 return `<article class="card"><div class="year">${y}</div><table><thead><tr><th>Variável</th><th>Média</th><th>CVaR 95%</th><th>EPaR 95%</th><th>Mercado</th></tr></thead><tbody>${linhas}</tbody></table>${dataMercado}<div class="partial" style="color:#64748b">Preços: R$/MWh · GSF e EAR: %</div></article>`;}).join('');}
function populate(){['select-ano-preco','select-ano-gsf','select-ano-ear'].forEach(id=>{let sel=document.getElementById(id);sel.innerHTML=D.anos.map(y=>`<option value="${y}">${y}</option>`).join('');sel.value=String(D.anos.includes(2027)?2027:D.anos.at(-1));sel.addEventListener('change',()=>id==='select-ano-preco'?drawPrices():(id==='select-ano-gsf'?drawGSF():drawEAR()))});
 document.getElementById('refnewave').textContent='Newave '+D.newave_referencia;
 document.getElementById('nseries').textContent=Number(D.quantidade_series).toLocaleString('pt-BR');
 const [a,m]=D.ultimo_realizado.split('-');document.getElementById('realizado').textContent=meses[Number(m)-1]+'/'+a;
}
function graphLayout(y){return {...layout0,margin:{...layout0.margin},xaxis:{...layout0.xaxis},yaxis:{...layout0.yaxis,title:{text:y,standoff:16}}};}
function drawPrices(){const year=Number(document.getElementById('select-ano-preco').value);const ids=D.periodos.map((p,i)=>p.ano===year?i:-1).filter(i=>i>=0);
 const labels=ids.map(i=>meses[D.periodos[i].mes-1]).concat('Ano');
 const traces=['SE/CO','Sul','Nordeste','Norte'].map(k=>{
 const dados=ids.map(i=>D.mensal[k][i]);const anual=info(k,year);
 const y=dados.map(x=>x.media).concat(anual?.media??null);
 const custom=dados.map((x,i)=>D.periodos[ids[i]].realizado?'Realizado':'Cenário').concat('Média anual por série');
 return {type:'bar',name:k,x:labels,y,marker:{color:cores[k],opacity:dados.map((x,i)=>D.periodos[ids[i]].realizado?1:.8).concat(1)},customdata:custom,
 hovertemplate:`<b>${k}</b><br>%{x}<br>R$ %{y:,.2f}/MWh<br>%{customdata}<extra></extra>`};});
 const lay=graphLayout('Preço (R$/MWh)');lay.barmode='group';lay.margin.b=72;
 lay.xaxis={...lay.xaxis,title:{text:'Mês / Consolidado anual',standoff:14},type:'category',categoryorder:'array',categoryarray:labels};
 Plotly.react('precos-chart',traces,lay,config);
}
function drawGSF(){const year=Number(document.getElementById('select-ano-gsf').value);const ids=D.periodos.map((p,i)=>p.ano===year?i:-1).filter(i=>i>=0);
 const xx=ids.map(i=>meses[D.periodos[i].mes-1]);const mensal=ids.map(i=>D.mensal.GSF[i]);const annual=info('GSF',year);
 const defs=[['Média','media','#2563EB'],['CVaR 95%','cvar','#DC2626'],['EPaR 95%','epar','#0D9488']];
 const trs=[];for(let [nm,key,color] of defs){trs.push({type:'scatter',mode:'lines+markers',name:nm,legendgroup:nm,x:xx,y:mensal.map(v=>v[key]),line:{color,width:2.8,shape:'spline'},marker:{size:6},hovertemplate:'%{x}<br>%{y:.2f}%<extra>'+nm+'</extra>'});
 trs.push({type:'scatter',mode:'markers',name:nm+' · Ano',legendgroup:nm,showlegend:false,x:['Ano'],y:[annual?.[key]??null],marker:{symbol:'diamond',size:12,color},hovertemplate:'Consolidado anual<br>%{y:.2f}%<extra>'+nm+'</extra>'});}
 const lay=graphLayout('GSF (%)');lay.xaxis={...lay.xaxis,title:{text:'Mês',standoff:14},categoryorder:'array',categoryarray:[...xx,'Ano']};
 Plotly.react('gsf-chart',trs,lay,config);
}
function drawEAR(){const year=Number(document.getElementById('select-ano-ear').value);const ids=D.periodos.map((p,i)=>p.ano===year?i:-1).filter(i=>i>=0);
 const labels=ids.map(i=>meses[D.periodos[i].mes-1]);const arr=ids.map(i=>D.mensal['EAR SIN'][i]);
 const defs=[['Média','media','#2563EB'],['CVaR 95%','cvar','#DC2626'],['EPaR 95%','epar','#0D9488']];
 const tr=defs.map(([nm,k,c])=>({type:'scatter',mode:'lines+markers',name:nm,x:labels,y:arr.map(v=>v[k]),line:{color:c,width:2.6,shape:'spline'},marker:{size:6},hovertemplate:'%{x}<br>%{y:.2f}%<extra>'+nm+'</extra>'}));
 const lay=graphLayout('Níveis dos reservatórios (%)');lay.xaxis={...lay.xaxis,title:{text:'Mês'},categoryorder:'array',categoryarray:labels};
 Plotly.react('ear-chart',tr,lay,config);
}
function drawBox(id,k,cor){const tr=D.anos.map(y=>({type:'box',name:String(y),y:D.caixas[k]?.[String(y)]||[],boxpoints:false,quartilemethod:'linear',line:{width:1.7,color:cor},fillcolor:cor==='#2563EB'?'rgba(37,99,235,.18)':cor==='#D97706'?'rgba(217,119,6,.16)':cor==='#DC2626'?'rgba(220,38,38,.14)':cor==='#0D9488'?'rgba(13,148,136,.18)':'rgba(80,110,160,.16)',
 hovertemplate:'Ano '+y+'<br>%{y:.2f}<extra>'+k+'</extra>'}));
 const lay=graphLayout(unit(k)==='%'?k+' (%)':'Preço (R$/MWh)');lay.showlegend=false;lay.margin={l:80,r:20,t:20,b:48};lay.xaxis={...lay.xaxis,title:{text:'Ano'}};
 Plotly.react(id,tr,lay,config);}
renderCards();populate();drawPrices();drawGSF();drawEAR();drawBox('box-se','SE/CO',cores['SE/CO']);drawBox('box-sul','Sul',cores['Sul']);drawBox('box-ne','Nordeste',cores['Nordeste']);drawBox('box-n','Norte',cores['Norte']);drawBox('box-gsf','GSF','#64748B');drawBox('box-ear','EAR SIN','#64748B');
function ajustarAltura(){if(window.parent!==window)window.parent.postMessage({tipo:'altura-painel-ons',id:'newave',altura:document.body.scrollHeight+24},'*');}
window.addEventListener('load',ajustarAltura);window.addEventListener('resize',()=>{ajustarAltura();document.querySelectorAll('.js-plotly-plot').forEach(x=>Plotly.Plots.resize(x))});
if(window.ResizeObserver)new ResizeObserver(ajustarAltura).observe(document.body);
setTimeout(ajustarAltura,600);
</script></body></html>'''


def pagina_html(dados):
    js=json.dumps(dados,ensure_ascii=False,separators=(',',':'),allow_nan=False).replace('</','<\\/')
    return HTML_TEMPLATE.replace('__PLOTLY__',get_plotlyjs()).replace('__DADOS__',js)


def integrar_index():
    if not INDEX.exists():
        print('Newave: graficos/index.html não encontrado; gerado apenas newave.html')
        return
    txt=INDEX.read_text(encoding='utf-8')
    if 'id="botao-newave"' not in txt:
        botao='<button role="tab" id="botao-newave" aria-selected="false" aria-controls="aba-newave" onclick="selecionar(\'newave\')">Projeções Newave</button>'
        secao='<section class="aba" id="aba-newave" role="tabpanel" aria-labelledby="botao-newave"><div class="molde"><iframe id="quadro-newave" title="Projeções Newave" src="newave.html" loading="lazy" style="height:4200px"></iframe></div></section>'
        txt=txt.replace('</nav>',botao+'\n</nav>',1)
        txt=txt.replace('</main>',secao+'\n</main>',1)
    # Atualiza listas explícitas de abas do menu (e detecção de altura).
    pat=re.compile(r'\[(?:\s*[\'\"](?:ear|ena|pld|mercado|operacao|newave)[\'\"]\s*,?)+\]')
    def inserir(m):
        s=m.group(0)
        nomes=re.findall(r"['\"](ear|ena|pld|mercado|operacao|newave)['\"]",s)
        if 'newave' not in nomes and {'ear','ena','pld'}.issubset(nomes):return s[:-1]+",'newave']"
        return s
    txt=pat.sub(inserir,txt)
    # Integração de impressão existente: adicionar o sexto painel.
    txt=re.sub(r"(\['mercado','Preço de Mercado'\])(?=\s*\])",r"\1,['newave','Projeções Newave']",txt)
    INDEX.write_text(txt,encoding='utf-8')
    print('Newave: botão e iframe integrados no painel principal.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--somente-local',action='store_true',help='Apenas lê arquivos locais, sem download')
    args=parser.parse_args()
    GRAFICOS.mkdir(parents=True,exist_ok=True);DADOS.mkdir(parents=True,exist_ok=True)
    candidato=next((p for p in ARQUIVOS_XLSX if p.exists()),None)
    if not candidato:
        PAGINA.write_text(html_espera(),encoding='utf-8')
        integrar_index()
        print('Newave: aguardando dados/Produtos Novo.xlsx — gerada página informativa.')
        return
    print('Newave: lendo',candidato)
    periodos,mat,n=ler_xlsx(candidato)
    resumo=consolidar(periodos,mat,n)
    RESUMO.write_text(json.dumps(resumo,ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf8')
    PAGINA.write_text(pagina_html(resumo),encoding='utf8')
    integrar_index()
    print(f'Newave: referência Newave {resumo["newave_referencia"]}, {len(resumo["anos"])} anos, {n} cenários.')
    print(f'Newave: {len(resumo["referencias_realizadas"])} meses substituídos por realizados do PLD e EAR; GSF sem realizado em {len(resumo["gsf_mes_sem_realizado"])} meses.')
    print('Newave: arquivos gerados',PAGINA,RESUMO)

if __name__=='__main__':main()
