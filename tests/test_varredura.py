"""Story de varredura: adicionar/listar/rodar URLs coladas de páginas de editais.

Os cenários da matriz I/O da story rodam CONTRA o servidor fake local
(``ServidorFalso``) e o CLI real — a varredura é a descoberta reutilizada com
``comando='varredura'`` e um Portal sintético em memória.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agente_editais.consulta import app
from agente_editais.manifest import Manifesto, agora_iso_utc

from .conftest import (
    escrever_mapa,
    mapa_portal,
    saida_cli,
    tipos_eventos,
    url_do,
)


def _adicionar_ok(cli, politeness_veloz, servidor_fake, caminho: str) -> str:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    url = url_do(servidor_fake, caminho)
    resultado = cli.invoke(app, ["varredura", "adicionar", url])
    assert resultado.exit_code == 0, saida_cli(resultado)
    return url


def _linhas_varredura(caminho_manifesto) -> list[dict]:
    with Manifesto(caminho_manifesto) as manifesto:
        return [dict(linha) for linha in manifesto.varreduras_por_status()]


def test_varredura_adicionar_registra_pendente_com_contexto_e_evento(
    cli, politeness_veloz, servidor_fake
) -> None:
    url = _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    linhas = _linhas_varredura(politeness_veloz.manifesto)
    assert len(linhas) == 1
    assert linhas[0]["url"] == url
    assert linhas[0]["status"] == "pendente"
    assert linhas[0]["instituicao_sigla"] == "TST"
    assert linhas[0]["portal_nome"] == "Portal TST"
    assert linhas[0]["criado_em"]

    tipos = [tipo for tipo, _ in tipos_eventos(politeness_veloz.manifesto)]
    assert "varredura_adicionada" in tipos


def test_varredura_adicionar_duplicada_avisa_e_nao_duplica(
    cli, politeness_veloz, servidor_fake
) -> None:
    url = _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    resultado = cli.invoke(app, ["varredura", "adicionar", url])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "já cadastrada" in saida_cli(resultado)
    assert len(_linhas_varredura(politeness_veloz.manifesto)) == 1, "UNIQUE url"


def test_varredura_adicionar_url_malformada_recusa_tudo_exit2(
    cli, politeness_veloz, servidor_fake
) -> None:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(
        app, ["varredura", "adicionar", "nao-eh-url", "ftp://x.org/editais.pdf"]
    )

    assert resultado.exit_code == 2
    saida = saida_cli(resultado)
    assert "esquema inválido" in saida or "URL sem host" in saida
    assert "nenhuma URL registrada" in saida
    assert _linhas_varredura(politeness_veloz.manifesto) == [], "nada é escrito"


def test_varredura_adicionar_host_fora_do_mapa_recusa_exit2_e_lista_conhecidos(
    cli, politeness_veloz, servidor_fake
) -> None:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0
    externa = url_do(servidor_fake, "/outra").replace("127.0.0.1", "ph.com.br")

    resultado = cli.invoke(app, ["varredura", "adicionar", externa])

    assert resultado.exit_code == 2
    saida = saida_cli(resultado)
    assert "host sem portal" in saida
    assert "hosts conhecidos" in saida
    assert _linhas_varredura(politeness_veloz.manifesto) == []


def test_varredura_listar_mostra_filtro_e_valida_status(
    cli, politeness_veloz, servidor_fake
) -> None:
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    listagem = cli.invoke(app, ["varredura", "listar"])
    assert listagem.exit_code == 0, saida_cli(listagem)
    saida = saida_cli(listagem)
    assert "ID" in saida and "STATUS" in saida
    assert "pendente" in saida

    filtro = cli.invoke(app, ["varredura", "listar", "--status", "pendente"])
    assert filtro.exit_code == 0
    assert "Portal TST" in saida_cli(filtro)

    vazio = cli.invoke(app, ["varredura", "listar", "--status", "concluida"])
    assert vazio.exit_code == 0
    assert "Nenhuma varredura com status 'concluida'" in saida_cli(vazio)

    invalido = cli.invoke(app, ["varredura", "listar", "--status", "nada"])
    assert invalido.exit_code == 2
    assert "STATUS" in saida_cli(invalido).upper()


def test_varredura_rodar_executa_pagina_do_edital_e_conclui_com_resumo(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/editais/lista"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body>"
        "<a href='/pdfs/edital-publico.pdf'>Edital Público de Pesquisa 2024</a>"
        "</body></html>",
    )
    servidor_fake.paginas["/pdfs/edital-publico.pdf"] = (
        200,
        "application/pdf",
        b"%PDF-fake",
    )
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    resultado = cli.invoke(app, ["varredura", "rodar"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "[TST] Portal TST" in saida
    assert "1 visitadas" in saida  # a própria URL colada (profundidade 0)
    assert "1 novos" in saida

    linhas = _linhas_varredura(politeness_veloz.manifesto)
    assert len(linhas) == 1
    assert linhas[0]["status"] == "concluida"
    assert linhas[0]["concluido_em"]
    resumo_log = json.loads(linhas[0]["log"])
    assert resumo_log["secoes_visitadas"] == 1
    assert resumo_log["candidatos_novos"] == 1

    with Manifesto(politeness_veloz.manifesto) as manifesto:
        candidatos = [
            tuple(row)
            for row in manifesto.consultar(
                "SELECT url, tipo FROM candidatos ORDER BY url"
            )
        ]
    assert candidatos == [(url_do(servidor_fake, "/pdfs/edital-publico.pdf"), "pdf")]

    tipos = [tipo for tipo, _ in tipos_eventos(politeness_veloz.manifesto)]
    assert "descoberta_portal_concluida" in tipos


def test_varredura_rodar_repetido_e_idempotente_ja_conhecidas(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/editais/lista"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='/pdfs/a.pdf'>Edital A</a></body></html>",
    )
    servidor_fake.paginas["/pdfs/a.pdf"] = (200, "application/pdf", b"%PDF-fake")
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")
    assert cli.invoke(app, ["varredura", "rodar"]).exit_code == 0

    pedidos_antes = len(
        [r for r in servidor_fake.registros if r["metodo"] == "GET"]
    )
    (linha,) = _linhas_varredura(politeness_veloz.manifesto)

    segunda = cli.invoke(app, ["varredura", "rodar", "--id", str(linha["id"])])

    assert segunda.exit_code == 0, saida_cli(segunda)
    saida = saida_cli(segunda)
    assert "0 visitadas" in saida
    assert "1 já conhecidas" in saida
    assert _linhas_varredura(politeness_veloz.manifesto)[0]["status"] == "concluida"
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        assert manifesto.contar_candidatos() == 1, "dedupe por URL normalizada"
    novos = [r for r in servidor_fake.registros if r["metodo"] == "GET"][pedidos_antes:]
    assert novos == [] or all(r["caminho"] == "/robots.txt" for r in novos), (
        "nunca revisitar a página no 2º ciclo"
    )


def test_varredura_rodar_fora_da_janela_recusa_sem_rede_e_fica_pendente(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    from agente_editais import consulta

    # politeness SEM [crawl] ⇒ default TRUE: varredura obriga a janela
    (politeness_veloz.configs / "politeness.toml").write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua/1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(consulta, "dentro_da_janela_off_peak", lambda *_a, **_k: False)
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    resultado = cli.invoke(app, ["varredura", "rodar"])

    assert resultado.exit_code == 1
    saida = saida_cli(resultado)
    assert "off-peak" in saida.lower()
    assert "coleta_complementar.bat" in saida
    assert servidor_fake.registros == [], "nenhuma requisição fora da janela"
    assert _linhas_varredura(politeness_veloz.manifesto)[0]["status"] == "pendente"


def test_varredura_rodar_fora_da_janela_com_flag_explicito_executa(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    from agente_editais import consulta

    (politeness_veloz.configs / "politeness.toml").write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "ua/1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(consulta, "dentro_da_janela_off_peak", lambda *_a, **_k: False)
    servidor_fake.paginas["/editais/lista"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><a href='/pdfs/x.pdf'>Edital X</a></body></html>",
    )
    servidor_fake.paginas["/pdfs/x.pdf"] = (200, "application/pdf", b"%PDF-fake")
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    resultado = cli.invoke(app, ["varredura", "rodar", "--fora-da-janela"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert _linhas_varredura(politeness_veloz.manifesto)[0]["status"] == "concluida"


def test_varredura_rodar_id_inexistente_exit1(cli, politeness_veloz, servidor_fake) -> None:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["varredura", "rodar", "--id", "99"])

    assert resultado.exit_code == 1
    assert "não existe" in saida_cli(resultado)


def test_varredura_rodar_sem_pendentes_e_sucesso_silencioso(
    cli, politeness_veloz, servidor_fake
) -> None:
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0

    resultado = cli.invoke(app, ["varredura", "rodar"])

    assert resultado.exit_code == 0
    assert "Nenhuma varredura pendente" in saida_cli(resultado)


def test_varredura_rodar_falha_vira_falhou_e_lote_segue_exit0(
    cli, politeness_veloz, servidor_fake, monkeypatch
) -> None:
    from agente_editais import consulta

    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    def _falha(*_args, **_kwargs) -> None:
        raise RuntimeError("rede eleitoral caiu")

    monkeypatch.setattr(consulta, "rodar_varredura", _falha)

    resultado = cli.invoke(app, ["varredura", "rodar"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "FALHA" in saida_cli(resultado)
    (linha,) = _linhas_varredura(politeness_veloz.manifesto)
    assert linha["status"] == "falhou", "falha pontual vira status + evento"
    assert json.loads(linha["log"]).get("erro") == "rede eleitoral caiu"


def test_varredura_rodar_id_reexecuta_e_recupera_travada_em_rodando(
    cli, politeness_veloz, servidor_fake
) -> None:
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        (linha,) = manifesto.varreduras_por_status("pendente")
        travada_em = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        manifesto.executar(
            "UPDATE varreduras SET status = 'rodando', iniciado_em = ? WHERE id = ?",
            (travada_em, linha["id"]),
        )

    resultado = cli.invoke(app, ["varredura", "rodar", "--id", str(linha["id"])])

    assert resultado.exit_code == 0, saida_cli(resultado)
    (atual,) = _linhas_varredura(politeness_veloz.manifesto)
    assert atual["status"] == "concluida", "--id força e recupera travadas (>2h)"


def test_varredura_rodar_id_recusa_rodando_recente_menos_de_2h(
    cli, politeness_veloz, servidor_fake
) -> None:
    """Varredura 'rodando' iniciada há < 2h NÃO é recuperada (pode estar viva).

    O guard E4 recusa forçar até a janela de recuperação.
    """
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")
    with Manifesto(politeness_veloz.manifesto) as manifesto:
        (linha,) = manifesto.varreduras_por_status("pendente")
        recente = agora_iso_utc()
        manifesto.executar(
            "UPDATE varreduras SET status = 'rodando', iniciado_em = ? WHERE id = ?",
            (recente, linha["id"]),
        )

    resultado = cli.invoke(app, ["varredura", "rodar", "--id", str(linha["id"])])

    assert resultado.exit_code == 1, saida_cli(resultado)
    saida = saida_cli(resultado)
    assert "rodando" in saida
    assert "menos de" in saida
    assert "2h" in saida or "2 h" in saida
    (atual,) = _linhas_varredura(politeness_veloz.manifesto)
    assert atual["status"] == "rodando", "não deve ter mudado"


def test_varredura_rodar_sem_candidatos_conclui_exit0_com_zero(
    cli, politeness_veloz, servidor_fake
) -> None:
    servidor_fake.paginas["/editais/lista"] = (
        200,
        "text/html; charset=utf-8",
        "<html><body><p>Em breve novos editais.</p></body></html>",
    )
    _adicionar_ok(cli, politeness_veloz, servidor_fake, "/editais/lista")

    resultado = cli.invoke(app, ["varredura", "rodar"])

    assert resultado.exit_code == 0, saida_cli(resultado)
    assert "0 novos" in saida_cli(resultado)
    (linha,) = _linhas_varredura(politeness_veloz.manifesto)
    assert linha["status"] == "concluida"
    resumo_log = json.loads(linha["log"])
    assert resumo_log["candidatos_novos"] == 0
    assert resumo_log["secoes_visitadas"] == 1


def test_varredura_adicionar_mista_com_portal_nao_sincronizado_nao_escreve(
    cli, politeness_veloz, servidor_fake
) -> None:
    estranha = url_do(servidor_fake).replace("127.0.0.1", "ph2.com.br")
    escrever_mapa(politeness_veloz, mapa_portal(servidor_fake, sigla="TST"))
    assert cli.invoke(app, ["mapa", "validar"]).exit_code == 0  # só TST sincronizado

    # mapa do arquivo é ATUALIZADO com o portal BBB (host novo) SEM revalidar:
    # o portal existe na config mas ainda não foi sincronizado no Manifesto.
    escrever_mapa(
        politeness_veloz,
        mapa_portal(servidor_fake, sigla="TST")
        + '\n[[instituicao]]\nsigla = "BBB"\nnome = "Instituto BBB"\n\n'
        + '  [[instituicao.portal]]\n  nome = "Portal BBB"\n'
        + '  categoria = "integra"\n'
        + f'  url = "{estranha}"\n  seeds = ["{estranha}"]\n',
    )

    resultado = cli.invoke(
        app,
        [
            "varredura",
            "adicionar",
            url_do(servidor_fake, "/editais/lista"),
            estranha,
        ],
    )

    assert resultado.exit_code == 1
    assert "não sincronizados" in saida_cli(resultado)
    assert _linhas_varredura(politeness_veloz.manifesto) == [], (
        "validação antes de qualquer INSERT (AD-3)"
    )
