#!/usr/bin/env python3
"""Verificador estatico de MQL5 — o que da para checar sem o compilador.

## Por que isto existe

O compilador MQL5 e proprietario: so existe dentro do MetaEditor, que e
Windows e nao e distribuido separadamente. Em CI, e neste repositorio, os
`.mq5` sao os unicos arquivos que chegam ao usuario **sem nenhuma
verificacao automatica** — sem lint, sem tipo, sem teste. A unica defesa
e revisao humana, e ela ja deixou passar um erro de tipo neste projeto
(`NotifyIfReady(velho, vies)`, string onde se esperava bool).

Este script cobre a familia de erros que mais aparece ao escrever MQL5
sem compilador por perto:

- delimitadores desbalanceados;
- chamada a funcao que nao existe (erro de digitacao no nome);
- numero errado de argumentos numa funcao definida no proprio codigo;
- `#include` que aponta para arquivo inexistente;
- campo de struct que nao existe (erro de digitacao no canal);
- funcao definida duas vezes.

## O que ele NAO e

**Nao e um compilador.** Nao verifica tipos, nao resolve sobrecarga, nao
conhece a biblioteca padrao inteira, nao avalia macros. Passar aqui NAO
significa que compila — significa que esta familia de erros nao esta
presente. O F7 continua sendo necessario.

## Como ele evita falso positivo

O conjunto de nomes conhecidos vem de tres fontes: as funcoes definidas
nos proprios arquivos analisados, os `#include` resolvidos, e uma lista
de builtins do MQL5. Um nome fora das tres e reportado como SUSPEITA, nao
como erro — porque a lista de builtins e necessariamente incompleta, e um
verificador que grita sobre a biblioteca padrao seria desligado na
primeira semana.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- lexer ------------------------------------------------------------------
#
# Uma passagem so. Regex em varias passagens erra de um jeito especifico e
# silencioso: `//` dentro de "https://" vira comentario e desalinha as
# aspas do resto do arquivo, produzindo um balanco falso.


def strip_code(src: str) -> str:
    """Remove comentarios e literais, preservando posicao de linha."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
        elif c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            i += 1
            out.append('""')
        elif c == "'":
            i += 1
            while i < n and src[i] != "'":
                i += 2 if src[i] == "\\" else 1
            i += 1
            out.append("'c'")
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --- o que conhecemos -------------------------------------------------------

KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default", "return",
    "break", "continue", "sizeof", "new", "delete", "operator", "template",
    "struct", "class", "enum", "union", "public", "private", "protected",
    "virtual", "const", "static", "extern", "input", "sinput", "void", "int",
    "uint", "long", "ulong", "short", "ushort", "char", "uchar", "bool",
    "double", "float", "string", "datetime", "color", "typename", "this",
}

TYPES = {
    "void", "int", "uint", "long", "ulong", "short", "ushort", "char", "uchar",
    "bool", "double", "float", "string", "datetime", "color",
}

# Builtins do MQL5 efetivamente usados neste repositorio, mais os vizinhos
# obvios. Lista curada de proposito: cobrir a API inteira seria impossivel
# de manter, e o relatorio separa "suspeita" de "erro" justamente porque
# ela e incompleta.
BUILTINS = {
    # strings
    "StringLen", "StringFind", "StringSubstr", "StringGetCharacter", "StringFormat",
    "StringToDouble", "StringToInteger", "StringTrimLeft", "StringTrimRight",
    "StringReplace", "StringConcatenate", "StringSplit", "StringToCharArray",
    "CharArrayToString", "IntegerToString", "DoubleToString", "TimeToString",
    "StringAdd", "StringInit", "StringCompare", "ShortToString",
    "CharToString", "StringSetCharacter", "StringToUpper", "StringToLower",
    # math
    "MathAbs", "MathMax", "MathMin", "MathPow", "MathSqrt", "MathRound",
    "MathFloor", "MathCeil", "MathMod", "MathLog", "MathExp", "MathSin",
    "MathCos", "MathTan", "MathRand", "MathSrand", "NormalizeDouble",
    # arrays
    "ArrayResize", "ArraySize", "ArrayFree", "ArrayCopy", "ArrayInitialize",
    "ArraySetAsSeries", "ArrayMaximum", "ArrayMinimum", "ArraySort", "ArrayFill",
    # tempo
    "TimeCurrent", "TimeLocal", "TimeGMT", "TimeTradeServer", "TimeToStruct",
    "StructToTime", "TimeDaylightSavings", "TimeGMTOffset",
    "PeriodSeconds", "GetTickCount", "GetMicrosecondCount",
    # grafico e objetos
    "ChartID", "ChartRedraw", "ChartGetInteger", "ChartGetDouble",
    "ChartSetInteger", "ChartSetString", "ChartPeriod", "ChartSymbol",
    "ObjectCreate", "ObjectDelete", "ObjectFind", "ObjectName", "ObjectsTotal",
    "ObjectSetInteger", "ObjectSetDouble", "ObjectSetString",
    "ObjectGetInteger", "ObjectGetDouble", "ObjectGetString", "ObjectMove",
    "ObjectsDeleteAll",
    # indicadores
    "iATR", "iMA", "iRSI", "iTime", "iOpen", "iHigh", "iLow", "iClose",
    "iVolume", "CopyBuffer", "CopyRates", "CopyTime", "CopyClose", "Bars",
    "IndicatorRelease", "IndicatorSetString", "IndicatorSetInteger",
    "IndicatorSetDouble", "SetIndexBuffer", "PlotIndexSetInteger",
    # simbolo e conta
    "SymbolInfoTick", "SymbolInfoDouble", "SymbolInfoInteger", "SymbolInfoString",
    "SymbolSelect", "SymbolsTotal", "SymbolName",
    "AccountInfoInteger", "AccountInfoDouble", "AccountInfoString",
    "TerminalInfoInteger", "TerminalInfoString", "MQLInfoInteger", "MQLInfoString",
    # variaveis globais do terminal
    "GlobalVariableSet", "GlobalVariableGet", "GlobalVariableCheck",
    "GlobalVariableDel", "GlobalVariablesDeleteAll", "GlobalVariablesTotal",
    "GlobalVariableName", "GlobalVariableTime",
    # eventos e io
    "EventSetTimer", "EventKillTimer", "EventSetMillisecondTimer",
    "WebRequest", "Print", "PrintFormat", "Alert", "Comment", "SendNotification",
    "ResetLastError", "GetLastError", "Sleep", "ExpertRemove",
    "FileOpen", "FileClose", "FileWrite", "FileWriteString", "FileRead",
    "FileReadString", "FileDelete", "FileIsExist", "FolderCreate",
    "FileWriteArray", "FileReadArray", "FileMove", "FileCopy", "FileSeek",
    "FileSize", "FileFlush",
    # calendario
    "CalendarValueHistory", "CalendarEventById", "CalendarCountryById",
    # ordens (presentes na API; a ausencia delas no codigo e checada em teste)
    "OrderSend", "OrderCalcProfit", "OrderCalcMargin", "PositionsTotal",
    "PositionGetTicket", "PositionGetDouble", "PositionGetInteger",
    "PositionSelectByTicket",
}

HANDLERS = {
    "OnInit", "OnDeinit", "OnTick", "OnTimer", "OnCalculate", "OnChartEvent",
    "OnStart", "OnTrade", "OnTradeTransaction", "OnBookEvent", "OnTester",
}


@dataclass
class Func:
    name: str
    min_args: int
    max_args: int
    line: int
    file: str


@dataclass
class Finding:
    level: str          # ERRO | SUSPEITA
    file: str
    line: int
    message: str


@dataclass
class Unit:
    """Um `.mq5` com seus includes resolvidos."""

    path: Path
    sources: dict[str, str] = field(default_factory=dict)
    funcs: dict[str, Func] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


# --- analise ----------------------------------------------------------------

FUNC_DEF = re.compile(
    r"^[ \t]*(?:static[ \t]+)?([A-Za-z_][\w]*)[ \t]+([A-Za-z_]\w*)[ \t]*\(([^;{]*)\)[ \t]*$",
    re.MULTILINE,
)
"""Definicao: tipo nome(params) numa linha, com `{` na linha seguinte —
o estilo deste repositorio. Prototipos (terminados em `;`) ficam de fora
pelo `[^;{]`."""

CALL = re.compile(r"\b([A-Za-z_]\w*)[ \t]*\(")
INCLUDE = re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]', re.MULTILINE)
STRUCT_DEF = re.compile(r"\bstruct\s+(\w+)\s*\{", re.MULTILINE)


def split_args(raw: str) -> list[str]:
    """Separa argumentos no nivel zero de parenteses."""
    partes, nivel, atual = [], 0, []
    for ch in raw:
        if ch in "([{":
            nivel += 1
        elif ch in ")]}":
            nivel -= 1
        if ch == "," and nivel == 0:
            partes.append("".join(atual))
            atual = []
        else:
            atual.append(ch)
    resto = "".join(atual).strip()
    if resto:
        partes.append(resto)
    return [p.strip() for p in partes if p.strip()]


def collect_funcs(nome_arquivo: str, limpo: str, unit: Unit) -> None:
    for m in FUNC_DEF.finditer(limpo):
        tipo, nome, params = m.group(1), m.group(2), m.group(3)
        if tipo in KEYWORDS - TYPES or nome in KEYWORDS:
            continue
        linha = limpo[: m.start()].count("\n") + 1

        args = split_args(params)
        if len(args) == 1 and args[0] == "void":
            args = []
        obrigatorios = sum(1 for a in args if "=" not in a)

        if nome in unit.funcs and unit.funcs[nome].file == nome_arquivo:
            unit.findings.append(
                Finding(
                    "ERRO", nome_arquivo, linha,
                    f"funcao '{nome}' definida mais de uma vez "
                    f"(antes na linha {unit.funcs[nome].line})",
                )
            )
            continue

        unit.funcs[nome] = Func(nome, obrigatorios, len(args), linha, nome_arquivo)


def check_calls(nome_arquivo: str, limpo: str, unit: Unit) -> None:
    for m in CALL.finditer(limpo):
        nome = m.group(1)
        if nome in KEYWORDS or nome in TYPES:
            continue

        linha = limpo[: m.start()].count("\n") + 1

        # A definicao em si casa com o padrao de chamada; pular.
        inicio_linha = limpo.rfind("\n", 0, m.start()) + 1
        prefixo = limpo[inicio_linha : m.start()].strip()
        if prefixo and prefixo.split()[-1] in TYPES:
            continue

        if nome in BUILTINS or nome in HANDLERS:
            continue

        if nome not in unit.funcs:
            unit.findings.append(
                Finding(
                    "SUSPEITA", nome_arquivo, linha,
                    f"'{nome}' e chamada mas nao foi definida nos arquivos "
                    "analisados nem esta na lista de builtins conhecidos",
                )
            )
            continue

        # Aridade: so para funcoes definidas AQUI, onde sabemos a verdade.
        fecha = encontrar_fecha(limpo, m.end() - 1)
        if fecha < 0:
            continue
        passados = len(split_args(limpo[m.end() : fecha]))
        alvo = unit.funcs[nome]
        if not (alvo.min_args <= passados <= alvo.max_args):
            faixa = (
                str(alvo.min_args)
                if alvo.min_args == alvo.max_args
                else f"{alvo.min_args}..{alvo.max_args}"
            )
            unit.findings.append(
                Finding(
                    "ERRO", nome_arquivo, linha,
                    f"'{nome}' recebe {faixa} argumento(s), mas foi chamada "
                    f"com {passados}",
                )
            )


def encontrar_fecha(texto: str, abre: int) -> int:
    nivel = 0
    for i in range(abre, len(texto)):
        if texto[i] == "(":
            nivel += 1
        elif texto[i] == ")":
            nivel -= 1
            if nivel == 0:
                return i
    return -1


def check_balance(nome_arquivo: str, limpo: str, unit: Unit) -> None:
    for abre, fecha in ("{}", "()", "[]"):
        delta = limpo.count(abre) - limpo.count(fecha)
        if delta:
            unit.findings.append(
                Finding(
                    "ERRO", nome_arquivo, 0,
                    f"delimitadores desbalanceados: {abre}{fecha} difere em {delta:+d}",
                )
            )


def check_struct_fields(nome_arquivo: str, limpo: str, unit: Unit, structs: dict) -> None:
    """Campo acessado que a struct nao declara.

    Pega o erro de digitacao no canal entre EA e indicador, que de outra
    forma so apareceria como um valor zerado no grafico.
    """
    for var, tipo in re.findall(r"\b(\w+)\s+(\w+)\s*;", limpo):
        if var not in structs:
            continue
        for uso in re.findall(rf"\b{re.escape(tipo)}\.(\w+)", limpo):
            if uso not in structs[var]:
                unit.findings.append(
                    Finding(
                        "ERRO", nome_arquivo, 0,
                        f"'{tipo}.{uso}': a struct {var} nao declara o campo '{uso}'",
                    )
                )


def collect_structs(limpo: str) -> dict[str, set[str]]:
    structs: dict[str, set[str]] = {}
    for m in STRUCT_DEF.finditer(limpo):
        nome = m.group(1)
        fim = encontrar_chave(limpo, m.end() - 1)
        corpo = limpo[m.end() : fim] if fim > 0 else ""
        campos = set(re.findall(r"\b\w+\s+(\w+)\s*;", corpo))
        structs[nome] = campos
    return structs


def encontrar_chave(texto: str, abre: int) -> int:
    nivel = 0
    for i in range(abre, len(texto)):
        if texto[i] == "{":
            nivel += 1
        elif texto[i] == "}":
            nivel -= 1
            if nivel == 0:
                return i
    return -1


def analisar(caminho: Path, include_dirs: list[Path]) -> Unit:
    unit = Unit(path=caminho)
    pendentes = [caminho]
    vistos: set[Path] = set()

    while pendentes:
        atual = pendentes.pop(0)
        if atual in vistos:
            continue
        vistos.add(atual)

        if not atual.exists():
            unit.findings.append(
                Finding("ERRO", str(caminho.name), 0, f"#include nao encontrado: {atual}")
            )
            continue

        bruto = atual.read_text(encoding="utf-8", errors="replace")
        limpo = strip_code(bruto)
        unit.sources[atual.name] = limpo

        for alvo in INCLUDE.findall(limpo if "#include" in limpo else bruto):
            achado = next(
                (d / alvo for d in include_dirs if (d / alvo).exists()), None
            )
            if achado is None:
                unit.findings.append(
                    Finding("ERRO", atual.name, 0, f"#include nao resolvido: {alvo}")
                )
            else:
                pendentes.append(achado)

    todas = "\n".join(unit.sources.values())
    structs = collect_structs(todas)

    for nome, limpo in unit.sources.items():
        check_balance(nome, limpo, unit)
        collect_funcs(nome, limpo, unit)

    for nome, limpo in unit.sources.items():
        check_calls(nome, limpo, unit)
        check_struct_fields(nome, limpo, unit, structs)

    return unit


def main(argv: list[str]) -> int:
    raiz = Path(argv[1]) if len(argv) > 1 else Path("scripts/mql5")
    include_dirs = [raiz / "Include", raiz]

    total_erros = 0
    for arquivo in sorted(raiz.glob("*.mq5")):
        unit = analisar(arquivo, include_dirs)
        erros = [f for f in unit.findings if f.level == "ERRO"]
        suspeitas = [f for f in unit.findings if f.level == "SUSPEITA"]
        total_erros += len(erros)

        marca = "FALHOU" if erros else "ok"
        print(f"\n{arquivo.name}  [{marca}]")
        print(f"  arquivos: {', '.join(sorted(unit.sources))}")
        print(f"  funcoes definidas: {len(unit.funcs)}")

        for f in erros:
            print(f"  ERRO      {f.file}:{f.line}  {f.message}")
        for f in suspeitas:
            print(f"  suspeita  {f.file}:{f.line}  {f.message}")

    print(
        "\nNAO e um compilador: nao verifica tipos nem conhece a API inteira.\n"
        "Passar aqui significa que esta familia de erros nao esta presente."
    )
    return 1 if total_erros else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
