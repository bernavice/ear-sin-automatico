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
        title=dict(text=titulo, x=0.5, xanchor='center',
                   font=dict(size=22 if grande else 17, color='#263142')),
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
        fig = grafico_ena(mensal, f'ENA — {nome}', coluna, grande=maior)
        fragmento = fig.to_html(full_html=False, include_plotlyjs=False,
                                div_id=f'plot-ena-{i}',
                                config={'responsive': True, 'displaylogo': False})
        tipo = 'sin' if maior else 'sub'
        blocos.append(
            f'<section class="painel {tipo}">{fragmento}'
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
  .topo {{max-width:1450px;margin:auto;padding:24px 24px 14px}}
  .topo h1 {{font-size:26px;letter-spacing:-.6px;margin:0 0 5px;font-weight:650}}
  .topo p {{margin:0;color:#697586;font-size:13px;line-height:1.65}}
  .topo .fonte-atualizacao {{color:#526071;margin-top:3px;font-size:12px}}
  .abas {{display:flex;gap:10px;padding-top:18px;flex-wrap:wrap}}
  button {{font:inherit;border:1px solid #d9e1eb;background:white;color:#526071;
      padding:11px 26px;border-radius:9px;font-weight:600;cursor:pointer}}
  button[aria-selected="true"] {{color:white;background:#1b477b;border-color:#1b477b}}
  button:focus-visible {{outline:3px solid #73b7fa;outline-offset:2px}}
  .conteudo {{max-width:1450px;margin:auto;padding:0 16px 28px}}
  .aba {{display:none}}
  .aba.ativa {{display:block}}
  iframe {{display:block;border:0;background:white;width:100%;height:700px}}
  .molde {{background:white;border:1px solid #e7edf5;border-radius:13px;overflow:hidden}}
  @media(max-width:700px) {{.topo {{padding:17px 16px 12px}}
     .topo h1 {{font-size:21px}} .conteudo {{padding:0 6px 24px}}}}
</style></head><body>
<header class="topo"><h1>Painel Hidrológico do SIN</h1>
<p>EAR: último dia disponível · ENA: média dos dias disponíveis do mês</p>
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
