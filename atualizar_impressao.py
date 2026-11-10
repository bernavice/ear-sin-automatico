#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gera graficos/impressao.html e atualiza o botão de imprimir em graficos/index.html.

Uso: python atualizar_impressao.py
Executar DEPOIS das rotinas que geram EAR, ENA, PLD, Mercado, Operação, Newave e Decomp.
Este script não muda dados nem calcula indicadores.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
GRAFICOS = ROOT / "graficos"
INDEX = GRAFICOS / "index.html"
REPORT = GRAFICOS / "impressao.html"

# Modelo estático embutido. A página busca os módulos HTML na mesma pasta em tempo de execução.
REPORT_HTML = '<!DOCTYPE html>\n<html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Relatório completo — Acompanhamento de mercado</title>\n<style>\n:root{font-family:Arial,\'Segoe UI\',sans-serif;color:#233443;background:#eaf0f3}\n*{box-sizing:border-box}\nbody{margin:0} .toolbar{position:sticky;top:0;z-index:10;background:#fff;border-bottom:3px solid #007e80;display:flex;align-items:center;justify-content:space-between;padding:13px 22px;gap:16px;box-shadow:0 2px 12px #1a344220}\n.toolbar h1{font-size:17px;color:#007e80;margin:0 0 3px}.toolbar p{font-size:12px;margin:0;color:#607180}\n.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}button{padding:11px 16px;background:#007e80;color:#fff;border:0;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer}button:disabled{opacity:.5;cursor:wait}button.secondary{background:#e9f2f3;color:#006970}\n#status{max-width:1090px;margin:15px auto;font-size:13px;color:#425968;background:white;border-radius:9px;padding:13px 16px}\n#report{max-width:1090px;margin:20px auto;padding:20px 24px;background:white;box-shadow:0 3px 20px #0a2d371a}\n.cover{padding:28px 20px 24px;border-bottom:4px solid #007e80;margin-bottom:26px}\n.cover h1{font-size:27px;color:#007e80;margin:0 0 10px}.cover .date{color:#677886;font-size:13px}\n.module-heading{font-size:21px;color:#006d74;line-height:1.2;margin:0 0 10px; padding:9px 10px 10px;border-bottom:2px solid #007e80;break-after:avoid}\n.module{margin:0 0 20px}.module+.module{break-before:page;page-break-before:always}\n.print-piece{display:block;width:100%;margin:0 0 16px;break-inside:auto;page-break-inside:auto}\n.print-piece.chart-piece{break-inside:avoid;page-break-inside:avoid}\n.print-piece.chart-piece:has(img.plot-export){break-inside:avoid;page-break-inside:avoid}\n.print-piece .piece-shell{width:100%;min-width:0}\n.print-piece img.plot-export{width:100%;height:auto;max-height:420px;object-fit:contain;display:block;margin:5px auto}\n.print-piece .js-plotly-plot{display:none!important}\n.print-piece .modebar,.print-piece button,.print-piece input,.print-piece select,.print-piece textarea,.print-piece .toolbar,.print-piece .filtros,.print-piece .filters,.print-piece .controls,.print-piece .toolbar-botoes{display:none!important}\n.print-piece [style*="overflow: auto"],.print-piece [style*="overflow:auto"]{overflow:visible!important}\niframe.collector{position:fixed;width:1260px;height:900px;left:-5000px;top:0;opacity:0;pointer-events:none;border:0}\n.error{background:#fff3eb;border-left:4px solid #c66b1e;padding:12px;font-size:13px}\n.small-note{font-size:10px;color:#6c7780}\n@page{size:A4 landscape;margin:11mm 12mm 14mm;@bottom-right{content:"Página " counter(page);font:9px Arial;color:#69818c}@bottom-left{content:"Acompanhamento de mercado";font:9px Arial;color:#69818c}}\n@media print {\n :root{background:#fff}body{margin:0;color:#172b38;-webkit-print-color-adjust:exact;print-color-adjust:exact}\n .toolbar,#status,iframe.collector{display:none!important}\n #report{box-shadow:none;margin:0;max-width:none;padding:0;width:auto}\n .cover{padding:12px 0 14px;margin-bottom:18px}\n .cover h1{font-size:24px}\n .module-heading{font-size:18px;margin-bottom:9px}\n .module{width:100%;margin:0}.print-piece{margin-bottom:10px}\n .print-piece img.plot-export{max-height:385px}\n .print-piece [class*="grid"],.print-piece [class*="cards"]{max-width:100%}\n}\n</style></head><body>\n<div class="toolbar"><div><h1>Relatório completo · Acompanhamento de mercado</h1><p>Inclui Operação, ENA, EAR, PLD, Mercado, Newave e Decomp</p></div><div class="actions"><button class="secondary" id="reload" onclick="location.reload()">Atualizar relatório</button><button id="print" disabled>Imprimir / Salvar PDF</button></div></div>\n<div id="status" role="status">Iniciando preparação…</div>\n<div id="report"><header class="cover"><h1>Acompanhamento de mercado</h1><div class="date" id="report-date"></div><div class="small-note">Dados e filtros iniciais dos módulos na versão publicada. A seleção de gráficos no painel original não é transferida automaticamente ao relatório.</div></header><div id="sections"></div></div>\n<script>\n\'use strict\';\nconst MODULES=[\n [\'operacao\',\'Operação do SIN\',\'operacao.html\'],\n [\'ena\',\'ENA · Afluências\',\'ena.html\'],\n [\'ear\',\'EAR · Armazenamento\',\'ear_sin.html\'],\n [\'pld\',\'PLD · Preços\',\'pld.html\'],\n [\'mercado\',\'Mercado · Negócios e MTM\',\'mercado.html\'],\n [\'newave\',\'Projeções Newave\',\'newave.html\'],\n [\'decomp\',\'Projeções Decomp\',\'decomp.html\']\n];\nconst $=id=>document.getElementById(id);\nconst sleep=ms=>new Promise(r=>setTimeout(r,ms));\nconst waitForLoad=(frame,ms=45000)=>new Promise((resolve,reject)=>{\n let finished=false;\n const timer=setTimeout(()=>{if(!finished){finished=true;reject(new Error(\'Tempo limite ao carregar módulo\'))}},ms);\n frame.addEventListener(\'load\',()=>{if(!finished){finished=true;clearTimeout(timer);resolve()}},{once:true});\n frame.addEventListener(\'error\',()=>{if(!finished){finished=true;clearTimeout(timer);reject(new Error(\'Falha ao carregar módulo\'))}},{once:true});\n});\nfunction getPieces(doc,id){\n const area=doc.querySelector(\'main\')||doc.body;\n if(id===\'decomp\'){\n  const out=[];\n  const summary=doc.getElementById(\'panel-summary\');if(summary)out.push(summary);\n  const group=doc.getElementById(\'sections\');\n  if(group)out.push(...[...group.children].filter(el=>el.tagName!==\'SCRIPT\'));\n  return out.length?out:[...area.children].filter(el=>el.tagName!==\'SCRIPT\');\n }\n if(id===\'operacao\'){\n  const elems=[...area.children].filter(el=>el.tagName!==\'SCRIPT\');\n  return elems.flatMap(el=>el.id===\'graficos-sub\'?[...el.children]:[el]);\n }\n return [...area.children].filter(el=>![\'SCRIPT\',\'STYLE\'].includes(el.tagName));\n}\nfunction scopeCss(doc,root){\n const css=[...doc.querySelectorAll(\'head style\')].map(el=>el.textContent||\'\').join(\'\\n\');\n const style=document.createElement(\'style\');\n // Each print piece is isolated in a shadow root to avoid CSS from other modules.\n style.textContent=css+`\\n:host{display:block;width:100%;font-family:Arial,sans-serif;color:#193245;background:white}\n *{box-sizing:border-box}\n .js-plotly-plot{display:none!important}\n img.plot-export{width:100%;max-height:405px;object-fit:contain;display:block;margin:3px auto}\n .modebar,button,input,select,textarea,[role="button"],.toolbar,.filters,.filtros{display:none!important}\n .panel,.painel,.section,.grafico-box{margin:0!important;max-width:100%!important;width:100%!important;overflow:visible!important}\n .cards,.graficos-sub,.grid{max-width:100%!important}\n @media print{:host{display:block}img.plot-export{max-height:365px!important}.painel,.panel,.section{break-inside:auto!important}}\n `;\n root.appendChild(style);\n}\nasync function exportCharts(doc,original,cloned,id){\n const sourcePlots=[...original.querySelectorAll(\'.js-plotly-plot\')];\n const clonedPlots=[...cloned.querySelectorAll(\'.js-plotly-plot\')];\n let captured=0;\n for(let i=0;i<sourcePlots.length;i++){\n  const src=sourcePlots[i],dst=clonedPlots[i];\n  if(!dst)continue;\n  if(!src._fullLayout){dst.remove();continue;}\n  try{\n   const width=Math.min(1150,Math.max(700,src.clientWidth||920));\n   const height=Math.min(460,Math.max(270,src.clientHeight||390));\n   // SVG retains sharp labels/lines in PDF; raster PNG fallback for unsupported traces.\n   let imgsrc;\n   try{imgsrc=await doc.defaultView.Plotly.toImage(src,{format:\'svg\',width,height,scale:1});}\n   catch(_){imgsrc=await doc.defaultView.Plotly.toImage(src,{format:\'png\',width,height,scale:1.6});}\n   const im=document.createElement(\'img\');im.src=imgsrc;im.className=\'plot-export\';im.alt=\'Gráfico do módulo \'+id+\' \'+(i+1);\n   dst.replaceWith(im);captured++;\n  }catch(err){console.warn(\'Gráfico não exportado\',id,i,err);dst.remove();}\n }\n return captured;\n}\nasync function prepare(){\n $(\'report-date\').textContent=\'Gerado em \'+new Date().toLocaleString(\'pt-BR\',{dateStyle:\'long\',timeStyle:\'short\'});\n let errors=[], charts=0;\n for(let index=0;index<MODULES.length;index++){\n  const [id,name,filename]=MODULES[index];\n  $(\'status\').textContent=\'Preparando \'+(index+1)+\' de \'+MODULES.length+\': \'+name+\'…\';\n  let frame;\n  const module=document.createElement(\'section\');module.className=\'module\';module.id=\'mod-\'+id;\n  const heading=document.createElement(\'h2\');heading.className=\'module-heading\';heading.textContent=name;module.appendChild(heading);\n  $(\'sections\').appendChild(module);\n  try{\n   frame=document.createElement(\'iframe\');frame.className=\'collector\';frame.title=\'Carregamento de \'+name;\n   document.body.appendChild(frame);\n   const loaded=waitForLoad(frame);\n   frame.src=filename;\n   await loaded;\n   const doc=frame.contentDocument;if(!doc)throw new Error(\'Não foi possível acessar o conteúdo do módulo.\');\n   // Let scripts mount and Plotly finish drawing before export.\n   const expected={operacao:3,ena:5,ear:5,pld:5,mercado:1,newave:3,decomp:1}[id]||1;\n   for(let n=0;n<34;n++){\n    const plots=[...doc.querySelectorAll(\'.js-plotly-plot\')].filter(el=>el._fullLayout);\n    if(plots.length>=expected)break;\n    await sleep(350);\n   }\n   await sleep(400);\n   const parts=getPieces(doc,id);\n   if(!parts.length)throw new Error(\'Nenhuma seção identificada.\');\n   for(const piece of parts){\n    const origPlots=piece.querySelectorAll(\'.js-plotly-plot\').length;\n    const clone=piece.cloneNode(true);\n    charts+=await exportCharts(doc,piece,clone,id);\n    const holder=document.createElement(\'div\');holder.className=\'print-piece\'+(origPlots===1?\' chart-piece\':\'\');\n    const sh=holder.attachShadow({mode:\'open\'});scopeCss(doc,sh);\n    const shell=document.createElement(\'div\');shell.className=\'piece-shell\';shell.appendChild(document.importNode(clone,true));sh.appendChild(shell);\n    module.appendChild(holder);\n   }\n  }catch(err){\n   errors.push(name+\': \'+err.message);\n   let item=document.createElement(\'p\');item.className=\'error\';item.textContent=\'Falha ao preparar \'+name+\': \'+err.message;module.appendChild(item);\n  }finally{if(frame){frame.remove()}}\n }\n // Wait for imported images and fonts before enabling printing.\n const imgs=[...document.querySelectorAll(\'.print-piece\')].flatMap(x=>[...x.shadowRoot.querySelectorAll(\'img.plot-export\')]);\n await Promise.all(imgs.map(img=>img.complete?Promise.resolve():new Promise(resolve=>{img.onload=resolve;img.onerror=resolve;setTimeout(resolve,15000)})));\n await (document.fonts?.ready||Promise.resolve());\n $(\'status\').textContent=`Relatório preparado: ${MODULES.length-errors.length}/${MODULES.length} módulos; ${charts} gráficos exportados.`+(errors.length?\' Falhas: \'+errors.join(\' | \'):\' Confira as seções antes de salvar o PDF.\');\n $(\'print\').disabled=false;\n if(errors.length)console.warn(errors);\n}\n$(\'print\').addEventListener(\'click\',()=>window.print());\nwindow.addEventListener(\'load\',()=>prepare().catch(err=>{$(\'status\').textContent=\'Erro ao preparar relatório: \'+err.message;console.error(err)}));\n</script></body></html>'

def atualizar_index(html: str) -> str:
    """Patch idempotente, sem tocar nas abas, iframes e demais scripts."""
    fn = """function imprimirDashboardCompleto(){
  const url = new URL('impressao.html', window.location.href);
  const janela = window.open(url.href, '_blank', 'noopener');
  if (!janela) alert('Libere pop-ups para este site ou abra impressao.html diretamente.');
}"""
    # Replace the legacy huge script dedicated to printing, if present.
    old = re.compile(r'<script>\s*async\s+function\s+imprimirDashboardCompleto\s*\(\s*\)\s*\{.*?</script>', re.S)
    html, n = old.subn(lambda _: '<script>\n'+fn+'\n</script>', html, count=1)
    if not n:
        # Also recognize the previously installed short function if script is being rerun.
        short = re.compile(r'<script>\s*function\s+imprimirDashboardCompleto\s*\(\s*\)\s*\{.*?</script>', re.S)
        html, n = short.subn(lambda _: '<script>\n'+fn+'\n</script>', html, count=1)
    if not n:
        html = html.replace('</body>', '<script>\n'+fn+'\n</script>\n</body>', 1)
    # Existing button is kept, but its label/title always point to the report.
    button = re.compile(r'(<button\b[^>]*id=["\']imprimir-dashboard["\'][^>]*)(>)(.*?)(</button>)', re.I|re.S)
    def patch_button(m):
        opening = re.sub(r'\s+title=["\'][^"\']*["\']', '', m.group(1))
        if 'onclick=' not in opening:
            opening += ' onclick="imprimirDashboardCompleto()"'
        return opening + ' title="Abrir relatório completo para salvar em PDF"' + m.group(2) + 'Imprimir dashboard em PDF' + m.group(4)
    html, count = button.subn(patch_button, html, count=1)
    if count == 0:
        # Insert after the navigation bar; do not disturb header or tabs.
        controls = ('<div class="barra-impresso"><button id="imprimir-dashboard" '
                    'type="button" onclick="imprimirDashboardCompleto()" '
                    'title="Abrir relatório completo para salvar em PDF">'
                    'Imprimir dashboard em PDF</button></div>')
        if '</nav>' not in html:
            raise RuntimeError('Não encontrei </nav> no index.html; arquivo preservado.')
        html = html.replace('</nav>', '</nav>'+controls, 1)
    if '.barra-impresso{' not in html:
        style = '.barra-impresso{display:flex;justify-content:flex-end;margin:8px 0 2px}'
        html = html.replace('</style>', style+'\n</style>', 1)
    return html


def main():
    GRAFICOS.mkdir(parents=True, exist_ok=True)
    if not INDEX.is_file():
        raise FileNotFoundError('graficos/index.html precisa existir antes desta etapa.')
    before = INDEX.read_text(encoding='utf-8')
    after = atualizar_index(before)
    # Validate all prerequisites before saving either file.
    if 'imprimirDashboardCompleto' not in after or 'impressao.html' not in after:
        raise RuntimeError('Falha ao instalar o novo botão de impressão')
    REPORT.write_text(REPORT_HTML, encoding='utf-8')
    if after != before:
        INDEX.write_text(after, encoding='utf-8')
    print('Relatório gerado:', REPORT)
    print('Botão de impressão atualizado:', INDEX)


if __name__ == '__main__':
    main()
