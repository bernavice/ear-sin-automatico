"""Atualiza a EAR diária/mensal do SIN e recria o gráfico Plotly.

Fonte: ONS Dados Abertos, EAR Diário por Subsistema.
O arquivo anual do ONS é baixado, mas apenas uma janela recente é incorporada
à base histórica salva no GitHub.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests

RAIZ = Path(__file__).resolve().parent
DIARIO = RAIZ / "dados" / "ear_diaria.csv"
MENSAL = RAIZ / "dados" / "ear_mensal.csv"
GRAFICO = RAIZ / "graficos" / "ear_sin.html"
SUBS = ["earse", "earsu", "earne", "earn"]
COLUNAS = ["data", *SUBS, "earsin"]
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


def carregar_ons(ano: int, arquivo: Path | None = None) -> pd.DataFrame:
    if arquivo:
        print(f"Teste local: lendo arquivo ONS de {arquivo}")
        raw = pd.read_csv(arquivo, sep=";", dtype=str, encoding="utf-8-sig")
    else:
        url = BASE_ONS.format(ano=ano)
        print(f"Baixando ONS: {url}")
        try:
            resposta = requests.get(url, timeout=90)
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


def atualizar_historico(hist: pd.DataFrame, ons: pd.DataFrame, dias_revisao: int) -> pd.DataFrame:
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
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


def montar_mensal(diario: pd.DataFrame) -> pd.DataFrame:
    df = diario.sort_values("data").copy()
    df["ano"] = df["data"].dt.year
    df["mes"] = df["data"].dt.month
    # Inclui mês em andamento: escolhe último dia DISPONÍVEL, não último dia do calendário.
    mensal = df.groupby(["ano", "mes"], as_index=False).tail(1)
    return mensal[["data", "ano", "mes", *SUBS, "earsin"]].reset_index(drop=True)


def montar_grafico(mensal: pd.DataFrame) -> str:
    nomes = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    ano_max = int(mensal["ano"].max())
    mostrar = mensal["ano"] >= ano_max - 9
    grafico = mensal.loc[mostrar].sort_values(["ano", "mes"])
    cores = ["#7E57C2", "#00897B", "#D97929", "#DB4C66", "#607D8B", "#8D6E63", "#43A047", "#9C27B0", "#00ACC1", "#2563EB"]
    fig = go.Figure()
    for i, (ano, grupo) in enumerate(grafico.groupby("ano", sort=True)):
        atual = ano == ano_max
        fig.add_trace(go.Scatter(
            x=grupo["mes"], y=grupo["earsin"],
            customdata=grupo["data"].dt.strftime("%d/%m/%Y"),
            name=str(ano), mode="lines+markers",
            line=dict(shape="spline", smoothing=0.8, width=3.8 if atual else 2.2,
                      color="#1D4ED8" if atual else cores[i % len(cores)]),
            marker=dict(size=7 if atual else 5),
            opacity=1 if atual else 0.75,
            hovertemplate=("<b>%{fullData.name}</b><br>Referência: %{customdata}"
                           "<br>EAR SIN: %{y:.2f}%<extra></extra>"),
        ))

    ultimo = mensal.iloc[-1]
    fig.update_layout(
        title=dict(text="EAR do SIN | último dia disponível de cada mês", x=0.04),
        template="plotly_white", height=610,
        xaxis=dict(title="Mês", tickmode="array", tickvals=list(range(1, 13)),
                   ticktext=nomes, range=[0.8, 12.2], dtick=1),
        yaxis=dict(title="Energia armazenada (% da EAR máxima)", range=[0, 100], ticksuffix="%"),
        legend=dict(title="Ano", orientation="h", y=-0.22, x=0.5, xanchor="center"),
        hovermode="closest", margin=dict(l=65, r=30, t=85, b=130),
        annotations=[dict(
            text=(f"Último dado: {ultimo['data'].strftime('%d/%m/%Y')} "
                  f"— EAR SIN {ultimo['earsin']:.2f}% · Fonte: ONS / histórico fornecido"),
            x=0.01, y=-0.29, xref="paper", yref="paper", showarrow=False,
            xanchor="left", font=dict(size=12, color="#64748B"),
        )],
    )
    return fig.to_html(full_html=True, include_plotlyjs="cdn", div_id="grafico-ear-sin",
                       config={"responsive": True, "displaylogo": False})


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
        ano = datetime.now(ZoneInfo("America/Sao_Paulo")).year
        ons = carregar_ons(ano, arquivo=args.ons_arquivo)
        historico = atualizar_historico(historico, ons, args.dias_revisao)
    salvar(historico)


if __name__ == "__main__":
    main()
