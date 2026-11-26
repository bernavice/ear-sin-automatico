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


# ---------------------------------------------------------------------------
# Leitura narrativa geométrica do IPDO: seções que atravessam páginas.
# Conserva a proveniência de página e nunca converte ausência de texto em zero.
# ---------------------------------------------------------------------------

_RE_FOOTER = re.compile(
    r'^(?:OPERADOR NACIONAL DO SISTEMA|ONS/CNOS|INFORMATIVO PRELIMINAR DA OPERA[CÇ][AÃ]O DO DIA|'
    r'Elaborado a partir de dados n[aã]o consistidos|Os dados consolidados constar[aã]o|'
    r'Pag(?:ina)?\.?\s*\d+\b|www\.ons\.org\.br)', re.I)


def _norm_pdf(t):
    t = unicodedata.normalize('NFKD', str(t))
    return re.sub(r'\s+', ' ', ''.join(c for c in t if not unicodedata.combining(c))).casefold().strip()


def _repair_pdf_letters(line):
    """Trata texto com tracking (espaçamento entre letras) sem tentar adivinhar
    onde terminam palavras quando o PDF perdeu essa separação.
    """
    s = re.sub(r'[\u200b\ufeff\u00ad]', '', str(line)).strip()
    s = s.replace('\u00a0', ' ')
    if len(s) > 10:
        groups = re.split(r'\s{2,}', s)
        rebuilt = []
        changed = 0
        for group in groups:
            letters = group.split()
            if len(letters) >= 2 and sum(len(w) == 1 for w in letters)/len(letters) >= .90:
                word = ''.join(letters)
                if len(word) > 5 and word[0] in 'AOÀOa o'.replace(' ', '') and word[1].islower():
                    word = word[0] + ' ' + word[1:]
                rebuilt.append(word)
                changed += 1
            else:
                rebuilt.append(group)
        if changed and (len(groups) > 1 or sum(len(w)==1 for w in s.split())/max(len(s.split()),1) > .75):
            s = ' '.join(rebuilt)
    return re.sub(r'\s+([,.;:!?])',r'\1',s).strip()


def _doc_pdf_lines(doc):
    """Ordena linhas textuais por geometria PyMuPDF e elimina cabeçalhos/rodapés.

    Usamos o dicionário de blocos, não a string total de texto: isso evita
    misturar colunas distintas e permite rastrear a página de cada ocorrência.
    """
    out=[]
    for page_i,page in enumerate(doc):
        lines=[]
        try:
            blocks=page.get_text('dict', sort=True).get('blocks', [])
        except (TypeError,ValueError,AttributeError):
            blocks=[]
        for block in blocks:
            if block.get('type',0) != 0:continue
            for line in block.get('lines', []):
                spans=line.get('spans',[])
                if not spans:continue
                parts=[]; prev_x=None
                for sp in sorted(spans,key=lambda e:e.get('bbox',[0])[0]):
                    txt=str(sp.get('text',''))
                    if not txt:continue
                    x0,x1=sp.get('bbox',(0,0,0,0))[0],sp.get('bbox',(0,0,0,0))[2]
                    gap=x0-prev_x if prev_x is not None else 0
                    if parts and gap > max(1.7,float(sp.get('size',10))*.21) and not parts[-1].endswith(' ') and not txt.startswith(' '):
                        parts.append(' ')
                    parts.append(txt)
                    prev_x=x1
                value=_repair_pdf_letters(''.join(parts))
                if value:
                    bbox=line.get('bbox',block.get('bbox',(0,0,0,0)))
                    lines.append((round(float(bbox[1]),2),round(float(bbox[0]),2),value))
        if not lines:
            lines=[(i,0,_repair_pdf_letters(s)) for i,s in enumerate(page.get_text('text',sort=True).splitlines()) if s.strip()]
        lines.sort(key=lambda x:(x[0],x[1]))
        for _,__,value in lines:
            if _RE_FOOTER.search(value):continue
            # Trechos secundários de rodapé em PDFs com o parágrafo dividido.
            n=_norm_pdf(value)
            if n.startswith(('exceto sabados, domingos e feriados',
                             'a partir das 14 horas, exceto',
                             'uso operacional interno. os dados consolidados')):
                continue
            out.append({'page':page_i+1,'text':value})
    return out


def _is_pdf_heading(line):
    t=_norm_pdf(line).strip(' *•-:')
    if re.match(r'^4\s*[-–.]\s*destaques da operacao',t):return 'secao4'
    if re.match(r'^5\s*[-–.]\s*gerac(?:oes|ao) termic',t):return 'secao5'
    if re.match(r'^6\s*[-–.]\s*destaques da geracao termica',t):return 'secao6'
    if re.match(r'^7\s*[-–.]\s*demandas maximas',t):return 'secao7'
    if re.match(r'^8\s*[-–.]\s*dados hidraulicos',t):return 'secao8'
    if re.match(r'^(?:\*\s*)?carga e producao de energia por submercado',t):return 'producao'
    if re.match(r'^(?:\*\s*)?transferencia de energia entre submercados',t):return 'transferencia'
    if re.match(r'^(?:\*\s*)?restricao de geracao renovavel',t):return 'restricao'
    if re.match(r'^(?:\*\s*)?intercambio internacional',t):return 'intercambio'
    if re.match(r'^(?:\*\s*)?ocorrencias',t):return 'ocorrencias'
    if re.match(r'^(?:\*\s*)?integracao de novas instalacoes',t):return 'integracao'
    if re.match(r'^(?:\*\s*)?informacoes adicionais',t):return 'adicionais'
    return None

_SUBMARKET = re.compile(r'^\s*\*?\s*submercado\s+(sul|sudeste\s*[/\-]\s*centro[\- ]oeste|nordeste|norte|se\s*/\s*co|ne|n)\s*:\s*$', re.I)


def _pdf_submarket(line):
    m=_SUBMARKET.match(_norm_pdf(line))
    if not m:return None
    s=m.group(1).replace(' ','')
    if s in ('sul',):return 'SUL'
    if s in ('sudeste/centro-oeste','se/co'):return 'SE/CO'
    if s in ('nordeste','ne'):return 'NE'
    if s in ('norte','n'):return 'N'
    return None


def _complete_pdf_text(text):
    value=re.sub(r'\s+',' ',text).strip('• *-\t')
    if not value or _norm_pdf(value).startswith('nada a relatar'):return False
    if not (16 <= len(value) <= 1150):return False
    words=re.findall(r'[A-Za-zÀ-ÿ]+',value)
    if len(words) < 4:return False
    if sum(len(t)==1 for t in words)/len(words)>.36:return False
    if len(words)>5 and sum(c.isupper() for c in value if c.isalpha())/max(1,sum(c.isalpha() for c in value))>.85:return False
    if not re.search(r'[.!?;]\s*$',value):return False
    return True


def _join_pdf_paragraphs(items):
    """Une quebras de linha do mesmo parágrafo e preserva frases curtas completas."""
    out=[];buffer='';pageno=0
    def flush():
        nonlocal buffer
        s=re.sub(r'\s+',' ',buffer).strip()
        if _complete_pdf_text(s):out.append({'texto':s,'pagina':pageno})
        buffer=''
    for row in items:
        line=row['text'].strip(' •*\t')
        if not line:continue
        if not buffer:pageno=row['page']
        if buffer.endswith('-') and line[:1].islower():buffer=buffer[:-1]+line
        else:buffer=(buffer+' '+line).strip()
        # Não separar depois de abreviaturas operativas (ex: UTEs S.A.).
        if re.search(r'[.!?;]\s*$',line) and not re.search(r'\b(?:S\.A\.|Sr\.|Dr\.)\s*$',line,re.I):flush()
        elif len(buffer)>1100:buffer=''
    if buffer:flush()
    return out


def _restriction_from_lines(sub,items):
    txt=' '.join(r['text'].strip() for r in items)
    txt=re.sub(r'\s+',' ',txt).strip()
    maximum=re.search(r'valor\s+m[aá]ximo\s*:\s*([\d.]+)\s*MW\b',txt,re.I)
    limit=re.search(r'valor\s+m[aá]ximo\s*:\s*(houve\s+apenas\s+limita[cç][aã]o|apenas\s+limita[cç][aã]o|sem\s+valor\s+num[eé]rico)',txt,re.I)
    motive=re.search(r'motivos?\s*:\s*(.*?)(?=(?:per[ií]odo|valor\s+m[aá]ximo)\s*:|$)',txt,re.I)
    period=re.search(r'per[ií]odo\s*:\s*(.*?)(?=(?:motivos?|valor\s+m[aá]ximo)\s*:|$)',txt,re.I)
    if maximum:
        number=int(maximum.group(1).replace('.',''))
        if not 0<=number<=100000:return None
        situacao='valor máximo identificado'
    elif limit:
        number=None;situacao='limitação sem valor máximo numérico informado'
    else:
        number=None;situacao='valor máximo não identificado'
    mm=motive.group(1).strip(' .;') if motive else None
    pp=period.group(1).strip(' .;') if period else None
    if not (maximum or limit or pp or mm):return None
    return {'subsistema':sub,'maximo_mw':number,'maximo_texto':limit.group(1) if limit else None,
            'periodo':pp,'motivo':mm,'situacao':situacao,'pagina':items[0]['page'] if items else None}


def parse_ipdo_narratives(doc):
    """Reconstrói as narrativas da seção 4 mesmo quando atravessam páginas.

    Destaca o texto somente com começo e fim verificáveis; não trata 'Nada a
    relatar' como falha de extração, nem 'houve apenas limitação' como zero MW.
    """
    source=_doc_pdf_lines(doc)
    section=[];start=False
    for line in source:
        label=_is_pdf_heading(line['text'])
        if not start:
            if label=='secao4':start=True
            continue
        if label=='secao5':break
        section.append({**line,'section':label})
    if not start:
        return {'destaques':[],'restricoes':[],'ocorrencias':[],
                'status_destaques':'Seção 4 não localizada no PDF; verifique o documento original.',
                'status_restricoes':'Seção de restrições não localizada; não é ausência de restrição.',
                'diagnostico_texto':{'secao4_encontrada':False,'paginas':[]}}

    mode=None;sub=None;groups=[];active=[];restrs=[];current_restr=None;restr_lines=[]
    none_marked=defaultdict(bool)
    sections_present=set()
    def flush_highlights():
        nonlocal active
        if mode in ('producao','transferencia','ocorrencias','integracao','adicionais','intercambio') and active:
            paragraphs=_join_pdf_paragraphs(active)
            for p in paragraphs:
                groups.append({'categoria':mode,'subsistema':sub if mode=='producao' else None,**p})
        active=[]
    def flush_restrict():
        nonlocal restr_lines
        if mode=='restricao' and current_restr and restr_lines:
            item=_restriction_from_lines(current_restr,restr_lines)
            if item:restrs.append(item)
        restr_lines=[]
    for row in section:
        label=row['section']
        if label and label not in ('secao4','secao5'):
            flush_highlights();flush_restrict()
            mode=label;sub=None;current_restr=None;sections_present.add(label)
            continue
        local_sub=_pdf_submarket(row['text'])
        if local_sub:
            flush_highlights();flush_restrict()
            sub=local_sub
            if mode=='restricao':current_restr=local_sub
            continue
        line=row['text'].strip()
        if not line:continue
        if _norm_pdf(line).startswith('nada a relatar'):
            none_marked[mode]=True
            flush_highlights()
            continue
        if mode=='restricao':
            if current_restr:restr_lines.append(row)
        elif mode in ('producao','transferencia','ocorrencias','integracao','adicionais','intercambio'):
            active.append(row)
    flush_highlights();flush_restrict()
    seen=set();highlights=[];occ=[]
    for item in groups:
        text=item['texto'];cat=item['categoria'];sub=item['subsistema']
        if cat in ('producao','transferencia'):
            label=(sub+' — ' if sub else 'Intercâmbios — ' if cat=='transferencia' else '')+text
            norm=_norm_pdf(label)
            if norm not in seen:seen.add(norm);highlights.append(label)
        elif cat in ('ocorrencias','integracao','adicionais'):
            occ.append({'categoria':cat,'texto':text,'pagina':item['pagina']})
    unique=[];seen_r=set()
    for r in restrs:
        key=(r['subsistema'],r['maximo_mw'],r.get('periodo'),r.get('motivo'))
        if key not in seen_r:seen_r.add(key);unique.append(r)
    if highlights:st_h=f'{len(highlights)} destaques extraídos da seção 4 com contexto de submercado.'
    elif sections_present.intersection({'producao','transferencia'}) and none_marked['producao'] and none_marked['transferencia']:
        st_h='O IPDO informa Nada a relatar nos destaques e nas transferências.'
    else:st_h='Não foi possível recuperar destaques completos; consulte a seção 4 do PDF.'
    if unique:st_r=f'{len(unique)} registros de restrição identificados na seção 4 (inclui limitações sem MW).'
    elif 'restricao' in sections_present and none_marked['restricao']:
        st_r='O IPDO informa Nada a relatar em restrições de renováveis.'
    else:st_r='Restrições não recuperadas de forma verificável; não representa zero.'
    return {'destaques':highlights[:30], 'restricoes':unique,'ocorrencias':occ[:20],
            'status_destaques':st_h,'status_restricoes':st_r,
            'diagnostico_texto':{'secao4_encontrada':True,
                                  'paginas':sorted(set(x['page'] for x in section)),
                                  'subsecoes_detectadas':sorted(sections_present),
                                  'destaques_extraidos':len(highlights),
                                  'restricoes_extraidas':len(unique),
                                  'outras_ocorrencias_extraidas':len(occ)}}


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


def _pt_natural_number(x):
    try:
        return float(str(x).replace('.','').replace(',','.'))
    except (TypeError,ValueError):return None


def _thermal_row(items):
    """Interpreta linha validada da tabela 6.1 (Progr/Verif/Dif/Motivos).

    Em caso de ambiguidade geométrica, não gera diferença fictícia.
    """
    items=[re.sub(r'\s+',' ',str(x)).strip() for x in items]
    if len(items)!=5:return None
    name,prog,verif,diff,reason=items
    if not (3<=len(name)<70 and re.search('[A-Za-zÀ-ÿ]',name)):return None
    if _norm_pdf(name).startswith(('total','usinas','progr','verif')):return None
    if not (2<=len(reason)<110 and re.search('[A-Za-zÀ-ÿ]',reason)):return None
    nums=[_pt_natural_number(x) for x in (prog,verif,diff)]
    if any(n is None for n in nums):return None
    prog,verif,diff=nums
    if not (0<=prog<10000 and -100<=verif<10000 and abs(prog-verif+diff)<2):return None
    return {'usina':name,'programado_mwmed':prog,'verificado_mwmed':verif,
            'diferenca_mwmed':diff,'motivo':reason,'justificativa':'Variação diária (verificado − programado): '
             +f'{diff:+,.0f}'.replace(',','.')+' MWmed. Motivo informado pelo ONS: '+reason+'.'}


def _page_pdf_lines(page):
    # Reuse the canonical extraction without requiring a Document slice.
    return _doc_pdf_lines([page])


def _thermal_table_61(doc, first_page, last_page):
    """Lê a tabela de diferenças por texto e opcionalmente com find_tables()."""
    result=[]
    # Primeira tentativa: reconhecimento tabular do PyMuPDF, quando há grade.
    for page_i in range(first_page-1,min(last_page,len(doc))):
        page=doc[page_i]
        try: tables=page.find_tables().tables
        except Exception:tables=[]
        for table in tables:
            try: rows=table.extract()
            except Exception:continue
            header=' '.join(str(x or '') for r in rows[:2] for x in r)
            if not ('usin' in _norm_pdf(header) and ('motivo' in _norm_pdf(header) or 'difer' in _norm_pdf(header))):continue
            for r in rows[1:]:
                if not r:continue
                values=[re.sub(r'\s+',' ',str(x or '')).strip() for x in r]
                # A tabela 6.1 pode vir com uma coluna extra ao lado do nome.
                if len(values)>=5:
                    entry=_thermal_row(values[:5])
                    if entry:result.append(entry)
    # Fallback para extração linear: texto da mesma linha ou cinco linhas
    # consecutivas (nome, programado, verificado, diferença e motivo).
    for page_i in range(first_page-1,min(last_page,len(doc))):
        texts=[x['text'] for x in _page_pdf_lines(doc[page_i])]
        for i,line in enumerate(texts):
            clean=re.sub(r'\s+',' ',line).strip()
            # Procura três números consecutivos: nome + prog + verif + dif + motivo.
            m=re.match(r'^(.*?)\s+(-?[\d.,]+)\s+(-?[\d.,]+)\s+(-?[\d.,]+)\s+(.+)$',clean)
            if m:
                entry=_thermal_row(list(m.groups()))
                if entry:result.append(entry)
            if i+4<len(texts):
                entry=_thermal_row(texts[i:i+5])
                if entry:result.append(entry)
    unique={}
    for r in result:
        k=(_norm_pdf(r['usina']),r['programado_mwmed'],r['verificado_mwmed'])
        unique.setdefault(k,r)
    return list(unique.values())


def parse_termicas_6(doc):
    """Seção 6: motivos e diferenças diárias efetivas; notas narrativas por UTE.

    A tabela 6.1, quando verificável, fornece o ranking verdadeiro por MWmed.
    Os parágrafos adicionais são preservados separadamente como justificativas.
    """
    source=_doc_pdf_lines(doc)
    start=next((i for i,x in enumerate(source) if _is_pdf_heading(x['text'])=='secao6'),None)
    if start is None:
        return {'destaques':[],'top3':[],'tabela_diferencas':[],
                'status':'seção 6 não localizada'}
    end=next((i for i in range(start+1,len(source)) if _is_pdf_heading(source[i]['text'])=='secao7'),len(source))
    section=source[start:end]
    first=section[0]['page'];last=section[-1]['page']
    table=_thermal_table_61(doc,first,last)
    # Extração de notas adicionais separadas da tabela 6.1.
    after_add=False;notes=[]
    for r in section:
        t=_norm_pdf(r['text']).strip('* ')
        if t.startswith('informacoes adicionais'):
            after_add=True
            continue
        if after_add:notes.append(r)
    paragraphs=_join_pdf_paragraphs(notes)
    narratives=[]
    for p in paragraphs:
        text=p['texto']
        match=re.search(r'\b(?:A|Na|Da|As|Pela)\s+UTEs?\s+(.{2,80}?)(?=\s*\(|\s*,|\s+(?:est[aã]o|est[aá]|foi|foram|ficou|teve|gerou|permanece|opera|com\b))',text,re.I)
        if not match:continue
        usina=match.group(1).strip(' .,')
        if len(usina)>65:continue
        narratives.append({'usina':usina,'justificativa':text,'pagina':p['pagina']})
    seen=set();narratives_unique=[]
    for p in narratives:
        key=_norm_pdf(p['usina'])
        if key not in seen:seen.add(key);narratives_unique.append(p)
    ranked=sorted((x for x in table if x['diferenca_mwmed']!=0),key=lambda x:abs(x['diferenca_mwmed']),reverse=True)
    top3=ranked[:3] if ranked else narratives_unique[:3]
    status=(f'{len(table)} diferenças da tabela 6.1 validadas; top3 por desvio absoluto em MWmed.'
            if table else 'tabela 6.1 não validada; lista apenas justificativas narrativas, sem ranking')
    return {'destaques':narratives_unique[:30],'top3':top3,'tabela_diferencas':table,
            'status':status,'paginas':sorted(set(x['page'] for x in section))}


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
    # Quando o recorde do IPDO já é o pico do próprio dia, não temos o máximo
    # PRECEDENTE nessa mesma linha. Não afirmar quebra de recorde por igualdade.
    exceeded=True if actual>record else (False if actual<record else None)
    return {'verificada_mw':actual,'hora':match.group(2)+':'+match.group(3),
            'recorde_mw':record,'recorde_data':recday,'novo_recorde':exceeded,
            'status':'A própria linha do IPDO pode conter recorde atualizado; igualdade exige série anterior para comprovar superação'}


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

def curtailment_urls(reference, n_meses=25):
    """Lista até 25 competências (2 anos completos + mês corrente), por fonte.
    Arquivos históricos serão importados progressivamente e persistidos no GitHub.
    """
    base='https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/'
    month=reference.replace(day=1)
    for _ in range(n_meses):
        ym=month.strftime('%Y_%m')
        for fonte,pasta,prefixo in (
            ('Eólica','restricao_coff_eolica_tm','RESTRICAO_COFF_EOLICA_'),
            ('Solar','restricao_coff_fotovoltaica_tm','RESTRICAO_COFF_FOTOVOLTAICA_')):
            yield month,fonte,base+pasta+'/'+prefixo+ym+'.csv'
        month=(month-dt.timedelta(days=1)).replace(day=1)


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
    coverage_file=raw/'curtailment_competencias.json'
    try:
        coverage=json.loads(coverage_file.read_text(encoding='utf-8'))
        if not isinstance(coverage,dict):coverage={}
    except (OSError,ValueError):coverage={}
    # Também aceita CSVs oficiais arquivados localmente, sem depender da rede.
    sources=[]
    for local in sorted((raw/'curtailment').glob('*.csv')) if (raw/'curtailment').exists() else []:
        source='Solar' if 'foto' in local.stem.lower() or 'solar' in local.stem.lower() else 'Eólica' if 'eol' in local.stem.lower() else None
        if source:sources.append((source,local.read_bytes(),local.name))
    if online:
        session=requests.Session()
        session.headers.update({'User-Agent':'Painel-IPDO-ONS/2.0 (consulta-publica)','Accept':'text/csv,*/*'})
        candidates=list(curtailment_urls(reference))
        recent=candidates[:4]  # mês corrente e anterior, sempre sujeitos a revisões
        pending=[(day,fonte,url) for day,fonte,url in candidates[4:]
                 if (day.strftime('%Y_%m')+'_'+fonte) not in coverage]
        # Progresso histórico sem sobrecarregar uma única execução diária.
        # Configure IPDO_CURTAILMENT_BACKFILL=50 para importar os 24+ meses de uma vez.
        import os
        try:limit=max(0,min(52,int(os.getenv('IPDO_CURTAILMENT_BACKFILL','10'))))
        except ValueError:limit=10
        # Prioridade para o mês mais antigo; próxima execução prossegue automaticamente.
        chosen=recent+(pending[-limit:] if limit else [])
        for day,fonte,url in chosen:
            attempted.append(url)
            try:
                resp=session.get(url,timeout=(5,18))
                if resp.status_code==200 and 40<len(resp.content)<65_000_000:
                    sources.append((fonte,resp.content,url))
                    # Registraremos competência só após interpretar o CSV sem erro.
                else:log.info('Curtailment %s (%s): HTTP %s',fonte,day.strftime('%Y_%m'),resp.status_code)
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
            match=re.search(r'(20\d\d_\d\d)',url)
            if match:coverage[match.group(1)+'_'+fonte]=url
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
        hist_file.parent.mkdir(parents=True,exist_ok=True)
        hist_file.write_text(json.dumps(recent,ensure_ascii=False,indent=2),encoding='utf-8')
        coverage_file.write_text(json.dumps(coverage,ensure_ascii=False,indent=2),encoding='utf-8')
    first=min((r['data'] for r in recent),default=None)
    count_covered=len({r['data'][:7] for r in recent})
    total_competencias=25
    faltantes=max(0,total_competencias-count_covered)
    return {'series':recent,'cobertura_inicio':first,'meses_com_dados':count_covered,
            'meses_meta':total_competencias,'status_cobertura':('histórico em preenchimento: '+str(faltantes)+' meses ainda sem dados' if faltantes else 'cobertura mensal de 2 anos ou mais'),
            'data_mais_recente':max((r['data'] for r in recent),default=None),
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
    series=[]
    max_so_far=None
    max_day=None
    for date,val in sorted(bydate.items()):
        is_record=max_so_far is not None and val>max_so_far
        series.append({'data':date,'mw':val,'novo_recorde':is_record,
                       'referencia_anterior_mw':max_so_far,
                       'referencia_anterior_data':max_day})
        if max_so_far is None or val>max_so_far:
            max_so_far=val
            max_day=date
    if not series:
        return {'dados':None,'historico':[], 'fontes':refs,'status':'Base SIN da Demanda Máxima ONS indisponível ou sem linha SIN.'}
    today=series[-1]
    previous=today.get('referencia_anterior_mw')
    new=today.get('novo_recorde') if previous is not None else None
    return {'dados':{
              'verificada_mw':today['mw'],'data':today['data'],
              'recorde_mw':previous,'recorde_anterior_data':today.get('referencia_anterior_data'),
              'recorde_vigente_mw':max(previous or 0,today['mw']),
              'novo_recorde':new,'hora':'',
              'status':f'ONS Dados Abertos; referência: máximo dos dias anteriores disponíveis desde {series[0]["data"]}, não necessariamente recorde histórico absoluto.'},
            'historico':series[-730:],'fontes':refs,'status':'dados oficiais tabulares (sujeitos a revisão)'}



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
 ('Restrição elétrica','eletrica','#db8445'),
 ('Substituição de mérito','gsub','#6a79c9'),
 ('Geração fora de mérito','gfom','#c15c81'),
 ('Energia de reposição','reposicao','#a7b84e'),
 ('Garantia energética','garantia','#8d64c0'),
 ('Exportação','exportacao','#b7833f'),
 ('Reserva de potência','reservapotencia','#56adc0'),
 ('Unit commitment','unitcommitment','#5b84d7'),
 ('Não classificada','outros','#8996a5')]


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
        # ONS: parcelas exclusivas da geração VERIFICADA em base horária.
        # Ordem de mérito acima da inflexibilidade evita dupla contagem.
        merit=val('val_verifordemdemeritoacimadainflex') if 'val_verifordemdemeritoacimadainflex' in cols else max(0.,val('val_verifordemmerito')-val('val_verifinflexembutmerito'))
        inflex=val('val_verifinflexibilidade') if 'val_verifinflexibilidade' in cols else val('val_verifinflexpura')+val('val_verifinflexembutmerito')
        reasons={
           'merito':merit, 'inflexibilidade':inflex,
           'eletrica':val('val_verifrazaoeletrica'),
           'gsub':val('val_verifgsub'),
           'gfom':val('val_verifgfom'),
           'reposicao':val('val_verifreposicaoperdas'),
           'garantia':val('val_verifgarantiaenergetica'),
           'exportacao':val('val_verifexportacao'),
           'reservapotencia':val('val_verifreservapotencia'),
           'unitcommitment':val('val_verifunitcommitment')
        }
        known=sum(reasons.values())
        if known>actual*1.025+2:
            # Não publicar categorias com soma superior ao total verificado.
            reasons={k:0. for k in reasons}
            known=0.
        reasons['outros']=max(0.,actual-known)
        k=(date[:10],date[11:13],sub)
        h=hourly[k]
        h['total']+=actual
        for name,value in reasons.items():h[name]+=value
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
        if len(daily)!=4 or min(x['horas'] for x in daily)<20:continue
        reason_keys=[k for _,k,_ in THERMAL_MOTIVES]
        sin={key:round(sum(x.get(key,0) for x in daily),3) for key in ['total']+reason_keys}
        top=sorted([{'usina':p,'subsistema':sub,'mwmed':round(v/max(1,len(hours[(d,sub)])),2)}
                    for (d,sub,p),v in byplants.items() if d==day],key=lambda x:-x['mwmed'])[:3]
        out.append({'data':day,'submercados':daily,'sin':{'subsistema':'SIN',**sin},
                    'motivos':[{'nome':nome,'campo':campo,'cor':cor} for nome,campo,cor in THERMAL_MOTIVES],
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
