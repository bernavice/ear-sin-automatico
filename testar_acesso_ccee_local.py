"""Diagnóstico CONTROLADO de acesso oficial da CCEE.

Execute em seu computador com acesso permitido à internet/rede corporativa:
    python testar_acesso_ccee_local.py

Apenas 1 requisição por fonte; NÃO tenta contornar bloqueios, não altera dados,
não armazena cookies ou credenciais e não baixa o CSV completo.

Opcional: --incluir-ip consulta o IP público por serviço externo para suporte.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import ssl
from pathlib import Path

import requests

API = "https://dadosabertos.ccee.org.br/api/3/action/datastore_search"
CSV_2026 = "https://pda-download.ccee.org.br/6A5wq97KTCWv_bvs3CqsQQ/content"
RECURSO_2026 = "3f279d6b-1069-42f7-9b0a-217b084729c4"

HEADERS = {
    "User-Agent": "Painel-Vale-PLD/1.1 (integracao-dados-abertos; diagnostico)",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "application/json, text/csv, text/plain, */*",
}

IDS_RESPOSTA = (
    "x-request-id", "x-correlation-id", "x-amzn-trace-id", "cf-ray",
    "x-akamai-request-id", "traceparent", "server-timing",
)


def testar(session: requests.Session, nome: str, url: str, parametros=None):
    registro = {
        "fonte": nome,
        "hora_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "url_solicitada": url,
    }
    try:
        # stream=True: obtém status/cabeçalhos, sem baixar o CSV anual completo.
        with session.get(url, params=parametros, stream=True, timeout=(12, 35),
                         allow_redirects=True) as resposta:
            registro.update({
                "http": resposta.status_code,
                "url_final": resposta.url,
                "tipo_resposta": resposta.headers.get("Content-Type", ""),
                "redirecionamentos": [
                    {"http": r.status_code, "url": r.url}
                    for r in resposta.history
                ],
                "identificadores": {k: resposta.headers[k] for k in IDS_RESPOSTA
                                     if k in resposta.headers},
            })
            if resposta.status_code == 403:
                registro["interpretacao"] = "Acesso recusado. Encaminhar ao suporte CCEE; nao tentar contornar."
            elif resposta.status_code == 200:
                tipo = registro["tipo_resposta"].lower()
                esperado = "json" if nome.startswith("API") else "csv"
                registro["interpretacao"] = (
                    f"HTTP 200; conferir se conteudo e de fato {esperado}."
                    if esperado in tipo else
                    f"HTTP 200 mas Content-Type inesperado ({tipo or 'nao informado'})."
                )
            else:
                registro["interpretacao"] = "Verificar status e exigencias do endpoint oficial."
    except requests.RequestException as exc:
        registro["erro"] = f"{type(exc).__name__}: {exc}"
    print(f"\n=== {nome} ===")
    for k, valor in registro.items():
        print(f"{k}: {valor}")
    return registro


def main():
    parser = argparse.ArgumentParser(description="Teste autorizado das fontes de PLD da CCEE")
    parser.add_argument("--incluir-ip", action="store_true",
                        help="Consultar IP publico em checkip.amazonaws.com (opcional)")
    parser.add_argument("--saida", default="diagnostico_ccee_local.json")
    args = parser.parse_args()
    ambiente = {
        "python": platform.python_version(),
        "requests": requests.__version__,
        "openssl": ssl.OPENSSL_VERSION,
        "sistema": platform.platform(),
    }
    if args.incluir_ip:
        try:
            r = requests.get("https://checkip.amazonaws.com", timeout=(8, 12))
            r.raise_for_status()
            ambiente["ip_publico_observado"] = r.text.strip()
        except requests.RequestException as exc:
            ambiente["erro_ip"] = str(exc)
    print("=== AMBIENTE ===")
    for k, v in ambiente.items():
        print(f"{k}: {v}")
    with requests.Session() as sessao:
        sessao.headers.update(HEADERS)
        resultados = [
            testar(sessao, "API datastore_search", API, {
                "resource_id": RECURSO_2026, "limit": 1,
                "filters": json.dumps({"MES_REFERENCIA": "202611"}, separators=(",", ":")),
            }),
            testar(sessao, "CSV oficial 2026", CSV_2026),
        ]
    Path(args.saida).write_text(
        json.dumps({"ambiente": ambiente, "testes": resultados},
                   ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nDiagnostico salvo em: {args.saida}")
    print("Se ambos retornarem 403, solicitar canal/credenciais oficiais de integracao a CCEE.")


if __name__ == "__main__":
    main()
