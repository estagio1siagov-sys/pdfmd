"""Testes dos portoes deterministicos (SINGRA/SIAGOV, 18/08/2026).

Cada caso abaixo e' um defeito MEDIDO no lote de 10 resolucoes do CRM-PB convertido
em 18/08/2026 - nao e' hipotese. Rodar com:  python manage.py test converter
Nenhum destes testes toca a API do Gemini: rodam offline, de graca, em segundos.
"""
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from converter.forms import PdfUploadForm
from converter.models import ConversionJob
from converter.pipeline.cleanup_worker import _cleanup_once
from converter.pipeline.gemini_client import (
    CALL_BUDGET_PER_STEP,
    TRUNCATION_MAX_ATTEMPTS,
    TRUNCATION_MAX_TOKENS_ATTEMPTS,
    _CallBudget,
    _call_with_truncation_guard,
)

from converter.pipeline.portoes import (
    MARCA_BLOCO_AUSENTE,
    atos_no_documento,
    blocos_ausentes,
    conferir,
    filtra_trechos_resolvidos,
    rebaixar_h1_extras,
    espinha_de_artigos,
    buracos_na_espinha,
    historico_sem_marcador,
    revogacao_sem_tachado,
    DECLARA_REVOGACAO,
)


def _h1(md):
    return [l for l in md.split("\n") if l.startswith("# ")]


class HierarquiaDeTitulosTest(SimpleTestCase):
    """A conversao e' bloco-a-bloco e cada bloco volta com o seu proprio '# '."""

    def test_167_h1_no_cabecalho_do_orgao_e_corrigido(self):
        """167_2014: o '# ' ficou em 'O CRM-PB' e o ato virou '##'. Contador
        localiza, nao decide: `grep -c '^# ' == 1` PASSAVA nesta entrega errada."""
        md = "# O CRM-PB\n## RESOLUÇÃO CRM-PB Nº 167/2014\n\nCONSIDERANDO x\n"
        novo, mud = rebaixar_h1_extras(md)
        self.assertEqual(_h1(novo), ["# RESOLUÇÃO CRM-PB Nº 167/2014"])
        self.assertIn("## O CRM-PB", novo)
        self.assertTrue(mud)

    def test_163_anexo_nao_e_promovido_a_titulo_do_ato(self):
        """163_2014: 3 '#'. O anexo CITA a norma-mae; nao e' o ato."""
        md = ("# CONSELHO REGIONAL DE MEDICINA DO ESTADO DA PARAÍBA - CRM-PB\n\n"
              "**Resolução CRM-PB nº 163/2014**\n\nCONSIDERANDO x\n\n"
              "# ANEXO II À RESOLUÇÃO CRM-PB Nº 163/2014\n")
        novo, _ = rebaixar_h1_extras(md)
        self.assertEqual(_h1(novo), ["# Resolução CRM-PB nº 163/2014"])
        self.assertIn("## ANEXO II À RESOLUÇÃO CRM-PB Nº 163/2014", novo)

    def test_168_titulo_com_data_por_extenso_e_reconhecido(self):
        """168/170/176 grafam 'N.º 168, DE 1º DE DEZEMBRO DE 2014' - sem numero/ano.
        Regressao: uma versao anterior do portao NAO reconhecia e rebaixava tudo."""
        md = "# RESOLUÇÃO CRM-PB N.º 168, DE 1º DE DEZEMBRO DE 2014.\n\nCONSIDERANDO x\n"
        novo, mud = rebaixar_h1_extras(md)
        self.assertEqual(_h1(novo), ["# RESOLUÇÃO CRM-PB N.º 168, DE 1º DE DEZEMBRO DE 2014."])
        self.assertEqual(mud, [], "documento ja correto nao deve ser alterado")

    def test_portao_jamais_zera_os_h1_do_documento(self):
        """A GUARDA. Um portao que rebaixa tudo deixa o .md sem '# ' nenhum -
        estrago pior que o defeito. Melhor errar por inercia que por estrago."""
        md = "# TITULO QUE O PORTAO NAO RECONHECE\n\ntexto qualquer\n"
        novo, avisos = rebaixar_h1_extras(md)
        self.assertEqual(len(_h1(novo)), 1)
        self.assertEqual(novo, md, "sem candidato reconhecido, nao se mexe")
        self.assertTrue(avisos, "mas avisa")

    def test_documento_ja_correto_nao_e_tocado(self):
        md = "# RESOLUÇÃO CRM-PB nº 175/2015\n\nCONSIDERANDO x\n\n## ANEXO I\n"
        novo, mud = rebaixar_h1_extras(md)
        self.assertEqual(novo, md)
        self.assertEqual(mud, [])


class AtosAutonomosTest(SimpleTestCase):
    """165_2014.pdf tem 4 paginas e DOIS atos. So o primeiro foi entregue,
    e a PORTARIA CRM-PB no 16/2014 inteira ficou fora do acervo."""

    def test_dois_atos_no_mesmo_documento(self):
        md = ("# RESOLUÇÃO CRM-PB nº 165/2014\n\nCONSIDERANDO a necessidade\n\nRESOLVE:\n\n"
              "**Art. 1º.** As instalações...\n\nJoão Pessoa, 30 de junho de 2014\n\n"
              "PORTARIA CRM-PB nº 16/2014\n\nO PRESIDENTE do CRM-PB...\n\n"
              "CONSIDERANDO que o Conselho\n\nRESOLVE:\n\n**Art. 1º** - Os conselheiros...\n")
        atos = atos_no_documento(md)
        self.assertEqual(len(atos), 2)
        self.assertEqual({a["numero"] for a in atos}, {"165/2014", "16/2014"})

    def test_citacao_de_norma_nao_conta_como_ato(self):
        """Sem esta trava, o 165_2014 acusava 5 atos onde ha 2."""
        md = ("# RESOLUÇÃO CRM-PB nº 165/2014\n\n"
              "Revoga a Resolução CRM-PB nº 153/2011 e estabelece o protocolo.\n\n"
              "CONSIDERANDO x\n\nRESOLVE:\n\n"
              "REVOGADA pela Resolução CRM PB nº 0210/2025\n\n"
              "conforme o art.1º da Resolução CRM-PB nº 163/2014;\n")
        self.assertEqual(len(atos_no_documento(md)), 1)

    def test_conferir_avisa_quando_ha_mais_de_um_ato(self):
        md = ("# RESOLUÇÃO CRM-PB nº 165/2014\n\nCONSIDERANDO x\n\nRESOLVE:\n\n"
              "PORTARIA CRM-PB nº 16/2014\n\nCONSIDERANDO y\n\nRESOLVE:\n")
        _, avisos = conferir(md)
        self.assertTrue(any("2 atos autonomos" in a for a in avisos))


class BlocoAusenteTest(SimpleTestCase):
    """O `continue` do runner descartava bloco bloqueado em silencio: o .md saia
    sem aquelas paginas e o job terminava DONE. Agora o buraco vai DENTRO do
    artefato, onde viaja junto com ele."""

    def test_marcador_de_bloco_ausente_e_detectado(self):
        md = f"# RESOLUÇÃO CRM-PB nº 1/2020\n\n{MARCA_BLOCO_AUSENTE}: paginas 3-4 -->\n"
        self.assertEqual(len(blocos_ausentes(md)), 1)
        _, avisos = conferir(md)
        self.assertTrue(any("nao entraram no arquivo" in a for a in avisos))

    def test_documento_integro_nao_gera_aviso(self):
        md = "# RESOLUÇÃO CRM-PB nº 1/2020\n\nCONSIDERANDO x\n\nRESOLVE:\n"
        _, avisos = conferir(md)
        self.assertEqual(avisos, [])


class FiltroDeTrechosResolvidosTest(SimpleTestCase):
    """`filtra_trechos_resolvidos` decide se um aviso CHEGA ou NAO ao usuario, e roda
    ANTES do meta-validador: o item que ela descarta nunca vai a segunda instancia.
    E' a classe de codigo que mais precisa de teste."""

    def test_descarta_quando_a_grafia_original_foi_restituida(self):
        """O validador reprovou numa versao antiga; a reconversao seguinte ja corrigiu."""
        trechos = [{"tipo": "valor_incorreto",
                    "descricao": 'A palavra "CONSIDERENDO" do PDF virou "CONSIDERANDO" no Markdown.'}]
        self.assertEqual(filtra_trechos_resolvidos(trechos, "**Art. 1º** CONSIDERENDO o que..."), [])

    def test_mantem_quando_o_defeito_persiste(self):
        trechos = [{"tipo": "valor_incorreto",
                    "descricao": 'A palavra "CONSIDERENDO" do PDF virou "CONSIDERANDO" no Markdown.'}]
        self.assertEqual(len(filtra_trechos_resolvidos(trechos, "**Art. 1º** CONSIDERANDO o que...")), 1)

    def test_descarta_omissao_que_o_texto_final_ja_contem(self):
        trechos = [{"tipo": "omissao", "descricao": 'Falta o "Parágrafo único" do Art. 3.'}]
        self.assertEqual(filtra_trechos_resolvidos(trechos, "Parágrafo único – A verba..."), [])

    def test_tabela_achatada_NAO_e_descartada_pela_presenca_do_cabecalho(self):
        """Defeito de FORMA: a tabela achatada em texto corrido contem o proprio
        cabecalho que a descricao cita. Presenca do termo nao prova correcao."""
        trechos = [{"tipo": "tabela_incorreta",
                    "descricao": 'A tabela com cabeçalho "Descrição da Despesa" não foi convertida para markdown nativo.'}]
        md = "Descrição da Despesa Qde Vlr. Unitário Total em R$\nTOTAL .....R$\n"
        self.assertEqual(len(filtra_trechos_resolvidos(trechos, md)), 1)

    def test_hierarquia_quebrada_NAO_e_descartada_pela_presenca_do_titulo(self):
        """Mesmo principio: "# ANEXO I" indevido contem "ANEXO I"."""
        trechos = [{"tipo": "estrutura_incorreta",
                    "descricao": 'O bloco "ANEXO I" está marcado como título de nível 1 (#).'}]
        md = "# RESOLUÇÃO CRM-PB nº 172/2015\n\ntexto\n\n# ANEXO I\n"
        self.assertEqual(len(filtra_trechos_resolvidos(trechos, md)), 1)

    def test_sem_termo_entre_aspas_mantem_por_seguranca(self):
        trechos = [{"tipo": "omissao", "descricao": "Falta um parágrafo inteiro no meio do Art. 4."}]
        self.assertEqual(len(filtra_trechos_resolvidos(trechos, "qualquer texto")), 1)


class DispositivoRevogadoTest(SimpleTestCase):
    """Dispositivo revogado / redacao substituida - SINGRA, 21/08/2026.

    Caso-ancora: RN-TC 01/2017 do TCE-PB. O PDF preserva os arts. 9o e 10 tachados, com
    "(Artigo revogado pela Resolucao Normativa TC no 06/2020 ...)"; o conversor os excluiu
    para nao repetir texto, e o .md saltou do art. 8o para o art. 11. Passou por DUAS
    conversoes independentes sem ser detectado, e chegou ao acervo.
    """

    RNTC_COMO_SAIU = (
        "# RESOLUCAO NORMATIVA RN-TC No 01/2017\n\n"
        "**Art. 8o.** Os autos eletronicos do Processo de Acompanhamento da Gestao deverao "
        "ser juntados ao respectivo Processo de Prestacao de Contas Anual. "
        "(Redacao dada pela Resolucao Normativa TC no 01/2025)\n\n"
        "## CAPITULO III\n\n"
        "**Art. 11.** O balancete declarado como nao entregue ensejara as penalidades previstas.\n"
    )

    RNTC_COMO_DEVE_SAIR = (
        "# RESOLUCAO NORMATIVA RN-TC No 01/2017\n\n"
        "**[REDACAO ANTERIOR - art. 8o, substituida pela RN-TC no 01/2025]**\n\n"
        "~~**Art. 8o.** Todos os achados de auditoria durante o acompanhamento da Gestao "
        "deverao ser juntados aos autos eletronicos do respectivo processo.~~\n\n"
        "**Art. 8o.** Os autos eletronicos do Processo de Acompanhamento da Gestao deverao "
        "ser juntados ao respectivo Processo de Prestacao de Contas Anual. "
        "(Redacao dada pela Resolucao Normativa TC no 01/2025)\n\n"
        "**[DISPOSITIVO REVOGADO - art. 9o, pela RN-TC no 06/2020, DOE-TCE/PB de 22/12/2020]**\n\n"
        "~~**Art. 9o.** Apos o processamento do balancete relativo a dezembro de cada exercicio, "
        "sera elaborado o Relatorio Previo sobre a Gestao do Poder ou Orgao.~~ "
        "(Artigo revogado pela Resolucao Normativa TC no 06/2020)\n\n"
        "## CAPITULO III\n\n"
        "**[DISPOSITIVO REVOGADO - art. 10, pela RN-TC no 06/2020, DOE-TCE/PB de 22/12/2020]**\n\n"
        "~~**Art. 10.** O Gestor quando da apresentacao da respectiva Prestacao de Contas Anual "
        "devera, a titulo de defesa, esclarecer todas as irregularidades.~~ "
        "(Artigo revogado pela Resolucao Normativa TC no 06/2020)\n\n"
        "**Art. 11.** O balancete declarado como nao entregue ensejara as penalidades previstas.\n"
    )

    def test_exclusao_do_revogado_e_apanhada_pelo_buraco_na_espinha(self):
        self.assertEqual(buracos_na_espinha(self.RNTC_COMO_SAIU), [9, 10])

    def test_redacao_dada_sem_marcador_e_apanhada(self):
        """O que este portao pega neste fixture e o art. 8o, que declara "(Redacao dada
        pela ...)" sem o par marcador+tachado. Os arts. 9o e 10 excluidos nao deixam
        rastro que ESTA funcao possa ver - quem os pega e o buraco na espinha, acima.
        A assercao antiga era so `assertTrue(achados)`, que passava sem dizer por que."""
        achados = historico_sem_marcador(self.RNTC_COMO_SAIU)
        self.assertEqual(len(achados), 1)
        self.assertIn("Art. 8o", achados[0])

    def test_conferir_avisa_nos_dois_eixos(self):
        _, avisos = conferir(self.RNTC_COMO_SAIU)
        texto = "\n".join(avisos)
        # Discriminadores do que foi DETECTADO, nao do texto fixo do aviso: antes isto
        # casava com "DISPOSITIVO REVOGADO" impresso no boilerplate da mensagem, e
        # passaria mesmo com deteccao zerada.
        self.assertEqual(len(avisos), 2)
        self.assertIn("Redacao dada", texto)
        self.assertIn("faltam [9, 10]", texto)

    def test_transcricao_correta_nao_gera_aviso_de_historico(self):
        # Assercao forte: a versao correta nao produz aviso NENHUM. A antiga procurava
        # "nao ha marcador", string que nao existe em lugar nenhum do projeto - passava
        # mesmo se o portao acusasse o documento inteiro.
        self.assertEqual(historico_sem_marcador(self.RNTC_COMO_DEVE_SAIR), [])
        _, avisos = conferir(self.RNTC_COMO_DEVE_SAIR)
        self.assertEqual(avisos, [])

    def test_artigo_tachado_entra_na_espinha(self):
        self.assertEqual(espinha_de_artigos(self.RNTC_COMO_DEVE_SAIR), [8, 9, 10, 11])

    def test_salto_legitimo_do_orgao_e_apenas_AVISO_nunca_correcao(self):
        """Portaria CRM-PB 16/2014: o original pula do art. 3o ao 10. Preservar e obrigacao."""
        portaria = (
            "# PORTARIA CRM-PB no 16/2014\n\n"
            "**Art. 1o** - Os conselheiros farao jus a diaria.\n\n"
            "**Art. 2o** - Os funcionarios farao jus a diaria.\n\n"
            "**Art. 3o** - Fica estabelecido o valor da verba indenizatoria.\n\n"
            "**Art. 10** - Fica estabelecido o valor do auxilio de representacao.\n\n"
            "**Art. 11** - Caso os valores ultrapassem os limites do CFM.\n"
        )
        corrigido, avisos = conferir(portaria)
        self.assertEqual(corrigido, portaria)          # NAO tocou no documento
        self.assertIn("NAO E PROVA DE OMISSAO", "\n".join(avisos))

    def test_marcador_correto_num_dispositivo_nao_esconde_falta_em_outro(self):
        """Bug real medido: art. 5o saiu com **[REDACAO ANTERIOR...]** certinho, mas os
        arts. 9o/10 (revogados, SEM substituto - sem o par contrastante que parece ajudar
        o modelo a aplicar a regra) sairam so com "(Artigo revogado pela ...)" no fim da
        frase, sem tachado nem marcador algum. A checagem antiga era global no documento
        (`any(marcador in markdown)`): o marcador do art. 5o fazia o portao dar o
        documento INTEIRO por resolvido, escondendo os arts. 9o/10 sem marcador."""
        md = (
            "# RESOLUCAO NORMATIVA RN-TC No 01/2017\n\n"
            "**[REDACAO ANTERIOR - art. 5o, substituida pela RN-TC no 06/2020]**\n\n"
            "~~**Art. 5o.** Sem prejuizo da instauracao de processos de Tomadas de Contas "
            "Especial, da instrucao do processo de acompanhamento decorrera a/o:~~\n\n"
            "**Art. 5o.** Do acompanhamento da gestao estadual e municipal decorrera a/o: "
            "(Redacao dada pela Resolucao Normativa TC no 06/2020)\n\n"
            "**Art. 9o.** Apos o processamento do balancete, sera elaborado o Relatorio "
            "Previo. (Artigo revogado pela Resolucao Normativa TC no 06/2020)\n\n"
            "**Art. 10.** O Gestor devera esclarecer as irregularidades remanescentes. "
            "(Artigo revogado pela Resolucao Normativa TC no 06/2020)\n"
        )
        # Revogacao (arts. 9o/10) e' responsabilidade de revogacao_sem_tachado(), nao mais
        # de historico_sem_marcador() - ver o comentario dessa funcao no portoes.py sobre
        # janela fixa ter quebrado nos dois tamanhos tentados.
        achados = revogacao_sem_tachado(md)
        self.assertEqual(len(achados), 2)  # os DOIS artigos sem marcador, nao mascarados
        self.assertTrue(any("balancete" in a[2] for a in achados))
        self.assertTrue(any("Gestor" in a[2] for a in achados))

    def test_citacao_de_artigo_de_outra_norma_nao_entra_na_espinha(self):
        """Art. 14 da RN-TC 01/2017 transcreve o art. 10 da RN-TC 03/2014."""
        md = (
            "# RESOLUCAO NORMATIVA RN-TC No 01/2017\n\n"
            "**Art. 13.** Durante o exercicio financeiro objeto do acompanhamento.\n\n"
            "**Art. 14.** O art. 10 da Resolucao Normativa RN-TC No 03/2014 passa a vigorar "
            "com a seguinte redacao:\n\n"
            "**Art. 10.** da Resolucao Normativa RN-TC No 03/2014 - Os balancetes mensais nao "
            "gozarao da possibilidade de substituicao.\n\n"
            "**Art. 15.** Esta Resolucao entra em vigor na data da sua publicacao.\n"
        )
        self.assertNotIn(10, espinha_de_artigos(md))
        self.assertEqual(espinha_de_artigos(md), [13, 14, 15])


class RevogadoSemSubstitutoTest(SimpleTestCase):
    """CASO B - revogacao SEM redacao substituta. O que mais escapa.

    Medido na reconversao de 21/08/2026 da RN-TC 01/2017, JA COM O PATCH APLICADO: o
    conversor acertou os arts. 4o e 5o (caso A, que tem par antiga/nova) e transcreveu os
    arts. 9o e 10 (caso B) com texto NORMAL - sem tachado, sem marcador - como se
    estivessem em vigor. A v1 do portao passou o arquivo com ZERO avisos, porque
    perguntava "ha ALGUM marcador no documento?" e havia (os dos arts. 4o e 5o).
    A pergunta certa e "CADA marca tem o SEU marcador?".
    """

    # exatamente o padrao que veio na reconversao: marcador correto em cima, defeito embaixo
    V3_COMO_VEIO = (
        "# RESOLUCAO NORMATIVA RN-TC No 01/2017\n\n"
        "**[REDACAO ANTERIOR - art. 4o, substituida pela RN-TC no 06/2020]**\n\n"
        "~~**Art. 4o.** Ato da Presidencia do Tribunal definira os procedimentos.~~\n\n"
        "**Art. 4o.** Ato do Presidente do Tribunal aprovara os procedimentos. "
        "(Redacao dada pela Resolucao Normativa TC no 06/2020)\n\n"
        "**Art. 9o.** Apos o processamento do balancete relativo a dezembro de cada "
        "exercicio, sera elaborado o Relatorio Previo sobre a Gestao do Poder ou Orgao.\n\n"
        "**§ 1o.** No ambito do Poder Executivo estadual, deverao ser gerados Relatorios.\n\n"
        "**§ 2o.** No ambito do Poder Executivo municipal, deverao ser gerados Relatorios "
        "quando preenchidos os criterios. (Artigo revogado pela Resolucao Normativa TC no "
        "06/2020, publicada no Diario Oficial Eletronico do TCE/PB, de 22 de dezembro de 2020)\n"
    )

    V3_COMO_DEVE_SAIR = (
        "# RESOLUCAO NORMATIVA RN-TC No 01/2017\n\n"
        "**[REDACAO ANTERIOR - art. 4o, substituida pela RN-TC no 06/2020]**\n\n"
        "~~**Art. 4o.** Ato da Presidencia do Tribunal definira os procedimentos.~~\n\n"
        "**Art. 4o.** Ato do Presidente do Tribunal aprovara os procedimentos. "
        "(Redacao dada pela Resolucao Normativa TC no 06/2020)\n\n"
        "**[DISPOSITIVO REVOGADO - art. 9o, pela RN-TC no 06/2020, DOE-TCE/PB 22/12/2020]**\n\n"
        "~~**Art. 9o.** Apos o processamento do balancete relativo a dezembro de cada "
        "exercicio, sera elaborado o Relatorio Previo sobre a Gestao do Poder ou Orgao.~~\n\n"
        "~~**§ 1o.** No ambito do Poder Executivo estadual, deverao ser gerados Relatorios.~~\n\n"
        "~~**§ 2o.** No ambito do Poder Executivo municipal, deverao ser gerados Relatorios "
        "quando preenchidos os criterios.~~ (Artigo revogado pela Resolucao Normativa TC no "
        "06/2020, publicada no Diario Oficial Eletronico do TCE/PB, de 22 de dezembro de 2020)\n"
    )

    def test_marcador_de_OUTRO_artigo_nao_absolve_o_defeituoso(self):
        """A REGRESSAO que motivou este portao: havia marcador, e o defeito passou."""
        self.assertEqual(historico_sem_marcador(self.V3_COMO_VEIO), [])   # v1 nao via nada
        self.assertTrue(revogacao_sem_tachado(self.V3_COMO_VEIO))          # v2 ve

    def test_conferir_agora_reprova_o_arquivo_que_passou(self):
        _, avisos = conferir(self.V3_COMO_VEIO)
        texto = "\n".join(avisos)
        self.assertIn("transcrito(s) como se estivessem EM VIGOR", texto)

    def test_transcricao_correta_do_caso_B_nao_gera_aviso_de_revogacao(self):
        _, avisos = conferir(self.V3_COMO_DEVE_SAIR)
        self.assertEqual(revogacao_sem_tachado(self.V3_COMO_DEVE_SAIR), [])
        self.assertNotIn("EM VIGOR", "\n".join(avisos))
        # O fixture continua produzindo UM aviso, o de salto na numeracao (o trecho vai do
        # art. 4o ao 9o). Isso e' pergunta, nao defeito - ver buracos_na_espinha. Fica
        # assertado de proposito: o nome antigo do teste dizia "nao gera aviso" e mascarava
        # que este padrao-ouro cairia em needs_review na producao.
        self.assertEqual(len(avisos), 1)
        self.assertIn("numeracao dos artigos salta", avisos[0])

    def test_marca_no_fim_do_artigo_alcanca_o_caput_acima(self):
        """A marca vem colada ao ultimo paragrafo; a janela tem de subir ate o caput."""
        orfas = revogacao_sem_tachado(self.V3_COMO_VEIO)
        self.assertEqual(len(orfas), 1)
        linha, falta, _ = orfas[0]
        self.assertIn("~~tachado~~", falta)
        self.assertIn("DISPOSITIVO REVOGADO", falta)


class FurosDosPortoesTest(SimpleTestCase):
    """Regressao dos 8 furos achados na revisao completa de 21/08/2026.

    Cada teste abaixo REPRODUZ o input exato que passava batido (ou que era acusado
    indevidamente) antes da correcao. Sao os casos que a suite anterior nao cobria:
    ela testava os portoes contra os documentos-ancora do lote de 18/08 e nao contra
    as variacoes de grafia que o mesmo acervo produz.
    """

    def test_declaracao_de_revogacao_aceita_sujeito_qualificado(self):
        """"(Inciso II revogado pela ...)" e a forma mais comum em texto compilado.
        O padrao antigo so aceitava o substantivo nu e perdia todas as qualificadas."""
        for decl in (
            "(Artigo revogado pela Resolucao X)",
            "(Inciso II revogado pela Lei X)",
            "(Paragrafo unico revogado pela Lei X)",
            "(Artigo 9o revogado pela Resolucao X)",
            "(Revogado pela Lei X)",
        ):
            with self.subTest(decl=decl):
                self.assertTrue(DECLARA_REVOGACAO.search(decl))

    def test_tachado_parcial_do_bloco_e_defeito(self):
        """Quarto estado que validator_prompt declara reprovavel: caput e § 1o saem como
        vigentes e so o § 2o (o que toca a nota) sai riscado. Aceitar UMA linha riscada
        em qualquer lugar do bloco deixava isso passar com zero avisos."""
        md = (
            "**[DISPOSITIVO REVOGADO - art. 9o, pela RN-TC no 06/2020]**\n\n"
            "**Art. 9o.** Caput NAO tachado, como se vigente.\n\n"
            "**Paragrafo 1o.** Paragrafo NAO tachado.\n\n"
            "~~**Paragrafo 2o.** So este saiu tachado.~~ (Artigo revogado pela Resolucao X)\n"
        )
        orfas = revogacao_sem_tachado(md)
        self.assertEqual(len(orfas), 1)
        self.assertIn("bloco INTEIRO", orfas[0][1])

    def test_marcador_de_revogado_nao_absolve_redacao_dada(self):
        """O **[DISPOSITIVO REVOGADO]** do art. 9o nao cobre o art. 10, que declara
        "(Redacao dada pela ...)" e nao tem marcador nem tachado proprios."""
        md = (
            "**[DISPOSITIVO REVOGADO - art. 9o, pela RN-TC no 06/2020]**\n\n"
            "~~**Art. 9o.** Texto revogado.~~\n\n"
            "**Art. 10.** Texto novo. (Redacao dada pela Resolucao Normativa TC no 06/2020)\n"
        )
        self.assertEqual(len(historico_sem_marcador(md)), 1)

    def test_redacao_anterior_de_duas_linhas_nao_gera_falso_positivo(self):
        """Redacao antiga ocupando caput + inciso poe o marcador 3 paragrafos atras.
        A janela fixa de 2 acusava documento correto; agora sobe enquanto houver tachado."""
        md = (
            "**[REDACAO ANTERIOR - art. 5o, substituida pela RN-TC no 06/2020]**\n\n"
            "~~**Art. 5o.** Caput antigo:~~\n\n"
            "~~I - inciso antigo;~~\n\n"
            "**Art. 5o.** Caput novo: (Redacao dada pela Resolucao Normativa TC no 06/2020)\n"
        )
        self.assertEqual(historico_sem_marcador(md), [])

    def test_citacao_de_outra_norma_com_travessao_nao_entra_na_espinha(self):
        """"**Art. 10** - da Resolucao X" e citacao dentro de artigo alterador. O limpador
        de marcacao parava no espaco depois do "**", a citacao virava artigo proprio e
        fabricava buraco [11, 12] num documento correto."""
        md = (
            "# RESOLUCAO NORMATIVA RN-TC No 01/2017\n\n"
            "**Art. 13.** Durante o exercicio.\n\n"
            "**Art. 14.** O art. 10 da Resolucao Normativa RN-TC No 03/2014 passa a vigorar:\n\n"
            "**Art. 10** - da Resolucao Normativa RN-TC No 03/2014 - Os balancetes mensais.\n\n"
            "**Art. 15.** Esta Resolucao entra em vigor.\n"
        )
        self.assertEqual(espinha_de_artigos(md), [13, 14, 15])
        self.assertEqual(buracos_na_espinha(md), [])

    def test_artigo_como_heading_entra_na_espinha(self):
        """Artigo emitido como "### Art. 1o" zerava a espinha inteira, e espinha vazia
        devolve buracos [] - o documento PASSAVA como integro em vez de avisar."""
        md = "# RES X\n\n### Art. 1o\n\nTexto\n\n### Art. 5o\n\nTexto\n"
        self.assertEqual(espinha_de_artigos(md), [1, 5])
        self.assertEqual(buracos_na_espinha(md), [2, 3, 4])

    def test_cerca_de_codigo_aberta_nao_apaga_o_resto_do_documento(self):
        """Uma ``` sem fechar cegava tudo dali pra baixo - inclusive o marcador de bloco
        ausente que o runner grava no artefato pra nao perder paginas em silencio."""
        md = (
            "# RES X\n\n**Art. 1o** Texto\n\n```\n**Art. 9o** Texto\n\n"
            + MARCA_BLOCO_AUSENTE + ": paginas 3-4 -->\n"
        )
        self.assertEqual(espinha_de_artigos(md), [1, 9])
        self.assertEqual(len(blocos_ausentes(md)), 1)

    def test_marcador_em_caixa_mista_e_reconhecido(self):
        """O marcador e escrito por LLM e sai tanto em caixa alta quanto em Title Case.
        A comparacao literal com `in` acusava documento corretamente marcado."""
        md = (
            "**[Dispositivo revogado - art. 9o]**\n\n"
            "~~**Art. 9o.** Texto.~~ (Artigo revogado pela Lei X)\n"
        )
        self.assertEqual(revogacao_sem_tachado(md), [])

    def test_revogacao_do_ato_inteiro_no_cabecalho_nao_vira_dispositivo_orfao(self):
        """"RESOLUCAO X (Revogada pela Y)" no titulo revoga o ATO, nao um dispositivo.
        A subida ate o caput chegava ao topo e acusava a linha do titulo."""
        md = (
            "# RESOLUCAO CFM no 1.931/2009 (Revogada pela Resolucao CFM no 2.217/2018)\n\n"
            "CONSIDERANDO x\n\nRESOLVE:\n\n**Art. 1o** Texto.\n"
        )
        self.assertEqual(revogacao_sem_tachado(md), [])

    def test_apostrofo_nao_trunca_o_termo_citado(self):
        """A aspa simples reta e apostrofo em portugues. Tratada como delimitador, ela
        cortava o termo em "o servidor d", que casa com quase tudo - e o filtro
        DESCARTAVA um item legitimo, o pior erro que ele pode cometer."""
        trechos = [{
            "tipo": "omissao",
            "descricao": "Falta o trecho \"o servidor d'agua sera designado pelo Presidente\" do Art. 5.",
        }]
        final = "**Art. 5o.** o servidor da secretaria assinara."
        self.assertEqual(filtra_trechos_resolvidos(trechos, final), trechos)

    def test_numero_do_ato_com_separador_de_milhar_e_ano_por_extenso(self):
        """Dois defeitos no rotulo mostrado ao usuario: "No 1.931/2009" virava "931/2009"
        (o prefixo preguicoso reengatava depois do ponto) e a forma "N.o 168, DE 1o DE
        DEZEMBRO DE 2014" virava "168/None" (lia so o grupo do ano com barra)."""
        md = (
            "RESOLUCAO CFM no 1.931/2009\n\nCONSIDERANDO x\n\nRESOLVE:\n\n"
            "PORTARIA CRM-PB N.o 168, DE 1o DE DEZEMBRO DE 2014\n\nCONSIDERANDO y\n\nRESOLVE:\n"
        )
        atos = atos_no_documento(md)
        self.assertEqual([a["numero"] for a in atos], ["1931/2009", "168/2014"])


class DownloadEExposicaoDeErroTest(TestCase):
    """Cobre as duas rotas publicas. Nenhuma tinha teste, e as duas mexem com coisa
    irreversivel (apagar arquivo) ou sensivel (mensagem de erro crua)."""

    def test_download_all_recusa_GET(self):
        """A view APAGA jobs e arquivos depois de montar o ZIP. Como GET, um prefetch do
        navegador ou um <img src> de terceiro disparava a exclusao."""
        resp = self.client.get(reverse("converter:download_all"), {"ids": "1,2"})
        self.assertEqual(resp.status_code, 405)

    def test_progress_status_nao_vaza_traceback(self):
        """`error_message` guarda o traceback completo, e este endpoint e publico com
        job_id sequencial - devolver o texto cru expunha caminho de servidor e codigo."""
        job = ConversionJob.objects.create(
            original_filename="x.pdf",
            status=ConversionJob.STATUS_FAILED,
            error_message='Erro inesperado:\nTraceback (most recent call last):\n  File "/app/x.py"',
        )
        data = self.client.get(reverse("converter:progress_status", args=[job.pk])).json()
        self.assertNotIn("Traceback", data["error_message"])
        self.assertNotIn("/app/x.py", data["error_message"])

    def test_progress_status_preserva_erro_util_ao_usuario(self):
        """O saneamento nao pode engolir a mensagem acionavel (ex.: PDF com senha)."""
        job = ConversionJob.objects.create(
            original_filename="x.pdf",
            status=ConversionJob.STATUS_FAILED,
            error_message="PDF protegido por senha não é suportado. Remova a senha antes de enviar.",
        )
        data = self.client.get(reverse("converter:progress_status", args=[job.pk])).json()
        self.assertIn("protegido por senha", data["error_message"])


class LimpezaAutomaticaTest(TestCase):
    """`_cleanup_once` apaga registro e arquivo permanentemente e nao tinha teste:
    se o filtro de status se perdesse, apagaria job em pleno processamento."""

    def _job(self, status, horas_atras):
        job = ConversionJob.objects.create(original_filename="x.pdf", status=status)
        # updated_at e auto_now: precisa de UPDATE direto para simular idade.
        ConversionJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(hours=horas_atras)
        )
        return job

    def test_apaga_so_o_que_esta_concluido_e_velho(self):
        velho_done = self._job(ConversionJob.STATUS_DONE, 3)
        velho_failed = self._job(ConversionJob.STATUS_FAILED, 3)
        velho_running = self._job(ConversionJob.STATUS_RUNNING, 3)
        velho_queued = self._job(ConversionJob.STATUS_QUEUED, 3)
        novo_done = self._job(ConversionJob.STATUS_DONE, 0)

        _cleanup_once()

        vivos = set(ConversionJob.objects.values_list("pk", flat=True))
        self.assertNotIn(velho_done.pk, vivos)
        self.assertNotIn(velho_failed.pk, vivos)
        # Um job em processamento nunca pode sumir por baixo do worker.
        self.assertIn(velho_running.pk, vivos)
        self.assertIn(velho_queued.pk, vivos)
        self.assertIn(novo_done.pk, vivos)


class UploadFormTest(SimpleTestCase):
    """`clean_pdf_files` e a unica barreira do lado do servidor: o aviso de tamanho na
    tela de upload e JavaScript e um `curl` passa por cima."""

    def _form(self, arquivo):
        return PdfUploadForm({}, {"pdf_files": [arquivo]})

    def test_aceita_pdf_valido_e_devolve_o_ponteiro_ao_inicio(self):
        f = SimpleUploadedFile("ok.pdf", b"%PDF-1.7 conteudo", content_type="application/pdf")
        form = self._form(f)
        self.assertTrue(form.is_valid(), form.errors)
        # Sem o seek(0), os 5 bytes lidos na validacao sumiriam do arquivo gravado.
        self.assertEqual(form.cleaned_data["pdf_files"][0].read(5), b"%PDF-")

    def test_recusa_arquivo_renomeado_para_pdf(self):
        f = SimpleUploadedFile("falso.pdf", b"MZ\x90\x00 executavel", content_type="application/pdf")
        self.assertFalse(self._form(f).is_valid())

    def test_recusa_extensao_errada(self):
        f = SimpleUploadedFile("doc.txt", b"%PDF-1.7", content_type="application/pdf")
        self.assertFalse(self._form(f).is_valid())

    def test_recusa_arquivo_acima_do_limite(self):
        with override_settings(MAX_PDF_BYTES=10):
            f = SimpleUploadedFile("grande.pdf", b"%PDF-" + b"x" * 100)
            form = self._form(f)
            self.assertFalse(form.is_valid())
            self.assertIn("excede o limite", str(form.errors))


class TetoDeChamadasNaApiTest(SimpleTestCase):
    """O guard de truncamento roda DENTRO do @_rate_limit_retry, entao as duas contagens
    se multiplicavam: medido com contador, 15 chamadas reais numa unica conversao de
    bloco - todas debitadas da cota do Google. Estes testes fixam o teto."""

    def setUp(self):
        # O guard dorme TRUNCATION_WAIT_SECONDS entre tentativas. Sem neutralizar, estes
        # quatro testes sozinhos custavam ~15s numa suite que roda em milissegundos.
        patcher = mock.patch("converter.pipeline.gemini_client.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _resposta(self, texto, finish):
        cand = type("C", (), {"finish_reason": type("F", (), {"name": finish})})
        return type("R", (), {"text": texto, "candidates": [cand]})

    def test_max_tokens_nao_reenvia_tres_vezes(self):
        """Reenviar o MESMO input reproduz o mesmo corte: insistir era queima de quota."""
        chamadas = []
        cortada = self._resposta("cortado", "MAX_TOKENS")
        texto, _ = _call_with_truncation_guard(lambda: (chamadas.append(1), cortada)[1])
        self.assertEqual(len(chamadas), TRUNCATION_MAX_TOKENS_ATTEMPTS)
        self.assertEqual(texto, "cortado", "devolve o melhor texto obtido, nunca vazio a toa")

    def test_resposta_vazia_ainda_usa_todas_as_tentativas(self):
        """Vazio e hiccup transitorio (safety/instabilidade), nao teto de saida."""
        chamadas = []
        vazia = self._resposta("", "STOP")
        _call_with_truncation_guard(lambda: (chamadas.append(1), vazia)[1])
        self.assertEqual(len(chamadas), TRUNCATION_MAX_ATTEMPTS)

    def test_budget_limita_o_total_mesmo_com_varias_tentativas(self):
        """O budget e criado FORA da funcao decorada de proposito. Se for movido pra
        dentro, o tenacity zera o teto a cada retentativa e a multiplicacao volta."""
        chamadas = []
        vazia = self._resposta("", "STOP")
        budget = _CallBudget(2)
        for _ in range(5):   # simula as retentativas do tenacity reentrando no guard
            _call_with_truncation_guard(lambda: (chamadas.append(1), vazia)[1], budget)
        self.assertEqual(len(chamadas), 2)

    def test_sucesso_de_primeira_gasta_uma_chamada_so(self):
        chamadas = []
        boa = self._resposta("markdown bom", "STOP")
        texto, _ = _call_with_truncation_guard(
            lambda: (chamadas.append(1), boa)[1], _CallBudget(CALL_BUDGET_PER_STEP)
        )
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(texto, "markdown bom")
