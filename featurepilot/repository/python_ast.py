"""Small Python AST index used by the repository profiler."""

import ast
from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class Symbol:
    name: str
    kind: str
    line: int
    end_line: int | None = None


@dataclass(slots=True)
class PythonModule:
    path: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    syntax_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "symbols": [asdict(symbol) for symbol in self.symbols],
            "imports": self.imports,
            "syntax_error": self.syntax_error,
        }


def parse_python_source(source: str, path: str) -> PythonModule:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        return PythonModule(path=path, syntax_error=str(error))

    symbols: list[Symbol] = []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="function",
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", None),
                )
            )
        elif isinstance(node, ast.ClassDef):
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", None),
                )
            )
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)

    symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
    return PythonModule(path=path, symbols=symbols, imports=sorted(imports))
