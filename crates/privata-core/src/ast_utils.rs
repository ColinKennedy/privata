//! Shared AST helpers used across the privacy-analysis modules.

use std::collections::HashSet;

use ruff_python_ast::Expr;
use ruff_source_file::LineIndex;
use ruff_text_size::TextSize;

pub use crate::models::NAMESPACE_SEPARATOR;

/// Return the flat list of names bound by an assignment target.
///
/// A plain name binds itself; a tuple or list target (destructuring) binds
/// every name inside it, recursively. Anything else (an attribute, a
/// subscript) binds no top-level name Privata tracks.
pub fn names_from_target(expr: &Expr) -> Vec<String> {
    match expr {
        Expr::Name(name) => vec![name.id.to_string()],
        Expr::Tuple(tuple) => tuple.elts.iter().flat_map(names_from_target).collect(),
        Expr::List(list) => list.elts.iter().flat_map(names_from_target).collect(),
        _ => Vec::new(),
    }
}

/// Return the string set of a literal list/tuple/set expression, or `None`
/// when any element is not a string literal.
pub fn string_literal_set(expr: &Expr) -> Option<HashSet<String>> {
    let elts: &[Expr] = match expr {
        Expr::List(list) => &list.elts,
        Expr::Tuple(tuple) => &tuple.elts,
        Expr::Set(set) => &set.elts,
        _ => return None,
    };
    let mut names = HashSet::new();
    for elt in elts {
        match elt {
            Expr::StringLiteral(s) => {
                names.insert(s.value.to_str().to_string());
            }
            _ => return None,
        }
    }
    Some(names)
}

/// Resolve a chained attribute expression to a dotted string, or `None`.
pub fn dotted_name(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Name(name) => Some(name.id.to_string()),
        Expr::Attribute(attr) => {
            let parent = dotted_name(&attr.value)?;
            Some(format!("{parent}{NAMESPACE_SEPARATOR}{}", attr.attr.id))
        }
        _ => None,
    }
}

/// Convert a byte offset to a 1-indexed line number.
pub fn lineno_at(index: &LineIndex, offset: TextSize) -> u32 {
    u32::try_from(index.line_index(offset).get()).unwrap_or(u32::MAX)
}

/// Return every attribute name and string literal a subtree mentions.
///
/// String literals count because a name reached through
/// `getattr(obj, "run")` is as real a use as `obj.run`.
pub fn referenced_names(body: &[ruff_python_ast::Stmt]) -> HashSet<String> {
    use ruff_python_ast::visitor::{walk_body, Visitor};

    struct Collector {
        names: HashSet<String>,
    }
    impl<'a> Visitor<'a> for Collector {
        fn visit_expr(&mut self, expr: &'a Expr) {
            match expr {
                Expr::Attribute(attr) => {
                    self.names.insert(attr.attr.id.to_string());
                }
                Expr::StringLiteral(s) => {
                    self.names.insert(s.value.to_str().to_string());
                }
                _ => {}
            }
            ruff_python_ast::visitor::walk_expr(self, expr);
        }
    }

    let mut collector = Collector {
        names: HashSet::new(),
    };
    walk_body(&mut collector, body);
    collector.names
}

#[cfg(test)]
mod tests {
    use super::*;
    use ruff_python_parser::parse_module;

    #[test]
    fn dotted_name_resolves_chained_attributes() {
        let parsed = parse_module("a.b.c\n").unwrap();
        let module = parsed.into_syntax();
        let Some(ruff_python_ast::Stmt::Expr(expr_stmt)) = module.body.first() else {
            panic!("expected expression statement");
        };
        assert_eq!(dotted_name(&expr_stmt.value), Some("a.b.c".to_string()));
    }

    #[test]
    fn dotted_name_rejects_non_dotted_expressions() {
        let parsed = parse_module("a()\n").unwrap();
        let module = parsed.into_syntax();
        let Some(ruff_python_ast::Stmt::Expr(expr_stmt)) = module.body.first() else {
            panic!("expected expression statement");
        };
        assert_eq!(dotted_name(&expr_stmt.value), None);
    }

    #[test]
    fn referenced_names_collects_attributes_and_string_literals() {
        let src = "obj.run()\ngetattr(obj, \"other\")\n";
        let parsed = parse_module(src).unwrap();
        let module = parsed.into_syntax();
        let names = referenced_names(&module.body);
        assert!(names.contains("run"));
        assert!(names.contains("other"));
    }
}
