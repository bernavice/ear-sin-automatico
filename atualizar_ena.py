"""Atualiza ENA bruta do ONS, calcula SIN ponderado e gera o painel EAR/ENA.

Fonte: ONS Dados Abertos — ENA Diário por Subsistema.
Dados diários consolidados ficam no GitHub. A ENA mensal é a MÉDIA
dos valores diários até a última data disponível do mês, inclusive o mês
em andamento. A EAR, diferentemente, usa o valor do último dia disponível.

A execução 'python atualizar_ena.py --somente-local' regenera páginas sem internet.
Execute este script depois de atualizar_ear.py.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
import re
import unicodedata
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs_version
import requests

RAIZ = Path(__file__).resolve().parent
DIARIO = RAIZ / 'dados' / 'ena_diaria.csv'
MENSAL = RAIZ / 'dados' / 'ena_mensal.csv'
PAGINA_ENA = RAIZ / 'graficos' / 'ena.html'
PAGINA_INICIAL = RAIZ / 'graficos' / 'index.html'
PAGINA_PLD = RAIZ / 'graficos' / 'pld.html'
PLD_LONG = RAIZ / 'dados' / 'pld_historico_long.csv'
PAGINA_EAR = RAIZ / 'graficos' / 'ear_sin.html'
URL_ONS = ('https://ons-aws-prod-opendata.s3.amazonaws.com/'
           'dataset/ena_subsistema_di/ENA_DIARIO_SUBSISTEMA_{ano}.csv')

SUBS = {
    'SE/CO': ('enaseabs', 'enasepp', 'enasemlt'),
    'Sul': ('enasuabs', 'enasupp', 'enasunmlt'),
    'Norte': ('enanabs', 'enanpp', 'enannmlt'),
    'Nordeste': ('enaneabs', 'enanepp', 'enanenmlt'),
}
COLUNAS = [
    'data', 'enaseabs', 'enasepp', 'enasuabs', 'enasupp',
    'enaneabs', 'enanepp', 'enanabs', 'enanpp',
    'enasinabs', 'enasinpp', 'enasemlt', 'enasunmlt',
    'enanenmlt', 'enannmlt', 'enasinmlt',
]
PERCENTUAIS = {'SIN': 'enasinpp', 'SE/CO': 'enasepp',
                'Sul': 'enasupp', 'Norte': 'enanpp', 'Nordeste': 'enanepp'}

MESES = list(range(1, 13))
NOMES_MESES = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
               'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
FONTE = 'Century Gothic, CenturyGothic, Arial, sans-serif'
COR_VALE = '#007E80'
BANNER_BASE64 = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAABUcAAAB7CAYAAABXXzGLAABBmklEQVR4nO3dd3Qc9bn/8ffsqsvq1VaxbNly7xgbbKpjY9OTQEIKSUgg7Xdz77k3PfeGhBRIICGkkk5CQguEFgIB3LuNC+6yZclVvfey2p3fH2s5Ljuzq7qS9/M6R4eDZ6fs7HynPPN8v4/B/d82EREREREREREREQkxjmBvgIiIiIiIiIiIiEgwKDgqIiIiIiIiIiIiIUnBUREREREREREREQlJCo6KiIiIiIiIiIhISFJwVEREREREREREREKSgqMiIiIiIiIiIiISkhQcFRERERERERERkZCk4KiIiIiIiIiIiIiEJAVHRUREREREREREJCQpOCoiIiIiIiIiIiIhScFRERERERERERERCUkKjoqIiIiIiIiIiEhIUnBUREREREREREREQpKCoyIiIiIiIiIiIhKSFBwVERERERERERGRkKTgqIiIiIiIiIiIiIQkBUdFREREREREREQkJCk4KiIiIiIiIiIiIiFJwVEREREREREREREJSQqOioiIiIiIiIiISEhScFRERERERERERERCkoKjIiIiIiIiIiIiEpIUHBUREREREREREZGQFBbsDRhUpmk/3TCGZjtERERERERERERk2Lkkg6OGCYbDIMzhxOkwcDgc3hRZwwDTxG2adJseut0eTI8HU0FSERERERERERGRkHNpBEfPZIhGhIURHxFBUnQ0OQkJTElNZXxyEmPi44iLiCTS6aTT7aaytYWjtXXsLq/gSG0ttW1tNHd24unJNFWwVERERERERERE5JI3coOjpokBGA4HqaNimZqaxtVjc5mflcXk1FTSYmOIcIYR5nTgMAzODXd6zmSPdnR3U97UzDtlZbxeVMSWk6cobWqi2+MZ+QHSC4cUGOnfR0REREREREREZIAZ3P9tPwNzDi8G4DQMkqKjmJ6RyfXj8rgiJ5tp6Rmkx8Z4A6a9CASagGmatLpcHKisYs2x47xeVMS+ygqaOjpHRpf7nkCxYRDhdBIdHk5UmJNwhxMTcLnddHR30+Zy4fJ4ME1TwVIREREREREREQl5IyY4agCYJrEREVw7Po+PzpzFtePySIqKIsLpHJB19AQSK1ta+PvBQ/xh124OVVfh9gzPYKJxJjvU6XCQm5jA5NQ0ZmVmMCElhez4OBIiowCo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Ll49WIhHGaMiEkLmjLbOGgVljoqIiIiIiMjwoWr1/WQAo+NG8YNlS/nw9Ok4HA663R5eKTzMf/zzn1S1tKg7vYiIiIiIiIiIyDCkzNF+MoGKpma+t24dW06dxu3xsKuinAc3bFBgVEREREREREREZBhTcHQAmIbB0Zo6HtqwkdeLinh44yb2VVQoMCoiIiIiIiIiIjKMqVv9AHKYJrjdmA4npkOBURERERERERERkeEsLNgbcCnxGAaEaZeKiIiIiIiIiIiMBOpWLyIiIiIiIiIiIiFJwVEREREREREREREJSQqOioiIiIiIiIiISEhScFRERERERERERERCkoKjIiIiIiIiIiIiEpIUHBUREREREREREZGQpOCoiIiIiIiIiIiIhCQFR0VERERERERERCQkKTgqIiIiIiIiIiIiIUnBUREREREREREREQlJCo6KiIiIiIiIiIhISFJwVEREREREREREREKSgqMiIiIiIiIiIiISkhQcFRERERERERERkZCk4KiIiIiIiIiIiIiEJAVHRUREREREREREJCQpOCoiIiIiIiIiIiIhScFRERERERERERERCUkKjoqIiIiIiIiIiEhIUnBUREREREREREREQpKCoyIiIiIiIiIiIhKSFBwVERERERERERGRkKTgqIiIiIiIiIiIiIQkBUdFREREREREREQkJCk4KiIiIiIiIiIiIiFJwVEREREREREREREJSWGWU0xzCDdDRAaVYQR7C0REREREREREhh3r4KiCKSIiIiIiIiIiInIJU7d6ERERERERERERCUkKjoqIiIiIiIiIiEhIUnBUREREREREREREQtL/Bz/MUCpgriUvAAAAAElFTkSuQmCC'


class ArquivoNaoPublicado(FileNotFoundError):
    pass


def numero(v: object) -> float:
    texto = str(v).strip().replace(' ', '')
    if not texto or texto.lower() in {'nan', 'none'}:
        raise ValueError('Valor ENA vazio no ONS')
    if ',' in texto and '.' in texto:
        if texto.rfind(',') > texto.rfind('.'):
            texto = texto.replace('.', '').replace(',', '.')
        else:
            texto = texto.replace(',', '')
    else:
        texto = texto.replace(',', '.')
    return float(texto)


def normalizar(v: object) -> str:
    txt = unicodedata.normalize('NFKD', str(v or ''))
    return ''.join(c for c in txt if not unicodedata.combining(c)).upper().strip()


def subsistema(id_sub: object, nome_sub: object) -> str | None:
    codigo = re.sub('[^A-Z0-9]', '', normalizar(id_sub))
    nome = normalizar(nome_sub)
    if codigo in {'SE', 'SECO', 'SECOESTE'} or 'SUDESTE' in nome:
        return 'SE/CO'
    if codigo in {'S', 'SU'} or nome == 'SUL':
        return 'Sul'
    if codigo == 'NE' or 'NORDESTE' in nome:
        return 'Nordeste'
    if codigo == 'N' or nome == 'NORTE':
        return 'Norte'
    return None


def carregar_historico() -> pd.DataFrame:
    if not DIARIO.exists():
        raise FileNotFoundError('Falta dados/ena_diaria.csv: envie o histórico inicial do ZIP.')
    df = pd.read_csv(DIARIO, sep=';', decimal=',', encoding='utf-8-sig')
    faltantes = sorted(set(COLUNAS) - set(df.columns))
    if faltantes:
        raise ValueError(f'Histórico ENA incompleto: faltam {faltantes}')
    df = df[COLUNAS].copy()
    df['data'] = pd.to_datetime(df['data'], errors='raise')
    for col in COLUNAS[1:]:
        df[col] = pd.to_numeric(df[col], errors='raise')
    if df['data'].duplicated().any():
        raise ValueError('Histórico ENA possui datas repetidas')
    return df.sort_values('data').reset_index(drop=True)


def carregar_ons(ano: int, csv_teste: Path | None = None) -> pd.DataFrame:
    if csv_teste is not None:
        raw = pd.read_csv(csv_teste, sep=';', dtype=str, encoding='utf-8-sig')
    else:
        url = URL_ONS.format(ano=ano)
        print('Consultando ENA ONS:', url)
        try:
            resposta = requests.get(url, timeout=90)
            if resposta.status_code == 404:
                raise ArquivoNaoPublicado(f'ENA ONS ainda não tem CSV de {ano}')
            resposta.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f'Erro consultando ONS para ENA em {ano}: {exc}') from exc
        raw = pd.read_csv(BytesIO(resposta.content), sep=';', dtype=str, encoding='utf-8-sig')

    raw.columns = [c.strip().lower() for c in raw.columns]
    campos = {'id_subsistema', 'nom_subsistema', 'ena_data',
              'ena_bruta_regiao_mwmed', 'ena_bruta_regiao_percentualmlt'}
    if not campos.issubset(raw.columns):
        raise ValueError('Layout de ENA do ONS mudou. Faltam colunas: '
                         + str(sorted(campos - set(raw.columns))))

    raw['data'] = pd.to_datetime(raw['ena_data'], errors='coerce')
    raw['subsistema'] = [subsistema(cod, nome) for cod, nome in
                         zip(raw['id_subsistema'], raw['nom_subsistema'])]
    raw = raw.dropna(subset=['data', 'subsistema']).copy()
    raw = raw.loc[raw['data'].dt.year == ano].copy()
    if raw.empty:
        raise ValueError(f'ONS ENA: nenhum registro válido de {ano}')
    if raw.duplicated(['data', 'subsistema']).any():
        raise ValueError('ONS ENA: subsistemas duplicados na mesma data')

    raw['abs'] = raw['ena_bruta_regiao_mwmed'].map(numero)
    raw['pp'] = raw['ena_bruta_regiao_percentualmlt'].map(numero)
    if (~np.isfinite(raw[['abs', 'pp']].to_numpy(dtype=float))).any():
        raise ValueError('ONS ENA: valor numérico não finito')
    if (raw['abs'] < 0).any():
        raise ValueError('ONS ENA: ENA bruta negativa; verificar fonte')
    # MLT implícita, usada para ponderar corretamente a ENA do SIN.
    # Percentual nulo/negativo não permite inferir MLT desta fonte diária.
    raw['mlt'] = np.where(raw['pp'] > 0,
                          100 * raw['abs'] / raw['pp'], np.nan)
    raw.loc[(raw['abs'] <= 0) | (raw['pp'] <= 0) | (raw['mlt'] <= 0), 'mlt'] = np.nan

    valida = raw.groupby('data').agg(n=('subsistema', 'nunique'),
                                    validos=('mlt', 'count'))
    completas = valida.index[(valida['n'] == 4) & (valida['validos'] == 4)]
    raw = raw.loc[raw['data'].isin(completas)].copy()
    if raw.empty:
        raise ValueError('ONS ENA: nenhuma data com os quatro subsistemas completos')

    linhas = []
    for dia, grupo in raw.groupby('data', sort=True):
        d = {'data': dia}
        for nome, (colabs, colpp, colmlt) in SUBS.items():
            linha = grupo.loc[grupo['subsistema'] == nome].iloc[0]
            d[colabs] = float(linha['abs'])
            d[colpp] = float(linha['pp'])
            d[colmlt] = float(linha['mlt'])
        d['enasinabs'] = sum(d[item[0]] for item in SUBS.values())
        d['enasinmlt'] = sum(d[item[2]] for item in SUBS.values())
        d['enasinpp'] = 100 * d['enasinabs'] / d['enasinmlt']
        linhas.append(d)
    df = pd.DataFrame(linhas)[COLUNAS]
    print(f'ENA ONS: {len(df)} dias completos, último {df["data"].max().date()}')
    return df


def coletar_recente(data_execucao: date, dias_revisao: int,
                    csv_teste: Path | None = None) -> pd.DataFrame:
    inicio_ano = date(data_execucao.year, 1, 1)
    em_virada = (data_execucao - inicio_ano).days <= max(dias_revisao, 45)
    anos = ([data_execucao.year - 1] if em_virada and csv_teste is None else []) + [data_execucao.year]
    partes = []
    for ano in anos:
        try:
            partes.append(carregar_ons(ano, csv_teste if ano == data_execucao.year else None))
        except ArquivoNaoPublicado:
            if ano == data_execucao.year and em_virada and partes:
                print(f'ENA do ano {ano} ainda não publicada, preservando histórico.')
                continue
            raise
    return pd.concat(partes, ignore_index=True).sort_values('data').drop_duplicates('data', keep='last')


def atualizar(historico: pd.DataFrame, ons: pd.DataFrame,
              dias_revisao: int, data_execucao: date | None = None) -> pd.DataFrame:
    hoje = data_execucao or datetime.now(ZoneInfo('America/Sao_Paulo')).date()
    ons = ons.loc[ons['data'].dt.date <= hoje].copy()
    if ons.empty:
        print('ENA: ONS não trouxe nenhum registro atual. Preservando histórico.')
        return historico
    limite = historico['data'].max() - timedelta(days=dias_revisao)
    ons = ons.loc[ons['data'] >= limite].copy()
    if ons.empty:
        return historico
    return (pd.concat([historico, ons], ignore_index=True)
            .drop_duplicates('data', keep='last')
            .sort_values('data').reset_index(drop=True))


def montar_mensal(diario: pd.DataFrame) -> pd.DataFrame:
    """Calcula médias mensais de ENA de TODOS os dias disponíveis.

    ENA (MWmed): média aritmética dos valores diários de cada mês.
    ENA (% MLT): média aritmética dos percentuais diários (incluindo o SIN,
    cujo percentual diário já é calculado ponderando pelas MLTs das regiões).
    'data' é a última data disponível do mês, apenas para referência.
    'dias_observados' permite auditar quantos dias entraram no cálculo.
    """
    d = diario.sort_values('data').copy()
    if d['data'].duplicated().any():
        raise ValueError('ENA: há datas duplicadas no histórico diário')
    d['ano'] = d['data'].dt.year
    d['mes'] = d['data'].dt.month
    agregacoes = {
        'data': ('data', 'max'),
        'dias_observados': ('data', 'count'),
        **{col: (col, 'mean') for col in COLUNAS if col != 'data'},
    }
    mensal = d.groupby(['ano', 'mes'], as_index=False).agg(**agregacoes)
    return mensal.sort_values(['ano', 'mes']).reset_index(drop=True)



def cores_anos(ano_corrente: int) -> dict[int, str]:
    return {
        2021: '#D62828',     # vermelho
        2023: '#8ED1FC',     # azul muito claro
        ano_corrente - 1: '#D97706',
        ano_corrente: '#2563EB',
    }


def grafico_ena(mensal: pd.DataFrame, titulo: str, coluna: str,
                grande: bool = False) -> go.Figure:
    ano_corrente = datetime.now(ZoneInfo('America/Sao_Paulo')).year
    ultimo_ano = int(mensal['ano'].max())
    m = mensal[mensal['ano'] >= ultimo_ano - 9]
    selecionados = {ano_corrente, ano_corrente - 1, 2021, 2023}
    cores = cores_anos(ano_corrente)
    extras = ['#64748B', '#2A9D8F', '#7C8C43', '#6D77AE', '#A56B43', '#778899']

    f = go.Figure()
    # Linha de referência tracejada: ENA igual à Média de Longo Termo.
    f.add_trace(go.Scatter(
        x=MESES, y=[100.0] * 12, mode='lines', name='MLT (100%)',
        line=dict(color='#717985', width=2, dash='dash'),
        hovertemplate='Referência: 100% da MLT<extra></extra>',
    ))
    for i, (ano, grupo) in enumerate(m.groupby('ano', sort=True)):
        ano = int(ano)
        visivel = ano in selecionados
        cor = cores.get(ano, extras[i % len(extras)])
        f.add_trace(go.Scatter(
            x=grupo['mes'], y=grupo[coluna],
            name=str(ano), visible=True if visivel else 'legendonly',
            mode='lines+markers',
            line=dict(shape='spline', smoothing=0.75,
                      color=cor, width=3.4 if ano == ano_corrente else 2.4),
            marker=dict(symbol='circle', color=cor,
                        size=7 if ano == ano_corrente else 5),
            customdata=grupo['data'].dt.strftime('%d/%m/%Y'),
            hovertemplate=('<b>%{fullData.name}</b><br>Média mensal até: %{customdata}'
                           '<br>ENA média: %{y:.1f}% MLT<extra></extra>'),
        ))

    marcadores_cruz = [dict(
        type='line', xref='x', yref='paper',
        x0=x, x1=x, y0=-0.018, y1=0.018,
        line=dict(color='#475467', width=1.1), layer='above',
    ) for x in MESES]
    f.update_layout(
        title=dict(text='', x=0.5, xanchor='center'),
        font=dict(family=FONTE, size=12, color='#344054'),
        template='plotly_white', height=580 if grande else 450,
        autosize=True, paper_bgcolor='white', plot_bgcolor='white',
        hovermode='closest', showlegend=False, margin=dict(l=68, r=32, t=18, b=82),
        # Sem título embutido na legenda: ele ocupava a mesma linha dos anos
        # e causava sobreposição nas visualizações com duas colunas.
        legend=dict(orientation='h',
                    x=.5, xanchor='center', y=-.31, yanchor='top',
                    font=dict(size=10), traceorder='normal',
                    itemwidth=72, entrywidth=72, entrywidthmode='pixels',
                    itemsizing='constant'),
        shapes=marcadores_cruz,
        # A fonte é exibida abaixo do gráfico em HTML, nunca na área da legenda.
    )
    f.update_xaxes(
        title_text='Mês', range=[.75, 12.25],
        tickmode='array', tickvals=MESES, ticktext=NOMES_MESES,
        showline=True, linewidth=1.5, linecolor='#475467',
        ticks='', showgrid=False, zeroline=False,
        minor=dict(tickmode='array',
                   tickvals=[x + .5 for x in range(1, 12)],
                   ticks='outside', ticklen=3,
                   tickcolor='#98A2B3', showgrid=False),
    )
    f.update_yaxes(
        title_text='ENA média mensal (% MLT)', rangemode='tozero',
        ticksuffix='%', showline=True, linewidth=1.5,
        linecolor='#475467', ticks='outside', ticklen=6,
        tickcolor='#475467', showgrid=False, zeroline=False,
        minor=dict(ticks='outside', ticklen=3,
                   tickcolor='#98A2B3', showgrid=False),
    )
    return f




def legenda_interativa(fig: go.Figure, plot_id: str) -> str:
    """Legenda HTML independente do Plotly: quebra linhas sem sobreposição."""
    from html import escape
    conteudo = [f'<div class="legenda-externa" role="group" aria-label="Legenda {escape(plot_id)}">']
    for i, traco in enumerate(fig.data):
        nome = escape(traco.name or f'Série {i + 1}')
        visivel = traco.visible not in ('legendonly', False)
        cor = traco.line.color or '#8b96a7'
        if traco.fill == 'toself':
            cor = '#b7bfc8'
        conteudo.append(
            f'<button type="button" class="legenda-item" data-plot="{plot_id}" data-indice="{i}" '
            f'aria-pressed="{str(visivel).lower()}">'
            f'<span class="legenda-linha" style="--cor-serie:{cor}"></span>{nome}</button>'
        )
    conteudo.append('</div>')
    return ''.join(conteudo)

def pagina_ena(mensal: pd.DataFrame) -> str:
    configs = [
        ('SIN', 'enasinpp', True),
        ('SE/CO', 'enasepp', False),
        ('Sul', 'enasupp', False),
        ('Norte', 'enanpp', False),
        ('Nordeste', 'enanepp', False),
    ]
    blocos = []
    ultima_data_ena = pd.Timestamp(mensal['data'].max()).strftime('%d/%m/%Y')
    for i, (nome, coluna, maior) in enumerate(configs):
        titulo_barra = f'ENA — {nome}'
        fig = grafico_ena(mensal, titulo_barra, coluna, grande=maior)
        fragmento = fig.to_html(full_html=False, include_plotlyjs=False,
                                div_id=f'plot-ena-{i}',
                                config={'responsive': True, 'displaylogo': False})
        tipo = 'sin' if maior else 'sub'
        blocos.append(
            f'<section class="painel {tipo}">'
            f'<div class="barra-titulo">{titulo_barra}</div>'
            f'{fragmento}{legenda_interativa(fig, f"plot-ena-{i}")}'
            f'<footer class="fonte-dados">Fonte: ONS &nbsp;|&nbsp; '
            f'Dados atualizados até: {ultima_data_ena}</footer></section>'
        )
    return f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ENA média mensal | SIN e Subsistemas</title>
<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"></script>
<style>
  * {{box-sizing:border-box}}
  body {{margin:0;background:#fff;color:#263142;font-family:{FONTE}}}
  .area {{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px;padding:0 12px 20px}}
  .painel {{min-width:0;background:#fff;border:1px solid #e1e7ef;border-radius:14px;overflow:hidden;display:flex;flex-direction:column}}
  .painel.sin {{grid-column:1/-1}}
  .barra-titulo {{display:block;width:100%;margin:0;background:{COR_VALE};color:#fff;font-weight:700;text-align:center;padding:12px 16px;font-size:18px;line-height:1.2;border-radius:14px 14px 0 0;}}
  .painel.sub .barra-titulo {{font-size:16px;padding:10px 14px;}}
  .js-plotly-plot {{width:100%!important}}
  .legenda-externa {{display:flex;flex-wrap:wrap;justify-content:center;gap:8px 12px;
                      align-items:center;padding:2px 10px 13px;min-height:38px}}
  .legenda-item {{display:inline-flex;align-items:center;justify-content:center;gap:7px;
                 border:1px solid #e0e6ee;border-radius:7px;background:white;color:#344054;
                 padding:6px 9px;white-space:nowrap;cursor:pointer;min-width:68px;
                 font-family:inherit;font-size:11px}}
  .legenda-item[aria-pressed="false"] {{opacity:.34}}
  .legenda-linha {{width:25px;height:0;border-top:3px solid var(--cor-serie);display:inline-block}}

  .fonte-dados {{padding:2px 18px 18px;color:#667085;font-size:11px;line-height:1.5;margin-top:auto;}}
  @media(max-width:850px) {{.area {{grid-template-columns:minmax(0,1fr);padding:0;gap:10px}}
    .painel {{border-radius:7px}} .painel.sin {{grid-column:auto}} }}
</style></head>
<body><main class="area">{''.join(blocos)}</main>
<script>
  document.querySelectorAll('.legenda-item').forEach(function(botao) {{
    botao.addEventListener('click', function() {{
      const grafico=document.getElementById(botao.dataset.plot);
      const indice=Number(botao.dataset.indice);
      const ligado=botao.getAttribute('aria-pressed')==='true';
      botao.setAttribute('aria-pressed', ligado ? 'false' : 'true');
      Plotly.restyle(grafico, {{visible: ligado ? 'legendonly' : true}}, [indice]);
    }});
  }});
  function comunicarAltura() {{
    window.parent.postMessage({{tipo:'altura-painel-ons', id:'ena',
      altura:document.querySelector('main').offsetHeight+12}},'*');
  }}
  window.addEventListener('load',comunicarAltura);
  window.addEventListener('resize',comunicarAltura);
  if(window.ResizeObserver) new ResizeObserver(comunicarAltura).observe(document.querySelector('main'));
</script></body></html>'''


def pagina_abertura(data_ear: str, data_ena: str, data_pld: str | None = None) -> str:
    pld_txt = f' · PLD {data_pld}' if data_pld else ''
    return f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel Hidrológico | EAR, ENA e PLD</title>
<style>
  :root {{font-family:{FONTE};color:#253345;background:#f7f9fc}}
  * {{box-sizing:border-box}}
  body {{margin:0}}
  .topo {{max-width:1450px;margin:auto;padding:6px 24px 14px}}
  .topo .banner {{width:100%;display:block;border:0;border-radius:16px}}
  .topo p {{margin:0;color:#697586;font-size:13px;line-height:1.65}}
  .topo .subtexto {{margin-top:10px}}
  .topo .fonte-atualizacao {{color:#526071;margin-top:3px;font-size:12px}}
  .abas {{display:flex;gap:10px;padding-top:18px;flex-wrap:wrap}}
  button {{font:inherit;border:1px solid #d9e1eb;background:white;color:#526071; padding:11px 26px;border-radius:9px;font-weight:600;cursor:pointer}}
  button[aria-selected="true"] {{color:white;background:{COR_VALE};border-color:{COR_VALE}}}
  button:focus-visible {{outline:3px solid #73b7fa;outline-offset:2px}}
  .conteudo {{max-width:1450px;margin:auto;padding:0 16px 28px}}
  .aba {{display:none}}
  .aba.ativa {{display:block}}
  iframe {{display:block;border:0;background:white;width:100%;height:700px}}
  .molde {{background:white;border:1px solid #e7edf5;border-radius:16px;overflow:hidden}}
  @media(max-width:700px) {{.topo {{padding:8px 16px 12px}} .conteudo {{padding:0 6px 24px}} button {{width:100%;}}}}
</style></head><body>
<header class="topo">
  <img class="banner" alt="Vale - Acompanhamento de mercado" src="{BANNER_BASE64}">
  <p class="subtexto">EAR: último dia disponível · ENA: média dos dias disponíveis do mês · PLD: atualização até o último dado disponível</p>
  <p class="fonte-atualizacao">Fonte: ONS / CCEE &nbsp;|&nbsp; Dados atualizados até: EAR {data_ear} · ENA {data_ena}{pld_txt}</p>
<nav class="abas" role="tablist" aria-label="Escolher grandeza">
<button role="tab" id="botao-ear" aria-selected="true" aria-controls="aba-ear" onclick="selecionar('ear')">EAR · Armazenamento</button>
<button role="tab" id="botao-ena" aria-selected="false" aria-controls="aba-ena" onclick="selecionar('ena')">ENA · Afluências</button>
<button role="tab" id="botao-pld" aria-selected="false" aria-controls="aba-pld" onclick="selecionar('pld')">PLD · Preços</button>
</nav></header>
<main class="conteudo">
<section class="aba ativa" id="aba-ear" role="tabpanel" aria-labelledby="botao-ear"><div class="molde"><iframe id="quadro-ear" title="Energia armazenada" src="ear_sin.html" loading="eager" style="height:1900px"></iframe></div></section>
<section class="aba" id="aba-ena" role="tabpanel" aria-labelledby="botao-ena"><div class="molde"><iframe id="quadro-ena" title="Energia natural afluente" src="ena.html" loading="eager" style="height:1900px"></iframe></div></section>
<section class="aba" id="aba-pld" role="tabpanel" aria-labelledby="botao-pld"><div class="molde"><iframe id="quadro-pld" title="PLD" src="pld.html" loading="eager" style="height:2600px"></iframe></div></section>
</main>
<script>
  function selecionar(tipo) {{
    for (const t of ['ear','ena','pld']) {{
      document.getElementById('aba-'+t).classList.toggle('ativa',tipo===t);
      document.getElementById('botao-'+t).setAttribute('aria-selected',tipo===t?'true':'false');
    }}
    history.replaceState(null,'',location.pathname+(tipo==='ear'?'':'?visao='+tipo));
    requestAnimationFrame(()=> {{
      const quadro=document.getElementById('quadro-'+tipo);
      if(quadro.contentWindow && quadro.contentWindow.Plotly) {{
        quadro.contentWindow.document.querySelectorAll('.js-plotly-plot').forEach(g=> {{quadro.contentWindow.Plotly.Plots.resize(g);}});
      }}
      ajustarAltura(quadro);
    }});
  }}
  function ajustarAltura(quadro) {{
    try {{
      const doc=quadro.contentDocument;
      if(doc && doc.documentElement) {{
        const main=doc.querySelector('main');
        const altura=main?main.offsetHeight+12:doc.documentElement.scrollHeight+6;
        const min = quadro.id==='quadro-pld'?2400:(quadro.id==='quadro-ear'?1600:700);
        quadro.style.height=Math.max(min,altura)+'px';
      }}
    }} catch(e) {{ }}
  }}
  for(const nome of ['ear','ena','pld']) {{ const quadro=document.getElementById('quadro-'+nome); quadro.addEventListener('load',()=>ajustarAltura(quadro)); }}
  window.addEventListener('message',(event)=> {{
    if(event.origin!==location.origin) return;
    if(event.data?.tipo==='altura-painel-ons' && ['ena','ear','pld'].includes(event.data.id)) {{
      const alturaMinima=event.data.id==='pld'?2400:(event.data.id==='ena'?700:1600);
      document.getElementById('quadro-'+event.data.id).style.height=Math.max(alturaMinima,event.data.altura+8)+'px';
    }}
  }});
  window.addEventListener('resize',()=> {{ const ativo=document.querySelector('.aba.ativa iframe'); if(ativo) ajustarAltura(ativo); }});
  const visao = new URLSearchParams(location.search).get('visao');
  if(['ena','pld'].includes(visao)) selecionar(visao);
</script></body></html>'''


def salvar(diario: pd.DataFrame) -> None:
    DIARIO.parent.mkdir(parents=True, exist_ok=True)
    PAGINA_ENA.parent.mkdir(parents=True, exist_ok=True)
    if not PAGINA_EAR.exists():
        raise FileNotFoundError('Primeiro execute atualizar_ear.py para criar graficos/ear_sin.html')
    d = diario[COLUNAS].sort_values('data').copy()
    m = montar_mensal(d)
    caminho_ear_mensal = RAIZ / 'dados' / 'ear_mensal.csv'
    if not caminho_ear_mensal.exists():
        raise FileNotFoundError('Falta dados/ear_mensal.csv; execute atualizar_ear.py antes da ENA.')
    datas_ear = pd.read_csv(caminho_ear_mensal, sep=';', usecols=['data'])
    data_ear = pd.to_datetime(datas_ear['data'], errors='raise').max().strftime('%d/%m/%Y')
    data_ena = pd.Timestamp(m['data'].max()).strftime('%d/%m/%Y')
    data_pld = None
    if PLD_LONG.exists():
        try:
            datas_pld = pd.read_csv(PLD_LONG, sep=';', usecols=['data'])
            data_pld = pd.to_datetime(datas_pld['data'], errors='coerce').max().strftime('%d/%m/%Y')
        except Exception:
            data_pld = None
    temporarios = []
    for destino, conteudo in [(PAGINA_ENA, pagina_ena(m)),
                              (PAGINA_INICIAL, pagina_abertura(data_ear, data_ena, data_pld))]:
        temp = destino.with_suffix(destino.suffix+'.tmp')
        temp.write_text(conteudo, encoding='utf-8')
        temporarios.append((temp,destino))
    for df, destino in [(d,DIARIO),(m,MENSAL)]:
        temp = destino.with_suffix(destino.suffix+'.tmp')
        df.to_csv(temp,sep=';',decimal=',',float_format='%.3f',
                  date_format='%Y-%m-%d',index=False)
        temporarios.append((temp,destino))
    for temp, destino in temporarios:
        temp.replace(destino)
    print(f'Histórico ENA: {len(d)} dias, {len(m)} pontos mensais.')
    print(f'Última ENA mensal: até {m["data"].max().date()} ({int(m.iloc[-1]["dias_observados"])} dias), SIN = {m.iloc[-1]["enasinpp"]:.2f}% MLT')
    print('Páginas:', PAGINA_INICIAL, PAGINA_ENA, PAGINA_EAR)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--somente-local',action='store_true',
                   help='Regenera gráficos a partir do histórico, sem consulta à internet')
    p.add_argument('--ons-arquivo',type=Path,help='CSV ONS local para teste')
    p.add_argument('--dias-revisao',type=int,default=45)
    args = p.parse_args()
    if args.dias_revisao < 0:
        p.error('--dias-revisao deve ser >= 0')
    historico = carregar_historico()
    if not args.somente_local:
        hoje = datetime.now(ZoneInfo('America/Sao_Paulo')).date()
        novo = coletar_recente(hoje,args.dias_revisao,args.ons_arquivo)
        historico = atualizar(historico,novo,args.dias_revisao,hoje)
    salvar(historico)


if __name__ == '__main__':
    main()
