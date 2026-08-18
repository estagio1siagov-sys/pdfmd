"""
PORTOES DETERMINISTICOS - SINGRA/SIAGOV, 18/08/2026.

Por que existir: hoje o unico juiz da conversao e' o proprio Gemini (validator_prompt).
Juiz e reu do mesmo tribunal. Estes portoes rodam SEM API, sobre o markdown final, e
pegam a classe de defeito que a auto-validacao nao pega - a que ja causou perda medida
no acervo do CRM-PB.

Nenhum deles depende de rede, chave ou cota.
"""
import re

ESPECIE = (r'(RESOLU[ÇC][ÃA]O|PORTARIA|DELIBERA[ÇC][ÃA]O|INSTRU[ÇC][ÃA]O\s+NORMATIVA'
           r'|DECIS[ÃA]O|ATO|DECRETO|LEI)')
# Duas grafias reais do CRM-PB, medidas no lote de 18/08:
#   "RESOLUCAO CRM-PB no 165/2014"                        -> numero/ano
#   "RESOLUCAO CRM-PB N.o 168, DE 1o DE DEZEMBRO DE 2014" -> numero, data por extenso
_NUM = r'[^\n]{0,40}?n?[º°o.]*\s*(\d{1,5})\s*(?:[/.-]\s*(\d{4})|,\s*DE\s+[^\n]{4,40}?\s+DE\s+(\d{4}))'
ATO_INLINE = re.compile(ESPECIE + _NUM, re.I)
ATO_ABERTURA = re.compile(r'^\s*#*\s*\**\s*' + ESPECIE + _NUM, re.I)
# Anexo/apendice CITA a norma-mae; nao abre ato. Sem isto, o 163_2014 promovia
# "ANEXO II A RESOLUCAO CRM-PB no 163/2014" a titulo do documento.
NAO_E_ATO = re.compile(r'^\s*#*\s*\**\s*(ANEXO|AP[ÊE]NDICE|TABELA|FORMUL[ÁA]RIO|MODELO|QUADRO)\b', re.I)
GATILHO = re.compile(r'^\s*\**\s*(RESOLVE|RESOLVEM|CONSIDERANDO|CONSIDERENDO|O\s+PRESIDENTE'
                     r'|o\s+Conselho\s+Regional|O\s+CONSELHO\s+REGIONAL)', re.I | re.M)

MARCA_BLOCO_AUSENTE = "<!-- SINGRA:BLOCO-AUSENTE"


def _fora_de_codigo(markdown):
    """Itera (numero_da_linha, linha) ignorando o que esta dentro de ``` ```."""
    dentro = False
    for i, l in enumerate(markdown.split('\n'), 1):
        if l.startswith('```'):
            dentro = not dentro
            continue
        if not dentro:
            yield i, l


def rebaixar_h1_extras(markdown):
    """G5/G6 - garante UM unico '# ' no documento, e que ele seja o TITULO DO ATO.

    Necessario porque o pipeline converte bloco a bloco e cada bloco volta com o seu
    proprio '# '; o merger apenas concatena. Medido no lote de 18/08: '#' extras em
    163_2014, 172_2015 e 177_2016 (anexos e cabecalho do orgao promovidos a documento).

    Em markdown '#' significa "comeca um documento". Cada '#' extra faz o RAG entender
    que ali nasceu outro documento, e o anexo passa a aparecer na busca solto, sem a
    norma que o criou.

    Regra: o primeiro '# ' que for titulo de ato permanece; todos os demais viram '##'.
    Se NENHUM '# ' for titulo de ato mas existir uma linha que seja (caso 167_2014, em
    que o '# ' ficou com o cabecalho 'O CRM-PB'), promove essa linha e rebaixa a outra.
    Devolve (markdown_corrigido, lista_de_mudancas).
    """
    linhas = markdown.split('\n')
    dentro = False
    h1s, candidato_ato = [], None
    for i, l in enumerate(linhas):
        if l.startswith('```'):
            dentro = not dentro
            continue
        if dentro:
            continue
        if l.startswith('# '):
            h1s.append(i)
        if (candidato_ato is None and ATO_ABERTURA.match(l)
                and not NAO_E_ATO.match(l) and l.lstrip('#*').strip()):
            candidato_ato = i

    mudancas = []
    if not h1s:
        if candidato_ato is not None:
            alvo = linhas[candidato_ato].lstrip('#').strip().strip('*').strip()
            linhas[candidato_ato] = '# ' + alvo
            mudancas.append(f"L{candidato_ato+1}: promovido a '# ' (titulo do ato, antes sem marcacao)")
        return '\n'.join(linhas), mudancas

    manter = None
    for i in h1s:
        if ATO_INLINE.search(linhas[i]) and not NAO_E_ATO.match(linhas[i]):
            manter = i
            break

    if manter is None and candidato_ato is not None:
        alvo = linhas[candidato_ato].lstrip('#').strip().strip('*').strip()
        linhas[candidato_ato] = '# ' + alvo
        mudancas.append(f"L{candidato_ato+1}: promovido a '# ' ('{alvo[:48]}' e' o titulo do ato)")
        manter = candidato_ato

    if manter is None:
        # Nenhum '# ' foi reconhecido como titulo de ato e nao ha candidato.
        # NAO MEXER. Um portao que rebaixa tudo deixa o documento sem '# ' nenhum -
        # estrago pior que o defeito. Medido: 168_2014, 170_2014 e 176_2016, cujo
        # titulo usa "N.o 168, DE 1o DE DEZEMBRO DE 2014" e escapava ao padrao.
        # Melhor errar por inercia que por estrago: avisa e devolve intacto.
        return markdown, [f"{len(h1s)} titulo(s) de nivel 1 e nenhum reconhecido como "
                          f"titulo de ato - hierarquia NAO alterada; conferir a mao: "
                          + "; ".join(f"L{i+1} '{linhas[i].lstrip('# ').strip()[:40]}'" for i in h1s)]

    for i in h1s:
        if i == manter:
            continue
        linhas[i] = '#' + linhas[i]
        mudancas.append(f"L{i+1}: '# ' -> '## ' ('{linhas[i].lstrip('#').strip()[:48]}')")

    assert sum(1 for x in linhas if x.startswith('# ')) >= 1, \
        "portao nunca pode zerar os '# ' do documento"
    return '\n'.join(linhas), mudancas


def atos_no_documento(markdown):
    """G4 - quantos atos AUTONOMOS o documento contem.

    Um PDF pode trazer mais de um ato: medido em 165_2014.pdf (4 paginas), que contem
    a RESOLUCAO CRM-PB no 165/2014 (pags. 1-2) e a PORTARIA CRM-PB no 16/2014 (pags. 3-4).
    A entrega trouxe so o primeiro, e a Portaria inteira ficou fora do acervo.

    Tres exigencias cumulativas, para nao confundir CITACAO com abertura de ato:
    (a) a linha comeca pela especie - mata "Revoga a Resolucao X" e "REVOGADA pela
        Resolucao Y", que sao citacoes;
    (b) tirado o titulo, sobra quase nada na linha;
    (c) nas 30 linhas seguintes vem preambulo/CONSIDERANDO/RESOLVE.
    Sem (a), o 165_2014 acusava 5 atos onde ha 2, e o 167_2014 acusava 2 onde ha 1.
    """
    linhas = markdown.split('\n')
    achados = []
    for i, l in enumerate(linhas):
        m = ATO_ABERTURA.match(l)
        if not m:
            continue
        if NAO_E_ATO.match(l):
            continue
        if len(ATO_ABERTURA.sub('', l).strip(' \t.,;:-–()*#')) > 14:
            continue
        if not GATILHO.search('\n'.join(linhas[i + 1:i + 31])):
            continue
        achados.append({
            "linha": i + 1,
            "especie": m.group(1).upper(),
            "numero": f"{int(m.group(2))}/{m.group(3)}",
            "texto": l.strip(' #*'),
        })
    return achados


def blocos_ausentes(markdown):
    """Conta os marcadores de bloco perdido deixados pelo runner."""
    return [l.strip() for _, l in _fora_de_codigo(markdown) if MARCA_BLOCO_AUSENTE in l]


def conferir(markdown):
    """Roda tudo e devolve (markdown_corrigido, avisos). Avisos nao vazios => needs_review."""
    corrigido, mudancas = rebaixar_h1_extras(markdown)
    avisos = []
    if mudancas:
        avisos.append("Hierarquia de titulos ajustada automaticamente:\n  - " +
                      "\n  - ".join(mudancas))
    atos = atos_no_documento(corrigido)
    if len(atos) > 1:
        det = "; ".join(f"L{a['linha']} {a['especie']} {a['numero']}" for a in atos)
        avisos.append(
            f"ATENCAO: este PDF contem {len(atos)} atos autonomos [{det}]. "
            "O acervo trata cada ato como um documento proprio - o arquivo precisa ser "
            "desmembrado em um .md por ato antes do deposito."
        )
    aus = blocos_ausentes(corrigido)
    if aus:
        avisos.append(f"ATENCAO: {len(aus)} bloco(s) de pagina nao entraram no arquivo:\n  - " +
                      "\n  - ".join(aus))
    return corrigido, avisos
