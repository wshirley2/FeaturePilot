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
    routes: list[str] = field(default_factory=list)
    syntax_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "symbols": [asdict(symbol) for symbol in self.symbols],
            "imports": self.imports,
            "routes": self.routes,
            "syntax_error": self.syntax_error,
        }


def parse_python_source(source: str, path: str) -> PythonModule:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        return PythonModule(path=path, syntax_error=str(error))

    symbols: list[Symbol] = []
    imports: set[str] = set()
    routes: set[str] = set()
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
            routes.update(_extract_routes(node))
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
            imports.add("." * node.level + node.module)
        elif isinstance(node, ast.ImportFrom) and node.level:
            imports.add("." * node.level)

    symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
    return PythonModule(
        path=path,
        symbols=symbols,
        imports=sorted(imports),
        routes=sorted(routes),
    )


def _extract_routes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    routes: set[str] = set()
    route_methods = {"delete", "get", "head", "options", "patch", "post", "put", "route"}
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        if decorator.func.attr == "api_route":
            methods = _literal_methods(decorator)
        elif decorator.func.attr in route_methods:
            methods = [decorator.func.attr.upper()]
        else:
            continue
        if not decorator.args:
            continue
        try:
            path = ast.literal_eval(decorator.args[0])
        except (ValueError, SyntaxError):
            continue
        if isinstance(path, str):
            routes.update(f"{method} {path}" for method in methods)
    return routes


def _literal_methods(decorator: ast.Call) -> list[str]:
    for keyword in decorator.keywords:
        if keyword.arg != "methods":
            continue
        try:
            methods = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError):
            return []
        if isinstance(methods, (list, tuple, set)):
            return [method.upper() for method in methods if isinstance(method, str)]
    return ["GET"]
