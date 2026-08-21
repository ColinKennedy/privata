//! Import analysis for public symbols and private modules.

use std::collections::{HashMap, HashSet};

use rayon::prelude::*;
use ruff_python_ast::visitor::{walk_body, walk_expr, Visitor};
use ruff_python_ast::{Expr, Stmt};

use crate::ast_utils::{dotted_name, lineno_at, NAMESPACE_SEPARATOR};
use crate::models::{Module, PrivateModuleImport, PrivateSymbolImport, Symbol};

/// Resolve a dotted base to a module by matching its longest alias prefix.
fn resolve_alias_prefix(base: &str, import_aliases: &HashMap<String, String>) -> Option<String> {
    let parts: Vec<&str> = base.split(NAMESPACE_SEPARATOR).collect();
    for i in (1..=parts.len()).rev() {
        let prefix = parts[..i].join(NAMESPACE_SEPARATOR);
        if let Some(aliased) = import_aliases.get(&prefix) {
            let suffix = parts[i..].join(NAMESPACE_SEPARATOR);
            return Some(if suffix.is_empty() {
                aliased.clone()
            } else {
                format!("{aliased}{NAMESPACE_SEPARATOR}{suffix}")
            });
        }
    }
    None
}

/// Return whether any segment of a dotted module path is private.
fn is_private_module_name(module_name: &str) -> bool {
    module_name
        .split(NAMESPACE_SEPARATOR)
        .any(|part| part.starts_with('_'))
}

/// Return the package that owns a private module.
fn private_module_owner_package(module_name: &str) -> String {
    match module_name.rsplit_once(NAMESPACE_SEPARATOR) {
        Some((head, _)) => head.to_string(),
        None => module_name.to_string(),
    }
}

/// Return whether a module is inside a package subtree.
fn module_is_within_package(module_name: &str, package_name: &str) -> bool {
    module_name == package_name
        || module_name.starts_with(&format!("{package_name}{NAMESPACE_SEPARATOR}"))
}

/// Resolve a relative import to an absolute dotted module name.
pub fn resolve_import_source(
    importer_package: &[String],
    level: u32,
    module_attr: Option<&str>,
) -> Option<String> {
    if level == 0 {
        return module_attr.map(str::to_string);
    }

    let up = (level - 1) as usize;
    if up > importer_package.len() {
        return None;
    }
    let mut base: Vec<String> = importer_package[..importer_package.len() - up].to_vec();
    if let Some(attr) = module_attr {
        base.extend(attr.split(NAMESPACE_SEPARATOR).map(str::to_string));
    }
    if base.is_empty() {
        None
    } else {
        Some(base.join(NAMESPACE_SEPARATOR))
    }
}

struct ImportAliasPass<'a> {
    modules: &'a HashSet<String>,
    defined: &'a HashMap<String, HashSet<String>>,
    consumer_name: &'a str,
    package_parts: &'a [String],
    import_aliases: HashMap<String, String>,
    imported_modules: HashSet<String>,
    used: HashSet<(String, String)>,
}

impl<'a, 'b> Visitor<'b> for ImportAliasPass<'a> {
    fn visit_stmt(&mut self, stmt: &'b Stmt) {
        match stmt {
            Stmt::Import(import) => {
                for alias in &import.names {
                    if let Some(asname) = &alias.asname {
                        self.import_aliases
                            .insert(asname.id.to_string(), alias.name.id.to_string());
                    } else {
                        self.imported_modules.insert(alias.name.id.to_string());
                    }
                }
            }
            Stmt::ImportFrom(import) => {
                let module_attr = import.module.as_ref().map(|m| m.id.as_str());
                if let Some(source) =
                    resolve_import_source(self.package_parts, import.level, module_attr)
                {
                    for alias in &import.names {
                        let sym = alias.name.id.as_str();
                        if sym == "*" {
                            if source != self.consumer_name {
                                if let Some(public_symbols) = self.defined.get(&source) {
                                    for public_symbol in public_symbols {
                                        self.used.insert((source.clone(), public_symbol.clone()));
                                    }
                                }
                            }
                            continue;
                        }

                        let submodule = format!("{source}{NAMESPACE_SEPARATOR}{sym}");
                        if self.modules.contains(&submodule) {
                            let local = alias
                                .asname
                                .as_ref()
                                .map(|a| a.id.to_string())
                                .unwrap_or_else(|| sym.to_string());
                            self.import_aliases.insert(local, submodule);
                            continue;
                        }

                        if source != self.consumer_name
                            && self.defined.get(&source).is_some_and(|d| d.contains(sym))
                        {
                            self.used.insert((source.clone(), sym.to_string()));
                        }
                    }
                }
            }
            _ => {}
        }
        ruff_python_ast::visitor::walk_stmt(self, stmt);
    }
}

struct AttributeUsagePass<'a> {
    defined: &'a HashMap<String, HashSet<String>>,
    consumer_name: &'a str,
    imported_modules: &'a HashSet<String>,
    import_aliases: &'a HashMap<String, String>,
    used: HashSet<(String, String)>,
}

impl<'a, 'b> Visitor<'b> for AttributeUsagePass<'a> {
    fn visit_expr(&mut self, expr: &'b Expr) {
        if let Expr::Attribute(attr) = expr {
            let attr_name = attr.attr.id.to_string();
            if let Some(base) = dotted_name(&attr.value) {
                if self.imported_modules.contains(&base)
                    && base != self.consumer_name
                    && self
                        .defined
                        .get(&base)
                        .is_some_and(|d| d.contains(&attr_name))
                {
                    self.used.insert((base, attr_name));
                } else if let Some(resolved) = resolve_alias_prefix(&base, self.import_aliases) {
                    if resolved != self.consumer_name
                        && self
                            .defined
                            .get(&resolved)
                            .is_some_and(|d| d.contains(&attr_name))
                    {
                        self.used.insert((resolved, attr_name));
                    }
                }
            }
        }
        walk_expr(self, expr);
    }
}

/// Return symbols of `modules` that another module imports or uses.
///
/// Imports are scanned in `consumers`, which defaults to `modules` themselves.
pub fn find_cross_imports(
    modules: &HashMap<String, Module>,
    consumers: Option<&HashMap<String, Module>>,
) -> HashSet<(String, String)> {
    let known: HashSet<String> = modules.keys().cloned().collect();
    let defined: HashMap<String, HashSet<String>> = modules
        .iter()
        .map(|(name, m)| {
            (
                name.clone(),
                m.symbols.iter().map(|s| s.name.clone()).collect(),
            )
        })
        .collect();
    let consumers_ref = consumers.unwrap_or(modules);

    consumers_ref
        .par_iter()
        .filter_map(|(consumer_name, consumer)| {
            let tree = consumer.tree.as_ref()?;
            Some(cross_imports_in_tree(
                tree,
                &consumer.package_parts,
                consumer_name,
                &known,
                &defined,
            ))
        })
        .reduce(HashSet::new, |mut a, b| {
            a.extend(b);
            a
        })
}

/// The per-consumer body of [`find_cross_imports`], usable directly by
/// callers (such as the checker's test-helper pass) that already hold a
/// root-scoped, borrowed view of the module maps.
pub(crate) fn cross_imports_in_tree(
    tree: &ruff_python_ast::ModModule,
    package_parts: &[String],
    consumer_name: &str,
    known: &HashSet<String>,
    defined: &HashMap<String, HashSet<String>>,
) -> HashSet<(String, String)> {
    let mut used = HashSet::new();

    let mut alias_pass = ImportAliasPass {
        modules: known,
        defined,
        consumer_name,
        package_parts,
        import_aliases: HashMap::new(),
        imported_modules: HashSet::new(),
        used: HashSet::new(),
    };
    walk_body(&mut alias_pass, &tree.body);
    used.extend(alias_pass.used);

    let mut attr_pass = AttributeUsagePass {
        defined,
        consumer_name,
        imported_modules: &alias_pass.imported_modules,
        import_aliases: &alias_pass.import_aliases,
        used: HashSet::new(),
    };
    walk_body(&mut attr_pass, &tree.body);
    used.extend(attr_pass.used);

    used
}

struct PrivateModuleImportPass<'a> {
    consumer: &'a Module,
    private_modules: &'a HashSet<String>,
    findings: HashSet<(String, u32)>,
}

impl<'a, 'b> Visitor<'b> for PrivateModuleImportPass<'a> {
    fn visit_stmt(&mut self, stmt: &'b Stmt) {
        let index = self.consumer.line_index.as_ref();
        match stmt {
            Stmt::Import(import) => {
                for alias in &import.names {
                    if self.private_modules.contains(alias.name.id.as_str()) {
                        let lineno = index
                            .map(|i| lineno_at(i, alias.range.start()))
                            .unwrap_or(0);
                        self.findings.insert((alias.name.id.to_string(), lineno));
                    }
                }
            }
            Stmt::ImportFrom(import) => {
                let module_attr = import.module.as_ref().map(|m| m.id.as_str());
                if let Some(source) =
                    resolve_import_source(&self.consumer.package_parts, import.level, module_attr)
                {
                    if self.private_modules.contains(&source) {
                        let lineno = index
                            .map(|i| lineno_at(i, import.range.start()))
                            .unwrap_or(0);
                        self.findings.insert((source.clone(), lineno));
                    }
                    for alias in &import.names {
                        if alias.name.id.as_str() == "*" {
                            continue;
                        }
                        let submodule = format!("{source}{NAMESPACE_SEPARATOR}{}", alias.name.id);
                        if self.private_modules.contains(&submodule) {
                            let lineno = index
                                .map(|i| lineno_at(i, alias.range.start()))
                                .unwrap_or(0);
                            self.findings.insert((submodule, lineno));
                        }
                    }
                }
            }
            _ => {}
        }
        ruff_python_ast::visitor::walk_stmt(self, stmt);
    }
}

/// Return private modules imported from outside their package subtree.
pub fn collect_private_module_imports(
    modules: &HashMap<String, Module>,
) -> Vec<PrivateModuleImport> {
    let private_modules: HashSet<String> = modules
        .keys()
        .filter(|name| is_private_module_name(name))
        .cloned()
        .collect();

    let per_consumer: Vec<((String, String, u32), PrivateModuleImport)> = modules
        .par_iter()
        .filter_map(|(_, consumer)| {
            let tree = consumer.tree.as_ref()?;
            let mut pass = PrivateModuleImportPass {
                consumer,
                private_modules: &private_modules,
                findings: HashSet::new(),
            };
            walk_body(&mut pass, &tree.body);

            let mut local = Vec::new();
            for (private_module_name, lineno) in pass.findings {
                if consumer.ignored_lines.contains(&lineno) {
                    continue;
                }
                if private_module_name == consumer.name {
                    continue;
                }
                let owner_package = private_module_owner_package(&private_module_name);
                if module_is_within_package(&consumer.name, &owner_package) {
                    continue;
                }
                local.push((
                    (private_module_name.clone(), consumer.name.clone(), lineno),
                    PrivateModuleImport {
                        module: private_module_name.clone(),
                        path: modules[&private_module_name].path.clone(),
                        imported_by: consumer.name.clone(),
                        imported_by_path: consumer.path.clone(),
                        lineno,
                    },
                ));
            }
            Some(local)
        })
        .flatten()
        .collect();

    let mut findings: HashMap<(String, String, u32), PrivateModuleImport> = HashMap::new();
    for (key, finding) in per_consumer {
        findings.entry(key).or_insert(finding);
    }

    let mut result: Vec<PrivateModuleImport> = findings.into_values().collect();
    result.sort_by(|a, b| {
        (a.imported_by_path.to_string_lossy(), a.lineno, &a.module).cmp(&(
            b.imported_by_path.to_string_lossy(),
            b.lineno,
            &b.module,
        ))
    });
    result
}

struct PrivateSymbolImportPass<'a> {
    consumer: &'a Module,
    private_symbols: &'a HashMap<String, HashMap<String, &'a Symbol>>,
    findings: HashSet<(String, String, u32)>,
}

impl<'a, 'b> Visitor<'b> for PrivateSymbolImportPass<'a> {
    fn visit_stmt(&mut self, stmt: &'b Stmt) {
        if let Stmt::ImportFrom(import) = stmt {
            let module_attr = import.module.as_ref().map(|m| m.id.as_str());
            if let Some(source) =
                resolve_import_source(&self.consumer.package_parts, import.level, module_attr)
            {
                if let Some(symbols) = self.private_symbols.get(&source) {
                    for alias in &import.names {
                        let name = alias.name.id.as_str();
                        if name == "*" || !symbols.contains_key(name) {
                            continue;
                        }
                        let lineno = self
                            .consumer
                            .line_index
                            .as_ref()
                            .map(|i| lineno_at(i, alias.range.start()))
                            .unwrap_or(0);
                        if self.consumer.ignored_lines.contains(&lineno) {
                            continue;
                        }
                        self.findings
                            .insert((source.clone(), name.to_string(), lineno));
                    }
                }
            }
        }
        ruff_python_ast::visitor::walk_stmt(self, stmt);
    }
}

/// Return private top-level symbols imported from another production module.
pub fn collect_private_symbol_imports(
    modules: &HashMap<String, Module>,
) -> Vec<PrivateSymbolImport> {
    let private_symbols: HashMap<String, HashMap<String, &Symbol>> = modules
        .iter()
        .filter(|(_, module)| !module.private_symbols.is_empty())
        .map(|(name, module)| {
            (
                name.clone(),
                module
                    .private_symbols
                    .iter()
                    .map(|s| (s.name.clone(), s))
                    .collect(),
            )
        })
        .collect();

    let per_consumer: Vec<((String, String, String, u32), PrivateSymbolImport)> = modules
        .par_iter()
        .filter_map(|(_, consumer)| {
            let tree = consumer.tree.as_ref()?;
            let mut pass = PrivateSymbolImportPass {
                consumer,
                private_symbols: &private_symbols,
                findings: HashSet::new(),
            };
            walk_body(&mut pass, &tree.body);

            let mut local = Vec::new();
            for (source, name, lineno) in pass.findings {
                if source == consumer.name {
                    continue;
                }
                let symbol = private_symbols[&source][&name];
                local.push((
                    (source.clone(), name.clone(), consumer.name.clone(), lineno),
                    PrivateSymbolImport {
                        module: source.clone(),
                        name: name.clone(),
                        path: symbol.path.clone(),
                        imported_by: consumer.name.clone(),
                        imported_by_path: consumer.path.clone(),
                        lineno,
                    },
                ));
            }
            Some(local)
        })
        .flatten()
        .collect();

    let mut findings: HashMap<(String, String, String, u32), PrivateSymbolImport> = HashMap::new();
    for (key, finding) in per_consumer {
        findings.entry(key).or_insert(finding);
    }

    let mut result: Vec<PrivateSymbolImport> = findings.into_values().collect();
    result.sort_by(|a, b| {
        (
            a.imported_by_path.to_string_lossy(),
            a.lineno,
            &a.module,
            &a.name,
        )
            .cmp(&(
                b.imported_by_path.to_string_lossy(),
                b.lineno,
                &b.module,
                &b.name,
            ))
    });
    result
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
    fn resolve_import_source_handles_absolute_and_relative_imports() {
        let pkg = vec!["pkg".to_string(), "sub".to_string()];
        assert_eq!(
            resolve_import_source(&pkg, 0, Some("other.mod")),
            Some("other.mod".to_string())
        );
        // `from . import x` from pkg.sub -> pkg.sub
        assert_eq!(
            resolve_import_source(&pkg, 1, None),
            Some("pkg.sub".to_string())
        );
        // `from .. import x` from pkg.sub -> pkg
        assert_eq!(
            resolve_import_source(&pkg, 2, None),
            Some("pkg".to_string())
        );
        // `from .sibling import x` from pkg.sub -> pkg.sub.sibling
        assert_eq!(
            resolve_import_source(&pkg, 1, Some("sibling")),
            Some("pkg.sub.sibling".to_string())
        );
        // climbing past the package root is unresolvable
        assert_eq!(resolve_import_source(&pkg, 4, None), None);
    }

    #[test]
    fn private_module_within_same_package_is_not_flagged() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/one/_internal.py",
            "def helper() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "src/pkg/one/public.py",
            "from ._internal import helper\n\nVALUE = helper()\n",
        );
        let modules = modules_for(&tmp);
        assert!(collect_private_module_imports(&modules).is_empty());
    }

    #[test]
    fn private_module_imported_from_other_package_is_reported() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "src/pkg/one/_internal.py", "VALUE = 1\n");
        write(
            tmp.path(),
            "src/pkg/two/public.py",
            "from pkg.one import _internal\n\nVALUE = _internal.VALUE\n",
        );
        let modules = modules_for(&tmp);
        let findings = collect_private_module_imports(&modules);
        assert!(findings
            .iter()
            .any(|f| f.module == "pkg.one._internal" && f.imported_by == "pkg.two.public"));
    }

    #[test]
    fn private_symbol_import_is_reported_with_source_and_consumer() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/producer.py",
            "class _PrivateService:\n    pass\n",
        );
        write(
            tmp.path(),
            "src/pkg/consumer.py",
            "from .producer import _PrivateService\n",
        );
        let modules = modules_for(&tmp);
        let findings = collect_private_symbol_imports(&modules);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].module, "pkg.producer");
        assert_eq!(findings[0].name, "_PrivateService");
        assert_eq!(findings[0].imported_by, "pkg.consumer");
    }

    #[test]
    fn private_symbol_self_import_is_ignored() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/producer.py",
            "from .producer import _PrivateService\n\nclass _PrivateService:\n    pass\n",
        );
        let modules = modules_for(&tmp);
        assert!(collect_private_symbol_imports(&modules).is_empty());
    }

    #[test]
    fn cross_import_detects_from_import_and_attribute_access() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/producer.py",
            "def helper() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "src/pkg/consumer_a.py",
            "from pkg.producer import helper\n\nVALUE = helper()\n",
        );
        write(
            tmp.path(),
            "src/pkg/consumer_b.py",
            "import pkg.producer\n\nVALUE = pkg.producer.helper()\n",
        );
        let modules = modules_for(&tmp);
        let used = find_cross_imports(&modules, None);
        assert!(used.contains(&("pkg.producer".to_string(), "helper".to_string())));
    }

    #[test]
    fn cross_import_star_import_marks_every_public_symbol_used() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/producer.py",
            "def helper() -> int:\n    return 1\n\n\ndef other() -> int:\n    return 2\n",
        );
        write(
            tmp.path(),
            "src/pkg/consumer.py",
            "from pkg.producer import *\n",
        );
        let modules = modules_for(&tmp);
        let used = find_cross_imports(&modules, None);
        assert!(used.contains(&("pkg.producer".to_string(), "helper".to_string())));
        assert!(used.contains(&("pkg.producer".to_string(), "other".to_string())));
    }

    #[test]
    fn cross_import_aliased_module_attribute_access_is_detected() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/producer.py",
            "def helper() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "src/pkg/consumer.py",
            "import pkg.producer as p\n\nVALUE = p.helper()\n",
        );
        let modules = modules_for(&tmp);
        let used = find_cross_imports(&modules, None);
        assert!(used.contains(&("pkg.producer".to_string(), "helper".to_string())));
    }

    #[test]
    fn self_use_within_the_defining_module_does_not_count_as_cross_import() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def helper() -> int:\n    return 1\n\n\ndef run() -> int:\n    return helper()\n",
        );
        let modules = modules_for(&tmp);
        let used = find_cross_imports(&modules, None);
        assert!(!used.contains(&("pkg.mod".to_string(), "helper".to_string())));
    }
}
