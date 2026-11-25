#!/usr/bin/env python3
"""Build the small Android.bp subset used by exfatprogs with Ninja.

This is intentionally not a general Soong replacement.  Unsupported cc_ module
types and properties are errors so that this tool cannot silently produce a
build which differs from the Android.bp description.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_PROPERTIES = {
    "cc_library_headers": {"name", "export_include_dirs"},
    "cc_defaults": {
        "name",
        "cflags",
        "defaults",
        "header_libs",
        "shared_libs",
        "export_header_lib_headers",
    },
    "cc_library_static": {
        "name",
        "cflags",
        "defaults",
        "header_libs",
        "shared_libs",
        "srcs",
        "static_libs",
    },
    "cc_binary": {
        "name",
        "cflags",
        "defaults",
        "header_libs",
        "shared_libs",
        "srcs",
        "static_libs",
    },
}

LIST_PROPERTIES = {
    "cflags",
    "defaults",
    "export_header_lib_headers",
    "export_include_dirs",
    "header_libs",
    "shared_libs",
    "srcs",
    "static_libs",
}

NDK_SHARED_LIBS = {
    # Clang links libc automatically.
    "libc": [],
    "libm": ["-lm"],
    "libdl": ["-ldl"],
    "liblog": ["-llog"],
    "libandroid": ["-landroid"],
    "libz": ["-lz"],
}


class BlueprintError(Exception):
    pass


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int
    column: int


@dataclass
class Module:
    kind: str
    name: str
    properties: dict[str, Any]
    directory: Path
    source_file: Path


class Lexer:
    def __init__(self, text: str, filename: Path):
        self.text = text
        self.filename = filename
        self.offset = 0
        self.line = 1
        self.column = 1

    def _advance(self, value: str) -> None:
        lines = value.splitlines(keepends=True)
        if len(lines) > 1:
            self.line += len(lines) - 1
            self.column = len(lines[-1]) + 1
        else:
            self.column += len(value)
        self.offset += len(value)

    def tokens(self) -> list[Token]:
        result: list[Token] = []
        while self.offset < len(self.text):
            rest = self.text[self.offset :]

            match = re.match(r"(?:\s+|//[^\n]*(?:\n|$)|/\*.*?\*/)", rest, re.S)
            if match:
                self._advance(match.group(0))
                continue

            line, column = self.line, self.column
            if rest[0] == '"':
                match = re.match(r'"(?:\\.|[^"\\])*"', rest, re.S)
                if not match:
                    raise BlueprintError(
                        f"{self.filename}:{line}:{column}: unterminated string"
                    )
                raw = match.group(0)
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as error:
                    raise BlueprintError(
                        f"{self.filename}:{line}:{column}: {error.msg}"
                    ) from error
                result.append(Token("string", value, line, column))
                self._advance(raw)
                continue

            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", rest)
            if match:
                value = match.group(0)
                result.append(Token("identifier", value, line, column))
                self._advance(value)
                continue

            match = re.match(r"-?[0-9]+", rest)
            if match:
                value = match.group(0)
                result.append(Token("number", value, line, column))
                self._advance(value)
                continue

            if rest[0] in "{}[]:,.+":
                result.append(Token(rest[0], rest[0], line, column))
                self._advance(rest[0])
                continue

            raise BlueprintError(
                f"{self.filename}:{line}:{column}: unexpected character {rest[0]!r}"
            )

        result.append(Token("eof", "", self.line, self.column))
        return result


class Parser:
    def __init__(self, tokens: list[Token], filename: Path):
        self.tokens = tokens
        self.filename = filename
        self.position = 0

    def current(self) -> Token:
        return self.tokens[self.position]

    def accept(self, kind: str) -> Token | None:
        if self.current().kind == kind:
            token = self.current()
            self.position += 1
            return token
        return None

    def expect(self, kind: str) -> Token:
        token = self.accept(kind)
        if token is None:
            current = self.current()
            raise BlueprintError(
                f"{self.filename}:{current.line}:{current.column}: "
                f"expected {kind}, found {current.kind}"
            )
        return token

    def parse(self) -> list[tuple[str, dict[str, Any]]]:
        modules: list[tuple[str, dict[str, Any]]] = []
        while self.current().kind != "eof":
            kind = self.expect("identifier").value
            properties = self.parse_map(braced=True)
            modules.append((kind, properties))
        return modules

    def parse_map(self, braced: bool) -> dict[str, Any]:
        if braced:
            self.expect("{")
        result: dict[str, Any] = {}
        while self.current().kind != "}":
            key = self.expect("identifier").value
            self.expect(":")
            if key in result:
                current = self.current()
                raise BlueprintError(
                    f"{self.filename}:{current.line}:{current.column}: "
                    f"duplicate property {key!r}"
                )
            result[key] = self.parse_value()
            self.expect(",")
        self.expect("}")
        return result

    def parse_value(self) -> Any:
        token = self.current()
        if self.accept("string"):
            return token.value
        if self.accept("number"):
            return int(token.value)
        if token.kind == "identifier" and token.value in {"true", "false"}:
            self.position += 1
            return token.value == "true"
        if self.accept("["):
            values: list[Any] = []
            while self.current().kind != "]":
                values.append(self.parse_value())
                if not self.accept(",") and self.current().kind != "]":
                    current = self.current()
                    raise BlueprintError(
                        f"{self.filename}:{current.line}:{current.column}: "
                        "expected , or ]"
                    )
            self.expect("]")
            return values
        if token.kind == "{":
            return self.parse_map(braced=True)
        raise BlueprintError(
            f"{self.filename}:{token.line}:{token.column}: unsupported value expression"
        )


def string_list(module: Module, property_name: str) -> list[str]:
    value = module.properties.get(property_name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BlueprintError(
            f"{module.source_file}: {module.name}.{property_name} must be a string list"
        )
    return value


class BuildGraph:
    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, Module] = {}
        self._resolved_defaults: dict[str, dict[str, list[str]]] = {}

    def load(self) -> None:
        blueprint_files = sorted(self.root.rglob("Android.bp"))
        if not blueprint_files:
            raise BlueprintError(f"no Android.bp files found below {self.root}")

        for blueprint in blueprint_files:
            relative_file = blueprint.relative_to(self.root)
            text = blueprint.read_text(encoding="utf-8")
            parsed = Parser(Lexer(text, relative_file).tokens(), relative_file).parse()
            directory = relative_file.parent

            for kind, properties in parsed:
                if kind.startswith("cc_") and kind not in SUPPORTED_PROPERTIES:
                    raise BlueprintError(f"{relative_file}: unsupported module type {kind}")
                if kind not in SUPPORTED_PROPERTIES:
                    continue

                unsupported = set(properties) - SUPPORTED_PROPERTIES[kind]
                if unsupported:
                    names = ", ".join(sorted(unsupported))
                    raise BlueprintError(
                        f"{relative_file}: {kind} uses unsupported properties: {names}"
                    )

                name = properties.get("name")
                if not isinstance(name, str) or not name:
                    raise BlueprintError(f"{relative_file}: {kind} requires a name")
                if name in self.modules:
                    previous = self.modules[name].source_file
                    raise BlueprintError(
                        f"{relative_file}: duplicate module {name!r}; first defined in {previous}"
                    )

                module = Module(kind, name, properties, directory, relative_file)
                for property_name in LIST_PROPERTIES & properties.keys():
                    string_list(module, property_name)
                self.modules[name] = module

        self.validate()

    def require(self, owner: Module, name: str, expected_kinds: set[str]) -> Module:
        dependency = self.modules.get(name)
        if dependency is None:
            raise BlueprintError(
                f"{owner.source_file}: {owner.name} references missing module {name!r}"
            )
        if dependency.kind not in expected_kinds:
            expected = ", ".join(sorted(expected_kinds))
            raise BlueprintError(
                f"{owner.source_file}: {owner.name} requires {name!r} to be {expected}, "
                f"not {dependency.kind}"
            )
        return dependency

    def validate(self) -> None:
        for module in self.modules.values():
            for name in string_list(module, "defaults"):
                self.require(module, name, {"cc_defaults"})
            for name in string_list(module, "header_libs"):
                self.require(module, name, {"cc_library_headers"})
            for name in string_list(module, "static_libs"):
                self.require(module, name, {"cc_library_static"})
            for source in string_list(module, "srcs"):
                path = self.root / module.directory / source
                if not path.is_file():
                    raise BlueprintError(
                        f"{module.source_file}: {module.name} source does not exist: {source}"
                    )
            for include_dir in string_list(module, "export_include_dirs"):
                path = self.root / module.directory / include_dir
                if not path.is_dir():
                    raise BlueprintError(
                        f"{module.source_file}: {module.name} include directory "
                        f"does not exist: {include_dir}"
                    )

        for module in self.modules.values():
            if module.kind == "cc_defaults":
                self.resolve_defaults(module, [])
        for module in self.modules.values():
            if module.kind not in {"cc_defaults", "cc_library_static", "cc_binary"}:
                continue
            properties = self.resolved(module)
            for name in properties["export_header_lib_headers"]:
                self.require(module, name, {"cc_library_headers"})
                if name not in properties["header_libs"]:
                    raise BlueprintError(
                        f"{module.source_file}: {module.name} exports header library "
                        f"{name!r} without listing it in header_libs"
                    )

    def resolve_defaults(
        self, module: Module, stack: list[str]
    ) -> dict[str, list[str]]:
        if module.name in self._resolved_defaults:
            return self._resolved_defaults[module.name]
        if module.name in stack:
            cycle = " -> ".join(stack + [module.name])
            raise BlueprintError(f"defaults dependency cycle: {cycle}")

        result = {property_name: [] for property_name in LIST_PROPERTIES}
        for name in string_list(module, "defaults"):
            dependency = self.require(module, name, {"cc_defaults"})
            inherited = self.resolve_defaults(dependency, stack + [module.name])
            for property_name in result:
                result[property_name].extend(inherited[property_name])
        for property_name in result:
            result[property_name].extend(string_list(module, property_name))
        self._resolved_defaults[module.name] = result
        return result

    def resolved(self, module: Module) -> dict[str, list[str]]:
        result = {property_name: [] for property_name in LIST_PROPERTIES}
        for name in string_list(module, "defaults"):
            dependency = self.require(module, name, {"cc_defaults"})
            inherited = self.resolve_defaults(dependency, [])
            for property_name in result:
                result[property_name].extend(inherited[property_name])
        for property_name in result:
            result[property_name].extend(string_list(module, property_name))
        return result


def ninja_path(path: Path | str) -> str:
    return str(path).replace("$", "$$").replace(" ", "$ ").replace(":", "$:")


def shell_words(values: list[str]) -> str:
    return " ".join(shlex.quote(value) for value in values)


class NinjaGenerator:
    def __init__(
        self,
        graph: BuildGraph,
        out: Path,
        cc: str,
        ar: str,
        target: str,
        shared_libs: dict[str, list[str]],
        selected_modules: list[str],
    ) -> None:
        self.graph = graph
        self.out = out
        self.cc = cc
        self.ar = ar
        self.target = target
        self.shared_libs = shared_libs
        self.selected_modules = selected_modules
        self.outputs: dict[str, Path] = {}
        self.objects: dict[str, list[Path]] = {}
        self.generated_modules: list[str] = []

    @staticmethod
    def safe_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", name)

    def object_path(self, module: Module, source: str) -> Path:
        relative = module.directory / source
        return self.out / "obj" / self.safe_name(module.name) / Path(str(relative) + ".o")

    def module_output(self, module: Module) -> Path:
        if module.kind == "cc_library_static":
            return self.out / "lib" / f"{module.name}.a"
        return self.out / "bin" / module.name

    def include_flags(self, module: Module, properties: dict[str, list[str]]) -> list[str]:
        header_names = list(properties["header_libs"])
        for static_name in properties["static_libs"]:
            static_library = self.graph.require(
                module, static_name, {"cc_library_static"}
            )
            static_properties = self.graph.resolved(static_library)
            header_names.extend(static_properties["export_header_lib_headers"])

        result: list[str] = []
        seen: set[str] = set()
        for name in header_names:
            if name in seen:
                continue
            seen.add(name)
            header = self.graph.require(module, name, {"cc_library_headers"})
            for include_dir in string_list(header, "export_include_dirs"):
                result.append(f"-I{header.directory / include_dir}")
        return result

    def static_dependencies(self, module: Module) -> list[Path]:
        result: list[Path] = []
        visited: set[str] = set()

        def visit(owner: Module, name: str, stack: list[str]) -> None:
            if name in stack:
                raise BlueprintError("static library cycle: " + " -> ".join(stack + [name]))
            if name in visited:
                return
            visited.add(name)
            dependency = self.graph.require(owner, name, {"cc_library_static"})
            result.append(self.outputs[name])
            properties = self.graph.resolved(dependency)
            for child in properties["static_libs"]:
                visit(dependency, child, stack + [name])

        properties = self.graph.resolved(module)
        for name in properties["static_libs"]:
            visit(module, name, [module.name])
        return result

    def shared_dependencies(self, module: Module) -> list[str]:
        names: list[str] = []
        visited_static: set[str] = set()

        def visit(current: Module) -> None:
            properties = self.graph.resolved(current)
            names.extend(properties["shared_libs"])
            for static_name in properties["static_libs"]:
                if static_name in visited_static:
                    continue
                visited_static.add(static_name)
                visit(
                    self.graph.require(
                        current, static_name, {"cc_library_static"}
                    )
                )

        visit(module)
        result: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            link_args = self.shared_libs.get(name)
            if link_args is None:
                raise BlueprintError(
                    f"{module.source_file}: {module.name} uses shared library {name!r}, "
                    f"which is not supplied by the NDK mapping; pass "
                    f"--shared-lib {name}=-l<library>"
                )
            result.extend(link_args)
        return result

    def build_modules(self) -> list[Module]:
        buildable_kinds = {"cc_library_static", "cc_binary"}
        if not self.selected_modules:
            return [
                module
                for module in self.graph.modules.values()
                if module.kind in buildable_kinds
            ]

        selected: set[str] = set()

        def select(module: Module, stack: list[str]) -> None:
            if module.name in stack:
                raise BlueprintError(
                    "static library cycle: " + " -> ".join(stack + [module.name])
                )
            if module.name in selected:
                return
            if module.kind not in buildable_kinds:
                raise BlueprintError(
                    f"module {module.name!r} is {module.kind}, not a buildable target"
                )
            selected.add(module.name)
            for name in self.graph.resolved(module)["static_libs"]:
                select(
                    self.graph.require(module, name, {"cc_library_static"}),
                    stack + [module.name],
                )

        for name in self.selected_modules:
            module = self.graph.modules.get(name)
            if module is None:
                raise BlueprintError(f"requested module {name!r} does not exist")
            select(module, [])

        return [
            module for module in self.graph.modules.values() if module.name in selected
        ]

    def generate(self) -> Path:
        build_modules = self.build_modules()
        if not build_modules:
            raise BlueprintError("no cc_library_static or cc_binary modules found")
        self.generated_modules = [module.name for module in build_modules]

        for module in build_modules:
            self.outputs[module.name] = self.module_output(module)
            self.objects[module.name] = [
                self.object_path(module, source)
                for source in self.graph.resolved(module)["srcs"]
            ]

        for path in self.outputs.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        for paths in self.objects.values():
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# Generated from Android.bp; do not edit.",
            f"builddir = {ninja_path(self.out)}",
            f"cc = {self.cc}",
            f"ar = {self.ar}",
            "",
            "rule cc",
            "  command = $cc $cflags -MMD -MF $out.d -c $in -o $out",
            "  depfile = $out.d",
            "  deps = gcc",
            "  description = CC $out",
            "",
            "rule ar",
            "  command = $ar rcs $out $in",
            "  description = AR $out",
            "",
            "rule link",
            "  command = $cc --target=$target $in $ldlibs -o $out",
            "  description = LINK $out",
            "",
        ]

        for module in build_modules:
            properties = self.graph.resolved(module)
            flags = [f"--target={self.target}"]
            flags.extend(properties["cflags"])
            flags.extend(self.include_flags(module, properties))
            for source, obj in zip(properties["srcs"], self.objects[module.name]):
                source_path = module.directory / source
                lines.extend(
                    [
                        f"build {ninja_path(obj)}: cc {ninja_path(source_path)}",
                        f"  cflags = {shell_words(flags)}",
                        "",
                    ]
                )

        static_modules = [m for m in build_modules if m.kind == "cc_library_static"]
        binary_modules = [m for m in build_modules if m.kind == "cc_binary"]
        for module in static_modules:
            inputs = " ".join(ninja_path(path) for path in self.objects[module.name])
            lines.extend(
                [f"build {ninja_path(self.outputs[module.name])}: ar {inputs}", ""]
            )

        for module in binary_modules:
            inputs = self.objects[module.name] + self.static_dependencies(module)
            input_text = " ".join(ninja_path(path) for path in inputs)
            ldlibs = shell_words(self.shared_dependencies(module))
            lines.extend(
                [
                    f"build {ninja_path(self.outputs[module.name])}: link {input_text}",
                    f"  target = {self.target}",
                    f"  ldlibs = {ldlibs}",
                    "",
                ]
            )

        binary_outputs = " ".join(
            ninja_path(self.outputs[module.name]) for module in binary_modules
        )
        lines.extend([f"build all: phony {binary_outputs}", "default all", ""])

        ninja_file = self.out / "build.ninja"
        ninja_file.parent.mkdir(parents=True, exist_ok=True)
        ninja_file.write_text("\n".join(lines), encoding="utf-8")
        return ninja_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cc", required=True)
    parser.add_argument("--ar", required=True)
    parser.add_argument("--target", default="aarch64-linux-android24")
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="generate only this module and its static dependencies (repeatable)",
    )
    parser.add_argument(
        "--shared-lib",
        action="append",
        default=[],
        metavar="NAME=LINK_ARG",
        help="map an additional Soong shared library to a linker argument",
    )
    return parser.parse_args()


def shared_lib_mappings(values: list[str]) -> dict[str, list[str]]:
    result = {name: list(args) for name, args in NDK_SHARED_LIBS.items()}
    for value in values:
        if "=" not in value:
            raise BlueprintError(
                f"invalid --shared-lib {value!r}; expected NAME=LINK_ARG"
            )
        name, link_arg = value.split("=", 1)
        if not name or not link_arg:
            raise BlueprintError(
                f"invalid --shared-lib {value!r}; expected NAME=LINK_ARG"
            )
        result[name] = [link_arg]
    return result


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    out = args.out.resolve()
    try:
        graph = BuildGraph(root)
        graph.load()
        mappings = shared_lib_mappings(args.shared_lib)
        ninja_file = NinjaGenerator(
            graph,
            out,
            args.cc,
            args.ar,
            args.target,
            mappings,
            args.module,
        ).generate()
    except BlueprintError as error:
        print(f"android_bp_to_ninja: error: {error}", file=sys.stderr)
        return 1

    selected = args.module or [
        module.name
        for module in graph.modules.values()
        if module.kind in {"cc_library_static", "cc_binary"}
    ]
    print(f"Generated {ninja_file} for: {', '.join(selected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
