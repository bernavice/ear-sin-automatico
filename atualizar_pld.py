from __future__ import annotations

import argparse
import io
import re
import unicodedata
import warnings
from datetime import datetime, timedelta
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
URL_CCEE = 'https://www.ccee.org.br/web/guest/precos/painel-precos'
FONTE = 'Century Gothic, CenturyGothic, Arial, sans-serif'
COR_VALE = '#007E80'
CORES = {'SE/CO': '#2F66E0', 'SUL': '#D97706', 'NE': '#DC2626', 'N': '#7EC0EE'}
ORDEM = ['SE/CO', 'SUL', 'NE', 'N']
PT_DIAS = {0:'Seg',1:'Ter',2:'Qua',3:'Qui',4:'Sex',5:'Sáb',6:'Dom'}


def _sem_acento(s: str) -> str:
    s = unicodedata.normalize('NFKD', str(s))
    return ''.join(c for c in s if not unicodedata.combining(c)).strip().lower()


def _padronizar_submercado(s: str) -> str:
    x = _sem_acento(s)
    mapa = {
        'sudeste': 'SE/CO', 'sudeste/centro-oeste': 'SE/CO', 'sudeste-centro-oeste': 'SE/CO',
        'se/co': 'SE/CO', 'se': 'SE/CO', 'centro-oeste': 'SE/CO', 'sudeste-centrooeste': 'SE/CO',
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
    s = s.replace('.', '').replace(',', '.') if ',' in s else s
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
    colunas_orig = list(df.columns)
    cols_norm = {_sem_acento(c): c for c in df.columns}

    # Caso 1: formato largo do histórico fornecido (Hora, Submercado, datas...)
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

    # Caso 2: formato tabular já longo
    mapa = {k: _sem_acento(k) for k in colunas_orig}
    rev = {v: k for k, v in mapa.items()}
    poss_data = next((rev[k] for k in rev if k in {'data', 'dt', 'date'}), None)
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

    raise ValueError(f'Formato de PLD não reconhecido. Colunas: {list(df.columns)[:12]}...')


def carregar_historico_base(caminho: Path | None = None) -> pd.DataFrame:
    candidatos = [caminho] if caminho else []
    candidatos += CANDIDATOS_BRUTO
    for cand in candidatos:
        if cand and Path(cand).exists():
            bruto = ler_bruto(Path(cand))
            hist = normalizar_bruto(bruto)
            if not hist.empty:
                return hist
    raise FileNotFoundError('Não encontrei o arquivo histórico do PLD. Coloque-o em dados/pld_historico_bruto.csv')


def carregar_base_consolidada(caminho: Path = LONGO) -> pd.DataFrame:
    """Prioriza base histórica persistente, preservando atualizações anteriores."""
    if caminho.exists():
        df = pd.read_csv(caminho, sep=';', decimal=',', encoding='utf-8-sig')
        cols = ['data', 'hora', 'submercado', 'pld']
        if not set(cols).issubset(df.columns):
            raise ValueError('Base consolidada PLD tem colunas inesperadas')
        df = df[cols].copy()
        df['data'] = pd.to_datetime(df['data'], errors='raise')
        df['hora'] = pd.to_numeric(df['hora'], errors='raise').astype(int)
        df['pld'] = pd.to_numeric(df['pld'], errors='raise')
        df['submercado'] = df['submercado'].map(_padronizar_submercado)
        df = df.sort_values(['data','hora','submercado']).drop_duplicates(
            ['data','hora','submercado'], keep='last')
        return df
    return carregar_historico_base()


def normalizar_csv_ccee(df: pd.DataFrame, ano: int) -> pd.DataFrame:
    """Converte CSV anual PLD_HORARIO do catálogo público CCEE em data/hora/sub/PLD."""
    nomes = {str(c).strip().upper(): c for c in df.columns}
    campos = {'DIA', 'HORA', 'SUBMERCADO', 'PLD_HORA'}
    if not campos.issubset(nomes):
        raise ValueError('Layout CCEE alterado: faltam ' + str(sorted(campos - set(nomes))))
    out = df[[nomes[c] for c in ['DIA','HORA','SUBMERCADO','PLD_HORA']]].copy()
    out.columns = ['data','hora','submercado','pld']
    out['data'] = pd.to_datetime(out['data'], dayfirst=True, errors='coerce')
    out['hora'] = pd.to_numeric(out['hora'], errors='coerce')
    out['pld'] = out['pld'].map(_to_num)
    out['submercado'] = out['submercado'].map(_padronizar_submercado)
    out = out.dropna(subset=['data','hora','pld']).copy()
    out = out[(out['data'].dt.year == ano) & out['submercado'].isin(ORDEM)].copy()
    if out.empty:
        raise ValueError(f'Arquivo anual CCEE {ano} sem dados horários reconhecidos')
    out['hora'] = out['hora'].astype(int)
    # Em algumas bases comerciais a hora é 1-24; padronizamos para 0-23.
    if out['hora'].min() == 1 and out['hora'].max() == 24:
        out['hora'] = out['hora'] - 1
    if not out['hora'].between(0,23).all():
        raise ValueError('Horas fora da faixa 0-23 no CSV CCEE')
    out['pld'] = out['pld'].astype(float)
    if out.duplicated(['data','hora','submercado']).any():
        raise ValueError('CSV CCEE: datas/horas/submercados duplicados')
    return out[['data','hora','submercado','pld']]


def buscar_ons_ccee(ano: int) -> pd.DataFrame | None:
    """Baixa recurso anual da API CKAN de Dados Abertos da CCEE.

    Descobre a URL pela API, sem URL rígida e sem interação com o navegador.
    Não faz requisições de dados de anos históricos já preservados.
    """
    import requests
    api = 'https://dadosabertos.ccee.org.br/api/3/action/package_show'
    session = requests.Session()
    session.headers.update({'User-Agent': 'Painel-Hidrologico-PLD/1.0'})
    resposta = session.get(api, params={'id': 'pld_horario'}, timeout=60)
    resposta.raise_for_status()
    meta = resposta.json()
    if not meta.get('success'):
        raise RuntimeError('CCEE CKAN API retornou sucesso=false')
    nome = f'pld_horario_{ano}'
    recursos = meta['result'].get('resources', [])
    recurso = next((r for r in recursos if r.get('name','').strip().lower() == nome), None)
    if not recurso:
        print(f'CCEE: recurso anual {nome} ainda indisponível; preservando histórico.')
        return None
    url = recurso.get('url')
    if not url or not url.startswith('https://'):
        raise ValueError('CCEE retornou endereço inválido para CSV')
    print(f'CCEE: baixando {nome} (id={recurso.get("id")})')
    r = session.get(url, timeout=120)
    r.raise_for_status()
    if not r.content or len(r.content) < 100:
        raise ValueError('CSV anual CCEE vazio ou truncado')
    from io import BytesIO
    import csv
    amostra = r.content[:10000].decode('utf-8-sig', errors='replace')
    sep = ';' if amostra.splitlines()[0].count(';') >= amostra.splitlines()[0].count(',') else ','
    try:
        df = pd.read_csv(BytesIO(r.content), sep=sep, dtype=str, encoding='utf-8-sig', low_memory=False)
    except UnicodeDecodeError:
        df = pd.read_csv(BytesIO(r.content), sep=sep, dtype=str, encoding='latin1', low_memory=False)
    result = normalizar_csv_ccee(df, ano)
    print(f'CCEE {ano}: {len(result):,} preços horários; até {result.data.max().date()}.')
    return result


def atualizar_com_online(historico: pd.DataFrame, usar_online: bool) -> pd.DataFrame:
    if not usar_online:
        print('PLD: execução offline, usando histórico consolidado.')
        return historico
    hoje = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    # Durante janeiro e fevereiro, revisa também o arquivo do ano anterior.
    anos = [hoje.year - 1, hoje.year] if hoje.month <= 2 else [hoje.year]
    partes = []
    for ano in anos:
        try:
            extra = buscar_ons_ccee(ano)
            if extra is not None:
                partes.append(extra)
        except Exception as exc:
            warnings.warn(f'CCEE: consulta ao PLD de {ano} falhou ({exc}). Histórico existente preservado.')
    if partes:
        hist = pd.concat([historico, *partes], ignore_index=True)
        hist = hist.sort_values(['data', 'hora', 'submercado']).drop_duplicates(
            subset=['data', 'hora', 'submercado'], keep='last')
        return hist
    print('AVISO: PLD sem novos arquivos confirmados na CCEE nesta execução; dados anteriores preservados.')
    return historico


def inicio_semana_operativa(datas: pd.Series) -> pd.Series:
    """Início no sábado, término na sexta, incluindo semanas parciais."""
    data = pd.to_datetime(datas).dt.normalize()
    return data - pd.to_timedelta((data.dt.weekday - 5) % 7, unit='D')


def codigo_semana(ini: pd.Timestamp) -> str:
    """SNNAAAA: semanas numeradas pelos sábados dentro do ano de início."""
    ini = pd.Timestamp(ini)
    primeiro_dia = pd.Timestamp(ini.year, 1, 1)
    primeiro_sabado = primeiro_dia + pd.Timedelta(days=(5 - primeiro_dia.weekday()) % 7)
    numero = (ini - primeiro_sabado).days // 7 + 1
    if numero <= 0:
        raise ValueError('Início da semana não corresponde ao ano')
    return f'S{numero:02d}{ini.year}'


def agregar_anual(hist: pd.DataFrame) -> pd.DataFrame:
    df = hist.assign(ano=hist['data'].dt.year)
    df = df.groupby(['ano', 'submercado'], as_index=False)['pld'].mean()
    return df.pivot(index='ano', columns='submercado', values='pld').reindex(columns=ORDEM).reset_index()


def agregar_mensal(hist: pd.DataFrame) -> pd.DataFrame:
    df = hist.copy()
    df['mes'] = df['data'].dt.to_period('M')
    df = df.groupby(['mes', 'submercado'], as_index=False)['pld'].mean()
    df = df.pivot(index='mes', columns='submercado', values='pld').reindex(columns=ORDEM).reset_index()
    df['label'] = df['mes'].dt.to_timestamp().dt.strftime('%b/%Y')
    mapa_meses = {1:'jan',2:'fev',3:'mar',4:'abr',5:'mai',6:'jun',7:'jul',8:'ago',9:'set',10:'out',11:'nov',12:'dez'}
    df['label'] = df['mes'].apply(lambda m: f'{mapa_meses[m.month]}/{m.year}')
    return df


def agregar_semanal(hist: pd.DataFrame) -> pd.DataFrame:
    df = hist.copy()
    df['inicio_semana'] = inicio_semana_operativa(df['data'])
    df = df.groupby(['inicio_semana', 'submercado'], as_index=False)['pld'].mean()
    df = df.pivot(index='inicio_semana', columns='submercado', values='pld').reindex(columns=ORDEM).reset_index()
    df['label'] = df['inicio_semana'].map(codigo_semana)
    df['fim_semana'] = df['inicio_semana'] + pd.Timedelta(days=6)
    return df


def agregar_horario(hist: pd.DataFrame, dia: pd.Timestamp) -> pd.DataFrame:
    df = hist.loc[hist['data'] == dia]
    tabela = df.pivot_table(index='hora', columns='submercado', values='pld', aggfunc='mean')
    return tabela.reindex(index=range(24), columns=ORDEM).reset_index()


def configurar_layout(fig: go.Figure, eixo_x: str, horizontal: bool = False) -> go.Figure:
    fig.update_layout(
        height=490 if horizontal else 510,
        paper_bgcolor='white', plot_bgcolor='white',
        font=dict(family=FONTE, size=13, color='#263142'),
        showlegend=False,  # Legenda interativa é HTML, com espaço próprio fora do Plotly.
        hovermode='x unified',
        margin=dict(l=90, r=30, t=24, b=80),
        xaxis=dict(
            title=eixo_x, showgrid=False, showline=True,
            linecolor='#50627a', tickcolor='#50627a', ticks='outside',
            ticklen=7, tickwidth=1.2, zeroline=False,
            minor=dict(ticks='outside', ticklen=3, tickcolor='#98a2b3', showgrid=False),
            automargin=True,
        ),
        yaxis=dict(
            title='Preço (R$/MWh)', showgrid=False, showline=True,
            linecolor='#50627a', tickcolor='#50627a', ticks='outside',
            ticklen=7, tickwidth=1.2, zeroline=False, rangemode='tozero',
            minor=dict(ticks='outside', ticklen=3, tickcolor='#98a2b3', showgrid=False),
            automargin=True,
        ),
    )
    return fig


def criar_figura(df: pd.DataFrame, eixo_x: str, coluna_x: str, granularidade: str) -> go.Figure:
    fig = go.Figure()
    numero_pontos = len(df)
    for sub in ORDEM:
        valores = pd.to_numeric(df[sub], errors='coerce')
        fig.add_trace(go.Scatter(
            x=df[coluna_x], y=valores,
            name=sub, mode='lines+markers',
            line=dict(color=CORES[sub], width=2.8, shape='spline', smoothing=0.4),
            marker=dict(color=CORES[sub], size=6 if numero_pontos <= 24 else 3),
            hovertemplate='%{y:,.2f} R$/MWh<extra>' + sub + '</extra>',
        ))
    configurar_layout(fig, eixo_x, horizontal=granularidade == 'hora')
    if granularidade == 'ano':
        fig.update_xaxes(type='category')
    elif granularidade == 'mes':
        marcas = list(range(0, len(df), max(1, len(df)//12)))
        fig.update_xaxes(type='category', tickmode='array',
                         tickvals=[str(df.iloc[i]['label']) for i in marcas],
                         ticktext=[str(df.iloc[i]['label']) for i in marcas], tickangle=-40)
    elif granularidade == 'semana':
        marcas = list(range(0, len(df), max(1, len(df)//14)))
        fig.update_xaxes(type='category', tickmode='array',
                         tickvals=[str(df.iloc[i]['label']) for i in marcas],
                         ticktext=[str(df.iloc[i]['label']) for i in marcas], tickangle=-45)
    else:
        fig.update_xaxes(type='linear', tickmode='linear', tick0=0, dtick=1, range=[-0.5,23.5])
    return fig


def legenda_html(plot_id: str) -> str:
    """Legenda externa com clique para alternar cada submercado sem sobreposição."""
    from html import escape
    partes = [f'<div class="legenda-externa" role="group" aria-label="Séries do gráfico {escape(plot_id)}">']
    for i, sub in enumerate(ORDEM):
        partes.append(
            f'<button type="button" class="legenda-item" data-plot="{plot_id}" '
            f'data-indice="{i}" aria-pressed="true">'
            f'<span class="legenda-linha" style="--cor-serie:{CORES[sub]}"></span>{escape(sub)}</button>'
        )
    partes.append('</div>')
    return ''.join(partes)


def _horarios_por_dia(hist: pd.DataFrame) -> dict[str, list[list[float | None]]]:
    """Conteúdo compacto de cada dia para trocar a data diretamente no navegador."""
    mapa = {}
    for dia, bloco in hist.groupby('data', sort=True):
        tabela = bloco.pivot(index='hora', columns='submercado', values='pld')
        tabela = tabela.reindex(index=range(24), columns=ORDEM)
        por_sub = []
        for sub in ORDEM:
            serie = pd.to_numeric(tabela[sub], errors='coerce')
            por_sub.append([None if pd.isna(v) else round(float(v), 2) for v in serie])
        mapa[pd.Timestamp(dia).strftime('%Y-%m-%d')] = por_sub
    return mapa


def pagina_pld(hist: pd.DataFrame) -> str:
    from html import escape
    import json
    hist = hist[hist['submercado'].isin(ORDEM)].copy()
    if hist.empty:
        raise ValueError('PLD: nenhum registro de submercados válido')
    hist['data'] = pd.to_datetime(hist['data'])
    hist = hist.sort_values(['data','hora','submercado']).drop_duplicates(
        ['data','hora','submercado'], keep='last')
    ultima = pd.Timestamp(hist['data'].max())
    ultima_br = ultima.strftime('%d/%m/%Y')

    anual = agregar_anual(hist)
    mensal = agregar_mensal(hist)
    semanal = agregar_semanal(hist)
    horario = agregar_horario(hist, ultima)
    primeiro = pd.Timestamp(hist['data'].min()).strftime('%d/%m/%Y')

    configs = [
        ('PLD — Anual', f'Média dos preços horários por ano — último ano parcial até {ultima_br}',
         criar_figura(anual, 'Ano', 'ano', 'ano')),
        ('PLD — Mensal', f'Média dos preços horários por mês — mês corrente parcial até {ultima_br}',
         criar_figura(mensal, 'Mês', 'label', 'mes')),
        ('PLD — Semanal', f'Média dos preços horários por semana operativa (sábado a sexta) — semana atual parcial até {ultima_br}',
         criar_figura(semanal, 'Semana', 'label', 'semana')),
        ('PLD — Horário', f'Curva horária de 0 a 23h — escolha a data desejada',
         criar_figura(horario, 'Hora', 'hora', 'hora')),
    ]
    blocos = []
    for i, (titulo, subtitulo, figura) in enumerate(configs):
        pid = f'plot-pld-{i}'
        grafico = figura.to_html(
            full_html=False, include_plotlyjs=False, div_id=pid,
            config={'responsive': True, 'displaylogo': False},
        )
        controle_data = ''
        if i == 3:
            controle_data = (
                '<div class="controle-data">'
                '<label for="data-pld">Data do PLD horário:</label>'
                f'<input type="date" id="data-pld" value="{ultima.strftime("%Y-%m-%d")}" '
                f'min="{hist["data"].min().strftime("%Y-%m-%d")}" max="{ultima.strftime("%Y-%m-%d")}" />'
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
  .painel {{min-width:0;background:#fff;border:1px solid #e1e7ef;border-radius:14px;
            overflow:hidden;display:flex;flex-direction:column}}
  .barra-titulo {{width:100%;background:{COR_VALE};color:#fff;font-weight:700;text-align:center;
                 padding:12px 16px;font-size:18px;border-radius:14px 14px 0 0}}
  .subtitulo {{padding:12px 18px 0;text-align:center;color:#667085;font-size:12px;line-height:1.6}}
  .js-plotly-plot {{width:100%!important}}
  .legenda-externa {{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;
                     gap:10px 18px;padding:4px 16px 15px;min-height:40px}}
  .legenda-item {{border:1px solid #e0e6ee;border-radius:8px;background:#fff;color:#263142;
                  font:inherit;font-size:12px;display:inline-flex;align-items:center;justify-content:center;
                  gap:9px;padding:7px 12px;min-width:92px;cursor:pointer;white-space:nowrap}}
  .legenda-item[aria-pressed="false"] {{opacity:.38}}
  .legenda-linha {{height:0;width:28px;border-top:3px solid var(--cor-serie);display:inline-block;
                  position:relative}}
  .legenda-linha:after {{content:"";position:absolute;left:10px;top:-5px;width:7px;height:7px;
                         background:var(--cor-serie);border-radius:50%}}
  .controle-data {{display:flex;gap:12px;flex-wrap:wrap;align-items:center;
                   justify-content:center;padding:16px 12px 0;font-size:13px}}
  #data-pld {{font:inherit;color:#263142;padding:8px 10px;border:1px solid #c6d0dc;border-radius:8px}}
  #status-data-pld {{color:#667085}}
  .fonte-dados {{padding:7px 18px 18px;color:#667085;font-size:11px;line-height:1.5;margin-top:auto}}
  @media(max-width:750px) {{ .area {{padding:0;gap:10px}} .painel {{border-radius:8px}}
    .legenda-externa {{gap:7px}} .legenda-item {{min-width:76px;padding:7px 8px}} }}
</style></head><body>
<main class="area">{''.join(blocos)}</main>
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
  const plotHorario = document.getElementById('plot-pld-3');
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
  inputData.addEventListener('change', atualizarDia);
  function comunicarAlturaPLD() {{
    if(window.parent !== window) window.parent.postMessage(
      {{tipo:'altura-painel-ons', id:'pld', altura:document.querySelector('main').offsetHeight+12}}, '*');
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
    data_max = pd.Timestamp(hist['data'].max()).strftime('%d/%m/%Y')
    print(f'Histórico PLD: {len(hist):,} registros horários.')
    print(f'Última data do PLD: {data_max}')
    print('Página:', PAGINA_PLD)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--somente-local', action='store_true', help='Usa apenas o histórico local')
    p.add_argument('--historico-base', type=Path, help='Arquivo base do histórico do PLD')
    args = p.parse_args()

    hist = carregar_historico_base(args.historico_base) if args.historico_base else carregar_base_consolidada()
    hist = atualizar_com_online(hist, usar_online=not args.somente_local)
    salvar(hist)


if __name__ == '__main__':
    main()
