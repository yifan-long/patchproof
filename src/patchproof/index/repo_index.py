"""Static, line-addressable repository index used to build model context."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..workspace.artifact_policy import is_denied_artifact

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "data",
}
SUPPORTED_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".vue", ".md"}


@dataclass
class Symbol:
    path: str
    name: str
    kind: str
    line: int
    end_line: int
    doc: str = ""


@dataclass
class RepoIndex:
    root: Path
    files: list[str] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def build(cls, root: Path, max_files: int = 5000) -> RepoIndex:
        index = cls(root=root)
        for path in sorted(_iter_files(root))[:max_files]:
            rel = path.relative_to(root).as_posix()
            index.files.append(rel)
            if path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            module = rel[:-3].replace("/", ".").replace(".", ".")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    index.symbols.append(
                        Symbol(
                            path=rel,
                            name=node.name,
                            kind=kind,
                            line=node.lineno,
                            end_line=getattr(node, "end_lineno", node.lineno),
                            doc=ast.get_docstring(node) or "",
                        )
                    )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        index.edges.append({"source": rel, "relation": "imports", "target": alias.name})
                elif isinstance(node, ast.ImportFrom):
                    target = node.module or ""
                    index.edges.append({"source": rel, "relation": "imports", "target": target})
            index.edges.append({"source": rel, "relation": "contains", "target": module})
        return index

    def context_for(self, goal: str, max_symbols: int = 30) -> str:
        tokens = _tokens(goal)
        ranked: list[tuple[int, Symbol]] = []
        for symbol in self.symbols:
            blob = f"{symbol.path} {symbol.name} {symbol.doc}".lower()
            score = sum(1 for token in tokens if token in blob)
            if score:
                ranked.append((score, symbol))
        ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
        selected = ranked[:max_symbols]
        lines = ["Repository root: <isolated-repository>", f"Files indexed: {len(self.files)}", "Relevant symbols:"]
        for score, symbol in selected:
            lines.append(f"- [{score}] {symbol.kind} {symbol.name} at {symbol.path}:{symbol.line}-{symbol.end_line}")
        lines.append("Import/containment edges:")
        for edge in self.edges[:80]:
            lines.append(f"- {edge['source']} -[{edge['relation']}]-> {edge['target']}")
        lines.append("Important: use the repository files as the source of truth; do not invent APIs or test commands.")
        return "\n".join(lines)

    def source_context(
        self,
        goal: str,
        max_files: int = 8,
        max_chars: int = 24_000,
        focus_paths: list[str] | None = None,
    ) -> str:
        """Return deterministic source context for an edit proposal.

        This is deliberately static indexing, not RAG: code navigation should
        be reproducible and line-addressable. The harness can later add an
        LSP adapter without changing the Agent loop contract.
        """
        tokens = _tokens(goal)
        normalized_focus = {item.replace("\\", "/") for item in (focus_paths or [])}
        candidates: list[tuple[int, str]] = []
        for rel in self.files:
            blob = rel.lower()
            score = sum(1 for token in tokens if token in blob)
            if rel in normalized_focus:
                score += 100
            symbol_match = any(
                symbol.path == rel and any(token in symbol.name.lower() for token in tokens)
                for symbol in self.symbols
            )
            if symbol_match:
                score += 2
            candidates.append((score, rel))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        # Explicit focus files are always wanted: the initial-failure evidence
        # (or a symbol it names) points at them, so they must not be crowded
        # out by many equally-boosted candidates. Reserve slots for them first.
        focused = sorted(rel for rel in self.files if rel in normalized_focus)
        focused = focused[:max_files]
        selected = focused + [
            rel
            for score, rel in candidates
            if rel not in normalized_focus and score > 0
        ][: max_files - len(focused)]
        if not selected:
            selected = self.files[:max_files]
        # Distribute the budget across files instead of letting the first
        # (possibly huge) file consume the whole cap and starve the rest. A
        # focused context that hides the library modules a fix actually needs
        # leaves a model with no exact text to match.
        per_file = max(1, max_chars // max_files)
        chunks: list[str] = []
        for rel in selected:
            path = self.root / rel
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            chunks.append(f"--- {rel} ---\n{content[:per_file]}")
        return "\n\n".join(chunks)

    def summary(self) -> dict[str, int]:
        return {"files": len(self.files), "symbols": len(self.symbols), "edges": len(self.edges)}


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        # Exclude directories inside the repository only; absolute path parts
        # (for example a repo that lives under a ``data/`` directory) must not
        # suppress the whole tree.
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.name in {".env", ".env.example"} or is_denied_artifact(relative):
            continue
        yield path


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text.lower())}
