"""Testes para o módulo de análise textual (stopwords, conectivos, limpeza)."""

import pytest

from agente_editais.texto_analise import (
    CONECTIVOS_MULTIWORD,
    CONECTIVOS_VERSAO,
    STOPWORDS_PT,
    STOPWORDS_VERSAO,
    TOKEN_PATTERN,
    info_versao,
    limpar_texto,
    limpar_textos,
)


class TestStopwordsVersao:
    def test_stopwords_versao_existe(self):
        assert isinstance(STOPWORDS_VERSAO, int)
        assert STOPWORDS_VERSAO >= 2

    def test_conectivos_versao_existe(self):
        assert isinstance(CONECTIVOS_VERSAO, int)
        assert CONECTIVOS_VERSAO >= 1


class TestStopwordsConteudo:
    # Stopwords base (herdadas do dashboard original)
    @pytest.mark.parametrize("sw", [
        "a", "o", "as", "os", "e", "em", "de", "da", "do", "das", "dos",
        "para", "por", "com", "sem", "que", "se", "não", "mais", "menos",
        "este", "esta", "esses", "essas", "aquele", "aquela", "isto", "isso",
        "sua", "seu", "suas", "seus", "dele", "dela", "deles", "delas",
    ])
    def test_stopwords_base_presentes(self, sw):
        assert sw in STOPWORDS_PT

    # Preposições expandidas
    @pytest.mark.parametrize("prep", [
        "junto", "conforme", "segundo", "consoante", "mediante",
        "exceto", "salvo", "acerca", "relativo",
    ])
    def test_preposicoes_expandidas(self, prep):
        assert prep in STOPWORDS_PT

    # Conjunções/advérbios conectivos
    @pytest.mark.parametrize("conn", [
        "assim", "então", "entretanto", "portanto", "outrossim",
        "igualmente", "contudo", "apesar",
    ])
    def test_conjuncoes_conectivos(self, conn):
        assert conn in STOPWORDS_PT

    # Verbos auxiliares e formas verbais frequentes
    @pytest.mark.parametrize("verb", [
        "deve", "devem", "será", "ser", "estar", "estão",
        "pode", "podendo", "realizado", "realizar",
        "objetiva", "atuar", "atuarem", "torna",
    ])
    def test_verbos_formas_verbais(self, verb):
        assert verb in STOPWORDS_PT

    # Boilerplate de edital
    @pytest.mark.parametrize("bp", [
        "processo", "seletivo", "inscrição", "candidato",
        "documento", "documentação", "prazo", "edital",
        "chamada", "curso", "público", "presente", "anexo",
    ])
    def test_boilerplate_edital(self, bp):
        assert bp in STOPWORDS_PT

    # Termos de interesse NÃO devem estar nas stopwords
    @pytest.mark.parametrize("keep", [
        "inovação", "bolsista", "política", "projeto", "programa",
        "ação", "atividade", "meta", "indicador", "território",
        "sustentabilidade", "inclusão", "governança", "rede",
        "comunidade", "empresa", "parceria", "impacto", "social",
        "desenvolvimento", "regional", "tecnologia", "transferência",
        "propriedade", "intelectual", "licenciamento", "coprodução",
    ])
    def test_termos_interesse_nao_removidos(self, keep):
        assert keep not in STOPWORDS_PT


class TestConectivosMultiword:
    def test_conectivos_lista_nao_vazia(self):
        assert len(CONECTIVOS_MULTIWORD) > 0

    @pytest.mark.parametrize("mw", [
        "bem como", "por meio de", "no âmbito", "a fim de",
        "de acordo com", "através de", "além de", "tendo em vista",
        "com base em", "com relação a",
    ])
    def test_conectivos_principais(self, mw):
        assert mw in CONECTIVOS_MULTIWORD


class TestLimparTexto:
    def test_remove_conectivo_bem_como(self):
        txt = "O edital prevê inovação bem como extensão tecnológica"
        limpo = limpar_texto(txt)
        assert "bem como" not in limpo
        assert "inovação" in limpo
        assert "extensão" in limpo

    def test_remove_multiplos_conectivos(self):
        txt = "Por meio de parcerias, no âmbito do programa, a fim de fomentar inovação."
        limpo = limpar_texto(txt)
        assert "por meio de" not in limpo
        assert "no âmbito" not in limpo
        assert "a fim de" not in limpo
        assert "inovação" in limpo

    def test_case_insensitive(self):
        txt = "BEM COMO Por Meio De No Âmbito"
        limpo = limpar_texto(txt)
        assert "bem como" not in limpo
        assert "por meio de" not in limpo
        assert "no âmbito" not in limpo

    def test_normaliza_espacos(self):
        txt = "inovação   bem como    extensão"
        limpo = limpar_texto(txt)
        assert limpo == "inovação extensão"

    def test_minusculas(self):
        txt = "INOVAÇÃO BEM COMO EXTENSÃO"
        limpo = limpar_texto(txt)
        assert limpo == "inovação extensão"

    def test_texto_vazio(self):
        assert limpar_texto("") == ""
        assert limpar_texto("   ") == ""

    def test_mantem_termos_interesse(self):
        txt = "bolsista de inovação atua no projeto de política tecnológica"
        limpo = limpar_texto(txt)
        for termo in ["bolsista", "inovação", "projeto", "política", "tecnológica"]:
            assert termo in limpo


class TestLimparTextos:
    def test_dict_input(self):
        docs = {"a": "inovação bem como extensão", "b": "por meio de parceria"}
        limpos = limpar_textos(docs)
        assert "bem como" not in limpos["a"]
        assert "por meio de" not in limpos["b"]
        assert "inovação" in limpos["a"]
        assert "parceria" in limpos["b"]


class TestTokenPattern:
    def test_token_pattern_existe(self):
        assert TOKEN_PATTERN == r"(?u)\b[^\W\d_]\w+\b"

    def test_token_pattern_exclui_numeros(self):
        import re
        pattern = re.compile(TOKEN_PATTERN)
        assert pattern.match("inovação") is not None
        assert pattern.match("2025") is None
        assert pattern.match("123") is None


class TestInfoVersao:
    def test_retorna_dict_completo(self):
        info = info_versao()
        assert "stopwords_versao" in info
        assert "conectivos_versao" in info
        assert "n_stopwords" in info
        assert "n_conectivos_multiword" in info
        assert info["stopwords_versao"] == STOPWORDS_VERSAO
        assert info["conectivos_versao"] == CONECTIVOS_VERSAO
        assert info["n_stopwords"] == len(STOPWORDS_PT)
        assert info["n_conectivos_multiword"] == len(CONECTIVOS_MULTIWORD)


class TestIntegracaoContagemTermos:
    """Teste de integração simulando _contagem_termos com a nova limpeza."""

    def test_conectivo_nao_aparece_como_termo(self):
        from sklearn.feature_extraction.text import CountVectorizer

        from agente_editais.texto_analise import STOPWORDS_PT, TOKEN_PATTERN, limpar_texto

        textos = {
            "d1": "inovação bem como extensão tecnológica",
            "d2": "bolsista atua por meio de projeto inovador",
        }
        docs_limpos = [limpar_texto(t) for t in textos.values()]

        vec = CountVectorizer(
            ngram_range=(1, 1),
            stop_words=list(STOPWORDS_PT),
            token_pattern=TOKEN_PATTERN,
            lowercase=True,
        )
        vec.fit_transform(docs_limpos)
        termos = vec.get_feature_names_out()

        # Conectivos multiword não devem aparecer como termos
        assert "bem como" not in termos
        assert "por meio de" not in termos
        # Termos de interesse devem aparecer
        assert "inovação" in termos
        assert "extensão" in termos
        assert "bolsista" in termos
        assert "projeto" in termos


class TestAmostraPDFsAnexados:
    """Validação manual com trechos dos PDFs anexados (IFBA PRPGI)."""

    def test_edital_disposicoes_preliminares(self):
        # Trecho extraído do edital-no-09-2026-prpgi-ifba.pdf (seção 1)
        trecho = (
            "o processo seletivo objetiva o preenchimento de vagas "
            "para atuação como bolsistas para atuarem junto as ações "
            "do programa hotel de projetos tecnológicos bem como do "
            "programa empreendedorismo inovador"
        )
        limpo = limpar_texto(trecho)
        # Conectivos removidos pelo limpar_texto
        assert "bem como" not in limpo
        # Termos-chave mantidos pelo limpar_texto
        assert "bolsistas" in limpo or "bolsista" in limpo
        assert "ações" in limpo
        assert "projetos" in limpo
        assert "tecnológicos" in limpo
        assert "empreendedorismo" in limpo
        assert "inovador" in limpo

    def test_chamada_disposicoes_preliminares(self):
        # Trecho extraído da chamada-no-12-2026-prpgi-ifba-...pdf (seção 1)
        trecho = (
            "esta chamada interna simplificada será conduzida pela "
            "pró-reitoria de pós-graduação pesquisa e inovação "
            "através do departamento de inovação"
        )
        limpo = limpar_texto(trecho)
        assert "através de" not in limpo
        # Termos de interesse mantidos
        assert "inovação" in limpo
        assert "departamento" in limpo
        assert "pró-reitoria" in limpo or "pós-graduação" in limpo

    def test_pipeline_completo_remove_stopwords_e_conectivos(self):
        """Teste de integração: limpar_texto + CountVectorizer remove stopwords e conectivos."""
        from sklearn.feature_extraction.text import CountVectorizer

        from agente_editais.texto_analise import STOPWORDS_PT, TOKEN_PATTERN, limpar_texto

        trecho = (
            "o processo seletivo objetiva o preenchimento de vagas "
            "para atuação como bolsistas para atuarem junto as ações "
            "do programa hotel de projetos tecnológicos bem como do "
            "programa empreendedorismo inovador"
        )
        limpo = limpar_texto(trecho)
        docs = [limpo]

        vec = CountVectorizer(
            ngram_range=(1, 1),
            stop_words=list(STOPWORDS_PT),
            token_pattern=TOKEN_PATTERN,
            lowercase=True,
        )
        vec.fit_transform(docs)
        termos = set(vec.get_feature_names_out())

        # Stopwords removidas pelo CountVectorizer
        assert "processo" not in termos
        assert "seletivo" not in termos
        assert "objetiva" not in termos
        assert "vagas" not in termos
        assert "atuarem" not in termos
        assert "junto" not in termos

        # Conectivos multiword já removidos pelo limpar_texto
        assert "bem como" not in termos

        # Termos analíticos mantidos
        assert "bolsistas" in termos or "bolsista" in termos
        assert "ações" in termos
        assert "projetos" in termos
        assert "tecnológicos" in termos
        assert "empreendedorismo" in termos
        assert "inovador" in termos


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
