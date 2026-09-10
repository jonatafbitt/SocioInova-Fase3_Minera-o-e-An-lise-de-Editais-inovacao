"""Cenários do resgate por OCR (Fase 3.1): estágio opcional e NÃO automático.

NENHUM teste chama o binário Tesseract real — o empilhamento é trocado por
fakes (module-level) via monkeypatch em ``importlib.import_module``. O que é
testado de verdade: decisões de estado (confiança ≥ limiar, retomada por
``ocr_em``, não-escaneado fora do lote), o ``.txt`` irmão (AD-2), as colunas
``ocr_*`` da v11 e os eventos; o CLI guarda indisponibilidade com exit 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agente_editais.consulta import app
from agente_editais.manifest import Manifesto
from agente_editais.mapa import Portal
from agente_editais.texto import (
    ContextoTexto,
    ErroOcrIndisponivel,
    _importar_ocr,
    _ocr_uma_pagina,
    ocr_disponivel,
    ocrescer_portal,
)

from .conftest import saida_cli

# -- fakes do empilhamento OCR (nunca tocam o binário real) -------------------

class ImagemFake:
    def to_pil(self) -> ImagemFake:
        return self

    def convert(self, modo: str) -> ImagemFake:
        return self


class PaginaFake:
    def render(self, scale: float = 1.0) -> ImagemFake:
        return ImagemFake()


class PdfiumFake:
    """pypdfium2 falso: ``PdfDocument`` devolve o próprio objeto com páginas."""

    def __init__(self, paginas: int) -> None:
        self._paginas = [PaginaFake() for _ in range(paginas)]

    def PdfDocument(self, caminho: str) -> PdfiumFake:
        return self

    def __len__(self) -> int:
        return len(self._paginas)

    def __getitem__(self, indice: int) -> PaginaFake:
        return self._paginas[indice]

    def close(self) -> None:
        pass


class TesseractFake:
    """pytesseract falso: respostas pré-programadas por chamada de página."""

    class Output:
        DICT = "dict"

    def __init__(self, respostas: list[dict]) -> None:
        self._fila = iter(respostas)

    def get_languages(self, config: str = "") -> list[str]:
        return ["por", "eng"]

    def image_to_data(self, imagem: object, lang: str, config: str, output_type: str) -> dict:
        return next(self._fila)


def _resposta(*, palavras: list[tuple[str, int]]) -> dict:
    """Resposta DICT com texto e conf por palavra (conf -1 é descartada)."""
    return {
        "text": [palavra for palavra, _ in palavras],
        "conf": [confianca for _, confianca in palavras],
    }


def _instalar_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tesseract: TesseractFake,
    pdfium: PdfiumFake,
) -> None:
    """Troca import_module: pytesseract/pypdfium2/PIL viram os fakes do teste."""

    def fabricar(nome: str) -> object:
        if nome == "pytesseract":
            return tesseract
        if nome == "pypdfium2":
            return pdfium
        if nome == "PIL":
            return object()
        raise ImportError(nome)

    monkeypatch.setattr("importlib.import_module", fabricar)


# -- cena: Manifesto + portal + candidatos + documentos escaneados reais -------


def _montar_cena_ocr(
    tmp_path: Path,
    *,
    documentos: list[tuple[str, str, int, str | None]],
) -> tuple[Manifesto, ContextoTexto]:
    """Manifesto com portal OCR e um Documento escaneado por perfil.

    ``documentos``: (id, url, flag_escaneado, ocr_em). PDFs são bytes fake
    reais no disco (o caminho é o que o estágio lê); o hash é inventado.
    """
    manifesto = Manifesto(tmp_path / "m.sqlite3")
    manifesto.executar(
        "INSERT INTO instituicoes (sigla, nome, criado_em) "
        "VALUES ('OCR', 'Instituição OCR', '2026-01-01T00:00:00+00:00')"
    )
    manifesto.executar(
        """
        INSERT INTO portais (
            id, instituicao_id, nome, categoria, url, dinamico,
            profundidade_maxima, criado_em
        ) VALUES (1, 1, 'Portal OCR', 'integra', 'http://ocr.test', 0, 3,
                  '2026-01-01T00:00:00+00:00')
        """
    )
    manifesto.executar(
        """
        INSERT INTO editais (id, instituicao_id, ano_provisorio, criado_em)
        VALUES ('ocr-2026-001', 1, 2026, '2026-01-01T00:00:00+00:00')
        """
    )
    for documento_id, url, flag, ocr_em in documentos:
        manifesto.executar(
            "INSERT INTO candidatos (portal_id, url, tipo, descoberto_em) "
            "VALUES (1, ?, 'pdf', '2026-04-04T00:00:00+00:00')",
            (url,),
        )
        caminho_pdf = tmp_path / f"{documento_id}.pdf"
        caminho_pdf.write_bytes(b"%PDF-fake-bytes")
        manifesto.executar(
            """
            INSERT INTO documentos (
                id, edital_id, url_origem, caminho, hash_sha256, data_captura,
                ano_provisorio, versao_crawler, flag_escaneado, ocr_em
            ) VALUES (?, 'ocr-2026-001', ?, ?, ?, '2026-04-04T01:00:00+00:00',
                      2026, '0.1.0', ?, ?)
            """,
            (documento_id, url, str(caminho_pdf), "h" * 64, flag, ocr_em),
        )
    contexto = ContextoTexto(
        "OCR",
        Portal(nome="Portal OCR", categoria="integra", url="http://ocr.test", seeds=["http://ocr.test/"]),
        1,
    )
    return manifesto, contexto


# -- desfecho ``resgatado`` com confiança no limiar ----------------------------


def test_ocrescer_resgata_no_limiar_e_registra_proveniencia(tmp_path, monkeypatch) -> None:
    """Duas páginas: uma acima (95/90) e uma abaixo (10) do limiar.

    ``resgatado``: .txt irmão com as páginas aceitas, flag zerada, colunas
    ``ocr_*`` preenchidas e evento ``texto_ocr_aplicado`` — a página ruim NÃO
    entra no texto óptico nem na média (média das aceitas: 92.5).
    """
    manifesto, contexto = _montar_cena_ocr(
        tmp_path,
        documentos=[("ocrdoc01", "http://ocr.test/doc1.pdf", 1, None)],
    )
    tesseract = TesseractFake(
        [
            _resposta(palavras=[("Edital", 95), ("nº", 90)]),
            _resposta(palavras=[("borrão.", 10)]),
        ]
    )
    _instalar_fakes(monkeypatch, tesseract=tesseract, pdfium=PdfiumFake(paginas=2))

    resumo = ocrescer_portal(contexto, manifesto, confianca_minima=60, idioma="por")

    try:
        assert resumo.escaneados == 1 and resumo.resgatados == 1
        assert resumo.falhas == 0 and resumo.pulados == 0

        (linha,) = manifesto.consultar("SELECT * FROM documentos")
        txt = Path(linha["texto_caminho"])
        assert txt.read_text(encoding="utf-8") == "Edital nº", "só a página aceita entra"
        assert linha["flag_escaneado"] == 0, "resgate validado zera a flag"
        assert linha["ocr_metodo"] == "pypdfium2+tesseract"
        assert linha["ocr_confianca_media"] == 92.5
        assert linha["ocr_paginas_resgatadas"] == 1
        assert linha["ocr_tentativas"] == 1
        assert linha["ocr_em"], "carimbo de retomada preenchido"

        (evento,) = manifesto.consultar(
            "SELECT detalhe FROM eventos WHERE tipo = 'texto_ocr_aplicado'"
        )
        detalhe = json.loads(evento["detalhe"])
        assert detalhe["chars"] == len("Edital nº")
        assert detalhe["paginas"] == 1
        assert detalhe["confianca_media"] == 92.5
        assert detalhe["confianca_minima"] == 60 and detalhe["idioma"] == "por"
    finally:
        manifesto.fechar()


# -- desfecho ``falhou`` por confiança --------------------------------


def test_ocrescer_falha_se_nenhuma_pagina_atinge_confianca(tmp_path, monkeypatch) -> None:
    """Páginas bem abaixo do limiar ⇒ ``falhou``.

    O documento PERMANECE escaneado (flag 1), sem ``ocr_em`` e sem .txt — o
    texto óptico ruim não é gravado nem vira "texto".
    """
    manifesto, contexto = _montar_cena_ocr(
        tmp_path,
        documentos=[("ocrdoc02", "http://ocr.test/doc2.pdf", 1, None)],
    )
    tesseract = TesseractFake(
        [
            _resposta(palavras=[("bagunça aaaaa.", 15)]),
            _resposta(palavras=[("aaaaaaa.", 8)]),
        ]
    )
    _instalar_fakes(monkeypatch, tesseract=tesseract, pdfium=PdfiumFake(paginas=2))

    resumo = ocrescer_portal(contexto, manifesto, confianca_minima=60, idioma="por")

    try:
        assert resumo.escaneados == 1 and resumo.falhas == 1 and resumo.resgatados == 0
        assert resumo.urls_falhas == ["http://ocr.test/doc2.pdf"]

        (linha,) = manifesto.consultar("SELECT * FROM documentos")
        assert linha["flag_escaneado"] == 1, "falha NÃO zera a flag (continua escaneado)"
        assert linha["ocr_em"] is None and linha["texto_caminho"] is None

        (evento,) = manifesto.consultar(
            "SELECT detalhe FROM eventos WHERE tipo = 'texto_ocr_falhou'"
        )
        assert json.loads(evento["detalhe"])["fase"] == "confianca"
    finally:
        manifesto.fechar()


# -- retomada: não-escaneado e já-resgatado ficam FORA do lote -----------------


def test_ocrescer_pula_nao_escaneados_e_ja_resgatados(tmp_path, monkeypatch) -> None:
    """Três perfis: nativo (flag 0), resgatado em ciclo anterior (``ocr_em``
    preenchido, flag ainda 1) e escaneado de verdade — só o terceiro roda OCR.
    """
    manifesto, contexto = _montar_cena_ocr(
        tmp_path,
        documentos=[
            ("ocrdoc00", "http://ocr.test/doc0.pdf", 0, "2026-05-05T00:00:00+00:00"),
            ("ocrdoc04", "http://ocr.test/doc4.pdf", 1, "2026-05-05T00:00:00+00:00"),
            ("ocrdoc03", "http://ocr.test/doc3.pdf", 1, None),
        ],
    )
    tesseract = TesseractFake([_resposta(palavras=[("Edital", 95)])])
    _instalar_fakes(monkeypatch, tesseract=tesseract, pdfium=PdfiumFake(paginas=1))

    resumo = ocrescer_portal(contexto, manifesto, confianca_minima=60, idioma="por")

    try:
        # doc0: flag 0 ⇒ pulado; doc4: flag 1 mas ocr_em ⇒ guard interno pula;
        # doc3: único que roda OCR de verdade e é resgatado
        assert resumo.escaneados == 2 and resumo.resgatados == 1
        assert resumo.pulados == 2 and resumo.falhas == 0

        (ja_resgatado,) = manifesto.consultar(
            "SELECT ocr_metodo, ocr_confianca_media FROM documentos WHERE id = 'ocrdoc04'"
        )
        assert ja_resgatado["ocr_metodo"] is None, "já-resgatado NUNCA reprocessa"
        assert ja_resgatado["ocr_confianca_media"] is None
    finally:
        manifesto.fechar()


# -- indisponibilidade: guard recusa com ErroOcrIndisponivel -------------------


def test_importar_ocr_recusa_sem_extra(monkeypatch) -> None:
    """Sem o extra 'ocr' sincronizado, _importar_ocr não engole o pacote."""

    def faltando(_nome: str) -> object:
        raise ImportError("extra 'ocr' ausente")

    monkeypatch.setattr("importlib.import_module", faltando)
    with pytest.raises(ErroOcrIndisponivel) as excinfo:
        _importar_ocr()
    assert "extra opcional 'ocr'" in str(excinfo.value)
    assert ocr_disponivel() is False


def test_ocr_uma_pagina_descarta_palavras_sem_confianca() -> None:
    """Conf -1 (palavra sem linha) fica FORA do texto e da média."""
    tesseract = TesseractFake([_resposta(palavras=[("Edital", 95), ("bleh", -1), ("nº", 85)])])
    texto, media = _ocr_uma_pagina(tesseract, object(), "por")
    assert texto == "Edital nº"
    assert media == 90.0


# -- CLI: guard e flags --------------------------------------------------------


def test_ocrescer_recusa_sem_flags(cli) -> None:
    """'ocrescer' sem --portal nem --todos é malformado (exit 2, antes do banco)."""
    resultado = cli.invoke(app, ["ocrescer"])
    assert resultado.exit_code == 2
    assert "ocrescer" in saida_cli(resultado)


def test_ocrescer_recusa_portal_mais_todos(cli) -> None:
    """Flags excludentes juntas também recusam (exit 2)."""
    resultado = cli.invoke(app, ["ocrescer", "--portal", "TST", "--todos"])
    assert resultado.exit_code == 2
    assert saida_cli(resultado)


def test_ocrescer_exit_2_sem_dependencias(cli, ambiente, monkeypatch) -> None:
    """Guard real do CLI: empilhamento indisponível ⇒ recusa com exit 2,
    sem tocar em nenhum documento."""
    import agente_editais.consulta as consulta_modulo

    (ambiente.configs / "politeness.toml").write_text(
        'delay_minimo_s = 0.0\noff_peak = "22:00-06:00"\nuser_agent = "teste"\n',
        encoding="utf-8",
    )
    from .conftest import MAPA_MINIMO, escrever_mapa

    escrever_mapa(
        ambiente,
        MAPA_MINIMO.format(
            sigla="TST", portal="TST", categoria="integra",
            url_portal="http://tst.test", seeds='"http://tst.test/"',
        ),
    )
    monkeypatch.setattr(consulta_modulo, "ocr_disponivel", lambda idioma: False)

    resultado = cli.invoke(app, ["ocrescer", "--portal", "TST"])
    assert resultado.exit_code == 2
    assert "OCR indisponível" in saida_cli(resultado)
