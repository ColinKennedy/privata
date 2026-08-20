//! Shared data models for privacy checks.

use std::collections::HashSet;
use std::fmt;
use std::path::PathBuf;

use ruff_python_ast::ModModule;
use ruff_source_file::LineIndex;

pub const NAMESPACE_SEPARATOR: &str = ".";

/// The syntactic kind of a top-level symbol.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SymbolKind {
    Function,
    Class,
    Variable,
}

impl fmt::Display for SymbolKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            SymbolKind::Function => "function",
            SymbolKind::Class => "class",
            SymbolKind::Variable => "variable",
        };
        f.write_str(s)
    }
}

/// A public top-level symbol found in a module.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Symbol {
    pub name: String,
    pub kind: SymbolKind,
    pub lineno: u32,
    pub module: String,
    pub path: PathBuf,
}

/// A public method that no other production module references.
///
/// `class_lineno` and `class_public_methods` describe the owning class, so a
/// caller can group findings and see how much of the class they cover. Ten
/// findings out of ten public methods is one question about the class; ten
/// out of a hundred is ten separate helpers that leaked.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Method {
    pub name: String,
    pub class_name: String,
    pub lineno: u32,
    pub module: String,
    pub path: PathBuf,
    pub class_lineno: u32,
    pub class_public_methods: u32,
}

/// A parsed Python module with its top-level symbols.
pub struct Module {
    pub name: String,
    pub path: PathBuf,
    pub package_parts: Vec<String>,
    pub symbols: Vec<Symbol>,
    pub private_symbols: Vec<Symbol>,
    pub tree: Option<ModModule>,
    pub line_index: Option<LineIndex>,
    pub ignored_lines: HashSet<u32>,
    pub exports: HashSet<String>,
}

impl Module {
    pub fn new(name: String, path: PathBuf, package_parts: Vec<String>) -> Self {
        Module {
            name,
            path,
            package_parts,
            symbols: Vec::new(),
            private_symbols: Vec::new(),
            tree: None,
            line_index: None,
            ignored_lines: HashSet::new(),
            exports: HashSet::new(),
        }
    }
}

/// A module name that resolves to more than one file across source roots.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModuleCollision {
    pub module: String,
    pub paths: Vec<PathBuf>,
}

/// A source file that could not be parsed, so its references were not seen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnparsableModule {
    pub module: String,
    pub path: PathBuf,
    pub lineno: u32,
    pub message: String,
}

/// A private module imported from outside its containing package subtree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrivateModuleImport {
    pub module: String,
    pub path: PathBuf,
    pub imported_by: String,
    pub imported_by_path: PathBuf,
    pub lineno: u32,
}

/// A private top-level symbol imported from another production module.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrivateSymbolImport {
    pub module: String,
    pub name: String,
    pub path: PathBuf,
    pub imported_by: String,
    pub imported_by_path: PathBuf,
    pub lineno: u32,
}

/// The kind of mismatch between a literal `__all__` and public module bindings.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ExportIssueKind {
    /// `__all__` names something that is not bound in the module.
    Unknown,
    /// `__all__` names a private binding.
    Private,
    /// A public binding is missing from `__all__`.
    Missing,
}

impl fmt::Display for ExportIssueKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let s = match self {
            ExportIssueKind::Unknown => "unknown",
            ExportIssueKind::Private => "private",
            ExportIssueKind::Missing => "missing",
        };
        f.write_str(s)
    }
}

/// A mismatch between literal `__all__` and public module bindings.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportIssue {
    pub module: String,
    pub path: PathBuf,
    pub name: String,
    pub kind: ExportIssueKind,
    pub lineno: u32,
}

/// A candidate top-level symbol before filtering.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SymbolCandidate {
    pub name: String,
    pub kind: SymbolKind,
    pub lineno: u32,
}
