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
