import re
from pathlib import Path


FILES = (
    Path("ultima_live.txt"),
    Path("ultima_live_btg.txt"),
    Path("ultimo_video_clube_dos_dividendos.txt"),
)

# O 2outube pode devolver a transcrição praticamente inteira em uma única linha,
# com cada timestamp colado à fala seguinte, por exemplo:
#   5:18Fala galera...6:12Sobe descansa...
#
# A regex abaixo reconhece somente timestamps colados diretamente a uma fala
# (letra Unicode, '[' ou '>'). Assim, horários normais citados no discurso,
# como "às 9:15 decisão do BCE", não são quebrados quando existe espaço após
# o horário.
GLUED_TIMESTAMP_RE = re.compile(
    r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?=(?:[^\W\d_]|[\[>]))",
    flags=re.UNICODE,
)

TIMESTAMP_LINE_RE = re.compile(
    r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s+"
)


def semantic_fingerprint(text: str) -> str:
    """Remove apenas whitespace para provar que nenhuma palavra foi alterada."""
    return re.sub(r"\s+", "", text or "")


def format_transcript_body(body: str) -> str:
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n")

    def repl(match: re.Match) -> str:
        start = match.start()
        prefix = "" if start == 0 or body[start - 1] == "\n" else "\n"
        return f"{prefix}{match.group(1)} "

    formatted = GLUED_TIMESTAMP_RE.sub(repl, body)
    formatted = "\n".join(line.rstrip() for line in formatted.splitlines())
    formatted = re.sub(r"\n{4,}", "\n\n\n", formatted)
    return formatted.strip()


def process_file(path: Path) -> bool:
    if not path.exists():
        print(f"IGNORADO: {path} não existe.")
        return False

    original = path.read_text(encoding="utf-8", errors="strict")

    if "\n\n" not in original:
        raise RuntimeError(
            f"{path}: estrutura inesperada; não encontrei separação entre "
            "metadados e transcrição."
        )

    header, body = original.split("\n\n", 1)
    formatted_body = format_transcript_body(body)

    if semantic_fingerprint(body) != semantic_fingerprint(formatted_body):
        raise RuntimeError(
            f"{path}: a formatação alteraria conteúdo não-whitespace; operação abortada."
        )

    updated = f"{header}\n\n{formatted_body}\n"

    if updated == original:
        timestamp_lines = len(TIMESTAMP_LINE_RE.findall(formatted_body))
        print(
            f"SEM ALTERAÇÃO: {path} já está formatado "
            f"({timestamp_lines} linhas com timestamp)."
        )
        return False

    temp_path = path.with_name(f"{path.stem}_formatado{path.suffix}")
    temp_path.write_text(updated, encoding="utf-8")

    check = temp_path.read_text(encoding="utf-8", errors="strict")
    check_header, check_body = check.split("\n\n", 1)

    if check_header != header:
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"{path}: metadados foram alterados inesperadamente.")

    if semantic_fingerprint(check_body) != semantic_fingerprint(body):
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"{path}: falha na validação final de integridade.")

    temp_path.replace(path)

    timestamp_lines = len(TIMESTAMP_LINE_RE.findall(formatted_body))
    total_lines = len(updated.splitlines())
    print(
        f"FORMATADO: {path} | timestamps em linhas={timestamp_lines} | "
        f"linhas totais={total_lines}"
    )
    return True


def main():
    changed = 0

    for path in FILES:
        if process_file(path):
            changed += 1

    print("")
    print(f"Arquivos formatados nesta execução: {changed}")


if __name__ == "__main__":
    main()
