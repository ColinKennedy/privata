//! Re-export discovery for public classes, in packages and facade modules.

use std::collections::{HashMap, HashSet};

use ruff_python_ast::{Expr, Stmt, StmtImportFrom};

use crate::ast_utils::{dotted_name, NAMESPACE_SEPARATOR};
use crate::imports::resolve_import_source;
use crate::models::Module;

/// Return classes exposed by runtime imports in package and facade modules.
///
/// A package `__init__.py` re-exports whatever it imports. Any other module
/// re-exports only what its `__all__` names, which is the explicit form of
/// the same intent: a facade such as `pkg/api.py` that imports `Service` and
/// lists it in `__all__` publishes that class just as an `__init__.py` would.
pub fn collect_reexports(modules: &HashMap<String, Module>) -> HashSet<(String, String)> {
    let mut reexports = HashSet::new();

    for module in modules.values() {
        let Some(tree) = &module.tree else { continue };
        let is_package_init =
            module.path.file_name().and_then(|n| n.to_str()) == Some("__init__.py");
        if !is_package_init && module.exports.is_empty() {
            continue;
        }

        for node in runtime_module_imports(&tree.body) {
            let module_attr = node.module.as_ref().map(|m| m.id.as_str());
            let Some(source) =
                resolve_import_source(&module.package_parts, node.level, module_attr)
            else {
                continue;
            };
            let Some(source_module) = modules.get(&source) else {
                continue;
            };

            if node.names.iter().any(|alias| alias.name.id.as_str() == "*") {
                for symbol in &source_module.symbols {
                    if is_exposed(&symbol.name, module, is_package_init) {
                        reexports.insert((source.clone(), symbol.name.clone()));
                    }
                }
            }

            let defined: HashSet<&str> = source_module
                .symbols
                .iter()
                .map(|s| s.name.as_str())
                .collect();
            for alias in &node.names {
                let local = alias
                    .asname
                    .as_ref()
                    .map(|a| a.id.as_str())
                    .unwrap_or(alias.name.id.as_str());
                if alias.name.id.as_str() == "*" || !is_exposed(local, module, is_package_init) {
                    continue;
                }
                let submodule = format!("{source}{NAMESPACE_SEPARATOR}{}", alias.name.id);
                if !modules.contains_key(&submodule) && defined.contains(alias.name.id.as_str()) {
                    reexports.insert((source.clone(), alias.name.id.to_string()));
                }
            }
        }
    }
    reexports
}

/// Return whether a module publishes an imported name to its consumers.
fn is_exposed(local: &str, module: &Module, is_package_init: bool) -> bool {
    if !is_package_init {
        return module.exports.contains(local);
    }
    !local.starts_with('_') || module.exports.contains(local)
}

/// Yield imports that can create module-level runtime bindings.
fn runtime_module_imports(statements: &[Stmt]) -> Vec<&StmtImportFrom> {
    let mut out = Vec::new();
    runtime_module_imports_into(statements, &mut out);
    out
}

fn runtime_module_imports_into<'a>(statements: &'a [Stmt], out: &mut Vec<&'a StmtImportFrom>) {
    for node in statements {
        match node {
            Stmt::ImportFrom(import) => out.push(import),
            Stmt::If(if_stmt) => {
                let guard = type_checking_guard(&if_stmt.test);
                match guard {
                    Some(true) => runtime_clauses_into(&if_stmt.elif_else_clauses, out),
                    Some(false) => runtime_module_imports_into(&if_stmt.body, out),
                    None => {
                        runtime_module_imports_into(&if_stmt.body, out);
                        runtime_clauses_into(&if_stmt.elif_else_clauses, out);
                    }
                }
            }
            Stmt::Try(try_stmt) => {
                runtime_module_imports_into(&try_stmt.body, out);
                runtime_module_imports_into(&try_stmt.orelse, out);
                runtime_module_imports_into(&try_stmt.finalbody, out);
                for handler in &try_stmt.handlers {
                    let ruff_python_ast::ExceptHandler::ExceptHandler(handler) = handler;
                    runtime_module_imports_into(&handler.body, out);
                }
            }
            Stmt::For(for_stmt) => {
                runtime_module_imports_into(&for_stmt.body, out);
                runtime_module_imports_into(&for_stmt.orelse, out);
            }
            Stmt::While(while_stmt) => {
                runtime_module_imports_into(&while_stmt.body, out);
                runtime_module_imports_into(&while_stmt.orelse, out);
            }
            Stmt::With(with_stmt) => {
                runtime_module_imports_into(&with_stmt.body, out);
            }
            Stmt::Match(match_stmt) => {
                for case in &match_stmt.cases {
                    runtime_module_imports_into(&case.body, out);
                }
            }
            _ => {}
        }
    }
}

/// Process an elif/else chain the way Python's `test`/`body`/`orelse` shape
/// would: an elif clause behaves like a nested `if`, and the final `else`
/// (a clause with no test) behaves like a plain `orelse` body.
fn runtime_clauses_into<'a>(
    clauses: &'a [ruff_python_ast::ElifElseClause],
    out: &mut Vec<&'a StmtImportFrom>,
) {
    let Some((first, rest)) = clauses.split_first() else {
        return;
    };
    match &first.test {
        None => runtime_module_imports_into(&first.body, out),
        Some(test) => match type_checking_guard(test) {
            Some(true) => runtime_clauses_into(rest, out),
            Some(false) => runtime_module_imports_into(&first.body, out),
            None => {
                runtime_module_imports_into(&first.body, out);
                runtime_clauses_into(rest, out);
            }
        },
    }
}

/// Return whether a conventional guard disables its body at runtime.
fn type_checking_guard(expr: &Expr) -> Option<bool> {
    if let Expr::UnaryOp(unary) = expr {
        if matches!(unary.op, ruff_python_ast::UnaryOp::Not) {
            return type_checking_guard(&unary.operand).map(|guarded| !guarded);
        }
    }
    match dotted_name(expr).as_deref() {
        Some("TYPE_CHECKING") | Some("typing.TYPE_CHECKING") => Some(true),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::modules::collect_modules;
    use crate::source_roots::source_roots;
    use tempfile::TempDir;

    fn write(root: &std::path::Path, rel: &str, contents: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, contents).unwrap();
    }

    #[test]
    fn package_init_reexports_every_public_import() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/service.py",
            "class Service:\n    pass\n",
        );
        write(
            tmp.path(),
            "src/pkg/__init__.py",
            "from pkg.service import Service\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let reexports = collect_reexports(&modules);
        assert!(reexports.contains(&("pkg.service".to_string(), "Service".to_string())));
    }

    #[test]
    fn facade_module_reexports_only_names_listed_in_all() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/service.py",
            "class Service:\n    pass\nclass Other:\n    pass\n",
        );
        write(
            tmp.path(),
            "src/pkg/api.py",
            "from pkg.service import Service, Other\n\n__all__ = [\"Service\"]\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let reexports = collect_reexports(&modules);
        assert!(reexports.contains(&("pkg.service".to_string(), "Service".to_string())));
        assert!(!reexports.contains(&("pkg.service".to_string(), "Other".to_string())));
    }

    #[test]
    fn type_checking_guarded_import_is_not_a_runtime_reexport() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/service.py",
            "class Service:\n    pass\n",
        );
        write(
            tmp.path(),
            "src/pkg/__init__.py",
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from pkg.service import Service\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let reexports = collect_reexports(&modules);
        assert!(!reexports.contains(&("pkg.service".to_string(), "Service".to_string())));
    }

    #[test]
    fn star_import_in_package_init_reexports_all_public_symbols() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/service.py",
            "class Service:\n    pass\n\ndef helper() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "src/pkg/__init__.py",
            "from pkg.service import *\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let reexports = collect_reexports(&modules);
        assert!(reexports.contains(&("pkg.service".to_string(), "Service".to_string())));
        assert!(reexports.contains(&("pkg.service".to_string(), "helper".to_string())));
    }

    #[test]
    fn non_facade_module_without_all_reexports_nothing() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/service.py",
            "class Service:\n    pass\n",
        );
        write(
            tmp.path(),
            "src/pkg/other.py",
            "from pkg.service import Service\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let reexports = collect_reexports(&modules);
        assert!(!reexports.contains(&("pkg.service".to_string(), "Service".to_string())));
    }
}
