"""Cenários do I/O Matrix para a análise L2 (CAP-7): gates bloqueantes,
instrumento congelado por lote, verificação citação↔texto e retomada.

Nenhum teste acessa a rede real: o ponto ÚNICO de rede do estágio
(``llm_adapter.concluir``) é monkeypatched com recorder — padrão
``playwright_falso`` da suíte de descoberta. Codebooks de teste são gerados
em tmp_path (congelado e não-congelado); os Documentos nascem da pipeline
REAL contra o servidor fake local (coletar → textuar).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agente_editais import llm_adapter
from agente_editais.analise import (
    SaidaInvalida,
    montar_prompt_sistema,
    validar_saida,
    verificar_citacao,
)
from agente_editais.codebook import Codebook, ErroCodebook, carregar_codebook
from agente_editais.consulta import app
from agente_editais.manifest import Manifesto

from .conftest import (
    CONFIGS_DO_REPO,
    coletar_pdfs,
    documentos_do_manifesto,
    longo,
    mapa_portal,
    pdf_apenas_imagem,
    pdf_com_texto,
    escrever_mapa,
    saida_cli,
    tipos_eventos,
    url_do,
)


# -- infra local: codebooks de teste e adaptador falso ------------------------------


_CODEBOOK = """\
schema_version: 1
precedencia_intra_edital: "Cronologia de captura: captura posterior prevalece."
dimensoes:
  - id: dimensao_exemplo
    nome: "Dimensão de teste"
    definicao_operacional: "Agrupa os campos de teste do codebook mínimo."
    campos:
      - id: grau_exemplo
        nome: "Grau exemplo"
        escala: ordinal
        valores: [0, 1, 2]
        permite_na: true
        definicao_operacional: "Grau de teste: 0 ausente, 1 menção, 2 mecanismo."
        regra_decisao: "Codifique o grau máximo encontrado no edital."
        ancoras_positivas: ["ancora positiva de teste"]
        ancoras_negativas: ["ancora negativa de teste"]
        regra_boilerplate: "Cláusula padrão replicada não conta sozinha."
      - id: tipo_exemplo
        nome: "Tipo exemplo"
        escala: nominal
        opcoes: [categoria_a, categoria_b]
        permite_na: true
        definicao_operacional: "Categoria dominante de teste."
        regra_decisao: "Escolha UMA categoria com base nas âncoras."
        ancoras_positivas: ["ancora categoria_a de teste"]
        ancoras_negativas: ["ausência total de base textual"]
        regra_boilerplate: "Definição legal copiada não decide sozinha."
congelamento:
  congelado_em:{congelado_em}
  congelado_por:{congelado_por}
trietica_dahlin:
  resolvida:{resolvida}
  decisao:{decisao}
  justificativa:{justificativa}
acordo_humano_maquina:
  limiar_kappa:{kappa}
"""


def escrever_codebook(ambiente: SimpleNamespace, *, congelado: bool) -> Path:
    """Grava um codebook válido em tmp; ``congelado`` liga os três gates.

    Estados coerentes com os validadores estritos: resolvida=true exige
    decisao no enum (incorporada dispensa justificativa); resolvida=false
    exige justificativa registrando a pendência.
    """
    if congelado:
        preenchidos = {
            "congelado_em": ' "2026-08-25T10:00:00-03:00"',
            "congelado_por": ' "Pesquisadora Teste"',
            "resolvida": " true",
            "decisao": " incorporada",
            "justificativa": "",
            "kappa": " 0.75",
        }
    else:
        preenchidos = {
            "congelado_em": "",
            "congelado_por": "",
            "resolvida": " false",
            "decisao": "",
            "justificativa": " Pendente de decisão com o(a) orientador(a) (OQ-6).",
            "kappa": "",
        }
    caminho = ambiente.configs / "codebook.yaml"
    caminho.write_text(_CODEBOOK.format(**preenchidos), encoding="utf-8")
    return caminho


@pytest.fixture
def adaptador_falso(monkeypatch):
    """Substitui o ponto único de rede do estágio; recorder das chamadas.

    Define credenciais DUMMY do provedor — o comando ``analise`` faz preflight
    de env ANTES de abrir banco/lote, então todo fluxo feliz precisa delas
    definidas mesmo com ``concluir`` falsificado. ``estado.respostas`` é uma
    fila: str devolvido como saída; Exception levantada (simula provedor
    caindo). Sem resposta enfileirada ⇒ falha de teste, nunca sucesso
    silencioso.
    """
    monkeypatch.setenv("LLM_BASE_URL", "https://llm-falso.teste/v1")
    monkeypatch.setenv("LLM_API_KEY", "chave-de-teste")
    estado = SimpleNamespace(chamadas=[], respostas=[])

    def _falso(sistema: str, usuario: str) -> str:
        estado.chamadas.append({"sistema": sistema, "usuario": usuario})
        if not estado.respostas:
            raise AssertionError("adaptador falso sem resposta enfileirada")
        proxima = estado.respostas.pop(0)
        if isinstance(proxima, Exception):
            raise proxima
        if callable(proxima):
            return proxima(usuario)
        return proxima

    monkeypatch.setattr(llm_adapter, "concluir", _falso)
    return estado


# -- leitores auxiliares ------------------------------------------------------------


def catalogo_do(caminho_manifesto: Path) -> list[dict]:
    with Manifesto(caminho_manifesto) as manifesto:
        return [
            dict(linha)
            for linha in manifesto.consultar("SELECT * FROM catalogo_l2 ORDER BY campo")
        ]


def lotes_do(caminho_manifesto: Path) -> list[dict]:
    with Manifesto(caminho_manifesto) as manifesto:
        return [dict(linha) for linha in manifesto.consultar("SELECT * FROM lotes_l2")]


def _ids_da_entrada(usuario: str) -> list[str]:
    return [
        linha.split()[2]
        for linha in usuario.splitlines()
        if linha.startswith("=== DOCUMENTO ")
    ]


def _trecho_literal(texto: str) -> str:
    return " ".join(texto.split()[:4])


def _textos_por_id(caminho_manifesto: Path) -> dict[str, str]:
    textos: dict[str, str] = {}
    for documento in documentos_do_manifesto(caminho_manifesto):
        if documento["texto_caminho"]:
            textos[documento["id"]] = Path(documento["texto_caminho"]).read_text(
                encoding="utf-8"
            )
    return textos


def _resposta_ok(usuario: str, textos_por_id: dict[str, str]) -> str:
    """Saída válida citando o PRIMEIRO documento da entrada com trecho real."""
    ids = _ids_da_entrada(usuario)
    primeiro = ids[0]
    trecho = _trecho_literal(textos_por_id[primeiro])
    return json.dumps(
        {
            "campos": {
                "grau_exemplo": {
                    "valor": 2,
                    "citacao_documento": primeiro,
                    "citacao_trecho": trecho,
                },
                "tipo_exemplo": {
                    "valor": "N/A",
                    "citacao_documento": None,
                    "citacao_trecho": None,
                },
            }
        }
    )


def preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus):
    """Pipeline REAL: UM edital com DOIS documentos nativos datáveis."""
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/docs/a/edital-x.pdf": pdf_com_texto([longo("Primeiro documento nativo do edital")]),
            "/docs/b/edital-x.pdf": pdf_com_texto([longo("Segundo documento nativo do mesmo edital")]),
        },
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0


def preparar_corpus_dois_editais(cli, politeness_veloz, servidor_fake, corpus):
    """Pipeline REAL: DOIS editais distintos, cada um com um documento."""
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/docs/a/edital-x.pdf": pdf_com_texto([longo("Documento do primeiro edital")]),
            "/docs/b/edital-y.pdf": pdf_com_texto([longo("Documento do segundo edital")]),
        },
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0


def excluir_edital_inteiro(caminho_manifesto: Path, url: str) -> None:
    """Decisão humana VIGENTE de exclusão sobre todos os docs da url."""
    with Manifesto(caminho_manifesto) as manifesto:
        portal_id = manifesto.consultar("SELECT MIN(id) AS i FROM portais")[0]["i"]
        assert manifesto.enfileirar(url, portal_id, "sem_data") is True
        item = manifesto.consultar(
            "SELECT id FROM fila_revisao WHERE url_origem = ?", (url,)
        )[0]
        assert (
            manifesto.registrar_decisao_fila(
                int(item["id"]),
                decidido_ano=None,
                decidido_exclusao=True,
                justificativa="Não é edital.",
                autor="Pesquisadora",
            )
            is True
        )


# -- Cenário "Gates fechados": exit 2 nomeando o gate, ZERO chamadas ---------------


def test_gates_fechados_exit_2_sem_nenhuma_chamada_ao_provedor(
    cli, politeness_veloz, servidor_fake, adaptador_falso
):
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    escrever_codebook(politeness_veloz, congelado=False)

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "modelo-x"])

    assert resultado.exit_code == 2
    saida = saida_cli(resultado)
    assert "congelamento" in saida
    assert "trietica_dahlin" in saida
    assert "limiar_kappa" in saida
    assert adaptador_falso.chamadas == [], "gate pendente ⇒ ZERO chamadas ao provedor"
    assert lotes_do(politeness_veloz.manifesto) == []


def test_gate_de_congelamento_resolvido_sai_da_mensagem_mas_outros_seguram(
    cli, politeness_veloz, servidor_fake, adaptador_falso
):
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    caminho_codebook = escrever_codebook(politeness_veloz, congelado=False)
    # congelamento completo, Dahlin e κ continuam pendentes
    texto = caminho_codebook.read_text(encoding="utf-8")
    texto = texto.replace('congelado_em:', 'congelado_em: "2026-08-25T10:00:00-03:00"')
    texto = texto.replace('congelado_por:', 'congelado_por: "Pesquisadora Teste"')
    caminho_codebook.write_text(texto, encoding="utf-8")

    resultado = cli.invoke(app, ["analise", "--portal", "TST", "--modelo", "m"])

    assert resultado.exit_code == 2
    saida = saida_cli(resultado)
    assert "gate 'congelamento'" not in saida, "gate resolvido sai da lista"
    assert "trietica_dahlin" in saida and "limiar_kappa" in saida
    assert adaptador_falso.chamadas == []


def test_flags_mutuamente_exclusivas_e_modelo_ausente_no_analise(
    cli, politeness_veloz, servidor_fake, adaptador_falso, monkeypatch
):
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    escrever_codebook(politeness_veloz, congelado=True)
    assert cli.invoke(app, ["analise"]).exit_code == 2
    assert cli.invoke(app, ["analise", "--portal", "TST", "--todos"]).exit_code == 2
    assert cli.invoke(app, ["analise", "--portal", ""]).exit_code == 2
    # mapa e codebook ok, mas sem --modelo e sem LLM_MODELO ⇒ exit 2 antes do banco
    monkeypatch.delenv("LLM_MODELO", raising=False)
    resultado = cli.invoke(app, ["analise", "--portal", "TST"])
    assert resultado.exit_code == 2
    assert "--modelo" in saida_cli(resultado)
    assert adaptador_falso.chamadas == []


# -- Preflight de credenciais (fail-fast): exit 2 ANTES do banco/lote ----------------


def test_credenciais_provedor_ausentes_exit_2_antes_do_banco(
    cli, politeness_veloz, servidor_fake, monkeypatch
):
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    escrever_codebook(politeness_veloz, congelado=True)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 2
    saida = saida_cli(resultado)
    assert "LLM_BASE_URL" in saida and "LLM_API_KEY" in saida
    # gates passaram; o preflight recusou ANTES de abrir banco/lote/provedor
    assert lotes_do(politeness_veloz.manifesto) == []
    eventos = tipos_eventos(politeness_veloz.manifesto)
    assert all(tipo != "analise_concluido" for tipo, _ in eventos)


def test_credencial_parcial_tambem_recusa_exit_2(
    cli, politeness_veloz, servidor_fake, adaptador_falso, monkeypatch
):
    """Só uma das duas env definidas ⇒ exit 2 nomeando a FALTANTE."""
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    escrever_codebook(politeness_veloz, congelado=True)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 2
    saida = saida_cli(resultado)
    # a mensagem nomeia APENAS a env faltante
    assert "variável de ambiente 'LLM_API_KEY'" in saida
    assert "variável de ambiente 'LLM_BASE_URL'" not in saida
    assert adaptador_falso.chamadas == []


# -- Cenário "Codificação feliz": catálogo gravado com citações ok ------------------


def test_codificacao_feliz_grava_catalogo_e_lote_com_assinatura(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)

    def _resposta_dinamica(usuario: str) -> str:
        return _resposta_ok(usuario, textos)

    adaptador_falso.respostas = [_resposta_dinamica]

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "fronteira-1"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "codificados: 1" in saida

    linhas = catalogo_do(politeness_veloz.manifesto)
    assert len(linhas) == 2, "um campo do codebook ⇒ uma linha"
    por_campo = {linha["campo"]: linha for linha in linhas}
    grau = por_campo["grau_exemplo"]
    assert grau["valor"] == "2"
    assert grau["verificacao"] == "ok"
    assert grau["citacao_pagina"] == 1
    assert grau["documento_id"] in textos, "FK composta aponta p/ doc da entrada"
    na = por_campo["tipo_exemplo"]
    assert na["valor"] == "N/A" and na["verificacao"] == "ok"
    assert na["documento_id"] is None and na["citacao_trecho"] is None

    (lote,) = lotes_do(politeness_veloz.manifesto)
    assert lote["modelo"] == "fronteira-1"
    assert lote["temperatura"] == 0.0
    assert lote["seed"] is None
    assert len(lote["codebook_sha256"]) == 64
    assert len(lote["prompt_sha256"]) == 64
    assert lote["prompt_versao"]
    assert lote["versao_agente"]
    assert lote["schema_version"] == 6
    assert lote["status"] == "concluido"

    eventos = tipos_eventos(politeness_veloz.manifesto)
    tipos = [tipo for tipo, _ in eventos]
    assert tipos.count("analise_aplicada") == 1
    aplicada = next(d for t, d in eventos if t == "analise_aplicada")
    assert aplicada["campos_total"] == 2
    assert aplicada["campos_ok"] == 2
    assert aplicada["campos_invalidados"] == 0
    assert aplicada["documentos_entrada"] == 2, "os dois docs alimentam UMA chamada"
    assert tipos.count("analise_portal_concluida") == 1
    assert tipos.count("analise_concluido") == 1


# -- Cenário "Saída inválida ao esquema": reprocessa, JAMAIS grava ------------------


def test_saida_invalida_reprocessa_e_nao_grava_nada_lote_segue_exit0(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_dois_editais(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)

    invalidas = ['{"campos": {}}', '{"campos": {"grau_exemplo": {"valor": 9}}}']
    adaptador_falso.respostas.extend(invalidas)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(
        app, ["analise", "--todos", "--modelo", "m", "--tentativas", "2"]
    )

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "inválidos: 1" in saida
    assert "codificados: 1" in saida, "o lote segue para o próximo edital"

    linhas = catalogo_do(politeness_veloz.manifesto)
    codificado = {linha["edital_id"] for linha in linhas}
    assert len(codificado) == 1, "apenas o edital com saída válida tem catálogo"

    eventos = tipos_eventos(politeness_veloz.manifesto)
    invalida = [d for t, d in eventos if t == "analise_invalida"]
    assert len(invalida) == 1, "o evento registra a invalidade"
    assert invalida[0]["motivo"] == "esquema", "distingue o motivo da exaustão"
    assert invalida[0]["tentativas"] == 2
    assert invalida[0]["problema"]


def test_saida_invalida_una_vez_e_valida_na_segunda_grava_na_retomada_da_chamada(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append("desculpe, nao sei responder em JSON")
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    eventos = tipos_eventos(politeness_veloz.manifesto)
    aplicada = next(d for t, d in eventos if t == "analise_aplicada")
    assert aplicada["tentativas_usadas"] == 2, "reprocessada até --tentativas"
    assert "analise_invalida" not in {t for t, _ in eventos}
    assert len(adaptador_falso.chamadas) == 2
    assert len(catalogo_do(politeness_veloz.manifesto)) == 2


# -- Cenário "Citação inexistente": campo gravado como citacao_invalidada -----------


def test_citacao_fabricada_vira_citacao_invalidada_fora_dos_validos(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    primeiro = sorted(textos)[0]

    def _resposta(usuario: str) -> str:
        ids = _ids_da_entrada(usuario)
        return json.dumps(
            {
                "campos": {
                    "grau_exemplo": {
                        "valor": 1,
                        "citacao_documento": ids[0],
                        "citacao_trecho": "trecho fabricado que jamais existiu no texto",
                    },
                    "tipo_exemplo": {
                        "valor": "categoria_a",
                        "citacao_documento": ids[0],
                        "citacao_trecho": _trecho_literal(textos[primeiro]),
                    },
                }
            }
        )

    adaptador_falso.respostas.append(_resposta)

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    por_campo = {
        linha["campo"]: linha for linha in catalogo_do(politeness_veloz.manifesto)
    }
    fabricada = por_campo["grau_exemplo"]
    assert fabricada["verificacao"] == "citacao_invalidada"
    assert fabricada["citacao_pagina"] is None
    assert fabricada["citacao_trecho"] == (
        "trecho fabricado que jamais existiu no texto"
    ), "custódia preservada"
    valida = por_campo["tipo_exemplo"]
    assert valida["verificacao"] == "ok"

    eventos = tipos_eventos(politeness_veloz.manifesto)
    aplicada = next(d for t, d in eventos if t == "analise_aplicada")
    assert aplicada["campos_ok"] == 1, "invalidada fica FORA dos válidos"
    assert aplicada["campos_invalidados"] == 1


# -- Cenário "Documento escaneado": pulado com evento; integral ⇒ erro dedicado -----


def test_documento_escaneado_e_pulado_sem_ocr_e_nao_entra_na_chamada(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/docs/a/edital-x.pdf": pdf_apenas_imagem(paginas=2),
            "/docs/b/edital-x.pdf": pdf_com_texto([longo("Nativo que sustenta a analise")]),
        },
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "escaneados pulados (sem OCR): 1" in saida_cli(resultado)
    eventos = tipos_eventos(politeness_veloz.manifesto)
    escaneados = [d for t, d in eventos if t == "analise_escaneado_sem_ocr"]
    assert len(escaneados) == 1
    ids_na_chamada = _ids_da_entrada(adaptador_falso.chamadas[0]["usuario"])
    assert len(ids_na_chamada) == 1, "escaneado NÃO alimenta a chamada"
    assert all(doc["flag_escaneado"] == 0 or doc["id"] not in ids_na_chamada
               for doc in documentos_do_manifesto(politeness_veloz.manifesto))


def test_edital_integralmente_escaneado_vira_erro_dedicado_sem_chamada(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {"/scan/antigo.pdf": pdf_apenas_imagem(paginas=3)},
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0
    escrever_codebook(politeness_veloz, congelado=True)

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "erros: 1" in saida
    # contador HONESTO: o early-return do edital integral conta os escaneados
    assert "escaneados pulados (sem OCR): 1" in saida
    assert adaptador_falso.chamadas == [], "nada é enviado ao provedor"
    eventos = tipos_eventos(politeness_veloz.manifesto)
    erro = [d for t, d in eventos if t == "analise_erro"]
    assert len(erro) == 1
    assert erro[0]["motivo"] == "edital_escaneado_sem_ocr"
    portal_concluida = next(
        d for t, d in eventos if t == "analise_portal_concluida"
    )
    assert portal_concluida["escaneados_sem_ocr"] == 1


# -- Cenário "Retomada idempotente" e "Troca de instrumento" -------------------------


def test_mesma_assinatura_continua_o_lote_e_pula_editais_codificados(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))
    assert cli.invoke(app, ["analise", "--todos", "--modelo", "m"]).exit_code == 0
    linhas_antes = catalogo_do(politeness_veloz.manifesto)

    segunda = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert segunda.exit_code == 0, saida_cli(segunda)
    assert "já codificados: 1" in saida_cli(segunda)
    assert "codificados: 0" in saida_cli(segunda)
    assert len(adaptador_falso.chamadas) == 1, "nenhuma nova chamada ao provedor"
    assert catalogo_do(politeness_veloz.manifesto) == linhas_antes, "nada duplica"
    assert len(lotes_do(politeness_veloz.manifesto)) == 1, "mesmo lote continua"
    concluido = [
        d for t, d in tipos_eventos(politeness_veloz.manifesto)
        if t == "analise_concluido"
    ][-1]
    assert concluido["totais"]["pulados_ja_codificados"] == 1


def test_lote_concluido_eh_reaberto_e_reconcluido_na_retomada(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    """Reuso HONESTO: lote concluído com a mesma assinatura é reaberto
    (status='aberto', concluido_em=NULL) e só re-concluído no fim — a
    delimitação nunca mente sobre conclusão enquanto o lote está em uso."""
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))
    assert cli.invoke(app, ["analise", "--todos", "--modelo", "m"]).exit_code == 0

    (lote_primeiro,) = lotes_do(politeness_veloz.manifesto)
    assert lote_primeiro["status"] == "concluido"
    assert lote_primeiro["concluido_em"] is not None

    segunda = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])
    assert segunda.exit_code == 0, saida_cli(segunda)

    (lote_segundo,) = lotes_do(politeness_veloz.manifesto)
    assert int(lote_segundo["id"]) == int(lote_primeiro["id"]), "MESMO lote reaberto"
    assert lote_segundo["aberto_em"] == lote_primeiro["aberto_em"], (
        "abertura original preservada"
    )
    assert lote_segundo["status"] == "concluido", "re-concluído no fim da rodada"
    assert lote_segundo["concluido_em"] is not None
    assert lote_segundo["concluido_em"] >= lote_primeiro["concluido_em"]
    # durante a segunda rodada o lote esteve aberto (evento lote_l2_continuado)
    continuado = [
        d for t, d in tipos_eventos(politeness_veloz.manifesto)
        if t == "lote_l2_continuado"
    ]
    assert continuado and continuado[-1]["status"] == "aberto"


def test_instrumento_alterado_abre_novo_lote_com_linhas_proprias(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))
    assert cli.invoke(app, ["analise", "--todos", "--modelo", "m"]).exit_code == 0

    segunda = cli.invoke(
        app, ["analise", "--todos", "--modelo", "m", "--temperatura", "0.7"]
    )

    assert segunda.exit_code == 0, saida_cli(segunda)
    lotes = lotes_do(politeness_veloz.manifesto)
    assert len(lotes) == 2, "componente diferente ⇒ NOVO lote"
    temperaturas = sorted(lote["temperatura"] for lote in lotes)
    assert temperaturas == [0.0, 0.7]
    linhas = catalogo_do(politeness_veloz.manifesto)
    por_lote = {linha["lote_id"] for linha in linhas}
    assert len(por_lote) == 2, "linhas delimitadas por lote_id"
    assert "codificados: 1" in saida_cli(segunda), "novo lote recodifica o edital"


# -- Cenário "Provedor cai": retry cobre blips; exaustão vira erro dedicado ----------


def test_blip_do_provedor_e_coberto_pelo_orcamento_de_tentativas(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    """Falha do provedor consome o MESMO orçamento --tentativas: um blip HTTP
    seguido de resposta válida codifica o edital normalmente."""
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(llm_adapter.ErroProvedorLLM("HTTP 503"))
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "codificados: 1" in saida_cli(resultado)
    assert len(adaptador_falso.chamadas) == 2
    aplicada = next(
        d for t, d in tipos_eventos(politeness_veloz.manifesto)
        if t == "analise_aplicada"
    )
    assert aplicada["tentativas_usadas"] == 2


def test_provedor_cai_em_todas_as_tentativas_evento_distingue_o_motivo(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_dois_editais(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    # edital A esgota as 2 tentativas com falhas do provedor; B codifica
    adaptador_falso.respostas.append(llm_adapter.ErroProvedorLLM("HTTP 503"))
    adaptador_falso.respostas.append(llm_adapter.ErroProvedorLLM("HTTP 503"))
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "erros: 1" in saida
    assert "codificados: 1" in saida, "o lote segue para o próximo edital"

    eventos = tipos_eventos(politeness_veloz.manifesto)
    erros = [d for t, d in eventos if t == "analise_erro"]
    de_provedor = [erro for erro in erros if erro.get("motivo") == "provedor"]
    assert len(de_provedor) == 1, "exaustão por provedor vira UM evento dedicado"
    assert de_provedor[0]["tentativas"] == 2
    assert "HTTP 503" in de_provedor[0]["erro"]
    invalidos = [d for t, d in eventos if t == "analise_invalida"]
    assert all(inv["motivo"] == "esquema" for inv in invalidos), (
        "evento de esquema não é usado para falha de provedor"
    )
    assert len(adaptador_falso.chamadas) == 3


# -- Cenário "Edital excluído": fora do lote, contado no resumo ----------------------


def test_edital_com_exclusao_vigente_fica_fora_do_lote_sem_chamada(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/docs/a/edital-x.pdf": pdf_com_texto([longo("Edital excluido pelo humano")]),
            "/docs/b/edital-y.pdf": pdf_com_texto([longo("Edital que segue no lote")]),
        },
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0
    url_x = next(
        doc["url_origem"]
        for doc in documentos_do_manifesto(politeness_veloz.manifesto)
        if doc["url_origem"].endswith("edital-x.pdf")
    )
    excluir_edital_inteiro(politeness_veloz.manifesto, url_x)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "excluídos: 1" in saida
    assert "codificados: 1" in saida
    assert len(adaptador_falso.chamadas) == 1
    ids = _ids_da_entrada(adaptador_falso.chamadas[0]["usuario"])
    assert len(ids) == 1, "excluído não entra na entrada"
    editais_codificados = {linha["edital_id"] for linha in catalogo_do(politeness_veloz.manifesto)}
    assert len(editais_codificados) == 1
    concluido = next(
        d for t, d in tipos_eventos(politeness_veloz.manifesto)
        if t == "analise_concluido"
    )
    assert concluido["totais"]["excluidos"] == 1


# -- Flags: seed fora do INTEGER do SQLite e teto de tentativas -----------------------


def test_seed_fora_do_intervalo_integer_do_sqlite_exit_2(
    cli, politeness_veloz, servidor_fake, adaptador_falso
):
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    escrever_codebook(politeness_veloz, congelado=True)

    resultado = cli.invoke(
        app, ["analise", "--todos", "--modelo", "m", "--seed", str(2**63)]
    )

    assert resultado.exit_code == 2
    saida = saida_cli(resultado)
    assert "INTEGER do SQLite" in saida
    assert adaptador_falso.chamadas == []
    assert lotes_do(politeness_veloz.manifesto) == [], "recusa antes de abrir o lote"


def test_seed_no_limite_do_intervalo_e_aceita(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(
        app,
        ["analise", "--todos", "--modelo", "m", "--seed", str(2**63 - 1)],
    )

    assert resultado.exit_code == 0, saida_cli(resultado)
    (lote,) = lotes_do(politeness_veloz.manifesto)
    assert lote["seed"] == 2**63 - 1


def test_tentativas_acima_do_teto_sao_recusadas_pela_flag(cli, politeness_veloz) -> None:
    resultado = cli.invoke(
        app, ["analise", "--todos", "--modelo", "m", "--tentativas", "11"]
    )
    assert resultado.exit_code == 2, "typer recusa max=10 antes de qualquer I/O"


# -- Lacunas de verificação: ordem cronológica, instrumento por env, vigência --------


def test_entrada_respeita_ordem_cronologica_de_captura(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    """FR-17 fixado por teste: seções `=== DOCUMENTO` aparecem oldest-first
    no payload, mesmo que o rowid (ordem de inserção) diga o contrário."""
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/docs/a/edital-x.pdf": pdf_com_texto([longo("Documento capturado primeiro")]),
            "/docs/b/edital-x.pdf": pdf_com_texto([longo("Documento capturado depois")]),
        },
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0

    # capturas distintas e DETERMINÍSTICAS: A mais antiga que B
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        manifesto.executar(
            "UPDATE documentos SET data_captura = '2026-01-02T00:00:00+00:00' "
            "WHERE url_origem LIKE '%/docs/a/%'"
        )
        manifesto.executar(
            "UPDATE documentos SET data_captura = '2026-01-05T00:00:00+00:00' "
            "WHERE url_origem LIKE '%/docs/b/%'"
        )
        capturas = {
            linha["id"]: linha["data_captura"]
            for linha in manifesto.consultar("SELECT id, data_captura FROM documentos")
        }
    assert len(set(capturas.values())) == 2, "guarda: capturas devem ser distintas"

    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])
    assert resultado.exit_code == 0, saida_cli(resultado)

    usuario = adaptador_falso.chamadas[0]["usuario"]
    ids_na_ordem = _ids_da_entrada(usuario)
    mais_antigo = min(capturas, key=lambda did: capturas[did])
    mais_recente = max(capturas, key=lambda did: capturas[did])
    assert ids_na_ordem[0] == mais_antigo, "oldest-first na entrada"
    assert ids_na_ordem[-1] == mais_recente
    assert usuario.index(f"=== DOCUMENTO {mais_antigo} ===") < usuario.index(
        f"=== DOCUMENTO {mais_recente} ==="
    )


def test_instrumento_por_env_llm_modelo_e_versao(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso, monkeypatch
):
    """Sem --modelo: LLM_MODELO resolve o modelo e LLM_VERSAO_MODELO entra na
    assinatura do lote — caminho de sucesso inteiro por ambiente."""
    monkeypatch.setenv("LLM_MODELO", "modelo-env-x")
    monkeypatch.setenv("LLM_VERSAO_MODELO", "snap-y-2026")
    preparar_corpus_edital_duplo(cli, politeness_veloz, servidor_fake, corpus)
    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos"])  # SEM --modelo

    assert resultado.exit_code == 0, saida_cli(resultado)
    (lote,) = lotes_do(politeness_veloz.manifesto)
    assert lote["modelo"] == "modelo-env-x"
    assert lote["versao_do_modelo"] == "snap-y-2026"


def test_drift_de_vigencia_tira_documento_da_entrada_e_lote_segue(
    cli, politeness_veloz, servidor_fake, corpus, adaptador_falso
):
    """Triple-check honesto: bytes do PDF alterados APÓS o textuar derrubam a
    vigência — o documento sai da entrada com evento texto_indisponivel e o
    RESTO do edital codifica normalmente."""
    coletar_pdfs(
        cli,
        politeness_veloz,
        servidor_fake,
        {
            "/docs/a/edital-x.pdf": pdf_com_texto([longo("Documento que vai derivar")]),
            "/docs/b/edital-x.pdf": pdf_com_texto([longo("Documento integro do edital")]),
        },
    )
    assert cli.invoke(app, ["textuar", "--portal", "TST"]).exit_code == 0

    vitima = next(
        doc
        for doc in documentos_do_manifesto(politeness_veloz.manifesto)
        if doc["url_origem"].endswith("/docs/a/edital-x.pdf")
    )
    Path(vitima["caminho"]).write_bytes(b"%PDF-1.4 bytes-alterados-depois-do-textuar")

    escrever_codebook(politeness_veloz, congelado=True)
    textos = _textos_por_id(politeness_veloz.manifesto)
    adaptador_falso.respostas.append(lambda u: _resposta_ok(u, textos))

    resultado = cli.invoke(app, ["analise", "--todos", "--modelo", "m"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    eventos = tipos_eventos(politeness_veloz.manifesto)
    indisponiveis = [
        d for t, d in eventos if t == "analise_erro"
        and d.get("motivo") == "texto_indisponivel"
    ]
    assert len(indisponiveis) == 1
    assert indisponiveis[0]["documento_id"] == vitima["id"]
    ids_na_chamada = _ids_da_entrada(adaptador_falso.chamadas[0]["usuario"])
    assert vitima["id"] not in ids_na_chamada, "drift sai da entrada"
    assert len(ids_na_chamada) == 1, "o resto do edital segue"
    aplicada = next(d for t, d in eventos if t == "analise_aplicada")
    assert aplicada["edital_id"] == vitima["edital_id"]
    assert aplicada["documentos_entrada"] == 1


# -- Unidades: verificador de citação e validador de esquema -------------------------


def test_verificar_citacao_normaliza_caixa_espaco_e_calcula_pagina():
    texto = "pagina um com conteudo\npagina dois com TRECHO   alvo aqui\npagina tres"

    assert verificar_citacao("trecho alvo aqui", texto) == 2
    assert verificar_citacao("PAGINA   um COM", texto) == 1
    assert verificar_citacao("pagina tres", texto) == 3
    assert verificar_citacao("trecho inexistente", texto) is None
    assert verificar_citacao("   ", texto) is None
    # trecho colando fim de página e início da seguinte casa via normalização
    assert verificar_citacao("conteudo pagina", texto) == 1


def _codebook_congelado_em(pasta: Path) -> Codebook:
    ambiente = SimpleNamespace(configs=pasta)
    return carregar_codebook(escrever_codebook(ambiente, congelado=True))


def test_validar_saida_aceita_contrato_completo(tmp_path):
    codebook = _codebook_congelado_em(tmp_path)
    documentos = {"doc000000001": {"url_origem": "http://x.org/a.pdf"}}
    bom = json.dumps(
        {
            "campos": {
                "grau_exemplo": {
                    "valor": 1,
                    "citacao_documento": "doc000000001",
                    "citacao_trecho": "trecho real",
                },
                "tipo_exemplo": {
                    "valor": "categoria_b",
                    "citacao_documento": "doc000000001",
                    "citacao_trecho": "outro trecho",
                },
            }
        }
    )
    registros = validar_saida(bom, codebook, documentos)  # type: ignore[arg-type]
    assert len(registros) == 2
    por_campo = {r["campo"]: r for r in registros}
    assert por_campo["grau_exemplo"]["documento_id"] == "doc000000001"
    assert por_campo["grau_exemplo"]["url_origem"] == "http://x.org/a.pdf"


def test_validar_saida_recusa_violacoes_do_esquema(tmp_path):
    codebook = _codebook_congelado_em(tmp_path)
    documentos = {"doc000000001": {"url_origem": "http://x.org/a.pdf"}}
    casos_ruins = [
        '{"campos": {}}',
        '{"campos": {"grau_exemplo": {"valor": 3, "citacao_documento": "doc000000001", "citacao_trecho": "t"}}}',
        '{"campos": {"grau_exemplo": {"valor": true, "citacao_documento": "doc000000001", "citacao_trecho": "t"}, "tipo_exemplo": {"valor": "N/A"}}}',
        '{"campos": {"grau_exemplo": {"valor": 1, "citacao_documento": "fantasma", "citacao_trecho": "t"}, "tipo_exemplo": {"valor": "N/A"}}}',
        '{"campos": {"grau_exemplo": {"valor": 1, "citacao_documento": "doc000000001"}, "tipo_exemplo": {"valor": "N/A"}}}',
        '{"campos": {"grau_exemplo": {"valor": 1, "citacao_documento": "doc000000001", "citacao_trecho": ""}, "tipo_exemplo": {"valor": "N/A"}}}',
        '{"campos": {"grau_exemplo": {"valor": 0, "citacao_documento": null, "citacao_trecho": null}, "tipo_exemplo": {"valor": "N/A"}}}',
        '{"campos": {"grau_exemplo": {"valor": "N/A", "citacao_documento": "doc000000001", "citacao_trecho": "t"}, "tipo_exemplo": {"valor": "N/A"}}}',
        '{"campos": {"grau_exemplo": {"valor": 1, "citacao_documento": "doc000000001", "citacao_trecho": "t"}, "tipo_exemplo": {"valor": "categoria_z", "citacao_documento": "doc000000001", "citacao_trecho": "t"}}}',
        '{"campos": {"grau_exemplo": {"valor": 1, "citacao_documento": "doc000000001", "citacao_trecho": "t"}, "tipo_exemplo": {"valor": "categoria_a", "citacao_documento": "doc000000001", "citacao_trecho": "t"}, "extra": {"valor": 0}}}',
        '{"resposta": 42}',
        'sem json algum',
    ]
    for bruto in casos_ruins:
        with pytest.raises(SaidaInvalida):
            validar_saida(bruto, codebook, documentos)  # type: ignore[arg-type]


# -- Unidades do carregador de codebook -----------------------------------------------


def test_carregar_codebook_repo_exemplo_tem_tres_gates_pendentes():
    """Hermético: deriva do __file__ (como configs_reais_no_tmp), imune ao CWD."""
    codebook = carregar_codebook(CONFIGS_DO_REPO / "codebook.yaml")
    pendentes = codebook.gates_pendentes()
    assert len(pendentes) == 3
    assert any("congelamento" in g for g in pendentes)
    assert any("trietica_dahlin" in g for g in pendentes)
    assert any("limiar_kappa" in g for g in pendentes)


def test_codebook_congelado_nao_tem_gates_pendentes(tmp_path):
    ambiente = SimpleNamespace(configs=tmp_path)
    codebook = carregar_codebook(escrever_codebook(ambiente, congelado=True))
    assert codebook.gates_pendentes() == []
    assert codebook.ids_de_campos() == ["grau_exemplo", "tipo_exemplo"]


# -- Gates estritos: validadores do carregador (recusas) ------------------------------


def _codebook_variante(tmp_path, *, base: str, trocas: dict[str, str]) -> Path:
    """Codebook derivado por substituições literais no template gerado."""
    caminho = escrever_codebook(SimpleNamespace(configs=tmp_path), congelado=base == "frozen")
    texto = caminho.read_text(encoding="utf-8")
    for antigo, novo in trocas.items():
        assert antigo in texto, f"âncora ausente no template: {antigo!r}"
        texto = texto.replace(antigo, novo)
    caminho.write_text(texto, encoding="utf-8")
    return caminho


def test_congelado_em_nao_iso_8601_eh_recusado(tmp_path):
    caminho = _codebook_variante(
        tmp_path,
        base="frozen",
        trocas={'congelado_em: "2026-08-25T10:00:00-03:00"': 'congelado_em: "banana"'},
    )

    with pytest.raises(ErroCodebook) as excinfo:
        carregar_codebook(caminho)
    assert "ISO 8601" in str(excinfo.value)


def test_congelado_por_vazio_normaliza_para_gate_pendente(tmp_path):
    """Autoria em branco não conta como preenchida — o gate segue pendente."""
    caminho = _codebook_variante(
        tmp_path,
        base="unfrozen",
        trocas={
            'congelado_em:': 'congelado_em: "2026-08-25T10:00:00-03:00"',
            'congelado_por:': 'congelado_por: "   "',
        },
    )
    codebook = carregar_codebook(caminho)
    pendentes = codebook.gates_pendentes()
    assert any("congelamento" in g for g in pendentes)


@pytest.mark.parametrize(
    ("trocas", "fragmento"),
    [
        (
            {"decisao: incorporada": "decisao:"},
            "decisao",
        ),
        (
            {"decisao: incorporada": "decisao: talvez"},
            "incorporada, excluida_justificada",
        ),
        (
            {
                "decisao: incorporada": "decisao: excluida_justificada",
                'justificativa:': 'justificativa:',
            },
            "justificativa",
        ),
    ],
)
def test_resolvida_true_com_decisao_incoerente_eh_recusado(
    tmp_path, trocas, fragmento
):
    caminho = _codebook_variante(tmp_path, base="frozen", trocas=trocas)

    with pytest.raises(ErroCodebook) as excinfo:
        carregar_codebook(caminho)
    assert fragmento in str(excinfo.value)


def test_excluida_justificada_sem_justificativa_eh_recusada_mesmo_pendente(tmp_path):
    """Exclusão precisa estar justificada EM ALGUM LUGAR — inclusive no estado
    intermediário resolvida=false com a decisão já declarada."""
    caminho = _codebook_variante(
        tmp_path,
        base="unfrozen",
        trocas={
            "resolvida: false": "resolvida: false",
            "decisao:": "decisao: excluida_justificada",
            'justificativa: Pendente de decisão com o(a) orientador(a) (OQ-6).': "justificativa:",
        },
    )

    with pytest.raises(ErroCodebook) as excinfo:
        carregar_codebook(caminho)
    assert "justificativa" in str(excinfo.value)


def test_resolvida_false_sem_justificativa_eh_recusado(tmp_path):
    caminho = _codebook_variante(
        tmp_path,
        base="unfrozen",
        trocas={
            "justificativa: Pendente de decisão com o(a) orientador(a) (OQ-6).":
                "justificativa:",
        },
    )

    with pytest.raises(ErroCodebook) as excinfo:
        carregar_codebook(caminho)
    assert "não resolvida exige 'justificativa'" in str(excinfo.value)


@pytest.mark.parametrize("kappa", ["0", "0.0", "-0.1", "1.5"])
def test_limiar_kappa_fora_de_zero_menor_um_eh_recusado(tmp_path, kappa):
    caminho = _codebook_variante(
        tmp_path,
        base="frozen",
        trocas={"limiar_kappa: 0.75": f"limiar_kappa: {kappa}"},
    )

    with pytest.raises(ErroCodebook):
        carregar_codebook(caminho)


def test_kappa_um_eh_aceito_na_fronteira(tmp_path):
    caminho = _codebook_variante(
        tmp_path,
        base="frozen",
        trocas={"limiar_kappa: 0.75": "limiar_kappa: 1.0"},
    )
    codebook = carregar_codebook(caminho)
    assert codebook.gates_pendentes() == []


def test_codebook_chave_desconhecida_eh_erro_com_linha(tmp_path):
    caminho = tmp_path / "codebook.yaml"
    conteudo = escrever_codebook(SimpleNamespace(configs=tmp_path), congelado=True)
    texto = conteudo.read_text(encoding="utf-8")
    texto = texto.replace("schema_version: 1", "schema_version: 1\nchave_estranha: 1")
    caminho.write_text(texto, encoding="utf-8")

    with pytest.raises(ErroCodebook) as excinfo:
        carregar_codebook(caminho)
    problema = excinfo.value.problemas[0]
    assert "chave_estranha" in problema
    assert "linha 2" in problema


def test_codebook_yaml_malformado_eh_erro_acionavel(tmp_path):
    caminho = tmp_path / "codebook.yaml"
    caminho.write_text("dimensoes: [sem, fechamento", encoding="utf-8")

    with pytest.raises(ErroCodebook) as excinfo:
        carregar_codebook(caminho)
    assert "YAML malformado" in excinfo.value.problemas[0]


def test_codebook_id_de_campo_duplicado_eh_erro(tmp_path):
    ambiente = SimpleNamespace(configs=tmp_path)
    caminho = escrever_codebook(ambiente, congelado=True)
    carregar_codebook(caminho)  # baseline válida
    texto = caminho.read_text(encoding="utf-8")
    # segunda dimensão com o MESMO id de campo — ids de campos são chaves do catálogo
    insercao = (
        "  - id: dimensao_extra\n"
        "    nome: \"Extra\"\n"
        "    definicao_operacional: \"Duplicata de teste.\"\n"
        "    campos:\n"
        "      - id: grau_exemplo\n"
        "        nome: \"Grau duplicado\"\n"
        "        escala: ordinal\n"
        "        valores: [0, 1]\n"
        "        permite_na: false\n"
        "        definicao_operacional: \"Duplicado.\"\n"
        "        regra_decisao: \"Regra.\"\n"
        "        ancoras_positivas: [\"p\"]\n"
        "        ancoras_negativas: [\"n\"]\n"
        "        regra_boilerplate: \"b\"\n"
    )
    texto = texto.replace("congelamento:", insercao + "congelamento:")
    caminho.write_text(texto, encoding="utf-8")

    with pytest.raises(ErroCodebook) as excinfo:
        carregar_codebook(caminho)
    assert "duplicado" in excinfo.value.problemas[0]


def test_prompt_sistema_eh_estavel_e_traz_o_contrato(tmp_path):
    codebook = _codebook_congelado_em(tmp_path)
    sistema1, sha1 = montar_prompt_sistema(codebook)
    sistema2, sha2 = montar_prompt_sistema(_codebook_congelado_em(tmp_path))
    assert sha1 == sha2, "mesmo codebook ⇒ mesmo hash de prompt"
    assert "grau_exemplo" in sistema1 and "tipo_exemplo" in sistema1
    assert "N/A" in sistema1
    assert '"campos"' in sistema1


# -- Adaptador: isolamento do provedor sem vazar credenciais ---------------------------


class _RespostaFake:
    def __init__(self, status_code: int, corpo: object) -> None:
        self.status_code = status_code
        self._corpo = corpo

    def json(self) -> object:
        if isinstance(self._corpo, Exception):
            raise self._corpo
        return self._corpo


@pytest.fixture
def ambiente_llm(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.teste/v1")
    monkeypatch.setenv("LLM_API_KEY", "segredo-ultra-sigiloso")
    monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
    llm_adapter.reiniciar_instrumento()
    yield
    llm_adapter.reiniciar_instrumento()


def test_concluir_envia_payload_e_devolve_conteudo(ambiente_llm, monkeypatch):
    capturas: dict = {}

    def _post(url, json=None, headers=None, timeout=None):
        capturas.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _RespostaFake(
            200,
            {"choices": [{"message": {"content": "resposta do modelo"}}]},
        )

    monkeypatch.setattr(llm_adapter.requests, "post", _post)
    llm_adapter.definir_instrumento(modelo="modelo-x", temperatura=0.5, seed=42)

    saida = llm_adapter.concluir("instrucoes", "entrada")

    assert saida == "resposta do modelo"
    assert capturas["url"].startswith("https://llm.teste/v1")
    assert capturas["json"]["model"] == "modelo-x"
    assert capturas["json"]["temperature"] == 0.5
    assert capturas["json"]["seed"] == 42
    assert capturas["headers"]["Authorization"] == "Bearer segredo-ultra-sigiloso"


def test_concluir_sem_seed_nao_envia_seed(ambiente_llm, monkeypatch):
    capturas: dict = {}

    def _post(url, json=None, headers=None, timeout=None):
        capturas.update({"json": json})
        return _RespostaFake(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_adapter.requests, "post", _post)
    llm_adapter.definir_instrumento(modelo="m", temperatura=0.0)

    llm_adapter.concluir("s", "u")
    assert "seed" not in capturas["json"]


def test_concluir_sem_instrumento_falha_rapido(ambiente_llm):
    with pytest.raises(llm_adapter.ErroProvedorLLM):
        llm_adapter.concluir("s", "u")


def _assert_sem_credenciais(mensagem: str) -> None:
    """Em QUALQUER caminho de falha, nada de chave, header Bearer ou base_url."""
    assert "segredo-ultra-sigiloso" not in mensagem
    assert "Bearer" not in mensagem
    assert "https://llm.teste" not in mensagem


@pytest.mark.parametrize(
    ("nome_cenario", "status", "corpo"),
    [
        ("http-500", 500, {}),
        ("http-401", 401, {"error": "bad key"}),
        ("http-403", 403, {}),
        ("http-503", 503, {"error": "unavailable"}),
        ("corpo-inesperado", 200, {"inesperado": True}),
        ("choices-vazio", 200, {"choices": []}),
        ("mensagem-ausente", 200, {"choices": [{"message": {}}]}),
        ("conteudo-vazio", 200, {"choices": [{"message": {"content": "   "}}]}),
        ("json-invalido", 200, ValueError("bytes podres")),
    ],
)
def test_nenhuma_falha_vaza_credencial_nem_url_na_mensagem(
    ambiente_llm, monkeypatch, nome_cenario, status, corpo
):
    """Assertion INCONDICIONAL em qualquer caminho: mensagem do
    ErroProvedorLLM jamais contém base_url nem chave."""
    monkeypatch.setattr(
        llm_adapter.requests,
        "post",
        lambda url, json=None, headers=None, timeout=None: _RespostaFake(status, corpo),
    )
    llm_adapter.definir_instrumento(modelo="m", temperatura=0.0)

    with pytest.raises(llm_adapter.ErroProvedorLLM) as excinfo:
        llm_adapter.concluir("s", "u")
    _assert_sem_credenciais(str(excinfo.value))


@pytest.mark.parametrize(
    ("modulo_excecao", "nome_excecao"),
    [
        (None, "Timeout"),
        (None, "ConnectionError"),
        ("exceptions", "ReadTimeout"),
        ("exceptions", "SSLError"),
    ],
)
def test_rede_morta_ou_timeout_vira_erro_provedor_sem_vazar_nada(
    ambiente_llm, monkeypatch, modulo_excecao, nome_excecao
):
    origem = (
        llm_adapter.requests
        if modulo_excecao is None
        else getattr(llm_adapter.requests, modulo_excecao)
    )
    excecao = getattr(origem, nome_excecao)

    def _post(url, json=None, headers=None, timeout=None):
        # a mensagem da exceção real costuma conter a URL — o adapter NUNCA
        # deve repassá-la
        raise excecao("POST https://llm.teste/v1/chat/completions falhou")

    monkeypatch.setattr(llm_adapter.requests, "post", _post)
    llm_adapter.definir_instrumento(modelo="m", temperatura=0.0)

    with pytest.raises(llm_adapter.ErroProvedorLLM) as excinfo:
        llm_adapter.concluir("s", "u")
    _assert_sem_credenciais(str(excinfo.value))


def test_prazo_total_da_chamada_eh_cobrado_alem_do_timeout_do_requests(
    ambiente_llm, monkeypatch
):
    """time.monotonic cobre o orçamento TOTAL: resposta que chega depois do
    prazo vira ErroProvedorLLM 'prazo total excedido', mesmo com HTTP 200."""
    relogio = iter([100.0, 100.0 + 999.0])
    monkeypatch.setattr(llm_adapter.time, "monotonic", lambda: next(relogio))
    monkeypatch.setattr(
        llm_adapter.requests,
        "post",
        lambda url, json=None, headers=None, timeout=None: _RespostaFake(
            200, {"choices": [{"message": {"content": "chegou tarde"}}]}
        ),
    )
    llm_adapter.definir_instrumento(modelo="m", temperatura=0.0)

    with pytest.raises(llm_adapter.ErroProvedorLLM) as excinfo:
        llm_adapter.concluir("s", "u")
    assert "prazo total excedido" in str(excinfo.value)
    _assert_sem_credenciais(str(excinfo.value))


def test_prazo_total_dentro_do_orcamento_passa_normal(ambiente_llm, monkeypatch):
    relogio = iter([10.0, 10.5])
    monkeypatch.setattr(llm_adapter.time, "monotonic", lambda: next(relogio))
    monkeypatch.setattr(
        llm_adapter.requests,
        "post",
        lambda url, json=None, headers=None, timeout=None: _RespostaFake(
            200, {"choices": [{"message": {"content": "ok"}}]}
        ),
    )
    llm_adapter.definir_instrumento(modelo="m", temperatura=0.0, timeout_s=60.0)

    assert llm_adapter.concluir("s", "u") == "ok"


def test_concluir_rede_morta_vira_erro_provedor(ambiente_llm, monkeypatch):
    def _post(*args, **kwargs):
        raise llm_adapter.requests.ConnectionError("recusado")

    monkeypatch.setattr(llm_adapter.requests, "post", _post)
    llm_adapter.definir_instrumento(modelo="m", temperatura=0.0)

    with pytest.raises(llm_adapter.ErroProvedorLLM):
        llm_adapter.concluir("s", "u")


def test_credenciais_ausentes_viram_erro_claro(ambiente_llm, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY")
    llm_adapter.definir_instrumento(modelo="m", temperatura=0.0)

    with pytest.raises(llm_adapter.ErroProvedorLLM) as excinfo:
        llm_adapter.concluir("s", "u")
    assert "LLM_API_KEY" in str(excinfo.value)


def test_definir_instrumento_recusa_parametros_invalidos(ambiente_llm):
    with pytest.raises(ValueError):
        llm_adapter.definir_instrumento(modelo="", temperatura=0.0)
    with pytest.raises(ValueError):
        llm_adapter.definir_instrumento(modelo="m", temperatura=3.5)
    with pytest.raises(ValueError):
        llm_adapter.definir_instrumento(modelo="m", temperatura=float("nan"))
    with pytest.raises(ValueError):
        llm_adapter.definir_instrumento(modelo="m", temperatura=0.0, timeout_s=-1)
