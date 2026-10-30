# EAR do SIN — atualização automática pelo GitHub Actions

Projeto inicial de Energia Armazenada (EAR) em % da EAR máxima.

**Fonte de atualização:** [EAR Diário por Subsistema — ONS](https://dados.ons.org.br/dataset/ear-diario-por-subsistema).

## Arquivos

- `dados/ear_diaria.csv`: histórico diário a partir de 01/01/2020, extraído da planilha fornecida, com percentuais dos 4 subsistemas e SIN; coluna `tx_var_ear_sin` é variação diária em **pontos percentuais**.
- `dados/ear_mensal.csv`: observação do último dia **disponível** de cada mês, incluindo mês em andamento.
- `graficos/ear_sin.html`: Plotly, uma linha por ano, suavizada; gráfico mostra até 10 anos (mas só temos 2020 em diante na base inicial).
- `atualizar_ear.py`: rotina de consulta, consolidação e geração do gráfico.
- `.github/workflows/atualizar-ear.yml`: agenda diária e execução manual.

## Como usar

1. Suba o **conteúdo desta pasta** na raiz de um repositório GitHub (inclua `.github`, que é oculta em alguns ambientes).
2. Verifique em **Settings > Actions > General > Workflow permissions** se a política do repositório permite gravar dados. O YAML também define `contents: write`.
3. Acesse **Actions > Atualizar EAR do SIN > Run workflow > Run workflow**.
4. Acompanhe os logs. O script só faz commit quando os arquivos mudam.
5. Depois a rotina será executada diariamente às **20h15 (America/Sao_Paulo)**. O início agendado pode sofrer atraso operacional no GitHub.

## Atualização incremental

O ONS disponibiliza um **arquivo anual**, que o script baixa em cada execução, não o histórico inteiro de 2020 a 2026. A rotina substitui/insere observações da janela mais recente (padrão 45 dias), preserva as demais e recria os fechamentos mensais e gráfico.

SIN (%) = 100 × soma da EAR verificada (MWmês) / soma da EAR máxima (MWmês). Não utiliza média aritmética dos quatro percentuais.

A etapa ONS falha com mensagem explícita se houver erro de download, alteração de layout ou ausência dos quatro subsistemas; nesse caso o histórico anterior permanece salvo e não ocorre commit.

O arquivo ONS de 2026 foi identificado no endereço:
`https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/ear_subsistema_di/EAR_DIARIO_SUBSISTEMA_2026.csv`

**Observação:** neste ambiente foi possível verificar o link e o dicionário oficial, mas não realizar o download de teste do CSV do ONS. O primeiro `Run workflow` no GitHub verificará a leitura real da fonte.

## Execução local, opcional

`pip install -r requirements-ear.txt`

`python atualizar_ear.py --somente-local` — refaz somente o mensal e o HTML, sem internet.

`python atualizar_ear.py` — baixa o arquivo anual atual e reconcilia os últimos 45 dias.

## Nota para dashboards

Para o Render, a aplicação pode usar `dados/ear_mensal.csv` ou integrar `graficos/ear_sin.html` ao layout existente. O gráfico HTML carrega Plotly via CDN e requer internet para renderização.

Não publica nem altera automaticamente seu repositório existente: o envio inicial e o acionamento no GitHub dependem de você.
