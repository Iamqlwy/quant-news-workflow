from __future__ import annotations

import ast
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build", "data"}
CALL_NAMES = {
    "strftime",
    "strptime",
    "isoformat",
    "fromisoformat",
    "to_datetime",
    "Timestamp",
    "date_range",
    "localtime",
}


def iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def analyze_file(path: Path) -> list[dict]:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source = path.read_text(encoding="utf-8-sig")

    tree = ast.parse(source, filename=str(path))
    rows: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted_name(node.func)
        short = name.split(".")[-1] if name else None
        if short not in CALL_NAMES:
            continue

        format_literal = None
        unit_literal = None
        if short in {"strftime", "strptime"} and len(node.args) >= 1:
            format_literal = literal_str(node.args[-1])
        elif short == "to_datetime":
            for kw in node.keywords:
                if kw.arg == "format":
                    format_literal = literal_str(kw.value)
                if kw.arg == "unit":
                    unit_literal = literal_str(kw.value)
        elif short == "date_range":
            for kw in node.keywords:
                if kw.arg == "freq":
                    unit_literal = literal_str(kw.value)

        rows.append(
            {
                "file": str(path.relative_to(ROOT)),
                "line": getattr(node, "lineno", 0),
                "call": name or "<unknown>",
                "format": format_literal,
                "unit": unit_literal,
            }
        )
    return rows


def main() -> None:
    rows: list[dict] = []
    for path in iter_python_files(ROOT):
        try:
            rows.extend(analyze_file(path))
        except SyntaxError as exc:
            print(f"[skip] {path}: {exc}")

    by_call = Counter(row["call"].split(".")[-1] for row in rows)
    by_format = Counter(row["format"] for row in rows if row["format"])
    by_unit = Counter(row["unit"] for row in rows if row["unit"])
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["call"].split(".")[-1]].append(row)

    print("== Call Summary ==")
    for name, count in sorted(by_call.items()):
        print(f"{name}: {count}")

    print("\n== Format Literals ==")
    for fmt, count in by_format.most_common():
        print(f"{fmt}: {count}")

    print("\n== Units / Freq ==")
    for item, count in by_unit.most_common():
        print(f"{item}: {count}")

    print("\n== Details ==")
    for call_name in sorted(grouped):
        print(f"\n[{call_name}]")
        for row in sorted(grouped[call_name], key=lambda item: (item["file"], item["line"])):
            extra = []
            if row["format"]:
                extra.append(f"format={row['format']}")
            if row["unit"]:
                extra.append(f"unit={row['unit']}")
            extra_text = f" ({', '.join(extra)})" if extra else ""
            print(f"{row['file']}:{row['line']} {row['call']}{extra_text}")


if __name__ == "__main__":
    main()
