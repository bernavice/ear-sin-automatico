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


def preparar_diarios(hist: pd.DataFrame) -> pd.DataFrame:
    d = hist.groupby(['data', 'submercado'], as_index=False)['pld'].mean()
    d['ano'] = d['data'].dt.year
    d['mes'] = d['data'].dt.month
    d['dia'] = d['data'].dt.day
    d['weekday'] = d['data'].dt.weekday
    return d


def semana_operativa(dt: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    delta = (dt.weekday() - 5) % 7  # sábado=5
    inicio = (dt - pd.Timedelta(days=delta)).normalize()
    fim = inicio + pd.Timedelta(days=6)
    return inicio, fim


def pivotar(df: pd.DataFrame, x: str, y: str) -> pd.DataFrame:
    p = df.pivot(index=x, columns='submercado', values=y).reset_index()
    for sm in ORDEM:
        if sm not in p.columns:
            p[sm] = pd.NA
    return p[[x] + ORDEM]


def serie_anual(diarios: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    data_max = diarios['data'].max()
    ano = data_max.year
    base = diarios[diarios['ano'] == ano].copy()
    mensal = base.groupby(['mes', 'submercado'], as_index=False)['pld'].mean()
    pivot = pivotar(mensal, 'mes', 'pld')
    pivot['label'] = pivot['mes'].map({1:'Jan',2:'Fev',3:'Mar',4:'Abr',5:'Mai',6:'Jun',7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'})
    return pivot, f'PLD anual de {ano} (média mensal; mês corrente acumulado até {data_max.strftime("%d/%m/%Y")})'


def serie_mensal(diarios: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    data_max = diarios['data'].max()
    base = diarios[(diarios['ano'] == data_max.year) & (diarios['mes'] == data_max.month)].copy()
    base = base.groupby(['dia', 'submercado'], as_index=False)['pld'].mean()
    pivot = pivotar(base, 'dia', 'pld')
    return pivot, f'PLD mensal de {data_max.strftime("%m/%Y")} (média diária até {data_max.strftime("%d/%m/%Y")})'


def serie_semanal(diarios: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    data_max = diarios['data'].max()
    ini, fim = semana_operativa(pd.Timestamp(data_max))
    base = diarios[(diarios['data'] >= ini) & (diarios['data'] <= min(fim, pd.Timestamp(data_max)))].copy()
    base = base.groupby(['data', 'submercado'], as_index=False)['pld'].mean()
    base['label'] = base['data'].dt.strftime('%d/%m') + ' · ' + base['data'].dt.weekday.map(PT_DIAS)
    pivot = pivotar(base, 'label', 'pld')
    return pivot, f'PLD semanal (semana operativa {ini.strftime("%d/%m")} a {fim.strftime("%d/%m")}; acumulado até {data_max.strftime("%d/%m/%Y")})'


def serie_horaria(hist: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    data_max = hist['data'].max()
    base = hist[hist['data'] == data_max].copy()
    base = base.groupby(['hora', 'submercado'], as_index=False)['pld'].mean()
    pivot = pivotar(base, 'hora', 'pld')
    return pivot, f'PLD horário de {pd.Timestamp(data_max).strftime("%d/%m/%Y")}'


def configurar_layout(fig: go.Figure, titulo: str, x_title: str, y_title: str, tipo_x: str='categoria') -> go.Figure:
    fig.update_layout(
        title=dict(text='', x=.5),
        height=540,
        paper_bgcolor='white', plot_bgcolor='white',
        font=dict(family=FONTE, color='#263142', size=14),
        hovermode='x unified', margin=dict(l=68, r=28, t=18, b=110),
        legend=dict(orientation='h', x=.5, xanchor='center', y=-.22, yanchor='top', font=dict(size=12)),
        xaxis=dict(title=x_title, type='category' if tipo_x == 'categoria' else 'linear',
                   showgrid=False, showline=True, linecolor='#50627a', tickcolor='#50627a',
                   ticks='outside', ticklen=8, tickwidth=1.3, mirror=False, showticklabels=True,
                   minor=dict(ticklen=4, tickcolor='#50627a', showgrid=False), automargin=True),
        yaxis=dict(title=y_title, showgrid=False, showline=True, linecolor='#50627a', tickcolor='#50627a',
                   ticks='outside', ticklen=8, tickwidth=1.3, mirror=False, zeroline=False, automargin=True),
    )
    return fig


def montar_figura(df: pd.DataFrame, x: str, titulo: str, x_title: str, y_title: str) -> go.Figure:
    fig = go.Figure()
    for sm in ORDEM:
        fig.add_trace(go.Scatter(
            x=df[x], y=df[sm], mode='lines+markers', name=sm,
            line=dict(width=2.5, color=CORES[sm], shape='spline', smoothing=.65),
            marker=dict(size=5, color=CORES[sm])
        ))
    return configurar_layout(fig, titulo, x_title, y_title)


def pagina_pld(hist: pd.DataFrame) -> str:
    diarios = preparar_diarios(hist)
    anual, subtitulo_anual = serie_anual(diarios)
    mensal, subtitulo_mensal = serie_mensal(diarios)
    semanal, subtitulo_semanal = serie_semanal(diarios)
    horario, subtitulo_horario = serie_horaria(hist)
    ult_data = pd.Timestamp(hist['data'].max()).strftime('%d/%m/%Y')

    configs = [
        ('PLD — Anual', subtitulo_anual, montar_figura(anual, 'label', 'PLD — Anual', 'Mês', 'R$/MWh')),
        ('PLD — Mensal', subtitulo_mensal, montar_figura(mensal, 'dia', 'PLD — Mensal', 'Dia do mês', 'R$/MWh')),
        ('PLD — Semanal', subtitulo_semanal, montar_figura(semanal, 'label', 'PLD — Semanal', 'Dia da semana operativa', 'R$/MWh')),
        ('PLD — Horário', subtitulo_horario, montar_figura(horario, 'hora', 'PLD — Horário', 'Hora', 'R$/MWh')),
    ]
    blocos = []
    for i, (titulo, subtitulo, fig) in enumerate(configs):
        fragmento = fig.to_html(full_html=False, include_plotlyjs=False,
                                div_id=f'plot-pld-{i}', config={'responsive': True, 'displaylogo': False})
        blocos.append(
            f'<section class="painel">'
            f'<div class="barra-titulo">{titulo}</div>'
            f'<div class="subtitulo">{subtitulo}</div>'
            f'{fragmento}'
            f'<footer class="fonte-dados">Fonte: CCEE &nbsp;|&nbsp; Dados atualizados até: {ult_data}</footer></section>'
        )
    return f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PLD | Painel</title>
<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"></script>
<style>
  * {{box-sizing:border-box}}
  body {{margin:0;background:#fff;color:#263142;font-family:{FONTE}}}
  .area {{display:grid;grid-template-columns:minmax(0,1fr);gap:15px;padding:0 12px 20px}}
  .painel {{min-width:0;background:#fff;border:1px solid #e1e7ef;border-radius:14px;overflow:hidden;display:flex;flex-direction:column}}
  .barra-titulo {{display:block;width:100%;margin:0;background:{COR_VALE};color:#fff;font-weight:700;text-align:center;padding:12px 16px;font-size:18px;line-height:1.2;border-radius:14px 14px 0 0;}}
  .subtitulo {{padding:10px 16px 0;color:#667085;font-size:12px;line-height:1.45;text-align:center}}
  .js-plotly-plot {{width:100%!important}}
  .fonte-dados {{padding:2px 18px 18px;color:#667085;font-size:11px;line-height:1.5;margin-top:auto}}
  @media(max-width:850px) {{ .area {{padding:0;gap:10px}} .painel {{border-radius:8px}} }}
</style></head><body>
<main class="area">{''.join(blocos)}</main>
<script>
  function comunicarAlturaPLD() {{
    if (window.parent !== window) window.parent.postMessage(
      {{tipo:'altura-painel-ons', id:'pld', altura:document.querySelector('main').offsetHeight+12}}, '*');
  }}
  window.addEventListener('load', comunicarAlturaPLD);
  window.addEventListener('resize', comunicarAlturaPLD);
  if (window.ResizeObserver) new ResizeObserver(comunicarAlturaPLD).observe(document.querySelector('main'));
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
