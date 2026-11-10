"""Atualização incremental de 17 produtos BBCE e painel Preço de Mercado.

Entradas em dados/:
  historico_precos_bbce.csv             — histórico filtrado inicial
  todosnegocios.xlsx                    — novos negócios (nome único obrigatório)

Saídas em dados/:
  historico_precos_bbce.csv             — histórico de 17 produtos, atualizado
  precos_mercado_diario.csv             — médias diárias sem preenchimento artificial
  produtos_mercado.csv                  — catálogo dos 17 produtos
Saída em gráficos/: mercado.html, além da atualização da aba no index.html.

Média diária ponderada por Q.M (MWh). Preços dos dias não operados são
carregados apenas na visualização e na comparação, nunca gravados como negócios.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from plotly.offline import get_plotlyjs

ROOT = Path(__file__).resolve().parent
DADOS = ROOT / 'dados'
GRAFICOS = ROOT / 'graficos'
CSV = DADOS / 'historico_precos_bbce.csv'
DIARIO = DADOS / 'precos_mercado_diario.csv'
CATALOGO = DADOS / 'produtos_mercado.csv'
HTML = GRAFICOS / 'mercado.html'
INDEX = GRAFICOS / 'index.html'
VERDE = '#007E80'
COLUNAS = ['PRODUTO', 'DATA/HORA', 'Q.N', 'U.N.', 'Q.M', 'U.M.', 'PREÇO',
           'TIPO DE CONTRATO', 'TENDÊNCIA', 'STATUS']

# Os 17 produtos são uma lista fechada, preservando os nomes oficiais.
# Corrigidos dois erros de pontuação do pedido: MAR/27 e Q2_27 são itens
# distintos, e o código do quarto trimestre de 2027 é Q4_27.
PRODUTOS = [
    ('FEN - SE CON MEN OUT/26 - Preço Fixo', 'OUT/26'),
    ('FEN - SE CON MEN NOV/26 - Preço Fixo', 'NOV/26'),
    ('FEN - SE CON MEN DEZ/26 - Preço Fixo', 'DEZ/26'),
    ('FEN - SE CON TRI OUT/26 DEZ/26 - Preço Fixo', 'Q4_26'),
    ('FEN - SE CON ANU JAN/27 DEZ/27 - Preço Fixo', '2027'),
    ('FEN - SE CON TRI JAN/27 MAR/27 - Preço Fixo', 'Q1_27'),
    ('FEN - SE CON MEN JAN/27 - Preço Fixo', 'JAN/27'),
    ('FEN - SE CON MEN FEV/27 - Preço Fixo', 'FEV/27'),
    ('FEN - SE CON MEN MAR/27 - Preço Fixo', 'MAR/27'),
    ('FEN - SE CON TRI ABR/27 JUN/27 - Preço Fixo', 'Q2_27'),
    ('FEN - SE CON TRI JUL/27 SET/27 - Preço Fixo', 'Q3_27'),
    ('FEN - SE CON TRI OUT/27 DEZ/27 - Preço Fixo', 'Q4_27'),
    ('FEN - SE CON ANU JAN/28 DEZ/28 - Preço Fixo', '2028'),
    ('FEN - SE CON ANU JAN/29 DEZ/29 - Preço Fixo', '2029'),
    ('FEN - SE CON ANU JAN/30 DEZ/30 - Preço Fixo', '2030'),
    ('FEN - SE CON ANU JAN/31 DEZ/31 - Preço Fixo', '2031'),
    ('FEN - SE CON ANU JAN/32 DEZ/32 - Preço Fixo', '2032'),
]
ORIGINAL_A_APELIDO = dict(PRODUTOS)
APELIDO_A_ORIGINAL = {apelido: original for original, apelido in PRODUTOS}
DEFAULT_CARDS = ['Q1_27', '2027', '2028', '2029', '2030']
PERIODOS = [('1d', 'Dia anterior'), ('7d', 'Semana anterior'),
            ('15d', '15 dias atrás'), ('30d', '30 dias atrás'),
            ('1y', 'Ano passado'), ('data', 'Escolher data')]


def corrigir_texto(valor) -> str:
    if pd.isna(valor):
        return ''
    s = ' '.join(str(valor).split())
    if any(x in s for x in ('Ã', 'Â', 'â€')):
        try:
            s = s.encode('latin-1').decode('utf-8')
        except (UnicodeError, ValueError):
            pass
    return ' '.join(s.split())


def normalizar_cabecalho(s):
    s = unicodedata.normalize('NFKD', corrigir_texto(s))
    return ''.join(c for c in s.upper() if c.isalnum() and not unicodedata.combining(c))


def numero(v):
    if pd.isna(v) or str(v).strip() == '':
        return float('nan')
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(' ', '')
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    return pd.to_numeric(s, errors='coerce')


def normalizar(df: pd.DataFrame) -> pd.DataFrame:
    mapa = {normalizar_cabecalho(c): c for c in df.columns}
    faltando = [c for c in COLUNAS if normalizar_cabecalho(c) not in mapa]
    if faltando:
        raise ValueError(f'Colunas ausentes: {faltando}. Recebidas: {list(df.columns)}')
    out = df[[mapa[normalizar_cabecalho(c)] for c in COLUNAS]].copy()
    out.columns = COLUNAS
    for c in ['PRODUTO', 'U.N.', 'U.M.', 'TIPO DE CONTRATO', 'TENDÊNCIA', 'STATUS']:
        out[c] = out[c].map(corrigir_texto)
    out['DATA/HORA'] = pd.to_datetime(out['DATA/HORA'], dayfirst=True, errors='coerce')
    for c in ['Q.N', 'Q.M', 'PREÇO']:
        out[c] = out[c].map(numero).astype(float)
    out = out[out['PRODUTO'].isin(ORIGINAL_A_APELIDO)].copy()
    # Descartamos negócios cancelados; mantemos o status histórico "Não".
    out = out[~out['STATUS'].str.casefold().isin(['cancelado', 'cancelada', 'inativo', 'inativa'])]
    out = out.dropna(subset=['DATA/HORA', 'PREÇO'])
    return out[COLUNAS]


def carregar_historicos() -> pd.DataFrame:
    """Lê exclusivamente o CSV histórico de 17 produtos, sem duplicar bases.

    Os antigos arquivos .csv.gz não participam mais da leitura e podem ser
    removidos do repositório depois da primeira atualização validada.
    """
    if not CSV.exists():
        raise FileNotFoundError('Envie dados/historico_precos_bbce.csv ao repositório.')
    bruto = pd.read_csv(CSV, encoding='utf-8-sig', low_memory=False)
    base = normalizar(bruto)
    print(f'Mercado: {len(base):,} linhas dos produtos-alvo no histórico CSV.')
    return base


def localizar_excel() -> Path | None:
    """O usuário sempre envia dados/todosnegocios.xlsx (grafia correta)."""
    correto = DADOS / 'todosnegocios.xlsx'
    if correto.is_file():
        return correto
    outros = [p.name for p in DADOS.iterdir()
              if p.is_file() and p.name.lower() in {'todosnegicios.xlsx', 'todosnegócios.xlsx'}]
    if outros:
        raise FileNotFoundError(
            f'Arquivo com nome incorreto: {outros}. Renomeie para dados/todosnegocios.xlsx.')
    return None


def importar_novos() -> pd.DataFrame | None:
    excel = localizar_excel()
    if not excel:
        print('Mercado: não há planilha nova em dados/; será usado o histórico existente.')
        return None
    bruto = pd.read_excel(excel, engine='openpyxl')
    novo = normalizar(bruto)
    print(f'Mercado: {len(bruto):,} registros no Excel; {len(novo):,} válidos dos 17 produtos, excluídos cancelados.')
    return novo


def consolidar(base, novos):
    base_unica = base.drop_duplicates(subset=COLUNAS, keep='first')
    partes = [base_unica] if novos is None else [base_unica, novos]
    junto = pd.concat(partes, ignore_index=True)
    n_antes = len(junto)
    junto = junto.drop_duplicates(subset=COLUNAS, keep='first')
    junto = junto.sort_values(['DATA/HORA', 'PRODUTO', 'PREÇO'], kind='stable').reset_index(drop=True)
    novos_unicos = len(junto) - len(base_unica)
    print(f'Mercado: {novos_unicos:,} novos negócios incorporados; '
          f'{n_antes-len(junto):,} repetidos na importação; '
          f'{len(junto):,} registros consolidados.')
    return junto


def calc_diario(hist):
    ativos = hist.copy()
    ativos['APELIDO'] = ativos['PRODUTO'].map(ORIGINAL_A_APELIDO)
    ativos['DIA'] = ativos['DATA/HORA'].dt.normalize()
    ativos['PESO'] = ativos['Q.M'].where(ativos['Q.M'] > 0, 0).fillna(0)
    ativos['POND'] = ativos['PREÇO'] * ativos['PESO']
    agg = ativos.groupby(['APELIDO', 'DIA'], as_index=False).agg(
        SOMA_POND=('POND', 'sum'), VOLUME_MWH=('PESO', 'sum'),
        MEDIA_SIMPLES=('PREÇO', 'mean'), NEGOCIOS=('PREÇO', 'size'))
    agg['PRECO_MEDIO'] = agg['SOMA_POND'].div(agg['VOLUME_MWH'].where(agg['VOLUME_MWH'] > 0))\
                                   .fillna(agg['MEDIA_SIMPLES'])
    return agg[['APELIDO', 'DIA', 'PRECO_MEDIO', 'VOLUME_MWH', 'NEGOCIOS']].sort_values(['APELIDO', 'DIA'])


def inserir_aba_index():
    """Integra Mercado a versões novas e antigas do menu, sem perder outras abas.

    A ENA atual usa modulosDisponiveis() e NÃO contém um array literal na
    função selecionar(). Nesse caso, basta criar a seção e o botão: o JavaScript
    descobre ambos pelo DOM. Apenas os menus legados exigem atualizar arrays.
    """
    if not INDEX.exists():
        print('Mercado: graficos/index.html ausente; gerado apenas mercado.html.')
        return
    html = INDEX.read_text(encoding='utf-8')
    obrigatorias = ('ear', 'ena', 'pld')
    faltantes = [nome for nome in obrigatorias
                 if f'id="botao-{nome}"' not in html or f'id="aba-{nome}"' not in html]
    if faltantes:
        raise RuntimeError(f'Index sem as abas básicas {faltantes}; arquivo preservado.')
    if '</nav>' not in html or '</main>' not in html:
        raise RuntimeError('Index sem nav/main completos; arquivo preservado.')

    botao = ('<button role="tab" id="botao-mercado" aria-selected="false" '
             'aria-controls="aba-mercado" onclick="selecionar(\'mercado\')">'
             'Mercado · Negócios</button>')
    secao = ('<section class="aba" id="aba-mercado" role="tabpanel" '
             'aria-labelledby="botao-mercado"><div class="molde"><iframe '
             'id="quadro-mercado" title="Preço de Mercado" src="mercado.html" '
             'loading="eager" style="height:1250px"></iframe></div></section>')

    if 'id="botao-mercado"' not in html:
        html = html.replace('</nav>', botao + '\n</nav>', 1)
    if 'id="aba-mercado"' not in html:
        html = html.replace('</main>', secao + '\n</main>', 1)

    # A versão nova de atualizar_ena.py usa modulosDisponiveis(): o menu é
    # dinâmico e não se deve exigir uma lista literal dentro de selecionar().
    if re.search(r'function\s+modulosDisponiveis\s*\(', html):
        print('Mercado: navegação dinâmica da ENA detectada; mantendo o JavaScript original.')
    else:
        # Compatibilidade com as versões antigas, nas quais as abas aparecem
        # explicitamente em arrays no JavaScript. Atualiza SOMENTE arrays
        # de nomes de abas, preservando as demais estruturas do script.
        ordem = ('operacao', 'ena', 'ear', 'pld', 'mercado')
        permitidas = set(ordem)
        def atualizar_array(match):
            itens = re.findall(r"['\"]([^'\"]+)['\"]", match.group('itens'))
            if {'ear', 'ena', 'pld'}.issubset(itens) and set(itens).issubset(permitidas):
                ativos = set(itens) | {'mercado'}
                novo = '[' + ','.join(repr(x) for x in ordem if x in ativos) + ']'
                return match.group('prefixo') + novo + match.group('sufixo')
            return match.group(0)

        html, n_loops = re.subn(
            r'(?P<prefixo>for\s*\(\s*const\s+\w+\s+of\s*)'
            r'\[(?P<itens>[^\]\r\n]*)\](?P<sufixo>)',
            atualizar_array, html)
        html, n_visao = re.subn(
            r'(?P<prefixo>)\[(?P<itens>[^\]\r\n]*)\]'
            r'(?P<sufixo>\s*\.includes\(\s*visao\s*\))',
            atualizar_array, html)
        print(f'Mercado: listas do menu legado verificadas ({n_loops} laços, {n_visao} seleções).')

    if 'Fonte: ONS / CCEE / Mercado' not in html:
        html = html.replace('Fonte: ONS / CCEE', 'Fonte: ONS / CCEE / Mercado', 1)

    # Ordem preferida nos botões. Operação pode ser acrescentada posteriormente
    # por atualizar_operacao.py, sem exigir sua presença neste momento.
    ordem = ('operacao', 'ena', 'ear', 'pld', 'mercado')
    padrao = re.compile(
        r'<button\s+role="tab"\s+id="botao-(operacao|ena|ear|pld|mercado)"[^>]*>.*?</button>',
        re.S)
    achados = {m.group(1):m.group(0) for m in padrao.finditer(html)}
    if len(achados) >= 3:
        sequencia_atual = [m.group(1) for m in padrao.finditer(html)]
        nova_sequencia = [k for k in ordem if k in achados]
        if sequencia_atual != nova_sequencia:
            html = padrao.sub('', html)
            html = html.replace('</nav>', '\n'.join(achados[k] for k in nova_sequencia) + '\n</nav>', 1)

    # Escreve só após todas as validações; evita corromper o painel em falhas.
    INDEX.write_text(html, encoding='utf-8')
    print('Mercado: aba integrada ao index.html sem alterar outras seções.')


def pagina_mercado(diario, hoje):
    # Catálogo fixo: todos os 17 produtos da solicitação continuam selecionáveis.
    mapa = {}
    for alias, g in diario.groupby('APELIDO'):
        mapa[alias] = {
            'datas': g['DIA'].dt.strftime('%Y-%m-%d').tolist(),
            'precos': [round(float(x), 5) for x in g['PRECO_MEDIO']],
            'qtd': [int(x) for x in g['NEGOCIOS']],
            'volumes': [round(float(x), 2) for x in g['VOLUME_MWH']],
        }
    labels = [alias for _, alias in PRODUTOS]
    ultimo_dia = max((pd.Timestamp(d) for x in mapa.values() for d in x['datas']), default=hoje)
    dados_js = json.dumps(mapa, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    nomes_js = json.dumps(labels, ensure_ascii=False)
    originais_js = json.dumps(APELIDO_A_ORIGINAL, ensure_ascii=False).replace('</', '<\\/')
    periodos_js = json.dumps(PERIODOS, ensure_ascii=False)
    defaults_js = json.dumps(DEFAULT_CARDS)
    plotly_js = get_plotlyjs()
    # A data de referência é a data mais nova que o histórico contém, e não a data
    # de processamento. Quando não houver negociação, usa o último dia anterior.
    return '''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preço de Mercado — Acompanhamento de mercado</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#193245;font-family:'Century Gothic',CenturyGothic,Arial,sans-serif}
.panel{border:1px solid #e3eaf0;border-radius:15px;overflow:hidden;background:#fff;margin:0 10px 16px}
.bar{background:#007E80;color:white;padding:13px 18px;text-align:center;font-size:17px;font-weight:700}
.caption{font-size:12px;color:#667085;line-height:1.5;margin:12px 20px}
.cards{padding:17px 17px 20px;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.card{min-width:0;border:1px solid #dae5ec;border-radius:13px;background:#f8fafb;padding:12px;box-shadow:0 1px 3px #12233308}
.card-top{display:flex;align-items:center;justify-content:space-between;gap:7px;margin-bottom:12px}
.card-index{font-size:16px;letter-spacing:-.015em;color:#007E80;font-weight:800}
.card-date{font-size:10px;color:#667085}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:11px}
.field label{font-size:10px;color:#667085;font-weight:700}
.field select,.field input[type=date]{width:100%;min-width:0;padding:8px 7px;font:inherit;font-size:12px;border-radius:7px;border:1px solid #cbd8df;background:#fff;color:#16384a}
.field input[type=date]{color-scheme:light;min-height:36px}
.field.calendario{margin-top:-2px;margin-bottom:11px}
.strong-number{font-size:24px;font-weight:750;letter-spacing:-.04em;color:#113e4c;line-height:1.15}
.metric-label{color:#64748b;font-size:10px;margin-top:4px}
.comparison{border-top:1px solid #dce5ea;margin-top:11px;padding-top:10px;display:grid;grid-template-columns:1fr auto;gap:8px;align-items:end}
.compare-number{font-size:16px;font-weight:700;color:#385467}
.delta{font-weight:750;font-size:14px;white-space:nowrap}
.delta-indice{font-weight:700;font-size:13px;white-space:nowrap;margin-top:6px}
.delta-indice.pos{color:#b45309}.delta-indice.neg{color:#0f766e}.delta-indice.zero{color:#64748b}
.delta-unidade{font-weight:500;font-size:10px;letter-spacing:0}
.delta-duplo{text-align:right}
.delta.pos{color:#b45309}.delta.neg{color:#0f766e}.delta.zero{color:#64748b}
.card-foot{font-size:10px;color:#64748b;margin-top:9px;line-height:1.5;overflow-wrap:anywhere}
.toolbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;padding:10px 20px 8px}
.search{min-width:205px;flex:1;font:inherit;font-size:13px;padding:11px 13px;border:1px solid #cbd8df;border-radius:9px}
.toolbar button{padding:10px 13px;border:1px solid #c8d5db;border-radius:8px;background:#fff;color:#24525b;cursor:pointer;font:inherit;font-size:12px}
.selection{padding:2px 20px 10px;display:flex;flex-wrap:wrap;gap:7px}
.chip{display:inline-flex;gap:8px;align-items:center;border-radius:20px;border:1px solid #d8e2ea;padding:7px 9px;font-size:12px;background:#f6fafb}
.chip button{border:0;background:transparent;cursor:pointer;color:#637b83;font-size:14px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex-shrink:0}
.catalog{padding:10px;max-height:118px;overflow:auto;margin:0 20px 8px;border:1px solid #e4eaf0;border-radius:9px;display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.catalog label{cursor:pointer;display:flex;align-items:center;gap:8px;font-size:12px;padding:7px;border-radius:7px;background:#f8fafb}
.catalog input{accent-color:#007E80}.catalog label:hover{background:#e8f5f3}
#market-chart{width:100%;height:610px;background:#0e0e0e}
.chart-shell{margin:4px 18px 8px;border:1px solid #292929;border-radius:10px;overflow:hidden;background:#0e0e0e}
@media(max-width:700px){.chart-shell{margin:4px 8px}.delta-duplo{text-align:left}}
.note{padding:0 20px 15px;color:#667085;font-size:11px;line-height:1.55}
@media(max-width:1400px){.cards{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.strong-number{font-size:21px}}
@media(max-width:540px){.cards{grid-template-columns:1fr}.toolbar{padding:12px}.catalog{margin:0 12px 8px}.selection{padding:4px 12px 8px}}

/* MTM */
.mtm-layout{display:grid;grid-template-columns:minmax(300px,370px) minmax(0,1fr);gap:16px;padding:17px}
.mtm-editor{background:#f8fafb;border:1px solid #dfe8ed;border-radius:12px;padding:16px;align-self:start}
.mtm-editor h3{margin:0 0 12px;color:#164c53;font-size:16px}
.mtm-editor label{display:block;font-size:11px;font-weight:700;color:#667085;margin:11px 0 5px}
.mtm-editor input,.mtm-editor select{width:100%;padding:10px;border:1px solid #cbd8df;border-radius:8px;background:white;color:#193245;font:inherit;font-size:13px}
.mtm-pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.mtm-btn{background:#007E80!important;color:#fff!important;border:0!important;border-radius:8px;cursor:pointer;padding:11px 14px;font:inherit;font-size:12px;font-weight:700}
.mtm-btn.alt{background:white!important;color:#007E80!important;border:1px solid #007E80!important}
.mtm-btn.danger{background:#fff!important;color:#b42318!important;border:1px solid #e8b9b6!important}
.mtm-toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:15px 0}
.mtm-components{border:1px dashed #b7d1d2;border-radius:10px;padding:9px;margin-top:7px;background:white}
.mtm-component{display:flex;align-items:center;justify-content:space-between;gap:9px;padding:7px 4px;border-bottom:1px solid #edf0f3;font-size:12px}
.mtm-component:last-child{border-bottom:0}
.mtm-component button{border:0;background:transparent;color:#a32929;cursor:pointer;font-size:18px}
.mtm-main{min-width:0}
.mtm-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}
.mtm-metric{background:#f8fafb;border:1px solid #dfe7ed;border-radius:11px;padding:13px;min-width:0}
.mtm-metric small{display:block;font-size:11px;color:#64748b;margin-bottom:6px}
.mtm-metric strong{font-size:22px;color:#164454;white-space:nowrap}
.mtm-chart-shell{border-radius:11px;overflow:hidden;background:#0e0e0e}
#mtm-chart{height:410px;width:100%}
.mtm-op-table{width:100%;border-collapse:collapse;font-size:12px}
.mtm-op-table td,.mtm-op-table th{padding:10px;border-bottom:1px solid #e7edf0;text-align:left}
.mtm-op-table th{color:#64748b;background:#f8fafb}
.mtm-table-wrap{overflow:auto;border:1px solid #e7edf0;border-radius:9px;margin-top:12px}
.mtm-pick{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.mtm-pick select{padding:8px 10px;border:1px solid #cbd8df;border-radius:8px;font:inherit;font-size:12px;max-width:270px}
.mtm-small{font-size:11px;color:#64748b;line-height:1.6;margin:8px 0}
.mtm-message{font-size:12px;color:#b42318;min-height:17px}
.mtm-empty{color:#64748b;font-size:13px;padding:20px;text-align:center}
@media(max-width:970px){.mtm-layout{grid-template-columns:1fr}.mtm-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:560px){.mtm-metrics{grid-template-columns:1fr}.mtm-pair{grid-template-columns:1fr}}
</style>
<script>''' + plotly_js + '''</script></head><body>
<section class="panel"><div class="bar">Preço de Mercado — Resumo</div>
<div id="market-cards" class="cards"></div>
</section>

<section class="panel"><div class="bar">Preço de Mercado — Evolução diária</div>
<p class="caption">Preço médio diário ponderado por MWh. Nos dias sem negócios, mantém-se o último preço disponível.</p>
<div class="toolbar"><input type="search" class="search" id="search" placeholder="Buscar produto (ex.: Q1_27, 2028, NOV/26)" aria-label="Buscar produto">
<button type="button" id="clear">Limpar seleção</button><button type="button" id="add-visible">Adicionar resultados</button></div>
<div class="selection" id="selected"></div>
<div class="catalog" id="catalog" aria-label="Produtos negociados"></div>
<div class="chart-shell"><div id="market-chart"></div></div>
<footer class="note">Fonte: BBCE</footer>
</section>
<section class="panel" id="mtm-section"><div class="bar">Marcação a Mercado (MTM) — Operações e Swaps</div>
<div class="mtm-layout"><aside class="mtm-editor">
<h3>Montagem da operação</h3>
<label for="mtm-name">Nome da operação</label><input id="mtm-name" placeholder="Ex.: Swap Q1/27 × 2028">
<label for="mtm-date">Data de fechamento</label><input id="mtm-date" type="date">
<div class="mtm-pair"><div><label for="mtm-side">Ponta</label><select id="mtm-side"><option value="compra">Compra</option><option value="venda">Venda</option></select></div>
<div><label for="mtm-volume">Volume (MWm)</label><input id="mtm-volume" type="number" min="0.0001" step="any" value="5"></div></div>
<label for="mtm-price">Preço contratado (R$/MWh)</label><input id="mtm-price" type="number" min="0" step="any" value="230">
<label for="mtm-product">Produto da ponta (ou componente mensal)</label><select id="mtm-product"></select>
<div class="mtm-toolbar"><button type="button" id="mtm-add-component" class="mtm-btn alt">+ Adicionar produto para marcação de mercado</button><button type="button" id="mtm-clear-components" class="mtm-btn alt">Limpar produtos</button></div>
<div class="mtm-components" id="mtm-components"></div>
<p class="mtm-small">Monte JAN/27 + FEV/27 + MAR/27 para formar Q1/27. Produtos sobrepostos não podem ficar na mesma ponta; pontas de compra e venda podem ter períodos coincidentes.</p>
<button type="button" id="mtm-add-leg" class="mtm-btn" style="width:100%;margin:5px 0">+ Incluir outros produtos na operação</button>
<label>Produtos da operação em montagem</label><div class="mtm-components" id="mtm-draft-legs"></div>
<div class="mtm-toolbar"><button type="button" id="mtm-close" class="mtm-btn">Fechar operação</button><button type="button" id="mtm-new" class="mtm-btn alt">+ MTM operação</button></div>
<div id="mtm-message" class="mtm-message" role="status"></div>
</aside><div class="mtm-main">
<div class="mtm-metrics"><div class="mtm-metric"><small>MTM consolidado da carteira</small><strong id="mtm-total">—</strong></div>
<div class="mtm-metric"><small>Operações cadastradas</small><strong id="mtm-count">0</strong></div>
<div class="mtm-metric"><small>Data da última cotação disponível</small><strong id="mtm-ref">—</strong></div></div>
<div class="mtm-pick"><label for="mtm-focus" style="font-size:12px;font-weight:700">Escopo:</label><select id="mtm-focus"><option value="all">Carteira consolidada</option></select>
<label for="mtm-view" style="font-size:12px;font-weight:700">Gráfico:</label><select id="mtm-view"><option value="products">MTM individual de cada produto (todas as séries)</option><option value="total">Soma da carteira (série única)</option></select></div>
<div class="mtm-pick"><button type="button" class="mtm-btn" id="mtm-export-excel">Exportar Excel — gráficos e memória de cálculo</button></div>
<div class="mtm-chart-shell"><div id="mtm-chart"></div></div>
<div class="mtm-table-wrap"><table class="mtm-op-table"><thead><tr><th>Operação</th><th>Estrutura</th><th>Produtos / pontas</th><th>MTM (MMR$)</th><th></th></tr></thead><tbody id="mtm-rows"></tbody></table></div>
<p class="mtm-small">Compra: (mercado − preço contratado) × MWm × horas / 1.000.000; venda: sinal inverso. Um swap soma o MTM de todas os seus produtos. Para produtos compostos, cada mês usa sua própria cotação e quantidade de horas. Na visualização por produto, cada série identifica o produto e a operação correspondente; produtos iguais em operações distintas aparecem em séries separadas. Sem cotação anterior, o MTM é indisponível. Fonte: BBCE. Operações salvas neste navegador, não compartilhadas entre dispositivos.</p>
</div></div></section>
<script>
const DIAS=''' + dados_js + ''';
const NOMES=''' + nomes_js + ''';
const ORIGINAIS=''' + originais_js + ''';
const PERIODOS=''' + periodos_js + ''';
const DEFAULTS=''' + defaults_js + ''';
const HOJE=''' + json.dumps(hoje.strftime('%Y-%m-%d')) + ''';
const ULTIMO=''' + json.dumps(pd.Timestamp(ultimo_dia).strftime('%Y-%m-%d')) + ''';
const CORES=NOMES.map((_,i)=>`hsl(${Math.round((i*137.508)%360)}, ${i%3===0?77:72}%, ${i%4===0?62:55}%)`); // 17 cores diferentes, uma por produto
const CARTOES=Array.from({length:5},(_,i)=>({produto:DEFAULTS[i],periodo:'7d',dataReferencia:addDays(ULTIMO,-7)}));
const ANO_CORRENTE=Number(HOJE.slice(0,4));
const PRODUTO_INICIAL=NOMES.includes(String(ANO_CORRENTE))?String(ANO_CORRENTE):(NOMES.includes('Q4_'+String(ANO_CORRENTE).slice(-2))?'Q4_'+String(ANO_CORRENTE).slice(-2):NOMES.find(x=>x.includes('/'+String(ANO_CORRENTE).slice(-2)))||NOMES[0]);
const SEL=new Set([PRODUTO_INICIAL]);
const cardEl=document.getElementById('market-cards');
const chart=document.getElementById('market-chart');
const catalogEl=document.getElementById('catalog');
const selectedEl=document.getElementById('selected');
const searchEl=document.getElementById('search');
function safe(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function fmt(n){return n===null||n===undefined||!Number.isFinite(n)?'—':Number(n).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2})}
function br(iso){return iso?iso.slice(8,10)+'/'+iso.slice(5,7)+'/'+iso.slice(0,4):'—'}
function addDays(iso,n){let d=new Date(iso+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}
function subtractYear(iso){let d=new Date(iso+'T00:00:00Z');let dia=d.getUTCDate();d.setUTCFullYear(d.getUTCFullYear()-1);if(d.getUTCDate()!==dia)d.setUTCDate(0);return d.toISOString().slice(0,10)}
function dataComparacao(d,p,personalizada){return p==='data'?(personalizada||addDays(d,-7)):p==='1d'?addDays(d,-1):p==='7d'?addDays(d,-7):p==='15d'?addDays(d,-15):p==='30d'?addDays(d,-30):subtractYear(d)}
function acharPreco(nome,data){let d=DIAS[nome];if(!d||!d.datas.length)return null;
 let i=d.datas.length-1;while(i>=0&&d.datas[i]>data)i--;
 let futuro=false;if(i<0){i=0;futuro=true} // ausência anterior: usa o primeiro registro da série.
 return {valor:d.precos[i],dia:d.datas[i],semAnterior:futuro};}
function montarCards(){cardEl.innerHTML=CARTOES.map((c,i)=>{
 let atual=acharPreco(c.produto,ULTIMO),alvo=dataComparacao(ULTIMO,c.periodo,c.dataReferencia),antigo=acharPreco(c.produto,alvo);
 let delta=atual&&antigo?atual.valor-antigo.valor:null;
 // Índice de preços: referência = 100; sua diferença é expressa em pontos percentuais.
 let deltaIndice=atual&&antigo&&antigo.valor!==0?(100*atual.valor/antigo.valor-100):null;
 let classe=delta===null||Math.abs(delta)<.00001?'zero':delta>0?'pos':'neg';
 let sinal=delta===null?'':delta>0?'+':'';
 let opcoes=NOMES.map(p=>`<option value="${safe(p)}" ${p===c.produto?'selected':''}>${safe(p)}</option>`).join('');
 let pp=PERIODOS.map(([id,rot])=>`<option value="${id}" ${id===c.periodo?'selected':''}>${rot}</option>`).join('');
 let aviso=antigo&&antigo.semAnterior?'Sem registro anterior à data-alvo; usado o primeiro preço disponível.':'';
 return `<article class="card" data-card="${i}"><div class="card-top"><span class="card-index">${safe(c.produto)}</span><span class="card-date">Ref. ${br(ULTIMO)}</span></div>
 <div class="field"><label>Produto</label><select class="card-produto" aria-label="Produto do card ${i+1}">${opcoes}</select></div>
 <div class="field"><label>Comparar com</label><select class="card-periodo" aria-label="Período do card ${i+1}">${pp}</select></div>
 ${c.periodo==='data'?`<div class="field calendario"><label>Data de referência</label><input class="card-data" type="date" aria-label="Data de referência do card ${i+1}" max="${ULTIMO}" value="${c.dataReferencia||addDays(ULTIMO,-7)}"></div>`:''}
 <div><div class="strong-number">${atual?'R$ '+fmt(atual.valor):'—'}</div><div class="metric-label">Preço atual · último registro ${atual?br(atual.dia):'—'}</div></div>
 <div class="comparison"><div><div class="compare-number">${antigo?'R$ '+fmt(antigo.valor):'—'}</div><div class="metric-label">Preço na referência · ${br(alvo)}${antigo?' (registro '+br(antigo.dia)+')':''}</div></div>
 <div class="delta-duplo"><div class="delta ${classe}">Δ ${delta===null?'—':sinal+fmt(delta)} <span class="delta-unidade">R$/MWh</span></div>
 <div class="delta-indice ${classe}">Δ ${deltaIndice===null?'—':(deltaIndice>0?'+':'')+fmt(deltaIndice)} <span class="delta-unidade">p.p.</span></div></div></div>
 ${aviso?`<div class="card-foot">${aviso}</div>`:''}
 </article>`;}).join('');
 cardEl.querySelectorAll('.card').forEach(div=>{
  const i=Number(div.dataset.card);div.querySelector('.card-produto').addEventListener('change',e=>{CARTOES[i].produto=e.target.value;montarCards();ajustarAltura()});
  div.querySelector('.card-periodo').addEventListener('change',e=>{CARTOES[i].periodo=e.target.value;montarCards();ajustarAltura()});
  const calendario=div.querySelector('.card-data');
  if(calendario)calendario.addEventListener('change',e=>{
    if(!e.target.value||e.target.value>ULTIMO)return;
    CARTOES[i].dataReferencia=e.target.value;montarCards();ajustarAltura();
  });
 });}
function cor(nome){return CORES[Math.max(0,NOMES.indexOf(nome))%CORES.length]}
function catalogo(){const q=searchEl.value.trim().toUpperCase();catalogEl.innerHTML=NOMES.filter(p=>p.toUpperCase().includes(q)).map(p=>
 `<label title="${safe(ORIGINAIS[p])}"><input type="checkbox" data-name="${safe(p)}" ${SEL.has(p)?'checked':''}><span class="dot" style="background:${cor(p)}"></span>${safe(p)}</label>`).join('');
 catalogEl.querySelectorAll('input').forEach(el=>el.addEventListener('change',()=>{el.checked?SEL.add(el.dataset.name):SEL.delete(el.dataset.name);atualizarChart()}));}
function tokens(){selectedEl.innerHTML=[...SEL].map(p=>`<span class="chip"><span class="dot" style="background:${cor(p)}"></span>${safe(p)}<button title="Remover ${safe(p)}" data-name="${safe(p)}">×</button></span>`).join('');
 selectedEl.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{SEL.delete(b.dataset.name);atualizarChart()}));}
function curve(nome){const d=DIAS[nome];if(!d||!d.datas.length)return null;
 let registros=new Map(d.datas.map((x,i)=>[x,{v:d.precos[i],q:d.qtd[i],vol:d.volumes[i]}]));
 let x=[],y=[],detalhe=[];let v=null;
 for(let t=Date.parse(d.datas[0]+'T00:00:00Z');t<=Date.parse(HOJE+'T00:00:00Z');t+=86400000){
  let iso=new Date(t).toISOString().slice(0,10),r=registros.get(iso);if(r)v=r.v;
  x.push(iso);y.push(v);detalhe.push(r?`Negociado (${r.q} negócios; ${fmt(r.vol)} MWh)`:'Carregado do último dia com negócio');
 }
 return {x,y,customdata:detalhe,name:nome,mode:'lines',line:{color:cor(nome),width:2.5,shape:'hv'},
 hovertemplate:'<b>'+safe(nome)+'</b><br>%{x|%d/%m/%Y}<br>R$ %{y:.2f}/MWh<br>%{customdata}<extra></extra>'};}
function desenhar(){
 let trs=[...SEL].map(curve).filter(Boolean);
 const pequeno=window.innerWidth<850;
 Plotly.react(chart,trs,{
  height:pequeno?570:615,
  margin:{l:pequeno?68:92,r:pequeno?24:220,t:64,b:pequeno?96:70},
  font:{family:'Century Gothic,CenturyGothic,Arial,sans-serif',color:'#E0E0E0',size:13},
  paper_bgcolor:'#0e0e0e',plot_bgcolor:'#0e0e0e',hovermode:'closest',
  title:{text:'Preço Médio de Mercado',x:.055,xanchor:'left',font:{color:'#E0E0E0',size:23}},
  hoverlabel:{bgcolor:'#1f1f1f',bordercolor:'#444',font:{color:'#FFFFFF',size:14}},
  xaxis:{title:'Dia da operação',type:'date',showline:true,linecolor:'#737373',linewidth:1,
    showgrid:true,gridcolor:'rgba(255,255,255,.08)',ticks:'outside',tickcolor:'#777',
    tickfont:{color:'#D6D6D6',size:12},titlefont:{color:'#E0E0E0'},zeroline:false},
  yaxis:{title:{text:'Preço (R$/MWh)',font:{color:'#E0E0E0',size:14},standoff:14},showline:true,linecolor:'#737373',linewidth:1,
    showgrid:true,gridcolor:'rgba(255,255,255,.08)',ticks:'outside',tickcolor:'#777',
    tickfont:{color:'#D6D6D6',size:12},titlefont:{color:'#E0E0E0'},zeroline:false},
  legend:pequeno?{orientation:'h',x:.5,xanchor:'center',y:-.22,yanchor:'top',
    bgcolor:'rgba(20,20,20,.62)',font:{color:'#fff',size:11}}:
    {orientation:'v',x:1.02,xanchor:'left',y:1,yanchor:'top',
    bgcolor:'rgba(20,20,20,.62)',bordercolor:'rgba(180,180,180,.2)',borderwidth:1,
    font:{color:'#fff',size:12}},
  showlegend:trs.length>0,
  annotations:trs.length?[]:[{text:'Selecione um produto',x:.5,y:.5,xref:'paper',yref:'paper',
    showarrow:false,font:{color:'#d5d5d5',size:16}}]
 },{responsive:true,displaylogo:false});}
function atualizarChart(){catalogo();tokens();desenhar();ajustarAltura()}
searchEl.addEventListener('input',catalogo);
document.getElementById('clear').addEventListener('click',()=>{SEL.clear();atualizarChart()});
document.getElementById('add-visible').addEventListener('click',()=>{let q=searchEl.value.trim().toUpperCase();NOMES.filter(x=>x.toUpperCase().includes(q)).forEach(x=>SEL.add(x));atualizarChart()});
function ajustarAltura(){if(window.parent!==window)window.parent.postMessage({tipo:'altura-painel-ons',id:'mercado',altura:document.body.scrollHeight+24},'*')}
window.addEventListener('load',ajustarAltura);window.addEventListener('resize',ajustarAltura);
if(window.ResizeObserver)new ResizeObserver(ajustarAltura).observe(document.body);
montarCards();atualizarChart();

// MTM — utiliza exclusivamente preços da série BBCE e suporta múltiplas pontas por operação.
const MTM_META={"OUT/26":{"start":"2026-10-01","end":"2026-11-01"},"NOV/26":{"start":"2026-11-01","end":"2026-12-01"},"DEZ/26":{"start":"2026-12-01","end":"2027-01-01"},"Q4_26":{"start":"2026-10-01","end":"2027-01-01"},"2027":{"start":"2027-01-01","end":"2028-01-01"},"Q1_27":{"start":"2027-01-01","end":"2027-04-01"},"JAN/27":{"start":"2027-01-01","end":"2027-02-01"},"FEV/27":{"start":"2027-02-01","end":"2027-03-01"},"MAR/27":{"start":"2027-03-01","end":"2027-04-01"},"Q2_27":{"start":"2027-04-01","end":"2027-07-01"},"Q3_27":{"start":"2027-07-01","end":"2027-10-01"},"Q4_27":{"start":"2027-10-01","end":"2028-01-01"},"2028":{"start":"2028-01-01","end":"2029-01-01"},"2029":{"start":"2029-01-01","end":"2030-01-01"},"2030":{"start":"2030-01-01","end":"2031-01-01"},"2031":{"start":"2031-01-01","end":"2032-01-01"},"2032":{"start":"2032-01-01","end":"2033-01-01"}};
const mtmId=id=>document.getElementById(id);
const mtmDate=mtmId('mtm-date');mtmDate.max=HOJE;mtmDate.value=ULTIMO<=HOJE?ULTIMO:HOJE;
const mtmProd=mtmId('mtm-product');mtmProd.innerHTML=NOMES.map(s=>`<option value="${safe(s)}">${safe(s)}</option>`).join('');
const mtmKey='mercado-bbce-mtm-v2';
let mtmOps=[],mtmProducts=[],mtmDraftLegs=[];
const mtmMoney=v=>v===null||!Number.isFinite(v)?'Indisponível':(v<0?'-':'')+'R$ '+fmt(Math.abs(v))+' MM';
function mtmHours(s){let m=MTM_META[s];return m?Math.round((Date.parse(m.end+'T00:00:00Z')-Date.parse(m.start+'T00:00:00Z'))/3600000):0;}
function mtmPrice(s,day){let x=DIAS[s];if(!x||!x.datas.length)return null;let a=0,b=x.datas.length-1,idx=-1;while(a<=b){let mid=(a+b)>>1;if(x.datas[mid]<=day){idx=mid;a=mid+1;}else b=mid-1;}return idx<0?null:Number(x.precos[idx]);}
function mtmCheck(products){if(!Array.isArray(products)||!products.length||products.some(s=>!MTM_META[s]))return 'Selecione pelo menos um produto válido.';
 let r=products.map(s=>({name:s,start:MTM_META[s].start,end:MTM_META[s].end})).sort((a,b)=>a.start.localeCompare(b.start));
 for(let i=1;i<r.length;i++)if(r[i].start<r[i-1].end)return 'Os períodos '+r[i-1].name+' e '+r[i].name+' se sobrepõem na mesma ponta.';return '';}
function mtmNormalize(op){if(!op||!op.date||op.date>HOJE)return null;
 let legs=Array.isArray(op.positions)?op.positions:(Array.isArray(op.legs)?[{side:op.side,mw:op.mw,price:op.price,products:op.legs}]:null);
 if(!legs||!legs.length||legs.some(l=>!l||!['compra','venda'].includes(l.side)||!(Number(l.mw)>0)||!Number.isFinite(Number(l.price))||Number(l.price)<0||mtmCheck(l.products)))return null;
 return {id:String(op.id||'op-'+Date.now()),name:String(op.name||'Operação'),date:op.date,positions:legs.map(l=>({side:l.side,mw:Number(l.mw),price:Number(l.price),products:[...l.products]}))};}
try{let raw=JSON.parse(localStorage.getItem(mtmKey)||'[]');if(Array.isArray(raw))mtmOps=raw.map(mtmNormalize).filter(Boolean);
 if(!localStorage.getItem(mtmKey)){let prev=JSON.parse(localStorage.getItem('mercado-bbce-mtm-v1')||'[]');if(Array.isArray(prev))mtmOps=prev.map(mtmNormalize).filter(Boolean);}}catch(e){}
function mtmValuePosition(l,s,d){let p=mtmPrice(s,d);return p===null?null:(p-l.price)*l.mw*mtmHours(s)*(l.side==='compra'?1:-1)/1000000;}
function mtmVal(op,day){let result=0;for(let l of op.positions)for(let s of l.products){let v=mtmValuePosition(l,s,day);if(v===null)return null;result+=v;}return result;}
function mtmSave(){try{localStorage.setItem(mtmKey,JSON.stringify(mtmOps));}catch(e){mtmId('mtm-message').textContent='Armazenamento local indisponível neste navegador.';}}
function mtmRenderProducts(){mtmId('mtm-components').innerHTML=mtmProducts.length?mtmProducts.map((s,i)=>`<div class="mtm-component"><span><b>${safe(s)}</b> · ${fmt(mtmHours(s))} h</span><button data-i="${i}" aria-label="Remover ${safe(s)}">×</button></div>`).join(''):'<div class="mtm-empty">Adicione um ou mais produtos para esta ponta.</div>';
 mtmId('mtm-components').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{mtmProducts.splice(Number(b.dataset.i),1);mtmRenderProducts();}));}
function mtmRenderDraft(){mtmId('mtm-draft-legs').innerHTML=mtmDraftLegs.length?mtmDraftLegs.map((l,i)=>`<div class="mtm-component"><span><b>${i+1}. ${l.side==='compra'?'Compra':'Venda'} ${fmt(l.mw)} MWm</b> · R$ ${fmt(l.price)}/MWh<br>${l.products.map(safe).join(' + ')}</span><button data-i="${i}" aria-label="Remover ponta ${i+1}">×</button></div>`).join(''):'<div class="mtm-empty">Nenhuma ponta adicionada. Inclua compras e/ou vendas.</div>';
 mtmId('mtm-draft-legs').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{mtmDraftLegs.splice(Number(b.dataset.i),1);mtmRenderDraft();}));}
function mtmBuildLeg(){let mw=Number(mtmId('mtm-volume').value),pr=Number(mtmId('mtm-price').value),err=mtmCheck(mtmProducts);
 if(err)return [null,err];if(!(mw>0)||!Number.isFinite(mw))return [null,'Informe um volume positivo em MWm.'];if(mtmId('mtm-price').value===''||!Number.isFinite(pr)||pr<0)return [null,'Informe um preço contratado válido.'];
 return [{side:mtmId('mtm-side').value,mw,price:pr,products:[...mtmProducts]},''];}
function mtmRender(){mtmId('mtm-count').textContent=mtmOps.length;mtmId('mtm-ref').textContent=br(ULTIMO);
 let values=mtmOps.map(o=>mtmVal(o,HOJE)),total=mtmOps.length&&values.every(x=>x!==null)?values.reduce((a,b)=>a+b,0):null;
 mtmId('mtm-total').textContent=mtmOps.length?mtmMoney(total):'R$ 0,00 MM';
 let f=mtmId('mtm-focus'),old=f.value;f.innerHTML='<option value="all">Carteira consolidada</option>'+mtmOps.map(o=>`<option value="${safe(o.id)}">${safe(o.name)}</option>`).join('');f.value=[...f.options].some(o=>o.value===old)?old:'all';
 mtmId('mtm-rows').innerHTML=mtmOps.length?mtmOps.map((o,i)=>`<tr><td><strong>${safe(o.name)}</strong><div style="color:#64748b">${br(o.date)}</div></td><td>${o.positions.length>1?'Swap / '+o.positions.length+' pontas':o.positions[0].side==='compra'?'Compra':'Venda'}</td><td>${o.positions.map(l=>`<div>${l.side==='compra'?'Compra':'Venda'} ${fmt(l.mw)} MWm @ R$ ${fmt(l.price)} — ${l.products.map(safe).join(' + ')}</div>`).join('')}</td><td style="font-weight:700;color:${values[i]===null?'#64748b':values[i]>=0?'#0f766e':'#b42318'}">${mtmMoney(values[i])}</td><td><button class="mtm-btn danger" data-id="${safe(o.id)}">Excluir</button></td></tr>`).join(''):'<tr><td colspan="5" class="mtm-empty">Nenhuma operação cadastrada. Monte uma ou mais pontas e feche a operação.</td></tr>';
 mtmId('mtm-rows').querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{if(!window.confirm('Excluir esta operação?'))return;mtmOps=mtmOps.filter(o=>o.id!==b.dataset.id);mtmSave();mtmRender();}));mtmDraw();ajustarAltura();}
function mtmDraw(){let focus=mtmId('mtm-focus').value,view=mtmId('mtm-view').value,selected=focus==='all'?mtmOps:mtmOps.filter(o=>o.id===focus),traces=[];
 if(selected.length){let begin=selected.map(o=>o.date).sort()[0],xs=[];
 for(let t=Date.parse(begin+'T00:00:00Z');t<=Date.parse(HOJE+'T00:00:00Z');t+=86400000)xs.push(new Date(t).toISOString().slice(0,10));
 if(view==='total'){
  let ys=xs.map(d=>{let active=selected.filter(o=>o.date<=d);if(!active.length)return null;let vals=active.map(o=>mtmVal(o,d));return vals.every(v=>v!==null)?vals.reduce((a,b)=>a+b,0):null;});
  traces.push({x:xs,y:ys,mode:'lines',name:focus==='all'?'MTM total da carteira':selected[0].name,line:{color:'#48c9bd',width:3},connectgaps:false});
 }else{
  // Cada série corresponde à combinação operação + produto, sem misturar operações distintas.
  let entries=selected.flatMap(op=>[...new Set(op.positions.flatMap(l=>l.products))].map(prod=>({op,prod})));
  entries.forEach(({op,prod},i)=>{let ys=xs.map(d=>{if(op.date>d)return null;let legs=op.positions.filter(l=>l.products.includes(prod));let vals=legs.map(l=>mtmValuePosition(l,prod,d));return vals.every(v=>v!==null)?vals.reduce((a,b)=>a+b,0):null;});
   traces.push({x:xs,y:ys,mode:'lines',name:prod+' — '+op.name,connectgaps:false,line:{color:cor(prod),width:2.5,dash:['solid','dot','dash','dashdot'][selected.indexOf(op)%4]},hovertemplate:'<b>'+safe(prod)+' — '+safe(op.name)+'</b><br>%{x|%d/%m/%Y}<br>%{y:.4f} MMR$<extra></extra>'});});
 }}
 Plotly.react('mtm-chart',traces,{height:430,paper_bgcolor:'#0e0e0e',plot_bgcolor:'#0e0e0e',font:{family:'Century Gothic,Arial',color:'#eee'},margin:{l:82,r:traces.length>1?215:28,b:65,t:60},title:{text:view==='total'?'MTM consolidado — soma das operações':'MTM da carteira — uma série por produto',x:.03,xanchor:'left',font:{size:18}},xaxis:{title:'Data da marcação',showgrid:false,showline:true,linecolor:'#777'},yaxis:{title:'MTM (MMR$)',showgrid:true,gridcolor:'rgba(255,255,255,.09)',zeroline:true,zerolinecolor:'#a1a1a1'},showlegend:view==='products'&&traces.length>0,legend:{orientation:'v',x:1.02,y:1},hovermode:'x unified',annotations:traces.length?[]:[{text:'Cadastre uma operação para visualizar o histórico',xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{color:'#ddd',size:13}}]},{responsive:true,displaylogo:false});}
mtmId('mtm-add-component').addEventListener('click',()=>{let draft=[...mtmProducts,mtmProd.value],error=mtmCheck(draft);if(error){mtmId('mtm-message').textContent=error;return;}mtmProducts=draft;mtmId('mtm-message').textContent='';mtmRenderProducts();});
mtmId('mtm-clear-components').addEventListener('click',()=>{mtmProducts=[];mtmRenderProducts();});
mtmId('mtm-add-leg').addEventListener('click',()=>{let [leg,error]=mtmBuildLeg();if(error){mtmId('mtm-message').textContent=error;return;}mtmDraftLegs.push(leg);mtmProducts=[];mtmRenderProducts();mtmRenderDraft();mtmId('mtm-message').textContent='Ponta incluída. Você pode incluir a ponta oposta para formar um swap.';});
mtmId('mtm-new').addEventListener('click',()=>{mtmProducts=[];mtmDraftLegs=[];mtmId('mtm-name').value='';mtmRenderProducts();mtmRenderDraft();mtmId('mtm-message').textContent='Formulário preparado para uma nova operação.';});
mtmId('mtm-close').addEventListener('click',()=>{let date=mtmDate.value,legs=[...mtmDraftLegs];if(!date||date>HOJE){mtmId('mtm-message').textContent='Informe uma data de fechamento até hoje.';return;}
 if(mtmProducts.length){let [leg,error]=mtmBuildLeg();if(error){mtmId('mtm-message').textContent=error;return;}legs.push(leg);}
 if(!legs.length){mtmId('mtm-message').textContent='Inclua pelo menos uma ponta na operação.';return;}
 let op={id:'op-'+Date.now()+'-'+Math.random().toString(36).slice(2,8),name:mtmId('mtm-name').value.trim()||'Operação '+(mtmOps.length+1),date,positions:legs};mtmOps.push(op);mtmProducts=[];mtmDraftLegs=[];mtmRenderProducts();mtmRenderDraft();mtmSave();mtmId('mtm-message').textContent='Operação registrada com '+legs.length+' ponta(s).';mtmRender();mtmId('mtm-focus').value=op.id;mtmDraw();});
mtmId('mtm-focus').addEventListener('change',mtmDraw);mtmId('mtm-view').addEventListener('change',mtmDraw);
// Exportação XLSX nativa/offline: pacote OOXML ZIP sem bibliotecas externas.
function mtmQuoteInfo(prod,day){
 const x=DIAS[prod];if(!x||!x.datas.length)return null;
 let a=0,b=x.datas.length-1,k=-1;
 while(a<=b){let m=(a+b)>>1;if(x.datas[m]<=day){k=m;a=m+1;}else b=m-1;}
 return k<0?null:{price:Number(x.precos[k]),reference:x.datas[k]};
}
function mtmDates(from){let days=[];for(let t=Date.parse(from+'T00:00:00Z'),end=Date.parse(HOJE+'T00:00:00Z');t<=end;t+=86400000)days.push(new Date(t).toISOString().slice(0,10));return days;}
function mtmXlsxCol(n){let s='';while(n){n--;s=String.fromCharCode(65+n%26)+s;n=Math.floor(n/26);}return s;}
function mtmXmlEscape(value){return String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&apos;');}
function mtmSheetXml(rows){let xml='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"/></sheetViews><sheetFormatPr defaultRowHeight="15"/><sheetData>';
 rows.forEach((cells,i)=>{let n=i+1;xml+='<row r="'+n+'">';cells.forEach((v,j)=>{if(v===null||v===undefined||v==='')return;let pos=mtmXlsxCol(j+1)+n;if(typeof v==='object'&&v.formula){xml+='<c r="'+pos+'" s="2"><f>'+mtmXmlEscape(v.formula)+'</f><v>'+Number(v.value||0)+'</v></c>';}
 else if(typeof v==='number'&&Number.isFinite(v)){xml+='<c r="'+pos+'" s="2"><v>'+v+'</v></c>';}
 else {let val=String(v);xml+='<c r="'+pos+'" t="inlineStr"'+(i===0?' s="1"':'')+'><is><t>'+mtmXmlEscape(val)+'</t></is></c>';}});xml+='</row>';});return xml+'</sheetData></worksheet>';}
function mtmCrcTable(){let table=[];for(let i=0;i<256;i++){let c=i;for(let j=0;j<8;j++)c=c&1?0xEDB88320^(c>>>1):c>>>1;table[i]=c>>>0;}return table;}
function mtmZip(entries){const enc=new TextEncoder(),tab=mtmCrcTable(),out=[],central=[];let offset=0;
 const u16=n=>[n&255,(n>>>8)&255],u32=n=>[n&255,(n>>>8)&255,(n>>>16)&255,(n>>>24)&255];
 for(const [filename,content] of entries){const name=enc.encode(filename),body=enc.encode(content);let crc=0xFFFFFFFF;for(const v of body)crc=tab[(crc^v)&255]^(crc>>>8);crc=(crc^0xFFFFFFFF)>>>0;
 const local=new Uint8Array([...u32(0x04034b50),...u16(20),...u16(0x0800),...u16(0),...u16(0),...u16(0),...u32(crc),...u32(body.length),...u32(body.length),...u16(name.length),...u16(0),...name]);out.push(local,body);
 const centralHeader=new Uint8Array([...u32(0x02014b50),...u16(20),...u16(20),...u16(0x0800),...u16(0),...u16(0),...u16(0),...u32(crc),...u32(body.length),...u32(body.length),...u16(name.length),...u16(0),...u16(0),...u16(0),...u16(0),...u32(0),...u32(offset),...name]);central.push(centralHeader);offset+=local.length+body.length;}
 const centralSize=central.reduce((sum,b)=>sum+b.length,0),ending=new Uint8Array([...u32(0x06054b50),...u16(0),...u16(0),...u16(entries.length),...u16(entries.length),...u32(centralSize),...u32(offset),...u16(0)]);
 return new Blob([...out,...central,ending],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
}
function mtmXlsx(sheets){let files=[['[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+sheets.map((_,i)=>'<Override PartName="/xl/worksheets/sheet'+(i+1)+'.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>').join('')+'</Types>'],
 ['_rels/.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'],
 ['xl/workbook.xml','<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets>'+sheets.map(([name],i)=>'<sheet name="'+mtmXmlEscape(name)+'" sheetId="'+(i+1)+'" r:id="rId'+(i+1)+'"/>').join('')+'</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>'],
 ['xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+sheets.map((_,i)=>'<Relationship Id="rId'+(i+1)+'" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet'+(i+1)+'.xml"/>').join('')+'<Relationship Id="rId'+(sheets.length+1)+'" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'],
 ['xl/styles.xml','<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF007E80"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1"/><xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>']];
 sheets.forEach(([name,rows],i)=>files.push(['xl/worksheets/sheet'+(i+1)+'.xml',mtmSheetXml(rows)]));return mtmZip(files);
}
function mtmExportExcel(){
 try{
 if(!mtmOps.length){mtmId('mtm-message').textContent='Cadastre pelo menos uma operação antes de exportar.';return;}
 const focus=mtmId('mtm-focus').value,selected=focus==='all'?mtmOps:mtmOps.filter(o=>o.id===focus),series=selected.flatMap(op=>[...new Set(op.positions.flatMap(l=>l.products))].map(prod=>({op,prod,label:prod+' — '+op.name}))),start=selected.map(o=>o.date).sort()[0],days=mtmDates(start);
 const graphs=[['Data',...series.map(s=>'MTM '+s.label+' (MMR$)'),'Total carteira (MMR$)']];
 const selectedChart=[['Data',...(mtmId('mtm-view').value==='products'?series.map(s=>'MTM '+s.label+' (MMR$)'):['Total carteira (MMR$)'])]];
 const memo=[['Data MTM','Operação','Fechamento','Ponta','Compra/Venda','Produto','Volume MWm','Preço contratado R$/MWh','Horas','Preço BBCE R$/MWh','Data cotação BBCE','Sinal','MTM (MMR$)','Status cotação']];
 const operations=[['Operação','Data fechamento','Ponta','Compra/Venda','Produto','Volume MWm','Preço contratado R$/MWh','Horas','MTM atual (MMR$)']];
 selected.forEach(o=>o.positions.forEach((leg,index)=>leg.products.forEach(prod=>operations.push([o.name,o.date,index+1,leg.side,prod,leg.mw,leg.price,mtmHours(prod),mtmValuePosition(leg,prod,HOJE)]))));
 for(const day of days){
 const active=selected.filter(o=>o.date<=day);let total=0,valid=true;
 const seriesValues=series.map(({op,prod})=>{if(op.date>day)return null;const legs=op.positions.filter(l=>l.products.includes(prod));let sum=0;for(const l of legs){let v=mtmValuePosition(l,prod,day);if(v===null)return null;sum+=v;}return sum;});
 for(const op of active){for(let legIndex=0;legIndex<op.positions.length;legIndex++){const leg=op.positions[legIndex];for(const prod of leg.products){
 const quote=mtmQuoteInfo(prod,day),hours=mtmHours(prod),sign=leg.side==='compra'?1:-1,mtm=quote?(quote.price-leg.price)*leg.mw*hours*sign/1000000:null;
 let rowIndex=memo.length+1;
 memo.push([day,op.name,op.date,legIndex+1,leg.side,prod,leg.mw,leg.price,hours,quote?quote.price:null,quote?quote.reference:'',sign,mtm===null?null:{formula:`(J${rowIndex}-H${rowIndex})*G${rowIndex}*I${rowIndex}*L${rowIndex}/1000000`,value:mtm},quote?(quote.reference===day?'Negociado no dia':'Última cotação anterior'):'Sem cotação anterior']);
 if(mtm===null){valid=false;}else total+=mtm;
 }} }
 let vals=seriesValues,t=active.length&&valid?total:null;
 graphs.push([day,...vals,t]);selectedChart.push([day,...(mtmId('mtm-view').value==='products'?vals:[t])]);
 }
 const sheetList=[['Gráfico selecionado',selectedChart],['Séries por produto',graphs],['Memória de cálculo',memo],['Operações',operations],['Metodologia',[
 ['Fonte','BBCE; preços médios diários ponderados por MWh conforme dados do painel'],['Data de exportação',HOJE],['Escopo',focus==='all'?'Carteira consolidada':selected[0].name],['Unidade','MMR$ (milhões de reais)'],['Compra','(Preço BBCE - Preço contratado) × volume MWm × horas / 1.000.000'],['Venda','(Preço contratado - Preço BBCE) × volume MWm × horas / 1.000.000'],['Swap','Soma algébrica das pontas de compra e venda'],['Séries por produto','Cada coluna representa um produto em uma operação específica; o mesmo produto em operações distintas ocupa colunas diferentes'],['Cotação sem negociação','Última cotação registrada até a data da marcação; nunca antecipar cotação futura'],['Cotação indisponível','Célula em branco; sem negócio anterior válido'],['Observação','Horas correspondem ao período completo do produto; não há ajuste de horas remanescentes']]]];
 const blob=mtmXlsx(sheetList),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='marcacao_mercado_'+HOJE+'.xlsx';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);mtmId('mtm-message').textContent='Excel exportado com séries, operações e memória de cálculo.';
 }catch(e){console.error(e);mtmId('mtm-message').textContent='Erro na exportação Excel: '+e.message;}
}

mtmId('mtm-export-excel').addEventListener('click',mtmExportExcel);
mtmRenderProducts();mtmRenderDraft();mtmRender();
</script></body></html>'''


def main():
    argp = argparse.ArgumentParser()
    argp.add_argument('--somente-local', action='store_true', help='Compatibilidade com a rotina existente')
    argp.add_argument('--data-referencia', help='Data visual do painel para teste (AAAA-MM-DD)')
    args = argp.parse_args()
    DADOS.mkdir(parents=True, exist_ok=True)
    GRAFICOS.mkdir(parents=True, exist_ok=True)
    base = carregar_historicos()
    novos = importar_novos()
    historico = consolidar(base, novos)
    diario = calc_diario(historico)
    # Somente o CSV é fonte de verdade; arquivos auxiliares são derivados.
    saida = historico[COLUNAS].copy()
    saida['DATA/HORA'] = saida['DATA/HORA'].dt.strftime('%d/%m/%Y %H:%M:%S')
    saida.to_csv(CSV, index=False, encoding='utf-8-sig', float_format='%.8g')
    diario.to_csv(DIARIO, index=False, encoding='utf-8-sig', date_format='%Y-%m-%d', float_format='%.5f')
    pd.DataFrame(PRODUTOS, columns=['PRODUTO', 'APELIDO']).to_csv(CATALOGO, index=False, encoding='utf-8-sig')
    hoje = pd.Timestamp(args.data_referencia) if args.data_referencia else pd.Timestamp(datetime.now(ZoneInfo('America/Sao_Paulo')).date())
    HTML.write_text(pagina_mercado(diario, hoje), encoding='utf-8')
    inserir_aba_index()
    print(f'Mercado: {len(historico):,} negócios, {len(PRODUTOS)} produtos, {len(diario):,} pontos diários. Última negociação {historico["DATA/HORA"].max():%d/%m/%Y}.')
    print(f'Mercado: gerado {HTML}')


if __name__ == '__main__':
    main()
