"""Camada complementar IPDO: extrações conservadoras e séries históricas com fonte identificada.
Não inferir dados das ilustrações hidráulicas sem estrutura verificável.
"""
from __future__ import annotations
import csv, datetime as dt, io, re, statistics, logging, json, unicodedata
from collections import defaultdict
from pathlib import Path
import requests

log = logging.getLogger('ipdo.enriquecimento')

# Na seção 5.5: grupos de motivos, usinas, e total com capacidade instalada/disponível.
MOTIVOS = {'1': 'Manutenção', '2': 'Restrição operativa',
           '3': 'Restrição operativa e manutenção', '4': 'Total'}


def read_pdf_section(doc, start, end):
    """Localiza a seção verdadeira, evitando a ocorrência abreviada no sumário.

    Alguns IPDOs repetem cabeçalhos no sumário e no corpo do documento.
    Preferimos o último bloco suficientemente extenso antes da próxima seção.
    """
    text = '\n'.join(page.get_text('text', sort=True) for page in doc)
    beginnings = list(re.finditer(start, text, re.I))
    if not beginnings:
        return ''
    candidates = []
    for a in beginnings:
        b = re.search(end, text[a.end():], re.I)
        fragment = text[a.start():a.end()+b.start()] if b else text[a.start():a.start()+20000]
        candidates.append(fragment)
    substantial = [x for x in candidates if len(x) > 250]
    return substantial[-1] if substantial else candidates[-1]


def parse_triplets(chunk):
    """Reconhece [usina, instalada, disponível, diferença] em mesma linha ou
    linhas sequenciais e valida Instalada - Disponível = Diferença.
    Aceita valores MW brasileiros com ponto de milhar e/ou vírgula decimal.
    """
    clean = [re.sub(r'\s+', ' ', x).strip() for x in chunk.splitlines()]
    skip = re.compile(r'^(?:usinas?|capacidade|instalada|dispon[ií]vel|diferença|aparecida|ons/cnos|informativo|operador nacional|pag\.|[0-9]{1,2}\s*[-–]\s*)$',re.I)
    number = r'-?(?:\d{1,3}(?:\.\d{3})+|\d{1,6})(?:,\d{1,2})?'
    numeric = re.compile('^' + number + '$')
    combined = re.compile(r'^(.*?)\s+('+number+r')\s+('+number+r')\s+('+number+r')\s*$')
    def conv(x):
        return float(x.replace('.','').replace(',','.'))
    out=[]
    for i,line in enumerate(clean):
        if not line or skip.match(line) or re.match(r'^5\.5\.',line) or len(line)>150:
            continue
        m=combined.match(line)
        if m:
            name=m.group(1)
            vals=[conv(v) for v in m.groups()[1:]]
        elif not numeric.fullmatch(line) and i+3<len(clean) and all(numeric.fullmatch(clean[i+j]) for j in (1,2,3)):
            name=line
            vals=[conv(clean[i+j]) for j in (1,2,3)]
        else:
            continue
        if not re.search(r'[A-Za-zÀ-ÿ]',name):
            continue
        installed,available,diff=vals
        if installed<0 or available<0 or abs(installed-available-diff)>2 or max(installed,available)>100000:
            continue
        out.append({'usina':name,'instalada_mw':round(installed,2),'disponivel_mw':round(available,2),'diferenca_mw':round(diff,2)})
    unique={}
    for r in out:
        unique.setdefault((r['usina'],r['instalada_mw'],r['disponivel_mw']),r)
    return list(unique.values())


def parse_capacidade_55(doc):
    section=read_pdf_section(doc,r'5\.5\s*[-–]\s*Principais diferenças',r'(?:5\.6\s*[-–]|6\s*[-–]\s*Destaques)')
    if not section:return {'total':None,'grupos':[],'top3':[], 'status':'seção 5.5 não localizada'}
    parts=re.split(r'5\.5\.([1-4])\s*[-–]',section,flags=re.I)
    groups=[];total=None;plants=[]
    for ix in range(1,len(parts)-1,2):
        key=parts[ix]; rows=parse_triplets(parts[ix+1])
        if not rows: continue
        totals=[r for r in rows if r['usina'].casefold().strip()=='total']
        if key=='4':
            if totals: total=totals[-1]
            groups=[{'motivo':r['usina'],'instalada_mw':r['instalada_mw'],'disponivel_mw':r['disponivel_mw'],'diferenca_mw':r['diferenca_mw']} for r in rows if r['usina'].casefold().strip()!='total']
        else:
            for r in rows:
                if r['usina'].casefold().strip()!='total':plants.append({**r,'motivo':MOTIVOS[key]})
    plants.sort(key=lambda r:r['diferenca_mw'],reverse=True)
    if total and total['diferenca_mw']<0: total=None
    return {'total':total,'grupos':groups,'top3':plants[:3],
            'status':'extraído da seção 5.5' if total else 'total 5.5.4 não validado'}


def parse_termicas_6(doc):
    section=read_pdf_section(doc,r'6\s*[-–]\s*Destaques da Geração Térmica',r'7\s*[-–]\s*Demandas Máximas')
    if not section:return {'destaques':[],'top3':[],'status':'seção 6 não localizada'}
    # Descartar texto institucional repetitivo de propósito. A seção 6 é narrativa,
    # sem ranking de desvios por usina; top 3 = primeiras usinas com justificativa legível.
    clean=re.sub(r'\s+',' ',section)
    cut=re.search(r'(?:PRINCIPAIS\s+DIFERENÇAS|A\s+UTE\s+)',clean,re.I)
    if cut:clean=clean[cut.start():]
    chunks=re.split(r'(?=\bA\s+UTE\s+)',clean,flags=re.I)
    result=[]
    for ch in chunks:
        ch=ch.strip(' *•-')
        if not ch.lower().startswith('a ute '):continue
        if len(ch)<55 or len(ch)>10000:continue
        # Análise lexical limitada a quando UTE + razão estão legíveis.
        nm=re.search(r'^A\s+UTE\s+(.{3,60}?)(?:\s*\(|\s+gerou\b|\s+teve\b|\s+ficou\b|\s+apresentou\b|\s+foi\b)',ch,re.I)
        if not nm:continue
        usina=nm.group(1).strip(' .')
        text=ch[:500].split(' A UTE ')[0].strip()
        if len(usina)>50 or len(text)<45:continue
        result.append({'usina':usina,'justificativa':text})
    # Uma usina por entrada, sem atribuir magnitude inventada
    unique={}
    for r in result:unique.setdefault(r['usina'].casefold(),r)
    got=list(unique.values())
    return {'destaques':got[:10],'top3':got[:3],
            'status':'3 primeiras justificativas identificáveis; não é ranking quantitativo' if got else 'narrativa sem extração segura'}


def parse_demanda_7(doc, day):
    section=read_pdf_section(doc,r'7\s*[-–]\s*Demandas Máximas',r'8\s*[-–]\s*Dados Hidráulicos')
    if not section:return {'verificada_mw':None,'recorde_mw':None,'novo_recorde':None,'status':'seção 7 não localizada'}
    # Requer que os valores do SIN apareçam em uma mesma linha; rejeita diagramas com
    # valores dispersos, que poderiam fazer atribuir cargas de outro submercado ao SIN.
    match=None
    for line in section.splitlines():
        s=re.sub(r'\s+',' ',line).strip()
        if not re.match(r'^SIN\b',s,re.I):continue
        # Ex.: SIN 108.200 às 19h34min 106.536 em 26/02/2025
        m=re.search(r'\bSIN\s+([\d.]+)\s+às\s+(\d{1,2})h(\d{2})min\s+([\d.]+)\s+em\s+(\d{2}/\d{2}/20\d\d)',s,re.I)
        if m: match=m;break
    if not match:
        return {'verificada_mw':None,'recorde_mw':None,'novo_recorde':None,
                'status':'seção 7 encontrada, porém tabela SIN requer leitura estruturada; sem alerta especulativo'}
    actual=int(match.group(1).replace('.',''))
    record=int(match.group(4).replace('.',''))
    recday=match.group(5)
    if not (30000<=actual<=160000 and 30000<=record<=160000):
        return {'verificada_mw':None,'recorde_mw':None,'novo_recorde':None,'status':'demanda fora da faixa plausível'}
    # Se o ONS atualizou o recorde no próprio dia, a data do recorde é a data verificada.
    exceeded=actual>=record and (actual>record or recday==day.strftime('%d/%m/%Y'))
    return {'verificada_mw':actual,'hora':match.group(2)+':'+match.group(3),
            'recorde_mw':record,'recorde_data':recday,'novo_recorde':exceeded,
            'status':'comparação com máxima histórica informada no próprio IPDO'}


def parse_ons_curtailment_csv(blob:bytes,fonte:str,com_denominador:bool=False):
    """GNRa em MWmed por intervalo de 30 min -> MWh. Denominador consistente
    somente quando val_geracao verificada está presente em TODOS os intervalos
    usados no numerador, na mesma granularidade e fonte.
    """
    stream=io.StringIO(blob.decode('utf-8-sig'))
    first=stream.readline();stream.seek(0)
    reader=csv.DictReader(stream,delimiter=';' if first.count(';')>=first.count(',') else ',')
    cols={c.strip().lower():c for c in (reader.fieldnames or [])}
    if not {'din_instante','val_geracaonaorealizadaapurada'}.issubset(cols):
        raise ValueError('Colunas ONS obrigatorias para GNRa ausentes')
    energy=defaultdict(float)
    expected=defaultdict(lambda:{'verificada_mwh':0.,'validos':0,'total':0})
    valid=0
    for row in reader:
        day=str(row.get(cols['din_instante'],'')).strip()[:10]
        if not re.fullmatch(r'20\d\d-\d\d-\d\d',day):continue
        raw=str(row.get(cols['val_geracaonaorealizadaapurada'],'')).strip()
        if raw in ('','-','nan','NaN'):continue
        try:val=float(raw.replace(',','.'))
        except ValueError:continue
        if not 0<=val<1e7:continue
        key=(day,fonte)
        energy[key]+=val*.5
        valid+=1
        expected[key]['total']+=1
        col=cols.get('val_geracao')
        if col:
            gen=str(row.get(col,'')).strip()
            try:
                gv=float(gen.replace(',','.'))
                if 0<=gv<1e7:
                    expected[key]['verificada_mwh']+=gv*.5
                    expected[key]['validos']+=1
            except ValueError:pass
    if valid<1:raise ValueError('Nenhum dado GNRa valido')
    if com_denominador:return energy,valid,dict(expected)
    return energy,valid

def curtailment_urls(reference):
    base='https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/'
    months=[reference.replace(day=1)]
    for i in range(1,3):
        prev=months[-1]-dt.timedelta(days=1);months.append(prev.replace(day=1))
    for day in months:
        ym=day.strftime('%Y_%m')
        for fonte,pasta,prefixo in [
          ('Eólica','restricao_coff_eolica_tm','RESTRICAO_COFF_EOLICA_'),
          ('Solar','restricao_coff_fotovoltaica_tm','RESTRICAO_COFF_FOTOVOLTAICA_')]:
            yield day,fonte,base+pasta+'/'+prefixo+ym+'.csv'


def load_curtailment(raw:Path, reference:dt.date, online:bool):
    hist_file=raw/'curtailment_historico.json'
    try:
        import json
        history=json.loads(hist_file.read_text(encoding='utf-8'))
        if not isinstance(history,list):history=[]
    except Exception:history=[]
    values={(x['data'],x['fonte']):float(x['mwh']) for x in history if all(k in x for k in ('data','fonte','mwh'))}
    denominadores={(x['data'],x['fonte']):x.get('geracao_verificada_mwh') for x in history if x.get('geracao_verificada_mwh') is not None}
    attempted=[];updated=[]
    # Também aceita CSVs oficiais arquivados localmente, sem depender da rede.
    sources=[]
    for local in sorted((raw/'curtailment').glob('*.csv')) if (raw/'curtailment').exists() else []:
        source='Solar' if 'foto' in local.stem.lower() or 'solar' in local.stem.lower() else 'Eólica' if 'eol' in local.stem.lower() else None
        if source:sources.append((source,local.read_bytes(),local.name))
    if online:
        session=requests.Session()
        session.headers.update({'User-Agent':'Painel-IPDO-ONS/2.0 (consulta-publica)','Accept':'text/csv,*/*'})
        for _,fonte,url in curtailment_urls(reference):
            attempted.append(url)
            try:
                resp=session.get(url,timeout=(5,15))
                if resp.status_code==200 and len(resp.content)>40 and len(resp.content)<65_000_000:
                    sources.append((fonte,resp.content,url))
                else:log.info('Curtailment %s: HTTP %s',fonte,resp.status_code)
            except requests.RequestException as exc:log.warning('Curtailment indisponível %s: %s',url,exc)
    for fonte,blob,url in sources:
        try:
            # Nao sobrescrever `reference` (data limite da coleta) com o
            # dicionario de cobertura de geracao verificada do CSV.
            parsed,n,coverage_by_day=parse_ons_curtailment_csv(blob,fonte,com_denominador=True)
            values.update({k:round(v,3) for k,v in parsed.items()})
            for k,meta in coverage_by_day.items():
                if meta['validos']==meta['total'] and meta['total']>0:
                    denominadores[k]=round(meta['verificada_mwh'],3)
                else:denominadores.pop(k,None)
            updated.append({'fonte':fonte,'url':url,'registros':n})
        except Exception as exc:log.warning('CSV de curtailment ignorado (%s): %s',url,exc)
    # Persistir apenas dados resumidos. Ausência não vira zero.
    recent=[]
    for (d,fonte),mwh in values.items():
        if d>reference.isoformat():continue
        gen=denominadores.get((d,fonte))
        pot=mwh+gen if gen is not None else None
        pct=round(100*mwh/pot,3) if pot and pot>0 else None
        recent.append({'data':d,'fonte':fonte,'mwh':round(mwh,3),
                       'mwmed':round(mwh/24,3),
                       'geracao_verificada_mwh':gen,'geracao_potencial_mwh':round(pot,3) if pot is not None else None,
                       'percentual_corte':pct})
    recent=sorted(recent,key=lambda r:(r['data'],r['fonte']))[-12000:]
    if updated:
        import json
        hist_file.parent.mkdir(parents=True,exist_ok=True)
        hist_file.write_text(json.dumps(recent,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'series':recent,'data_mais_recente':max((r['data'] for r in recent),default=None),
            'arquivos_processados':updated,'fontes':{
             'Eólica':'https://dados.ons.org.br/dataset/restricao_coff_eolica_usi',
             'Solar':'https://dados.ons.org.br/dataset/restricao_coff_fotovoltaica'},
            'metodo':'GNRa semihoraria MWmed × 0,5 h = MWh; percentual = GNRa / (GNRa + geracao verificada) nas mesmas linhas de fonte e data. MWmed do dia=MWh/24; periodos incompletos sinalizados.'}


def load_hydro_anomalies(raw:Path, reference:dt.date, structured_rows=None):
    """Dados hidráulicos da seção 8 são diagramas: não atribuir automaticamente números.
    Se houver CSV estruturado de origem verificada, comparar cada usina consigo mesma.
    """
    path=raw/'vazoes_usinas.csv'
    result={'data':None,'alertas':[], 'status':'Vazões da seção 8 em diagramas: sem extração segura. Forneça base estruturada verificada para detecção.'}
    if not path.exists() and structured_rows is None:return result
    byname=defaultdict(list)
    try:
        if structured_rows is not None:
            for row in structured_rows:
                try:
                    day=dt.date.fromisoformat(row['data'])
                    val=float(row['vazao_afluente_m3s'])
                    if val<0 or day>reference:continue
                    byname[row['usina']].append((day,val,row.get('bacia','')))
                except (KeyError, ValueError, TypeError):continue
        elif path.exists():
            with path.open(encoding='utf-8-sig',newline='') as file:
                r=csv.DictReader(file,delimiter=';')
                if not {'data','usina','vazao_afluente_m3s'}.issubset(r.fieldnames or []):return result
                for row in r:
                    try:
                        day=dt.date.fromisoformat(row['data'])
                        v=float(row['vazao_afluente_m3s'].replace(',','.'))
                        if v<0 or v>250000 or day>reference:continue
                        byname[row['usina']].append((day,v,row.get('bacia','')))
                    except (ValueError,TypeError):continue
    except OSError:return result
    alerts=[]
    for name, rows in byname.items():
        rows=sorted(rows)
        day,val,bacia=rows[-1]
        if (reference-day).days>5:continue
        baseline=[v for d,v,_ in rows[:-1] if 0<(day-d).days<=60]
        if len(baseline)<14:continue
        med=statistics.median(baseline)
        if med<=10:continue
        # Exigir mudança ampla para reduzir falsos alarmes; não diagnostica anomalia causal.
        ratio=val/med
        if ratio>=2.5 and val-med>=100:
            alerts.append({'usina':name,'bacia':bacia,'data':day.isoformat(),'vazao_m3s':val,'mediana_60d_m3s':round(med,1),'razao':round(ratio,2),'tipo':'alta'})
        elif ratio<=0.30 and med-val>=100:
            alerts.append({'usina':name,'bacia':bacia,'data':day.isoformat(),'vazao_m3s':val,'mediana_60d_m3s':round(med,1),'razao':round(ratio,2),'tipo':'baixa'})
    alerts.sort(key=lambda x:max(x['razao'],1/max(x['razao'],.001)),reverse=True)
    result.update({'data':max((x[0] for arr in byname.values() for x in arr),default=None).isoformat() if byname else None,
                   'alertas':alerts[:10], 'status':'Comparação descritiva contra mediana dos 60 dias anteriores, mínimo 14 dias; requer verificação humana da fonte.'})
    return result

# Bases ONS com estrutura tabular para complementar as seções em PDF sem OCR.
DEMAND_URL = 'https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/demanda_maxima_di/DEMANDA_MAXIMA_DI_{year}.csv'
HYDRO_URL = 'https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/dados_hidrologicos_di/DADOS_HIDROLOGICOS_RES_{year}.csv'


def _get_official_bytes(url:str, online:bool, local:Path=None, limit:int=50_000_000):
    if local is not None and local.exists():
        blob=local.read_bytes()
        return blob if len(blob)<=limit else None
    if not online:return None
    try:
        resp=requests.get(url,headers={'User-Agent':'Painel-IPDO-ONS/2.0 (consulta-publica)', 'Accept':'text/csv,*/*'}, timeout=(6,22), stream=True)
        if resp.status_code!=200: log.warning('ONS %s: HTTP %s',url,resp.status_code);return None
        pieces=[];length=0
        for part in resp.iter_content(chunk_size=1024*256):
            length+=len(part)
            if length>limit:raise ValueError('Arquivo do ONS excedeu limite de tamanho')
            pieces.append(part)
        return b''.join(pieces)
    except (requests.RequestException,ValueError) as ex:
        log.warning('Falha fonte ONS %s: %s',url,str(ex)[:180])
        return None


def _csv_dict(blob):
    data=blob.decode('utf-8-sig',errors='replace')
    first=data.splitlines()[0] if data.splitlines() else ''
    delim=';' if first.count(';')>=first.count(',') else ','
    return csv.DictReader(io.StringIO(data),delimiter=delim)


def get_ons_demanda(raw:Path,reference:dt.date,online:bool):
    """Validação forte: SIN só pode vir da linha SIN, nunca da soma de picos submercados.
    Recorde é máximo do histórico carregado, NÃO necessariamente máximo histórico absoluto ONS.
    """
    bydate={};refs=[]
    for year in range(max(2024,reference.year-2),reference.year+1):
        url=DEMAND_URL.format(year=year)
        blob=_get_official_bytes(url,online,raw/f'DEMANDA_MAXIMA_DI_{year}.csv',8_000_000)
        if not blob:continue
        try:
            reader=_csv_dict(blob)
            cols={k.lower().strip():k for k in reader.fieldnames or []}
            mandatory={'nom_subsistema','dat_referencia','val_demandainstantanea'}
            if not mandatory.issubset(cols):raise ValueError('Colunas obrigatórias não encontradas')
            for r in reader:
                sub=str(r.get(cols['nom_subsistema'],'')).strip().upper()
                sid=str(r.get(cols.get('id_subsistema','id_subsistema'),'')).strip().upper()
                if not ('SIN'==sid or sub in ('SIN','SISTEMA INTERLIGADO NACIONAL')):continue
                day=str(r[cols['dat_referencia']])[:10]
                if not re.fullmatch(r'20\d\d-\d\d-\d\d',day) or day>reference.isoformat():continue
                val=float(str(r[cols['val_demandainstantanea']]).replace(',','.'))
                if not 30000<=val<=160000:continue
                bydate[day]=round(val,2)
            refs.append(url)
        except (ValueError,KeyError,TypeError) as ex:log.warning('Demanda ONS ignorada %s: %s',url,ex)
    series=[{'data':d,'mw':v} for d,v in sorted(bydate.items())]
    if not series:
        return {'dados':None,'historico':[], 'fontes':refs,'status':'Base SIN da Demanda Máxima ONS indisponível ou sem linha SIN.'}
    today=series[-1]
    old=[r['mw'] for r in series if r['data']<today['data']]
    prior_max=max(old) if old else None
    new= today['mw']>prior_max if prior_max else None
    hour=''
    return {'dados':{'verificada_mw':today['mw'],'data':today['data'], 'recorde_mw':prior_max,
                     'novo_recorde':new,'hora':hour,
                     'status':f'ONS Dados Abertos; comparação com o máximo dos {len(old)} dias anteriores disponíveis no período {series[0]["data"]}–{today["data"]}.'},
            'historico':series[-370:],'fontes':refs,'status':'dados oficiais tabulares (sujeitos a revisão)'}


def get_ons_hydro(raw:Path,reference:dt.date,online:bool):
    """Obter vazões com correspondência nominativa explícita do dataset diário do ONS.
    Não usar gráfico da seção 8 para atribuir número à usina.
    """
    url=HYDRO_URL.format(year=reference.year)
    local=raw/f'DADOS_HIDROLOGICOS_RES_{reference.year}.csv'
    blob=_get_official_bytes(url,online,local,55_000_000)
    if not blob:return None
    try:
        reader=_csv_dict(blob)
        cols={k.lower().strip():k for k in reader.fieldnames or []}
        if not {'din_instante','nom_reservatorio','val_vazaoafluente'}.issubset(cols):
            raise ValueError('Dados hidráulicos: colunas requeridas ausentes')
        cutoff=(reference-dt.timedelta(days=85)).isoformat()
        result=[]
        for r in reader:
            date=str(r[cols['din_instante']])[:10]
            if not (cutoff<=date<=reference.isoformat()):continue
            name=str(r[cols['nom_reservatorio']]).strip()
            raw_val=str(r[cols['val_vazaoafluente']]).strip()
            if not name or raw_val in ('','-', 'NaN'):continue
            val=float(raw_val.replace(',','.'))
            if not (0<=val<250000):continue
            result.append({'data':date,'usina':name,'vazao_afluente_m3s':val,
                           'bacia':r.get(cols.get('nom_bacia','nom_bacia'),'')})
        return result
    except (ValueError,KeyError,TypeError) as ex:
        log.warning('Dados hidráulicos ONS rejeitados: %s',ex)
        return None


# Geração térmica por motivo de despacho — base tabular oficial ONS.
# https://dados.ons.org.br/dataset/geracao-termica-despacho-2
THERMAL_BASE=('https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/'
              'geracao_termica_despacho_2_ho/GERACAO_TERMICA_DESPACHO-2_')
THERMAL_MOTIVES=[
 ('Ordem de mérito','merito','#1767a4'),
 ('Inflexibilidade','inflexibilidade','#21a398'),
 ('Razão elétrica','eletrica','#db8445'),
 ('Garantia energética','garantia','#8d64c0'),
 ('Unit commitment','unitcommitment','#5b84d7'),
 ('Outros motivos','outros','#8996a5')]

def _clean_number(value):
    if value is None:return 0.
    s=str(value).strip()
    if not s or s.lower() in ('nan','none','null','-'):return 0.
    try:return max(0.,float(s.replace(',','.')))
    except ValueError:return 0.

def _sub_label(x):
    s=''.join(c for c in unicodedata.normalize('NFKD',str(x).upper()) if not unicodedata.combining(c)).strip()
    if 'SUDESTE' in s or 'CENTRO' in s or s in ('SE','SE/CO','SECO','SECO/S'):return 'SE/CO'
    if 'NORDESTE' in s or s=='NE':return 'NE'
    if 'NORTE' in s or s=='N':return 'N'
    if 'SUL' in s or s=='S':return 'SUL'
    return None

def parse_thermal_dispatch_csv(blob:bytes):
    """Agrupa a geracao VERIFICADA em MWmed, evitando dupla contagem de
    inflexibilidade embutida na ordem de merito. Retorna dias por subsistema.
    """
    raw=blob.decode('utf-8-sig')
    fh=io.StringIO(raw)
    line=fh.readline();fh.seek(0)
    rdr=csv.DictReader(fh,delimiter=';' if line.count(';')>=line.count(',') else ',')
    cols={x.lower().strip():x for x in (rdr.fieldnames or [])}
    required={'din_instante','nom_subsistema','nom_usina','val_verifgeracao'}
    if not required.issubset(cols):raise ValueError('Colunas essenciais ausentes no CSV termico: '+str(required-set(cols)))
    hourly=defaultdict(lambda:defaultdict(float))
    plant_hourly=defaultdict(float)
    for row in rdr:
        date=str(row.get(cols['din_instante'],'')).strip()[:13]
        if not re.fullmatch(r'20\d\d-\d\d-\d\d[ T]\d\d',date):continue
        sub=_sub_label(row.get(cols['nom_subsistema'],''))
        if not sub:continue
        val=lambda key:_clean_number(row.get(cols[key])) if key in cols else 0.
        actual=val('val_verifgeracao')
        if actual>100000:continue
        merit=val('val_verifordemdemeritoacimadainflex') if 'val_verifordemdemeritoacimadainflex' in cols else val('val_verifordemmerito')
        inflex=(val('val_verifinflexembutmerito')+val('val_verifinflexpura')) if 'val_verifinflexpura' in cols else val('val_verifinflexibilidade')
        if 'val_verifordemdemeritoacimadainflex' not in cols and 'val_verifinflexpura' in cols:
            inflex=val('val_verifinflexpura')
        elec=val('val_verifrazaoeletrica')
        sec=val('val_verifgarantiaenergetica')
        unit=val('val_verifunitcommitment')
        # Demais componentes documentados; limitados ao residual, sem duplicar motivos.
        known=merit+inflex+elec+sec+unit
        if known>actual*1.025+2:
            # Inconsistencia de classificacao: preservar o total, indicar sem motivo
            # ao inves de exibir motivos com soma superior ao total.
            merit=inflex=elec=sec=unit=0.
        other=max(0.,actual-(merit+inflex+elec+sec+unit))
        k=(date[:10],date[11:13],sub)
        h=hourly[k]
        for name,value in [('total',actual),('merito',merit),('inflexibilidade',inflex),
                           ('eletrica',elec),('garantia',sec),('unitcommitment',unit),('outros',other)]:
            h[name]+=value
        plant=str(row.get(cols['nom_usina'],'')).strip()
        if plant:plant_hourly[(date[:10],date[11:13],sub,plant)]+=actual
    if not hourly:raise ValueError('CSV termico sem registros validos')
    agg=defaultdict(lambda:defaultdict(float));hours=defaultdict(set)
    for (day,hour,sub),vals in hourly.items():
        for key,val in vals.items():agg[(day,sub)][key]+=val
        hours[(day,sub)].add(hour)
    byplants=defaultdict(float)
    for (day,hour,sub,plant),val in plant_hourly.items():byplants[(day,sub,plant)]+=val
    dates=sorted({k[0] for k in agg})
    out=[]
    for day in dates:
        daily=[]
        for sub in ['SE/CO','SUL','NE','N']:
            if (day,sub) not in agg:continue
            n=len(hours[(day,sub)])
            values={k:round(v/n,3) for k,v in agg[(day,sub)].items()}
            daily.append({'data':day,'subsistema':sub,'horas':n,**values})
        if len(daily)!=4:continue
        sin={key:round(sum(x.get(key,0) for x in daily),3) for key in ('total','merito','inflexibilidade','eletrica','garantia','unitcommitment','outros')}
        top=sorted([{'usina':p,'subsistema':sub,'mwmed':round(v/max(1,len(hours[(d,sub)])),2)}
                    for (d,sub,p),v in byplants.items() if d==day],key=lambda x:-x['mwmed'])[:3]
        out.append({'data':day,'submercados':daily,'sin':{'subsistema':'SIN',**sin},
                    'top3_usinas':top,'cobertura_horaria_minima':min(x['horas'] for x in daily),
                    'metodo':'media horaria verificada das usinas por subsistema; classes exclusivas sem dupla contagem'})
    return out

def load_thermal_dispatch(raw:Path, reference:dt.date, online=True):
    """Importa CSV local ou duas ultimas referencias mensais oficiais; grava
    somente resumos diários (não publica o CSV bruto no site)."""
    path=raw/'termicas_despacho_historico.json'
    try:hist=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):hist=[]
    byday={r['data']:r for r in hist if isinstance(r,dict) and 'data' in r}
    sources=[];issues=[]
    manual=raw/'termicas'
    if manual.exists():
        for f in sorted(manual.glob('*.csv')):
            sources.append((f.name,f.read_bytes()))
    if online:
        month=reference.replace(day=1)
        for i in range(2):
            ym=month.strftime('%Y_%m')
            url=THERMAL_BASE+ym+'.csv'
            blob=_get_official_bytes(url,True,limit=75_000_000)
            if blob:sources.append((url,blob))
            else:issues.append('Arquivo '+ym+' nao acessivel')
            month=(month-dt.timedelta(days=1)).replace(day=1)
    for url,blob in sources:
        try:
            entries=parse_thermal_dispatch_csv(blob)
            for r in entries:
                if r['data']<=reference.isoformat():byday[r['data']]=r
            log.info('Termicas por motivo: %d dias processados de %s',len(entries),url)
        except Exception as ex:issues.append(url+': '+str(ex)[:120]);log.warning('Termicas CSV: %s',ex)
    hist=[byday[k] for k in sorted(byday)][-1100:]
    if sources and hist:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(hist,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'ultimo_dia':hist[-1] if hist else None,'historico':hist[-90:],
            'data_mais_recente':hist[-1]['data'] if hist else None,'fontes':[a for a,_ in sources],
            'status':'Base ONS geracao termica por motivo de despacho' if hist else 'Sem CSV termico validado',
            'avisos':issues[:5]}
