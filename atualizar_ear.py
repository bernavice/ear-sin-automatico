"""Atualiza a EAR diária/mensal do SIN e recria o gráfico Plotly.

Fonte: ONS Dados Abertos, EAR Diário por Subsistema.
O arquivo anual do ONS é baixado, mas apenas uma janela recente é incorporada
à base histórica salva no GitHub.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
from plotly.offline import get_plotlyjs_version

RAIZ = Path(__file__).resolve().parent
DIARIO = RAIZ / "dados" / "ear_diaria.csv"
MENSAL = RAIZ / "dados" / "ear_mensal.csv"
GRAFICO = RAIZ / "graficos" / "ear_sin.html"
SUBS = ["earse", "earsu", "earne", "earn"]
COLUNAS = ["data", *SUBS, "earsin"]
# Referências mensais informadas pelo usuário, em % da EAR máxima.
FAIXA_MIN = [21, 23, 30, 35, 36, 35, 33, 28, 23, 18, 19, 22]
FAIXA_MAX = [78, 84, 89, 90, 88, 87, 84, 79, 73, 69, 67, 72]
COR_VALE = '#007E80'

# Vmimop: informe o percentual oficial desejado para uma linha horizontal.
# Não é possível estabelecer o Vmimop do SIN com segurança sem esse valor.
# Exemplo de uso APÓS validação: VMIMOP_PCT = 18.0
# Enquanto None, a linha não é desenhada (para não divulgar limiar inventado).
VMIMOP_PCT = None

BASE_ONS = (
    "https://ons-aws-prod-opendata.s3.amazonaws.com/"
    "dataset/ear_subsistema_di/EAR_DIARIO_SUBSISTEMA_{ano}.csv"
)


def nome_normalizado(texto: object) -> str:
    txt = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(ch for ch in txt if not unicodedata.combining(ch)).upper().strip()


def subsistema(id_sub: object, nome_sub: object) -> str | None:
    codigo = re.sub(r"[^A-Z0-9]", "", nome_normalizado(id_sub))
    nome = nome_normalizado(nome_sub)
    if codigo in {"SE", "SECO", "SECOESTE"} or "SUDESTE" in nome:
        return "earse"
    if codigo in {"S", "SU"} or nome == "SUL":
        return "earsu"
    if codigo == "NE" or "NORDESTE" in nome:
        return "earne"
    if codigo == "N" or nome == "NORTE":
        return "earn"
    return None


def numero(valor: object) -> float:
    """Aceita decimal com ponto ou vírgula, com/sem separador de milhar."""
    s = str(valor).strip().replace(" ", "")
    if not s or s.lower() in {"nan", "none"}:
        raise ValueError("Valor numérico vazio no ONS")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def carregar_historico() -> pd.DataFrame:
    if not DIARIO.exists():
        raise FileNotFoundError(f"Histórico inicial não encontrado: {DIARIO}")
    df = pd.read_csv(DIARIO, sep=";", decimal=",", encoding="utf-8-sig")
    faltantes = set(COLUNAS) - set(df.columns)
    if faltantes:
        raise ValueError(f"Colunas ausentes na base histórica: {faltantes}")
    df = df[COLUNAS].copy()
    df["data"] = pd.to_datetime(df["data"], errors="raise")
    for col in COLUNAS[1:]:
        df[col] = pd.to_numeric(df[col], errors="raise")
    if df["data"].duplicated().any():
        raise ValueError("A base histórica possui datas duplicadas.")
    return df.sort_values("data").reset_index(drop=True)


class ArquivoONSNaoPublicado(FileNotFoundError):
    """O CSV do novo ano ainda não foi publicado pelo ONS (HTTP 404)."""


def carregar_ons(ano: int, arquivo: Path | None = None) -> pd.DataFrame:
    if arquivo:
        print(f"Teste local: lendo arquivo ONS de {arquivo}")
        raw = pd.read_csv(arquivo, sep=";", dtype=str, encoding="utf-8-sig")
    else:
        url = BASE_ONS.format(ano=ano)
        print(f"Baixando ONS: {url}")
        try:
            resposta = requests.get(url, timeout=90)
            if resposta.status_code == 404:
                raise ArquivoONSNaoPublicado(f"Arquivo de {ano} ainda não encontrado no ONS: {url}")
            resposta.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Falha ao baixar o arquivo anual do ONS: {exc}") from exc
        from io import BytesIO
        raw = pd.read_csv(BytesIO(resposta.content), sep=";", dtype=str, encoding="utf-8-sig")

    raw.columns = [c.strip().lower() for c in raw.columns]
    obrigatorias = {
        "ear_data", "id_subsistema", "nom_subsistema",
        "ear_max_subsistema", "ear_verif_subsistema_mwmes",
        "ear_verif_subsistema_percentual",
    }
    faltantes = obrigatorias - set(raw.columns)
    if faltantes:
        raise ValueError(f"Mudança no layout ONS. Colunas ausentes: {sorted(faltantes)}. Encontradas: {list(raw.columns)}")

    raw["data"] = pd.to_datetime(raw["ear_data"], errors="coerce")
    raw["subsistema"] = [
        subsistema(cod, nome)
        for cod, nome in zip(raw["id_subsistema"], raw["nom_subsistema"])
    ]
    raw = raw.dropna(subset=["data", "subsistema"]).copy()
    raw = raw[raw["data"].dt.year == ano].copy()
    if raw.empty:
        raise ValueError(f"ONS: não há dados de 4 subsistemas para {ano}.")

    for col in ["ear_max_subsistema", "ear_verif_subsistema_mwmes", "ear_verif_subsistema_percentual"]:
        raw[col] = raw[col].map(numero)

    if raw.duplicated(["data", "subsistema"]).any():
        raise ValueError("ONS possui datas/subsistemas duplicados; atualização interrompida.")

    # Somente datas com todos os 4 subsistemas. Não calcula SIN incompleto.
    contador = raw.groupby("data")["subsistema"].nunique()
    datas_validas = contador.loc[contador == 4].index
    raw = raw[raw["data"].isin(datas_validas)].copy()
    if raw.empty:
        raise ValueError("ONS: não foi encontrada nenhuma data completa com 4 subsistemas.")

    if (raw["ear_max_subsistema"] <= 0).any():
        raise ValueError("ONS: EAR máxima inválida.")
    if (raw["ear_verif_subsistema_mwmes"] < 0).any():
        raise ValueError("ONS: EAR verificada negativa.")
    if (~raw["ear_verif_subsistema_percentual"].between(0, 105)).any():
        raise ValueError("ONS: percentual fora da faixa esperada.")

    # O percentual do SIN NÃO é a média aritmética dos percentuais dos subsistemas.
    sin = raw.groupby("data").agg(
        ear_verificada=("ear_verif_subsistema_mwmes", "sum"),
        ear_max=("ear_max_subsistema", "sum"),
    )
    sin["earsin"] = (100 * sin["ear_verificada"] / sin["ear_max"]).round(2)

    piv = raw.pivot(index="data", columns="subsistema", values="ear_verif_subsistema_percentual")
    saida = piv.join(sin["earsin"]).reset_index()
    saida = saida[COLUNAS].sort_values("data").reset_index(drop=True)
    if not saida["earsin"].between(0, 105).all():
        raise ValueError("ONS: EAR SIN fora da faixa esperada.")
    print(f"ONS: {len(saida)} dias completos, último {saida['data'].max().date()}")
    return saida


def atualizar_historico(
    hist: pd.DataFrame,
    ons: pd.DataFrame,
    dias_revisao: int,
    data_execucao: date | None = None,
) -> pd.DataFrame:
    hoje = data_execucao or datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    ons = ons.loc[ons["data"].dt.date <= hoje].copy()
    if ons.empty:
        raise ValueError("ONS não trouxe datas válidas até hoje.")

    ultima_data = hist["data"].max()
    limite = ultima_data - timedelta(days=dias_revisao)
    ons = ons.loc[ons["data"] >= limite].copy()
    if ons.empty:
        print("ONS não possui registros na janela recente. Mantendo histórico.")
        return hist

    # ONS prevalece nas datas coincidentes da janela recente;
    # datas não presentes no ONS são preservadas no histórico.
    unificado = pd.concat([hist, ons], ignore_index=True)
    unificado = unificado.drop_duplicates(subset="data", keep="last")
    unificado = unificado.sort_values("data").reset_index(drop=True)
    return unificado


def coletar_ons_recente(
    data_execucao: date,
    dias_revisao: int,
    arquivo_teste: Path | None = None,
) -> pd.DataFrame:
    """Consulta 2 arquivos na virada do ano e apenas o atual no restante.

    Na virada, dezembro pode ser revisado e o CSV do novo ano pode
    demorar a surgir. Se estiver ausente (404), mantém o último histórico
    consistente, sem deixar o site fora do ar. Erros diferentes de 404
    continuam sendo reportados normalmente.
    """
    dias_desde_virada = (data_execucao - date(data_execucao.year, 1, 1)).days
    na_transicao = dias_desde_virada <= max(45, dias_revisao)
    anos = ([data_execucao.year - 1] if na_transicao and arquivo_teste is None else []) + [data_execucao.year]

    conjuntos = []
    for ano in anos:
        try:
            ons_ano = carregar_ons(
                ano,
                arquivo=arquivo_teste if ano == data_execucao.year else None,
            )
            conjuntos.append(ons_ano)
        except ArquivoONSNaoPublicado:
            if ano == data_execucao.year and na_transicao and conjuntos:
                print(f"ONS ainda não publicou {ano}; mantido histórico até a última data válida.")
                continue
            raise

    if not conjuntos:
        raise RuntimeError("Nenhum arquivo anual do ONS disponível na atualização.")

    ons = pd.concat(conjuntos, ignore_index=True)
    ons = ons.drop_duplicates(subset=["data"], keep="last")
    ons = ons.sort_values("data").reset_index(drop=True)
    return ons


def montar_mensal(diario: pd.DataFrame) -> pd.DataFrame:
    df = diario.sort_values("data").copy()
    df["ano"] = df["data"].dt.year
    df["mes"] = df["data"].dt.month
    # Inclui mês em andamento: escolhe último dia DISPONÍVEL, não último dia do calendário.
    mensal = df.groupby(["ano", "mes"], as_index=False).tail(1)
    return mensal[["data", "ano", "mes", *SUBS, "earsin"]].reset_index(drop=True)



def suavizar_curva_mensal(valores: list[int] | list[float], pontos_por_mes: int = 25):
    """Interpola os 12 valores com cúbicas monotônicas (PCHIP), sem SciPy.

    Evita oscilações artificiais e mantém os limites em seus valores mensais.
    A interpolação é feita apenas para o desenho; os dados de referência
    permanecem inalterados.
    """
    y = np.asarray(valores, dtype=float)
    x = np.arange(1, len(y) + 1, dtype=float)
    if len(y) < 3:
        return x, y

    delta = np.diff(y)
    derivadas = np.zeros_like(y)
    # Derivada harmônica nos pontos internos (malha mensal uniforme).
    for i in range(1, len(y) - 1):
        anterior, proxima = delta[i - 1], delta[i]
        if anterior * proxima > 0:
            derivadas[i] = 2 * anterior * proxima / (anterior + proxima)

    def derivada_extremo(a: float, b: float) -> float:
        valor = (3 * a - b) / 2
        if valor * a <= 0:
            return 0.0
        if a * b < 0 and abs(valor) > 3 * abs(a):
            return 3 * a
        return valor

    derivadas[0] = derivada_extremo(delta[0], delta[1])
    derivadas[-1] = derivada_extremo(delta[-1], delta[-2])

    xs = np.linspace(x[0], x[-1], (len(y) - 1) * pontos_por_mes + 1)
    indices = np.clip(np.floor(xs).astype(int) - 1, 0, len(y) - 2)
    t = xs - x[indices]
    t2, t3 = t * t, t * t * t
    ys = ((2 * t3 - 3 * t2 + 1) * y[indices]
          + (t3 - 2 * t2 + t) * derivadas[indices]
          + (-2 * t3 + 3 * t2) * y[indices + 1]
          + (t3 - t2) * derivadas[indices + 1])
    return xs, ys

def grafico_ear(mensal: pd.DataFrame, titulo: str, coluna: str,
                grande: bool = False) -> go.Figure:
    """Gráfico de EAR de um subsistema ou do SIN, mantendo a regra do último dia.

    Apenas o SIN possui faixa histórica mín.–máx. e eventual linha Vmimop.
    """
    meses = list(range(1, 13))
    nomes = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
             "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    ano_atual = datetime.now(ZoneInfo("America/Sao_Paulo")).year
    ultimo_ano_na_base = int(mensal["ano"].max())
    plotagem = mensal.loc[mensal["ano"] >= ultimo_ano_na_base - 9]
    plotagem = plotagem.sort_values(["ano", "mes"])
    anos_visiveis = {ano_atual, ano_atual - 1, 2021, 2023}
    cores_destaque = {
        2021: "#D62828",         # vermelho
        2023: "#8ED1FC",         # azul bem claro
        ano_atual - 1: "#D97706",  # âmbar
        ano_atual: "#2563EB",    # azul
    }
    outras_cores = ["#64748B", "#C05C62", "#7C8C43", "#6D77AE",
                    "#A56B43", "#778899", "#9C78AB", "#249A9A"]
    fig = go.Figure()

    if coluna == "earsin":
        # Banda cinza como um único polígono fechado. A legenda oculta
        # simultaneamente o preenchimento e os dois contornos suaves.
        x_suave, minimo_suave = suavizar_curva_mensal(FAIXA_MIN)
        _, maximo_suave = suavizar_curva_mensal(FAIXA_MAX)
        fig.add_trace(go.Scatter(
            x=np.concatenate([x_suave, x_suave[::-1]]),
            y=np.concatenate([maximo_suave, minimo_suave[::-1]]),
            mode="lines", fill="toself",
            fillcolor="rgba(187, 194, 202, 0.50)",
            line=dict(width=1.3, color="rgba(155, 164, 175, 0.70)"),
            name="Faixa hist.", showlegend=True, hoverinfo="skip",
        ))
        if VMIMOP_PCT is not None:
            valor_vmimop = float(VMIMOP_PCT)
            if not 0 <= valor_vmimop <= 100:
                raise ValueError("VMIMOP_PCT deve estar entre 0 e 100 (%)")
            fig.add_trace(go.Scatter(
                x=meses, y=[valor_vmimop] * 12,
                name="Vmimop", mode="lines",
                line=dict(color="#676B73", width=2.5, dash="dash"),
                hovertemplate="Vmimop: %{y:.2f}%<extra></extra>",
            ))

    for i, (ano, grupo) in enumerate(plotagem.groupby("ano", sort=True)):
        ano = int(ano)
        atual = ano == ano_atual
        exibir = ano in anos_visiveis
        cor = cores_destaque.get(ano, outras_cores[i % len(outras_cores)])
        fig.add_trace(go.Scatter(
            x=grupo["mes"], y=grupo[coluna],
            customdata=grupo["data"].dt.strftime("%d/%m/%Y"),
            name=str(ano), visible=True if exibir else "legendonly",
            mode="lines+markers",
            line=dict(shape="spline", smoothing=0.82,
                      width=3.7 if atual else 2.5, color=cor),
            marker=dict(size=7 if atual else 5,
                        symbol="circle", color=cor),
            opacity=1 if exibir else 0.8,
            hovertemplate=("<b>%{fullData.name}</b><br>Referência: %{customdata}"
                           "<br>EAR: %{y:.2f}%<extra></extra>"),
        ))

    fonte = "Century Gothic, CenturyGothic, Arial, sans-serif"
    marcadores_cruz = [dict(
        type="line", xref="x", yref="paper", x0=mes, x1=mes,
        y0=-0.018, y1=0.018,
        line=dict(color="#475467", width=1.2), layer="above",
    ) for mes in meses]

    fig.update_layout(
        title=dict(text='', x=0.5, xanchor='center'),
        font=dict(family=fonte, size=12, color="#344054"),
        template="plotly_white",
        height=660 if grande else 510,
        autosize=True, paper_bgcolor="white", plot_bgcolor="white",
        hovermode="closest", shapes=marcadores_cruz,
        # Sem título da legenda: evita sobreposição com os anos.
        legend=dict(
            orientation="h", x=0.5, xanchor="center",
            y=-0.30, yanchor="top", font=dict(size=11),
            traceorder="normal", itemwidth=70,
            entrywidth=92, entrywidthmode="pixels",
            itemsizing="constant",
        ),
        margin=dict(l=75 if grande else 64, r=36 if grande else 27,
                    t=18, b=195),
    )
    fig.update_xaxes(
        title_text="Mês", range=[0.75, 12.25],
        tickmode="array", tickvals=meses, ticktext=nomes,
        showline=True, linewidth=1.5, linecolor="#475467",
        ticks="", showgrid=False, zeroline=False,
        minor=dict(tickmode="array",
                   tickvals=[i + 0.5 for i in range(1, 12)],
                   ticks="outside", ticklen=3, tickwidth=1,
                   tickcolor="#98A2B3", showgrid=False),
    )
    fig.update_yaxes(
        title_text="EAR (%)", range=[0, 100],
        tickmode="linear", tick0=0, dtick=10, ticksuffix="%",
        showline=True, linewidth=1.5, linecolor="#475467",
        ticks="outside", ticklen=6, tickwidth=1.2,
        tickcolor="#475467", showgrid=False, zeroline=False,
        minor=dict(tickmode="linear", tick0=0, dtick=5,
                   ticks="outside", ticklen=3, tickwidth=1,
                   tickcolor="#98A2B3", showgrid=False),
    )
    return fig


def montar_grafico(mensal: pd.DataFrame) -> str:
    """Página EAR com SIN em largura total e quatro subsistemas em grade 2×2."""
    configuracoes = [
        ("SIN", "earsin", True),
        ("SE/CO", "earse", False),
        ("Sul", "earsu", False),
        ("Norte", "earn", False),
        ("Nordeste", "earne", False),
    ]
    ultima_data = pd.Timestamp(mensal["data"].max()).strftime("%d/%m/%Y")
    blocos = []
    for i, (nome, coluna, grande) in enumerate(configuracoes):
        titulo_barra = f"EAR — {nome}"
        fig = grafico_ear(mensal, titulo_barra, coluna, grande=grande)
        fragmento = fig.to_html(
            full_html=False, include_plotlyjs=False,
            div_id=f"plot-ear-{i}",
            config={"responsive": True, "displaylogo": False},
        )
        tipo = "sin" if grande else "sub"
        blocos.append(
            f'<section class="painel {tipo}">'
            f'<div class="barra-titulo">{titulo_barra}</div>'
            f'{fragmento}'
            f'<footer class="fonte-dados">Fonte: ONS &nbsp;|&nbsp; '
            f'Dados atualizados até: {ultima_data}</footer></section>'
        )
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energia Armazenada | SIN e Subsistemas</title>
<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"></script>
<style>
  * {{box-sizing:border-box}}
  body {{margin:0;background:#fff;color:#263142;
        font-family:Century Gothic,CenturyGothic,Arial,sans-serif}}
  .area {{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
          gap:15px;padding:0 12px 20px}}
  .painel {{min-width:0;background:#fff;border:1px solid #eaedf2;
            border-radius:12px;overflow:hidden;display:flex;flex-direction:column}}
  .painel.sin {{grid-column:1/-1}}
  .barra-titulo {{background:{COR_VALE};color:#fff;font-weight:700;
                 text-align:center;padding:12px 16px;font-size:18px;line-height:1.2;}}
  .painel.sub .barra-titulo {{font-size:16px;padding:10px 14px;}}
  .js-plotly-plot {{width:100%!important}}
  .fonte-dados {{padding:2px 18px 18px;color:#667085;
                 font-size:11px;line-height:1.5;margin-top:auto}}
  @media(max-width:850px) {{
    .area {{grid-template-columns:minmax(0,1fr);padding:0;gap:10px}}
    .painel {{border-radius:7px}}
    .painel.sin {{grid-column:auto}}
  }}
</style></head><body>
<main class="area">{''.join(blocos)}</main>
<script>
  function comunicarAlturaEAR() {{
    if (window.parent !== window) window.parent.postMessage(
      {{tipo:'altura-painel-ons', id:'ear',
        altura:document.querySelector('main').offsetHeight+12}}, '*');
  }}
  window.addEventListener('load', comunicarAlturaEAR);
  window.addEventListener('resize', comunicarAlturaEAR);
  if (window.ResizeObserver) {{
    new ResizeObserver(comunicarAlturaEAR)
      .observe(document.querySelector('main'));
  }}
</script></body></html>"""


def salvar(diario: pd.DataFrame) -> None:
    DIARIO.parent.mkdir(parents=True, exist_ok=True)
    GRAFICO.parent.mkdir(parents=True, exist_ok=True)
    diario = diario[COLUNAS].sort_values("data").copy()
    diario["tx_var_ear_sin"] = diario["earsin"].diff().round(3)
    mensal = montar_mensal(diario)

    # Gravações temporárias e substituição somente após todos os cálculos.
    diaria_temp = DIARIO.with_suffix(".tmp")
    mensal_temp = MENSAL.with_suffix(".tmp")
    grafico_temp = GRAFICO.with_suffix(".tmp")
    diario.to_csv(diaria_temp, index=False, sep=";", decimal=",", float_format="%.3f", date_format="%Y-%m-%d")
    mensal.to_csv(mensal_temp, index=False, sep=";", decimal=",", float_format="%.2f", date_format="%Y-%m-%d")
    grafico_temp.write_text(montar_grafico(mensal), encoding="utf-8")
    diaria_temp.replace(DIARIO)
    mensal_temp.replace(MENSAL)
    grafico_temp.replace(GRAFICO)
    print(f"Histórico: {len(diario)} dias; {len(mensal)} pontos mensais.")
    print(f"Mais recente: {mensal.iloc[-1]['data'].date()} | SIN = {mensal.iloc[-1]['earsin']:.2f}%")
    print(f"Saídas: {DIARIO}, {MENSAL}, {GRAFICO}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--somente-local", action="store_true", help="Apenas recria CSV mensal e HTML sem baixar ONS")
    parser.add_argument("--ons-arquivo", type=Path, help="Arquivo CSV local do ONS para teste")
    parser.add_argument("--dias-revisao", type=int, default=45, help="Janela de revisão (padrão: 45 dias)")
    args = parser.parse_args()
    if args.dias_revisao < 0:
        parser.error("--dias-revisao não pode ser negativo")
    historico = carregar_historico()
    if not args.somente_local:
        data_execucao = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        ons = coletar_ons_recente(data_execucao, args.dias_revisao, args.ons_arquivo)
        historico = atualizar_historico(historico, ons, args.dias_revisao, data_execucao)
    salvar(historico)


if __name__ == "__main__":
    main()
