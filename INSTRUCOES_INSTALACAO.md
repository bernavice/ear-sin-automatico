# Painel Hidrológico do SIN — EAR + ENA (GitHub Actions + Pages)

Este pacote reúne as versões consolidadas dos códigos de EAR e ENA, da automação e das bases iniciais.

## Regras dos dados

- **EAR mensal (%):** último dia com informação disponível de cada mês, incluindo o mês corrente.
- **ENA mensal (% MLT):** média aritmética dos percentuais diários do mês, até o último dia com dado disponível (inclusive o mês corrente).
- **ENA SIN diária:** percentual obtido usando ENAs absolutas e MLTs dos subsistemas, não uma média simples de percentuais por subsistema.
- O script consulta o ano corrente e, na transição, também o ano anterior, dentro da janela de revisão, para preservar dezembro e incorporar janeiro quando publicado.
- As bases de dias/meses anteriores ficam persistidas no GitHub; o script consulta anualmente o CSV vigente e reprocessa uma janela recente.

## O que aparece no painel

- Dois botões de navegação: **EAR · Armazenamento** e **ENA · Afluências**.
- **EAR:** gráfico do SIN, área cinza semitransparente com limites mensais mínimo/máximo, contornos suavizados, sem grades, anos 2021 (vermelho) e 2023 (azul claro).
- **ENA:** gráfico maior do SIN e quatro menores em duas colunas (SE/CO e Sul; Norte e Nordeste); linha tracejada horizontal em **100% da MLT**; legenda sem título embutido, para não sobrepor as séries.
- Century Gothic (se disponível no dispositivo; senão usa fontes alternativas).
- Fonte e última data do **dado disponível** no rodapé, separado da legenda.
- Por padrão ficam visíveis o ano corrente, o anterior, 2021 e 2023. Outros anos disponíveis na legenda.
- A linha **Vmimop** ainda não está desenhada: não foi definido valor oficial (`VMIMOP_PCT = None` no script EAR).

## Arquivos

```text
.github/workflows/atualizar-ear.yml   Agenda e publica GitHub Pages
atualizar_ear.py                     Atualiza EAR e gera graficos/ear_sin.html
atualizar_ena.py                     Atualiza ENA, gera graficos/ena.html e graficos/index.html
requirements-ear.txt                 Dependências Python
 dados/ear_diaria.csv                Histórico EAR inicial (backup)
 dados/ear_mensal.csv                Histórico EAR mensal inicial (backup)
 dados/ena_diaria.csv                Histórico ENA inicial
 dados/ena_mensal.csv                Histórico ENA mensal inicial (gerado)
 graficos/index.html                 Página inicial com botões (gerada)
 graficos/ear_sin.html               Página EAR (gerada)
 graficos/ena.html                   Página ENA (gerada)
```

## Instalação no repositório `bernavice/ear-sin-automatico`

1. **Faça backup** do repositório atual no botão `Code > Download ZIP`.
2. Extraia o ZIP deste pacote no Windows.
3. No GitHub (`Code > Add file > Upload files`), envie para a **raiz** do repositório os arquivos `atualizar_ear.py`, `atualizar_ena.py`, `requirements-ear.txt` e `INSTRUCOES_INSTALACAO.md`. Arquivos que já existem serão substituídos quando o commit for confirmado.
4. No GitHub, entre na pasta `dados` e envie `ena_diaria.csv`, caso ainda não exista. `ena_mensal.csv` será criado automaticamente pelo workflow. **Não substitua** `ear_diaria.csv` ou `ear_mensal.csv` se já estão atualizados no GitHub: os CSVs deste pacote são uma cópia inicial e podem ser mais antigos que os dados publicados desde então.
5. Acesse `.github/workflows/atualizar-ear.yml`, clique no lápis e **substitua o conteúdo inteiro** pelo arquivo de mesmo nome deste ZIP. Faça o commit na branch `main`.
6. Confirme em `Settings > Pages > Build and deployment > Source` que está marcado **GitHub Actions**.
7. Execute manualmente `Actions > Atualizar painel EAR e ENA do SIN > Run workflow` na branch `main`.
8. Aguarde ambos os trabalhos verdes, `atualizar-dados` e `publicar`. Acesse `https://bernavice.github.io/ear-sin-automatico/` e dê `Ctrl + F5` se o navegador estiver exibindo uma versão em cache.

### Atenção ao workflow

- Deve existir **somente um workflow responsável por publicar o painel**, para não ficar alternando entre versões.
- A versão correta publica três arquivos no GitHub Pages: `index.html`, `ear_sin.html` e `ena.html`.
- Nunca envie somente o ZIP para o GitHub esperando que ele extraia o conteúdo automaticamente.
- Não edite nem exclua o diretório `dados` no GitHub Pages: a página usa os HTMLs gerados; os CSVs ficam no repositório para persistir o histórico.

## Diagnóstico da execução

`Actions > Atualizar painel EAR e ENA do SIN > última execução > atualizar-dados`

Confira as etapas:
- Atualizar dados e grafico EAR
- Atualizar dados e graficos ENA e painel
- Conferir ENA mensal e botoes do painel
- Registrar dados e graficos no GitHub

Se aparecer erro, copie o trecho do log da etapa vermelha. Um sucesso no workflow comprova execução/publicação, mas a data do dado só avançará se o ONS disponibilizar informação nova.

## Teste local, opcional

```bash
pip install -r requirements-ear.txt
python atualizar_ear.py --somente-local
python atualizar_ena.py --somente-local
```

O teste local não consulta o ONS; apenas regenera o gráfico com a base histórica incluída no pacote.
