"""Testes dos portoes deterministicos (SINGRA/SIAGOV, 18/08/2026).

Cada caso abaixo e' um defeito MEDIDO no lote de 10 resolucoes do CRM-PB convertido
em 18/08/2026 - nao e' hipotese. Rodar com:  python manage.py test converter
Nenhum destes testes toca a API do Gemini: rodam offline, de graca, em segundos.
"""
from django.test import SimpleTestCase

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

    def test_exclusao_do_revogado_e_apanhada_pela_marca_sem_marcador(self):
        achados = historico_sem_marcador(self.RNTC_COMO_SAIU)
        self.assertTrue(achados)

    def test_conferir_avisa_nos_dois_eixos(self):
        _, avisos = conferir(self.RNTC_COMO_SAIU)
        texto = "\n".join(avisos)
        self.assertIn("DISPOSITIVO REVOGADO", texto)
        self.assertIn("numeracao dos artigos salta", texto)

    def test_transcricao_correta_nao_gera_aviso_de_historico(self):
        _, avisos = conferir(self.RNTC_COMO_DEVE_SAIR)
        texto = "\n".join(avisos)
        self.assertNotIn("nao ha marcador", texto.lower())
        self.assertNotIn("numeracao dos artigos salta", texto)

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
