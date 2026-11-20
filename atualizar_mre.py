#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ajuste MRE — gerador autônomo de módulo para Acompanhamento de Mercado.

Entradas:
 - dados/mre.xlsx (aba MRE; títulos e posições detectados automaticamente)
 - graficos/operacao_sin/resumo.json (geração hidráulica SIN, ONS)
Saídas:
 - graficos/ajuste_mre.json
 - graficos/ajuste_mre.html
 - integração de botão/iframe em graficos/index.html
 - inclusão no graficos/impressao.html, se existir

Este script NÃO altera a planilha ou dados históricos originais. Pode ser rodado
novamente após atualizar os arquivos, sem repetir o botão no menu.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, date
import json
import math
from pathlib import Path
import re
import unicodedata
from zoneinfo import ZoneInfo
from zipfile import ZipFile
from xml.etree import ElementTree as ET

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'dados'
GRAF=ROOT/'graficos'
MONTHS=['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez']
KEYS=['sin','gf_sem','mre','gf_aj','aj_g','aj_gf','media5','mercado','gsf']
LABELS={
 'sin':'Geração hidráulica SIN (MWm)',
 'gf_sem':'Garantia Física sem ajuste (MWm)',
 'mre':'Geração hidráulica MRE (MWm)',
 'gf_aj':'Garantia Física com ajuste (MWm)',
 'aj_g':'Ajuste G (%)',
 'aj_gf':'Ajuste GF (%)',
 'media5':'Geração hidráulica média 5 anos (MWm)',
 'mercado':'MRE Mercado (%)',
 'gsf':'GSF (%)',
}
REF_GF_ANUAL_2027=53619.0
NS={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'p':'http://schemas.openxmlformats.org/package/2006/relationships'}


def norm(value):
 s=unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode().lower()
 return re.sub(r'[^a-z0-9]+',' ',s).strip()


def col_idx(a1):
 letters=re.match(r'[A-Z]+',a1.upper()).group(0)
 v=0
 for x in letters:v=v*26+ord(x)-64
 return v-1


def category(label):
 s=norm(label)
 if 'geracao hidraulica media' in s and '5' in s:return 'media5'
 if 'garantia fisica sem ajuste' in s:return 'gf_sem'
 if 'garantia fisica com ajuste' in s:return 'gf_aj'
 if 'geracao hidraulica mre' in s:return 'mre'
 if 'geracao hidraulica sin' in s:return 'sin'
 if s.startswith('ajuste gf'):return 'aj_gf'
 if s.startswith('ajuste g'):return 'aj_g'
 if s.startswith('mre mercado'):return 'mercado'
 if s=='gsf' or s.startswith('gsf '):return 'gsf'
 return None


def as_float(value):
 if isinstance(value,bool):return None
 if isinstance(value,(int,float)):
  return float(value) if math.isfinite(float(value)) else None
 if value is None:return None
 ss=str(value).strip().replace('\u00a0','').replace(' ','')
 if not ss:return None
 if ',' in ss:
  ss=ss.replace('.','').replace(',','.')
 try:
  v=float(ss)
  return v if math.isfinite(v) else None
 except ValueError:return None


def as_year(value):
 f=as_float(value)
 if f is not None and f.is_integer() and 1950<=f<=2200:return int(f)
 return None


def parse_xlsx_raw(path:Path,sheet_name='MRE'):
 """Leitura mínima OOXML, inclusive valor *armazenado* de células com fórmulas.

 Requer que a planilha tenha sido salva/recalculada pelo Excel antes do upload;
 fórmulas sem valor armazenado são tratadas como dados ausentes, nunca como zero.
 """
 with ZipFile(path) as z:
  wb=ET.fromstring(z.read('xl/workbook.xml'))
  rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
  rels={v.attrib['Id']:v.attrib['Target'] for v in rel}
  target=None
  for sheet in wb.findall('x:sheets/x:sheet',NS):
   if norm(sheet.attrib.get('name',''))==norm(sheet_name):
    target=rels.get(sheet.attrib.get('{'+NS['r']+'}id'))
    break
  if not target:raise ValueError(f'Aba "{sheet_name}" não encontrada em {path.name}.')
  if target.startswith('/'):xmlpath=target.lstrip('/')
  elif target.startswith('xl/'):xmlpath=target
  else:xmlpath='xl/'+target
  xmlpath=re.sub(r'/\./','/',xmlpath)
  strings=[]
  if 'xl/sharedStrings.xml' in z.namelist():
   ss=ET.fromstring(z.read('xl/sharedStrings.xml'))
   strings=[''.join(x.itertext()) for x in ss.findall('x:si',NS)]
  sheetxml=ET.fromstring(z.read(xmlpath))
  rows={}
  for c in sheetxml.findall('.//x:sheetData/x:row/x:c',NS):
   ref=c.attrib.get('r')
   if not ref:continue
   mm=re.search(r'\d+',ref)
   if not mm:continue
   row=int(mm.group(0))-1;col=col_idx(ref)
   t=c.attrib.get('t','')
   if t=='inlineStr':
    si=c.find('x:is',NS)
    val=''.join(si.itertext()) if si is not None else None
   else:
    v=c.find('x:v',NS)
    raw=v.text if v is not None else None
    if t=='s' and raw is not None:
     try:val=strings[int(raw)]
     except (ValueError,IndexError):val=None
    elif t in ('str','e','d'):val=raw
    elif t=='b':val=(raw=='1')
    else:val=as_float(raw)
   if val is not None:rows.setdefault(row,{})[col]=val
 return rows


def detect_tables(rows):
 found={};diagnostics={}
 for rownum,row in sorted(rows.items()):
  for col,v in sorted(row.items()):
   key=category(v) if isinstance(v,str) else None
   if not key:continue
   monthcols={}
   # Os 12 cabeçalhos podem se mover; não usar coordenadas fixas.
   for j in range(col+1,col+28):
    entry=norm(row.get(j,''))
    if entry in MONTHS:monthcols[MONTHS.index(entry)+1]=j
    if len(monthcols)==12:break
   if len(monthcols)<12:continue
   # Não aceitar se encontrou duas tabelas com o mesmo título (evita ambiguidade).
   if key in found:raise ValueError(f'Duas tabelas "{LABELS[key]}" encontradas: verificar títulos duplicados.')
   out={}
   for ry,yrrow in sorted(rows.items()):
    if ry<=rownum:continue
    year=as_year(yrrow.get(col))
    if year is None:continue
    rec={}
    for m,cc in monthcols.items():
     val=as_float(yrrow.get(cc))
     if val is not None:rec[str(m)]=round(val,10)
    if rec:out[str(year)]=rec
   found[key]=out
   diagnostics[key]={'cabecalho':rownum+1,'coluna_ano':col+1,'anos':len(out),
                     'meses':sum(len(v) for v in out.values())}
 missing=[LABELS[k] for k in KEYS if k not in found]
 if missing:raise ValueError('Tabelas não encontradas na aba MRE: '+', '.join(missing))
 return found,diagnostics


def operation_data(path):
 if not path.exists():return {},['Arquivo de resumo mensal do ONS não encontrado: '+str(path)]
 try:
  contents=json.loads(path.read_text(encoding='utf8'))
 except (ValueError,OSError) as exc:
  return {},['Resumo ONS não pôde ser lido: '+str(exc)]
 out=defaultdict(dict)
 for row in contents.get('monthly',[]):
  # Formato do módulo Operação: [ano,mes,térmica,eólica,solar,hidráulica,intercâmbio,carga].
  if not isinstance(row,list) or len(row)<6:continue
  y=as_year(row[0]);m=as_float(row[1]);v=as_float(row[5])
  if y and m is not None and m.is_integer() and 1<=int(m)<=12 and v is not None and v>0:
   out[str(y)][str(int(m))]=round(v,4)
 return dict(out),[]


def last_factor_avg(hist,asof,metric):
 vals=[]
 for ys,months in hist[metric].items():
  for ms,v in months.items():
   yy,mm=int(ys),int(ms)
   if (yy,mm)<(asof.year,asof.month) and v>0:vals.append((yy,mm,v))
 vals.sort()
 last=vals[-12:]
 return (sum(x[-1] for x in last)/len(last) if last else None,
         f'{last[0][0]:04d}-{last[0][1]:02d} a {last[-1][0]:04d}-{last[-1][1]:02d}' if last else 'sem dados',
         len(last))


def gf_projection(media5, year,base=REF_GF_ANUAL_2027):
 vals=media5.get(str(year),{})
 if not all(str(m) in vals and vals[str(m)]>0 for m in range(1,13)):
  return None
 mean=sum(vals[str(m)] for m in range(1,13))/12
 return {str(m): round(vals[str(m)]/mean*base,5) for m in range(1,13)}


def build_payload(tables,diagnostics,ons,asof,ons_warnings):
 g,pg,ng=last_factor_avg(tables,asof,'aj_g')
 gf,pgf,ngf=last_factor_avg(tables,asof,'aj_gf')
 # Ano base: último ano ONS completo. Se ONS não tiver, último ano
 # completo da geração SIN da planilha, identificado na interface.
 full_ons=[int(y) for y,v in ons.items() if len(v)==12 and int(y)<asof.year]
 full_xls=[int(y) for y,v in tables['sin'].items() if len(v)==12 and int(y)<asof.year]
 ref_year=max(full_ons or full_xls or [asof.year-1])
 if not ons.get(str(asof.year),{}).get(str(asof.month)):
  ons_warnings.append('Sem média hidráulica ONS para o mês em curso: projeção do mês ficará indisponível até a atualização da Operação.')
 derived={}
 for year in range(2027,asof.year+2):
  proj=gf_projection(tables['media5'],year)
  if proj:derived[str(year)]=proj
  elif year<=asof.year+1:ons_warnings.append(f'Não há 12 meses válidos na média hidráulica de cinco anos para {year}; projeção GF sem ajuste indisponível.')
 return {'meta':{
    'atualizado_em':asof.isoformat(),'mes_corrente':f'{asof.year:04d}-{asof.month:02d}',
    'ano_base_default':ref_year,'g_default':round(g,8) if g else None,
    'gf_default':round(gf,8) if gf else None,
    'g_janela':pg,'gf_janela':pgf,'g_n':ng,'gf_n':ngf,
    'gf_anual_base':REF_GF_ANUAL_2027,
    'fonte_mre':'dados/mre.xlsx (aba MRE)',
    'fonte_ons':'graficos/operacao_sin/resumo.json (ONS — Operação)',
    'avisos':ons_warnings},
   'tabelas':tables,'ons':ons,'gf_projetada':derived,'diagnostico':diagnostics}


def inject_index():
 path=GRAF/'index.html'
 if not path.is_file():raise FileNotFoundError('graficos/index.html não encontrado. Execute após a geração das outras abas.')
 html=path.read_text(encoding='utf8')
 button='<button role="tab" id="botao-mre" aria-selected="false" aria-controls="aba-mre" onclick="selecionar(\'mre\')">Ajuste MRE</button>'
 panel='<section class="aba" id="aba-mre" role="tabpanel" aria-labelledby="botao-mre"><div class="molde"><iframe id="quadro-mre" title="Ajuste MRE" src="ajuste_mre.html" loading="lazy" style="height:2100px"></iframe></div></section>'
 if 'id="botao-mre"' not in html:
  if '</nav>' not in html:raise ValueError('Não foi possível localizar o menu </nav> da página inicial.')
  html=html.replace('</nav>',button+'\n</nav>',1)
 if 'id="aba-mre"' not in html:
  if '</main>' not in html:raise ValueError('Não foi possível localizar </main> da página inicial.')
  html=html.replace('</main>',panel+'\n</main>',1)
 if 'id="botao-mre"' not in html or 'id="aba-mre"' not in html:raise RuntimeError('Falha na injeção da aba MRE.')
 path.write_text(html,encoding='utf8')


def inject_print_report():
 p=GRAF/'impressao.html'
 if not p.exists():return
 txt=p.read_text(encoding='utf8')
 if "['mre','Ajuste MRE','ajuste_mre.html']" in txt:return
 # O módulo de impressão usa uma lista JavaScript de páginas geradas.
 marker="['decomp','Projeções Decomp','decomp.html']"
 if marker not in txt:return
 txt=txt.replace(marker,marker+",\n ['mre','Ajuste MRE','ajuste_mre.html']",1)
 txt=txt.replace('Newave e Decomp</p>','Newave, Decomp e Ajuste MRE</p>')
 p.write_text(txt,encoding='utf8')


def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--hoje',help='Data de referência YYYY-MM-DD para teste reprodutível')
 args=parser.parse_args()
 asof=date.fromisoformat(args.hoje) if args.hoje else datetime.now(ZoneInfo('America/Sao_Paulo')).date()
 wbpath=DATA/'mre.xlsx'
 if not wbpath.is_file():raise FileNotFoundError(f'Planilha não encontrada: {wbpath}. Coloque-a na pasta dados/.')
 rows=parse_xlsx_raw(wbpath)
 tables,diagnostics=detect_tables(rows)
 ons,warnings=operation_data(GRAF/'operacao_sin'/'resumo.json')
 payload=build_payload(tables,diagnostics,ons,asof,warnings)
 GRAF.mkdir(parents=True,exist_ok=True)
 (GRAF/'ajuste_mre.json').write_text(json.dumps(payload,ensure_ascii=False,separators=(',',':')),encoding='utf8')
 (GRAF/'ajuste_mre.html').write_text(HTML,encoding='utf8')
 inject_index();inject_print_report()
 print('Ajuste MRE: tabelas detectadas automaticamente:')
 for key,stat in diagnostics.items():
  print(f" - {LABELS[key]}: {stat['anos']} anos, {stat['meses']} valores")
 print('Últimos 12 meses: Ajuste G =',f"{payload['meta']['g_default']:.4%}" if payload['meta']['g_default'] else 'ND',
       'e Ajuste GF =',f"{payload['meta']['gf_default']:.4%}" if payload['meta']['gf_default'] else 'ND')
 if '2027' in payload['gf_projetada']:
  p=payload['gf_projetada']['2027']
  print('GF sem ajuste projetada 2027: jan =',round(p['1'],2),'MWm; média anual =',round(sum(p.values())/12,3))
 for warn in warnings:print('AVISO:',warn)
 print('Gerados graficos/ajuste_mre.html e graficos/ajuste_mre.json; menu atualizado.')


HTML=r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ajuste MRE — Acompanhamento de mercado</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
:root{--green:#007e80;--green-dark:#006d6f;--ink:#203747;--muted:#63758a;--line:#dce6ed;--bg:#f7fafb}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font-family:"Century Gothic",CenturyGothic,Arial,sans-serif;font-size:13px}
main{max-width:1480px;margin:0 auto;padding:12px 14px 40px}
.box{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:#fff;margin-bottom:17px}
.bar{background:var(--green);color:#fff;text-align:center;font-weight:800;font-size:18px;padding:12px}
.desc{padding:11px 18px 0;color:var(--muted);font-size:12px;line-height:1.6}
.controls{padding:16px 18px;display:flex;flex-wrap:wrap;align-items:end;gap:12px 19px}
.control{display:flex;flex-direction:column;gap:6px}.control label,.subhead{font-size:12px;font-weight:700;color:#4b6074}
select,input[type="number"]{font:inherit;font-size:12px;border:1px solid #cbd9e2;border-radius:8px;padding:8px 10px;background:white;color:#264052;min-height:36px}
input[type="number"]{width:120px}input:disabled{background:#f2f6f8;color:#617586}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row label{font-size:12px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:7px;padding:5px 17px 13px}
.chip{font:inherit;font-size:12px;padding:7px 11px;border-radius:8px;border:1px solid #ccd8e0;background:white;cursor:pointer;color:#3f5869}
.chip.on{background:#e8f5f4;border-color:#58aba9;color:#007e80;font-weight:800}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
#plot{width:100%;height:540px}.muted{color:var(--muted)}.note{font-size:11px;line-height:1.7;color:#617286;padding:0 18px 14px}
.cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;padding:13px 16px 17px}
.card{border:1px solid var(--line);border-radius:11px;padding:13px;background:#fbfdfe;min-width:0}
.card .label{font-size:11px;color:var(--muted);font-weight:700}.card .value{font-size:22px;font-weight:800;color:#006d6f;margin:8px 0 5px}
.card .small{font-size:11px;color:#66788a;line-height:1.5}.card.negative .value{color:#b84a4a}
.premises{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;padding:16px}
.premise{border:1px solid var(--line);border-radius:10px;padding:14px;background:#fbfdfe}.premise h3{font-size:13px;color:var(--green);margin:0 0 12px}
.switch{display:flex;align-items:center;gap:7px;font-size:12px;color:#41596b;padding-top:6px}.switch input{accent-color:var(--green)}
.formula{margin:9px 16px 16px;border:1px solid #d9e5eb;border-radius:10px;background:#f5fafb;padding:12px 15px;color:#42596a;font-size:12px;line-height:1.7}
.table-wrap{overflow:auto;padding:4px 17px 16px}table{border-collapse:collapse;width:100%;min-width:850px;font-size:11px}
th,td{padding:8px 7px;text-align:right;border-bottom:1px solid #e8eff3;white-space:nowrap}th{background:#f0f6f7;color:#476072}th:first-child,td:first-child{text-align:left;font-weight:700}
td.proj{background:#eff8f7}td.missing{color:#a4adb6}.warn{margin:10px 16px;padding:10px 12px;background:#fff9ee;border:1px solid #f0dfb4;border-radius:8px;color:#765a2b;line-height:1.5}

.small-title{font-size:12px;font-weight:800;padding:10px 16px 0;color:#526778}
.month-edit-wrap{padding:6px 16px 16px;overflow-x:auto}
.month-edit-wrap table{min-width:1120px;width:100%;font-size:12px}
.month-edit-wrap th,.month-edit-wrap td{padding:8px 7px;text-align:center;vertical-align:middle}
.month-edit-wrap th:first-child,.month-edit-wrap td:first-child{text-align:left}
.month-edit-wrap th{white-space:normal;line-height:1.4}
.month-edit-wrap td input{width:112px;max-width:100%;border:1px solid #cbd9e2;border-radius:7px;padding:7px 8px;text-align:right;color:#203747;font-size:12px;min-height:34px}
.month-edit-wrap td input.edited{border-color:#07957d;background:#eaf7f2;font-weight:700}
.month-edit-wrap td input:focus{outline:2px solid #51a8a0;outline-offset:1px}
.month-edit-wrap td.calculated{color:#006e70;font-weight:700;background:#f4faf9}
.month-edit-wrap tr.edited-row td:first-child{color:#007d75}
.month-edit-wrap .origin{display:block;color:#788896;font-size:10px;font-weight:400;padding-top:4px}
.btn-mre{font:inherit;font-size:11px;padding:7px 10px;border-radius:7px;border:1px solid #c3d3db;background:white;color:#31566a;cursor:pointer}
.btn-mre:hover{border-color:#00877d;color:#007d75}
.btn-mre:disabled{opacity:.45;cursor:default}
.btn-mre.primary{background:#ecf7f4;border-color:#96cfc1;color:#007b6e;font-weight:700}
.matrix-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 17px 12px;flex-wrap:wrap}
.matrix-status{font-size:11px;color:#5e7182}
.matrix-section{padding:4px 17px 0;color:#007e80;font-size:13px;font-weight:800}
@media print{.matrix-actions button,.month-edit-wrap .actions-col{display:none!important}.month-edit-wrap{overflow:visible}.month-edit-wrap table{min-width:0;font-size:9px}.month-edit-wrap td input{width:82px;font-size:9px;padding:3px}}

@media(max-width:1220px){.cards{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1000px){.premises{grid-template-columns:1fr 1fr}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:620px){main{padding:8px}.premises,.cards{grid-template-columns:1fr}.bar{font-size:16px}.controls{gap:12px}.card .value{font-size:19px}#plot{height:480px}}
@media print{.premises .control,.chips{display:none!important}.box{break-inside:avoid}#plot{height:430px}main{max-width:none}}
</style></head><body><main>
<section class="box"><div class="bar">Ajuste MRE — Resumo</div>
<p class="desc">Ajuste MRE = geração hidráulica MRE / garantia física com ajuste − 1. Valores positivos representam energia secundária; negativos representam déficit de geração (GSF abaixo de 100%). Para o mês corrente, a geração hidráulica SIN é a média mensal disponível na aba Operação do SIN.</p>
<div class="cards" id="cards"></div><div class="note" id="updated"></div></section>
<section class="box"><div class="bar">Ajuste MRE — Evolução mensal por ano</div>
<div class="controls"><div class="control"><label for="variable">Grandeza</label><select id="variable"></select></div>
<label class="switch"><input type="checkbox" id="toggle-history" checked> Histórico</label>
<label class="switch"><input type="checkbox" id="toggle-forecast" checked> Projeções</label></div>
<div class="small-title">Anos para comparação (cores por ano; linhas contínuas = histórico; tracejadas = projeções)</div>
<div class="chips" id="years"></div><div id="plot"></div>
<p class="note" id="plot-note"></p></section>
<section class="box"><div class="bar">Premissas das projeções</div>
<p class="desc">A geração hidráulica SIN pode usar anos de referência diferentes para o ano em curso e o ano seguinte. Padrão dos fatores G e GF: média dos últimos 12 meses realizados. Os percentuais informados manualmente substituem o fator escolhido. As premissas e alterações mensais são salvas somente neste navegador.</p>
<div class="premises">
  <div class="premise"><h3>Geração hidráulica SIN — <span id="label-hidro-current">ano em curso</span></h3><div class="control"><label for="ref-hidro-current">Ano de geração para projeções</label><select id="ref-hidro-current"></select></div><p class="note" style="padding:10px 0 0">Para o mês em curso, utiliza a média ONS do próprio mês; para os meses seguintes deste ano, aplica o perfil do ano escolhido.</p></div>
  <div class="premise"><h3>Geração hidráulica SIN — <span id="label-hidro-next">ano seguinte</span></h3><div class="control"><label for="ref-hidro-next">Ano de geração para projeções</label><select id="ref-hidro-next"></select></div><p class="note" style="padding:10px 0 0">Usa o perfil mensal do ano selecionado, independentemente da referência adotada no ano em curso.</p></div>
  <div class="premise"><h3>Ajuste G</h3><div class="control"><label for="ref-g">Referência dos fatores</label><select id="ref-g"></select></div><label class="switch"><input id="manual-g-check" type="checkbox"> Informar fator manualmente</label><div class="row"><input id="manual-g" type="number" step="0.01" min="0.01" max="200" disabled><label for="manual-g">%</label></div></div>
  <div class="premise"><h3>Ajuste GF</h3><div class="control"><label for="ref-gf">Referência dos fatores</label><select id="ref-gf"></select></div><label class="switch"><input id="manual-gf-check" type="checkbox"> Informar fator manualmente</label><div class="row"><input id="manual-gf" type="number" step="0.01" min="0.01" max="200" disabled><label for="manual-gf">%</label></div></div>
</div>
<div class="formula">Geração MRE projetada = Geração hidráulica SIN × Ajuste G · Garantia Física ajustada = Garantia Física sem ajuste × Ajuste GF · Ajuste MRE (%) = (Geração MRE / GF ajustada − 1) × 100.<br><strong>De janeiro/2027 em diante:</strong> GF sem ajuste mensal = (média hidráulica de cinco anos do mês ÷ média das 12 médias mensais do ano) × <strong>53.619 MWm</strong>. O valor 53.619 é a premissa fornecida para 2027, mantida para anos seguintes apenas enquanto não houver nova premissa oficial.</div>
</section>
<section class="box" id="monthly-premises-section"><div class="bar">Premissas mensais editáveis — Ano em curso e ano seguinte</div>
<p class="desc">Os campos abaixo são preenchidos automaticamente pelas referências selecionadas acima e pela planilha MRE. Você pode <strong>substituir qualquer mês projetado</strong> de forma independente. Campos em verde indicam alterações manuais. A geração do mês corrente vem do ONS por padrão, mas também pode ser editada. O histórico anterior ao mês corrente não é alterado.</p>
<div class="matrix-actions"><span class="matrix-status" id="matrix-status">Calculando premissas mensais…</span><button id="reset-all-months" type="button" class="btn-mre primary">Restaurar todas as premissas automáticas</button></div>
<div id="premise-matrix"></div><p class="note">Unidades: geração e garantia física em MWm; Ajuste G, Ajuste GF e GSF em %. As alterações manuais modificam imediatamente o GSF, os cards YTD/fechamento e as projeções dos gráficos. Os valores editados ficam apenas neste navegador (não atualizam a planilha do GitHub).</p></section>
<section class="box"><div class="bar">Valores mensais — Histórico e projeções</div><p class="desc" id="table-explanation">Indicador selecionado, com projeções recalculadas conforme as premissas acima.</p>
<div class="matrix-actions"><span class="matrix-status">Exporta o indicador, os anos e os filtros exibidos, incluindo ajustes mensais manuais.</span><button id="export-mre-xlsx" type="button" class="btn-mre primary">⬇ Exportar Excel (.xlsx)</button></div>
<div class="table-wrap" id="monthly-table"></div><p class="note">Fonte: <strong>dados/mre.xlsx</strong>, guia MRE; geração hidráulica do mês corrente: <strong>ONS — Operação do SIN</strong>. Projeções são estimativas e não representam valores homologados pela CCEE.</p></section>
<div id="warnings"></div>
</main><script>
'use strict';
const MONTHS=['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
const VARS=[
 ['gsf','Ajuste MRE / GSF (%)','%'],
 ['sin','Geração hidráulica SIN (MWm)','MWm'],
 ['gf_sem','Garantia Física sem ajuste (MWm)','MWm'],
 ['mercado','MRE Mercado (%)','%'],
 ['mre','Geração hidráulica MRE (MWm)','MWm'],
 ['gf_aj','Garantia Física com ajuste (MWm)','MWm']
];
const COLORS=['#d85b4c','#2666cf','#078f7d','#9464be','#d77c20','#537887','#789f47','#b85c8c','#139cba','#a27b45'];
const $=id=>document.getElementById(id);
let DATA=null, yearsSelected=new Set(),variable='gsf',showHistory=true,showForecast=true;
// Somente valores explicitamente alterados pelo usuário. Ausência => cálculo automático.
// Chave: AAAA-MM. Fatores são armazenados como razão (1 = 100%), não em %.
let monthlyOverrides={};
const OVERRIDE_KEYS=['sin','aj_g','gf_sem','aj_gf'];
function monthKey(y,m){return `${y}-${String(m).padStart(2,'0')}`}
function projectionMonths(){
 const {year:y,month:m}=current(),out=[];
 for(let yy=y;yy<=y+1;yy++)for(let mm=(yy===y?m:1);mm<=12;mm++)out.push([yy,mm]);
 return out;
}
function overrideVal(y,m,k){
 const value=monthlyOverrides[monthKey(y,m)]?.[k];
 return typeof value==='number'&&Number.isFinite(value)?value:null;
}
function validOverride(k,x){return Number.isFinite(x)&&x>0&&(k!=='aj_g'&&k!=='aj_gf'||x<=2)}
function cleanOverrides(raw){
 const out={};
 const allowed=new Set(projectionMonths().map(([y,m])=>monthKey(y,m)));
 for(const [ym,entries] of Object.entries(raw&&typeof raw==='object'?raw:{})){
  if(!allowed.has(ym)||!entries||typeof entries!=='object')continue;
  const row={};for(const k of OVERRIDE_KEYS){const v=entries[k];if(typeof v==='number'&&validOverride(k,v))row[k]=v}
  if(Object.keys(row).length)out[ym]=row;
 }
 return out;
}
const asPct=x=>x==null?null:x*100;
const metric=(key,year,month)=>DATA.tabelas?.[key]?.[String(year)]?.[String(month)]??null;
const ons=(year,month)=>DATA.ons?.[String(year)]?.[String(month)]??null;
const current=()=>{const [y,m]=DATA.meta.mes_corrente.split('-').map(Number);return {year:y,month:m}};
function txt(v,digits=1){return v==null||!isFinite(v)?'—':Number(v).toLocaleString('pt-BR',{minimumFractionDigits:digits,maximumFractionDigits:digits})}
function kmonth(y,m){return y*12+m}
function monthsWith(key){return Object.entries(DATA.tabelas[key]||{}).flatMap(([y,ms])=>Object.entries(ms).map(([m,v])=>[+y,+m,v])).filter(r=>r[2]>0).sort((a,b)=>kmonth(a[0],a[1])-kmonth(b[0],b[1]))}
function defaultFactor(key){return key==='aj_g'?DATA.meta.g_default:DATA.meta.gf_default}
function factor(key,year,month){
 const id=key==='aj_g'?'g':'gf';
 if($("manual-"+id+"-check").checked){const n=Number($("manual-"+id).value);return n>0&&n<=200?n/100:null}
 const ref=$("ref-"+id).value;
 if(ref==='auto12')return defaultFactor(key);
 if(ref==='month5'){
   const vs=monthsWith(key).filter(x=>x[1]===month && kmonth(x[0],x[1])<kmonth(current().year,current().month)).slice(-5);
   return vs.length?vs.reduce((a,x)=>a+x[2],0)/vs.length:defaultFactor(key);
 }
 const v=metric(key,+ref,month);
 return v!=null && v>0?v:defaultFactor(key);
}
function hydroForecast(year,month){
 const {year:cy,month:cm}=current();
 if(year===cy&&month===cm)return ons(cy,cm);
 const refInput=year===cy?'ref-hidro-current':'ref-hidro-next';
 const ref=Number($(refInput).value);
 return Number.isInteger(ref)&&ref>0?(ons(ref,month)??metric('sin',ref,month)??null):null;
}
function projection(year,month){
 const {year:cy,month:cm}=current();
 if(kmonth(year,month)<kmonth(cy,cm)||year>cy+1)return null;
 // No ano seguinte à data de referência, limitar a dezembro. Os dados históricos
 // permanecem visíveis independentemente do horizonte projetado.
 const sin=overrideVal(year,month,'sin')??hydroForecast(year,month);
 const gfSem=overrideVal(year,month,'gf_sem')??(year>=2027?(DATA.gf_projetada?.[String(year)]?.[String(month)]??null):metric('gf_sem',year,month));
 const ag=overrideVal(year,month,'aj_g')??factor('aj_g',year,month);
 const agf=overrideVal(year,month,'aj_gf')??factor('aj_gf',year,month);
 const mre=sin!=null&&ag!=null?sin*ag:null;
 const gfAj=gfSem!=null&&agf!=null?gfSem*agf:null;
 const gsf=mre!=null&&gfAj>0?mre/gfAj-1:null;
 const mercado=metric('mercado',year,month);
 return {sin,gf_sem:gfSem,mre,gf_aj:gfAj,gsf,mercado,aj_g:ag,aj_gf:agf};
}
function value(key,year,month){
 const {year:cy,month:cm}=current();
 if(kmonth(year,month)<kmonth(cy,cm))return {v:metric(key,year,month),status:'historico'};
 const p=projection(year,month);
 return {v:p?p[key]:null,status:'projecao'};
}
function valDisplay(key,v){return ['gsf','mercado'].includes(key)?asPct(v):v}
function formatValue(key,v){return v==null?'—':txt(valDisplay(key,v),['gsf','mercado'].includes(key)?2:0)+(['gsf','mercado'].includes(key)?'%':'')}
function savePrefs(){
 try{localStorage.setItem('painel-ajuste-mre-v3',JSON.stringify({monthlyOverrides,hydroCurrent:$('ref-hidro-current').value,hydroNext:$('ref-hidro-next').value,g:$('ref-g').value,gf:$('ref-gf').value,
  manualG:$('manual-g-check').checked,manualGF:$('manual-gf-check').checked,vG:$('manual-g').value,vGF:$('manual-gf').value}));}catch(_){}}
function loadPrefs(){
 try{
  const latest=localStorage.getItem('painel-ajuste-mre-v3');
  if(latest)return JSON.parse(latest);
  const previous=localStorage.getItem('painel-ajuste-mre-v2');
  if(previous)return JSON.parse(previous);
  // Compatibilidade com a versão anterior: reaproveitar a referência já selecionada.
  const old=JSON.parse(localStorage.getItem('painel-ajuste-mre-v1')||'{}');
  return {...old,hydroCurrent:old.hydro,hydroNext:old.hydro};
 }catch(_){return {}}
}
function setupPremises(){
 const years=Array.from(new Set([...Object.keys(DATA.ons||{}),...Object.keys(DATA.tabelas.sin||{})])).map(Number).filter(y=>y<current().year).sort((a,b)=>b-a);
 const valid=years.filter(y=>Array.from({length:12},(_,i)=>ons(y,i+1)??metric('sin',y,i+1)).every(x=>x!=null));
 if(!valid.includes(+DATA.meta.ano_base_default)&&valid.length)DATA.meta.ano_base_default=valid[0];
 const {year:cy}=current();
 $('label-hidro-current').textContent=String(cy);
 $('label-hidro-next').textContent=String(cy+1);
 for(const id of ['ref-hidro-current','ref-hidro-next']){
  $(id).innerHTML=valid.length
   ? valid.map(y=>`<option value="${y}">${y} — perfil hidráulico mensal</option>`).join('')
   : '<option value="">Nenhum ano histórico completo</option>';
  if(valid.length)$(id).value=String(DATA.meta.ano_base_default);
 }
 for(const [id,key] of [['g','aj_g'],['gf','aj_gf']]){
  const opts=['<option value="auto12">Média móvel dos 12 meses (padrão)</option>','<option value="month5">Média do mesmo mês — últimos 5 anos</option>'];
  const years=Array.from(new Set(Object.keys(DATA.tabelas[key]||{}))).map(Number).filter(y=>y<=current().year).sort((a,b)=>b-a);
  for(const y of years)opts.push(`<option value="${y}">Fatores mensais de ${y}</option>`);
  $('ref-'+id).innerHTML=opts.join('');
  $('manual-'+id).value=defaultFactor(key)!=null?Number((defaultFactor(key)*100).toFixed(2)):'';
 }
 const prefs=loadPrefs();
 monthlyOverrides=cleanOverrides(prefs.monthlyOverrides);
 for(const [id,pv] of [['ref-hidro-current',prefs.hydroCurrent],['ref-hidro-next',prefs.hydroNext],['ref-g',prefs.g],['ref-gf',prefs.gf]])if(pv&&[...$(id).options].some(o=>o.value===String(pv)))$(id).value=String(pv);
 for(const [id,checked,number] of [['g',prefs.manualG,prefs.vG],['gf',prefs.manualGF,prefs.vGF]]){
  $('manual-'+id+'-check').checked=!!checked;
  $('manual-'+id).disabled=!checked;
  if(checked&&number!=='')$('manual-'+id).value=number;
 }
 for(const id of ['ref-hidro-current','ref-hidro-next','ref-g','ref-gf','manual-g','manual-gf','manual-g-check','manual-gf-check']){
  $(id).addEventListener('change',()=>{
   for(const [fid,key] of [['g','aj_g'],['gf','aj_gf']]){
    $('manual-'+fid).disabled=!$('manual-'+fid+'-check').checked;
    if(!$('manual-'+fid+'-check').checked){
      const {year,month}=current(),v=factor(key,year,month);
      if(v!=null)$('manual-'+fid).value=Number((v*100).toFixed(2));
    }
   }
   savePrefs();renderAll();
  });
  if(id.startsWith('manual-')&&!id.endsWith('-check'))$(id).addEventListener('input',()=>{savePrefs();renderAll()});
 }
}
function possibleYears(){
 const hs=Object.keys(DATA.tabelas.sin||{}).map(Number);
 const {year:cy}=current();
 return Array.from(new Set([...hs.filter(y=>y<=cy),cy,cy+1])).sort((a,b)=>a-b);
}
// Exibe as premissas automáticas com campos editáveis. Os fatores são digitados em %.
function autoMonthlyValue(y,m,k){
 if(k==='sin')return hydroForecast(y,m);
 if(k==='gf_sem')return y>=2027?(DATA.gf_projetada?.[String(y)]?.[String(m)]??null):metric('gf_sem',y,m);
 return factor(k,y,m);
}
function premFormat(v,k){return v==null||!Number.isFinite(v)?'':String(Number((['aj_g','aj_gf'].includes(k)?v*100:v).toFixed(4)))}
function matrixRow(y,m){
 const overrides=monthlyOverrides[monthKey(y,m)]||{};
 const labels={sin:'Geração SIN',aj_g:'Ajuste G',gf_sem:'GF sem ajuste',aj_gf:'Ajuste GF'};
 const parts=OVERRIDE_KEYS.map(k=>{
  const manual=Object.prototype.hasOwnProperty.call(overrides,k);
  const value=manual?overrides[k]:autoMonthlyValue(y,m,k);
  const limit=['aj_g','aj_gf'].includes(k)?'max="200"':'',step=['aj_g','aj_gf'].includes(k)?'0.01':'0.01';
  return `<td><input aria-label="${labels[k]} ${MONTHS[m-1]}/${y}" title="${manual?'Valor editado manualmente':'Valor calculado automaticamente'}" data-year="${y}" data-month="${m}" data-key="${k}" type="number" step="${step}" min="0.01" ${limit} value="${premFormat(value,k)}" class="${manual?'edited':''}" placeholder="—"><span class="origin">${manual?'Manual':'Automático'}</span></td>`;
 }).join('');
 const p=projection(y,m),manualCount=Object.keys(overrides).length;
 return `<tr class="${manualCount?'edited-row':''}"><td>${MONTHS[m-1]}/${y}</td>${parts}`+
 `<td class="calculated">${formatValue('mre',p?.mre)}</td>`+
 `<td class="calculated">${formatValue('gf_aj',p?.gf_aj)}</td>`+
 `<td class="calculated">${formatValue('gsf',p?.gsf)}</td>`+
 `<td class="actions-col"><button class="btn-mre reset-month" type="button" data-year="${y}" data-month="${m}" ${manualCount?'':'disabled'}>Restaurar mês</button></td></tr>`;
}
function renderMatrix(){
 const {year:cy}=current(),groups=[cy,cy+1];
 let html='';
 for(const y of groups){
  const months=projectionMonths().filter(([yr])=>yr===y);
  html+=`<div class="matrix-section">${y} — ${y===cy?'do mês corrente até dezembro':'janeiro a dezembro'}</div>`+
  `<div class="month-edit-wrap"><table><thead><tr><th>Mês</th><th>Geração hidráulica SIN<br>(MWm)</th><th>Ajuste G<br>(%)</th><th>Garantia Física sem ajuste<br>(MWm)</th><th>Ajuste GF<br>(%)</th><th>Geração hidráulica MRE<br>(MWm)</th><th>Garantia Física ajustada<br>(MWm)</th><th>Ajuste MRE / GSF<br>(%)</th><th class="actions-col">Ações</th></tr></thead><tbody>`+
  months.map(([yy,mm])=>matrixRow(yy,mm)).join('')+'</tbody></table></div>';
 }
 $('premise-matrix').innerHTML=html;
 const all=Object.values(monthlyOverrides).reduce((n,r)=>n+Object.keys(r).length,0);
 $('matrix-status').textContent=all?`${all} campo(s) personalizado(s). Demais valores seguem as premissas automáticas.`:'Todos os meses seguem as premissas automáticas.';
 $('premise-matrix').querySelectorAll('input[data-key]').forEach(el=>el.addEventListener('change',()=>{
  const y=Number(el.dataset.year),m=Number(el.dataset.month),k=el.dataset.key;
  const raw=el.value.trim().replace(',','.');
  // Limpar o campo restaura a previsão automática para aquele mês e variável.
  if(raw===''){
   if(monthlyOverrides[monthKey(y,m)]){
    delete monthlyOverrides[monthKey(y,m)][k];
    if(!Object.keys(monthlyOverrides[monthKey(y,m)]).length)delete monthlyOverrides[monthKey(y,m)];
   }
  }else{
   let v=Number(raw);if(['aj_g','aj_gf'].includes(k))v/=100;
   if(!validOverride(k,v)){
    el.setCustomValidity('Informe um número positivo; fatores G/GF entre 0 e 200%.');
    el.reportValidity();
    el.value=premFormat(overrideVal(y,m,k)??autoMonthlyValue(y,m,k),k);
    el.setCustomValidity('');
    return;
   }
   if(!monthlyOverrides[monthKey(y,m)])monthlyOverrides[monthKey(y,m)]={};
   monthlyOverrides[monthKey(y,m)][k]=v;
  }
  savePrefs();renderAll();
 }));
 $('premise-matrix').querySelectorAll('button.reset-month').forEach(el=>el.addEventListener('click',()=>{
  delete monthlyOverrides[monthKey(Number(el.dataset.year),Number(el.dataset.month))];
  savePrefs();renderAll();
 }));
}
function setupMatrix(){
 $('reset-all-months').addEventListener('click',()=>{
  if(!Object.keys(monthlyOverrides).length)return;
  if(window.confirm&&!window.confirm('Restaurar todas as alterações mensais para os cálculos automáticos?'))return;
  monthlyOverrides={};savePrefs();renderAll();
 });
}
function setupPlot(){
 $('variable').innerHTML=VARS.map(([id,name])=>`<option value="${id}">${name}</option>`).join('');
 $('variable').value=variable;
 $('variable').addEventListener('change',()=>{variable=$('variable').value;renderAll()});
 $('toggle-history').addEventListener('change',()=>{showHistory=$('toggle-history').checked;renderAll()});
 $('toggle-forecast').addEventListener('change',()=>{showForecast=$('toggle-forecast').checked;renderAll()});
 const {year:cy}=current();yearsSelected=new Set([cy-1,cy,cy+1]);renderYears();
}
function renderYears(){
 const ys=possibleYears();
 $('years').innerHTML=ys.map((y,i)=>`<button type="button" data-year="${y}" class="chip ${yearsSelected.has(y)?'on':''}" aria-pressed="${yearsSelected.has(y)}"><span class="dot" style="background:${color(y)}"></span>${y}</button>`).join('');
 $('years').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
  const y=+b.dataset.year;
  if(yearsSelected.has(y)){if(yearsSelected.size>1)yearsSelected.delete(y)}else yearsSelected.add(y);
  renderYears();renderAll();
 }));
}
function color(y){const ys=possibleYears();const i=ys.indexOf(y);return COLORS[(i+Math.floor(i/COLORS.length))%COLORS.length]}
// Consolidar o GSF com a mesma ponderação mensal por dias usada no gráfico
// Operação/Ajuste MRE. Meses anteriores: histórico; mês atual e futuros:
// estimativa pelas premissas ativas, sem misturar fatores com percentuais exibidos.
function gsfAverage(year,throughMonth){
 let weighted=0,days=0,realizados=0,projetados=0;
 for(let m=1;m<=throughMonth;m++){
  const o=value('gsf',year,m);
  if(o.v==null||!Number.isFinite(o.v))return null;
  const d=new Date(year,m,0).getDate();
  weighted+=o.v*d;days+=d;
  if(o.status==='historico')realizados++;
  else if(o.status==='projecao')projetados++;
 }
 return days?{v:weighted/days,realizados,projetados,throughMonth}:null;
}
function renderCards(){
 const {year:y,month:m}=current(),p=projection(y,m),mm=String(m).padStart(2,'0');
 const ytd=gsfAverage(y,m),fechamento=gsfAverage(y,12);
 const planos=[
  [`GSF - ${mm}/${y}`,p?.gsf,'%',p?.gsf==null?'Estimativa indisponível':p.gsf>=0?'Energia secundária — mês corrente':'Déficit de geração — mês corrente'],
  [`Geração hidráulica SIN - ${mm}/${y}`,p?.sin,'MWm',p?.sin!=null?'ONS — média mensal disponível':'Aguardando média hidráulica do ONS'],
  [`Garantia Física Ajustada - ${mm}/${y}`,p?.gf_aj,'MWm',p?.gf_aj!=null?'GF sem ajuste × Ajuste GF':'Aguardando Garantia Física sem ajuste'],
  [`GSF - YTD ${y}`,ytd?.v,'%',ytd?`Jan–${MONTHS[m-1]}/${y} · ${ytd.realizados} realizado(s), ${ytd.projetados} projetado(s)`:'Média até o mês corrente indisponível'],
  [`GSF projetado para fechamento do ano - ${y}`,fechamento?.v,'%',fechamento?`Jan–Dez/${y} · ${fechamento.realizados} realizado(s), ${fechamento.projetados} projetado(s)`:'Projeção anual incompleta: confira os meses sem dados']
 ];
 $('cards').innerHTML=planos.map(([label,v,unit,note],i)=>`<div class="card ${[0,3,4].includes(i)&&v!=null&&v<0?'negative':''}"><div class="label">${label}</div><div class="value">${v==null?'—':txt(unit==='%'?v*100:v,unit==='%'?2:0)+(unit==='%'?'%':'')}</div><div class="small">${note}</div></div>`).join('');
 $('updated').textContent=`Referência: ${MONTHS[m-1]}/${y}. GSF YTD e fechamento: médias mensais ponderadas pelo número de dias de cada mês; meses anteriores realizados da planilha e meses a partir do atual estimados. Geração ${y}: perfil ${$('ref-hidro-current').value||'indisponível'} (mês corrente pelo ONS); geração ${y+1}: perfil ${$('ref-hidro-next').value||'indisponível'}. Ajuste G padrão: ${DATA.meta.g_default==null?'—':txt(DATA.meta.g_default*100,2)+'%'} (${DATA.meta.g_janela}). Ajuste GF padrão: ${DATA.meta.gf_default==null?'—':txt(DATA.meta.gf_default*100,2)+'%'} (${DATA.meta.gf_janela}).`;
}
function annualValue(key,year,status){
 const vals=[];
 for(let m=1;m<=12;m++){const o=value(key,year,m);if(o.status===status&&o.v!=null)vals.push([m,valDisplay(key,o.v)]);}
 if(vals.length!==12)return null;
 const weighted=vals.reduce((a,[m,v])=>a+v*new Date(year,m,0).getDate(),0);
 const days=vals.reduce((a,[m])=>a+new Date(year,m,0).getDate(),0);
 return weighted/days;
}
function renderPlot(){
 const obj=VARS.find(x=>x[0]===variable),unit=obj[2],ys=[...yearsSelected].sort((a,b)=>a-b);
 let traces=[];
 for(const y of ys){
  for(const [status,enabled,dash] of [['historico',showHistory,'solid'],['projecao',showForecast,'dash']]){
   if(!enabled)continue;
   const xs=[],vs=[],hover=[];
   for(let m=1;m<=12;m++){
    const o=value(variable,y,m);
    if(o.status!==status||o.v==null)continue;
    xs.push(m);vs.push(valDisplay(variable,o.v));
    hover.push(`${MONTHS[m-1]}/${y}<br>${status==='historico'?'Histórico':'Projeção'}<br>${obj[1]}: ${formatValue(variable,o.v)}`);
   }
   if(!xs.length)continue;
   const label=`${y} — ${status==='historico'?'Histórico':'Projeção'}`;
   traces.push({type:'scatter',mode:'lines+markers',name:label,x:xs,y:vs,customdata:hover,
    line:{color:color(y),width:3,dash},marker:{size:7,color:color(y)},
    hovertemplate:'%{customdata}<extra></extra>',legendgroup:String(y)});
   // "Ano": apenas série com 12 meses completos naquele status.
   const ann=annualValue(variable,y,status);
   if(ann!=null)traces.push({type:'scatter',mode:'markers',x:[13],y:[ann],name:`${y} — Ano (${status})`,showlegend:false,
      marker:{color:color(y),size:12,symbol:'diamond'},hovertemplate:`Média anual ${y}: %{y:.2f} ${unit}<extra></extra>`,legendgroup:String(y)});
  }
  // Para anos com histórico + projeção (ex.: 2026), consolidar os 12 meses.
  if(showHistory&&showForecast){
    const all=Array.from({length:12},(_,i)=>value(variable,y,i+1));
    if(all.every(o=>o.v!=null) && all.some(o=>o.status==='historico') && all.some(o=>o.status==='projecao')){
      let total=0,days=0;
      all.forEach((o,i)=>{const d=new Date(y,i+1,0).getDate();total+=valDisplay(variable,o.v)*d;days+=d});
      traces.push({type:'scatter',mode:'markers',x:[13],y:[total/days],showlegend:false,
        name:`${y} — Ano consolidado`,marker:{color:color(y),size:13,symbol:'diamond',line:{color:'#fff',width:1}},
        hovertemplate:`Média anual consolidada ${y}: %{y:.2f} ${unit}<extra></extra>`,legendgroup:String(y)});
    }
  }
 }
 if(!window.Plotly){$('plot').innerHTML='<p class="warn">Biblioteca de gráficos Plotly indisponível.</p>';return}
 Plotly.react('plot',traces,{height:540,showlegend:true,hovermode:'closest',paper_bgcolor:'#fff',plot_bgcolor:'#fff',
  font:{family:'Century Gothic,Arial',size:12,color:'#334b5a'},margin:{t:20,l:86,r:25,b:95},
  xaxis:{title:'Mês',range:[0.65,13.5],tickmode:'array',tickvals:Array.from({length:13},(_,i)=>i+1),ticktext:[...MONTHS,'Ano'],showgrid:false,showline:true,linecolor:'#9eacb7'},
  yaxis:{title:obj[1],ticksuffix:unit==='%'?'%':'',showline:true,linecolor:'#a7b4bf',gridcolor:'#ebf0f4'},
  legend:{orientation:'h',x:.5,xanchor:'center',y:-.26,yanchor:'top',font:{size:11}}},{responsive:true,displaylogo:false});
 $('plot-note').textContent=variable==='gsf'? 'Ajuste MRE positivo: energia secundária. Ajuste MRE negativo: déficit (GSF abaixo de 100%). Valores da planilha são históricos; projeções são recalculadas com as premissas selecionadas.' :
   `Comparação mensal de ${obj[1]}. Cores = anos; linha contínua = realizado; tracejada = projetado. Ponto “Ano” aparece somente para séries de 12 meses completos.`;
}
function renderTable(){
 const varname=VARS.find(x=>x[0]===variable)[1],ys=[...yearsSelected].sort((a,b)=>a-b);
 let rows=ys.map(y=>{
  let cells=`<td>${y}</td>`;
  for(let m=1;m<=12;m++){
   const o=value(variable,y,m),show=o.status==='historico'?showHistory:showForecast;
   const v=show?o.v:null;
   cells+=`<td class="${v==null?'missing':o.status==='projecao'?'proj':''}" title="${o.status==='projecao'?'Projeção':'Histórico'}">${formatValue(variable,v)}</td>`;
  }
  const all=Array.from({length:12},(_,i)=>value(variable,y,i+1)).map(x=>x.v);
  const yearVal=all.every(x=>x!=null)?all.reduce((total,v,i)=>total+valDisplay(variable,v)*new Date(y,i+1,0).getDate(),0)/all.reduce((total,v,i)=>total+new Date(y,i+1,0).getDate(),0):null;
  cells+=`<td>${yearVal==null?'—':txt(yearVal,variable==='gsf'||variable==='mercado'?2:0)}</td>`;
  return `<tr>${cells}</tr>`;
 }).join('');
 $('monthly-table').innerHTML=`<table><thead><tr><th>Ano</th>${MONTHS.map(m=>`<th>${m}</th>`).join('')}<th>Média ano</th></tr></thead><tbody>${rows}</tbody></table>`;
 $('table-explanation').textContent=`${varname}. Células verdes representam projeções; as médias anuais ponderam cada mês pelo número de dias e só aparecem quando há 12 meses disponíveis.`;
}
// Exportação OOXML XLSX sem dependências externas; utiliza as células exibidas
// na tabela, portanto inclui premissas mensais alteradas e filtros ativos.
function xmlE(x){return String(x??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&apos;')}
function xlCol(i){let out='';for(let n=i;n>0;n=Math.floor((n-1)/26))out=String.fromCharCode(65+(n-1)%26)+out;return out}
function xmlStr(r,c,v,style=0){const ref=xlCol(c)+r;return `<c r="${ref}"${style?` s="${style}"`:''} t="inlineStr"><is><t>${xmlE(v)}</t></is></c>`}
function xmlNum(r,c,v,style=0){const ref=xlCol(c)+r;return Number.isFinite(v)?`<c r="${ref}"${style?` s="${style}"`:''}><v>${v}</v></c>`:''}
function brazilNumber(value){const text=String(value??'').trim();if(!text||text==='—')return null;const num=Number(text.replace(/\s/g,'').replace(/%$/,'').replace(/\./g,'').replace(',','.'));return Number.isFinite(num)?num:null}
function uint16(out,v){out.push(v&255,(v>>>8)&255)}
function uint32(out,v){out.push(v&255,(v>>>8)&255,(v>>>16)&255,(v>>>24)&255)}
const CRC_XLSX=(()=>{const t=[];for(let i=0;i<256;i++){let c=i;for(let j=0;j<8;j++)c=c&1?0xedb88320^(c>>>1):c>>>1;t.push(c>>>0)}return t})();
function crcXlsx(bytes){let c=0xffffffff;for(const b of bytes)c=CRC_XLSX[(c^b)&255]^(c>>>8);return(c^0xffffffff)>>>0}
function zipXlsx(files){
 const encoder=new TextEncoder(),local=[],central=[];let offset=0;
 for(const [name,content] of files){
  const nb=encoder.encode(name),data=encoder.encode(content),crc=crcXlsx(data);
  const lh=[];uint32(lh,0x04034b50);uint16(lh,20);uint16(lh,0x0800);uint16(lh,0);uint16(lh,0);uint16(lh,0);uint32(lh,crc);uint32(lh,data.length);uint32(lh,data.length);uint16(lh,nb.length);uint16(lh,0);
  local.push(new Uint8Array(lh),nb,data);
  const ch=[];uint32(ch,0x02014b50);uint16(ch,20);uint16(ch,20);uint16(ch,0x0800);uint16(ch,0);uint16(ch,0);uint16(ch,0);uint32(ch,crc);uint32(ch,data.length);uint32(ch,data.length);uint16(ch,nb.length);uint16(ch,0);uint16(ch,0);uint16(ch,0);uint16(ch,0);uint32(ch,0);uint32(ch,offset);
  central.push(new Uint8Array(ch),nb);offset+=lh.length+nb.length+data.length;
 }
 const centralBytes=central.reduce((a,b)=>a+b.length,0),end=[];uint32(end,0x06054b50);uint16(end,0);uint16(end,0);uint16(end,files.length);uint16(end,files.length);uint32(end,centralBytes);uint32(end,offset);uint16(end,0);
 return new Blob([...local,...central,new Uint8Array(end)],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
}
function exportMensalXlsx(){
 if(!DATA){alert('Dados ainda não carregados.');return}
 const table=$('monthly-table').querySelector('table');if(!table){alert('Tabela mensal indisponível.');return}
 const varObj=VARS.find(x=>x[0]===variable),metricTitle=varObj[1];
 const selectedYears=[...yearsSelected].sort((a,b)=>a-b),r=[];
 r.push(`<row r="1">${xmlStr(1,1,'Ajuste MRE — Valores mensais',1)}</row>`);
 r.push(`<row r="2">${xmlStr(2,1,'Indicador',5)}${xmlStr(2,2,metricTitle,5)}</row>`);
 r.push(`<row r="3">${xmlStr(3,1,'Seleção',5)}${xmlStr(3,2,`Histórico: ${showHistory?'sim':'não'} | Projeções: ${showForecast?'sim':'não'} | Anos: ${selectedYears.join(', ')}`,5)}</row>`);
 r.push(`<row r="4">${xmlStr(4,1,'Fonte: dados/mre.xlsx e ONS — Operação do SIN. Valores verdes: projeção. Premissas manuais incluídas.',5)}</row>`);
 const heads=[...table.querySelectorAll('thead th')].map(e=>e.textContent.trim());
 r.push(`<row r="6">${heads.map((t,i)=>xmlStr(6,i+1,t,2)).join('')}</row>`);
 const bodyRows=[...table.querySelectorAll('tbody tr')];
 for(let i=0;i<bodyRows.length;i++){
  const rownum=i+7,cells=[...bodyRows[i].querySelectorAll('td')];
  let line=xmlNum(rownum,1,Number(cells[0].textContent.trim()),6);
  for(let j=1;j<cells.length;j++){
   const v=brazilNumber(cells[j].textContent),style=j===cells.length-1?7:cells[j].classList.contains('proj')?4:3;
   if(v!=null)line+=xmlNum(rownum,j+1,v,style);
  }
  r.push(`<row r="${rownum}">${line}</row>`);
 }
 const sheet=`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:N${Math.max(6,bodyRows.length+6)}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="6" topLeftCell="A7" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="1" width="15" customWidth="1"/><col min="2" max="13" width="14" customWidth="1"/><col min="14" max="14" width="18" customWidth="1"/></cols><sheetData>${r.join('')}</sheetData><mergeCells count="2"><mergeCell ref="A1:N1"/><mergeCell ref="A4:N4"/></mergeCells></worksheet>`;
 const styles=`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="3"><font><sz val="11"/><name val="Aptos"/></font><font><b/><sz val="15"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font><font><b/><sz val="11"/><color rgb="FF176768"/><name val="Aptos"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF007E80"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE8F6F0"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="8"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="4" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyNumberFormat="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="4" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`;
 const files=[
  ['[Content_Types].xml',`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>`],
  ['_rels/.rels',`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`],
  ['xl/workbook.xml',`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Valores mensais" sheetId="1" r:id="rId1"/></sheets></workbook>`],
  ['xl/_rels/workbook.xml.rels',`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>`],
  ['xl/worksheets/sheet1.xml',sheet],['xl/styles.xml',styles]
 ];
 const blob=zipXlsx(files),url=URL.createObjectURL(blob),a=document.createElement('a');
 const name=`Ajuste_MRE_Valores_Mensais_${variable}_${DATA.meta.mes_corrente.replace('-','')}.xlsx`;
 a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);
}

function renderWarnings(){const warnings=[...(DATA.meta.avisos||[])];
 const {year:y,month:m}=current();if(!DATA.gf_projetada[String(y+1)]&&y+1>=2027)warnings.push('GF sem ajuste do ano seguinte depende de 12 valores da média hidráulica de 5 anos na planilha.');
 $('warnings').innerHTML=warnings.map(t=>`<div class="warn">${String(t).replaceAll('<','&lt;')}</div>`).join('');
}
function renderAll(){renderMatrix();renderCards();renderPlot();renderTable();renderWarnings();sendHeight()}
function sendHeight(){if(window.parent!==window)window.parent.postMessage({tipo:'altura-painel-ons',id:'mre',altura:document.body.scrollHeight+28},'*')}
async function init(){
 try{
  const r=await fetch('ajuste_mre.json',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);
  DATA=await r.json();setupPremises();setupMatrix();setupPlot();$('export-mre-xlsx').addEventListener('click',exportMensalXlsx);renderAll();
 }catch(e){$('warnings').innerHTML='<div class="warn">Não foi possível carregar os dados do Ajuste MRE: '+String(e.message).replaceAll('<','&lt;')+'</div>';}
}
window.addEventListener('load',init);if(window.ResizeObserver)new ResizeObserver(sendHeight).observe(document.body);
</script></body></html>'''

if __name__=='__main__':main()
