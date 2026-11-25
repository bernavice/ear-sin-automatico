#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IPDO/ONS — extrator conservador de boletins preliminares + cruzamento da operação do SIN.

Execução: python atualizar_ipdo.py
Teste offline: python atualizar_ipdo.py --sem-rede --demo
Arquivo PDF manual: salvar um IPDO-AAAA-MM-DD.pdf em dados/ipdo/ (sem substituir o link oficial).
Dependências: requests, PyMuPDF (fitz), bibliotecas padrão.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import html
import json
import logging
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import requests
from ipdo_enrichment import (parse_capacidade_55, parse_termicas_6, parse_demanda_7,
                            load_curtailment, load_hydro_anomalies, get_ons_demanda, get_ons_hydro, load_thermal_dispatch)
try:
    import fitz
except ImportError as exc:
    raise SystemExit('Biblioteca PyMuPDF ausente: pip install PyMuPDF>=1.24,<2') from exc

LOG = logging.getLogger('ipdo')
ROOT = Path(__file__).resolve().parent
ONS = 'https://www.ons.org.br'
PDF_BASE = ONS + '/AcervoDigitalDocumentosEPublicacoes/'
RAW = ROOT / 'dados' / 'ipdo'
OUT = ROOT / 'graficos'
SNAPSHOTS = RAW / 'historico.json'
PREVIEW = ROOT / 'graficos' / 'ipdo.html'
DATA_JSON = OUT / 'ipdo.json'
BR_NAMES = ['Hidro Nacional', 'Itaipu Binacional', 'Termo Nuclear', 'Termo Convencional', 'Eólica', 'Solar', 'Total SIN', 'Intercâmbio Internacional', 'Carga']
SUBS = {'SE': 'SE/CO', 'SE/CO': 'SE/CO', 'S': 'SUL', 'SUL': 'SUL', 'NE': 'NE', 'N': 'N'}
KINDS = ['carga', 'hidraulica', 'termica', 'eolica', 'solar']
PREVIEW_DATA = {
    'data': '2026-11-21', 'origem': 'exemplo-publico',
    'url': PDF_BASE + 'IPDO-21-11-2026.pdf',
    'balanco': [
      {'fonte':'Hidro Nacional','programado':41261,'verificado':41497},
      {'fonte':'Itaipu Binacional','programado':6736,'verificado':6892},
      {'fonte':'Termo Nuclear','programado':1350,'verificado':1349},
      {'fonte':'Termo Convencional','programado':11081,'verificado':10927},
      {'fonte':'Eólica','programado':9913,'verificado':9774},
      {'fonte':'Solar','programado':15469,'verificado':15494},
      {'fonte':'Total SIN','programado':85810,'verificado':85933},
      {'fonte':'Intercâmbio Internacional','programado':0,'verificado':0},
      {'fonte':'Carga','programado':85810,'verificado':85933},
    ],
    'reservatorios': [
      {'subsistema':'SIN','ear':57.6,'variacao_diaria_pp':0.1},
      {'subsistema':'SUL','ear':81.7,'variacao_diaria_pp':0.2},
      {'subsistema':'SE/CO','ear':55.5,'variacao_diaria_pp':0.2},
      {'subsistema':'N','ear':50.7,'variacao_diaria_pp':-0.1},
      {'subsistema':'NE','ear':58.7,'variacao_diaria_pp':0.0},
    ],
    'destaques': [
      'SE/CO: geração hidráulica acima do programado devido à carga superior ao previsto.',
      'Nordeste: geração hidráulica acima do programado para controle da cascata do rio São Francisco.',
      'Nordeste: intercâmbio acima do programado, com maior geração eólica/hidráulica e carga inferior à prevista.',
    ],
    'restricoes': [{'subsistema':'NE', 'maximo_mw':6903, 'motivo':'Energético e elétrico'}],
    'ocorrencias': [],
}

def simple(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', s.lower()) if not unicodedata.combining(c))

def number_pt(text: str):
    t = str(text).strip().replace('\u00a0', '').replace(' ', '').replace('%', '')
    if t in ('', '—', '–', '-', '---'):
        return None
    t = re.sub(r'[^0-9,.-]', '', t)
    if not t or t in ('-', '.', ','):
        return None
    try:
        return float(t.replace('.', '').replace(',', '.'))
    except ValueError:
        return None

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return default

def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

def is_pdf(blob):
    return blob.startswith(b'%PDF-') and len(blob) > 3000

def fetch_latest(reference: dt.date, lookback: int = 9, session=None):
    """Tenta PDFs oficiais recentes; jamais usa índice de buscador como comprovação de disponibilidade."""
    client = session or requests.Session()
    client.headers.update({'User-Agent': 'Painel-Energetico-IPDO/1.0 (+consulta-publica-ONS)', 'Accept':'application/pdf', 'Accept-Language':'pt-BR,pt;q=0.9'})
    for offset in range(lookback):
        day = reference - dt.timedelta(days=offset)
        url = PDF_BASE + 'IPDO-' + day.strftime('%d-%m-%Y') + '.pdf'
        try:
            r = client.get(url, timeout=(6, 18), allow_redirects=True)
            if r.status_code == 200 and is_pdf(r.content):
                LOG.info('IPDO obtido: %s',url)
                return day, r.content, url
            LOG.info('IPDO não disponível (%s): %s', r.status_code, url)
        except requests.RequestException as e:
            LOG.warning('Falha temporária no IPDO %s: %s',url,e)
    return None

def get_lines(doc, index):
    if len(doc) <= index: return []
    return [x.strip() for x in doc[index].get_text('text', sort=True).splitlines() if x.strip()]

def row_numbers(lines, label):
    """Encontra linha ou vizinhança de rótulo da primeira página; valida posteriormente contra total."""
    labels = [simple(label)]
    if label == 'Solar': labels.append('solar (***)')
    if label == 'Total SIN': labels.extend(['total sin', 'total'])
    for i, line in enumerate(lines):
        normal = simple(line)
        if not any(normal.startswith(lab) or (lab in normal and len(normal) < 75) for lab in labels):
            continue
        # Algumas versões do PDF separam rótulos e números em linhas distintas.
        probe = line
        # Rótulo e valores costumam ocupar a mesma linha; não usar valores da próxima fonte.
        found = next((lab for lab in labels if normal.startswith(lab)), None)
        if found:
            probe = probe[len(found):]
        else:
            continue
        tokens = re.findall(r'(?<!\w)-?\d{1,3}(?:\.\d{3})+(?:,\d+)?|-?\d{2,6}(?:,\d+)?', probe)
        nums = [number_pt(t) for t in tokens]
        nums = [n for n in nums if n is not None]
        if len(nums) >= 2:
            return int(nums[0]),int(nums[1])
    return None

def geometry_rows(page):
    """Fallback baseado na posição dos textos, para PDF que organiza colunas como blocos."""
    words = page.get_text('words', sort=True)
    clusters=[]
    for w in words:
        if len(w)<5: continue
        x0,y0,x1,y1,val = w[:5]
        mid = (y0+y1)/2
        match = next((r for r in clusters if abs(r['y']-mid)<4),None)
        if not match:
            match={'y':mid,'terms':[]};clusters.append(match)
        match['terms'].append((x0,str(val)))
    rows={}
    for r in clusters:
        words=sorted(r['terms']);line=' '.join(t for _,t in words);norm=simple(line)
        for name in BR_NAMES:
            target=simple(name)
            if (target in norm) and name not in rows:
                tail=norm.split(target,1)[1]
                vals=re.findall(r'-?\d{1,3}(?:\.\d{3})+|-?\d{2,6}',tail)
                nums=[number_pt(x) for x in vals]
                if len(nums)>=2 and all(x is not None for x in nums[:2]):
                    rows[name]=(int(nums[0]),int(nums[1]))
    return rows

def parse_balance(doc):
    lines=get_lines(doc,0)
    geo=geometry_rows(doc[0])
    rows=[]
    for name in BR_NAMES:
        v=geo.get(name) or row_numbers(lines,name)
        if v:
            rows.append({'fonte':name,'programado':v[0],'verificado':v[1]})
    names={r['fonte'] for r in rows}
    if not {'Total SIN','Carga','Hidro Nacional','Eólica','Solar'}.issubset(names):
        raise ValueError('Tabela 1 do IPDO não reconhecida com segurança: '+', '.join(sorted(names)))
    by={r['fonte']:r for r in rows}
    total=by['Total SIN']['verificado']
    if total<20000 or total>300000:
        raise ValueError('Total SIN fora de faixa de plausibilidade: '+str(total))
    if len([k for k in BR_NAMES[:6] if k in by])==6:
        source_sum=sum(by[k]['verificado'] for k in BR_NAMES[:6])
        if abs(total-source_sum)>max(200,0.035*total):
            raise ValueError(f'Balanço inconsistente: componentes={source_sum}, total={total}')
    return rows

def extract_section(text, start, end, maxchars=6000):
    lower=simple(text)
    s=lower.find(simple(start))
    if s<0:return ''
    e=lower.find(simple(end), s+len(start)) if end else -1
    return text[s:e if e>s else s+maxchars][:maxchars]

def parse_storages(doc):
    """Extrai EAR e variação pelo texto quando os rótulos aparecem em uma mesma linha."""
    found=[]
    all_lines=[]
    for i in (1,2):all_lines.extend(get_lines(doc,i))
    for j,line in enumerate(all_lines):
        if 'Armazenamento ao final do dia (%)' in line:
            numeric = re.findall(r'-?\d{1,3}(?:[.,]\d+)?\s*%', ' '.join(all_lines[j:j+3]))
            if len(numeric)>=5:
                keys=['SIN','SUL','SE/CO','N','NE']
                for key,val in zip(keys,numeric[:5]):found.append({'subsistema':key,'ear':number_pt(val)})
                break
    return found

def _narrative_clean(text):
    """Não apresenta pedaços de frases/colunas embaralhadas como destaques.
    Conserva somente parágrafos com pontuação e proporção lexical plausível.
    """
    value=re.sub(r'\s+',' ',text).strip('•*- \t')
    if not (75<=len(value)<=700):return None
    if not re.search(r'[.!?]\s*$',value):return None
    terms=re.findall(r'\b[A-Za-zÀ-ÿ]+\b',value)
    if len(terms)<12:return None
    if sum(len(t)<=2 for t in terms)/len(terms)>.42:return None
    if sum(ch.isalpha() for ch in value)<len(value)*.56:return None
    # Evita reproduzir cabeçalhos de tabelas e descrições sem contexto.
    if len(value.split())>6 and sum(ch.isupper() for ch in value if ch.isalpha())/max(1,sum(ch.isalpha() for ch in value))>.78:
        return None
    return value

def parse_highlights(doc):
    # Extrair somente narrativa legível; jamais publicar títulos quebrados
    # pelas quebras de posição do PDF como se fossem frases operativas.
    content='\n'.join(get_lines(doc,2)+get_lines(doc,3))
    sect=extract_section(content,'4 - Destaques da Operação','5 - Gerações Térmicas',10000)
    if not sect:
        sect=extract_section(content,'CARGA E PRODUÇÃO DE ENERGIA POR SUBMERCADO','5 - Gerações',8000)
    lines=[x.strip() for x in sect.splitlines() if x.strip()]
    paragraphs=[];buf=''
    for line in lines:
        line=re.sub(r'\s+',' ',line).strip('•*- ')
        if not line:continue
        if re.match(r'^\s*(?:\d+(?:\.\d+)*[.:-]?|[-–•])\s+',line) or (line.isupper() and len(line)>12):
            if buf:paragraphs.append(buf)
            buf=''
            if line.isupper():continue
        if line.isupper() and len(line)>12:continue
        buf=(buf+' '+line).strip()
        if re.search(r'[.!?]\s*$',buf):paragraphs.append(buf);buf=''
        if len(buf)>750:buf=''
    if buf:paragraphs.append(buf)
    unique=[]
    for para in paragraphs:
        out=_narrative_clean(para)
        if out and out not in unique:unique.append(out)
    return unique[:7]

def parse_restrictions(doc):
    body='\n'.join(get_lines(doc,3))
    section=extract_section(body, 'RESTRIÇÃO DE GERAÇÃO RENOVÁVEL', 'INTERCÂMBIO INTERNACIONAL', 4000)
    pieces=re.split(r'(?i)Submercado\s+(Sul|Sudeste/Centro-Oeste|Nordeste|Norte)\s*:', section)
    out=[]
    aliases={'Sul':'SUL','Sudeste/Centro-Oeste':'SE/CO','Nordeste':'NE','Norte':'N'}
    for i in range(1,len(pieces)-1,2):
        name,block=pieces[i],pieces[i+1]
        m=re.search(r'(?i)valor m[áa]ximo\s*:\s*([\d.]+)\s*MW',block)
        motive=re.search(r'(?i)motivos?\s*:\s*([^\n.]+)',block)
        out.append({'subsistema':aliases[name], 'maximo_mw':int(m.group(1).replace('.','')) if m else None,
                    'motivo':motive.group(1).strip() if motive else None})
    return out

def parse_date(doc, fallback):
    t=' '.join(get_lines(doc,0)[:25]);m=re.search(r'\b(\d{1,2})\s+(?:de\s+)?(janeiro|fevereiro|mar[çc]o|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\s+(?:de\s+)?(20\d{2})',simple(t),re.I)
    if m:
        months='janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro'.split()
        return dt.date(int(m.group(3)),months.index(m.group(2))+1,int(m.group(1)))
    return fallback

def parse_pdf(blob, hint_date, url, origin='pdf-ons'):
    doc=fitz.open(stream=blob,filetype='pdf')
    if len(doc)<2:raise ValueError('IPDO incompleto: PDF com menos de duas páginas')
    doc_date=parse_date(doc,hint_date)
    if doc_date != hint_date:
        raise ValueError(f'Data interna do PDF {doc_date} difere da URL {hint_date}')
    bal=parse_balance(doc)
    return {'data':hint_date.isoformat(), 'origem':origin, 'url':url, 'balanco':bal,
            'reservatorios':parse_storages(doc), 'destaques':parse_highlights(doc),
            'restricoes':parse_restrictions(doc), 'ocorrencias':[],
            'capacidade_55':parse_capacidade_55(doc),
            'termicas_6':parse_termicas_6(doc),
            'demanda_7':parse_demanda_7(doc,hint_date)}

def load_pdf_manual(data_dir: Path):
    files=[]
    for f in data_dir.glob('*.pdf'):
        m=re.search(r'(?i)IPDO[-_](\d{2})[-_](\d{2})[-_](20\d{2})',f.name)
        if m:files.append((dt.date(int(m.group(3)),int(m.group(2)),int(m.group(1))),f))
    for day,f in sorted(files, reverse=True):
        try:
            blob=f.read_bytes()
            if is_pdf(blob):return day,blob,str(f.relative_to(ROOT))
        except Exception as e:LOG.warning('PDF local inválido %s: %s',f,e)
    return None

def load_operation():
    """Séries consolidadas do módulo Operação do SIN; preserva suas próprias datas."""
    op=read_json(ROOT/'graficos'/'operacao_sin'/'resumo.json',{})
    daily=[]
    for row in op.get('daily',[]):
        if len(row)<7:continue
        day=str(row[0])
        if not re.fullmatch(r'20\d\d-\d\d-\d\d',day):continue
        vals={k: round(float(row[i]),2) for i,k in enumerate(['termica','eolica','solar','hidraulica','intercambio','carga'],start=1) if isinstance(row[i],(int,float))}
        daily.append({'data':day,'subsistema':'SIN',**vals})
    daily=sorted(daily,key=lambda r:r['data'])[-740:]
    # Subsistemas: médias horárias agrupadas por dia, calculadas dos mesmos arquivos da aba Operação.
    latest_day=daily[-1]['data'] if daily else None
    subdaily=[]
    hourly_sin=[]
    hourly_data=defaultdict(dict)
    data_dir=ROOT/'dados'/'operacao_sin'
    years=sorted({dt.date.today().year,dt.date.today().year-1,dt.date.fromisoformat(latest_day).year if latest_day else dt.date.today().year})
    subfiles=[data_dir/f'operacao_subsistemas_{y}.csv.gz' for y in years]
    cutoff=(dt.date.fromisoformat(latest_day)-dt.timedelta(days=58)).isoformat() if latest_day else None
    aggregates=defaultdict(lambda:defaultdict(list))
    for subpath in subfiles:
        if not subpath.exists():continue
        with gzip.open(subpath,'rt',encoding='utf-8-sig') as h:
            for item in csv.DictReader(h):
                timestamp=item.get('datahora','')
                d=timestamp[:10];sub=SUBS.get(item.get('subsistema','').upper())
                if not sub or not d or (daily and d<daily[0]['data']):continue
                if latest_day and cutoff and cutoff<=d<=latest_day and len(timestamp)>=16:
                    try:
                        hourly_data[timestamp][sub] = {k:float(item[k]) for k in ('carga','eolica','solar')}
                    except (KeyError,TypeError,ValueError):pass
                for k in KINDS:
                    try: aggregates[(d,sub)][k].append(float(item[k]))
                    except (KeyError,TypeError,ValueError):pass
    for (day,sub),attrs in sorted(aggregates.items()):
        row={'data':day,'subsistema':sub}
        for k,seq in attrs.items():row[k]=round(sum(seq)/len(seq),2)
        subdaily.append(row)
    for timestamp, subs in sorted(hourly_data.items()):
        if set(subs)=={'SE/CO','SUL','NE','N'}:
            carga=sum(x['carga'] for x in subs.values())
            eolica=sum(x['eolica'] for x in subs.values())
            solar=sum(x['solar'] for x in subs.values())
            hourly_sin.append({'datahora':timestamp,'carga_mw':round(carga,2),
                               'eolica_mw':round(eolica,2),'solar_mw':round(solar,2),
                               'carga_liquida_proxy_mw':round(carga-eolica-solar,2)})
    all_daily=daily+subdaily
    for row in all_daily:
        if all(isinstance(row.get(k),(int,float)) for k in ('carga','eolica','solar')):
            row['carga_liquida_proxy']=round(row['carga']-row['eolica']-row['solar'],2)
    for row in all_daily:
        if row.get('carga') and 'carga_liquida_proxy' in row:
            row['pct_carga_liquida']=round(100*row['carga_liquida_proxy']/row['carga'],3)
    for row in hourly_sin:
        if row['carga_mw']>0:
            row['pct_carga_liquida']=round(100*row['carga_liquida_proxy_mw']/row['carga_mw'],3)
    latest=max((r['data'] for r in daily),default=None)
    changes=[]
    bygroup=defaultdict(list)
    for r in all_daily: bygroup[r['subsistema']].append(r)
    for sub,rows in bygroup.items():
        rows=sorted(rows,key=lambda r:r['data'])
        if len(rows)<2:continue
        current,prior=rows[-1],rows[-2]
        if (dt.date.fromisoformat(current['data'])-dt.date.fromisoformat(prior['data'])).days!=1:continue
        for k in KINDS:
            a,b=current.get(k),prior.get(k)
            if a is None or b is None or abs(b)<1:continue
            delta=a-b;pct=100*delta/b
            # Limiar de relevância *e* materialidade absoluta.
            threshold=3 if k=='carga' else 7
            minimum=300 if sub!='SIN' else 900
            if abs(pct)>=threshold and abs(delta)>=minimum:
                changes.append({'subsistema':sub,'variavel':k,'atual':a,'anterior':b,
                                'variacao_mwm':round(delta,1),'variacao_pct':round(pct,1),'data':current['data']})
    changes.sort(key=lambda x:abs(x['variacao_mwm']),reverse=True)
    return {'ultima_data':latest,'series':all_daily,'perfil_horario_sin':hourly_sin[-24:], 'perfis_horarios_sin':hourly_sin,
            'variacoes_relevantes':changes[:20],
            'metodo':'Média dos valores horários disponíveis por dia; comparação com dia anterior para cada submercado.'}

# --- Interface autônoma (funciona mesmo aberta em file://, com Plotly online) ---
HTML_TEMPLATE=r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>IPDO | Operação de Curtíssimo Prazo</title>
<style>
:root{font-family:Century Gothic,Arial,sans-serif;background:#f4f7fb;color:#162b42}*{box-sizing:border-box}
body{margin:0}.wrap{max-width:1390px;margin:auto;padding:20px}
.hero{padding:24px 26px;background:linear-gradient(110deg,#12334f,#07587c);border-radius:15px;color:#fff;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;align-items:center}
.hero h1{font-size:clamp(24px,3.2vw,37px);margin:0 0 7px}.hero p{margin:0;color:#c7e3ef;font-size:13px}.stamp{font-size:13px;background:#ffffff19;padding:12px 16px;border:1px solid #ffffff35;border-radius:12px}
.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:17px 0}
.kpi,.panel{background:#fff;border:1px solid #dfe8f1;border-radius:13px;padding:18px;box-shadow:0 3px 11px #082b460b}
.kpi .caption{color:#657890;font-size:12px;font-weight:700}.kpi strong{font-size:26px;display:block;margin:10px 0 4px}.kpi small{color:#60768b;font-size:11px}
.notice{padding:13px 17px;border-left:4px solid #df9b35;background:#fff9ed;border-radius:8px;font-size:12px;margin:12px 0 18px;line-height:1.5}
.notice.good{border-color:#0b8a85;background:#edf9f7}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.panel h2{margin:0 0 10px;font-size:16px;color:#143956}
.plot{width:100%;height:365px}.tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:9px}
select{border:1px solid #cddae6;border-radius:8px;padding:9px;background:white;font:inherit;color:#233d53}
.thead{background:#173f60;color:white}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:11px 8px;text-align:right;border-bottom:1px solid #e6edf3}th:first-child,td:first-child{text-align:left}tbody tr:hover{background:#f4f9fc}
.up{color:#10766a;font-weight:700}.down{color:#ad4a39;font-weight:700}.list{margin:0;padding-left:19px;line-height:1.7;color:#32485a;font-size:12px}li{margin-bottom:6px}
.empty{padding:20px;color:#60768b;font-size:13px;background:#f5f8fa;border-radius:8px}.meta{font-size:11px;color:#5d758b;margin-top:13px}
.alert-red{background:#fff1ef;border-color:#dc5b50;color:#8b2822}.alert-green{background:#e8f8f3;border-color:#0d9972;color:#126c4d}.highlights-flex{display:grid;grid-template-columns:1fr 1fr;gap:12px}.highlights-flex .part{background:#f5f9fd;padding:13px;border-radius:9px}.panel{min-width:0}.wide{grid-column:1/-1}.control-col{display:flex;flex-direction:column;gap:5px;min-width:130px}.control-col select{min-height:105px;padding:5px;font-size:12px}.mini-table{overflow:auto;max-width:100%}.mini-table table{white-space:nowrap;font-size:11px}.mini-table td,.mini-table th{padding:8px 9px}.mini-table td:not(:first-child){text-align:right}.hint{font-size:11px;color:#6c8194}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.tabs button{border:1px solid #cadae6;background:#fff;color:#204963;padding:8px 14px;border-radius:9px;cursor:pointer}.tabs button.active{background:#145b80;color:white;border-color:#145b80}.info-strip{display:flex;gap:13px;flex-wrap:wrap;margin:10px 0;color:#47687c;font-size:12px}.info-strip b{color:#173e5b}.footer{margin:22px 0;font-size:11px;color:#6d7b89}
a{color:#087da6}a:visited{color:#3e6985}
@media(max-width:980px){.highlights-flex{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.summary{grid-template-columns:repeat(2,1fr)}}
@media(max-width:500px){.wrap{padding:10px}.summary{grid-template-columns:1fr 1fr}.kpi{padding:13px}.kpi strong{font-size:21px}.hero{padding:17px}.plot{height:310px}}
@media print {.wrap{max-width:100%}.plot{break-inside:avoid}.panel{break-inside:avoid}}
</style>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script></head><body>
<main class="wrap"><header class="hero"><div><h1>IPDO · Operação de Curtíssimo Prazo</h1>
<p>Balanço de energia, despacho, restrições e variações da geração e carga no SIN</p></div><div class="stamp" id="reference">Carregando dados...</div></header>
<div id="notice" class="notice"></div>
<section class="panel" id="critical-card"><h2>⚑ Destaques e restrições operativas — prioridade do dia</h2><div id="highlights"></div></section>
<div id="demand-alert" class="notice" style="display:none"></div>
<section class="summary" id="kpis"></section>
<div class="grid"><section class="panel"><h2>Programado × verificado — IPDO</h2><div id="balance" class="plot"></div></section>
<section class="panel"><h2>Participação da geração — IPDO</h2><div id="mix" class="plot"></div></section></div>
<section class="panel" style="margin-top:14px"><h2>Operação do SIN · Evolução recente</h2>
<div class="tools"><div class="control-col"><label for="sub">Submercados</label><select id="sub" multiple size="5" aria-label="Selecione um ou mais submercados"></select></div>
<div class="control-col"><label for="metric">Grandezas — múltipla seleção</label><select id="metric" multiple size="5" aria-label="Selecione uma ou mais grandezas"></select></div>
<div class="hint">Use Ctrl+clique para escolher várias séries no computador.<br>As curvas indicam submercado e grandeza.</div></div>
<div id="trend" class="plot" style="height:405px"></div><div id="trend-table" class="mini-table"></div><p class="meta" id="opnote"></p></section>
<section class="panel" style="margin-top:14px"><h2>Curtailment — restrição de geração eólica e solar</h2>
<div class="tabs" id="curt-tabs"><button data-level="dia" class="active">Diário</button><button data-level="mes">Mensal</button><button data-level="ano">Anual</button></div>
<div id="curtailment-kpis" class="info-strip"></div><div id="curtailment" class="plot" style="height:390px"></div>
<div id="curtailment-table" class="mini-table"></div><p class="meta" id="curtailment-note"></p></section>
<section class="panel" style="margin-top:14px"><h2>Carga líquida — perfil diário e histórico</h2>
<div class="tools"><label for="netload-date">Selecionar dia:</label><select id="netload-date"></select></div>
<div id="netload-summary" class="info-strip"></div><div id="netload-day" class="plot" style="height:345px"></div>
<div id="netload" class="plot" style="height:315px"></div>
<div id="netload-ratio" class="plot" style="height:285px"></div>
<p class="meta">Indicador aproximado: carga líquida = carga global − geração eólica − geração solar. Razão (%) = carga líquida / carga global. Perfil horário em MW; médias históricas em MWmed. O marcador no fim do perfil indica a média do dia, não o valor da última hora.</p></section>
<div class="grid"><section class="panel"><h2>Demanda máxima instantânea do SIN — seção 7</h2>
<div id="demand-max" class="plot"></div><p class="meta" id="demand-note"></p></section>
<section class="panel"><h2>Geração térmica — motivo do despacho (ONS)</h2>
<div id="thermal-reasons" class="plot"></div><p class="meta" id="thermal-reasons-note"></p></section></div>
<div class="grid"><section class="panel"><h2>Top 3 — diferenças entre capacidade instalada e disponível</h2>
<div id="top-capacity"></div></section>
<section class="panel"><h2>Geração térmica — destaques e justificativas (seção 6)</h2><div id="thermal"></div></section></div>
<div class="grid"><section class="panel"><h2>Hidráulica — vazões afluentes fora do padrão (seção 8)</h2>
<div id="hydro-alerts"></div></section>
<section class="panel"><h2>Reservatórios — valores preliminares do IPDO</h2><div id="ear" class="plot"></div></section></div>
<section class="panel" style="margin-top:14px"><h2>Balanço detalhado — IPDO</h2><div style="overflow:auto"><table><thead class="thead"><tr><th>Fonte</th><th>Programado (MWmed)</th><th>Verificado (MWmed)</th><th>Δ (MWmed)</th><th>Δ (%)</th></tr></thead><tbody id="detail"></tbody></table></div></section>
<p class="footer">Fonte: ONS — IPDO (preliminar, sujeito a revisão) e Operação do SIN (séries independentes, sujeitas à consistência). Não confundir as datas e metodologias. <span id="sources"></span></p></main>
<script>const DATA=__EMBED_JSON__;
const f=(x,d=0)=> x===null||x===undefined||!Number.isFinite(Number(x))?'—':Number(x).toLocaleString('pt-BR',{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=(v)=>String(v??'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
const days=(s)=>s?String(s).split('-').reverse().join('/'):'indisponível';
const el=(id)=>document.getElementById(id);const plotCfg={responsive:true,displaylogo:false};
const layout={font:{family:'Century Gothic, Arial, sans-serif',size:11,color:'#284158'},margin:{l:58,r:18,b:60,t:13},paper_bgcolor:'#fff',plot_bgcolor:'#fff',hovermode:'x unified'};
function draw(){const ip=DATA.ipdo,op=DATA.operacao,bal=ip?.balanco||[],isDemo=DATA.modo==='demonstracao';
 el('reference').textContent='IPDO: '+(ip?days(ip.data):'não disponível')+'  |  Operação: '+days(op.ultima_data);
 const outdated=ip && DATA.dias_desatualizacao!==null && DATA.dias_desatualizacao>2;
 el('notice').className='notice'+(ip&&!outdated?' good':'');
 el('notice').textContent=isDemo?'PRÉVIA — valores de IPDO de 21/11/2026 transcritos de documento público; não é uma coleta automática.':
  ip?(outdated?'ATENÇÃO: IPDO não atualizado há '+DATA.dias_desatualizacao+' dias. ':'')+
    'IPDO com dados preliminares de '+days(ip.data)+'. Operação do SIN atualizada até '+days(op.ultima_data)+'. Datas e definições podem diferir.':
    'IPDO indisponível nesta execução. Os dados da aba Operação do SIN permanecem disponíveis. Não foram inventados dados do boletim.';
 const by=Object.fromEntries(bal.map(x=>[x.fonte,x]));const get=(s)=>by[s]?.verificado??null;
 const counts=[['Carga verificada — IPDO',get('Carga'),'MWmed'],['Geração hídrica + Itaipu',get('Hidro Nacional')===null?null:get('Hidro Nacional')+(get('Itaipu Binacional')||0),'MWmed'],['Térmica convencional',get('Termo Convencional'),'MWmed'],['Eólica + solar',(get('Eólica')===null||get('Solar')===null)?null:get('Eólica')+get('Solar'),'MWmed'],
 ['Carga líquida (proxy)',(op.series||[]).filter(x=>x.subsistema==='SIN'&&Number.isFinite(x.carga_liquida_proxy)).at(-1)?.carga_liquida_proxy??null,'MWmed · Operação '+days(op.ultima_data)],
 ['Diferença instalada - disponível',ip?.capacidade_55?.total?.diferenca_mw??null,'MW · IPDO 5.5']];
 el('kpis').innerHTML=counts.map(([n,v,u],idx)=>`<div class="kpi"><div class="caption">${esc(n)}</div><strong>${f(v)}</strong><small>${esc(u)} · ${idx===4?days(op.ultima_data):ip?days(ip.data):'sem IPDO'}</small></div>`).join('');
 const sources=bal.filter(x=>!['Total SIN','Carga','Intercâmbio Internacional'].includes(x.fonte));
 if(sources.length&&window.Plotly){
  Plotly.newPlot('balance',[{type:'bar',name:'Programado',x:sources.map(x=>x.fonte),y:sources.map(x=>x.programado),marker:{color:'#a6c4d9'}},{type:'bar',name:'Verificado',x:sources.map(x=>x.fonte),y:sources.map(x=>x.verificado),marker:{color:'#168eae'}}],{...layout,barmode:'group',xaxis:{tickangle:-28},yaxis:{title:'MWmed',gridcolor:'#ecf1f6'},legend:{orientation:'h',y:1.14},margin:{l:57,r:10,t:30,b:112}},plotCfg);
  Plotly.newPlot('mix',[{type:'pie',hole:.62,labels:sources.map(x=>x.fonte),values:sources.map(x=>x.verificado),textinfo:'percent',hovertemplate:'%{label}: %{value:,.0f} MWmed<extra></extra>',marker:{colors:['#156ba6','#5e9ad2','#9171bd','#f09052','#16a58d','#e6c768']}}],{...layout,margin:{l:5,r:5,t:12,b:12},legend:{orientation:'h',y:-.14}},plotCfg);
 }else{el('balance').textContent='Sem balanço IPDO disponível.';el('mix').textContent='Sem dados para composição.'}
 el('detail').innerHTML=bal.map(x=>{const delta=x.verificado-x.programado, pct=x.programado?delta/x.programado*100:null;const cls=delta>0?'up':delta<0?'down':'';
 return `<tr><td>${esc(x.fonte)}</td><td>${f(x.programado)}</td><td>${f(x.verificado)}</td><td class="${cls}">${delta>0?'+':''}${f(delta)}</td><td class="${cls}">${pct>0?'+':''}${f(pct,1)}%</td></tr>`}).join('')||'<tr><td colspan="5">Não foi possível obter o balanço do IPDO.</td></tr>';
 const sels=[...new Set((op.series||[]).map(r=>r.subsistema))].sort((a,b)=>['SIN','SE/CO','SUL','NE','N'].indexOf(a)-['SIN','SE/CO','SUL','NE','N'].indexOf(b));
 const metricNames={carga:'Carga',hidraulica:'Hidráulica',termica:'Térmica',eolica:'Eólica',solar:'Solar'};
 el('sub').innerHTML=sels.map(x=>`<option value="${esc(x)}"${x==='SIN'?' selected':''}>${esc(x)}</option>`).join('');
 el('metric').innerHTML=Object.entries(metricNames).map(([k,n])=>`<option value="${k}"${['carga','hidraulica'].includes(k)?' selected':''}>${n}</option>`).join('');
 const chosen=(id)=>[...el(id).selectedOptions].map(o=>o.value);
 const per=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)&&Math.abs(b)>1e-8?100*(a-b)/b:null;
 const mean=(arr)=>arr.length?arr.reduce((a,b)=>a+b,0)/arr.length:null;
 const prevMonth=(day)=>{const d=new Date(day+'T12:00:00Z');d.setUTCDate(1);d.setUTCMonth(d.getUTCMonth()-1);return d.toISOString().slice(0,7)};
 const avgDays=(dict,key,dates)=>mean(dates.map(d=>dict[d]?.[key]).filter(Number.isFinite));
 function deltaText(cur,prev){if(!Number.isFinite(cur)||!Number.isFinite(prev))return '—';const delta=cur-prev,pct=per(cur,prev);return `<span class="${delta>0?'up':delta<0?'down':''}">${delta>0?'+':''}${f(delta,0)} MWmed<br><small>${pct>0?'+':''}${f(pct,1)}%</small></span>`;}
 function updateTrend(){
  const subs=chosen('sub'),metrics=chosen('metric'),pairs=[];
  for(const sub of subs)for(const metric of metrics)pairs.push({sub,metric});
  if(!pairs.length){el('trend').innerHTML='<div class="empty">Selecione uma série.</div>';el('trend-table').innerHTML='';return;}
  const colors=['#0d7fa4','#25a191','#dd813f','#7259b3','#c15966','#506c9d','#61a1b3','#b37d36','#b75896'];
  const traces=pairs.map((pair,i)=>{
    const data=(op.series||[]).filter(r=>r.subsistema===pair.sub&&Number.isFinite(r[pair.metric])).sort((a,b)=>a.data.localeCompare(b.data));
    return {type:'scatter',mode:'lines',name:pair.sub+' · '+metricNames[pair.metric],x:data.map(x=>x.data),y:data.map(x=>x[pair.metric]),
      line:{width:2.5,color:colors[i%colors.length],shape:'spline',smoothing:.5},hovertemplate:'%{x}<br>%{y:,.0f} MWmed<extra>%{fullData.name}</extra>'};
  }).filter(x=>x.x.length);
  if(window.Plotly&&traces.length)Plotly.react('trend',traces,{...layout,xaxis:{type:'date',tickformat:'%d/%m',gridcolor:'#edf2f6'},yaxis:{title:'MWmed',gridcolor:'#edf2f6'},
      legend:{orientation:'h',x:0,y:-.22},margin:{l:75,r:15,t:18,b:100}},plotCfg);
  else el('trend').innerHTML='<div class="empty">Séries indisponíveis.</div>';
  const tr=pairs.map(pair=>{
    const series=(op.series||[]).filter(r=>r.subsistema===pair.sub&&Number.isFinite(r[pair.metric])).sort((a,b)=>a.data.localeCompare(b.data));
    if(!series.length)return '';
    const map=Object.fromEntries(series.map(r=>[r.data,r])); const last=series.at(-1),day=last.data;
    const dd=new Date(day+'T12:00:00Z'); const dateShift=n=>new Date(dd.getTime()+n*86400000).toISOString().slice(0,10);
    const last7=Array.from({length:7},(_,i)=>dateShift(-i)),old7=Array.from({length:7},(_,i)=>dateShift(-7-i));
    const week=avgDays(map,pair.metric,last7),prevWeek=avgDays(map,pair.metric,old7);
    const m=day.slice(0,7),pr=prevMonth(day),n=Number(day.slice(8));
    const current=Array.from({length:n},(_,i)=>m+'-'+String(i+1).padStart(2,'0'));
    const old=Array.from({length:n},(_,i)=>pr+'-'+String(i+1).padStart(2,'0'));
    const mon=avgDays(map,pair.metric,current),priorMon=avgDays(map,pair.metric,old);
    return `<tr><td><b>${esc(pair.sub)}</b> · ${esc(metricNames[pair.metric])}<div class="hint">${days(day)}</div></td><td>${f(last[pair.metric],0)}</td><td>${deltaText(last[pair.metric],map[dateShift(-1)]?.[pair.metric])}</td><td>${deltaText(week,prevWeek)}</td><td>${deltaText(mon,priorMon)}</td></tr>`;
  }).filter(Boolean);
  el('trend-table').innerHTML=`<table><thead class="thead"><tr><th>Série</th><th>Último dia<br>MWmed</th><th>Δ diário</th><th>Δ semanal</th><th>Δ mensal</th></tr></thead><tbody>${tr.join('')}</tbody></table>`;
  el('opnote').textContent='Variação diária: último dia − dia anterior. Semanal: média de 7 dias − média dos 7 dias anteriores. Mensal: média do mês corrente até a data − mesmo número de dias do mês anterior. Unidade MWmed; % sobre o período anterior. Série Operação do SIN até '+days(op.ultima_data)+'.';
 }
 el('sub').onchange=updateTrend;el('metric').onchange=updateTrend;updateTrend();
 if(ip?.reservatorios?.length&&window.Plotly){Plotly.newPlot('ear',[{type:'bar',x:ip.reservatorios.map(x=>x.subsistema),y:ip.reservatorios.map(x=>x.ear),marker:{color:'#149c8f'},text:ip.reservatorios.map(x=>f(x.ear,1)+'%'),textposition:'outside'}],{...layout,yaxis:{range:[0,100],title:'EAR (%)',gridcolor:'#eff3f7'},margin:{l:57,r:10,t:30,b:40}},plotCfg)}else{el('ear').innerHTML='<div class="empty">Os valores de EAR não foram extraídos com segurança desta edição do PDF. Consulte a aba EAR para dados consolidados.</div>'}
 const highlights=(ip?.destaques||[]).map(x=>`<li>${esc(x)}</li>`).join('');
 const restrict=(ip?.restricoes||[]).filter(x=>x.maximo_mw!==null).map(x=>`<li><b>${esc(x.subsistema)}</b>: restrição máxima de ${f(x.maximo_mw)} MW. ${esc(x.motivo||'')}</li>`).join('');
 el('highlights').innerHTML='<div class="highlights-flex"><div class="part"><h3 style="font-size:13px">Destaques da operação</h3>'+(highlights?'<ul class="list">'+highlights+'</ul>':'<div class="empty">Nenhum destaque extraído com segurança</div>')+'</div><div class="part"><h3 style="font-size:13px">Restrições de renováveis</h3>'+(restrict?'<ul class="list">'+restrict+'</ul>':'<div class="empty">Sem restrições estruturadas disponíveis</div>')+'</div></div>';
 // --- Enriquecimentos do IPDO, com dados ausentes claramente sinalizados ---
 const empty=(txt)=>'<div class="empty">'+esc(txt)+'</div>';
 const crt=DATA.curtailment||{}, crows=crt.series||[];
 function sumMetric(items,key){return items.every(r=>Number.isFinite(r[key]))?items.reduce((a,r)=>a+r[key],0):null;}
 const pad=(n)=>String(n).padStart(2,'0');
 function aggregateCuts(level){const groups=new Map();
  for(const r of crows){if(!r||!r.data||!Number.isFinite(r.mwh))continue;
   const k=level==='ano'?r.data.slice(0,4):level==='mes'?r.data.slice(0,7):r.data;
   if(!groups.has(k))groups.set(k,[]);groups.get(k).push(r);
  }
  return [...groups.entries()].sort(([a],[b])=>a.localeCompare(b)).map(([date,rows])=>{
   const daysUnique=new Set(rows.map(x=>x.data)).size;
   const total=rows.reduce((a,r)=>a+r.mwh,0);
   const pot=sumMetric(rows,'geracao_potencial_mwh');
   const by=Object.fromEntries(['Eólica','Solar'].map(src=>[src,rows.filter(r=>r.fonte===src).reduce((a,r)=>a+r.mwh,0)]));
   return {data:date,mwh:total,mwmed:total/(24*daysUnique),pct:pot>0?100*total/pot:null,dias:daysUnique,by};
  });
 }
 function updateCuts(level){const series=aggregateCuts(level);const plotted=series.slice(level==='dia'?-60:level==='mes'?-24:-12);
  if(plotted.length&&window.Plotly){
   const traces=['Eólica','Solar'].map((fonte,i)=>({type:'bar',name:fonte,x:plotted.map(r=>r.data),y:plotted.map(r=>r.by[fonte]),
     marker:{color:i?'#e8a246':'#148fa2'},hovertemplate:'%{x}<br>%{y:,.0f} MWh<extra>'+fonte+'</extra>'}));
   Plotly.react('curtailment',traces,{...layout,barmode:'stack',xaxis:{type:'category',tickangle:level==='dia'?-45:0},
    yaxis:{title:'Energia cortada (MWh)'},legend:{orientation:'h',x:0,y:-.28},margin:{l:75,r:20,t:18,b:95}},plotCfg);
   const recent=plotted.at(-1);const pct=Number.isFinite(recent.pct)?f(recent.pct,2)+'%':'sem denominador';
   el('curtailment-kpis').innerHTML=`<span><b>Último ${level}:</b> ${esc(recent.data)}</span><span><b>Corte:</b> ${f(recent.mwh)} MWh</span><span><b>Média:</b> ${f(recent.mwmed,1)} MWmed</span><span><b>Corte/potencial:</b> ${pct}</span><span><b>Dias observados:</b> ${recent.dias}</span>`;
   el('curtailment-table').innerHTML=`<table><thead class="thead"><tr><th>Período</th><th>Restrição (MWh)</th><th>Média (MWmed)</th><th>Restrição / potencial (%)</th><th>Dias com dados</th></tr></thead><tbody>${plotted.slice(-8).reverse().map(r=>`<tr><td>${esc(r.data)}</td><td>${f(r.mwh)}</td><td>${f(r.mwmed,1)}</td><td>${f(r.pct,2)}${Number.isFinite(r.pct)?'%':''}</td><td>${r.dias}</td></tr>`).join('')}</tbody></table>`;
  }else{el('curtailment').innerHTML='<div class="empty">Série de curtailment não disponível.</div>';el('curtailment-table').innerHTML='';el('curtailment-kpis').innerHTML='';}
  for(const btn of el('curt-tabs').querySelectorAll('button'))btn.classList.toggle('active',btn.dataset.level===level);
 }
 for(const btn of el('curt-tabs').querySelectorAll('button'))btn.addEventListener('click',()=>updateCuts(btn.dataset.level));updateCuts('mes');
 el('curtailment-note').textContent='Fonte: ONS Constrained-off Eólica/Fotovoltaica. MWh = GNRa (MWmed) × 0,5 h; MWmed = MWh / 24 h por dia observado. Percentual só aparece se geração verificada e GNRa estiverem completas para a mesma fonte/período. Períodos incompletos não representam ano/mês fechado. Data da fonte: '+days(crt.data_mais_recente)+'.';
 // Carga líquida: todos os perfis horários arquivados, seleção de dia e indicador %.
 const phAll=(op.perfis_horarios_sin||op.perfil_horario_sin||[]).filter(x=>x.datahora&&Number.isFinite(x.carga_liquida_proxy_mw));
 const phMap={};for(const r of phAll){const d=r.datahora.slice(0,10);(phMap[d]??=[]).push(r);}
 const phDates=Object.keys(phMap).filter(d=>phMap[d].length>=12).sort();
 el('netload-date').innerHTML=phDates.slice(-60).reverse().map(d=>`<option value="${d}">${days(d)}</option>`).join('');
 function updateNetDay(){const d=el('netload-date').value;const rows=(phMap[d]||[]).sort((a,b)=>a.datahora.localeCompare(b.data));
  if(!rows.length||!window.Plotly){el('netload-day').innerHTML='<div class="empty">Perfil horário indisponível.</div>';return;}
  const x=rows.map(r=>r.datahora.slice(11,16)),net=rows.map(r=>r.carga_liquida_proxy_mw),glob=rows.map(r=>r.carga_mw),pct=rows.map(r=>r.pct_carga_liquida);
  const avg=mean(net),meanG=mean(glob),ratio=mean(pct);
  const traces=[{type:'scatter',mode:'lines+markers',name:'Carga global (MW)',x,y:glob,line:{color:'#9fb4c4',width:2},marker:{size:3}},
    {type:'scatter',mode:'lines+markers',name:'Carga líquida (MW)',x,y:net,line:{color:'#137da6',width:3},marker:{size:3}},
    {type:'scatter',mode:'lines',name:'Relação líquida/global (%)',x,y:pct,yaxis:'y2',line:{color:'#9a68ba',dash:'dot',width:2}},
    {type:'scatter',mode:'markers+text',name:'Média diária líquida',x:[x.at(-1)],y:[avg],marker:{color:'#e9a23e',size:13,symbol:'diamond'},
      text:[f(avg,0)+' MWmed'],textposition:'top left',textfont:{size:11},showlegend:true}];
  Plotly.react('netload-day',traces,{...layout,xaxis:{title:'Hora',type:'category'},yaxis:{title:'MW'},
    yaxis2:{title:'%',overlaying:'y',side:'right',showgrid:false,range:[0,110]},
    legend:{orientation:'h',x:0,y:-.32},margin:{l:70,r:70,t:30,b:110}},plotCfg);
  el('netload-summary').innerHTML=`<span><b>Dia:</b> ${days(d)}</span><span><b>Carga global:</b> ${f(meanG)} MWmed</span><span><b>Carga líquida:</b> ${f(avg)} MWmed</span><span><b>Relação média:</b> ${f(ratio,1)}%</span><span><b>Horas:</b> ${rows.length}</span>`;
 }
 el('netload-date').onchange=updateNetDay;updateNetDay();
 const opNet=(op.series||[]).filter(x=>x.subsistema==='SIN'&&Number.isFinite(x.carga_liquida_proxy)&&Number.isFinite(x.carga)).sort((a,b)=>a.data.localeCompare(b.data)).slice(-365);
 if(opNet.length&&window.Plotly){
  Plotly.react('netload',[
   {type:'scatter',mode:'lines',name:'Carga global',x:opNet.map(r=>r.data),y:opNet.map(r=>r.carga),line:{color:'#a2b2c6',width:2}},
   {type:'scatter',mode:'lines',name:'Carga líquida',x:opNet.map(r=>r.data),y:opNet.map(r=>r.carga_liquida_proxy),line:{color:'#117da7',width:3}}],
   {...layout,title:{text:'Histórico — carga global × líquida (médias diárias)',font:{size:12}},xaxis:{type:'date',tickformat:'%d/%m'},yaxis:{title:'MWmed'},
     legend:{orientation:'h',x:0,y:-.28},margin:{l:70,r:18,t:45,b:85}},plotCfg);
  const ratios=opNet.map(x=>x.pct_carga_liquida??100*x.carga_liquida_proxy/x.carga);
  const rolling=ratios.map((_,i)=>mean(ratios.slice(Math.max(0,i-29),i+1)));
  Plotly.react('netload-ratio',[
   {type:'scatter',mode:'lines',name:'Carga líquida / global (%)',x:opNet.map(r=>r.data),y:ratios,line:{color:'#8b65b5',width:2}},
   {type:'scatter',mode:'lines',name:'Média móvel 30 dias',x:opNet.map(r=>r.data),y:rolling,line:{color:'#e9a33f',width:2,dash:'dash'}}],
   {...layout,title:{text:'Relação carga líquida / carga global — histórico (%)',font:{size:12}},xaxis:{type:'date',tickformat:'%d/%m'},yaxis:{title:'%'},
     legend:{orientation:'h',x:0,y:-.28},margin:{l:65,r:15,t:42,b:85}},plotCfg);
 }else{el('netload').innerHTML='<div class="empty">Histórico sem dados suficientes.</div>';el('netload-ratio').innerHTML='';}
 const dm=DATA.demanda_ons?.verificada_mw!=null?DATA.demanda_ons:(ip?.demanda_7||{}),dh=DATA.demanda_historico||[];
 let demandTxt=dm.verificada_mw==null?'Não foi possível validar a linha SIN da seção 7; não há alerta automático sem números confirmados.':
  `Pico verificado: ${f(dm.verificada_mw)} MW${dm.hora?' às '+esc(dm.hora):''}. Máximo anterior: ${f(dm.recorde_mw)} MW. ${esc(dm.status||'')}`;
 el('demand-note').textContent=demandTxt+' Fonte ONS, dado preliminar/revisável; picos instantâneos são diferentes das médias diárias de carga.';
 if(dh.length&&window.Plotly){const last=dh.slice(-45);
   Plotly.react('demand-max',[{type:'bar',name:'Demanda máxima diária SIN',x:last.map(r=>r.data),y:last.map(r=>r.mw),
    marker:{color:last.map(r=>dm.novo_recorde&&r.data===last.at(-1)?.data?'#e36a47':'#4d87c2')}}],
   {...layout,xaxis:{type:'date',tickformat:'%d/%m'},yaxis:{title:'MW'},margin:{l:72,r:14,b:74,t:24},
     shapes:dm.recorde_mw!=null?[{type:'line',x0:0,x1:1,xref:'paper',y0:dm.recorde_mw,y1:dm.recorde_mw,line:{color:'#df6345',dash:'dash'}}]:[]},plotCfg);
 }else el('demand-max').innerHTML=empty('Histórico da demanda máxima indisponível.');
 if(dm.novo_recorde===true){el('demand-alert').style.display='block';el('demand-alert').className='notice alert-red';el('demand-alert').textContent='ALERTA DE RECORDE: demanda máxima do SIN superou a máxima anterior do período disponível no ONS. '+demandTxt;}
 const thermalReasons=DATA.termicas_despacho||{},thermalDay=thermalReasons.ultimo_dia;
 if(thermalDay?.submercados?.length&&window.Plotly){
   const rows=[...thermalDay.submercados,thermalDay.sin];const cats=['SE/CO','SUL','NE','N','SIN'];
   const fields=[['Ordem de mérito','merito','#1767a4'],['Inflexibilidade','inflexibilidade','#21a398'],['Razão elétrica','eletrica','#db8445'],
                 ['Garantia energética','garantia','#8d64c0'],['Unit commitment','unitcommitment','#5b84d7'],['Outros motivos','outros','#8996a5']];
   const traces=fields.map(([name,k,color])=>({type:'bar',name,x:cats,y:cats.map(c=>rows.find(r=>r.subsistema===c)?.[k]??0),marker:{color},
     hovertemplate:'%{x}<br>%{y:,.1f} MWmed<extra>'+name+'</extra>'}));
   Plotly.react('thermal-reasons',traces,{...layout,barmode:'stack',xaxis:{type:'category'},yaxis:{title:'Geração verificada (MWmed)'},
     legend:{orientation:'h',x:0,y:-.3},margin:{l:75,r:10,b:110,t:20}},plotCfg);
   el('thermal-reasons-note').textContent='Fonte: ONS Geração Térmica por Motivo de Despacho (base horária), '+days(thermalDay.data)+'. Média por hora disponível, submercados e total SIN. Cobertura mínima '+thermalDay.cobertura_horaria_minima+' horas. A quinta barra é a soma das quatro anteriores, não geração adicional.';
 }else{el('thermal-reasons').innerHTML=empty('Dados horários por motivo de despacho não disponíveis. Não inferimos motivos a partir de texto fragmentado do IPDO.');
   el('thermal-reasons-note').textContent='Fonte oficial: dados.ons.org.br/dataset/geracao-termica-despacho-2. É possível colocar CSV na pasta dados/ipdo/termicas.';}
 const cap=ip?.capacidade_55||{},total=cap.total,hist=DATA.capacidade_historico||[];
 const capTop=cap.top3||[];
 el('top-capacity').innerHTML=capTop.length?capTop.map((r,i)=>`<div style="border-bottom:1px solid #e5edf6;padding:11px 0"><b>${i+1}º · ${esc(r.usina)}</b><span class="down" style="float:right">${f(r.diferenca_mw)} MW</span><div class="meta">Instalada ${f(r.instalada_mw)} MW · Disponível ${f(r.disponivel_mw)} MW · Motivo (IPDO 5.5): ${esc(r.motivo)}</div></div>`).join(''):
 empty('Ranking indisponível: a tabela por usina da seção 5.5 ainda não foi reconhecida de forma confiável.');
 const thermal=ip?.termicas_6||{};
 el('thermal').innerHTML=(thermal.top3||[]).length?(thermal.top3||[]).map((x,i)=>`<div style="border-bottom:1px solid #e5edf6;padding:10px 0"><b>${i+1}. ${esc(x.usina)}</b><p style="font-size:12px;line-height:1.55">${esc(x.justificativa)}</p></div>`).join('')+'<p class="meta">Três primeiras justificativas identificadas, sem ranking quantitativo de diferenças de geração.</p>':
 empty('A seção 6 contém justificativas textuais, mas não foi possível atribuir usinas e motivos com segurança nesta edição.');
 const hy=DATA.hidraulica||{};
 el('hydro-alerts').innerHTML=(hy.alertas||[]).length?(hy.alertas||[]).map(x=>`<div style="border-bottom:1px solid #e5edf6;padding:10px 0"><b>${esc(x.usina)}${x.bacia?' · '+esc(x.bacia):''}</b><span class="down" style="float:right">${x.razao>1?'+':''}${f((x.razao-1)*100,0)}%</span><div class="meta">${f(x.vazao_m3s,1)} m³/s (${days(x.data)}); mediana anterior ${f(x.mediana_60d_m3s,1)} m³/s. Variação a conferir, não diagnóstico de anormalidade.</div></div>`).join(''):
 empty(hy.status||'Sem dados estruturados de vazões para comparar. Os diagramas do IPDO não permitem atribuição automática confiável de vazões às usinas.');
 el('sources').innerHTML=ip?.url?`<a href="${esc(ip.url)}" target="_blank" rel="noopener noreferrer">Abrir a edição IPDO na fonte</a>`:'<a href="https://www.ons.org.br/paginas/resultados-da-operacao/boletins-da-operacao" target="_blank" rel="noopener noreferrer">Boletins da Operação do ONS</a>';
 window.parent?.postMessage({tipo:'altura-painel-ons',id:'ipdo',altura:document.body.scrollHeight},'*');
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',draw);else draw();
</script></body></html>'''

def render(payload):
    # impede encerramento de script com texto de PDF
    embedded=json.dumps(payload,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e')
    htmltext=HTML_TEMPLATE.replace('__EMBED_JSON__',embedded)
    PREVIEW.parent.mkdir(parents=True,exist_ok=True)
    PREVIEW.write_text(htmltext,encoding='utf-8')
    return htmltext

def inject_index():
    path=OUT/'index.html'
    if not path.exists():
        LOG.warning('Página inicial ainda não existe: %s',path)
        return
    s=path.read_text(encoding='utf-8')
    if 'id="botao-ipdo"' not in s:
        button='<button role="tab" id="botao-ipdo" aria-selected="false" aria-controls="aba-ipdo" onclick="selecionar(\'ipdo\')">IPDO · Curto Prazo</button>'
        s=s.replace('<button role="tab" id="botao-ena"',button+'\n<button role="tab" id="botao-ena"',1)
    if 'id="aba-ipdo"' not in s:
        pane='<section class="aba" id="aba-ipdo" role="tabpanel" aria-labelledby="botao-ipdo"><div class="molde"><iframe id="quadro-ipdo" title="IPDO - Operação de Curtíssimo Prazo" src="ipdo.html" loading="lazy" style="height:2000px"></iframe></div></section>'
        s=s.replace('<section class="aba" id="aba-ena"',pane+'\n<section class="aba" id="aba-ena"',1)
    if 'id="botao-ipdo"' not in s or 'id="aba-ipdo"' not in s:
        raise RuntimeError('Não foi possível integrar IPDO na página inicial; verificar layout do índice')
    path.write_text(s,encoding='utf-8')

def main(args=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--sem-rede',action='store_true',help='Não consultar a internet')
    ap.add_argument('--demo',action='store_true',help='Dados oficiais transcritos em 21/11/2026, APENAS para prévia')
    ap.add_argument('--data',help='Data de referência ISO: AAAA-MM-DD')
    ns=ap.parse_args(args)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    reference=dt.date.fromisoformat(ns.data) if ns.data else dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date()
    history=read_json(SNAPSHOTS,[])
    if not isinstance(history,list):history=[]
    latest=history[-1] if history else None
    if ns.demo:
        latest=PREVIEW_DATA
    else:
        manual=load_pdf_manual(RAW)
        downloaded=None if ns.sem_rede else fetch_latest(reference)
        # fonte de data mais recente prevalece; PDFs diferentes são validados internamente
        for result,kind in sorted([(x,k) for x,k in [(manual,'pdf-manual'),(downloaded,'pdf-ons')] if x], key=lambda x:x[0][0]):
            date,blob,url=result
            if date>reference:
                LOG.warning('Ignorando IPDO futuro: %s',date);continue
            if latest and date.isoformat()<latest['data']:continue
            try:
                parsed=parse_pdf(blob,date,url,kind)
                if not any(x.get('data')==parsed['data'] for x in history): history.append(parsed)
                else:history=[(parsed if x.get('data')==parsed['data'] else x) for x in history]
                latest=parsed
            except Exception as e:
                LOG.error('IPDO %s encontrado, mas extração insegura: %s',date,e)
        history=sorted(history,key=lambda r:r['data'])[-365:]
        if history:write_json(SNAPSHOTS,history)
        if not latest: LOG.warning('IPDO ainda não capturado. A aba exibirá estado sem IPDO.')
    op=load_operation()
    curtailment=load_curtailment(RAW,reference,online=not ns.sem_rede and not ns.demo)
    demanda_ons=get_ons_demanda(RAW,reference,online=not ns.sem_rede and not ns.demo)
    hydro_official=get_ons_hydro(RAW,reference,online=not ns.sem_rede and not ns.demo)
    vazoes=load_hydro_anomalies(RAW,reference,structured_rows=hydro_official)
    termicas_despacho=load_thermal_dispatch(RAW,reference,online=not ns.sem_rede and not ns.demo)
    cap_hist=[{'data':r['data'],'diferenca_mw':r['capacidade_55']['total']['diferenca_mw']}
              for r in history if r.get('capacidade_55',{}).get('total')]
    demand_hist=[{'data':r['data'],'mw':r['demanda_7']['verificada_mw']}
                 for r in history if r.get('demanda_7',{}).get('verificada_mw') is not None]
    days=(reference-dt.date.fromisoformat(latest['data'])).days if latest else None
    data={'gerado_em':dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).isoformat(timespec='seconds'),
          'modo':'demonstracao' if ns.demo else 'producao','ipdo':latest,'dias_desatualizacao':days,
          'operacao':op,'curtailment':curtailment,'hidraulica':vazoes,'termicas_despacho':termicas_despacho,
          'capacidade_historico':cap_hist,
          'demanda_historico':demanda_ons['historico'] or demand_hist,
          'demanda_ons':demanda_ons['dados'],
          'panorama_insumos':{'ipdo_data':latest['data'] if latest else None,'operacao_data':op['ultima_data'],
            'balanco':latest.get('balanco',[]) if latest else [],
            'restricoes':latest.get('restricoes',[]) if latest else [],
            'destaques':latest.get('destaques',[]) if latest else [],
            'variacoes_relevantes':op['variacoes_relevantes'],
            'perfil_carga_liquida':op['perfil_horario_sin'][-24:],
            'termicas_por_motivo':termicas_despacho['ultimo_dia'],
            'carga_liquida_proxy':next((x['carga_liquida_proxy'] for x in reversed(op['series']) if x['subsistema']=='SIN' and 'carga_liquida_proxy' in x),None),
            'demanda_maxima':demanda_ons['dados'] or (latest.get('demanda_7') if latest else None),
            'disponibilidade_termica':latest.get('capacidade_55') if latest else None,
            'destaques_termicos':latest.get('termicas_6') if latest else None,
            'curtailment_data':curtailment['data_mais_recente'],
            'curtailment_ultimos_30_dias':curtailment['series'][-60:],
            'alertas_vazoes':vazoes['alertas'],
            'alertas':(['IPDO desatualizado há '+str(days)+' dias'] if days is not None and days>2 else [])
                       +(['Demanda máxima SIN igualou ou superou referência histórica do IPDO']
                         if (demanda_ons['dados'] and demanda_ons['dados'].get('novo_recorde') is True) or
                            (latest and latest.get('demanda_7',{}).get('novo_recorde') is True) else [])
                       +(['Vazões com variações expressivas a verificar'] if vazoes['alertas'] else [])}}
    write_json(DATA_JSON,data)
    render(data)
    inject_index()
    LOG.info('IPDO pronto: data=%s, operação=%s, status=%s',latest['data'] if latest else 'indisponível',op['ultima_data'],data['modo'])
    return data

if __name__=='__main__':main()
