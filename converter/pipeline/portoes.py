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

# --- Dispositivo revogado / redacao substituida (SINGRA, 21/08/2026) ---
# Texto compilado traz o historico: a redacao antiga e a nova, e os artigos revogados
# preservados no lugar, tachados na pagina. O conversor vinha EXCLUINDO o trecho tachado
# para nao repetir texto - e criava omissao silenciosa. Medido na RN-TC 01/2017 do TCE-PB:
# os arts. 9o e 10, revogados pela RN-TC 06/2020 e PRESERVADOS no PDF com a marca, sumiram
# do .md; o documento saltava do art. 8o para o art. 11 e passou por DUAS conversoes assim.
MARCA_REVOGADO = "**[DISPOSITIVO REVOGADO"
MARCA_REDACAO_ANTERIOR = "**[REDACAO ANTERIOR"
# aceita com e sem acento - o marcador e escrito por LLM
_MARCADORES = (MARCA_REVOGADO, MARCA_REDACAO_ANTERIOR, "**[REDAÇÃO ANTERIOR")

# O proprio documento declara, colado ao dispositivo. Sobrevive ao pdftotext: nao depende
# de enxergar o tachado.
DECLARA_REVOGACAO = re.compile(r'\(\s*(?:Artigo|Paragrafo|Par[áa]grafo|Inciso|Al[íi]nea|Item)?\s*revogad[oa]s?\s+pel[ao]', re.I)
DECLARA_NOVA_REDACAO = re.compile(r'\(\s*Reda[çc][ãa]o\s+dada\s+pel[ao]', re.I)

# Artigo no inicio da linha, tolerando ~~tachado~~ e **negrito**.
# O terminador e' LOOKAHEAD e inclui '**': "**Art. 10** - texto" e a forma mais comum de
# artigo em markdown, e um terminador que so aceitasse [ºo°.-] a perderia em silencio -
# apanhado pelo test_salto_legitimo_do_orgao ao escrever a propria peca.
ART_LINHA = re.compile(r'^\s*~{0,2}\s*\**\s*Art(?:igo)?\.?\s*(\d{1,3})\s*(?=[ºo°.,\-–)]|\*\*|\s|$)', re.I)
# Lixo de marcacao entre o numero e o texto ("**", "º.", "~~") - limpo antes de testar citacao.
_SOBRA_MARCACAO = re.compile(r'\A\s*(?:[ºo°.,\-–)]|\*\*|~~)+')
# v1.6.2 do conferente: candidato seguido de PREPOSICAO + especie normativa e CITACAO de
# outra norma, nao artigo proprio. Sem isto, "Art. 10 da Resolucao ... 03/2014", transcrito
# DENTRO de um artigo alterador, entraria na espinha como se fosse dispositivo do documento.
CITA_OUTRA_NORMA = re.compile(
    r'\A\s*(?:d[aoe]s?|no|na|nos|nas|pelo|pela)\s+'
    r'(?:Constitui[çc][ãa]o|Carta\s+Magna|CF\b|Lei\s+Org[âa]nica|Regimento|RITCE|LOTCE|'
    r'(?:Lei\s+Complementar|Leis?|Decretos?|Resolu[çc][ãa]o|Portaria|Emenda|EC|LC|IN)'
    r'\s*(?:Normativa\s*)?(?:[A-Z-]{2,8}\s*)?(?:n[.ºo°]|\d))', re.I)



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


_ASPAS = re.compile(r'["\'“”‘’]([^"\'“”‘’]{2,80})["\'“”‘’]')

# So estes tipos podem ser descartados pela presenca literal do termo. Para eles, o termo
# citado ESTAR no texto final prova que o defeito sumiu: se a palavra omitida aparece, nao
# ha mais omissao; se a grafia original foi restituida, nao ha mais texto corrigido.
# FORA DA LISTA, de proposito: 'tabela_incorreta' e 'estrutura_incorreta'. Neles o defeito
# e' de FORMA, e o termo continua presente enquanto a forma continua errada - uma tabela
# achatada em texto corrido contem o proprio cabecalho que a descricao cita, e um "# ANEXO I"
# indevido contem "ANEXO I". Medido: os dois casos eram descartados com o defeito intacto,
# e como este filtro roda ANTES do meta-validador, o item descartado nunca chegava a segunda
# instancia para ser resgatado.
TIPOS_CHECAVEIS_POR_PRESENCA = frozenset({'omissao', 'alucinacao', 'valor_incorreto'})


def filtra_trechos_resolvidos(trechos, texto_final):
    """Segunda checagem, sem API, sobre a reprovacao do validador.

    O validador roda numa versao do bloco; o texto que de fato vai pro arquivo e' a
    versao seguinte, ja reconvertida. Se o validador aponta "a palavra 'X' do PDF virou
    'Y' no Markdown" e o termo 'X' (o original, entre aspas, sempre o primeiro citado -
    ver validator_prompt item 6) ja esta presente ao pe' da letra no texto final, a
    reprovacao ficou obsoleta: o defeito apontado nao existe na versao entregue. Medido
    no CRM-PB: reprovacao citando 'CONSIDRERANDO' -> 'CONSIDERANDO' sobrevivia no
    review_notes mesmo com 'CONSIDRERANDO' ja correto no .md salvo.

    So descarta o item quando (a) o TIPO admite a checagem por presenca
    (TIPOS_CHECAVEIS_POR_PRESENCA - defeito de forma nao admite), (b) ha um termo citavel
    entre aspas e (c) ele bate contra o texto final. Sem aspas na descricao, nao ha como
    checar deterministicamente - mantem o item por seguranca (falso negativo aqui e' pior
    que mostrar um aviso a mais).
    """
    resolvidos = []
    for trecho in trechos:
        if trecho.get("tipo") not in TIPOS_CHECAVEIS_POR_PRESENCA:
            resolvidos.append(trecho)   # defeito de forma: presenca do termo nao prova nada
            continue
        termos = _ASPAS.findall(trecho.get("descricao", ""))
        if termos and termos[0] in texto_final:
            continue
        resolvidos.append(trecho)
    return resolvidos


def espinha_de_artigos(markdown):
    """Numeros de artigo PROPRIOS do documento, em ordem de aparicao.

    Descarta o que for CITACAO de outra norma ("Art. 10 da Resolucao Normativa RN-TC
    No 03/2014"), que aparece dentro de artigo alterador e nao e dispositivo daqui.
    """
    nums = []
    for _, l in _fora_de_codigo(markdown):
        m = ART_LINHA.match(l)
        if not m:
            continue
        resto = _SOBRA_MARCACAO.sub(' ', l[m.end():])
        if CITA_OUTRA_NORMA.match(resto):
            continue
        n = int(m.group(1))
        if n not in nums:
            nums.append(n)
    return nums


def buracos_na_espinha(markdown):
    """Numeros faltantes entre o primeiro e o ultimo artigo do documento.

    ATENCAO - ISTO NAO E PROVA DE OMISSAO, E PERGUNTA. Buraco legitimo existe: a
    PORTARIA CRM-PB 16/2014 salta do art. 3o para o art. 10 NO DOCUMENTO OFICIAL, e
    preservar esse salto e obrigacao (Regra 8). Por isso o resultado vira AVISO, nunca
    correcao automatica - quem decide e a conferencia contra o PDF-fonte.
    """
    nums = espinha_de_artigos(markdown)
    if len(nums) < 2:
        return []
    ordenados = sorted(nums)
    return [n for n in range(ordenados[0], ordenados[-1] + 1) if n not in nums]


def historico_sem_marcador(markdown):
    """PARAGRAFO que DECLARA nova redacao mas nao tem o marcador por perto.

    So cuida de "(Redacao dada pela ...)" - o caso REVOGADO tem portao proprio,
    `revogacao_sem_tachado()`, que sobe ate o caput via ART_LINHA em vez de depender de
    tamanho de janela. Motivo de nao reaproveitar essa funcao aqui: tentado com janela
    fixa de paragrafos primeiro (2, depois 8) e as DUAS quebraram, em direcoes opostas -
    janela curta nao alcancava marcador de blocos longos (caput + varios paragrafos);
    janela larga alcancava o marcador de OUTRO dispositivo e mascarava o defeito de novo,
    o mesmo bug que este portao existe pra pegar. Janela fixa nao sabe onde um dispositivo
    termina e outro comeca; so subir ate o caput sabe. Redacao-dada nao tem esse problema
    porque o padrao e' sempre curto e fixo: marcador, tachado, novo texto - 3 paragrafos.

    Duas leituras possiveis por paragrafo sem marcador, e o aviso nao escolhe entre elas:
      - o conversor EXCLUIU o dispositivo tachado (omissao); ou
      - transcreveu o texto mas nao declarou o marcador (defeito de forma).
    Em ambos, o trecho nao pode seguir sem olho humano.
    """
    paragrafos = markdown.split('\n\n')
    achados = []
    for i, p in enumerate(paragrafos):
        if not DECLARA_NOVA_REDACAO.search(p):
            continue
        anteriores = paragrafos[max(0, i - 2):i]
        janela = p + ''.join(anteriores)
        if any(m in janela for m in _MARCADORES):
            continue
        trecho = p.strip().replace('\n', ' ')[:70]
        achados.append(f'declara "(Redacao dada pela ...)" sem marcador por perto: "{trecho}..."')
    return achados


def revogacao_sem_tachado(markdown):
    """CADA "(... revogado pela ...)" precisa estar em bloco tachado E com marcador.

    Nao basta existir marcador NO DOCUMENTO - foi assim que a v1 deste portao passou a
    reconversao de 21/08 com zero avisos: o arquivo trazia 4 marcadores (arts. 4o e 5o,
    corretos) e, logo abaixo, os arts. 9o e 10 REVOGADOS transcritos como se estivessem
    em vigor. O portao perguntava "ha algum marcador?"; a pergunta certa e "cada marca
    tem o SEU?".

    Devolve [(linha, trecho)] de cada marca de revogacao orfa de tachado/marcador.
    """
    linhas = markdown.split('\n')
    achados = []
    for i, l in enumerate(linhas):
        if not DECLARA_REVOGACAO.search(l):
            continue
        # O bloco do dispositivo vai do CAPUT ate a marca. Janela de tamanho FIXO nao
        # serve: com 14 linhas, a marca do art. 9o alcancava o marcador do art. 4o e se
        # dava por coberta - o mesmo defeito que este portao veio corrigir, em escala
        # menor. Sobe-se ate o caput e para-se nele.
        j = i
        while j > 0 and not ART_LINHA.match(linhas[j]):
            j -= 1
        bloco = linhas[j:i + 1]
        tachado = any('~~' in x for x in bloco)
        # O marcador pertence a ESTE dispositivo: tem de estar coladinho antes do caput.
        marcado = any(mk in x for x in linhas[max(0, j - 3):j] for mk in _MARCADORES)
        if not (tachado and marcado):
            falta = []
            if not tachado: falta.append('~~tachado~~')
            if not marcado: falta.append('marcador **[DISPOSITIVO REVOGADO - ...]**')
            achados.append((i + 1, ' e '.join(falta), l.strip()[:110]))
    return achados


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

    # Dispositivo revogado / redacao substituida (21/08/2026)
    orfas = revogacao_sem_tachado(corrigido)
    if orfas:
        det = "\n  - ".join(f"L{n}: falta {f} — \"{x}\"" for n, f, x in orfas)
        avisos.append(
            f"ATENCAO: {len(orfas)} dispositivo(s) declarado(s) REVOGADO(s) pelo proprio "
            "documento, mas transcrito(s) como se estivessem EM VIGOR:\n  - " + det +
            "\n  Revogacao SEM redacao substituta e o caso que mais escapa: nao ha par "
            "antiga/nova para chamar a atencao, e a marca costuma vir no FIM do artigo, "
            "depois do ultimo paragrafo — mas ela diz 'ARTIGO revogado', e alcanca o caput "
            "e todos os paragrafos. Marcar o artigo INTEIRO."
        )
    hist = historico_sem_marcador(corrigido)
    if hist:
        avisos.append(
            f"ATENCAO: {len(hist)} trecho(s) do documento declaram revogacao/nova redacao "
            "sem o marcador **[DISPOSITIVO REVOGADO - ...]** ou **[REDACAO ANTERIOR - ...]** "
            "por perto. Ou o trecho tachado foi EXCLUIDO (omissao - foi o que aconteceu com "
            "os arts. 9o e 10 da RN-TC 01/2017), ou foi transcrito sem declarar. Conferir "
            "contra o PDF:\n  - " + "\n  - ".join(hist)
        )
    furos = buracos_na_espinha(corrigido)
    if furos:
        avisos.append(
            f"ATENCAO: a numeracao dos artigos salta - faltam {furos} entre o primeiro e o "
            "ultimo do documento. ISTO NAO E PROVA DE OMISSAO: o salto pode estar no proprio "
            "ato (a Portaria CRM-PB 16/2014 pula do art. 3o ao 10 no original, e o salto se "
            "preserva). Mas pode ser dispositivo revogado que foi excluido em vez de marcado. "
            "Conferir o intervalo contra o PDF-fonte antes de aprovar."
        )
    return corrigido, avisos
