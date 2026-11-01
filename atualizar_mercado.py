"""Atualização incremental de 17 produtos BBCE e painel Preço de Mercado.

Entradas em dados/:
  historico_precos_bbce.csv             — histórico filtrado inicial
  todosnegocios.xlsx                    — novos negócios (nome único obrigatório)

Saídas em dados/:
  historico_precos_bbce.csv             — histórico de 17 produtos, atualizado
  precos_mercado_diario.csv             — médias diárias sem preenchimento artificial
  produtos_mercado.csv                  — catálogo dos 17 produtos
Saída em gráficos/: mercado.html, além da atualização da aba no index.html.

Média diária ponderada por Q.M (MWh). Preços dos dias não operados são
carregados apenas na visualização e na comparação, nunca gravados como negócios.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from plotly.offline import get_plotlyjs

ROOT = Path(__file__).resolve().parent
DADOS = ROOT / 'dados'
GRAFICOS = ROOT / 'graficos'
CSV = DADOS / 'historico_precos_bbce.csv'
DIARIO = DADOS / 'precos_mercado_diario.csv'
CATALOGO = DADOS / 'produtos_mercado.csv'
HTML = GRAFICOS / 'mercado.html'
INDEX = GRAFICOS / 'index.html'
VERDE = '#007E80'
COLUNAS = ['PRODUTO', 'DATA/HORA', 'Q.N', 'U.N.', 'Q.M', 'U.M.', 'PREÇO',
           'TIPO DE CONTRATO', 'TENDÊNCIA', 'STATUS']

# Os 17 produtos são uma lista fechada, preservando os nomes oficiais.
# Corrigidos dois erros de pontuação do pedido: MAR/27 e Q2_27 são itens
# distintos, e o código do quarto trimestre de 2027 é Q4_27.
PRODUTOS = [
    ('FEN - SE CON MEN OUT/26 - Preço Fixo', 'OUT/26'),
    ('FEN - SE CON MEN NOV/26 - Preço Fixo', 'NOV/26'),
    ('FEN - SE CON MEN DEZ/26 - Preço Fixo', 'DEZ/26'),
    ('FEN - SE CON TRI OUT/26 DEZ/26 - Preço Fixo', 'Q4_26'),
    ('FEN - SE CON ANU JAN/27 DEZ/27 - Preço Fixo', '2027'),
    ('FEN - SE CON TRI JAN/27 MAR/27 - Preço Fixo', 'Q1_27'),
    ('FEN - SE CON MEN JAN/27 - Preço Fixo', 'JAN/27'),
    ('FEN - SE CON MEN FEV/27 - Preço Fixo', 'FEV/27'),
    ('FEN - SE CON MEN MAR/27 - Preço Fixo', 'MAR/27'),
    ('FEN - SE CON TRI ABR/27 JUN/27 - Preço Fixo', 'Q2_27'),
    ('FEN - SE CON TRI JUL/27 SET/27 - Preço Fixo', 'Q3_27'),
    ('FEN - SE CON TRI OUT/27 DEZ/27 - Preço Fixo', 'Q4_27'),
    ('FEN - SE CON ANU JAN/28 DEZ/28 - Preço Fixo', '2028'),
    ('FEN - SE CON ANU JAN/29 DEZ/29 - Preço Fixo', '2029'),
    ('FEN - SE CON ANU JAN/30 DEZ/30 - Preço Fixo', '2030'),
    ('FEN - SE CON ANU JAN/31 DEZ/31 - Preço Fixo', '2031'),
    ('FEN - SE CON ANU JAN/32 DEZ/32 - Preço Fixo', '2032'),
]
ORIGINAL_A_APELIDO = dict(PRODUTOS)
APELIDO_A_ORIGINAL = {apelido: original for original, apelido in PRODUTOS}
DEFAULT_CARDS = ['Q1_27', '2027', '2028', '2029', '2030']
PERIODOS = [('1d', 'Dia anterior'), ('7d', 'Semana anterior'),
            ('15d', '15 dias atrás'), ('30d', '30 dias atrás'),
            ('1y', 'Ano passado')]


def corrigir_texto(valor) -> str:
    if pd.isna(valor):
        return ''
    s = ' '.join(str(valor).split())
    if any(x in s for x in ('Ã', 'Â', 'â€')):
        try:
            s = s.encode('latin-1').decode('utf-8')
        except (UnicodeError, ValueError):
            pass
    return ' '.join(s.split())


def normalizar_cabecalho(s):
    s = unicodedata.normalize('NFKD', corrigir_texto(s))
    return ''.join(c for c in s.upper() if c.isalnum() and not unicodedata.combining(c))


def numero(v):
    if pd.isna(v) or str(v).strip() == '':
        return float('nan')
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(' ', '')
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    return pd.to_numeric(s, errors='coerce')


def normalizar(df: pd.DataFrame) -> pd.DataFrame:
    mapa = {normalizar_cabecalho(c): c for c in df.columns}
    faltando = [c for c in COLUNAS if normalizar_cabecalho(c) not in mapa]
    if faltando:
        raise ValueError(f'Colunas ausentes: {faltando}. Recebidas: {list(df.columns)}')
    out = df[[mapa[normalizar_cabecalho(c)] for c in COLUNAS]].copy()
    out.columns = COLUNAS
    for c in ['PRODUTO', 'U.N.', 'U.M.', 'TIPO DE CONTRATO', 'TENDÊNCIA', 'STATUS']:
        out[c] = out[c].map(corrigir_texto)
    out['DATA/HORA'] = pd.to_datetime(out['DATA/HORA'], dayfirst=True, errors='coerce')
    for c in ['Q.N', 'Q.M', 'PREÇO']:
        out[c] = out[c].map(numero).astype(float)
    out = out[out['PRODUTO'].isin(ORIGINAL_A_APELIDO)].copy()
    # Descartamos negócios cancelados; mantemos o status histórico "Não".
    out = out[~out['STATUS'].str.casefold().isin(['cancelado', 'cancelada', 'inativo', 'inativa'])]
    out = out.dropna(subset=['DATA/HORA', 'PREÇO'])
    return out[COLUNAS]


def carregar_historicos() -> pd.DataFrame:
    """Lê exclusivamente o CSV histórico de 17 produtos, sem duplicar bases.

    Os antigos arquivos .csv.gz não participam mais da leitura e podem ser
    removidos do repositório depois da primeira atualização validada.
    """
    if not CSV.exists():
        raise FileNotFoundError('Envie dados/historico_precos_bbce.csv ao repositório.')
    bruto = pd.read_csv(CSV, encoding='utf-8-sig', low_memory=False)
    base = normalizar(bruto)
    print(f'Mercado: {len(base):,} linhas dos produtos-alvo no histórico CSV.')
    return base


def localizar_excel() -> Path | None:
    """O usuário sempre envia dados/todosnegocios.xlsx (grafia correta)."""
    correto = DADOS / 'todosnegocios.xlsx'
    if correto.is_file():
        return correto
    outros = [p.name for p in DADOS.iterdir()
              if p.is_file() and p.name.lower() in {'todosnegicios.xlsx', 'todosnegócios.xlsx'}]
    if outros:
        raise FileNotFoundError(
            f'Arquivo com nome incorreto: {outros}. Renomeie para dados/todosnegocios.xlsx.')
    return None


def importar_novos() -> pd.DataFrame | None:
    excel = localizar_excel()
    if not excel:
        print('Mercado: não há planilha nova em dados/; será usado o histórico existente.')
        return None
    bruto = pd.read_excel(excel, engine='openpyxl')
    novo = normalizar(bruto)
    print(f'Mercado: {len(bruto):,} registros no Excel; {len(novo):,} válidos dos 17 produtos, excluídos cancelados.')
    return novo


def consolidar(base, novos):
    base_unica = base.drop_duplicates(subset=COLUNAS, keep='first')
    partes = [base_unica] if novos is None else [base_unica, novos]
    junto = pd.concat(partes, ignore_index=True)
    n_antes = len(junto)
    junto = junto.drop_duplicates(subset=COLUNAS, keep='first')
    junto = junto.sort_values(['DATA/HORA', 'PRODUTO', 'PREÇO'], kind='stable').reset_index(drop=True)
    novos_unicos = len(junto) - len(base_unica)
    print(f'Mercado: {novos_unicos:,} novos negócios incorporados; '
          f'{n_antes-len(junto):,} repetidos na importação; '
          f'{len(junto):,} registros consolidados.')
    return junto


def calc_diario(hist):
    ativos = hist.copy()
    ativos['APELIDO'] = ativos['PRODUTO'].map(ORIGINAL_A_APELIDO)
    ativos['DIA'] = ativos['DATA/HORA'].dt.normalize()
    ativos['PESO'] = ativos['Q.M'].where(ativos['Q.M'] > 0, 0).fillna(0)
    ativos['POND'] = ativos['PREÇO'] * ativos['PESO']
    agg = ativos.groupby(['APELIDO', 'DIA'], as_index=False).agg(
        SOMA_POND=('POND', 'sum'), VOLUME_MWH=('PESO', 'sum'),
        MEDIA_SIMPLES=('PREÇO', 'mean'), NEGOCIOS=('PREÇO', 'size'))
    agg['PRECO_MEDIO'] = agg['SOMA_POND'].div(agg['VOLUME_MWH'].where(agg['VOLUME_MWH'] > 0))\
                                   .fillna(agg['MEDIA_SIMPLES'])
    return agg[['APELIDO', 'DIA', 'PRECO_MEDIO', 'VOLUME_MWH', 'NEGOCIOS']].sort_values(['APELIDO', 'DIA'])


def inserir_aba_index():
    """Compatibilidade com o workflow existente, executado após atualizar_ena.py."""
    if not INDEX.exists():
        print('Mercado: graficos/index.html ausente; gerado apenas mercado.html.')
        return
    html = INDEX.read_text(encoding='utf-8')
    if 'id="botao-mercado"' in html:
        print('Mercado: aba já presente no index.html.')
        return
    botao = ('<button role="tab" id="botao-mercado" aria-selected="false" '
             'aria-controls="aba-mercado" onclick="selecionar(\'mercado\')">'
             'Mercado · Negócios</button>')
    secao = ('<section class="aba" id="aba-mercado" role="tabpanel" '
             'aria-labelledby="botao-mercado"><div class="molde"><iframe '
             'id="quadro-mercado" title="Preço de Mercado" src="mercado.html" '
             'loading="eager" style="height:1250px"></iframe></div></section>')
    trocas = [
        ('</nav></header>', botao + '</nav></header>'),
        ('</main>', secao + '</main>'),
        ("['ear','ena','pld']", "['ear','ena','pld','mercado']"),
        ("['ena','ear','pld']", "['ena','ear','pld','mercado']"),
        ("['ena','pld'].includes(visao)", "['ena','pld','mercado'].includes(visao)"),
    ]
    for de, para in trocas:
        if de not in html:
            raise RuntimeError(f'graficos/index.html incompatível: {de!r}. Corrija a integração, preservando as outras abas.')
        html = html.replace(de, para)
    html = html.replace('Fonte: ONS / CCEE', 'Fonte: ONS / CCEE / Mercado')
    INDEX.write_text(html, encoding='utf-8')
    print('Mercado: quarta aba integrada ao painel principal.')


def pagina_mercado(diario, hoje):
    # Catálogo fixo: todos os 17 produtos da solicitação continuam selecionáveis.
    mapa = {}
    for alias, g in diario.groupby('APELIDO'):
        mapa[alias] = {
            'datas': g['DIA'].dt.strftime('%Y-%m-%d').tolist(),
            'precos': [round(float(x), 5) for x in g['PRECO_MEDIO']],
            'qtd': [int(x) for x in g['NEGOCIOS']],
            'volumes': [round(float(x), 2) for x in g['VOLUME_MWH']],
        }
    labels = [alias for _, alias in PRODUTOS]
    ultimo_dia = max((pd.Timestamp(d) for x in mapa.values() for d in x['datas']), default=hoje)
    dados_js = json.dumps(mapa, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    nomes_js = json.dumps(labels, ensure_ascii=False)
    originais_js = json.dumps(APELIDO_A_ORIGINAL, ensure_ascii=False).replace('</', '<\\/')
    periodos_js = json.dumps(PERIODOS, ensure_ascii=False)
    defaults_js = json.dumps(DEFAULT_CARDS)
    plotly_js = get_plotlyjs()
    # A data de referência é a data mais nova que o histórico contém, e não a data
    # de processamento. Quando não houver negociação, usa o último dia anterior.
    return '''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preço de Mercado — Acompanhamento de mercado</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#193245;font-family:'Century Gothic',CenturyGothic,Arial,sans-serif}
.panel{border:1px solid #e3eaf0;border-radius:15px;overflow:hidden;background:#fff;margin:0 10px 16px}
.bar{background:#007E80;color:white;padding:13px 18px;text-align:center;font-size:17px;font-weight:700}
.caption{font-size:12px;color:#667085;line-height:1.5;margin:12px 20px}
.cards{padding:17px 17px 20px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.card{min-width:0;border:1px solid #dae5ec;border-radius:13px;background:#f8fafb;padding:12px;box-shadow:0 1px 3px #12233308}
.card-top{display:flex;align-items:center;justify-content:space-between;gap:7px;margin-bottom:12px}
.card-index{font-size:16px;letter-spacing:-.015em;color:#007E80;font-weight:800}
.card-date{font-size:10px;color:#667085}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:11px}
.field label{font-size:10px;color:#667085;font-weight:700}
.field select{width:100%;min-width:0;padding:8px 7px;font:inherit;font-size:12px;border-radius:7px;border:1px solid #cbd8df;background:#fff;color:#16384a}
.strong-number{font-size:24px;font-weight:750;letter-spacing:-.04em;color:#113e4c;line-height:1.15}
.metric-label{color:#64748b;font-size:10px;margin-top:4px}
.comparison{border-top:1px solid #dce5ea;margin-top:11px;padding-top:10px;display:grid;grid-template-columns:1fr auto;gap:8px;align-items:end}
.compare-number{font-size:16px;font-weight:700;color:#385467}
.delta{font-weight:750;font-size:14px;white-space:nowrap}
.delta-indice{font-weight:700;font-size:13px;white-space:nowrap;margin-top:6px}
.delta-indice.pos{color:#b45309}.delta-indice.neg{color:#0f766e}.delta-indice.zero{color:#64748b}
.delta-unidade{font-weight:500;font-size:10px;letter-spacing:0}
.delta-duplo{text-align:right}
.delta.pos{color:#b45309}.delta.neg{color:#0f766e}.delta.zero{color:#64748b}
.card-foot{font-size:10px;color:#64748b;margin-top:9px;line-height:1.5;overflow-wrap:anywhere}
.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;padding:10px 20px 8px}
.search{min-width:205px;flex:1;font:inherit;font-size:13px;padding:11px 13px;border:1px solid #cbd8df;border-radius:9px}
.toolbar button{padding:10px 13px;border:1px solid #c8d5db;border-radius:8px;background:#fff;color:#24525b;cursor:pointer;font:inherit;font-size:12px}
.selection{padding:2px 20px 10px;display:flex;flex-wrap:wrap;gap:7px}
.chip{display:inline-flex;gap:8px;align-items:center;border-radius:20px;border:1px solid #d8e2ea;padding:7px 9px;font-size:12px;background:#f6fafb}
.chip button{border:0;background:transparent;cursor:pointer;color:#637b83;font-size:14px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex-shrink:0}
.catalog{padding:10px;max-height:118px;overflow:auto;margin:0 20px 8px;border:1px solid #e4eaf0;border-radius:9px;display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.catalog label{cursor:pointer;display:flex;align-items:center;gap:8px;font-size:12px;padding:7px;border-radius:7px;background:#f8fafb}
.catalog input{accent-color:#007E80}.catalog label:hover{background:#e8f5f3}
#market-chart{width:100%;height:610px;background:#0e0e0e}
.chart-shell{margin:4px 18px 8px;border:1px solid #292929;border-radius:10px;overflow:hidden;background:#0e0e0e}
@media(max-width:700px){.chart-shell{margin:4px 8px}.delta-duplo{text-align:left}}
.note{padding:0 20px 15px;color:#667085;font-size:11px;line-height:1.55}
@media(max-width:1400px){.cards{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.strong-number{font-size:21px}}
@media(max-width:540px){.cards{grid-template-columns:1fr}.toolbar{padding:12px}.catalog{margin:0 12px 8px}.selection{padding:4px 12px 8px}}
</style>
<script>''' + plotly_js + '''</script></head><body>
<section class="panel"><div class="bar">Preço de Mercado — Resumo</div>
<p class="caption">Preço atual versus referência. Δ em R$/MWh e Δ do índice de preços (base 100) em p.p.</p>
<div id="market-cards" class="cards"></div>
</section>
<section class="panel"><div class="bar">Preço de Mercado — Evolução diária</div>
<p class="caption">Preço médio diário ponderado por MWh. Nos dias sem negócios, mantém-se o último preço disponível.</p>
<div class="toolbar"><input type="search" class="search" id="search" placeholder="Buscar produto (ex.: Q1_27, 2028, NOV/26)" aria-label="Buscar produto">
<button type="button" id="clear">Limpar seleção</button><button type="button" id="add-visible">Adicionar resultados</button></div>
<div class="selection" id="selected"></div>
<div class="catalog" id="catalog" aria-label="Produtos negociados"></div>
<div class="chart-shell"><div id="market-chart"></div></div>
<footer class="note">Fonte: BBCE</footer>
</section>
<script>
const DIAS=''' + dados_js + ''';
const NOMES=''' + nomes_js + ''';
const ORIGINAIS=''' + originais_js + ''';
const PERIODOS=''' + periodos_js + ''';
const DEFAULTS=''' + defaults_js + ''';
const HOJE=''' + json.dumps(hoje.strftime('%Y-%m-%d')) + ''';
const ULTIMO=''' + json.dumps(pd.Timestamp(ultimo_dia).strftime('%Y-%m-%d')) + ''';
const CORES=['#45A0FF','#F6B23B','#FF6B73','#55D6BD','#C39AFF','#EE79D3','#A0D376','#F2BD73','#80B6EA','#EBA48C','#9DB0FB','#68D8ED','#B9A4EA','#FFAE68','#74B7D5','#5ECBA7','#E792B4'];
const CARTOES=Array.from({length:5},(_,i)=>({produto:DEFAULTS[i],periodo:'7d'}));
const SEL=new Set(['2027','NOV/26']);
const cardEl=document.getElementById('market-cards');
const chart=document.getElementById('market-chart');
const catalogEl=document.getElementById('catalog');
const selectedEl=document.getElementById('selected');
const searchEl=document.getElementById('search');
function safe(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function fmt(n){return n===null||n===undefined||!Number.isFinite(n)?'—':Number(n).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2})}
function br(iso){return iso?iso.slice(8,10)+'/'+iso.slice(5,7)+'/'+iso.slice(0,4):'—'}
function addDays(iso,n){let d=new Date(iso+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}
function subtractYear(iso){let d=new Date(iso+'T00:00:00Z');let dia=d.getUTCDate();d.setUTCFullYear(d.getUTCFullYear()-1);if(d.getUTCDate()!==dia)d.setUTCDate(0);return d.toISOString().slice(0,10)}
function dataComparacao(d,p){return p==='1d'?addDays(d,-1):p==='7d'?addDays(d,-7):p==='15d'?addDays(d,-15):p==='30d'?addDays(d,-30):subtractYear(d)}
function acharPreco(nome,data){let d=DIAS[nome];if(!d||!d.datas.length)return null;
 let i=d.datas.length-1;while(i>=0&&d.datas[i]>data)i--;
 let futuro=false;if(i<0){i=0;futuro=true} // ausência anterior: usa o primeiro registro da série.
 return {valor:d.precos[i],dia:d.datas[i],semAnterior:futuro};}
function montarCards(){cardEl.innerHTML=CARTOES.map((c,i)=>{
 let atual=acharPreco(c.produto,ULTIMO),alvo=dataComparacao(ULTIMO,c.periodo),antigo=acharPreco(c.produto,alvo);
 let delta=atual&&antigo?atual.valor-antigo.valor:null;
 // Índice de preços: referência = 100; sua diferença é expressa em pontos percentuais.
 let deltaIndice=atual&&antigo&&antigo.valor!==0?(100*atual.valor/antigo.valor-100):null;
 let classe=delta===null||Math.abs(delta)<.00001?'zero':delta>0?'pos':'neg';
 let sinal=delta===null?'':delta>0?'+':'';
 let opcoes=NOMES.map(p=>`<option value="${safe(p)}" ${p===c.produto?'selected':''}>${safe(p)}</option>`).join('');
 let pp=PERIODOS.map(([id,rot])=>`<option value="${id}" ${id===c.periodo?'selected':''}>${rot}</option>`).join('');
 let aviso=antigo&&antigo.semAnterior?'Sem registro anterior à data-alvo; usado o primeiro preço disponível.':'';
 return `<article class="card" data-card="${i}"><div class="card-top"><span class="card-index">${safe(c.produto)}</span><span class="card-date">Ref. ${br(ULTIMO)}</span></div>
 <div class="field"><label>Produto</label><select class="card-produto" aria-label="Produto do card ${i+1}">${opcoes}</select></div>
 <div class="field"><label>Comparar com</label><select class="card-periodo" aria-label="Período do card ${i+1}">${pp}</select></div>
 <div><div class="strong-number">${atual?'R$ '+fmt(atual.valor):'—'}</div><div class="metric-label">Preço atual · último registro ${atual?br(atual.dia):'—'}</div></div>
 <div class="comparison"><div><div class="compare-number">${antigo?'R$ '+fmt(antigo.valor):'—'}</div><div class="metric-label">Preço na referência · ${br(alvo)}${antigo?' (registro '+br(antigo.dia)+')':''}</div></div>
 <div class="delta-duplo"><div class="delta ${classe}">Δ ${delta===null?'—':sinal+fmt(delta)} <span class="delta-unidade">R$/MWh</span></div>
 <div class="delta-indice ${classe}">Δ ${deltaIndice===null?'—':(deltaIndice>0?'+':'')+fmt(deltaIndice)} <span class="delta-unidade">p.p.</span></div></div></div>
 ${aviso?`<div class="card-foot">${aviso}</div>`:''}
 </article>`;}).join('');
 cardEl.querySelectorAll('.card').forEach(div=>{
  const i=Number(div.dataset.card);div.querySelector('.card-produto').addEventListener('change',e=>{CARTOES[i].produto=e.target.value;montarCards();ajustarAltura()});
  div.querySelector('.card-periodo').addEventListener('change',e=>{CARTOES[i].periodo=e.target.value;montarCards();ajustarAltura()});
 });}
function cor(nome){return CORES[Math.max(0,NOMES.indexOf(nome))%CORES.length]}
function catalogo(){const q=searchEl.value.trim().toUpperCase();catalogEl.innerHTML=NOMES.filter(p=>p.toUpperCase().includes(q)).map(p=>
 `<label title="${safe(ORIGINAIS[p])}"><input type="checkbox" data-name="${safe(p)}" ${SEL.has(p)?'checked':''}><span class="dot" style="background:${cor(p)}"></span>${safe(p)}</label>`).join('');
 catalogEl.querySelectorAll('input').forEach(el=>el.addEventListener('change',()=>{el.checked?SEL.add(el.dataset.name):SEL.delete(el.dataset.name);atualizarChart()}));}
function tokens(){selectedEl.innerHTML=[...SEL].map(p=>`<span class="chip"><span class="dot" style="background:${cor(p)}"></span>${safe(p)}<button title="Remover ${safe(p)}" data-name="${safe(p)}">×</button></span>`).join('');
 selectedEl.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{SEL.delete(b.dataset.name);atualizarChart()}));}
function curve(nome){const d=DIAS[nome];if(!d||!d.datas.length)return null;
 let registros=new Map(d.datas.map((x,i)=>[x,{v:d.precos[i],q:d.qtd[i],vol:d.volumes[i]}]));
 let x=[],y=[],detalhe=[];let v=null;
 for(let t=Date.parse(d.datas[0]+'T00:00:00Z');t<=Date.parse(HOJE+'T00:00:00Z');t+=86400000){
  let iso=new Date(t).toISOString().slice(0,10),r=registros.get(iso);if(r)v=r.v;
  x.push(iso);y.push(v);detalhe.push(r?`Negociado (${r.q} negócios; ${fmt(r.vol)} MWh)`:'Carregado do último dia com negócio');
 }
 return {x,y,customdata:detalhe,name:nome,mode:'lines',line:{color:cor(nome),width:2.5,shape:'hv'},
 hovertemplate:'<b>'+safe(nome)+'</b><br>%{x|%d/%m/%Y}<br>R$ %{y:.2f}/MWh<br>%{customdata}<extra></extra>'};}
function desenhar(){
 let trs=[...SEL].map(curve).filter(Boolean);
 const pequeno=window.innerWidth<850;
 Plotly.react(chart,trs,{
  height:pequeno?570:615,
  margin:{l:pequeno?68:92,r:pequeno?24:220,t:64,b:pequeno?96:70},
  font:{family:'Century Gothic,CenturyGothic,Arial,sans-serif',color:'#E0E0E0',size:13},
  paper_bgcolor:'#0e0e0e',plot_bgcolor:'#0e0e0e',hovermode:'closest',
  title:{text:'Preço Médio de Mercado',x:.055,xanchor:'left',font:{color:'#E0E0E0',size:23}},
  hoverlabel:{bgcolor:'#1f1f1f',bordercolor:'#444',font:{color:'#FFFFFF',size:14}},
  xaxis:{title:'Dia da operação',type:'date',showline:true,linecolor:'#737373',linewidth:1,
    showgrid:true,gridcolor:'rgba(255,255,255,.08)',ticks:'outside',tickcolor:'#777',
    tickfont:{color:'#D6D6D6',size:12},titlefont:{color:'#E0E0E0'},zeroline:false},
  yaxis:{title:{text:'Preço (R$/MWh)',font:{color:'#E0E0E0',size:14},standoff:14},showline:true,linecolor:'#737373',linewidth:1,
    showgrid:true,gridcolor:'rgba(255,255,255,.08)',ticks:'outside',tickcolor:'#777',
    tickfont:{color:'#D6D6D6',size:12},titlefont:{color:'#E0E0E0'},zeroline:false},
  legend:pequeno?{orientation:'h',x:.5,xanchor:'center',y:-.22,yanchor:'top',
    bgcolor:'rgba(20,20,20,.62)',font:{color:'#fff',size:11}}:
    {orientation:'v',x:1.02,xanchor:'left',y:1,yanchor:'top',
    bgcolor:'rgba(20,20,20,.62)',bordercolor:'rgba(180,180,180,.2)',borderwidth:1,
    font:{color:'#fff',size:12}},
  showlegend:trs.length>0,
  annotations:trs.length?[]:[{text:'Selecione um produto',x:.5,y:.5,xref:'paper',yref:'paper',
    showarrow:false,font:{color:'#d5d5d5',size:16}}]
 },{responsive:true,displaylogo:false});}
function atualizarChart(){catalogo();tokens();desenhar();ajustarAltura()}
searchEl.addEventListener('input',catalogo);
document.getElementById('clear').addEventListener('click',()=>{SEL.clear();atualizarChart()});
document.getElementById('add-visible').addEventListener('click',()=>{let q=searchEl.value.trim().toUpperCase();NOMES.filter(x=>x.toUpperCase().includes(q)).forEach(x=>SEL.add(x));atualizarChart()});
function ajustarAltura(){if(window.parent!==window)window.parent.postMessage({tipo:'altura-painel-ons',id:'mercado',altura:document.body.scrollHeight+24},'*')}
window.addEventListener('load',ajustarAltura);window.addEventListener('resize',ajustarAltura);
if(window.ResizeObserver)new ResizeObserver(ajustarAltura).observe(document.body);
montarCards();atualizarChart();
</script></body></html>'''


def main():
    argp = argparse.ArgumentParser()
    argp.add_argument('--somente-local', action='store_true', help='Compatibilidade com a rotina existente')
    argp.add_argument('--data-referencia', help='Data visual do painel para teste (AAAA-MM-DD)')
    args = argp.parse_args()
    DADOS.mkdir(parents=True, exist_ok=True)
    GRAFICOS.mkdir(parents=True, exist_ok=True)
    base = carregar_historicos()
    novos = importar_novos()
    historico = consolidar(base, novos)
    diario = calc_diario(historico)
    # Somente o CSV é fonte de verdade; arquivos auxiliares são derivados.
    saida = historico[COLUNAS].copy()
    saida['DATA/HORA'] = saida['DATA/HORA'].dt.strftime('%d/%m/%Y %H:%M:%S')
    saida.to_csv(CSV, index=False, encoding='utf-8-sig', float_format='%.8g')
    diario.to_csv(DIARIO, index=False, encoding='utf-8-sig', date_format='%Y-%m-%d', float_format='%.5f')
    pd.DataFrame(PRODUTOS, columns=['PRODUTO', 'APELIDO']).to_csv(CATALOGO, index=False, encoding='utf-8-sig')
    hoje = pd.Timestamp(args.data_referencia) if args.data_referencia else pd.Timestamp(datetime.now(ZoneInfo('America/Sao_Paulo')).date())
    HTML.write_text(pagina_mercado(diario, hoje), encoding='utf-8')
    inserir_aba_index()
    print(f'Mercado: {len(historico):,} negócios, {len(PRODUTOS)} produtos, {len(diario):,} pontos diários. Última negociação {historico["DATA/HORA"].max():%d/%m/%Y}.')
    print(f'Mercado: gerado {HTML}')


if __name__ == '__main__':
    main()
