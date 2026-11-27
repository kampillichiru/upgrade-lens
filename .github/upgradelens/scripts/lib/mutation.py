"""Small source mutations of one Java line, for the assertion-strength check of phase 04-tests (job asserts).

A mutant changes one changed line of change_branch in a way that changes behavior but still compiles in most code:
a flipped comparison, && and || swapped, a flipped boolean literal, a number + 1, a changed string literal, an
arithmetic operator swapped, or a call statement removed. If the existing tests still pass with the mutant, they
run the line but do not check what it does: their assertions are too weak to prove the change.

Pure functions: the stage script (phase11-asserts.py) applies a mutant, runs the tests and restores the file.
"""

from __future__ import annotations

import re

# Lines that are not behavior: comments, imports, annotations, declarations, braces.
_SKIP = re.compile(r"^\s*(//|/\*|\*|import\s|package\s|@|\}|\{|$)")
_DECLARATION = re.compile(r"^\s*(public|protected|private|static|final|abstract|synchronized|class|interface|enum|record)\b"
                          r"[^=;]*(\(|\{)\s*$")
_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)\'')
_CALL_STATEMENT = re.compile(r"^(\s*)(?!return\b|throw\b|new\b|this\s*\(|super\s*\()[\w.$]+(?:\([^;]*\))(?:\.[\w$]+\([^;]*\))*\s*;\s*$")

# (operator name, pattern on the code outside literals, replacement function)
_SWAPS = [
    ("negate-equality", re.compile(r"(?<![=!<>])==(?!=)"), lambda m: "!="),
    ("negate-equality", re.compile(r"!=(?!=)"), lambda m: "=="),
    ("boundary", re.compile(r"<=(?!=)"), lambda m: ">"),
    ("boundary", re.compile(r">=(?!=)"), lambda m: "<"),
    ("boundary", re.compile(r"(?<=\s)<(?=\s)"), lambda m: ">="),
    ("boundary", re.compile(r"(?<=\s)>(?=\s)"), lambda m: "<="),
    ("and-or", re.compile(r"&&"), lambda m: "||"),
    ("and-or", re.compile(r"\|\|"), lambda m: "&&"),
    ("boolean", re.compile(r"\btrue\b"), lambda m: "false"),
    ("boolean", re.compile(r"\bfalse\b"), lambda m: "true"),
    ("number", re.compile(r"(?<![\w.$])(\d+)(?![\w.])"), lambda m: str(int(m.group(1)) + 1)),
    ("arithmetic", re.compile(r"(?<=\s)\+(?=\s)"), lambda m: "-"),
    ("arithmetic", re.compile(r"(?<=\s)-(?=\s)"), lambda m: "+"),
    ("arithmetic", re.compile(r"(?<=\s)\*(?=\s)"), lambda m: "/"),
]


def _literal_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _LITERAL.finditer(text)]


def _outside(spans, start: int) -> bool:
    return not any(a <= start < b for a, b in spans)


def mutable(text: str) -> bool:
    """Whether a source line is behavior a mutation can change (not a comment, import, annotation or declaration)."""
    return not _SKIP.search(text) and not _DECLARATION.search(text)


def line_mutants(text: str) -> list[dict]:
    """The mutants of one line, most telling first: {operator, mutated}. Each changes exactly one place."""
    if not mutable(text):
        return []
    out, seen = [], set()
    spans = _literal_spans(text)
    for name, rx, repl in _SWAPS:
        for m in rx.finditer(text):
            if not _outside(spans, m.start()):
                continue
            # A '+' next to a string literal is a concatenation: '-' would not compile, so skip it.
            if name == "arithmetic" and spans:
                continue
            mutated = text[:m.start()] + repl(m) + text[m.end():]
            if mutated != text and mutated not in seen:
                seen.add(mutated)
                out.append({"operator": name, "mutated": mutated})
            break
    for a, b in spans:
        lit = text[a:b]
        if lit.startswith('"'):
            mutated = text[:b - 1] + "_UL" + text[b - 1:]
            if mutated not in seen:
                seen.add(mutated)
                out.append({"operator": "string", "mutated": mutated})
            break
    m = _CALL_STATEMENT.match(text)
    # A whole statement only: not the continuation of a chained call (".toList();") or of an argument list
    # ("b, c);"), where removing the line leaves code that does not compile.
    code = _LITERAL.sub('""', text)
    whole = not text.lstrip().startswith(".") and code.count("(") == code.count(")")
    if m and whole and "=" not in text.split("(")[0]:
        mutated = f"{m.group(1)};"
        if mutated not in seen:
            out.append({"operator": "remove-call", "mutated": mutated})
    return out


def plan_mutants(lines_by_number: dict, line_numbers, per_method: int) -> list[dict]:
    """Mutants for one changed method: one per changed covered line, in line order, at most per_method.
    lines_by_number: head line number -> source text."""
    plan = []
    for n in sorted(set(line_numbers)):
        text = lines_by_number.get(n)
        if text is None:
            continue
        ms = line_mutants(text)
        if ms:
            plan.append({"line": n, "original": text.strip(), "operator": ms[0]["operator"], "mutatedLine": ms[0]["mutated"],
                         "mutated": ms[0]["mutated"].strip()})
        if len(plan) >= per_method:
            break
    return plan


def method_strength(mutants) -> str:
    """strong: every mutant that compiled made a test fail. weak: one compiled and no test failed.
    unknown: none compiled or none could be made."""
    results = [m.get("result") for m in mutants]
    if "survived" in results:
        return "weak"
    if any(r in ("killed", "timeout") for r in results):
        return "strong"
    return "unknown"
