from __future__ import annotations

import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "logs"
EN_RE = re.compile(r"[A-Za-z]")


def iter_log_files() -> list[Path]:
    return sorted(LOG_DIR.rglob("*.log"))


def english_ratio(text: str) -> float:
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return 0.0
    english = sum(1 for ch in visible if EN_RE.fullmatch(ch))
    return english / len(visible)


def message_part(line: str) -> str:
    parts = line.split(" | ", 4)
    if len(parts) == 5:
        return parts[-1]
    return line


def main() -> None:
    files = iter_log_files()
    if not files:
        print("No log files found.")
        return

    explicit_errors: list[tuple[Path, int, str]] = []
    heavy_full: list[tuple[Path, int, float, str]] = []
    heavy_msg: list[tuple[Path, int, float, str]] = []
    tool_errors = Counter()

    for path in files:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.rstrip("\n")
                if not line.strip():
                    continue

                if " ERROR " in line or "| ERROR |" in line:
                    explicit_errors.append((path, lineno, line))
                    if "缺少工具" in line:
                        m = re.search(r"tool=([A-Za-z_][A-Za-z0-9_]*)", line)
                        if m:
                            tool_errors[m.group(1)] += 1

                full_ratio = english_ratio(line)
                if full_ratio > 0.8:
                    heavy_full.append((path, lineno, full_ratio, line))

                msg = message_part(line)
                msg_ratio = english_ratio(msg)
                if msg_ratio > 0.8:
                    heavy_msg.append((path, lineno, msg_ratio, line))

    print("== Files ==")
    for path in files:
        print(path.relative_to(ROOT))

    print("\n== Explicit ERROR Lines ==")
    for path, lineno, line in explicit_errors[:200]:
        print(f"{path.relative_to(ROOT)}:{lineno}: {line}")

    print("\n== English-heavy Lines (full line > 50%) ==")
    for path, lineno, ratio, line in heavy_full[:200]:
        print(f"{path.relative_to(ROOT)}:{lineno}: ratio={ratio:.2%} | {line}")

    print("\n== English-heavy Lines (message only > 50%) ==")
    for path, lineno, ratio, line in heavy_msg[:200]:
        print(f"{path.relative_to(ROOT)}:{lineno}: ratio={ratio:.2%} | {line}")

    print("\n== Missing Tools ==")
    for tool_name, count in tool_errors.most_common():
        print(f"{tool_name}: {count}")

    print("\n== Summary ==")
    print(f"explicit_errors={len(explicit_errors)}")
    print(f"heavy_full={len(heavy_full)}")
    print(f"heavy_msg={len(heavy_msg)}")


if __name__ == "__main__":
    main()
