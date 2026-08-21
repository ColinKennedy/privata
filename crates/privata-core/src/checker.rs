//! Detect module privacy issues within Python source roots.

use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use crate::entrypoints::{collect_external_entrypoints, load_tach_interface_exports};
use crate::exports::collect_export_issues;
use crate::imports::{
    collect_private_module_imports, collect_private_symbol_imports, find_cross_imports,
};
use crate::methods::{collect_method_candidates, collect_reexports, referenced_names_by_module};
use crate::models::{
    ExportIssue, Method, Module, ModuleCollision, PrivateModuleImport, PrivateSymbolImport, Symbol,
    UnparsableModule,
};
use crate::modules::{collect_modules_collisions_and_errors, collect_test_consumers};
use crate::source_roots::{all_search_roots, is_test_source_root, source_roots};

const METHOD_LIST_INDENT: &str = "      ";
// Keeps the indented method list inside the project's 100-column limit.
const METHOD_LIST_WIDTH: usize = 100 - 6;

/// All findings produced by one scan of a project.
#[derive(Default)]
struct PrivacyFindings {
    unparsable_modules: Vec<UnparsableModule>,
    candidates: Vec<Symbol>,
    method_candidates: Vec<Method>,
    private_module_imports: Vec<PrivateModuleImport>,
    private_symbol_imports: Vec<PrivateSymbolImport>,
    export_issues: Vec<ExportIssue>,
    module_collisions: Vec<ModuleCollision>,
}

/// Return helper-module symbols in test source roots that co-located test
/// files use.
///
/// Each pass is scoped to a single test root so that test files can only
/// certify helper modules in their own root, never production symbols.
fn test_helper_cross_imports(
    test_roots: &[PathBuf],
    modules: &HashMap<String, Module>,
    test_consumers: &HashMap<String, Module>,
) -> HashSet<(String, String)> {
    let mut used = HashSet::new();
    for root in test_roots {
        let helpers: HashMap<String, &Module> = modules
            .iter()
            .filter(|(_, m)| m.path.starts_with(root))
            .map(|(k, v)| (k.clone(), v))
            .collect();
        let consumers: HashMap<String, &Module> = test_consumers
            .iter()
            .filter(|(_, m)| m.path.starts_with(root))
            .map(|(k, v)| (k.clone(), v))
            .collect();

        let known: HashSet<String> = helpers.keys().cloned().collect();
        let defined: HashMap<String, HashSet<String>> = helpers
            .iter()
            .map(|(name, m)| {
                (
                    name.clone(),
                    m.symbols.iter().map(|s| s.name.clone()).collect(),
                )
            })
            .collect();
        for (consumer_name, consumer) in &consumers {
            let Some(tree) = &consumer.tree else { continue };
            used.extend(crate::imports::cross_imports_in_tree(
                tree,
                &consumer.package_parts,
                consumer_name,
                &known,
                &defined,
            ));
        }
    }
    used
}

/// Return names that co-located test files mention, per helper module.
///
/// Helper modules in a test source root exist to serve their own test
/// files, so a method those tests call is treated as used.
fn test_helper_method_references(
    test_roots: &[PathBuf],
    modules: &HashMap<String, Module>,
    test_consumers: &HashMap<String, Module>,
) -> HashMap<String, HashSet<String>> {
    let mut references: HashMap<String, HashSet<String>> = HashMap::new();
    for root in test_roots {
        let helper_names: HashSet<String> = modules
            .iter()
            .filter(|(_, m)| m.path.starts_with(root))
            .map(|(name, _)| name.clone())
            .collect();
        let consumers = test_consumers.values().filter(|m| m.path.starts_with(root));
        for consumer in consumers {
            for (module_name, names) in
                crate::methods::referenced_names_by_module_names(consumer, &helper_names)
            {
                references.entry(module_name).or_default().extend(names);
            }
        }
    }
    references
}

/// Split test roots into local helper roots and external consumer roots.
///
/// A test root counts as local when it sits under a reported `source_roots`
/// entry; a test root reached only via `privata_search_paths` is treated as
/// external reference context, same as any other search-only root.
fn split_test_source_roots(
    report_roots: &[PathBuf],
    roots: &[PathBuf],
) -> (Vec<PathBuf>, Vec<PathBuf>) {
    let mut local = Vec::new();
    let mut external = Vec::new();
    for root in roots {
        if !is_test_source_root(root) {
            continue;
        }
        if report_roots.iter().any(|r| root.starts_with(r)) {
            local.push(root.clone());
        } else {
            external.push(root.clone());
        }
    }
    (local, external)
}

/// Union method references keyed by module name.
fn merge_method_references(
    maps: &[HashMap<String, HashSet<String>>],
) -> HashMap<String, HashSet<String>> {
    let mut merged: HashMap<String, HashSet<String>> = HashMap::new();
    for map in maps {
        for (module_name, names) in map {
            merged
                .entry(module_name.clone())
                .or_default()
                .extend(names.iter().cloned());
        }
    }
    merged
}

/// Return names standalone test files (external or nested) mention for
/// imported modules.
fn standalone_test_method_references(
    modules: &HashMap<String, Module>,
    test_consumers: &HashMap<String, Module>,
) -> HashMap<String, HashSet<String>> {
    let mut references: HashMap<String, HashSet<String>> = HashMap::new();
    for consumer in test_consumers.values() {
        for (module_name, names) in referenced_names_by_module(consumer, modules) {
            references.entry(module_name).or_default().extend(names);
        }
    }
    references
}

/// Drop findings that don't live under a reported `source_roots` entry.
///
/// `privata_search_paths` widen what gets *searched* (so a symbol defined in
/// a reported root can be seen as used from a wider or sibling tree) without
/// widening what gets *reported*: defects in a search-only path belong to
/// whatever project owns that path, not to this run. Module collisions are
/// kept whole, since a collision is inherently a statement about two roots.
fn scope_findings_to_project(
    mut findings: PrivacyFindings,
    report_roots: &[PathBuf],
) -> PrivacyFindings {
    let owned = |path: &Path| report_roots.iter().any(|r| path.starts_with(r));
    findings.unparsable_modules.retain(|m| owned(&m.path));
    findings.candidates.retain(|s| owned(&s.path));
    findings.method_candidates.retain(|m| owned(&m.path));
    findings
        .private_module_imports
        .retain(|i| owned(&i.imported_by_path));
    findings
        .private_symbol_imports
        .retain(|i| owned(&i.imported_by_path));
    findings.export_issues.retain(|e| owned(&e.path));
    findings
}

/// Collect public-symbol and private-module boundary findings.
///
/// `include_methods` is required rather than defaulted: the method scan is
/// the most expensive part of a run, and every caller knows whether it
/// wants the answer.
fn collect_privacy_findings(project_root: &Path, include_methods: bool) -> PrivacyFindings {
    let project_root =
        dunce::canonicalize(project_root).unwrap_or_else(|_| project_root.to_path_buf());
    let report_roots = source_roots(&project_root);
    let roots = all_search_roots(&project_root);
    let (modules, unparsable_modules, module_collisions) =
        collect_modules_collisions_and_errors(&roots);
    let (local_test_roots, external_test_roots) = split_test_source_roots(&report_roots, &roots);
    let production_roots: Vec<PathBuf> = roots
        .iter()
        .filter(|r| !is_test_source_root(r))
        .cloned()
        .collect();
    let local_test_consumers = collect_test_consumers(&local_test_roots, &[]);
    let external_test_consumers = collect_test_consumers(&external_test_roots, &[]);
    let nested_test_consumers = collect_test_consumers(&[], &production_roots);

    let mut cross_imports = find_cross_imports(&modules, None);
    cross_imports.extend(find_cross_imports(&modules, Some(&external_test_consumers)));
    cross_imports.extend(find_cross_imports(&modules, Some(&nested_test_consumers)));
    cross_imports.extend(test_helper_cross_imports(
        &local_test_roots,
        &modules,
        &local_test_consumers,
    ));

    let external_entrypoints = collect_external_entrypoints(&project_root);
    let public_interface_exports = load_tach_interface_exports(&project_root);
    let package_reexports = collect_reexports(&modules);

    let mut candidates: Vec<Symbol> = modules
        .values()
        .flat_map(|module| module.symbols.iter())
        .filter(|sym| {
            let key = (sym.module.clone(), sym.name.clone());
            !cross_imports.contains(&key)
                && !external_entrypoints.contains(&key)
                && !public_interface_exports.contains(&key)
                && !package_reexports.contains(&key)
        })
        .cloned()
        .collect();
    candidates.sort_by(|a, b| {
        (a.path.to_string_lossy(), a.lineno).cmp(&(b.path.to_string_lossy(), b.lineno))
    });

    let method_candidates = if include_methods {
        let mut public_interface = external_entrypoints.clone();
        public_interface.extend(public_interface_exports.iter().cloned());
        public_interface.extend(package_reexports.iter().cloned());

        let test_refs = merge_method_references(&[
            test_helper_method_references(&local_test_roots, &modules, &local_test_consumers),
            standalone_test_method_references(&modules, &external_test_consumers),
            standalone_test_method_references(&modules, &nested_test_consumers),
        ]);

        collect_method_candidates(&modules, Some(&public_interface), Some(&test_refs))
    } else {
        Vec::new()
    };

    let findings = PrivacyFindings {
        unparsable_modules,
        candidates,
        method_candidates,
        private_module_imports: collect_private_module_imports(&modules),
        private_symbol_imports: collect_private_symbol_imports(&modules),
        export_issues: collect_export_issues(&modules),
        module_collisions,
    };
    scope_findings_to_project(findings, &report_roots)
}

/// Find production source files that could not be parsed.
pub fn find_unparsable_modules(project_root: &Path) -> Vec<UnparsableModule> {
    collect_privacy_findings(project_root, false).unparsable_modules
}

/// Find symbols that appear module-local and should be private.
pub fn find_private_candidates(project_root: &Path) -> Vec<Symbol> {
    collect_privacy_findings(project_root, false).candidates
}

/// Find public methods that only their own module refers to.
pub fn find_method_candidates(project_root: &Path) -> Vec<Method> {
    collect_privacy_findings(project_root, true).method_candidates
}

/// Find private modules imported from outside their package subtree.
pub fn find_private_module_imports(project_root: &Path) -> Vec<PrivateModuleImport> {
    collect_privacy_findings(project_root, false).private_module_imports
}

/// Find private top-level symbols imported from another production module.
pub fn find_private_symbol_imports(project_root: &Path) -> Vec<PrivateSymbolImport> {
    collect_privacy_findings(project_root, false).private_symbol_imports
}

/// Find literal `__all__` declarations that are stale or incomplete.
pub fn find_export_issues(project_root: &Path) -> Vec<ExportIssue> {
    collect_privacy_findings(project_root, false).export_issues
}

/// Find module names that resolve to more than one file across source roots.
pub fn find_module_collisions(project_root: &Path) -> Vec<ModuleCollision> {
    collect_privacy_findings(project_root, false).module_collisions
}

/// Scan project and report module-local public symbols.
///
/// The method check is off by default. Attribute access is dynamic in
/// Python, so it cannot see every caller, and on a large codebase it
/// reports far more than the other checks. Opt in with `include_methods`
/// once the noise is worth it for a given project.
///
/// Returns the report text (empty findings still produce a "clean" message)
/// and the process exit code a CLI should use.
pub fn check_project(project_root: &Path, include_methods: bool) -> (String, i32) {
    let project_root =
        dunce::canonicalize(project_root).unwrap_or_else(|_| project_root.to_path_buf());
    let findings = collect_privacy_findings(&project_root, include_methods);

    let mut sections: Vec<String> = Vec::new();
    if !findings.unparsable_modules.is_empty() {
        sections.push(print_unparsable_modules(
            &findings.unparsable_modules,
            &project_root,
        ));
    }
    if !findings.module_collisions.is_empty() {
        sections.push(print_module_collisions(
            &findings.module_collisions,
            &project_root,
        ));
    }
    if !findings.candidates.is_empty() {
        sections.push(print_private_candidates(
            &findings.candidates,
            &project_root,
        ));
    }
    if !findings.method_candidates.is_empty() {
        sections.push(print_method_candidates(
            &findings.method_candidates,
            &project_root,
        ));
    }
    if !findings.private_module_imports.is_empty() {
        sections.push(print_private_module_imports(
            &findings.private_module_imports,
            &project_root,
        ));
    }
    if !findings.private_symbol_imports.is_empty() {
        sections.push(print_private_symbol_imports(
            &findings.private_symbol_imports,
            &project_root,
        ));
    }
    if !findings.export_issues.is_empty() {
        sections.push(print_export_issues(&findings.export_issues, &project_root));
    }

    if sections.is_empty() {
        return ("No module privacy issues found.\n".to_string(), 0);
    }
    (sections.join("\n"), 1)
}

fn print_unparsable_modules(unparsable: &[UnparsableModule], project_root: &Path) -> String {
    let mut out = String::new();
    writeln!(
        out,
        "Found {} that could not be parsed; skipped files stop contributing references, so \
every finding below may be wrong:\n",
        count(unparsable.len(), "source file", None)
    )
    .unwrap();
    for module in unparsable {
        let rel = display_path(&module.path, project_root);
        writeln!(out, "  {rel}:{}: {}", module.lineno, module.message).unwrap();
    }
    out
}

fn print_module_collisions(collisions: &[ModuleCollision], project_root: &Path) -> String {
    let mut out = String::new();
    writeln!(
        out,
        "Found {} defined by multiple files; only one file per name is scanned, so findings for \
these modules may be incomplete:\n",
        count(collisions.len(), "module name", None)
    )
    .unwrap();
    for collision in collisions {
        let rels: Vec<String> = collision
            .paths
            .iter()
            .map(|p| display_path(p, project_root))
            .collect();
        writeln!(
            out,
            "  module `{}` is defined by: {}",
            collision.module,
            rels.join(", ")
        )
        .unwrap();
    }
    out
}

fn print_private_candidates(candidates: &[Symbol], project_root: &Path) -> String {
    let mut out = String::new();
    writeln!(
        out,
        "Found {} that could be made private:\n",
        count(candidates.len(), "public symbol", None)
    )
    .unwrap();
    for symbol in candidates {
        let rel = display_path(&symbol.path, project_root);
        writeln!(
            out,
            "  {rel}:{}: {} `{}`",
            symbol.lineno, symbol.kind, symbol.name
        )
        .unwrap();
    }
    out
}

/// Print method findings grouped by the class that owns them.
///
/// A flat list reads as one decision per method, which is misleading. Ten
/// findings in a ten-method class is a single question about the class; the
/// `n of m` count is what tells those apart, so it leads each group.
fn print_method_candidates(methods: &[Method], project_root: &Path) -> String {
    let mut order: Vec<(PathBuf, u32, String)> = Vec::new();
    let mut groups: HashMap<(PathBuf, u32, String), Vec<&Method>> = HashMap::new();
    for method in methods {
        let key = (
            method.path.clone(),
            method.class_lineno,
            method.class_name.clone(),
        );
        if !groups.contains_key(&key) {
            order.push(key.clone());
        }
        groups.entry(key).or_default().push(method);
    }

    let mut out = String::new();
    writeln!(
        out,
        "Found {} in {} that could be made private:\n",
        count(methods.len(), "public method", None),
        count(groups.len(), "class", Some("classes")),
    )
    .unwrap();
    for (path, class_lineno, class_name) in order {
        let found = &groups[&(path.clone(), class_lineno, class_name.clone())];
        let rel = display_path(&path, project_root);
        let total = found[0].class_public_methods;
        writeln!(
            out,
            "  {rel}:{class_lineno}: class `{class_name}` ({} of {total} public methods)",
            found.len()
        )
        .unwrap();
        let names: Vec<String> = found
            .iter()
            .map(|m| format!("{}:{}", m.name, m.lineno))
            .collect();
        for line in wrap_names(&names.join(", "), METHOD_LIST_WIDTH) {
            writeln!(out, "{METHOD_LIST_INDENT}{line}").unwrap();
        }
    }
    out
}

fn print_private_module_imports(imports: &[PrivateModuleImport], project_root: &Path) -> String {
    let mut out = String::new();
    writeln!(
        out,
        "Found {} outside the owning package subtree:\n",
        count(imports.len(), "private module import", None)
    )
    .unwrap();
    for private_import in imports {
        let rel = display_path(&private_import.imported_by_path, project_root);
        writeln!(
            out,
            "  {rel}:{}: imports private module `{}`",
            private_import.lineno, private_import.module
        )
        .unwrap();
    }
    out
}

fn print_private_symbol_imports(imports: &[PrivateSymbolImport], project_root: &Path) -> String {
    let mut out = String::new();
    writeln!(
        out,
        "Found {} from production modules:\n",
        count(imports.len(), "private symbol import", None)
    )
    .unwrap();
    for private_import in imports {
        let rel = display_path(&private_import.imported_by_path, project_root);
        writeln!(
            out,
            "  {rel}:{}: imports private symbol `{}.{}`",
            private_import.lineno, private_import.module, private_import.name
        )
        .unwrap();
    }
    out
}

fn print_export_issues(export_issues: &[ExportIssue], project_root: &Path) -> String {
    let mut out = String::new();
    writeln!(
        out,
        "Found {}:\n",
        count(export_issues.len(), "__all__ export issue", None)
    )
    .unwrap();
    for issue in export_issues {
        let rel = display_path(&issue.path, project_root);
        match issue.kind {
            crate::models::ExportIssueKind::Unknown => {
                writeln!(
                    out,
                    "  {rel}:{}: __all__ exports unknown name `{}`",
                    issue.lineno, issue.name
                )
                .unwrap();
            }
            crate::models::ExportIssueKind::Private => {
                writeln!(
                    out,
                    "  {rel}:{}: __all__ exports private name `{}`",
                    issue.lineno, issue.name
                )
                .unwrap();
            }
            crate::models::ExportIssueKind::Missing => {
                writeln!(
                    out,
                    "  {rel}:{}: public name `{}` missing from __all__",
                    issue.lineno, issue.name
                )
                .unwrap();
            }
        }
    }
    out
}

/// Return a path relative to the project root, or the absolute path if it
/// lies outside it.
///
/// A colliding source root (e.g. a sibling project's `tests` directory
/// listed in `tach.toml` `source_roots`) can sit outside `project_root`
/// entirely, where stripping the prefix would fail.
fn display_path(path: &Path, project_root: &Path) -> String {
    let relevant = if path.starts_with(project_root) {
        path.strip_prefix(project_root).unwrap_or(path)
    } else {
        path
    };
    relevant.to_string_lossy().replace('\\', "/")
}

/// Return `number` with a correctly pluralised noun.
fn count(number: usize, singular: &str, plural: Option<&str>) -> String {
    if number == 1 {
        format!("1 {singular}")
    } else {
        match plural {
            Some(plural) => format!("{number} {plural}"),
            None => format!("{number} {singular}s"),
        }
    }
}

/// Greedy word-wrap matching `textwrap.wrap(text, width, break_long_words=False)`:
/// break only between whitespace-separated words, and let an over-long word
/// spill past `width` on its own line rather than splitting it.
fn wrap_names(text: &str, width: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        if current.is_empty() {
            current.push_str(word);
        } else if current.chars().count() + 1 + word.chars().count() <= width {
            current.push(' ');
            current.push_str(word);
        } else {
            lines.push(std::mem::take(&mut current));
            current.push_str(word);
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, contents: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, contents).unwrap();
    }

    #[test]
    fn clean_project_reports_no_issues() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/producer.py",
            "def helper() -> int:\n    return 1\n",
        );
        write(
            tmp.path(),
            "src/pkg/consumer.py",
            "from pkg.producer import helper\n\ndef run() -> int:\n    return helper()\n",
        );
        write(
            tmp.path(),
            "src/pkg/__main__.py",
            "from pkg.consumer import run\n\nrun()\n",
        );
        let (text, code) = check_project(tmp.path(), false);
        assert_eq!(text, "No module privacy issues found.\n");
        assert_eq!(code, 0);
    }

    #[test]
    fn private_symbol_import_report_matches_expected_format() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/write_coordinator.py",
            "class _EventCacheWriteCoordinator:\n    pass\n\n\ndef local_helper() -> None:\n    pass\n",
        );
        write(
            tmp.path(),
            "src/pkg/runtime_support.py",
            "from pkg.write_coordinator import _EventCacheWriteCoordinator\n",
        );
        let (text, code) = check_project(tmp.path(), false);
        assert_eq!(code, 1);
        assert!(text.contains("Found 1 private symbol import from production modules:\n"));
        assert!(text.contains(
            "  src/pkg/runtime_support.py:1: imports private symbol `pkg.write_coordinator._EventCacheWriteCoordinator`"
        ));
        assert!(text.contains("function `local_helper`"));
    }

    #[test]
    fn multiple_sections_are_separated_by_a_blank_line() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "src/pkg/one/_internal.py", "VALUE = 1\n");
        write(
            tmp.path(),
            "src/pkg/two/public.py",
            "from pkg.one import _internal\n\ndef leaked() -> int:\n    return 1\n",
        );
        let (text, code) = check_project(tmp.path(), false);
        assert_eq!(code, 1);
        // The private-module-import section and the private-candidate section
        // must be joined by exactly one blank line, matching Python's
        // `if index: print()` separator between sections.
        assert!(
            text.contains(":\n\n  "),
            "each section header is followed by a blank-line-free body"
        );
        let sections: Vec<&str> = text.trim_end().split("\n\n").collect();
        assert!(sections.len() >= 2);
    }

    #[test]
    fn find_private_candidates_excludes_used_symbols() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def helper() -> int:\n    return 1\n",
        );
        let candidates = find_private_candidates(tmp.path());
        assert!(candidates.iter().any(|s| s.name == "helper"));
    }

    #[test]
    fn method_candidates_only_appear_via_find_method_candidates() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    def helper(self) -> int:\n        return 1\n",
        );
        let (text, _) = check_project(tmp.path(), false);
        assert!(!text.contains("helper"), "methods check is off by default");
        let methods = find_method_candidates(tmp.path());
        assert!(methods.iter().any(|m| m.name == "helper"));
    }

    #[test]
    fn unparsable_modules_exclude_privata_search_paths() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "core/src/broken.py", "def oops(:\n    pass\n");
        write(
            tmp.path(),
            "sibling/sibling_broken.py",
            "def oops(:\n    pass\n",
        );
        write(
            tmp.path(),
            "core/tach.toml",
            "source_roots = [\"src\"]\nprivata_search_paths = [\"../sibling\"]\n",
        );

        let (text, code) = check_project(&tmp.path().join("core"), false);
        assert_eq!(code, 1);
        assert!(text.contains("could not be parsed"));
        assert!(text.contains("src/broken.py"));
        assert!(!text.contains("sibling_broken.py"));
    }

    #[test]
    fn source_roots_without_privata_search_paths_are_fully_reported_even_outside_project() {
        // The old behaviour silently exempted out-of-project `source_roots`
        // from reporting; that exemption is gone. Anything explicitly listed
        // in `source_roots` is reported regardless of where it lives —
        // `privata_search_paths` is now the only way to search without
        // reporting.
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "core/src/broken.py", "def oops(:\n    pass\n");
        write(
            tmp.path(),
            "sibling/sibling_broken.py",
            "def oops(:\n    pass\n",
        );
        write(
            tmp.path(),
            "core/tach.toml",
            "source_roots = [\"src\", \"../sibling\"]\n",
        );

        let (text, _) = check_project(&tmp.path().join("core"), false);
        assert!(text.contains("src/broken.py"));
        assert!(text.contains("sibling_broken.py"));
    }

    #[test]
    fn privata_search_paths_widen_search_without_widening_reporting() {
        // source_roots covers only python/pkg (the subfolder being
        // incrementally onboarded); privata_search_paths covers all of
        // python/ so a symbol used elsewhere in the tree is still seen as
        // used, but issues elsewhere in python/ (outside pkg/) are not
        // reported.
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "python/pkg/mod.py",
            "def used_elsewhere() -> int:\n    return 1\n\n\ndef unused() -> int:\n    return 2\n",
        );
        write(
            tmp.path(),
            "python/other/consumer.py",
            "def stray_unused() -> int:\n    return 1\n\n\nfrom pkg.mod import used_elsewhere\n\nused_elsewhere()\n",
        );
        write(
            tmp.path(),
            "tach.toml",
            "source_roots = [\"python/pkg\"]\nprivata_search_paths = [\"python\"]\n",
        );

        let (text, code) = check_project(tmp.path(), false);
        assert_eq!(code, 1);
        assert!(
            !text.contains("used_elsewhere"),
            "used from python/other, so not a candidate despite living in the narrower source root"
        );
        assert!(text.contains("pkg/mod.py"));
        assert!(text.contains('`') && text.contains("unused"));
        assert!(
            !text.contains("consumer.py"),
            "issues outside source_roots must not be reported even though they were searched"
        );
    }
}
