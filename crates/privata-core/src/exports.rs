//! Validation for literal `__all__` declarations.

use std::collections::{HashMap, HashSet};

use rayon::prelude::*;
use ruff_python_ast::{Expr, ModModule, Stmt};

use crate::ast_utils::{lineno_at, names_from_target, string_literal_set, NAMESPACE_SEPARATOR};
use crate::models::{ExportIssue, ExportIssueKind, Module};

const IGNORED_PUBLIC_BINDINGS: &[&str] = &["logger"];

/// Return mismatches between literal `__all__` and public bindings.
pub fn collect_export_issues(modules: &HashMap<String, Module>) -> Vec<ExportIssue> {
    let mut issues: Vec<ExportIssue> = modules
        .par_iter()
        .flat_map_iter(|(_, module)| {
            let mut module_issues = Vec::new();
            let Some(tree) = &module.tree else {
                return module_issues;
            };
            let Some(index) = &module.line_index else {
                return module_issues;
            };

            let (Some(all_names), all_offset) = literal_all(tree) else {
                return module_issues;
            };
            let lineno = lineno_at(index, all_offset);

            let all_bindings = collect_all_bindings(tree);
            let public_bindings = collect_public_bindings(tree);

            let mut unknown: Vec<&String> = all_names.difference(&all_bindings).collect();
            unknown.sort();
            module_issues.extend(unknown.into_iter().map(|name| ExportIssue {
                module: module.name.clone(),
                path: module.path.clone(),
                name: name.clone(),
                kind: ExportIssueKind::Unknown,
                lineno,
            }));

            let mut private: Vec<&String> = all_names
                .intersection(&all_bindings)
                .filter(|name| is_private(name))
                .collect();
            private.sort();
            module_issues.extend(private.into_iter().map(|name| ExportIssue {
                module: module.name.clone(),
                path: module.path.clone(),
                name: name.clone(),
                kind: ExportIssueKind::Private,
                lineno,
            }));

            let mut missing: Vec<&String> = public_bindings.difference(&all_names).collect();
            missing.sort();
            module_issues.extend(missing.into_iter().map(|name| ExportIssue {
                module: module.name.clone(),
                path: module.path.clone(),
                name: name.clone(),
                kind: ExportIssueKind::Missing,
                lineno,
            }));

            module_issues
        })
        .collect();

    issues.sort_by(|a, b| {
        (
            a.path.to_string_lossy(),
            a.lineno,
            a.kind.to_string(),
            &a.name,
        )
            .cmp(&(
                b.path.to_string_lossy(),
                b.lineno,
                b.kind.to_string(),
                &b.name,
            ))
    });
    issues
}

fn literal_all(tree: &ModModule) -> (Option<HashSet<String>>, ruff_text_size::TextSize) {
    for node in &tree.body {
        if let Stmt::Assign(assign) = node {
            for target in &assign.targets {
                if let Expr::Name(name) = target {
                    if name.id.as_str() == "__all__" {
                        return (string_literal_set(&assign.value), assign.range.start());
                    }
                }
            }
        }
    }
    (None, ruff_text_size::TextSize::default())
}

fn collect_all_bindings(tree: &ModModule) -> HashSet<String> {
    let mut bindings = HashSet::new();
    collect_bound_names(&tree.body, &mut bindings, false, true);
    bindings
}

fn collect_public_bindings(tree: &ModModule) -> HashSet<String> {
    let mut bindings = HashSet::new();
    collect_bound_names(&tree.body, &mut bindings, true, false);
    bindings
}

fn collect_bound_names(
    statements: &[Stmt],
    bindings: &mut HashSet<String>,
    public_only: bool,
    include_imports: bool,
) {
    for node in statements {
        let is_import = matches!(node, Stmt::Import(_) | Stmt::ImportFrom(_));
        if include_imports || !is_import {
            for name in bound_names(node) {
                add_binding(bindings, name, public_only);
            }
        }
        for nested in nested_public_binding_statements(node) {
            collect_bound_names(nested, bindings, public_only, include_imports);
        }
    }
}

fn bound_names(node: &Stmt) -> Vec<String> {
    match node {
        Stmt::FunctionDef(f) => vec![f.name.id.to_string()],
        Stmt::ClassDef(c) => vec![c.name.id.to_string()],
        Stmt::Assign(a) => a
            .targets
            .iter()
            .flat_map(names_from_target)
            .filter(|name| name != "__all__")
            .collect(),
        Stmt::AnnAssign(a) => names_from_target(&a.target),
        Stmt::TypeAlias(t) => names_from_target(&t.name),
        Stmt::Import(import) => import
            .names
            .iter()
            .map(|alias| {
                alias
                    .asname
                    .as_ref()
                    .map(|a| a.id.to_string())
                    .unwrap_or_else(|| {
                        alias
                            .name
                            .id
                            .split(NAMESPACE_SEPARATOR)
                            .next()
                            .unwrap_or(alias.name.id.as_str())
                            .to_string()
                    })
            })
            .collect(),
        Stmt::ImportFrom(import) => import_from_bound_names(import),
        _ => Vec::new(),
    }
}

fn import_from_bound_names(node: &ruff_python_ast::StmtImportFrom) -> Vec<String> {
    if node.module.as_ref().map(|m| m.id.as_str()) == Some("__future__") {
        return Vec::new();
    }
    node.names
        .iter()
        .filter(|alias| alias.name.id.as_str() != "*")
        .map(|alias| {
            alias
                .asname
                .as_ref()
                .map(|a| a.id.to_string())
                .unwrap_or_else(|| alias.name.id.to_string())
        })
        .collect()
}

fn nested_public_binding_statements(node: &Stmt) -> Vec<&[Stmt]> {
    let Stmt::Try(try_stmt) = node else {
        return Vec::new();
    };
    let mut groups: Vec<&[Stmt]> = vec![&try_stmt.body, &try_stmt.orelse, &try_stmt.finalbody];
    for handler in &try_stmt.handlers {
        let ruff_python_ast::ExceptHandler::ExceptHandler(handler) = handler;
        groups.push(&handler.body);
    }
    groups
}

fn add_binding(bindings: &mut HashSet<String>, name: String, public_only: bool) {
    if public_only && IGNORED_PUBLIC_BINDINGS.contains(&name.as_str()) {
        return;
    }
    if public_only && is_private(&name) {
        return;
    }
    bindings.insert(name);
}

fn is_private(name: &str) -> bool {
    name.starts_with('_') && !(name.starts_with("__") && name.ends_with("__"))
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

    fn modules_for(tmp: &TempDir) -> HashMap<String, Module> {
        collect_modules(&source_roots(tmp.path()))
    }

    #[test]
    fn no_literal_all_produces_no_issues() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def helper() -> int:\n    return 1\n",
        );
        let modules = modules_for(&tmp);
        assert!(collect_export_issues(&modules).is_empty());
    }

    #[test]
    fn all_naming_unbound_name_is_unknown() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "__all__ = [\"helper\", \"missing_name\"]\n\n\ndef helper() -> int:\n    return 1\n",
        );
        let modules = modules_for(&tmp);
        let issues = collect_export_issues(&modules);
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].kind.to_string(), "unknown");
        assert_eq!(issues[0].name, "missing_name");
    }

    #[test]
    fn all_naming_private_binding_is_private() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "__all__ = [\"_helper\"]\n\n\ndef _helper() -> int:\n    return 1\n",
        );
        let modules = modules_for(&tmp);
        let issues = collect_export_issues(&modules);
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].kind.to_string(), "private");
        assert_eq!(issues[0].name, "_helper");
    }

    #[test]
    fn public_binding_missing_from_all_is_reported() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "__all__ = [\"helper\"]\n\n\ndef helper() -> int:\n    return 1\n\n\ndef other() -> int:\n    return 2\n",
        );
        let modules = modules_for(&tmp);
        let issues = collect_export_issues(&modules);
        assert_eq!(issues.len(), 1);
        assert_eq!(issues[0].kind.to_string(), "missing");
        assert_eq!(issues[0].name, "other");
    }

    #[test]
    fn exact_match_produces_no_issues() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "__all__ = [\"helper\"]\n\n\ndef helper() -> int:\n    return 1\n",
        );
        let modules = modules_for(&tmp);
        assert!(collect_export_issues(&modules).is_empty());
    }

    #[test]
    fn non_literal_all_is_treated_as_absent() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def _compute() -> list[str]:\n    return []\n\n\n__all__ = _compute()\n\n\ndef helper() -> int:\n    return 1\n",
        );
        let modules = modules_for(&tmp);
        assert!(collect_export_issues(&modules).is_empty());
    }

    #[test]
    fn bindings_inside_try_are_counted() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "__all__ = []\n\ntry:\n    import tomllib\nexcept ImportError:\n    import tomli as tomllib\n",
        );
        let modules = modules_for(&tmp);
        // `tomllib` is bound via Import inside a Try; include_imports=True picks it up for the
        // "unknown" check (nothing here) but it is excluded from public_bindings since it's an
        // import, so it must not show up as "missing".
        let issues = collect_export_issues(&modules);
        assert!(issues.iter().all(|i| i.name != "tomllib"));
    }
}
