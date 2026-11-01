from __future__ import annotations

import argparse
import json
import re
import unicodedata
import warnings
from datetime import datetime, timedelta
from io import BytesIO
from tempfile import TemporaryDirectory
from html.parser import HTMLParser
from urllib.parse import urljoin
import os
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
CORES_ANOS = {2020: '#8B98A7', 2021: '#D62828', 2022: '#7D8B4E', 2023: '#77C9F5', 2024: '#9B88A3', 2025: '#D97706', 2026: '#2563EB'}
ESTILOS_SUB = {'SE/CO': 'solid', 'SUL': 'dash', 'NE': 'dot', 'N': 'dashdot'}

URL_DATASET = 'https://dadosabertos.ccee.org.br/dataset/pld_horario'
URL_API = 'https://dadosabertos.ccee.org.br/api/3/action/package_show'
# Identificadores dos recursos anuais confirmados no catálogo público da CCEE.
# Identificadores de anos futuros são descobertos quando publicados.
IDS_ANUAIS_CONHECIDOS = {
    2025: '2a180a6b-f092-43eb-9f82-a48798b803dc',
    2026: '3f279d6b-1069-42f7-9b0a-217b084729c4',
}
URL_DUMP = 'https://dadosabertos.ccee.org.br/datastore/dump/{id}?bom=True'

def url_dump(recurso_id: str) -> str:
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', recurso_id):
        raise ValueError('Identificador anual CCEE inválido')
    return URL_DUMP.format(id=recurso_id)
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
        'sul': 'SUL', 's': 'SUL', 'nordeste': 'NE', 'ne': 'NE', 'norte': 'N', 'n': 'N'
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
    """Localiza IDs CKAN em links de recursos anuais na página do catálogo."""
    def __init__(self):
        super().__init__()
        self.ano_atual = None
        self.recursos = {}
        self.links = []
        self._href = None
        self._texto = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self._href = dict(attrs).get('href', '')
            self._texto = []

    def handle_data(self, data):
        if self._href is not None:
            self._texto.append(data)
        m = re.search(r'pld_horario_(\d{4})', data.strip(), re.I)
        if m:
            self.ano_atual = int(m.group(1))

    def handle_endtag(self, tag):
        if tag != 'a' or self._href is None:
            return
        href = urljoin(URL_DATASET, self._href)
        txt = ''.join(self._texto)
        ano_m = re.search(r'pld_horario_(\d{4})', txt, re.I)
        recurso_m = re.search(r'/resource/([a-fA-F0-9-]{36})', href)
        if ano_m and recurso_m:
            ano = int(ano_m.group(1))
            self.recursos[ano] = {'name': f'pld_horario_{ano}', 'id': recurso_m.group(1)}
        elif recurso_m:
            self.links.append(recurso_m.group(1))
        self._href = None
        self._texto = []


def extrair_recursos_html(html: str) -> dict[int, dict]:
    parser = ExtratorLinksCCEE()
    parser.feed(html)
    return parser.recursos


def recursos_ccee_html() -> dict[int, dict]:
    import requests
    r = requests.get(URL_DATASET, timeout=60, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    rec = extrair_recursos_html(r.text)
    if not rec:
        raise RuntimeError('Não foi possível identificar os IDs anuais no catálogo público')
    return rec


def baixar_csv_ano(recurso: dict, ano: int) -> pd.DataFrame:
    import requests
    s = requests.Session()
    s.headers.update({'User-Agent': 'Painel-Vale-PLD/1.0'})
    url = recurso.get('url') or recurso.get('download_url')
    if not url and recurso.get('id'):
        url = url_dump(recurso['id'])
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
            if not raw:
                raise ValueError('arquivo vazio')
            # Download do Datastore costuma usar BOM UTF-8; nunca interpretar o BOM
            # como caracteres latinos no primeiro nome de coluna.
            try:
                primeiro = raw[:4096].decode('utf-8-sig').splitlines()[0]
                encoding = 'utf-8-sig'
            except UnicodeDecodeError:
                primeiro = raw[:4096].decode('latin1').splitlines()[0]
                encoding = 'latin1'
            sep = ';' if primeiro.count(';') > primeiro.count(',') else ','
            df = pd.read_csv(BytesIO(raw), sep=sep, encoding=encoding, dtype=str)
            df.columns = [str(c).lstrip('\ufeff').strip() for c in df.columns]
            df = normalizar_csv_ccee(df, ano)
            partes.append(df)
            print(f'PLD: importação manual {ano}: até {df.data.max().date()} ({len(df)} preços).')
        except Exception as exc:
            print(f'::warning title=PLD arquivo manual inválido::{arquivo.name} ({exc})')
    return partes


def _ler_download_formulario(arquivo: Path) -> pd.DataFrame:
    """Converte XML SpreadsheetML, CSV, XLSX ou HTML baixado do painel CCEE.

    Não aceita arquivos vazios nem preços agregados sem colunas dia/hora/submercado.
    """
    extensao = arquivo.suffix.lower()
    cabecalho = arquivo.read_bytes()[:160].lstrip()
    if extensao in {'.xlsx', '.xls'} and cabecalho.startswith(b'<?xml'):
        bruto = ler_xml_excel(arquivo)
    elif extensao in {'.xlsx', '.xls'}:
        bruto = pd.read_excel(arquivo, dtype=str)
    elif extensao in {'.htm', '.html'}:
        tabelas = pd.read_html(arquivo)
        if not tabelas:
            raise ValueError('HTML da CCEE sem tabela de preços')
        bruto = tabelas[0]
    else:
        bruto = ler_bruto(arquivo)
    try:
        dados = normalizar_bruto(bruto)
    except ValueError:
        # Aceita também layout longo anual com MES_REFERENCIA.
        anos = set()
        poss = {_sem_acento(c).replace(' ', '_') for c in bruto.columns}
        if 'mes_referencia' in poss:
            coluna = next(c for c in bruto.columns if _sem_acento(c).replace(' ', '_') == 'mes_referencia')
            anos = set(pd.to_numeric(bruto[coluna].astype(str).str[:4], errors='coerce').dropna().astype(int))
        if not anos:
            raise
        partes = []
        for ano in sorted(anos):
            partes.append(normalizar_csv_ccee(bruto, int(ano)))
        dados = pd.concat(partes, ignore_index=True)
    if dados.empty or not dados['submercado'].isin(ORDEM).any():
        raise ValueError('Download gerado não contém valores horários de PLD válidos')
    if dados['hora'].min() == 1 and dados['hora'].max() == 24:
        dados['hora'] = dados['hora'] - 1
    dados = dados[dados['hora'].between(0, 23) & dados['submercado'].isin(ORDEM)].copy()
    return dados.sort_values(['data', 'hora', 'submercado']).drop_duplicates(['data', 'hora', 'submercado'], keep='last')


# O site CCEE usa o identificador abaixo no controle de consulta/geração.
# Procurar pelo ID é mais robusto do que depender apenas do texto do botão.
SELETORES_GERAR_CCEE = (
    '#filtrarHistoricoPreco',  # XPath fornecido: //*[@id="filtrarHistoricoPreco"]
    '[id="filtrarHistoricoPreco"]',
    '#gerarArquivo',
    '#gerarHistoricoPreco',
)
URL_PAINEL_CCEE = 'https://www.ccee.org.br/web/guest/precos/painel-precos'


def _botao_formulario_ccee(frame):
    """Encontra o controle que dispara a busca, seja botão, input ou link."""
    for css in SELETORES_GERAR_CCEE:
        try:
            loc = frame.locator(css)
            for i in range(min(loc.count(), 4)):
                candidato = loc.nth(i)
                if candidato.is_visible():
                    return candidato
        except Exception:
            continue
    for loc in (
        frame.get_by_role('button', name=re.compile(r'Gerar\s+Arquivo|Filtrar\s+Hist[oó]rico', re.I)),
        frame.locator('input[type="submit"][value*="Gerar"], button:has-text("Gerar Arquivo")'),
        frame.get_by_text('Gerar Arquivo', exact=True),
    ):
        try:
            for i in range(min(loc.count(), 4)):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            pass
    return None


def _locate_form_frame(page, timeout_ms=60000):
    """Encontra o formulário mesmo se estiver em iframe carregado tardiamente.

    Evita procurar somente o texto 'Gerar Arquivo': a CCEE pode exibir o
    controle com ID filtrarHistoricoPreco e texto diferente.
    """
    from time import monotonic
    fim = monotonic() + timeout_ms / 1000
    ultima_contagem = 0
    while monotonic() < fim:
        ultima_contagem = len(page.frames)
        for frame in page.frames:
            try:
                if _botao_formulario_ccee(frame) is not None:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(1200)
    frames = ', '.join((f.url or '[sem URL]')[:120] for f in page.frames)
    raise RuntimeError(
        f'Controle #filtrarHistoricoPreco/Gerar Arquivo não localizado em '
        f'{ultima_contagem} frame(s). URLs: {frames[:800]}'
    )


def _selecionar_preco_horario(frame):
    """Usa select nativo ou combobox customizado conforme o portal."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    for select in frame.locator('select').all():
        try:
            textos = select.locator('option').all_text_contents()
            for i, txt in enumerate(textos):
                if 'hor' in _sem_acento(txt) and 'seman' not in _sem_acento(txt):
                    options = select.locator('option')
                    val = options.nth(i).get_attribute('value')
                    if val:
                        select.select_option(value=val)
                    else:
                        select.select_option(index=i)
                    return
        except Exception:
            pass
    candidatos = [
        frame.get_by_role('combobox', name=re.compile('tipo', re.I)),
        frame.get_by_text(re.compile('selecione o tipo de preço', re.I)),
        frame.get_by_role('combobox'),
    ]
    for candidato in candidatos:
        try:
            if candidato.count() == 0:
                continue
            candidato.first.click(timeout=3000)
            for opcao in [
                frame.get_by_role('option', name=re.compile('horário', re.I)),
                frame.get_by_text(re.compile('^(preço médio )?horário$', re.I)),
                frame.get_by_text(re.compile('preço horário', re.I)),
            ]:
                if opcao.count():
                    opcao.first.click(timeout=3000)
                    return
        except (PlaywrightTimeoutError, Exception):
            continue
    raise RuntimeError('Não foi possível selecionar Preço Horário; formulário pode ter mudado')


def _preencher_data_ccee(frame, inicio, fim):
    """Preenche 'De/Até' NO formulário do PLD, evitando filtros do cabeçalho.

    Aceita input date, text com máscara e componentes controlados por eventos.
    """
    botao = _botao_formulario_ccee(frame)
    escopo = frame
    if botao is not None:
        form = botao.locator('xpath=ancestor::form[1]')
        if form.count():
            escopo = form

    # Campo data dedicado CCEE, quando disponível.
    pares_css = (
        ('#dataInicialHistoricoPreco', '#dataFinalHistoricoPreco'),
        ('#dataInicioHistoricoPreco', '#dataFimHistoricoPreco'),
        ('input[name*="dataInicio"]', 'input[name*="dataFim"]'),
        ('input[id*="dataInicial"]', 'input[id*="dataFinal"]'),
    )
    candidatos = []
    for ini_css, fim_css in pares_css:
        try:
            a, b = frame.locator(ini_css), frame.locator(fim_css)
            if a.count() and b.count() and a.first.is_visible() and b.first.is_visible():
                candidatos = [a.first, b.first]
                break
        except Exception:
            pass

    if not candidatos:
        for seletor in (
            'input[placeholder*="dd/mm"], input[type="date"]',
            'input[placeholder*="aaaa"], input[aria-label*="data"], input[id*="Data"]',
        ):
            campos = [i for i in escopo.locator(seletor).all() if i.is_visible()]
            if len(campos) >= 2:
                candidatos = campos[-2:]
                break

    if not candidatos:
        campos = []
        for label in ('De', 'Até'):
            try:
                elemento = escopo.get_by_label(label, exact=True)
                if elemento.count() == 1 and elemento.first.is_visible():
                    campos.append(elemento.first)
            except Exception:
                pass
        if len(campos) == 2:
            candidatos = campos

    if len(candidatos) != 2:
        raise RuntimeError('Campos De/Até do formulário histórico CCEE não encontrados')

    for loc, data in zip(candidatos, [inicio, fim]):
        iso = data.strftime('%Y-%m-%d')
        brasileiro = data.strftime('%d/%m/%Y')
        valor = iso if loc.get_attribute('type') == 'date' else brasileiro
        try:
            loc.fill(valor, timeout=5000)
            loc.press('Tab', timeout=2000)
        except Exception:
            # Fallback para datepickers/inputs controlados por JavaScript.
            loc.evaluate("""(el, valor) => {
                const proto = Object.getPrototypeOf(el);
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, valor); else el.value = valor;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }""", valor)
        print(f'PLD formulário: campo {"De" if loc == candidatos[0] else "Até"} = {valor}')


def _links_download_ccee(frame):
    """Links associados ao resultado da exportação histórica, não à navegação."""
    seletores = (
        'a[download]',
        'a[href$=".xml"], a[href*=".xml?"], a[href$=".xlsx"], a[href$=".csv"]',
        'a:has-text("Preço Médio Horário")',
        'a:has-text("Histórico.xml")',
        'a:has-text("Nome do arquivo")',
    )
    links = []
    for seletor in seletores:
        try:
            for loc in frame.locator(seletor).all():
                if loc.is_visible():
                    # Usar o elemento como referência para evitar cliques repetidos.
                    links.append(loc)
        except Exception:
            pass
    return links


def _download_por_formulario(page, frame, destino: Path) -> Path:
    """Consulta usando o ID encontrado pelo usuário e salva o arquivo gerado.

    Alguns fluxos iniciam o download diretamente; outros exibem um link na seção
    'Download do histórico'. Suporta ambos sem depender só do nome do botão.
    """
    from time import monotonic
    downloads = []
    janelas = []
    page.on('download', lambda download: downloads.append(download))
    page.context.on('page', lambda nova: janelas.append(nova))
    botao = _botao_formulario_ccee(frame)
    if botao is None:
        raise RuntimeError('Controle #filtrarHistoricoPreco/Gerar Arquivo ausente')
    print(f'PLD formulário: acionando {botao.get_attribute("id") or botao.inner_text()[:60] or "controle"}')
    botao.click(timeout=15000)
    # Permitir que uma requisição de geração atualize o link da página.
    page.wait_for_timeout(1800)

    # A CCEE pode primeiro gerar o resultado e só então criar o link.
    # Dar tempo à geração, mas não clicar em outros botões da página.
    prazo = monotonic() + 65
    ultimo_link = None
    clicou_link = False
    while monotonic() < prazo and not downloads:
        if not clicou_link:
            for link in reversed(_links_download_ccee(frame)):
                try:
                    href = link.get_attribute('href') or ''
                    texto = link.inner_text(timeout=1200)
                    if not href or href.strip() == '#' or href.lower().startswith('javascript:'):
                        # Um link com handler JS ainda pode iniciar o download.
                        if not (link.get_attribute('onclick') or link.get_attribute('download')):
                            continue
                    ident = (href, texto[:100])
                    if ident == ultimo_link:
                        continue
                    ultimo_link = ident
                    link.click(timeout=8000)
                    clicou_link = True
                    print(f'PLD formulário: link de resultado acionado: {texto[:80] or href[:80]}')
                    break
                except Exception:
                    continue
        if downloads:
            break
        page.wait_for_timeout(1000)

    if not downloads:
        # Algumas implementações abrem o XML em nova aba, sem evento download.
        for nova in janelas:
            try:
                if nova.url.lower().split('?')[0].endswith(('.xml', '.csv', '.xlsx')):
                    resposta = nova.request.get(nova.url, timeout=30000)
                    if resposta.ok:
                        ext = Path(nova.url.split('?', 1)[0]).suffix.lower()
                        final = destino.with_suffix(ext)
                        final.write_bytes(resposta.body())
                        return final
            except Exception:
                pass
        raise RuntimeError('CCEE não iniciou o download após consultar #filtrarHistoricoPreco; '
                           'verificar screenshot em dados/diagnostico_formulario_ccee.png')

    download = downloads[-1]
    falha = download.failure()
    if falha:
        raise RuntimeError(f'Download CCEE falhou: {falha}')
    extensao = Path(download.suggested_filename or 'pld_horario.xml').suffix.lower()
    if extensao not in {'.xml', '.csv', '.xls', '.xlsx', '.html', '.htm'}:
        extensao = '.xml'
    final = destino.with_suffix(extensao)
    download.save_as(str(final))
    print(f'PLD formulário: arquivo recebido: {download.suggested_filename}, '
          f'{final.stat().st_size:,} bytes')
    if final.stat().st_size < 50:
        raise ValueError('Download CCEE muito pequeno para conter PLD horário')
    return final


def buscar_pld_formulario(ultima_data, data_final) -> pd.DataFrame | None:
    """3ª tentativa: consulta por data no formulário público da CCEE.

    Busca dias posteriores à última data, em blocos inferiores a seis meses.
    Requer Playwright/Chromium instalado no workflow.
    """
    from playwright.sync_api import sync_playwright
    inicio = pd.Timestamp(ultima_data).normalize() + pd.Timedelta(days=1)
    fim = pd.Timestamp(data_final).normalize()
    if inicio > fim:
        print('PLD formulário: histórico já cobre até a data atual, sem intervalo pendente.')
        return None
    partes = []
    with TemporaryDirectory(prefix='ccee_pld_') as temp_dir:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
            try:
                contexto = browser.new_context(accept_downloads=True, locale='pt-BR', timezone_id='America/Sao_Paulo')
                while inicio <= fim:
                    # Limite oficial 6 meses; consulta de no máximo ~5 meses, com margem.
                    fim_bloco = min(inicio + pd.DateOffset(months=5) - pd.Timedelta(days=1), fim)
                    page = contexto.new_page()
                    try:
                        page.goto('https://www.ccee.org.br/web/guest/precos/painel-precos',
                                  wait_until='domcontentloaded', timeout=60000)
                        frame = _locate_form_frame(page)
                        _selecionar_preco_horario(frame)
                        _preencher_data_ccee(frame, inicio, fim_bloco)
                        destino = Path(temp_dir) / f'pld_{inicio:%Y%m%d}_{fim_bloco:%Y%m%d}.xml'
                        arquivo = _download_por_formulario(page, frame, destino)
                        df = _ler_download_formulario(arquivo)
                        df = df[(df['data'] >= inicio) & (df['data'] <= fim_bloco)].copy()
                        if df.empty:
                            raise ValueError('Arquivo retornado não contém preços no intervalo solicitado')
                        partes.append(df)
                        print(f'PLD formulário: {inicio:%d/%m/%Y} a {fim_bloco:%d/%m/%Y}, '
                              f'{len(df):,} preços horários (até {df.data.max():%d/%m/%Y})')
                    except Exception:
                        # Captura apenas da página pública para diagnosticar mudanças no formulário.
                        try:
                            DADOS.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(DADOS / 'diagnostico_formulario_ccee.png'), full_page=True, timeout=10000)
                            print('PLD formulário: diagnóstico salvo em dados/diagnostico_formulario_ccee.png')
                        except Exception:
                            pass
                        raise
                    finally:
                        page.close()
                    inicio = fim_bloco + pd.Timedelta(days=1)
            finally:
                browser.close()
    if not partes:
        return None
    return pd.concat(partes, ignore_index=True)


def _mesclar_pld(base: pd.DataFrame, bloco: pd.DataFrame, fonte: str) -> pd.DataFrame:
    if bloco is None or bloco.empty:
        return base
    antes = pd.Timestamp(base['data'].max()) if not base.empty else pd.NaT
    saida = pd.concat([base, bloco], ignore_index=True)
    saida = saida.sort_values(['data', 'hora', 'submercado']).drop_duplicates(
        ['data', 'hora', 'submercado'], keep='last').reset_index(drop=True)
    depois = pd.Timestamp(saida['data'].max())
    print(f'PLD {fonte}: última data antes={antes}, depois={depois}; {len(saida):,} registros.')
    return saida


def _anos_consulta(hoje, historico: pd.DataFrame) -> list[int]:
    anos = {hoje.year}
    if hoje.month <= 2:
        anos.add(hoje.year - 1)
    if not historico.empty:
        ano_historico = pd.Timestamp(historico['data'].max()).year
        if ano_historico >= hoje.year - 1:
            anos.add(ano_historico)
    return sorted(anos)


def _buscar_pela_api(anos: list[int]) -> list[pd.DataFrame]:
    """1º método: API CKAN descobre os recursos e baixa seus CSVs."""
    recursos = recursos_ccee()
    partes = []
    for ano in anos:
        rec = recursos.get(ano)
        if not rec:
            print(f'PLD API: arquivo anual {ano} ainda não consta do catálogo.')
            continue
        try:
            urls = []
            if rec.get('url') or rec.get('download_url'):
                urls.append(rec.get('url') or rec.get('download_url'))
            if rec.get('id'):
                urls.append(url_dump(rec['id']))
            for url in dict.fromkeys(urls):
                try:
                    df = baixar_csv_ano({'url': url}, ano)
                    partes.append(df)
                    print(f'PLD API {ano}: {len(df):,} registros até {df.data.max():%d/%m/%Y}.')
                    break
                except Exception as exc:
                    print(f'::warning title=PLD API {ano}::{type(exc).__name__}: {exc}')
        except Exception as exc:
            print(f'::warning title=PLD API {ano}::{exc}')
    return partes


def _buscar_base_publica(anos: list[int]) -> list[pd.DataFrame]:
    """2º método: download direto do Datastore ou do link publicado no site."""
    recursos = {ano: {'id': rid} for ano, rid in IDS_ANUAIS_CONHECIDOS.items()}
    for ano in anos:
        manual = os.environ.get(f'CCEE_PLD_RECURSO_{ano}', '').strip()
        if manual:
            recursos[ano] = {'id': manual}
    if any(ano not in recursos for ano in anos):
        try:
            recursos.update(recursos_ccee_html())
        except Exception as exc:
            print(f'::warning title=PLD catálogo público::{exc}')
    partes = []
    for ano in anos:
        rec = recursos.get(ano)
        if not rec:
            print(f'PLD público: identificador para {ano} não encontrado; formulário será tentado.')
            continue
        urls = []
        if rec.get('id'):
            urls.append(url_dump(rec['id']))
        if rec.get('download_url') or rec.get('url'):
            urls.append(rec.get('download_url') or rec.get('url'))
        for url in dict.fromkeys(urls):
            try:
                df = baixar_csv_ano({'url': url}, ano)
                partes.append(df)
                print(f'PLD público {ano}: {len(df):,} registros até {df.data.max():%d/%m/%Y}.')
                break
            except Exception as exc:
                print(f'::warning title=PLD público {ano}::{type(exc).__name__}: {exc}')
    return partes


def atualizar_com_online(historico: pd.DataFrame, usar_online: bool) -> pd.DataFrame:
    """Ordem priorizada: navegador CCEE → API → base pública → importação.

    Não perde o histórico se a CCEE bloquear ou se o arquivo não estiver pronto.
    """
    historico = historico.copy()
    if historico.empty:
        raise ValueError('Histórico inicial de PLD vazio')
    hoje = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    data_meta = pd.Timestamp(hoje)
    anos = _anos_consulta(hoje, historico)
    if usar_online:
        # Método 1: navegador, prioritário para contornar as restrições da API
        # apenas através do formulário público oficial (sem acesso autenticado).
        if pd.Timestamp(historico['data'].max()).normalize() < data_meta:
            print('PLD: tentativa 1/4 — navegador CCEE (controle #filtrarHistoricoPreco).')
            try:
                bloco = buscar_pld_formulario(historico['data'].max(), data_meta)
                if bloco is not None:
                    historico = _mesclar_pld(historico, bloco, 'navegador CCEE')
            except Exception as exc:
                print(f'::warning title=PLD navegador CCEE::{type(exc).__name__}: {exc}')
        # Métodos 2 e 3: cascata de contingência; só tentar se faltarem dias.
        for ordem, nome, rotina in [
            (2, 'API CCEE', _buscar_pela_api),
            (3, 'base pública CCEE', _buscar_base_publica),
        ]:
            if pd.Timestamp(historico['data'].max()).normalize() >= data_meta:
                break
            print(f'PLD: tentativa {ordem}/4 — {nome} (contingência).')
            try:
                partes = rotina(anos)
                for parte in partes:
                    historico = _mesclar_pld(historico, parte, nome)
            except Exception as exc:
                print(f'::warning title=PLD método {ordem} falhou::{nome}: {exc}')
    else:
        print('PLD: modo offline; fontes remotas não consultadas.')
    # Método 4: CSV anual colocado em dados/pld_importacao_AAAA.csv.
    if pd.Timestamp(historico['data'].max()).normalize() < data_meta or not usar_online:
        print('PLD: tentativa 4/4 — arquivos CSV importados no GitHub.')
        partes = carregar_importacoes_manuais()
        for parte in partes:
            historico = _mesclar_pld(historico, parte, 'arquivo importado')
    ultimo = pd.Timestamp(historico['data'].max()).strftime('%d/%m/%Y')
    print(f'PLD: data final consolidada {ultimo}.')
    if usar_online and pd.Timestamp(historico['data'].max()).normalize() < data_meta:
        print('::warning title=PLD incompleto::Todas as fontes foram tentadas; '
              f'última data presente no histórico: {ultimo}.')
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
        ['PLD Horário', *media_periodo(hist['data'] == ultima)],
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
    # Máximo e mínimo horários do último dia disponível, por submercado.
    # A média principal do card continua sendo a média horária daquele dia.
    extremos = hist.loc[hist['data'] == ultima].groupby('submercado')['pld'].agg(['min', 'max'])
    notas['extremos_hoje'] = {
        sub: {'min': float(extremos.loc[sub, 'min']), 'max': float(extremos.loc[sub, 'max'])}
        for sub in ORDEM if sub in extremos.index
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


def figura_mensal_anos(hist: pd.DataFrame) -> tuple[go.Figure, list[int], list[str]]:
    """Gráfico mensal único: meses + marcador de consolidado anual (sem linha).

    Cada combinação ano/submercado gera duas traces Plotly:
      1. linha de janeiro a dezembro;
      2. apenas um marcador em x=13.3, representando a média anual dos
         preços horários disponíveis (inclusive ano ainda em andamento).
    Os metadados iguais permitem filtrar ambas pelas legendas de ano/sub.
    """
    dados = hist.copy()
    dados['ano'] = dados['data'].dt.year
    dados['mes'] = dados['data'].dt.month
    medias = dados.groupby(['ano', 'mes', 'submercado'], as_index=False)['pld'].mean()
    anuais = dados.groupby(['ano', 'submercado'])['pld'].mean()
    anos = sorted(int(a) for a in medias['ano'].unique())
    meses = list(range(1, 13))
    posicao_anual = 13.3
    nomes_meses = [MESES_PT[m].capitalize() for m in meses]
    tabela = medias.set_index(['ano', 'mes', 'submercado'])['pld']
    fig = go.Figure()
    paleta_extra = ['#1B857B', '#6D5AB6', '#BD5384', '#8B6B35', '#5A8DA8']
    cores_ano = {}
    ano_recente = max(anos)
    anos_iniciais = {ano_recente, ano_recente - 1}

    for i, ano in enumerate(anos):
        cor = CORES_ANOS.get(ano, paleta_extra[i % len(paleta_extra)])
        cores_ano[ano] = cor
        for sub in ORDEM:
            valores = [tabela.get((ano, mes, sub), float('nan')) for mes in meses]
            visivel = sub == 'SE/CO' and ano in anos_iniciais
            meta_base = {'ano': str(ano), 'sub': sub}
            fig.add_trace(go.Scatter(
                x=meses, y=valores, name=str(ano),
                mode='lines+markers', visible=visivel,
                meta={**meta_base, 'parte': 'mensal'}, showlegend=False,
                line={'color': cor, 'width': 3 if ano == ano_recente else 2.25,
                      'dash': ESTILOS_SUB[sub], 'shape': 'spline', 'smoothing': .65},
                marker={'color': cor, 'size': 7 if ano == ano_recente else 5},
                hovertemplate=(f'<b>{ano} · {sub}</b><br>Mês: %{{x}}'
                               '<br>PLD médio mensal: %{y:.2f} R$/MWh<extra></extra>'),
            ))
            media_ano = anuais.get((ano, sub), float('nan'))
            # Esta trace não contém linhas: Dez e Ano permanecem desconectados.
            fig.add_trace(go.Scatter(
                x=[posicao_anual], y=[media_ano], name=str(ano),
                mode='markers', visible=visivel,
                meta={**meta_base, 'parte': 'anual'}, showlegend=False,
                marker={'color': cor, 'size': 12, 'symbol': 'diamond',
                        'line': {'color': 'white', 'width': 1.3}},
                hovertemplate=(f'<b>{ano} · {sub}</b><br>Consolidado anual'
                               '<br>PLD médio: %{y:.2f} R$/MWh<extra></extra>'),
            ))
    configurar_layout(fig, 'Mês / Ano', y_title='Preço (R$/MWh)')
    fig.update_layout(height=520, hovermode='closest',
                      margin={'l': 95, 'r': 34, 't': 18, 'b': 78}, showlegend=False)
    fig.update_xaxes(type='linear', tickmode='array', tickvals=meses + [posicao_anual],
                     ticktext=nomes_meses + ['Ano'], range=[.7, 13.8], tickangle=0)
    return fig, anos, [cores_ano[a] for a in anos]


def painel_mensal_html(hist: pd.DataFrame, ultima_br: str) -> str:
    """Dois controles visuais: submercados por traçado, anos por cor."""
    fig, anos, cores = figura_mensal_anos(hist)
    plot_id = 'plot-pld-mensal'
    grafo = fig.to_html(full_html=False, include_plotlyjs=False, div_id=plot_id,
                        config={'responsive': True, 'displaylogo': False})
    filtro = []
    for sm in ORDEM:
        selecionado = (sm == 'SE/CO')
        estilo = ESTILOS_SUB[sm]
        classe = 'filtro-sub selecionado' if selecionado else 'filtro-sub'
        pressionado = 'true' if selecionado else 'false'
        nome = {'N': 'Norte', 'NE': 'Nordeste'}.get(sm, sm)
        filtro.append(
            f'<button type="button" class="{classe}" '
            f'data-sub="{sm}" aria-pressed="{pressionado}">'
            f'<span class="marcador-sub estilo-{estilo}" aria-hidden="true"></span>'
            f'{nome}</button>'
        )
    ano_mais_recente = max(anos)
    anos_iniciais = {ano_mais_recente, ano_mais_recente - 1}
    legendas = []
    for a, cor in zip(anos, cores):
        inicial_ativo = a in anos_iniciais
        pressionado_ano = 'true' if inicial_ativo else 'false'
        legendas.append(
            f'<button type="button" class="filtro-ano" data-ano="{a}" aria-pressed="{pressionado_ano}">'
            f'<span class="indicador-ano" style="--cor-ano:{cor}" aria-hidden="true"></span>{a}</button>'
        )
    return (
        '<section class="painel">'
        '<div class="barra-titulo">PLD — Mensal</div>'
        f'<div class="subtitulo">Média mensal dos preços horários, de janeiro a dezembro. '
        f'Todos os anos do histórico, até {ultima_br}.</div>'
        '<div class="filtro-mensal" role="group" aria-label="Selecionar submercados">'
        '<span class="nome-filtro">Submercados:</span>'
        + ''.join(filtro) + '</div>'
        + grafo +
        '<div class="legenda-anos" role="group" aria-label="Selecionar anos">'
        '<span class="nome-filtro">Anos:</span>'
        + ''.join(legendas) + '</div>'
        f'<footer class="fonte-dados">Fonte: CCEE &nbsp;|&nbsp; Dados disponíveis até: {ultima_br}</footer>'
        '</section>'
    )


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
    """Cards com estilos inline para não depender da folha CSS do site/iframe."""
    from html import escape

    def fmt(v):
        if pd.isna(v):
            return '—'
        return f'{float(v):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

    cabecalhos = {'SE/CO': 'SE/CO', 'SUL': 'SUL', 'NE': 'NE', 'N': 'Norte'}
    cores_periodos = ['#007E80', '#007E80', '#007E80', '#007E80']

    def cor_extremo(valor: float) -> str:
        # Cores solicitadas para os preços máximos e mínimos horários do dia.
        # A fronteira superior extrema tem prioridade sobre a primeira faixa.
        if valor >= 1611.04 - 1e-6:
            return '#990D1B'  # vermelho intenso / mais escuro
        if valor > 785.27 + 1e-6:
            return '#E03131'  # vermelho
        if abs(valor - 57.31) < 0.005:
            return '#3AAE78'  # verde-claro (piso)
        return '#1B2634'      # preto

    def detalhe_extremos(sub: str) -> str:
        dados = notas.get('extremos_hoje', {}).get(sub)
        if not dados:
            return ''
        maior, menor = dados['max'], dados['min']
        return (
            '<div class="faixa-extremos" style="margin-top:8px;padding-top:7px;'
            'border-top:1px solid #edf1f5;display:flex;flex-direction:column;gap:3px;'
            'font-size:11px;line-height:1.35;font-variant-numeric:tabular-nums">'
            f'<span style="color:{cor_extremo(maior)}">'
            f'Máx.: <strong>{fmt(maior)}</strong></span>'
            f'<span style="color:{cor_extremo(menor)}">'
            f'Mín.: <strong>{fmt(menor)}</strong></span>'
            '</div>'
        )

    # As referências agora ocupam uma faixa mais larga, abaixo do card
    # PLD Semana e se estendem até a largura dos dois blocos centrais.
    notas_periodos = (
        '<aside class="nota-resumo nota-semana-larga" '
        'style="min-width:0;padding:8px 4px 2px;margin-top:0;'
        'background:transparent;border:none;border-radius:0;'
        'color:#596978;font-size:11px;line-height:1.45;overflow-wrap:anywhere;'
        'box-sizing:border-box">'
        '<div style="font-weight:700;color:#344054;margin-bottom:6px">'
        'Períodos de referência</div>'
        '<div class="grade-nota-referencias" style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));column-gap:14px;row-gap:8px">'
        '<div style="min-width:0">'
        f'<div><strong>PLD Horário:</strong> {escape(notas["hoje"])}</div>'
        f'<div class="nota-semana-texto" style="margin-top:4px"><strong>Semana:</strong> {escape(notas["semana"])}</div>'
        '</div>'
        '<div class="referencias-mes-ano" style="min-width:0;grid-column:2;padding-left:28px">'
        f'<div><strong>Mês:</strong> {escape(notas["mes"])}</div>'
        f'<div style="margin-top:4px"><strong>Ano:</strong> {escape(notas["ano"])}</div>'
        '</div>'
        '</div>'
        '</aside>'
    )

    cards = []
    for indice, (_, linha) in enumerate(tabela.iterrows()):
        eh_hoje = linha['Indicador'] == 'PLD Horário'
        itens = []
        for sm in ORDEM:
            detalhe = detalhe_extremos(sm) if eh_hoje else ''
            itens.append(
                '<div class="item-resumo" '
                'style="min-width:0;display:flex;flex-direction:column;gap:5px;'
                'padding:10px 12px;background:#ffffff;border:1px solid #e4eaf0;'
                'border-radius:10px;box-sizing:border-box">'
                f'<span class="rotulo-resumo" style="font-size:11px;line-height:1.4;'
                f'font-weight:700;color:#667085;letter-spacing:.02em">{escape(cabecalhos[sm])}</span>'
                f'<span class="valor-resumo" style="font-size:19px;line-height:1.3;'
                f'font-weight:700;color:#17354a;font-variant-numeric:tabular-nums">{fmt(linha[sm])}</span>'
                + detalhe + '</div>'
            )
        artigo = (
            '<article class="card-resumo" '
            'style="min-width:0;box-sizing:border-box;background:#f8fafb;'
            'border:1px solid #dce6e9;border-radius:14px;padding:16px;'
            'box-shadow:0 2px 7px rgba(17,53,64,.04)">'
            f'<div class="titulo-card-resumo" style="font-size:15px;'
            f'font-weight:700;color:{cores_periodos[indice]};margin-bottom:14px">'
            f'{escape(str(linha["Indicador"]))}</div>'
            '<div class="grade-resumo" '
            'style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px">'
            + ''.join(itens) + '</div></article>'
        )
        cards.append(artigo)

    # O grid externo possui quatro colunas iguais. O grupo à direita
    # (Semana/Mês/Ano + nota) é um grid aninhado de três colunas iguais.
    # Assim a nota começa imediatamente abaixo de Semana/Mês, sem ser
    # empurrada pela maior altura do card PLD Horário.
    return (
        '<section class="painel">'
        '<div class="barra-titulo">PLD — Resumo</div>'
        '<div class="quadro-resumo" style="padding:20px 22px 18px;box-sizing:border-box">'
        '<div class="cards-resumo" '
        'style="display:grid;gap:14px;align-items:start;width:100%;box-sizing:border-box">'
        + cards[0]
        + '<div class="grupo-cards-direita" '
        'style="min-width:0;display:grid;gap:14px;align-items:start">'
        + ''.join(cards[1:]) + notas_periodos
        + '</div>'
        + '</div></div></section>'
    )


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
    semanal = agregar_semanal(hist)
    diario = agregar_diario(hist)
    horario = agregar_horario(hist, ultima)
    resumo, notas = resumo_pld(hist)

    configs = [
        ('PLD — Anual', f'Média anual dos preços horários por submercado — último ano parcial até {ultima_br}', criar_figura(anual, 'ano', 'Ano', 'ano', y_title='Preço (R$/MWh)')),
        ('PLD — Mensal', '', None),
        ('PLD — Semanal', f'Média semanal por semana operativa (sábado a sexta) — última semana parcial até {ultima_br}', criar_figura(semanal, 'label', 'Semana', 'semana', y_title='Preço (R$/MWh)')),
        ('PLD — Diário', f'Média diária dos preços horários por submercado — histórico acumulado até {ultima_br}', criar_figura(diario, 'label', 'Dia', 'dia', y_title='Preço (R$/MWh)')),
        ('PLD — Horário', 'Curva horária por data selecionada', criar_figura(horario, 'hora', 'Hora', 'hora', y_title='Preço (R$/MWh)')),
    ]

    blocos = [tabela_resumo_html(resumo, notas)]
    for i, (titulo, subtitulo, figura) in enumerate(configs):
        if titulo == 'PLD — Mensal':
            blocos.append(painel_mensal_html(hist, ultima_br))
            continue
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
  .quadro-resumo {{padding:22px 22px 18px}}
  .cards-resumo {{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}}
  .grupo-cards-direita {{grid-column:2 / -1;min-width:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;align-items:start}}
  .grupo-cards-direita > .nota-semana-larga {{grid-column:1 / 4;grid-row:2;align-self:start}}
  .nota-semana-texto {{white-space:nowrap;font-size:10px}}
  .card-resumo {{min-width:0;background:#f9fbfd;border:1px solid #dde6ee;border-radius:13px;padding:15px;box-shadow:0 1px 2px rgba(16,24,40,.035)}}
  .titulo-card-resumo {{font-size:15px;font-weight:700;color:#163f49;margin-bottom:12px}}
  .grade-resumo {{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}}
  .item-resumo {{min-width:0;display:flex;flex-direction:column;gap:5px;padding:10px 9px;background:#fff;border:1px solid #ebeff3;border-radius:9px}}
  .rotulo-resumo {{font-size:11px;font-weight:700;color:#667085}}
  .valor-resumo {{font-size:18px;font-weight:700;color:#123044;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}}
  .nota-resumo {{font-size:12px;color:#667085;line-height:1.7;margin-top:16px;text-align:left}}
  @media(max-width:1150px) {{
    .cards-resumo {{grid-template-columns:1fr}}
    .grupo-cards-direita {{grid-column:1 / -1;grid-template-columns:repeat(2,minmax(0,1fr))}}
    .grupo-cards-direita > .nota-semana-larga {{grid-column:1 / -1;grid-row:auto}}
    .nota-semana-texto {{white-space:normal}}
    .grade-nota-referencias {{grid-template-columns:repeat(2,minmax(0,1fr)) !important}}
    .referencias-mes-ano {{grid-column:2 !important;padding-left:10px !important}}
  }}
  @media(max-width:640px) {{
    .quadro-resumo {{padding:14px}}
    .grupo-cards-direita {{grid-template-columns:1fr}}
    .grupo-cards-direita > .nota-semana-larga > div:last-child {{grid-template-columns:1fr !important}}
    .referencias-mes-ano {{grid-column:1 !important;padding-left:0 !important}}
  }}
  .js-plotly-plot {{width:100%!important}}
  .legenda-externa {{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:10px 18px;padding:4px 16px 15px;min-height:40px}}
  .legenda-item {{border:1px solid #e0e6ee;border-radius:8px;background:#fff;color:#263142;font:inherit;font-size:12px;display:inline-flex;align-items:center;justify-content:center;gap:9px;padding:7px 12px;min-width:92px;cursor:pointer;white-space:nowrap}}
  .legenda-item[aria-pressed="false"] {{opacity:.38}}
  .legenda-linha {{height:0;width:28px;border-top:3px solid var(--cor-serie);display:inline-block;position:relative}}
  .legenda-linha:after {{content:"";position:absolute;left:10px;top:-5px;width:7px;height:7px;background:var(--cor-serie);border-radius:50%}}
  .filtro-mensal {{display:flex;align-items:center;justify-content:flex-start;flex-wrap:wrap;gap:9px;padding:17px 20px 0}}
  .nome-filtro {{font-size:12px;font-weight:700;color:#344054;margin-right:4px}}
  .filtro-sub {{display:inline-flex;align-items:center;gap:8px;border:1px solid #d5dde6;background:white;color:#344054;padding:7px 13px;border-radius:999px;cursor:pointer;font:inherit;font-size:12px;font-weight:600;white-space:nowrap;transition:all .18s ease}}
  .filtro-sub:hover {{border-color:#95a9ba}}
  .filtro-sub[aria-pressed="false"] {{opacity:.53;background:#f7f9fb}}
  .filtro-sub[aria-pressed="true"] {{border-color:#92bebc;background:#edf8f7}}
  .marcador-sub {{display:inline-block;width:28px;height:0;flex:0 0 28px;border-top:3px solid #526a80}}
  .marcador-sub.estilo-dash {{border-top-style:dashed}}
  .marcador-sub.estilo-dot {{border-top-style:dotted;border-top-width:4px}}
  .marcador-sub.estilo-dashdot {{height:3px;border:0;background:repeating-linear-gradient(90deg,#526a80 0 11px,transparent 11px 15px,#526a80 15px 18px,transparent 18px 23px)}}
  .legenda-anos {{display:flex;align-items:center;flex-wrap:wrap;gap:8px 10px;padding:8px 18px 2px}}
  .filtro-ano {{border:1px solid #d5dde6;border-radius:999px;background:#fff;color:#263142;font:inherit;font-size:12px;font-weight:600;display:inline-flex;align-items:center;gap:8px;padding:5px 11px;cursor:pointer;white-space:nowrap;transition:all .18s ease}}
  .filtro-ano:hover {{border-color:#aebfd1;box-shadow:0 1px 2px rgba(16,24,40,.06)}}
  .filtro-ano[aria-pressed="false"] {{opacity:.48;background:#f7f9fb}}
  .filtro-ano[aria-pressed="true"] {{border-color:#a9c1d6}}
  .indicador-ano {{display:inline-block;width:25px;height:0;border-top:3px solid var(--cor-ano);border-radius:999px;position:relative;flex:0 0 25px}}
  .indicador-ano:after {{content:"";position:absolute;left:9px;top:-6px;width:8px;height:8px;border-radius:50%;background:var(--cor-ano)}}
  .controle-data {{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:center;padding:16px 12px 0;font-size:13px}}
  #data-pld {{font:inherit;color:#263142;padding:8px 10px;border:1px solid #c6d0dc;border-radius:8px}}
  #status-data-pld {{color:#667085}}
  .fonte-dados {{padding:7px 18px 18px;color:#667085;font-size:11px;line-height:1.5;margin-top:auto}}
  @media(max-width:750px) {{.area {{padding:0;gap:10px}} .painel {{border-radius:8px}} .quadro-resumo {{padding:12px 8px 14px}} .legenda-externa {{gap:7px}} .legenda-item {{min-width:76px;padding:7px 8px}} }}
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
  // Mensal: um gráfico; 4 submercados filtráveis e todas as séries anuais.
  const mensal = document.getElementById('plot-pld-mensal');
  if (mensal) {{
    const subAtivos = {{'SE/CO': true, 'SUL': false, 'NE': false, 'N': false}};
    const anosAtivos = {{}};
    const anoCorrente = {pd.Timestamp(hist['data'].max()).year};
    mensal.data.forEach(t => {{ anosAtivos[t.meta.ano] = [anoCorrente, anoCorrente - 1].includes(Number(t.meta.ano)); }});
    function atualizarFiltrosMensal() {{
      const vis = mensal.data.map(t => Boolean(subAtivos[t.meta.sub] && anosAtivos[t.meta.ano]));
      Plotly.restyle(mensal, {{visible: vis}}, mensal.data.map((_, i) => i));
    }}
    document.querySelectorAll('.filtro-sub').forEach(b => {{
      b.addEventListener('click', () => {{
        const nome = b.dataset.sub;
        const ativo = !subAtivos[nome];
        if (!ativo && Object.values(subAtivos).filter(Boolean).length === 1) return;
        subAtivos[nome] = ativo;
        b.setAttribute('aria-pressed', String(ativo));
        b.classList.toggle('selecionado', ativo);
        atualizarFiltrosMensal();
      }});
    }});
    document.querySelectorAll('.filtro-ano').forEach(b => {{
      b.addEventListener('click', () => {{
        const ano = b.dataset.ano;
        anosAtivos[ano] = !anosAtivos[ano];
        b.setAttribute('aria-pressed', String(anosAtivos[ano]));
        atualizarFiltrosMensal();
      }});
    }});
  }}
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
