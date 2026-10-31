from __future__ import annotations

import argparse
import json
import re
import unicodedata
import warnings
from datetime import datetime
from io import BytesIO
from html.parser import HTMLParser
from urllib.parse import urljoin
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs_version

RAIZ = Path(__file__).resolve().parent
DADOS = RAIZ / 'dados'
GRAFICOS = RAIZ / 'graficos'
PAGINA_PLD = GRAFICOS / 'pld.html'
LONGO = DADOS / 'pld_historico_long.csv'
BRUTO = DADOS / 'pld_historico_bruto.csv'
CANDIDATOS_BRUTO = [
    BRUTO,
    RAIZ / 'csv_historico_pld.csv',
    RAIZ / 'csv_historico_pld(1).csv',
    Path('/mnt/data/csv_historico_pld.csv'),
    Path('/mnt/data/csv_historico_pld(1).csv'),
]
FONTE = 'Century Gothic, CenturyGothic, Arial, sans-serif'
COR_VALE = '#007E80'
CORES = {'SE/CO': '#2F66E0', 'SUL': '#D97706', 'NE': '#DC2626', 'N': '#7EC0EE'}
ORDEM = ['SE/CO', 'SUL', 'NE', 'N']
URL_DATASET = 'https://dadosabertos.ccee.org.br/dataset/pld_horario'
URL_API = 'https://dadosabertos.ccee.org.br/api/3/action/package_show'
# URL do recurso 2026 verificada na página pública da CCEE em 31/10/2026.
# Pode expirar; é apenas a terceira alternativa, depois de API e HTML.
URLS_DIRETAS_CONHECIDAS = {2026: 'https://pda-download.ccee.org.br/6A5wq97KTCWv_bvs3CqsQQ/content'}
MESES_PT = {1:'jan',2:'fev',3:'mar',4:'abr',5:'mai',6:'jun',7:'jul',8:'ago',9:'set',10:'out',11:'nov',12:'dez'}


def _sem_acento(s: str) -> str:
    s = unicodedata.normalize('NFKD', str(s))
    return ''.join(c for c in s if not unicodedata.combining(c)).strip().lower()


def _padronizar_submercado(s: str) -> str:
    x = _sem_acento(s)
    mapa = {
        'sudeste': 'SE/CO', 'sudeste/centro-oeste': 'SE/CO', 'sudeste-centro-oeste': 'SE/CO',
        'sudeste centro-oeste': 'SE/CO', 'sudeste/centrooeste': 'SE/CO', 'sudestecentrooeste': 'SE/CO',
        'se/co': 'SE/CO', 'se': 'SE/CO', 'centro-oeste': 'SE/CO', 'centrooeste': 'SE/CO',
        'sul': 'SUL', 'nordeste': 'NE', 'ne': 'NE', 'norte': 'N', 'n': 'N'
    }
    return mapa.get(x, str(s).upper())


def _eh_coluna_data(col: str) -> bool:
    txt = str(col).strip()
    return bool(re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', txt))


def _to_num(x):
    if pd.isna(x):
        return pd.NA
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip()
    if s == '':
        return pd.NA
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except Exception:
        return pd.NA


def ler_xml_excel(caminho: Path) -> pd.DataFrame:
    root = ET.fromstring(caminho.read_bytes())
    ns = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}
    rows = root.findall('.//ss:Worksheet/ss:Table/ss:Row', ns)
    if not rows:
        raise ValueError('XML sem tabela SpreadsheetML reconhecível')
    tabela = []
    for row in rows:
        linha = []
        for cell in row.findall('ss:Cell', ns):
            idx = cell.get('{urn:schemas-microsoft-com:office:spreadsheet}Index')
            if idx:
                while len(linha) < int(idx) - 1:
                    linha.append('')
            data = cell.find('ss:Data', ns)
            linha.append(data.text if data is not None else '')
        tabela.append(linha)
    cab = tabela[0]
    largura = max(len(r) for r in tabela)
    cab = cab + [f'col_{i}' for i in range(len(cab), largura)]
    norm = [r + [''] * (largura - len(r)) for r in tabela[1:]]
    return pd.DataFrame(norm, columns=cab)


def ler_bruto(caminho: Path) -> pd.DataFrame:
    ext = caminho.suffix.lower()
    if ext == '.xml':
        try:
            return ler_xml_excel(caminho)
        except Exception:
            return pd.read_xml(caminho)
    try:
        return pd.read_csv(caminho, sep=None, engine='python')
    except Exception:
        return pd.read_csv(caminho, sep=';', decimal=',')


def normalizar_bruto(df: pd.DataFrame) -> pd.DataFrame:
    cols_norm = {_sem_acento(c): c for c in df.columns}
    if 'hora' in cols_norm and 'submercado' in cols_norm:
        col_hora = cols_norm['hora']
        col_sub = cols_norm['submercado']
        datas = [c for c in df.columns if _eh_coluna_data(c)]
        if datas:
            base = df[[col_hora, col_sub] + datas].copy()
            longo = base.melt(id_vars=[col_hora, col_sub], value_vars=datas,
                              var_name='data', value_name='pld')
            longo = longo.rename(columns={col_hora: 'hora', col_sub: 'submercado'})
            longo['data'] = pd.to_datetime(longo['data'], dayfirst=True, errors='coerce')
            longo['hora'] = pd.to_numeric(longo['hora'], errors='coerce').astype('Int64')
            longo['submercado'] = longo['submercado'].map(_padronizar_submercado)
            longo['pld'] = longo['pld'].map(_to_num)
            longo = longo.dropna(subset=['data', 'hora', 'submercado', 'pld']).copy()
            longo['hora'] = longo['hora'].astype(int)
            return longo[['data', 'hora', 'submercado', 'pld']].sort_values(['data', 'hora', 'submercado'])

    rev = {_sem_acento(k): k for k in df.columns}
    poss_data = next((rev[k] for k in rev if k in {'data', 'dt', 'date', 'dia'}), None)
    poss_hora = next((rev[k] for k in rev if k in {'hora', 'hour'}), None)
    poss_sub = next((rev[k] for k in rev if k in {'submercado', 'submercado de preco', 'subsistema'}), None)
    poss_valor = next((rev[k] for k in rev if k in {'pld', 'valor', 'preco', 'preco medio', 'preco horario'}), None)
    if poss_data and poss_hora and poss_sub and poss_valor:
        out = df[[poss_data, poss_hora, poss_sub, poss_valor]].copy()
        out.columns = ['data', 'hora', 'submercado', 'pld']
        out['data'] = pd.to_datetime(out['data'], dayfirst=True, errors='coerce')
        out['hora'] = pd.to_numeric(out['hora'], errors='coerce').astype('Int64')
        out['submercado'] = out['submercado'].map(_padronizar_submercado)
        out['pld'] = out['pld'].map(_to_num)
        out = out.dropna(subset=['data', 'hora', 'submercado', 'pld']).copy()
        out['hora'] = out['hora'].astype(int)
        return out[['data', 'hora', 'submercado', 'pld']].sort_values(['data', 'hora', 'submercado'])
    raise ValueError('Formato do histórico PLD não reconhecido')


def carregar_historico_base(caminho: Path | None = None) -> pd.DataFrame:
    candidatos = [caminho] if caminho else []
    candidatos += CANDIDATOS_BRUTO
    for cand in candidatos:
        if cand and Path(cand).exists():
            hist = normalizar_bruto(ler_bruto(Path(cand)))
            if not hist.empty:
                return hist
    raise FileNotFoundError('Não encontrei o histórico-base do PLD')


def carregar_base_consolidada(caminho: Path = LONGO) -> pd.DataFrame:
    if caminho.exists():
        df = pd.read_csv(caminho, sep=';', decimal=',', encoding='utf-8-sig')
        df['data'] = pd.to_datetime(df['data'], errors='coerce')
        df['hora'] = pd.to_numeric(df['hora'], errors='coerce').astype('Int64')
        df['pld'] = pd.to_numeric(df['pld'], errors='coerce')
        df['submercado'] = df['submercado'].map(_padronizar_submercado)
        df = df.dropna(subset=['data', 'hora', 'pld', 'submercado']).copy()
        df['hora'] = df['hora'].astype(int)
        return df.sort_values(['data', 'hora', 'submercado']).drop_duplicates(['data', 'hora', 'submercado'], keep='last')
    return carregar_historico_base()


def normalizar_csv_ccee(df: pd.DataFrame, ano: int) -> pd.DataFrame:
    """Reconhece a data completa ou DIA numérico com MES_REFERENCIA (AAAAMM)."""
    norm = {_sem_acento(c).replace(' ', '_'): c for c in df.columns}
    def pick(opcoes):
        return next((norm[k] for k in opcoes if k in norm), None)

    c_data = pick(['dia', 'data', 'dt_referencia', 'data_referencia'])
    c_mes = pick(['mes_referencia', 'mes_ref'])
    c_hora = pick(['hora', 'hora_do_dia', 'hora_referencia'])
    c_sub = pick(['submercado', 'sub_mercado', 'subsistema'])
    c_val = pick(['pld_hora', 'pld', 'preco', 'valor', 'preco_hora'])
    if not all([c_data, c_hora, c_sub, c_val]):
        raise ValueError(f'Colunas do CSV CCEE não reconhecidas: {list(df.columns)}')

    out = df[[c_data, c_hora, c_sub, c_val]].copy()
    out.columns = ['dia_original', 'hora', 'submercado', 'pld']
    raw_dia = out['dia_original'].astype('string').str.strip()
    # Formato com dia do mês ("30") ou dia/mês/ano ("30/10/2026").
    data = pd.to_datetime(raw_dia, format='%d/%m/%Y', errors='coerce')
    sem_data = data.isna()
    if sem_data.any():
        data.loc[sem_data] = pd.to_datetime(raw_dia.loc[sem_data], format='%Y-%m-%d', errors='coerce')
    sem_data = data.isna()
    if sem_data.any() and c_mes:
        mes = df.loc[sem_data, c_mes].astype('string').str.replace(r'\D', '', regex=True).str.zfill(6)
        dia = pd.to_numeric(raw_dia.loc[sem_data], errors='coerce').astype('Int64').astype('string').str.zfill(2)
        data.loc[sem_data] = pd.to_datetime(mes + dia, format='%Y%m%d', errors='coerce')
    out['data'] = data
    out['hora'] = pd.to_numeric(out['hora'], errors='coerce')
    out['submercado'] = out['submercado'].map(_padronizar_submercado)
    out['pld'] = out['pld'].map(_to_num)
    out = out.dropna(subset=['data', 'hora', 'submercado', 'pld']).copy()
    out = out[out['submercado'].isin(ORDEM) & (out['data'].dt.year == ano)].copy()
    if out.empty:
        raise ValueError(f'CSV {ano} sem registros reconhecidos; verifique formato DIA/MES_REFERENCIA')
    out['hora'] = out['hora'].astype(int)
    if out['hora'].min() == 1 and out['hora'].max() == 24:
        out['hora'] = out['hora'] - 1
    if not out['hora'].between(0, 23).all():
        raise ValueError('Horas fora do intervalo 0–23')
    out['pld'] = out['pld'].astype(float)
    out = out[['data', 'hora', 'submercado', 'pld']]
    if out.duplicated(['data', 'hora', 'submercado']).any():
        raise ValueError('CSV CCEE contém horários duplicados')
    return out.sort_values(['data', 'hora', 'submercado']).reset_index(drop=True)


def recursos_ccee() -> dict[int, dict]:
    import requests
    s = requests.Session()
    s.headers.update({'User-Agent': 'Painel-Vale-PLD/1.0'})
    r = s.get(URL_API, params={'id': 'pld_horario'}, timeout=60)
    r.raise_for_status()
    meta = r.json()
    if not meta.get('success'):
        raise RuntimeError('API da CCEE retornou success=false')
    out = {}
    for rec in meta['result'].get('resources', []):
        nome = rec.get('name', '')
        m = re.fullmatch(r'pld_horario_(\d{4})', nome.strip().lower())
        if m:
            out[int(m.group(1))] = rec
    return out


class ExtratorLinksCCEE(HTMLParser):
    """Extrai links do download anual com a biblioteca padrão, sem lxml."""
    def __init__(self):
        super().__init__()
        self.ano_atual = None
        self.recursos = {}
        self.links_sem_ano = []
        self.anos_encontrados = []

    def handle_data(self, data):
        achou = re.search(r'pld_horario_(\d{4})', data.strip(), re.I)
        if achou:
            self.ano_atual = int(achou.group(1))
            if self.ano_atual not in self.anos_encontrados:
                self.anos_encontrados.append(self.ano_atual)

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        attr = dict(attrs)
        href = attr.get('href', '')
        if 'pda-download.ccee.org.br/' not in href and '/download/' not in href:
            return
        href = urljoin(URL_DATASET, href)
        if self.ano_atual:
            self.recursos[self.ano_atual] = {'name': f'pld_horario_{self.ano_atual}', 'url': href}
        else:
            self.links_sem_ano.append(href)


def extrair_recursos_html(html: str) -> dict[int, dict]:
    parser = ExtratorLinksCCEE()
    parser.feed(html)
    # Em alguns temas CKAN, os títulos aparecem antes da seção de downloads.
    if parser.links_sem_ano and len(parser.links_sem_ano) == len(parser.anos_encontrados):
        for ano, url in zip(parser.anos_encontrados, parser.links_sem_ano):
            parser.recursos.setdefault(ano, {'name': f'pld_horario_{ano}', 'url': url})
    return parser.recursos


def recursos_ccee_html() -> dict[int, dict]:
    """Fallback independente de lxml. Não tenta disfarçar bloqueios 403."""
    import requests
    s = requests.Session()
    s.headers.update({'User-Agent': 'Painel-Vale-PLD/1.0'})
    r = s.get(URL_DATASET, timeout=60)
    r.raise_for_status()
    resposta = extrair_recursos_html(r.text)
    if not resposta:
        raise RuntimeError('Página não expôs links diretos para os recursos anuais')
    return resposta


def baixar_csv_ano(recurso: dict, ano: int) -> pd.DataFrame:
    import requests
    s = requests.Session()
    s.headers.update({'User-Agent': 'Painel-Vale-PLD/1.0'})
    url = recurso.get('download_url') or recurso.get('url')
    if not url:
        raise ValueError('Recurso CCEE sem URL de download')
    r = s.get(url, timeout=120)
    r.raise_for_status()
    raw = r.content
    if not raw or len(raw) < 1000:
        raise ValueError('Download do CSV anual parece vazio ou truncado')
    head = raw[:12000].decode('utf-8-sig', errors='replace')
    primeira = head.splitlines()[0] if head.splitlines() else ''
    sep = ';' if primeira.count(';') >= primeira.count(',') else ','
    try:
        df = pd.read_csv(BytesIO(raw), sep=sep, dtype=str, encoding='utf-8-sig', low_memory=False)
    except UnicodeDecodeError:
        df = pd.read_csv(BytesIO(raw), sep=sep, dtype=str, encoding='latin1', low_memory=False)
    return normalizar_csv_ccee(df, ano)


def carregar_importacoes_manuais() -> list[pd.DataFrame]:
    """Permite alimentar a base pelo próprio GitHub quando a CCEE bloqueia o servidor."""
    partes = []
    for arquivo in sorted(DADOS.glob('pld_importacao_*.csv')):
        ano_match = re.search(r'(20\d{2})', arquivo.stem)
        if not ano_match:
            print(f'PLD: importação {arquivo.name} ignorada (ano ausente do nome).')
            continue
        ano = int(ano_match.group(1))
        try:
            raw = arquivo.read_bytes()
            primeiro = raw[:4096].decode('latin1', errors='replace').splitlines()[0]
            sep = ';' if primeiro.count(';') >= primeiro.count(',') else ','
            df = pd.read_csv(BytesIO(raw), sep=sep, encoding='latin1', dtype=str)
            df = normalizar_csv_ccee(df, ano)
            partes.append(df)
            print(f'PLD: importação manual {ano}: até {df.data.max().date()} ({len(df)} preços).')
        except Exception as exc:
            print(f'::warning title=PLD arquivo manual inválido::{arquivo.name} ({exc})')
    return partes


def atualizar_com_online(historico: pd.DataFrame, usar_online: bool) -> pd.DataFrame:
    import requests
    historico = historico.copy()
    partes = carregar_importacoes_manuais()
    if not usar_online:
        print('PLD: modo offline; apenas histórico e arquivos manuais.')
    else:
        recursos = {}
        try:
            recursos.update(recursos_ccee())
            print('PLD: API oficial da CCEE respondeu.')
        except Exception as exc:
            print(f'::warning title=CCEE API bloqueada::Falha no endpoint CKAN: {exc}')

        if not recursos:
            try:
                recursos.update(recursos_ccee_html())
                print('PLD: links anuais obtidos diretamente da página CCEE.')
            except Exception as exc:
                print(f'::warning title=CCEE página indisponível::Falha no HTML público: {exc}')

        hoje = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
        ano_atual = hoje.year
        # Somente ano corrente e ano anterior no começo do novo ano.
        anos = [ano_atual - 1, ano_atual] if hoje.month <= 2 else [ano_atual]
        for ano in anos:
            urls = []
            if ano in recursos:
                url = recursos[ano].get('download_url') or recursos[ano].get('url')
                if url:
                    urls.append(url)
            if ano in URLS_DIRETAS_CONHECIDAS and URLS_DIRETAS_CONHECIDAS[ano] not in urls:
                urls.append(URLS_DIRETAS_CONHECIDAS[ano])

            for url in urls:
                try:
                    df = baixar_csv_ano({'url': url}, ano)
                    partes.append(df)
                    print(f'CCEE {ano}: arquivo direto lido, {len(df):,} registros até {df.data.max().date()}.')
                    break
                except requests.HTTPError as exc:
                    print(f'::warning title=PLD download bloqueado::{ano}: {exc}')
                except Exception as exc:
                    print(f'::warning title=PLD download falhou::{ano}: {exc}')
            if not urls:
                print(f'::warning title=CCEE {ano}::Nenhum link anual recuperado para {ano}.')

    if partes:
        antigo = historico['data'].max() if len(historico) else pd.NaT
        # Mescla por chave, preservando dados já existentes e possíveis correções novas.
        historico = pd.concat([historico, *partes], ignore_index=True)
        historico = historico.sort_values(['data', 'hora', 'submercado']).drop_duplicates(
            ['data', 'hora', 'submercado'], keep='last')
        print('PLD: último dado antes:', antigo, '| depois:', historico['data'].max())
    elif usar_online:
        print('::warning title=PLD sem nova coleta::Todos os meios de consulta foram bloqueados ou indisponíveis. Histórico antigo preservado.')
    return historico


def inicio_semana_operativa(datas: pd.Series) -> pd.Series:
    data = pd.to_datetime(datas).dt.normalize()
    return data - pd.to_timedelta((data.dt.weekday - 5) % 7, unit='D')


def codigo_semana(ini: pd.Timestamp) -> str:
    ini = pd.Timestamp(ini)
    primeiro_dia = pd.Timestamp(ini.year, 1, 1)
    primeiro_sabado = primeiro_dia + pd.Timedelta(days=(5 - primeiro_dia.weekday()) % 7)
    numero = (ini - primeiro_sabado).days // 7 + 1
    if numero <= 0:
        numero = 1
    return f'S{numero}{ini.year}'


def agregar_anual(hist: pd.DataFrame) -> pd.DataFrame:
    df = hist.assign(ano=hist['data'].dt.year).groupby(['ano', 'submercado'], as_index=False)['pld'].mean()
    return df.pivot(index='ano', columns='submercado', values='pld').reindex(columns=ORDEM).reset_index()


def agregar_mensal(hist: pd.DataFrame) -> pd.DataFrame:
    df = hist.copy()
    df['mes_dt'] = df['data'].values.astype('datetime64[M]')
    agg = df.groupby(['mes_dt', 'submercado'], as_index=False)['pld'].mean()
    piv = agg.pivot(index='mes_dt', columns='submercado', values='pld').reindex(columns=ORDEM).reset_index()
    piv['label'] = piv['mes_dt'].apply(lambda x: f"{MESES_PT[pd.Timestamp(x).month]}/{pd.Timestamp(x).year}")
    return piv


def agregar_semanal(hist: pd.DataFrame) -> pd.DataFrame:
    df = hist.copy()
    df['inicio_semana'] = inicio_semana_operativa(df['data'])
    agg = df.groupby(['inicio_semana', 'submercado'], as_index=False)['pld'].mean()
    piv = agg.pivot(index='inicio_semana', columns='submercado', values='pld').reindex(columns=ORDEM).reset_index()
    piv['label'] = piv['inicio_semana'].map(codigo_semana)
    piv['fim_semana'] = piv['inicio_semana'] + pd.Timedelta(days=6)
    return piv


def agregar_diario(hist: pd.DataFrame) -> pd.DataFrame:
    agg = hist.groupby(['data', 'submercado'], as_index=False)['pld'].mean()
    piv = agg.pivot(index='data', columns='submercado', values='pld').reindex(columns=ORDEM).reset_index()
    piv['label'] = piv['data'].dt.strftime('%d/%m/%Y')
    return piv


def agregar_horario(hist: pd.DataFrame, dia: pd.Timestamp) -> pd.DataFrame:
    df = hist.loc[hist['data'] == dia]
    tabela = df.pivot_table(index='hora', columns='submercado', values='pld', aggfunc='mean')
    return tabela.reindex(index=range(24), columns=ORDEM).reset_index()


def resumo_pld(hist: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    ultima = pd.Timestamp(hist['data'].max()).normalize()
    ini_sem = inicio_semana_operativa(pd.Series([ultima])).iloc[0]
    fim_sem = ini_sem + pd.Timedelta(days=6)
    ini_mes = ultima.replace(day=1)
    ini_ano = ultima.replace(month=1, day=1)

    def media_periodo(mask):
        grp = hist.loc[mask].groupby('submercado')['pld'].mean()
        return [round(float(grp.get(sm, float('nan'))), 2) if pd.notna(grp.get(sm, pd.NA)) else None for sm in ORDEM]

    dados = [
        ['PLD Hoje', *media_periodo(hist['data'] == ultima)],
        ['PLD Semana', *media_periodo((hist['data'] >= ini_sem) & (hist['data'] <= ultima))],
        ['PLD Mês', *media_periodo((hist['data'] >= ini_mes) & (hist['data'] <= ultima))],
        ['PLD Ano', *media_periodo((hist['data'] >= ini_ano) & (hist['data'] <= ultima))],
    ]
    tabela = pd.DataFrame(dados, columns=['Indicador'] + ORDEM)
    notas = {
        'hoje': ultima.strftime('%d/%m/%Y'),
        'semana': f'{ini_sem.strftime("%d/%m/%Y")} a {fim_sem.strftime("%d/%m/%Y")} (acumulado até {ultima.strftime("%d/%m/%Y")})',
        'mes': f'{MESES_PT[ultima.month]}/{ultima.year}',
        'ano': str(ultima.year),
    }
    return tabela, notas


def configurar_layout(fig: go.Figure, eixo_x: str, y_title: str = 'Preço (R$/MWh)') -> go.Figure:
    fig.update_layout(
        height=500,
        paper_bgcolor='white', plot_bgcolor='white',
        font=dict(family=FONTE, size=13, color='#263142'),
        showlegend=False,
        hovermode='x unified',
        margin=dict(l=96, r=30, t=24, b=86),
        xaxis=dict(
            title=eixo_x, showgrid=False, showline=True,
            linecolor='#50627a', tickcolor='#50627a', ticks='outside',
            ticklen=7, tickwidth=1.2, zeroline=False,
            minor=dict(ticks='outside', ticklen=3, tickcolor='#98a2b3', showgrid=False),
            automargin=True,
        ),
        yaxis=dict(
            title=y_title, showgrid=False, showline=True,
            linecolor='#50627a', tickcolor='#50627a', ticks='outside',
            ticklen=7, tickwidth=1.2, zeroline=False, rangemode='tozero',
            minor=dict(ticks='outside', ticklen=3, tickcolor='#98a2b3', showgrid=False),
            automargin=True,
        ),
    )
    return fig


def criar_figura(df: pd.DataFrame, coluna_x: str, eixo_x: str, granularidade: str, y_title: str = 'Preço (R$/MWh)') -> go.Figure:
    fig = go.Figure()
    n = len(df)
    for sm in ORDEM:
        serie = pd.to_numeric(df[sm], errors='coerce')
        fig.add_trace(go.Scatter(
            x=df[coluna_x], y=serie, name=sm, mode='lines+markers',
            line=dict(color=CORES[sm], width=2.6, shape='spline', smoothing=0.4),
            marker=dict(color=CORES[sm], size=6 if n <= 30 else 4),
            hovertemplate='%{y:,.2f} R$/MWh<extra>' + sm + '</extra>',
        ))
    configurar_layout(fig, eixo_x, y_title=y_title)
    if granularidade == 'ano':
        fig.update_xaxes(type='category')
    elif granularidade == 'mes':
        passo = max(1, len(df) // 12)
        vals = [str(df.iloc[i][coluna_x]) for i in range(0, len(df), passo)]
        txt = [str(df.iloc[i][coluna_x]) for i in range(0, len(df), passo)]
        fig.update_xaxes(type='category', tickmode='array', tickvals=vals, ticktext=txt, tickangle=-35)
    elif granularidade == 'semana':
        passo = max(1, len(df) // 14)
        vals = [str(df.iloc[i][coluna_x]) for i in range(0, len(df), passo)]
        txt = [str(df.iloc[i][coluna_x]) for i in range(0, len(df), passo)]
        fig.update_xaxes(type='category', tickmode='array', tickvals=vals, ticktext=txt, tickangle=-40)
    elif granularidade == 'dia':
        passo = max(1, len(df) // 14)
        vals = [str(df.iloc[i][coluna_x]) for i in range(0, len(df), passo)]
        txt = [str(df.iloc[i][coluna_x]) for i in range(0, len(df), passo)]
        fig.update_xaxes(type='category', tickmode='array', tickvals=vals, ticktext=txt, tickangle=-40)
    else:
        fig.update_xaxes(type='linear', tickmode='linear', tick0=0, dtick=1, range=[-0.5, 23.5])
    return fig


def legenda_html(plot_id: str) -> str:
    from html import escape
    partes = [f'<div class="legenda-externa" role="group" aria-label="Séries do gráfico {escape(plot_id)}">']
    for i, sub in enumerate(ORDEM):
        partes.append(
            f'<button type="button" class="legenda-item" data-plot="{plot_id}" data-indice="{i}" aria-pressed="true">'
            f'<span class="legenda-linha" style="--cor-serie:{CORES[sub]}"></span>{escape(sub)}</button>'
        )
    partes.append('</div>')
    return ''.join(partes)


def _horarios_por_dia(hist: pd.DataFrame) -> dict[str, list[list[float | None]]]:
    mapa = {}
    for dia, bloco in hist.groupby('data', sort=True):
        tabela = bloco.pivot(index='hora', columns='submercado', values='pld').reindex(index=range(24), columns=ORDEM)
        por_sub = []
        for sm in ORDEM:
            serie = pd.to_numeric(tabela[sm], errors='coerce')
            por_sub.append([None if pd.isna(v) else round(float(v), 2) for v in serie])
        mapa[pd.Timestamp(dia).strftime('%Y-%m-%d')] = por_sub
    return mapa


def tabela_resumo_html(tabela: pd.DataFrame, notas: dict) -> str:
    def fmt(v):
        if pd.isna(v):
            return '—'
        return f'{float(v):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

    cards = []
    cabecalhos = {'SE/CO': 'SE/CO', 'SUL': 'SUL', 'NE': 'NE', 'N': 'Norte'}
    for _, row in tabela.iterrows():
        indicadores = ''.join(
            f'<div class="item-resumo"><span class="rotulo-resumo">{cabecalhos[c]}</span><span class="valor-resumo">{fmt(row[c])}</span></div>'
            for c in ORDEM
        )
        cards.append(
            f'<div class="card-resumo">'
            f'<div class="titulo-card-resumo">{row["Indicador"]}</div>'
            f'<div class="grade-resumo">{indicadores}</div>'
            f'</div>'
        )

    nota = (
        f'<div class="nota-resumo">'
        f'<strong>Referências:</strong> Hoje: {notas["hoje"]} &nbsp;|&nbsp; '
        f'Semana: {notas["semana"]} &nbsp;|&nbsp; '
        f'Mês: {notas["mes"]} &nbsp;|&nbsp; '
        f'Ano: {notas["ano"]}'
        f'</div>'
    )
    return f'<section class="painel"><div class="barra-titulo">PLD — Resumo</div><div class="quadro-resumo"><div class="cards-resumo">{"".join(cards)}</div>{nota}</div></section>'


def pagina_pld(hist: pd.DataFrame) -> str:
    from html import escape
    hist = hist[hist['submercado'].isin(ORDEM)].copy()
    if hist.empty:
        raise ValueError('PLD: nenhum registro válido')
    hist['data'] = pd.to_datetime(hist['data'])
    hist = hist.sort_values(['data', 'hora', 'submercado']).drop_duplicates(['data', 'hora', 'submercado'], keep='last')
    ultima = pd.Timestamp(hist['data'].max()).normalize()
    ultima_br = ultima.strftime('%d/%m/%Y')
    primeira = pd.Timestamp(hist['data'].min()).normalize()
    hoje = pd.Timestamp(datetime.now(ZoneInfo('America/Sao_Paulo')).date())
    dias_atraso = int((hoje - ultima).days)
    aviso_defasagem = ''
    if dias_atraso > 2:
        aviso_defasagem = (
            f'<div class="aviso-atraso">Atenção: PLD com última referência em {ultima_br}. '
            f'Última atualização com novos preços ainda não foi confirmada.</div>'
        )

    anual = agregar_anual(hist)
    mensal = agregar_mensal(hist)
    semanal = agregar_semanal(hist)
    diario = agregar_diario(hist)
    horario = agregar_horario(hist, ultima)
    resumo, notas = resumo_pld(hist)

    configs = [
        ('PLD — Anual', f'Média anual dos preços horários por submercado — último ano parcial até {ultima_br}', criar_figura(anual, 'ano', 'Ano', 'ano', y_title='Preço (R$/MWh)')),
        ('PLD — Mensal', f'Média mensal dos preços horários por submercado — último mês parcial até {ultima_br}', criar_figura(mensal, 'label', 'Mês', 'mes', y_title='Preço (R$/MWh)')),
        ('PLD — Semanal', f'Média semanal por semana operativa (sábado a sexta) — última semana parcial até {ultima_br}', criar_figura(semanal, 'label', 'Semana', 'semana', y_title='Preço (R$/MWh)')),
        ('PLD — Diário', f'Média diária dos preços horários por submercado — histórico acumulado até {ultima_br}', criar_figura(diario, 'label', 'Dia', 'dia', y_title='Preço (R$/MWh)')),
        ('PLD — Horário', 'Curva horária por data selecionada', criar_figura(horario, 'hora', 'Hora', 'hora', y_title='Preço (R$/MWh)')),
    ]

    blocos = [tabela_resumo_html(resumo, notas)]
    for i, (titulo, subtitulo, figura) in enumerate(configs):
        pid = f'plot-pld-{i}'
        grafico = figura.to_html(full_html=False, include_plotlyjs=False, div_id=pid, config={'responsive': True, 'displaylogo': False})
        controle_data = ''
        if titulo == 'PLD — Horário':
            controle_data = (
                '<div class="controle-data">'
                '<label for="data-pld">Data do PLD horário:</label>'
                f'<input type="date" id="data-pld" value="{ultima.strftime("%Y-%m-%d")}" min="{primeira.strftime("%Y-%m-%d")}" max="{ultima.strftime("%Y-%m-%d")}" />'
                f'<span id="status-data-pld" role="status">{ultima_br}</span>'
                '</div>'
            )
        blocos.append(
            f'<section class="painel">'
            f'<div class="barra-titulo">{escape(titulo)}</div>'
            f'<div class="subtitulo">{escape(subtitulo)}</div>'
            f'{controle_data}{grafico}{legenda_html(pid)}'
            f'<footer class="fonte-dados">Fonte: CCEE &nbsp;|&nbsp; Dados disponíveis até: {ultima_br}</footer>'
            f'</section>'
        )

    dados_horarios_js = json.dumps(_horarios_por_dia(hist), ensure_ascii=False, separators=(',', ':'))
    return f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PLD — Acompanhamento de mercado</title>
<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"></script>
<style>
  * {{box-sizing:border-box}}
  body {{margin:0;background:#fff;color:#263142;font-family:{FONTE}}}
  .area {{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;padding:0 12px 20px}}
  .aviso-atraso {{padding:11px 16px;border:1px solid #e9cf95;border-radius:10px;background:#fffaf0;color:#855200;font-size:13px}}
  .painel {{min-width:0;background:#fff;border:1px solid #e1e7ef;border-radius:14px;overflow:hidden;display:flex;flex-direction:column}}
  .barra-titulo {{width:100%;background:{COR_VALE};color:#fff;font-weight:700;text-align:center;padding:12px 16px;font-size:18px;border-radius:14px 14px 0 0}}
  .subtitulo {{padding:12px 18px 0;text-align:center;color:#667085;font-size:12px;line-height:1.6}}
  .quadro-tabela {{padding:18px 16px 18px}}
  table.resumo-pld {{width:auto;min-width:360px;border-collapse:collapse;margin:0 auto;font-size:14px;color:#101828}}
  .resumo-pld th,.resumo-pld td {{padding:6px 14px;border:1px solid #101828;text-align:center;white-space:nowrap}}
  .resumo-pld thead th:first-child {{background:#fff;border-top-color:transparent;border-left-color:transparent}}
  .resumo-pld tbody th {{text-align:left;font-weight:700}}
  .nota-resumo {{margin-top:10px;text-align:center;color:#667085;font-size:12px;line-height:1.6}}
  .js-plotly-plot {{width:100%!important}}
  .legenda-externa {{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:10px 18px;padding:4px 16px 15px;min-height:40px}}
  .legenda-item {{border:1px solid #e0e6ee;border-radius:8px;background:#fff;color:#263142;font:inherit;font-size:12px;display:inline-flex;align-items:center;justify-content:center;gap:9px;padding:7px 12px;min-width:92px;cursor:pointer;white-space:nowrap}}
  .legenda-item[aria-pressed="false"] {{opacity:.38}}
  .legenda-linha {{height:0;width:28px;border-top:3px solid var(--cor-serie);display:inline-block;position:relative}}
  .legenda-linha:after {{content:"";position:absolute;left:10px;top:-5px;width:7px;height:7px;background:var(--cor-serie);border-radius:50%}}
  .controle-data {{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:center;padding:16px 12px 0;font-size:13px}}
  #data-pld {{font:inherit;color:#263142;padding:8px 10px;border:1px solid #c6d0dc;border-radius:8px}}
  #status-data-pld {{color:#667085}}
  .fonte-dados {{padding:7px 18px 18px;color:#667085;font-size:11px;line-height:1.5;margin-top:auto}}
  @media(max-width:750px) {{.area {{padding:0;gap:10px}} .painel {{border-radius:8px}} .quadro-tabela {{padding:12px 8px 14px;overflow:auto}} .legenda-externa {{gap:7px}} .legenda-item {{min-width:76px;padding:7px 8px}} }}
</style></head><body>
<main class="area">{aviso_defasagem}{''.join(blocos)}</main>
<script>
(function() {{
  document.querySelectorAll('.legenda-item').forEach(function(b) {{
    b.addEventListener('click', function() {{
      const plot = document.getElementById(b.dataset.plot);
      const idx = Number(b.dataset.indice);
      const ativo = b.getAttribute('aria-pressed') === 'true';
      b.setAttribute('aria-pressed', ativo ? 'false' : 'true');
      Plotly.restyle(plot, {{visible: ativo ? 'legendonly' : true}}, [idx]);
    }});
  }});
  const historicoHorarios = {dados_horarios_js};
  const inputData = document.getElementById('data-pld');
  const status = document.getElementById('status-data-pld');
  const plotHorario = document.getElementById('plot-pld-4');
  function atualizarDia() {{
    const data = historicoHorarios[inputData.value];
    if(!data) {{
      status.textContent = 'Sem valores horários disponíveis nessa data';
      Plotly.restyle(plotHorario, {{y: [Array(24).fill(null), Array(24).fill(null), Array(24).fill(null), Array(24).fill(null)]}}, [0,1,2,3]);
      return;
    }}
    status.textContent = inputData.value.split('-').reverse().join('/');
    Plotly.restyle(plotHorario, {{y: data}}, [0,1,2,3]);
  }}
  if (inputData) inputData.addEventListener('change', atualizarDia);
  function comunicarAlturaPLD() {{
    if(window.parent !== window) window.parent.postMessage({{tipo:'altura-painel-ons', id:'pld', altura:document.querySelector('main').offsetHeight+12}}, '*');
  }}
  window.addEventListener('load', comunicarAlturaPLD);
  window.addEventListener('resize', comunicarAlturaPLD);
  if(window.ResizeObserver) new ResizeObserver(comunicarAlturaPLD).observe(document.querySelector('main'));
}})();
</script></body></html>'''


def salvar(hist: pd.DataFrame) -> None:
    DADOS.mkdir(parents=True, exist_ok=True)
    GRAFICOS.mkdir(parents=True, exist_ok=True)
    hist = hist.sort_values(['data', 'hora', 'submercado']).drop_duplicates(['data', 'hora', 'submercado'], keep='last')
    hist.to_csv(LONGO, sep=';', decimal=',', float_format='%.4f', date_format='%Y-%m-%d', index=False)
    PAGINA_PLD.write_text(pagina_pld(hist), encoding='utf-8')
    ultimo = pd.Timestamp(hist['data'].max())
    data_max = ultimo.strftime('%d/%m/%Y')
    atraso = (datetime.now(ZoneInfo('America/Sao_Paulo')).date() - ultimo.date()).days
    if atraso > 2:
        print(f'::warning title=PLD desatualizado::Último PLD: {data_max} ({atraso} dias atrás). Verifique o bloqueio da CCEE ou importe manualmente o CSV.')
    print(f'Histórico PLD: {len(hist):,} registros horários.')
    print(f'Última data do PLD: {data_max}')
    print(f'Página: {PAGINA_PLD}')


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--somente-local', action='store_true')
    p.add_argument('--historico-base', type=Path)
    args = p.parse_args()
    hist = carregar_historico_base(args.historico_base) if args.historico_base else carregar_base_consolidada()
    hist = atualizar_com_online(hist, usar_online=not args.somente_local)
    salvar(hist)


if __name__ == '__main__':
    main()
