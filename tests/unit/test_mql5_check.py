"""O verificador estatico de MQL5, e a prova de que ele pega erro.

Os `.mq5` sao os unicos arquivos deste repositorio que chegam ao usuario
sem lint, sem tipo e sem teste — o compilador e proprietario e so existe
dentro do MetaEditor, que e Windows. Este modulo faz duas coisas:

1. roda o verificador sobre os fontes reais e exige zero erros;
2. prova, com mutacoes, que ele FALHA quando deveria.

O item 2 e o que importa. Um verificador que sempre passa nao vale nada,
e o jeito de saber a diferenca e quebrar o codigo de proposito.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path("scripts/mql5")
CHECKER = Path("scripts/mql5_check.py")


def _rodar(raiz: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(raiz)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def copia(tmp_path: Path) -> Path:
    """Os fontes reais num diretorio temporario, para mutar sem estrago."""
    destino = tmp_path / "mql5"
    shutil.copytree(RAIZ, destino)
    return destino


# --- os fontes de verdade ---------------------------------------------------


def test_the_real_sources_have_no_errors() -> None:
    resultado = _rodar(RAIZ)

    assert resultado.returncode == 0, resultado.stdout
    assert "ERRO" not in resultado.stdout


def test_the_hedge_includes_resolve() -> None:
    """Include nao resolvido e o erro mais barato de cometer e o mais caro
    de descobrir: o operador so ve "nao compila" na maquina dele."""
    saida = _rodar(RAIZ).stdout

    assert "HedgeChannel.mqh" in saida
    assert "HedgeJson.mqh" in saida


def test_the_checker_does_not_claim_to_be_a_compiler() -> None:
    """A saida precisa dizer o que ela NAO prova. Sem isso, "passou no
    verificador" vira "compila", e a diferenca aparece no F7 do
    operador."""
    saida = _rodar(RAIZ).stdout

    assert "NAO e um compilador" in saida


# --- a prova de que ele falha quando deve -----------------------------------


def _mutar(copia: Path, arquivo: str, de: str, para: str) -> None:
    alvo = copia / arquivo
    texto = alvo.read_text()
    assert de in texto, f"ancora da mutacao nao encontrada em {arquivo}"
    alvo.write_text(texto.replace(de, para, 1))


def test_a_wrong_argument_count_is_caught(copia: Path) -> None:
    """A familia de erro que ja escapou de uma revisao neste projeto."""
    _mutar(
        copia,
        "TraderTopHedgeConnector.mq5",
        'int codigo = Http(ApiUrl + "/api/v1/hedge/snapshot", corpo, resposta);',
        'int codigo = Http(ApiUrl + "/api/v1/hedge/snapshot", corpo);',
    )

    resultado = _rodar(copia)

    assert resultado.returncode == 1
    assert "'Http' recebe 3 argumento(s), mas foi chamada com 2" in resultado.stdout


def test_a_typo_in_a_function_name_is_caught(copia: Path) -> None:
    _mutar(
        copia,
        "TraderTopHedgeConnector.mq5",
        "PublicarEstado(TTH_ST_WAITING);",
        "PublicarEstadoo(TTH_ST_WAITING);",
    )

    saida = _rodar(copia).stdout

    assert "PublicarEstadoo" in saida


def test_a_typo_in_a_struct_field_is_caught(copia: Path) -> None:
    """Erro de digitacao no canal entre EA e indicador. Sem esta checagem
    ele apareceria como um valor zerado no grafico, sem erro nenhum."""
    _mutar(
        copia,
        "TraderTopHedgeConnector.mq5",
        "f.updated_at = (double)TimeCurrent();",
        "f.updated_att = (double)TimeCurrent();",
    )

    resultado = _rodar(copia)

    assert resultado.returncode == 1
    assert "nao declara o campo 'updated_att'" in resultado.stdout


def test_an_unbalanced_brace_is_caught(copia: Path) -> None:
    _mutar(
        copia,
        "TraderTopHedgeIndicator.mq5",
        "void OnTimer()\n  {\n   Atualizar();\n  }",
        "void OnTimer()\n  {\n   Atualizar();\n",
    )

    resultado = _rodar(copia)

    assert resultado.returncode == 1
    assert "desbalanceados" in resultado.stdout


def test_a_missing_include_is_caught(copia: Path) -> None:
    (copia / "Include" / "TraderTop" / "HedgeChannel.mqh").unlink()

    resultado = _rodar(copia)

    assert resultado.returncode == 1
    assert "#include nao resolvido" in resultado.stdout


def test_a_duplicated_function_is_caught(copia: Path) -> None:
    alvo = copia / "TraderTopHedgeIndicator.mq5"
    alvo.write_text(
        alvo.read_text()
        + "\n\nvoid Reancorar()\n  {\n   AncorarTempo(g_pfx + \"zona_take\");\n  }\n"
    )

    resultado = _rodar(copia)

    assert resultado.returncode == 1
    assert "definida mais de uma vez" in resultado.stdout


# --- calibragem -------------------------------------------------------------


def test_a_known_good_file_stays_clean(copia: Path) -> None:
    """`AITraderPulse.mq5` compila em producao ha meses. Se o verificador
    comecar a reclamar dele, o problema e do verificador — e e assim que
    um falso positivo e detectado antes de virar ruido que todos
    ignoram."""
    saida = _rodar(copia).stdout

    bloco = saida.split("AITraderPulse.mq5")[1].split("\n\n")[0]
    assert "ERRO" not in bloco
    assert "suspeita" not in bloco
