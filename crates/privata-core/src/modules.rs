//! Python module collection and public-symbol extraction.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use ruff_python_ast::{Expr, ModModule, Stmt, StmtClassDef, StmtFunctionDef};
use ruff_source_file::LineIndex;

use crate::ast_utils::{dotted_name, lineno_at, names_from_target, string_literal_set};
use crate::models::{
    Module, ModuleCollision, Symbol, SymbolCandidate, SymbolKind, UnparsableModule,
    NAMESPACE_SEPARATOR,
};
use crate::source_roots::{
    is_in_ignored_directory, is_nested_test_file, is_test_module_filename, should_skip_source_file,
    walk_python_files,
};

const ROUTE_DECORATORS: &[&str] = &[
    "api_route",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
    "websocket",
    "websocket_route",
];
const CLI_DECORATORS: &[&str] = &["callback", "command"];
const FRAMEWORK_CONSTRUCTORS: &[&str] = &["APIRouter", "FastAPI", "Typer"];
const FRAMEWORK_REGISTRATION_CALLS: &[&str] =
    &["add_api_route", "add_api_websocket_route", "include_router"];
const ALLOWED_PUBLIC_NAMES: &[&str] = &["logger"];

fn module_name_from_path(py_file: &Path, source_root: &Path) -> Option<String> {
    let rel = py_file.strip_prefix(source_root).unwrap_or(py_file);
    let mut parts: Vec<String> = rel
        .iter()
        .filter_map(|p| p.to_str())
        .map(str::to_string)
        .collect();
    if let Some(last) = parts.last_mut() {
        if let Some(stem) = last.strip_suffix(".py") {
            *last = stem.to_string();
        }
    }
    if parts.last().map(String::as_str) == Some("__init__") {
        parts.pop();
    }
    if parts.is_empty() {
        return None;
    }
    Some(parts.join(NAMESPACE_SEPARATOR))
}

/// Return the package path used to resolve relative imports.
pub fn package_parts(module_name: &str, is_package_init: bool) -> Vec<String> {
    if is_package_init {
        return module_name
            .split(NAMESPACE_SEPARATOR)
            .map(str::to_string)
            .collect();
    }
    let mut parts: Vec<&str> = module_name.rsplitn(2, NAMESPACE_SEPARATOR).collect();
    if parts.len() == 1 {
        return Vec::new();
    }
    parts.reverse(); // rsplitn(2, ..) gives [tail, head]; reversed -> [head, tail]
    parts[0]
        .split(NAMESPACE_SEPARATOR)
        .map(str::to_string)
        .collect()
}

/// Return 1-indexed line numbers carrying a `# privata: ignore` comment.
fn ignored_lines(source: &str) -> HashSet<u32> {
    source
        .lines()
        .enumerate()
        .filter(|(_, line)| line.contains("# privata: ignore"))
        .map(|(i, _)| (i + 1) as u32)
        .collect()
}

/// Parse every production `.py` under source roots and collect top-level
/// public definitions.
pub fn collect_modules(source_roots: &[PathBuf]) -> HashMap<String, Module> {
    collect_modules_with_errors(source_roots).0
}

/// Parse every production `.py`, returning both the modules and the failures.
pub fn collect_modules_with_errors(
    source_roots: &[PathBuf],
) -> (HashMap<String, Module>, Vec<UnparsableModule>) {
    let mut modules: HashMap<String, Module> = HashMap::new();
    let mut unparsable: Vec<UnparsableModule> = Vec::new();

    for source_root in source_roots {
        for py_file in walk_python_files(source_root) {
            if should_skip_source_file(&py_file, source_root) {
                continue;
            }
            let Some(mod_name) = module_name_from_path(&py_file, source_root) else {
                continue;
            };

            let source = std::fs::read_to_string(&py_file)
                .unwrap_or_else(|err| panic!("failed to read {}: {err}", py_file.display()));

            match ruff_python_parser::parse_module(&source) {
                Err(error) => {
                    let line_index = LineIndex::from_source_text(&source);
                    unparsable.push(UnparsableModule {
                        module: mod_name,
                        path: py_file,
                        lineno: lineno_at(&line_index, error.location.start()),
                        message: error.error.to_string(),
                    });
                }
                Ok(parsed) => {
                    let tree = parsed.into_syntax();
                    let line_index = LineIndex::from_source_text(&source);
                    let explicit_exports = extract_all(&tree);
                    let framework_related_names = collect_framework_related_names(&tree.body);
                    let mut pydantic_model_names: HashSet<String> = HashSet::new();
                    let file_ignored_lines = ignored_lines(&source);
                    let is_package_init =
                        py_file.file_name().and_then(|n| n.to_str()) == Some("__init__.py");

                    let mut symbols = Vec::new();
                    let mut private_symbols = Vec::new();

                    for node in &tree.body {
                        match node {
                            Stmt::FunctionDef(f) => {
                                if is_framework_callback(f) || is_pytest_fixture(f) {
                                    continue;
                                }
                                maybe_add(
                                    &mut symbols,
                                    &mut private_symbols,
                                    &mod_name,
                                    &py_file,
                                    SymbolCandidate {
                                        name: f.name.id.to_string(),
                                        kind: SymbolKind::Function,
                                        lineno: lineno_at(&line_index, f.name.range.start()),
                                    },
                                    explicit_exports.as_ref(),
                                    &framework_related_names,
                                    &file_ignored_lines,
                                );
                            }
                            Stmt::ClassDef(c) => {
                                if is_pydantic_model(c, &pydantic_model_names) {
                                    pydantic_model_names.insert(c.name.id.to_string());
                                    continue;
                                }
                                maybe_add(
                                    &mut symbols,
                                    &mut private_symbols,
                                    &mod_name,
                                    &py_file,
                                    SymbolCandidate {
                                        name: c.name.id.to_string(),
                                        kind: SymbolKind::Class,
                                        lineno: lineno_at(&line_index, c.name.range.start()),
                                    },
                                    explicit_exports.as_ref(),
                                    &framework_related_names,
                                    &file_ignored_lines,
                                );
                            }
                            Stmt::Assign(a) => {
                                if is_framework_constructor_call(&a.value) {
                                    continue;
                                }
                                for target in &a.targets {
                                    for name in names_from_target(target) {
                                        maybe_add(
                                            &mut symbols,
                                            &mut private_symbols,
                                            &mod_name,
                                            &py_file,
                                            SymbolCandidate {
                                                name,
                                                kind: SymbolKind::Variable,
                                                lineno: lineno_at(&line_index, a.range.start()),
                                            },
                                            explicit_exports.as_ref(),
                                            &framework_related_names,
                                            &file_ignored_lines,
                                        );
                                    }
                                }
                            }
                            Stmt::AnnAssign(a) => {
                                if let Some(value) = &a.value {
                                    if is_framework_constructor_call(value) {
                                        continue;
                                    }
                                }
                                for name in names_from_target(&a.target) {
                                    maybe_add(
                                        &mut symbols,
                                        &mut private_symbols,
                                        &mod_name,
                                        &py_file,
                                        SymbolCandidate {
                                            name,
                                            kind: SymbolKind::Variable,
                                            lineno: lineno_at(&line_index, a.range.start()),
                                        },
                                        explicit_exports.as_ref(),
                                        &framework_related_names,
                                        &file_ignored_lines,
                                    );
                                }
                            }
                            Stmt::TypeAlias(t) => {
                                for name in names_from_target(&t.name) {
                                    maybe_add(
                                        &mut symbols,
                                        &mut private_symbols,
                                        &mod_name,
                                        &py_file,
                                        SymbolCandidate {
                                            name,
                                            kind: SymbolKind::Variable,
                                            lineno: lineno_at(&line_index, t.range.start()),
                                        },
                                        explicit_exports.as_ref(),
                                        &framework_related_names,
                                        &file_ignored_lines,
                                    );
                                }
                            }
                            _ => {}
                        }
                    }

                    let mut module = Module::new(
                        mod_name.clone(),
                        py_file,
                        package_parts(&mod_name, is_package_init),
                    );
                    module.symbols = symbols;
                    module.private_symbols = private_symbols;
                    module.exports = explicit_exports.unwrap_or_default();
                    module.ignored_lines = file_ignored_lines;
                    module.tree = Some(tree);
                    module.line_index = Some(line_index);

                    // A later source root must not evict an already-collected module: the
                    // collision is real (and collect_module_collisions reports it), but
                    // dropping the first file found would silently stop scanning it.
                    modules.entry(mod_name).or_insert(module);
                }
            }
        }
    }

    (modules, unparsable)
}

/// Return module names that resolve to more than one production source file.
///
/// Two files mapping to the same dotted name (e.g. `src/utils.py` and
/// `tests/utils.py`, or `pkg.py` next to `pkg/__init__.py`) are ambiguous at
/// import time, and only one of them can be scanned. Files are not parsed
/// here: a file with broken syntax still occupies its module name.
pub fn collect_module_collisions(source_roots: &[PathBuf]) -> Vec<ModuleCollision> {
    let mut paths_by_name: HashMap<String, HashSet<PathBuf>> = HashMap::new();
    for source_root in source_roots {
        for py_file in walk_python_files(source_root) {
            if should_skip_source_file(&py_file, source_root) {
                continue;
            }
            let Some(mod_name) = module_name_from_path(&py_file, source_root) else {
                continue;
            };
            paths_by_name.entry(mod_name).or_default().insert(py_file);
        }
    }

    let mut collisions: Vec<ModuleCollision> = paths_by_name
        .into_iter()
        .filter(|(_, paths)| paths.len() > 1)
        .map(|(module, paths)| {
            let mut paths: Vec<PathBuf> = paths.into_iter().collect();
            paths.sort();
            ModuleCollision { module, paths }
        })
        .collect();
    collisions.sort_by(|a, b| a.module.cmp(&b.module));
    collisions
}

/// Parse test files for use as import consumers only.
///
/// `test_source_roots` are roots that are themselves test directories, so
/// every file matching a test-file naming convention counts.
/// `nested_test_roots` are production roots: `collect_modules_with_errors`
/// already discards their test-shaped files via `should_skip_source_file`,
/// which otherwise leaves them neither scanned as modules nor counted as
/// consumers of anything they import.
pub fn collect_test_consumers(
    test_source_roots: &[PathBuf],
    nested_test_roots: &[PathBuf],
) -> HashMap<String, Module> {
    let mut consumers: HashMap<String, Module> = HashMap::new();
    for source_root in test_source_roots {
        for py_file in walk_python_files(source_root) {
            if !is_test_module_filename(py_file.file_name().and_then(|n| n.to_str()).unwrap_or(""))
            {
                continue;
            }
            if is_in_ignored_directory(&py_file, source_root) {
                continue;
            }
            add_test_consumer(&mut consumers, &py_file, source_root);
        }
    }
    for source_root in nested_test_roots {
        for py_file in walk_python_files(source_root) {
            if !is_nested_test_file(&py_file, source_root) {
                continue;
            }
            add_test_consumer(&mut consumers, &py_file, source_root);
        }
    }
    consumers
}

fn add_test_consumer(consumers: &mut HashMap<String, Module>, py_file: &Path, source_root: &Path) {
    let rel = py_file.strip_prefix(source_root).unwrap_or(py_file);
    let mut parts: Vec<String> = rel
        .iter()
        .filter_map(|p| p.to_str())
        .map(str::to_string)
        .collect();
    if let Some(last) = parts.last_mut() {
        if let Some(stem) = last.strip_suffix(".py") {
            *last = stem.to_string();
        }
    }
    let mod_name = parts.join(NAMESPACE_SEPARATOR);

    let Ok(source) = std::fs::read_to_string(py_file) else {
        return;
    };
    let Ok(parsed) = ruff_python_parser::parse_module(&source) else {
        return;
    };
    let tree = parsed.into_syntax();
    let line_index = LineIndex::from_source_text(&source);
    let mut module = Module::new(
        mod_name.clone(),
        py_file.to_path_buf(),
        package_parts(&mod_name, false),
    );
    module.tree = Some(tree);
    module.line_index = Some(line_index);
    consumers.insert(mod_name, module);
}

fn extract_all(tree: &ModModule) -> Option<HashSet<String>> {
    for node in &tree.body {
        if let Stmt::Assign(assign) = node {
            for target in &assign.targets {
                if let Expr::Name(name) = target {
                    if name.id.as_str() == "__all__" {
                        return string_literal_set(&assign.value);
                    }
                }
            }
        }
    }
    None
}

fn decorator_attr_name(decorator: &Expr) -> Option<String> {
    let target = match decorator {
        Expr::Call(call) => &call.func,
        other => other,
    };
    match target {
        Expr::Attribute(attr) => Some(attr.attr.id.to_string()),
        _ => None,
    }
}

fn is_framework_callback(node: &StmtFunctionDef) -> bool {
    node.decorator_list.iter().any(|decorator| {
        decorator_attr_name(&decorator.expression).is_some_and(|name| {
            ROUTE_DECORATORS.contains(&name.as_str()) || CLI_DECORATORS.contains(&name.as_str())
        })
    })
}

/// Detect `@pytest.fixture` / `@fixture`, bare or called, decorating a function.
///
/// A fixture is consumed by pytest through parameter-name injection, never
/// by import, so it can never be seen as "used" by `find_cross_imports`.
fn is_pytest_fixture(node: &StmtFunctionDef) -> bool {
    node.decorator_list.iter().any(|decorator| {
        let target = match &decorator.expression {
            Expr::Call(call) => &*call.func,
            other => other,
        };
        match target {
            Expr::Attribute(attr) => attr.attr.id.as_str() == "fixture",
            Expr::Name(name) => name.id.as_str() == "fixture",
            _ => false,
        }
    })
}

/// `pytest_plugins` and `pytest_*` hooks are consumed by pytest, not by import.
fn is_pytest_hook_name(name: &str) -> bool {
    name.starts_with("pytest_")
}

fn names_in_expr(expr: &Expr) -> HashSet<String> {
    use ruff_python_ast::visitor::{walk_expr, Visitor};

    struct Collector {
        names: HashSet<String>,
    }
    impl<'a> Visitor<'a> for Collector {
        fn visit_expr(&mut self, expr: &'a Expr) {
            if let Expr::Name(name) = expr {
                self.names.insert(name.id.to_string());
            }
            walk_expr(self, expr);
        }
    }
    let mut collector = Collector {
        names: HashSet::new(),
    };
    collector.visit_expr(expr);
    collector.names
}

fn framework_callback_names(node: &StmtFunctionDef) -> HashSet<String> {
    let mut expressions: Vec<&Expr> = node.decorator_list.iter().map(|d| &d.expression).collect();

    if let Some(returns) = &node.returns {
        expressions.push(returns);
    }

    let params = &node.parameters;
    for param in params
        .posonlyargs
        .iter()
        .chain(params.args.iter())
        .chain(params.kwonlyargs.iter())
    {
        if let Some(annotation) = &param.parameter.annotation {
            expressions.push(annotation);
        }
    }
    if let Some(vararg) = &params.vararg {
        if let Some(annotation) = &vararg.annotation {
            expressions.push(annotation);
        }
    }
    if let Some(kwarg) = &params.kwarg {
        if let Some(annotation) = &kwarg.annotation {
            expressions.push(annotation);
        }
    }

    for param in params.posonlyargs.iter().chain(params.args.iter()) {
        if let Some(default) = &param.default {
            expressions.push(default);
        }
    }
    for param in params.kwonlyargs.iter() {
        if let Some(default) = &param.default {
            expressions.push(default);
        }
    }

    let mut names = HashSet::new();
    for expr in expressions {
        names.extend(names_in_expr(expr));
    }
    names
}

fn is_framework_registration_call(expr: &Expr) -> bool {
    let Expr::Call(call) = expr else { return false };
    let Expr::Attribute(attr) = &*call.func else {
        return false;
    };
    FRAMEWORK_REGISTRATION_CALLS.contains(&attr.attr.id.as_str())
}

fn collect_framework_related_names(body: &[Stmt]) -> HashSet<String> {
    let mut names = HashSet::new();
    for node in body {
        if let Stmt::FunctionDef(f) = node {
            if is_framework_callback(f) {
                names.extend(framework_callback_names(f));
            }
            continue;
        }

        let expr: Option<&Expr> = match node {
            Stmt::Expr(e) => Some(&e.value),
            Stmt::Assign(a) => Some(&a.value),
            Stmt::AnnAssign(a) => a.value.as_deref(),
            _ => None,
        };

        if let Some(expr) = expr {
            if is_framework_registration_call(expr) {
                names.extend(names_in_expr(expr));
            }
        }
    }
    names
}

fn is_pydantic_model(node: &StmtClassDef, known_models: &HashSet<String>) -> bool {
    let Some(arguments) = &node.arguments else {
        return false;
    };
    for base in arguments.args.iter() {
        let Some(base_name) = dotted_name(base) else {
            continue;
        };
        if base_name == "BaseModel" || base_name.ends_with(".BaseModel") {
            return true;
        }
        let short = base_name
            .rsplit(NAMESPACE_SEPARATOR)
            .next()
            .unwrap_or(&base_name);
        if known_models.contains(short) {
            return true;
        }
    }
    false
}

fn is_framework_constructor_call(expr: &Expr) -> bool {
    let Expr::Call(call) = expr else { return false };
    let Some(callee) = dotted_name(&call.func) else {
        return false;
    };
    let short = callee.rsplit(NAMESPACE_SEPARATOR).next().unwrap_or(&callee);
    FRAMEWORK_CONSTRUCTORS.contains(&short)
}

fn is_private_symbol_name(name: &str) -> bool {
    name.starts_with('_') && !(name.starts_with("__") && name.ends_with("__"))
}

#[allow(clippy::too_many_arguments)]
fn maybe_add(
    symbols: &mut Vec<Symbol>,
    private_symbols: &mut Vec<Symbol>,
    module_name: &str,
    module_path: &Path,
    candidate: SymbolCandidate,
    explicit_exports: Option<&HashSet<String>>,
    ignored_names: &HashSet<String>,
    file_ignored_lines: &HashSet<u32>,
) {
    let name = candidate.name;
    if name.starts_with('_') {
        if is_private_symbol_name(&name) {
            private_symbols.push(Symbol {
                name,
                kind: candidate.kind,
                lineno: candidate.lineno,
                module: module_name.to_string(),
                path: module_path.to_path_buf(),
            });
        }
        return;
    }
    if ALLOWED_PUBLIC_NAMES.contains(&name.as_str()) || is_pytest_hook_name(&name) {
        return;
    }
    if let Some(exports) = explicit_exports {
        if exports.contains(&name) {
            return;
        }
    }
    if ignored_names.contains(&name) {
        return;
    }
    if file_ignored_lines.contains(&candidate.lineno) {
        return;
    }
    symbols.push(Symbol {
        name,
        kind: candidate.kind,
        lineno: candidate.lineno,
        module: module_name.to_string(),
        path: module_path.to_path_buf(),
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, contents: &str) -> PathBuf {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, contents).unwrap();
        path
    }

    fn has_symbol(module: &Module, name: &str) -> bool {
        module.symbols.iter().any(|s| s.name == name)
    }
    fn has_private_symbol(module: &Module, name: &str) -> bool {
        module.private_symbols.iter().any(|s| s.name == name)
    }
    fn symbol_lineno(module: &Module, name: &str) -> u32 {
        module
            .symbols
            .iter()
            .find(|s| s.name == name)
            .unwrap()
            .lineno
    }

    #[test]
    fn decorated_function_and_class_lineno_points_at_the_def_line_not_the_decorator() {
        // A ruff `StmtFunctionDef`/`StmtClassDef` range starts at the first
        // decorator, unlike CPython's `ast.FunctionDef.lineno` which starts
        // at `def`/`class` itself — reported findings must match the latter.
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "import functools\n\n\n@functools.wraps(\n    print,\n)\ndef helper():\n    pass\n\n\n@functools.wraps(print)\nclass Widget:\n    pass\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.mod"];
        assert_eq!(symbol_lineno(module, "helper"), 7);
        assert_eq!(symbol_lineno(module, "Widget"), 12);
    }

    #[test]
    fn collects_functions_classes_variables_and_type_aliases() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def helper() -> int:\n    return 1\n\n\nclass Widget:\n    pass\n\n\nVALUE = 1\n\ntype Alias = int\n",
        );
        let (modules, unparsable) = collect_modules_with_errors(&[tmp.path().join("src")]);
        assert!(unparsable.is_empty());
        let module = &modules["pkg.mod"];
        assert!(has_symbol(module, "helper"));
        assert!(has_symbol(module, "Widget"));
        assert!(has_symbol(module, "VALUE"));
        assert!(has_symbol(module, "Alias"));
    }

    #[test]
    fn private_names_are_collected_separately_and_dunders_are_ignored() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def _helper() -> int:\n    return 1\n\n\ndef __init__(self) -> None:\n    pass\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.mod"];
        assert!(has_private_symbol(module, "_helper"));
        assert!(!has_symbol(module, "_helper"));
        assert!(!has_private_symbol(module, "__init__"));
        assert!(!has_symbol(module, "__init__"));
    }

    #[test]
    fn explicit_all_exempts_listed_names() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "__all__ = [\"helper\"]\n\n\ndef helper() -> int:\n    return 1\n\n\ndef other() -> int:\n    return 2\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.mod"];
        assert!(!has_symbol(module, "helper"));
        assert!(has_symbol(module, "other"));
    }

    #[test]
    fn logger_and_pytest_hooks_are_always_ignored() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "logger = None\n\n\ndef pytest_configure() -> None:\n    pass\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.mod"];
        assert!(!has_symbol(module, "logger"));
        assert!(!has_symbol(module, "pytest_configure"));
    }

    #[test]
    fn fastapi_route_and_signature_names_are_skipped() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/api.py",
            "from fastapi import APIRouter\n\nrouter = APIRouter()\n\nclass Payload:\n    pass\n\n@router.get(\"/items\")\nasync def list_items() -> Payload:\n    return Payload()\n\ndef local_helper() -> int:\n    return 1\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.api"];
        assert!(
            !has_symbol(module, "router"),
            "framework constructor call assignment is skipped"
        );
        assert!(
            !has_symbol(module, "list_items"),
            "route callback is skipped"
        );
        assert!(
            !has_symbol(module, "Payload"),
            "name only reached via route signature is skipped"
        );
        assert!(has_symbol(module, "local_helper"));
    }

    #[test]
    fn typer_command_callback_is_skipped() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/cli.py",
            "import typer\n\napp = typer.Typer()\n\n@app.command()\ndef run() -> None:\n    pass\n\ndef local_helper() -> int:\n    return 1\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.cli"];
        assert!(!has_symbol(module, "app"));
        assert!(!has_symbol(module, "run"));
        assert!(has_symbol(module, "local_helper"));
    }

    #[test]
    fn pydantic_models_and_their_subclasses_are_skipped_entirely() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/api.py",
            "from pydantic import BaseModel\n\nclass BasePayload(BaseModel):\n    name: str\n\nclass ExtendedPayload(BasePayload):\n    age: int\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.api"];
        assert!(!has_symbol(module, "BasePayload"));
        assert!(!has_symbol(module, "ExtendedPayload"));
        assert!(!has_private_symbol(module, "BasePayload"));
    }

    #[test]
    fn pytest_fixture_bare_and_called_and_async_are_skipped() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/conftest_helpers.py",
            "import pytest\n\n@pytest.fixture\ndef bare_fixture() -> int:\n    return 1\n\n@pytest.fixture()\ndef called_fixture() -> int:\n    return 1\n\n@pytest.fixture\nasync def async_fixture() -> int:\n    return 1\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.conftest_helpers"];
        assert!(!has_symbol(module, "bare_fixture"));
        assert!(!has_symbol(module, "called_fixture"));
        assert!(!has_symbol(module, "async_fixture"));
    }

    #[test]
    fn privata_ignore_comment_suppresses_the_symbol_on_that_line() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def helper() -> int:  # privata: ignore\n    return 1\n\n\ndef other() -> int:\n    return 2\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        let module = &modules["pkg.mod"];
        assert!(!has_symbol(module, "helper"));
        assert!(has_symbol(module, "other"));
    }

    #[test]
    fn module_name_strips_init_and_joins_package_path() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/__init__.py",
            "def helper() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "src/pkg/sub/mod.py",
            "def other() -> int:\n    return 1\n",
        );
        let modules = collect_modules(&[tmp.path().join("src")]);
        assert!(modules.contains_key("pkg"));
        assert!(modules.contains_key("pkg.sub.mod"));
        assert_eq!(modules["pkg"].package_parts, vec!["pkg".to_string()]);
        assert_eq!(
            modules["pkg.sub.mod"].package_parts,
            vec!["pkg".to_string(), "sub".to_string()]
        );
    }

    #[test]
    fn unparsable_file_is_reported_with_its_line_and_a_message() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "src/pkg/__init__.py", "");
        write(tmp.path(), "src/pkg/broken.py", "def oops(:\n    pass\n");
        let (modules, unparsable) = collect_modules_with_errors(&[tmp.path().join("src")]);
        assert!(!modules.contains_key("pkg.broken"));
        assert_eq!(unparsable.len(), 1);
        assert_eq!(unparsable[0].module, "pkg.broken");
        assert_eq!(unparsable[0].lineno, 1);
        assert!(!unparsable[0].message.is_empty());
    }

    #[test]
    fn module_collisions_report_every_path_for_a_shared_name() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "src/utils.py", "");
        write(tmp.path(), "lib/utils.py", "");
        let collisions =
            collect_module_collisions(&[tmp.path().join("src"), tmp.path().join("lib")]);
        assert_eq!(collisions.len(), 1);
        assert_eq!(collisions[0].module, "utils");
        assert_eq!(collisions[0].paths.len(), 2);
    }

    #[test]
    fn first_source_root_wins_a_module_name_collision() {
        let tmp = TempDir::new().unwrap();
        let first = write(
            tmp.path(),
            "src/utils.py",
            "def a() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "lib/utils.py",
            "def b() -> int:\n    return 1\n",
        );
        let modules = collect_modules(&[tmp.path().join("src"), tmp.path().join("lib")]);
        assert_eq!(modules["utils"].path, first);
        assert!(has_symbol(&modules["utils"], "a"));
    }

    #[test]
    fn test_consumers_collect_test_shaped_files_from_a_test_root() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "tests/test_mod.py", "import pkg.mod\n");
        write(tmp.path(), "tests/helpers.py", "VALUE = 1\n");
        let consumers = collect_test_consumers(&[tmp.path().join("tests")], &[]);
        assert!(consumers.contains_key("test_mod"));
        assert!(
            !consumers.contains_key("helpers"),
            "non test-shaped files in a test root are not consumers"
        );
    }

    #[test]
    fn test_consumers_collect_nested_test_files_from_production_roots() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "src/pkg/test_helper.py", "import pkg.mod\n");
        let consumers = collect_test_consumers(&[], &[tmp.path().join("src")]);
        assert!(consumers.contains_key("pkg.test_helper"));
    }
}
