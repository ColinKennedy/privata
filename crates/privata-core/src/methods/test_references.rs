//! Reference attribution for co-located test helpers.

use std::collections::{HashMap, HashSet};

use ruff_python_ast::visitor::{walk_body, Visitor};
use ruff_python_ast::Stmt;

use crate::ast_utils::{referenced_names, NAMESPACE_SEPARATOR};
use crate::imports::resolve_import_source;
use crate::models::Module;

/// Return the names a test file mentions, for each helper module it imports.
///
/// Attribution is per file, not per receiver: every name the file mentions is
/// credited to every helper module it imports. A test that imports a helper is
/// taken to exercise it, so crediting the whole file can only keep a method
/// public that a finer reading would have flagged. That is the safe direction
/// for a checker whose findings are acted on by renaming a method.
pub fn referenced_names_by_module(
    module: &Module,
    known_modules: &HashMap<String, Module>,
) -> HashMap<String, HashSet<String>> {
    let known: HashSet<String> = known_modules.keys().cloned().collect();
    referenced_names_by_module_names(module, &known)
}

/// The same attribution as [`referenced_names_by_module`], for a caller that
/// only has the set of known module *names* (e.g. a root-scoped, borrowed
/// view of a larger module map).
pub(crate) fn referenced_names_by_module_names(
    module: &Module,
    known_module_names: &HashSet<String>,
) -> HashMap<String, HashSet<String>> {
    let Some(tree) = &module.tree else {
        return HashMap::new();
    };

    let imported = imported_known_modules(&tree.body, &module.package_parts, known_module_names);
    if imported.is_empty() {
        return HashMap::new();
    }

    let names = referenced_names(&tree.body);
    imported
        .into_iter()
        .map(|source| (source, names.clone()))
        .collect()
}

/// Return the known modules a file imports, at any nesting depth.
fn imported_known_modules(
    body: &[Stmt],
    package_parts: &[String],
    known_modules: &HashSet<String>,
) -> HashSet<String> {
    struct Collector<'a> {
        package_parts: &'a [String],
        known_modules: &'a HashSet<String>,
        imported: HashSet<String>,
    }
    impl<'a, 'b> Visitor<'b> for Collector<'a> {
        fn visit_stmt(&mut self, stmt: &'b Stmt) {
            match stmt {
                Stmt::Import(import) => {
                    for alias in &import.names {
                        if self.known_modules.contains(alias.name.id.as_str()) {
                            self.imported.insert(alias.name.id.to_string());
                        }
                    }
                }
                Stmt::ImportFrom(import) => {
                    let module_attr = import.module.as_ref().map(|m| m.id.as_str());
                    if let Some(source) =
                        resolve_import_source(self.package_parts, import.level, module_attr)
                    {
                        if self.known_modules.contains(&source) {
                            self.imported.insert(source.clone());
                        }
                        for alias in &import.names {
                            let submodule =
                                format!("{source}{NAMESPACE_SEPARATOR}{}", alias.name.id);
                            if self.known_modules.contains(&submodule) {
                                self.imported.insert(submodule);
                            }
                        }
                    }
                }
                _ => {}
            }
            ruff_python_ast::visitor::walk_stmt(self, stmt);
        }
    }

    let mut collector = Collector {
        package_parts,
        known_modules,
        imported: HashSet::new(),
    };
    walk_body(&mut collector, body);
    collector.imported
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
    fn credits_every_referenced_name_to_each_imported_known_module() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/helper.py",
            "def run() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "tests/test_helper.py",
            "from pkg.helper import run\n\nobj.run()\nobj.other_name\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let test_module = {
            let source = std::fs::read_to_string(tmp.path().join("tests/test_helper.py")).unwrap();
            let tree = ruff_python_parser::parse_module(&source)
                .unwrap()
                .into_syntax();
            let mut m = Module::new(
                "test_helper".to_string(),
                tmp.path().join("tests/test_helper.py"),
                Vec::new(),
            );
            m.tree = Some(tree);
            m
        };
        let refs = referenced_names_by_module(&test_module, &modules);
        assert!(refs.contains_key("pkg.helper"));
        assert!(refs["pkg.helper"].contains("run"));
        assert!(refs["pkg.helper"].contains("other_name"));
    }

    #[test]
    fn no_import_of_a_known_module_yields_no_references() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/helper.py",
            "def run() -> int:\n    return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let source = "print('hello')\n";
        let tree = ruff_python_parser::parse_module(source)
            .unwrap()
            .into_syntax();
        let mut m = Module::new(
            "unrelated".to_string(),
            tmp.path().join("unrelated.py"),
            Vec::new(),
        );
        m.tree = Some(tree);
        assert!(referenced_names_by_module(&m, &modules).is_empty());
    }
}
