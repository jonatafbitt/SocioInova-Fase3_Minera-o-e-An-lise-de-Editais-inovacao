"""Dicionário e utilidades de limpeza textual para análise de editais.

Expansão da lista de stopwords e conectivos multiword para remover
verbos, conjunções, preposições, boilerplate de editais, termos
institucionais da rede federal, temporalidade e artefatos digitais/OCR
de nuvens e funções de análise textual.

As stopwords são mantidas na forma acentuada (é assim que o texto
normalizado do corpus chega ao CountVectorizer) e, para os termos
domínio-específicos provenientes da curadoria SocioInova, também na
variante sem acento — cobre o texto resgatado por OCR, que pode chegar
sem diacríticos.
"""

from __future__ import annotations

import re

# Token exigindo início por LETRA: exclui números puros ("2025", "2024")
# da nuvem, dos top-termos e do TF-IDF.
TOKEN_PATTERN = r"(?u)\b[^\W\d_]\w+\b"

# ──────────────────────────────────────────────────────────────────────────────
# Versões (rastreabilidade — directive 404)
# ──────────────────────────────────────────────────────────────────────────────
STOPWORDS_VERSAO: int = 3
CONECTIVOS_VERSAO: int = 1

# ──────────────────────────────────────────────────────────────────────────────
# Stopwords PT expandidas (frozenset para uso em CountVectorizer/TfidfVectorizer)
# ──────────────────────────────────────────────────────────────────────────────
_STOPWORDS_BASE = """
a ao aos as e é em na no nas nos o os um uma umas uns ou com como
sem que se não nem mais menos mas ainda já muito muito muito muitos
toda todo todas todos outro outros outra outras cada qual quais
para pela pelas pelo pelos por este esta estes estas esse essa esses
essas aquele aquela aqueles aquelas isto isso aquilo algo alguém
nada ninguém tudo todos sempre nunca também embora sobre sob até
após desde entre dentro fora acima abaixo antes depois durante
mediante perante contra frente trás da das de do dos à às seu seus
sua suas dele dela deles delas neste nesta nesses nessas naquele
naquela naqueles naquelas seja sejam ser sendo sido está estão
eram foi foram fazer feito tinha tinham então quando enquanto
porque porém contudo portanto onde cujo cuja cujos cujas ao qual
aos quais no qual na qual nos quais nas quais em editais edital
chamada chamadas
"""

_STOPWORDS_EXPANDIDAS = """
# preposições e locuções prepositivas
junto conforme segundo consoante mediante exceto salvo acerca
relativo em virtude a partir até então

# conjunções advérbios conectivos locuções conjuntivas
assim então entretanto portanto outrossim igualmente contudo
apesar todavia contanto desde logo

# verbos auxiliares e formas verbais frequentes em editais boilerplate
deve devem será ser estar estão pode podendo realizado realizar
objetiva atuar atuarem torna torna-se torna pública torna publico
realizada realizada realizou realizar-se realizará realizarão
realizam realizando realizados realizou realizar-se-ão
iniciar iniciará iniciar-se iniciados iniciou inicia iniciar-se
conduzido conduzida conduzidos conduzidas conduzir conduzindo
conduziu conduzirá conduzirão realizam realizaram realizar-se
respeita respeitam referem refere referindo referido referidos
referidas referida referente referentes conforme segue seguem

# boilerplate genérico de editais não analiticamente relevante
processo seletivo inscrição candidato documento documentação
prazo edital chamada curso público presente anexo exemplo
vagas vaga seleção selecionado selecionados selecionar
publicação publicado publicar divulgação divulgar homologação
homologar resultado resultado-final resultado-preliminar
classificação classificado classificados classificar
dispõe dispor item items subitem subitens
inciso incisos alínea alíneas parágrafo parágrafos artigo
artigos capítulo capítulos seção seções título títulos
lei decreto resolução portaria norma normas regimento
regimento interno conselho comitê colegiado coordenador
coordenação coordenadores diretoria diretor superintendência
superintendente gerência gerente assessoria assessor
secretaria secretário servidores servidor técnico administrativo
bolsa bolsas auxílio auxílios
termo termos termo de referência termo-de-referência
cronograma cronogramas etapa etapas
"""

# ──────────────────────────────────────────────────────────────────────────────
# Camada domínio-específica (curadoria SocioInova): institucional da rede
# federal, burocracia normativa, acadêmico/citação, temporal e artefatos
# digitais/OCR. Portada com acentuação correta (+ variante sem acento, útil
# para texto resgatado por OCR). Não repete termos já cobertos na camada
# expandida acima. A string contém só palavras — sem comentários, para não
# vazar tokens ao .split().
# ──────────────────────────────────────────────────────────────────────────────
_STOPWORDS_SOCIOINOVA = """
caput apêndice apendice dou diário diario oficial sei nº número numero
vigor revogam revogadas disposições disposicoes cumprimento regulamento
minuta despacho considerando resolve estabelece certame constante
disposto art

instituto federal campus campi reitoria reitor reitora diretora pró pro
coordenadoria departamento ministério ministerio mec setec conif ifba
ifes ifrj ifsp ifpe ifrn ifce ifpb ifal ifs ifpi ifma ifto ifpa ifap
ifac ifam ifrr ifro ifmt ifms ifg ifgoiano ifb ifsc ifsul ifpr ifc cefet
utfpr cp2 ufba ufrb

resumo abstract introdução introducao metodologia conclusão conclusao
referências referencias bibliografia keywords palavras chave autor autores
et al vol edição edicao editora universidade faculdade tese dissertação
dissertacao periódico periodico revista pag pp doi

janeiro fevereiro março marco abril maio junho julho agosto setembro
outubro novembro dezembro ano mês mes dia data horas hrs cpf cnpj cep rg
local

cid www http https br gov edu org com página pagina

fazer fez fazem dever podem ter tendo haver houver promover promoverá
promovera criar criado apresentar apresentado enviar através atraves
"""

STOPWORDS_PT: frozenset[str] = frozenset(
    (_STOPWORDS_BASE + _STOPWORDS_EXPANDIDAS + _STOPWORDS_SOCIOINOVA).split()
)

# ──────────────────────────────────────────────────────────────────────────────
# Conectivos multiword (expressões que escapam do filtro unigram do CountVectorizer)
# São removidos ANTES da vetorização via substituição por espaço.
# ──────────────────────────────────────────────────────────────────────────────
CONECTIVOS_MULTIWORD: tuple[str, ...] = (
    "bem como",
    "por meio de",
    "no âmbito",
    "a fim de",
    "de acordo com",
    "através de",
    "além de",
    "tendo em vista",
    "com base em",
    "com relação a",
    "em relação a",
    "no que se refere",
    "no que tange",
    "no tocante a",
    "em virtude de",
    "a partir de",
    "em face de",
    "por força de",
    "em decorrência de",
    "uma vez que",
    "visto que",
    "dado que",
    "já que",
    "uma vez",
    "sem prejuízo de",
    "independentemente de",
    "mediante",
    "conforme",
    "consoante",
)

# ──────────────────────────────────────────────────────────────────────────────
# Compilado para substituição eficiente (case-insensitive, word boundaries flexíveis)
# ──────────────────────────────────────────────────────────────────────────────
_CONECTIVOS_REGEX = re.compile(
    r"\b(?:" + "|".join(re.escape(c) for c in CONECTIVOS_MULTIWORD) + r")\b",
    flags=re.IGNORECASE,
)


def limpar_texto(texto: str) -> str:
    """Normaliza e remove conectivos multiword do texto.

    - Converte para minúsculas
    - Colapsa espaços em branco
    - Remove conectivos multiword (substitui por espaço único)
    """
    if not texto:
        return ""
    t = texto.lower()
    t = _CONECTIVOS_REGEX.sub(" ", t)
    t = " ".join(t.split())
    return t


def limpar_textos(id_textos: dict[str, str]) -> dict[str, str]:
    """Aplica limpar_texto a um dicionário id -> texto."""
    return {doc_id: limpar_texto(txt) for doc_id, txt in id_textos.items()}


def info_versao() -> dict:
    """Retorna metadados de versão para relatório/ficha técnica."""
    return {
        "stopwords_versao": STOPWORDS_VERSAO,
        "conectivos_versao": CONECTIVOS_VERSAO,
        "n_stopwords": len(STOPWORDS_PT),
        "n_conectivos_multiword": len(CONECTIVOS_MULTIWORD),
    }
