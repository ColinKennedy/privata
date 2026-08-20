//! Candidate eligibility and production-reference analysis.

use std::collections::{HashMap, HashSet};

use ruff_python_ast::visitor::{walk_expr, walk_pattern, walk_stmt, Visitor};
use ruff_python_ast::{Expr, ExprContext, Pattern, Stmt, StmtClassDef, StmtFunctionDef};
use ruff_source_file::LineIndex;

use crate::ast_utils::{dotted_name, lineno_at, NAMESPACE_SEPARATOR};
use crate::models::{Method, Module};

// Decorators that leave a method free to be renamed. Canonical names avoid
// treating unrelated decorators with a trusted basename as rename-safe.
const SAFE_METHOD_DECORATORS: &[&str] = &[
    "builtins.classmethod",
    "builtins.property",
    "builtins.staticmethod",
    "functools.cache",
    "functools.cached_property",
    "functools.lru_cache",
    "typing.final",
    "typing_extensions.final",
];
const SAFE_CLASS_DECORATORS: &[&str] = &[
    "dataclasses.dataclass",
    "typing.final",
    "typing_extensions.final",
];
const LOCAL_DECORATOR_PREFIX: &str = "<local>";
// Builtins that reach an attribute by name, so a computed name hides the target.
const DYNAMIC_LOOKUPS: &[&str] = &["delattr", "getattr", "hasattr", "setattr"];

fn builtin_aliases() -> HashMap<String, String> {
    [
        ("classmethod", "builtins.classmethod"),
        ("object", "builtins.object"),
        ("property", "builtins.property"),
        ("staticmethod", "builtins.staticmethod"),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect()
}

fn safe_decorator_roots() -> HashSet<&'static str> {
    SAFE_METHOD_DECORATORS
        .iter()
        .chain(SAFE_CLASS_DECORATORS.iter())
        .map(|name| name.split(NAMESPACE_SEPARATOR).next().unwrap_or(name))
        .collect()
}

/// Return public methods that only their own module refers to.
///
/// A method counts as used when another production module mentions its name,
/// either as an attribute access or as a string literal (for `getattr` style
/// lookups). `test_references` supplies extra names for helper modules that
/// live in a test source root, mirroring the test-helper rule for symbols.
pub fn collect_method_candidates(
    modules: &HashMap<String, Module>,
    public_interface: Option<&HashSet<(String, String)>>,
    test_references: Option<&HashMap<String, HashSet<String>>>,
) -> Vec<Method> {
    let empty_interface = HashSet::new();
    let interface = public_interface.unwrap_or(&empty_interface);
    let empty_refs = HashMap::new();
    let extra_references = test_references.unwrap_or(&empty_refs);
    let references = references_by_module(modules);
    let base_names = base_class_names(modules);

    let mut candidates = Vec::new();
    for module in modules.values() {
        let Some(tree) = &module.tree else { continue };
        let Some(line_index) = &module.line_index else {
            continue;
        };
        for node in &tree.body {
            let Stmt::ClassDef(class_node) = node else {
                continue;
            };
            let class_lineno = lineno_at(line_index, class_node.name.range.start());
            let decorator_aliases = decorator_aliases_before(tree, class_lineno, line_index);
            if !is_checkable_class(
                node,
                class_node,
                module,
                interface,
                &decorator_aliases,
                &base_names,
            ) {
                continue;
            }
            let empty = HashSet::new();
            let module_test_references = extra_references.get(&module.name).unwrap_or(&empty);
            candidates.extend(class_method_candidates(
                module,
                line_index,
                class_node,
                class_lineno,
                &references,
                module_test_references,
                &decorator_aliases,
            ));
        }
    }

    candidates.sort_by(|a, b| {
        (a.path.to_string_lossy(), a.lineno).cmp(&(b.path.to_string_lossy(), b.lineno))
    });
    candidates
}

fn references_by_module(modules: &HashMap<String, Module>) -> HashMap<String, HashSet<String>> {
    let mut references: HashMap<String, HashSet<String>> = HashMap::new();
    for (module_name, module) in modules {
        let Some(tree) = &module.tree else { continue };
        for name in crate::ast_utils::referenced_names(&tree.body) {
            references
                .entry(name)
                .or_default()
                .insert(module_name.clone());
        }
    }
    references
}

/// Return every name that any class in the project uses as a base.
///
/// A subclass that only overrides a method never mentions the name as an
/// attribute, so the reference scan cannot see it. Renaming the base method
/// would leave the override stranded under its old name, so a class that
/// anything subclasses keeps its methods public.
fn base_class_names(modules: &HashMap<String, Module>) -> HashSet<String> {
    struct BaseCollector {
        names: HashSet<String>,
    }
    impl<'a> Visitor<'a> for BaseCollector {
        fn visit_stmt(&mut self, stmt: &'a Stmt) {
            if let Stmt::ClassDef(class_node) = stmt {
                if let Some(arguments) = &class_node.arguments {
                    self.names.extend(referenced_base_names(&arguments.args));
                }
            }
            walk_stmt(self, stmt);
        }
    }
    let mut collector = BaseCollector {
        names: HashSet::new(),
    };
    for module in modules.values() {
        if let Some(tree) = &module.tree {
            for stmt in &tree.body {
                collector.visit_stmt(stmt);
            }
        }
    }
    collector.names
}

/// Return the trailing names of every base expression.
///
/// Bases are matched by trailing name, so `Base`, `mod.Base` and the
/// subscripted `Base[int]` all protect a class named `Base`. Matching is
/// deliberately loose: over-matching only suppresses reports, which is the
/// safe direction.
fn referenced_base_names(bases: &[Expr]) -> HashSet<String> {
    struct NameCollector {
        names: HashSet<String>,
    }
    impl<'a> Visitor<'a> for NameCollector {
        fn visit_expr(&mut self, expr: &'a Expr) {
            match expr {
                Expr::Name(name) => {
                    self.names.insert(name.id.to_string());
                }
                Expr::Attribute(attr) => {
                    self.names.insert(attr.attr.id.to_string());
                }
                _ => {}
            }
            walk_expr(self, expr);
        }
    }
    let mut collector = NameCollector {
        names: HashSet::new(),
    };
    for base in bases {
        collector.visit_expr(base);
    }
    collector.names
}

#[allow(clippy::too_many_arguments)]
fn class_method_candidates(
    module: &Module,
    line_index: &LineIndex,
    class_node: &StmtClassDef,
    class_lineno: u32,
    references: &HashMap<String, HashSet<String>>,
    test_references: &HashSet<String>,
    decorator_aliases: &HashMap<String, String>,
) -> Vec<Method> {
    let mut aliases = decorator_aliases.clone();
    let protected_methods = protected_method_nodes(&class_node.body);
    let public_methods = class_node
        .body
        .iter()
        .filter(|node| matches!(node, Stmt::FunctionDef(f) if !f.name.id.starts_with('_')))
        .count() as u32;

    let mut out = Vec::new();
    for (index, node) in class_node.body.iter().enumerate() {
        let Stmt::FunctionDef(f) = node else {
            update_aliases(&mut aliases, node);
            continue;
        };
        let checkable = is_checkable_method(node, f, &aliases);
        aliases.insert(
            f.name.id.to_string(),
            format!("{LOCAL_DECORATOR_PREFIX}{NAMESPACE_SEPARATOR}{}", f.name.id),
        );
        if !checkable {
            continue;
        }
        if protected_methods.contains(&index) {
            continue;
        }
        let lineno = lineno_at(line_index, f.name.range.start());
        if module.ignored_lines.contains(&lineno) {
            continue;
        }
        if test_references.contains(f.name.id.as_str()) {
            continue;
        }
        let mut referencing = references
            .get(f.name.id.as_str())
            .cloned()
            .unwrap_or_default();
        referencing.remove(&module.name);
        if !referencing.is_empty() {
            continue;
        }
        out.push(Method {
            name: f.name.id.to_string(),
            class_name: class_node.name.id.to_string(),
            lineno,
            module: module.name.clone(),
            path: module.path.clone(),
            class_lineno,
            class_public_methods: public_methods,
        });
    }
    out
}

/// Return methods whose class binding is consumed or replaced later.
fn protected_method_nodes(class_body: &[Stmt]) -> HashSet<usize> {
    let mut current_methods: HashMap<String, usize> = HashMap::new();
    let mut protected: HashSet<usize> = HashSet::new();

    for (index, node) in class_body.iter().enumerate() {
        if let Stmt::FunctionDef(f) = node {
            let mut header: Vec<&Expr> = f.decorator_list.iter().map(|d| &d.expression).collect();
            let params = &f.parameters;
            for param in params.posonlyargs.iter().chain(params.args.iter()) {
                if let Some(default) = &param.default {
                    header.push(default);
                }
            }
            for param in params.kwonlyargs.iter() {
                if let Some(default) = &param.default {
                    header.push(default);
                }
            }
            for param in params
                .posonlyargs
                .iter()
                .chain(params.args.iter())
                .chain(params.kwonlyargs.iter())
            {
                if let Some(annotation) = &param.parameter.annotation {
                    header.push(annotation);
                }
            }
            if let Some(vararg) = &params.vararg {
                if let Some(annotation) = &vararg.annotation {
                    header.push(annotation);
                }
            }
            if let Some(kwarg) = &params.kwarg {
                if let Some(annotation) = &kwarg.annotation {
                    header.push(annotation);
                }
            }
            if let Some(returns) = &f.returns {
                header.push(returns);
            }

            for expr in header {
                for name in loaded_names_in_expr(expr) {
                    if let Some(&prev_idx) = current_methods.get(&name) {
                        protected.insert(prev_idx);
                    }
                }
            }
            if let Some(&prev_idx) = current_methods.get(f.name.id.as_str()) {
                protected.insert(prev_idx);
            }
            current_methods.insert(f.name.id.to_string(), index);
            continue;
        }

        for name in loaded_names_in_stmt(node) {
            if let Some(&prev_idx) = current_methods.get(&name) {
                protected.insert(prev_idx);
            }
        }
        for name in bound_names_in_stmt(node) {
            let key = name.split(NAMESPACE_SEPARATOR).next().unwrap_or(&name);
            if let Some(prev_idx) = current_methods.remove(key) {
                protected.insert(prev_idx);
            }
        }
    }
    protected
}

/// Return whether a class owns its method names outright.
///
/// Only plain, public, non-exported, non-subclassed classes qualify. A base
/// class Privata cannot see may require a method to keep its public name, an
/// exported class exposes its methods as part of the package interface, a
/// class with subclasses owes its method names to those overrides, and a
/// class that looks attributes up by a computed name may reach any of its
/// own methods without ever spelling them out.
fn is_checkable_class(
    stmt: &Stmt,
    node: &StmtClassDef,
    module: &Module,
    public_interface: &HashSet<(String, String)>,
    decorator_aliases: &HashMap<String, String>,
    base_class_names: &HashSet<String>,
) -> bool {
    let name = node.name.id.as_str();
    if name.starts_with('_')
        || module.exports.contains(name)
        || public_interface.contains(&(module.name.clone(), name.to_string()))
        || base_class_names.contains(name)
    {
        return false;
    }

    let has_keywords = node
        .arguments
        .as_ref()
        .is_some_and(|a| !a.keywords.is_empty());
    let has_non_object_base = node.arguments.as_ref().is_some_and(|a| {
        a.args.iter().any(|base| {
            resolved_name(base, decorator_aliases).as_deref() != Some("builtins.object")
        })
    });
    if has_keywords || has_non_object_base {
        return false;
    }
    if looks_up_attributes_dynamically(stmt) {
        return false;
    }
    has_only_safe_decorators(
        &node.decorator_list,
        SAFE_CLASS_DECORATORS,
        decorator_aliases,
    )
}

/// Return whether a class reaches attributes by a name it computes.
///
/// `getattr(self, "visit_" + kind)` names a method that appears nowhere as a
/// literal, so the reference scan cannot see the call. A class that
/// dispatches this way may reach any of its own methods, and renaming one
/// would break a call the scan never knew about, so none of its methods are
/// checked.
///
/// Only a non-literal second argument counts. `getattr(self, "run")` spells
/// the name out and is already picked up as a string reference.
fn looks_up_attributes_dynamically(stmt: &Stmt) -> bool {
    struct DynamicLookup {
        found: bool,
    }
    impl<'a> Visitor<'a> for DynamicLookup {
        fn visit_expr(&mut self, expr: &'a Expr) {
            if let Expr::Call(call) = expr {
                if let Expr::Name(name) = &*call.func {
                    if DYNAMIC_LOOKUPS.contains(&name.id.as_str())
                        && call.arguments.args.len() >= 2
                        && !is_string_literal(&call.arguments.args[1])
                    {
                        self.found = true;
                    }
                }
            }
            walk_expr(self, expr);
        }
    }
    let mut visitor = DynamicLookup { found: false };
    visitor.visit_stmt(stmt);
    visitor.found
}

fn is_string_literal(expr: &Expr) -> bool {
    matches!(expr, Expr::StringLiteral(_))
}

fn is_checkable_method(
    stmt: &Stmt,
    node: &StmtFunctionDef,
    decorator_aliases: &HashMap<String, String>,
) -> bool {
    if node.name.id.starts_with('_') {
        return false;
    }
    if forwards_to_same_named_super_method(stmt, node.name.id.as_str()) {
        return false;
    }
    has_only_safe_decorators(
        &node.decorator_list,
        SAFE_METHOD_DECORATORS,
        decorator_aliases,
    )
}

/// Return whether a method participates in a cooperative super call.
fn forwards_to_same_named_super_method(stmt: &Stmt, method_name: &str) -> bool {
    struct SuperForward<'a> {
        method_name: &'a str,
        found: bool,
    }
    impl<'a, 'b> Visitor<'b> for SuperForward<'a> {
        fn visit_expr(&mut self, expr: &'b Expr) {
            if let Expr::Attribute(attr) = expr {
                if attr.attr.id.as_str() == self.method_name {
                    if let Expr::Call(call) = &*attr.value {
                        if let Expr::Name(name) = &*call.func {
                            if name.id.as_str() == "super" {
                                self.found = true;
                            }
                        }
                    }
                }
            }
            walk_expr(self, expr);
        }
    }
    let mut visitor = SuperForward {
        method_name,
        found: false,
    };
    visitor.visit_stmt(stmt);
    visitor.found
}

fn has_only_safe_decorators(
    decorators: &[ruff_python_ast::Decorator],
    safe_names: &[&str],
    aliases: &HashMap<String, String>,
) -> bool {
    decorators.iter().all(|decorator| {
        decorator_name(&decorator.expression, aliases)
            .is_some_and(|name| safe_names.contains(&name.as_str()))
    })
}

fn decorator_name(decorator: &Expr, aliases: &HashMap<String, String>) -> Option<String> {
    let target = match decorator {
        Expr::Call(call) => &*call.func,
        other => other,
    };
    resolved_name(target, aliases)
}

fn resolved_name(expr: &Expr, aliases: &HashMap<String, String>) -> Option<String> {
    let dotted = dotted_name(expr)?;
    let parts: Vec<&str> = dotted.split(NAMESPACE_SEPARATOR).collect();
    for index in (1..=parts.len()).rev() {
        let prefix = parts[..index].join(NAMESPACE_SEPARATOR);
        if let Some(resolved) = aliases.get(&prefix) {
            let suffix = parts[index..].join(NAMESPACE_SEPARATOR);
            return Some(if suffix.is_empty() {
                resolved.clone()
            } else {
                format!("{resolved}{NAMESPACE_SEPARATOR}{suffix}")
            });
        }
    }
    Some(dotted)
}

/// Return canonical module-level names used by decorator expressions.
fn decorator_aliases_before(
    tree: &ruff_python_ast::ModModule,
    before_lineno: u32,
    line_index: &LineIndex,
) -> HashMap<String, String> {
    let mut aliases = builtin_aliases();
    for node in &tree.body {
        if lineno_at(line_index, stmt_start(node)) >= before_lineno {
            break;
        }
        update_aliases(&mut aliases, node);
    }
    aliases
}

fn stmt_start(stmt: &Stmt) -> ruff_text_size::TextSize {
    match stmt {
        Stmt::FunctionDef(s) => s.range.start(),
        Stmt::ClassDef(s) => s.range.start(),
        Stmt::Return(s) => s.range.start(),
        Stmt::Delete(s) => s.range.start(),
        Stmt::Assign(s) => s.range.start(),
        Stmt::AugAssign(s) => s.range.start(),
        Stmt::AnnAssign(s) => s.range.start(),
        Stmt::TypeAlias(s) => s.range.start(),
        Stmt::For(s) => s.range.start(),
        Stmt::While(s) => s.range.start(),
        Stmt::If(s) => s.range.start(),
        Stmt::With(s) => s.range.start(),
        Stmt::Match(s) => s.range.start(),
        Stmt::Raise(s) => s.range.start(),
        Stmt::Try(s) => s.range.start(),
        Stmt::Assert(s) => s.range.start(),
        Stmt::Import(s) => s.range.start(),
        Stmt::ImportFrom(s) => s.range.start(),
        Stmt::Global(s) => s.range.start(),
        Stmt::Nonlocal(s) => s.range.start(),
        Stmt::Expr(s) => s.range.start(),
        Stmt::Pass(s) => s.range.start(),
        Stmt::Break(s) => s.range.start(),
        Stmt::Continue(s) => s.range.start(),
        Stmt::IpyEscapeCommand(s) => s.range.start(),
    }
}

fn update_aliases(aliases: &mut HashMap<String, String>, node: &Stmt) {
    match node {
        Stmt::Import(import) => {
            for alias in &import.names {
                let local = alias
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
                    });
                let imported = if alias.asname.is_some() {
                    alias.name.id.to_string()
                } else {
                    local.clone()
                };
                aliases.insert(local, imported);
            }
        }
        Stmt::ImportFrom(import) => {
            let level = ".".repeat(import.level as usize);
            let module_name = import.module.as_ref().map(|m| m.id.as_str()).unwrap_or("");
            let source = format!("{level}{module_name}");
            for alias in &import.names {
                if alias.name.id.as_str() == "*" {
                    let roots = safe_decorator_roots();
                    let existing: Vec<String> = aliases.keys().cloned().collect();
                    for name in existing
                        .into_iter()
                        .chain(roots.iter().map(|r| r.to_string()))
                    {
                        aliases.insert(
                            name.clone(),
                            format!("{LOCAL_DECORATOR_PREFIX}{NAMESPACE_SEPARATOR}{name}"),
                        );
                    }
                } else {
                    let local = alias
                        .asname
                        .as_ref()
                        .map(|a| a.id.to_string())
                        .unwrap_or_else(|| alias.name.id.to_string());
                    aliases.insert(
                        local,
                        format!("{source}{NAMESPACE_SEPARATOR}{}", alias.name.id),
                    );
                }
            }
        }
        _ => {
            for name in bound_names_in_stmt(node) {
                aliases.insert(
                    name.clone(),
                    format!("{LOCAL_DECORATOR_PREFIX}{NAMESPACE_SEPARATOR}{name}"),
                );
            }
        }
    }
}

struct BoundNames {
    names: HashSet<String>,
}

impl<'a> Visitor<'a> for BoundNames {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        match stmt {
            Stmt::FunctionDef(f) => {
                self.names.insert(f.name.id.to_string());
            }
            Stmt::ClassDef(c) => {
                self.names.insert(c.name.id.to_string());
            }
            Stmt::Import(import) => {
                for alias in &import.names {
                    let local = alias
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
                        });
                    self.names.insert(local);
                }
            }
            Stmt::ImportFrom(import) => {
                for alias in &import.names {
                    if alias.name.id.as_str() != "*" {
                        self.names.insert(
                            alias
                                .asname
                                .as_ref()
                                .map(|a| a.id.to_string())
                                .unwrap_or_else(|| alias.name.id.to_string()),
                        );
                    }
                }
            }
            _ => walk_stmt(self, stmt),
        }
    }

    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::Name(name) => {
                if matches!(name.ctx, ExprContext::Store | ExprContext::Del) {
                    self.names.insert(name.id.to_string());
                }
            }
            Expr::Attribute(attr) => {
                if matches!(attr.ctx, ExprContext::Store | ExprContext::Del) {
                    if let Some(dotted) = dotted_name(expr) {
                        self.names.insert(dotted);
                    }
                } else {
                    walk_expr(self, expr);
                }
            }
            Expr::Lambda(_) => {}
            _ => walk_expr(self, expr),
        }
    }

    fn visit_except_handler(&mut self, handler: &'a ruff_python_ast::ExceptHandler) {
        let ruff_python_ast::ExceptHandler::ExceptHandler(h) = handler;
        if let Some(name) = &h.name {
            self.names.insert(name.id.to_string());
        }
        for stmt in &h.body {
            self.visit_stmt(stmt);
        }
    }

    fn visit_pattern(&mut self, pattern: &'a Pattern) {
        match pattern {
            Pattern::MatchAs(m) => {
                if let Some(name) = &m.name {
                    self.names.insert(name.id.to_string());
                }
            }
            Pattern::MatchStar(m) => {
                if let Some(name) = &m.name {
                    self.names.insert(name.id.to_string());
                }
            }
            Pattern::MatchMapping(m) => {
                if let Some(rest) = &m.rest {
                    self.names.insert(rest.id.to_string());
                }
            }
            _ => {}
        }
        walk_pattern(self, pattern);
    }
}

fn bound_names_in_stmt(node: &Stmt) -> HashSet<String> {
    let mut collector = BoundNames {
        names: HashSet::new(),
    };
    collector.visit_stmt(node);
    collector.names
}

struct LoadedNames {
    names: HashSet<String>,
}

impl<'a> Visitor<'a> for LoadedNames {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        match stmt {
            Stmt::FunctionDef(_) | Stmt::ClassDef(_) => {}
            _ => walk_stmt(self, stmt),
        }
    }

    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::Name(name) => {
                if matches!(name.ctx, ExprContext::Load) {
                    self.names.insert(name.id.to_string());
                }
            }
            Expr::Lambda(_) => {}
            _ => walk_expr(self, expr),
        }
    }
}

fn loaded_names_in_stmt(node: &Stmt) -> HashSet<String> {
    let mut collector = LoadedNames {
        names: HashSet::new(),
    };
    collector.visit_stmt(node);
    collector.names
}

fn loaded_names_in_expr(node: &Expr) -> HashSet<String> {
    let mut collector = LoadedNames {
        names: HashSet::new(),
    };
    collector.visit_expr(node);
    collector.names
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

    fn method_names(modules: &HashMap<String, Module>) -> HashSet<(String, String)> {
        collect_method_candidates(modules, None, None)
            .into_iter()
            .map(|m| (m.class_name, m.name))
            .collect()
    }

    #[test]
    fn unused_public_method_on_a_plain_class_is_flagged() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    def helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn method_referenced_by_name_in_another_module_is_not_flagged() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/service.py",
            "class Service:\n    def helper(self) -> int:\n        return 1\n",
        );
        write(
            tmp.path(),
            "src/pkg/consumer.py",
            "from pkg.service import Service\n\ndef run(svc: Service) -> int:\n    return svc.helper()\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(!method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn class_with_non_object_base_is_not_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Base:\n    pass\n\n\nclass Service(Base):\n    def helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let names = method_names(&modules);
        assert!(!names.contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn class_that_is_subclassed_elsewhere_is_not_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    def helper(self) -> int:\n        return 1\n\n\nclass Extended(Service):\n    pass\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(!method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn dataclass_decorated_class_is_still_checkable() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "from dataclasses import dataclass\n\n@dataclass\nclass Service:\n    def helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn class_with_unrecognised_decorator_is_not_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def register(cls):\n    return cls\n\n\n@register\nclass Service:\n    def helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(!method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn property_decorated_method_is_still_checkable() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    @property\n    def helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn method_with_unrecognised_decorator_is_not_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "def register(fn):\n    return fn\n\n\nclass Service:\n    @register\n    def helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(!method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn method_that_forwards_to_super_is_not_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    def helper(self) -> int:\n        return super().helper()\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(!method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn class_with_dynamic_getattr_is_not_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    def helper(self) -> int:\n        return 1\n\n    def dispatch(self, kind: str) -> object:\n        return getattr(self, \"visit_\" + kind)\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(!method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn class_with_getattr_literal_name_is_still_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    def helper(self) -> int:\n        return 1\n\n    def dispatch(self) -> object:\n        return getattr(self, \"helper\")\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        // `getattr(self, "helper")` spells the name out (unlike a computed name), so the
        // class stays checkable at all — but the reference is still same-module use, which
        // never counts, so `helper` is still flagged as an unused public method.
        assert!(method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }

    #[test]
    fn dunder_and_private_methods_are_never_flagged() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "class Service:\n    def __init__(self) -> None:\n        pass\n\n    def _helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        let names = method_names(&modules);
        assert!(!names.contains(&("Service".to_string(), "__init__".to_string())));
        assert!(!names.contains(&("Service".to_string(), "_helper".to_string())));
    }

    #[test]
    fn exported_class_is_not_checked() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "src/pkg/mod.py",
            "__all__ = [\"Service\"]\n\n\nclass Service:\n    def helper(self) -> int:\n        return 1\n",
        );
        let modules = collect_modules(&source_roots(tmp.path()));
        assert!(!method_names(&modules).contains(&("Service".to_string(), "helper".to_string())));
    }
}
