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
BANNER_BASE64 = 'data:image/png;base64,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'


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
        template='plotly_white', height=650 if grande else 460,
        autosize=True, paper_bgcolor='white', plot_bgcolor='white',
        hovermode='closest', margin=dict(l=68, r=32, t=77 if grande else 65, b=170),
        # Sem título embutido na legenda: ele ocupava a mesma linha dos anos
        # e causava sobreposição nas visualizações com duas colunas.
        legend=dict(orientation='h',
                    x=.5, xanchor='center', y=-.27, yanchor='top',
                    font=dict(size=11), traceorder='normal'),
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
            f'{fragmento}'
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
  .painel {{min-width:0;background:#fff;border:1px solid #eaedf2;border-radius:12px;overflow:hidden;display:flex;flex-direction:column}}
  .painel.sin {{grid-column:1/-1}}
  .barra-titulo {{background:{COR_VALE};color:#fff;font-weight:700;text-align:center;padding:12px 16px;font-size:18px;line-height:1.2;}}
  .painel.sub .barra-titulo {{font-size:16px;padding:10px 14px;}}
  .js-plotly-plot {{width:100%!important}}
  .fonte-dados {{padding:2px 18px 18px;color:#667085;font-size:11px;line-height:1.5;margin-top:auto;}}
  @media(max-width:850px) {{.area {{grid-template-columns:minmax(0,1fr);padding:0;gap:10px}}
    .painel {{border-radius:7px}} .painel.sin {{grid-column:auto}} }}
</style></head>
<body><main class="area">{''.join(blocos)}</main>
<script>
  function comunicarAltura() {{
    window.parent.postMessage({{tipo:'altura-painel-ons', id:'ena',
      altura:document.querySelector('main').offsetHeight+12}},'*');
  }}
  window.addEventListener('load',comunicarAltura);
  window.addEventListener('resize',comunicarAltura);
  if(window.ResizeObserver) new ResizeObserver(comunicarAltura).observe(document.querySelector('main'));
</script></body></html>'''


def pagina_abertura(data_ear: str, data_ena: str) -> str:
    return f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel Hidrológico | EAR e ENA mensal</title>
<style>
  :root {{font-family:{FONTE};color:#253345;background:#f7f9fc}}
  * {{box-sizing:border-box}}
  body {{margin:0}}
  .topo {{max-width:1450px;margin:auto;padding:6px 24px 14px}}
  .topo .banner {{width:100%;display:block;border:1px solid #d8e0ea}}
  .topo p {{margin:0;color:#697586;font-size:13px;line-height:1.65}}
  .topo .subtexto {{margin-top:10px}}
  .topo .fonte-atualizacao {{color:#526071;margin-top:3px;font-size:12px}}
  .abas {{display:flex;gap:10px;padding-top:18px;flex-wrap:wrap}}
  button {{font:inherit;border:1px solid #d9e1eb;background:white;color:#526071;
      padding:11px 26px;border-radius:9px;font-weight:600;cursor:pointer}}
  button[aria-selected="true"] {{color:white;background:{COR_VALE};border-color:{COR_VALE}}}
  button:focus-visible {{outline:3px solid #73b7fa;outline-offset:2px}}
  .conteudo {{max-width:1450px;margin:auto;padding:0 16px 28px}}
  .aba {{display:none}}
  .aba.ativa {{display:block}}
  iframe {{display:block;border:0;background:white;width:100%;height:700px}}
  .molde {{background:white;border:1px solid #e7edf5;border-radius:13px;overflow:hidden}}
  @media(max-width:700px) {{.topo {{padding:8px 16px 12px}}
     .conteudo {{padding:0 6px 24px}} button {{width:100%;}}}}
</style></head><body>
<header class="topo">
  <img class="banner" alt="Vale - Acompanhamento de mercado" src="{BANNER_BASE64}">
  <p class="subtexto">EAR: último dia disponível · ENA: média dos dias disponíveis do mês</p>
  <p class="fonte-atualizacao">Fonte: ONS &nbsp;|&nbsp; Dados atualizados até: EAR {data_ear} · ENA {data_ena}</p>
<nav class="abas" role="tablist" aria-label="Escolher grandeza">
<button role="tab" id="botao-ear" aria-selected="true" aria-controls="aba-ear" onclick="selecionar('ear')">EAR · Armazenamento</button>
<button role="tab" id="botao-ena" aria-selected="false" aria-controls="aba-ena" onclick="selecionar('ena')">ENA · Afluências</button>
</nav></header>
<main class="conteudo">
<section class="aba ativa" id="aba-ear" role="tabpanel" aria-labelledby="botao-ear">
  <div class="molde"><iframe id="quadro-ear" title="Energia armazenada SIN" src="ear_sin.html" loading="eager"></iframe></div>
</section>
<section class="aba" id="aba-ena" role="tabpanel" aria-labelledby="botao-ena">
  <div class="molde"><iframe id="quadro-ena" title="Energia natural afluente SIN e subsistemas" src="ena.html" loading="eager" style="height:1900px"></iframe></div>
</section>
</main>
<script>
  function selecionar(tipo) {{
    for (const t of ['ear','ena']) {{
      document.getElementById('aba-'+t).classList.toggle('ativa',tipo===t);
      document.getElementById('botao-'+t).setAttribute('aria-selected',tipo===t?'true':'false');
    }}
    history.replaceState(null,'',location.pathname+(tipo==='ena'?'?visao=ena':''));
    requestAnimationFrame(()=> {{
      const quadro=document.getElementById('quadro-'+tipo);
      if(quadro.contentWindow && quadro.contentWindow.Plotly) {{
        quadro.contentWindow.document.querySelectorAll('.js-plotly-plot').forEach(g=> {{
          quadro.contentWindow.Plotly.Plots.resize(g);
        }});
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
        quadro.style.height=Math.max(640,altura)+'px';
      }}
    }} catch(e) {{ /* Sem acesso cross-origin: manter altura padrão */ }}
  }}
  for(const nome of ['ear','ena']) {{
    const quadro=document.getElementById('quadro-'+nome);
    quadro.addEventListener('load',()=>ajustarAltura(quadro));
  }}
  window.addEventListener('message',(event)=> {{
    if(event.origin!==location.origin) return;
    if(event.data?.tipo==='altura-painel-ons' && ['ena','ear'].includes(event.data.id)) {{
      const alturaMinima=event.data.id==='ena'?700:640;
      document.getElementById('quadro-'+event.data.id).style.height=
        Math.max(alturaMinima,event.data.altura+8)+'px';
    }}
  }});
  window.addEventListener('resize',()=> {{
    const ativo=document.querySelector('.aba.ativa iframe');
    if(ativo) ajustarAltura(ativo);
  }});
  if(new URLSearchParams(location.search).get('visao')==='ena') selecionar('ena');
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
    temporarios = []
    for destino, conteudo in [(PAGINA_ENA, pagina_ena(m)),
                              (PAGINA_INICIAL, pagina_abertura(data_ear, data_ena))]:
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
