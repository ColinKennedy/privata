//! PyO3 bindings exposing privata's public checker API.
//!
//! Only the project-root-driven `find_*` functions from the original Python
//! package's public surface are bound here, along with the plain-data
//! finding types they return. The lower-level, composable `collect_*`
//! helpers (which pass `Module` objects — each carrying a parsed AST —
//! between calls) stay internal to `privata-core`: there is no practical way
//! to hand a ruff AST across the Python/Rust boundary, and every one of
//! those helpers is reachable indirectly through the `find_*` functions
//! anyway.

use std::path::PathBuf;

use pyo3::prelude::*;

macro_rules! path_string {
    ($path:expr) => {
        $path.to_string_lossy().into_owned()
    };
}

#[pyclass(get_all, frozen)]
#[derive(Clone)]
pub struct Symbol {
    name: String,
    kind: String,
    lineno: u32,
    module: String,
    path: String,
}

#[pymethods]
impl Symbol {
    fn __repr__(&self) -> String {
        format!(
            "Symbol(name={:?}, kind={:?}, lineno={}, module={:?}, path={:?})",
            self.name, self.kind, self.lineno, self.module, self.path
        )
    }
}

impl From<&privata_core::models::Symbol> for Symbol {
    fn from(s: &privata_core::models::Symbol) -> Self {
        Symbol {
            name: s.name.clone(),
            kind: s.kind.to_string(),
            lineno: s.lineno,
            module: s.module.clone(),
            path: path_string!(s.path),
        }
    }
}

#[pyclass(get_all, frozen)]
#[derive(Clone)]
pub struct Method {
    name: String,
    class_name: String,
    lineno: u32,
    module: String,
    path: String,
    class_lineno: u32,
    class_public_methods: u32,
}

#[pymethods]
impl Method {
    fn __repr__(&self) -> String {
        format!(
            "Method(name={:?}, class_name={:?}, lineno={}, module={:?}, path={:?}, \
class_lineno={}, class_public_methods={})",
            self.name,
            self.class_name,
            self.lineno,
            self.module,
            self.path,
            self.class_lineno,
            self.class_public_methods
        )
    }
}

impl From<&privata_core::models::Method> for Method {
    fn from(m: &privata_core::models::Method) -> Self {
        Method {
            name: m.name.clone(),
            class_name: m.class_name.clone(),
            lineno: m.lineno,
            module: m.module.clone(),
            path: path_string!(m.path),
            class_lineno: m.class_lineno,
            class_public_methods: m.class_public_methods,
        }
    }
}

#[pyclass(get_all, frozen)]
#[derive(Clone)]
pub struct ModuleCollision {
    module: String,
    paths: Vec<String>,
}

#[pymethods]
impl ModuleCollision {
    fn __repr__(&self) -> String {
        format!(
            "ModuleCollision(module={:?}, paths={:?})",
            self.module, self.paths
        )
    }
}

impl From<&privata_core::models::ModuleCollision> for ModuleCollision {
    fn from(m: &privata_core::models::ModuleCollision) -> Self {
        ModuleCollision {
            module: m.module.clone(),
            paths: m.paths.iter().map(|p| path_string!(p)).collect(),
        }
    }
}

#[pyclass(get_all, frozen)]
#[derive(Clone)]
pub struct UnparsableModule {
    module: String,
    path: String,
    lineno: u32,
    message: String,
}

#[pymethods]
impl UnparsableModule {
    fn __repr__(&self) -> String {
        format!(
            "UnparsableModule(module={:?}, path={:?}, lineno={}, message={:?})",
            self.module, self.path, self.lineno, self.message
        )
    }
}

impl From<&privata_core::models::UnparsableModule> for UnparsableModule {
    fn from(m: &privata_core::models::UnparsableModule) -> Self {
        UnparsableModule {
            module: m.module.clone(),
            path: path_string!(m.path),
            lineno: m.lineno,
            message: m.message.clone(),
        }
    }
}

#[pyclass(get_all, frozen)]
#[derive(Clone)]
pub struct PrivateModuleImport {
    module: String,
    path: String,
    imported_by: String,
    imported_by_path: String,
    lineno: u32,
}

#[pymethods]
impl PrivateModuleImport {
    fn __repr__(&self) -> String {
        format!(
            "PrivateModuleImport(module={:?}, path={:?}, imported_by={:?}, \
imported_by_path={:?}, lineno={})",
            self.module, self.path, self.imported_by, self.imported_by_path, self.lineno
        )
    }
}

impl From<&privata_core::models::PrivateModuleImport> for PrivateModuleImport {
    fn from(m: &privata_core::models::PrivateModuleImport) -> Self {
        PrivateModuleImport {
            module: m.module.clone(),
            path: path_string!(m.path),
            imported_by: m.imported_by.clone(),
            imported_by_path: path_string!(m.imported_by_path),
            lineno: m.lineno,
        }
    }
}

#[pyclass(get_all, frozen)]
#[derive(Clone)]
pub struct PrivateSymbolImport {
    module: String,
    name: String,
    path: String,
    imported_by: String,
    imported_by_path: String,
    lineno: u32,
}

#[pymethods]
impl PrivateSymbolImport {
    fn __repr__(&self) -> String {
        format!(
            "PrivateSymbolImport(module={:?}, name={:?}, path={:?}, imported_by={:?}, \
imported_by_path={:?}, lineno={})",
            self.module, self.name, self.path, self.imported_by, self.imported_by_path, self.lineno
        )
    }
}

impl From<&privata_core::models::PrivateSymbolImport> for PrivateSymbolImport {
    fn from(m: &privata_core::models::PrivateSymbolImport) -> Self {
        PrivateSymbolImport {
            module: m.module.clone(),
            name: m.name.clone(),
            path: path_string!(m.path),
            imported_by: m.imported_by.clone(),
            imported_by_path: path_string!(m.imported_by_path),
            lineno: m.lineno,
        }
    }
}

#[pyclass(get_all, frozen)]
#[derive(Clone)]
pub struct ExportIssue {
    module: String,
    path: String,
    name: String,
    kind: String,
    lineno: u32,
}

#[pymethods]
impl ExportIssue {
    fn __repr__(&self) -> String {
        format!(
            "ExportIssue(module={:?}, path={:?}, name={:?}, kind={:?}, lineno={})",
            self.module, self.path, self.name, self.kind, self.lineno
        )
    }
}

impl From<&privata_core::models::ExportIssue> for ExportIssue {
    fn from(e: &privata_core::models::ExportIssue) -> Self {
        ExportIssue {
            module: e.module.clone(),
            path: path_string!(e.path),
            name: e.name.clone(),
            kind: e.kind.to_string(),
            lineno: e.lineno,
        }
    }
}

#[pyfunction]
fn find_unparsable_modules(project_root: PathBuf) -> Vec<UnparsableModule> {
    privata_core::checker::find_unparsable_modules(&project_root)
        .iter()
        .map(Into::into)
        .collect()
}

#[pyfunction]
fn find_private_candidates(project_root: PathBuf) -> Vec<Symbol> {
    privata_core::checker::find_private_candidates(&project_root)
        .iter()
        .map(Into::into)
        .collect()
}

#[pyfunction]
fn find_method_candidates(project_root: PathBuf) -> Vec<Method> {
    privata_core::checker::find_method_candidates(&project_root)
        .iter()
        .map(Into::into)
        .collect()
}

#[pyfunction]
fn find_private_module_imports(project_root: PathBuf) -> Vec<PrivateModuleImport> {
    privata_core::checker::find_private_module_imports(&project_root)
        .iter()
        .map(Into::into)
        .collect()
}

#[pyfunction]
fn find_private_symbol_imports(project_root: PathBuf) -> Vec<PrivateSymbolImport> {
    privata_core::checker::find_private_symbol_imports(&project_root)
        .iter()
        .map(Into::into)
        .collect()
}

#[pyfunction]
fn find_export_issues(project_root: PathBuf) -> Vec<ExportIssue> {
    privata_core::checker::find_export_issues(&project_root)
        .iter()
        .map(Into::into)
        .collect()
}

#[pyfunction]
fn find_module_collisions(project_root: PathBuf) -> Vec<ModuleCollision> {
    privata_core::checker::find_module_collisions(&project_root)
        .iter()
        .map(Into::into)
        .collect()
}

/// Run the full checker and return `(report_text, exit_code)`.
///
/// Printing happens on the Python side (`cli.py`) rather than here, so that
/// pytest's `capsys` — which patches `sys.stdout`, not the OS file
/// descriptor a native extension would write to — can still capture CLI
/// output in tests.
#[pyfunction]
#[pyo3(name = "check_project", signature = (project_root, methods=false))]
fn check_project_py(project_root: PathBuf, methods: bool) -> (String, i32) {
    privata_core::checker::check_project(&project_root, methods)
}

#[pymodule]
fn _privata(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<Symbol>()?;
    m.add_class::<Method>()?;
    m.add_class::<ModuleCollision>()?;
    m.add_class::<UnparsableModule>()?;
    m.add_class::<PrivateModuleImport>()?;
    m.add_class::<PrivateSymbolImport>()?;
    m.add_class::<ExportIssue>()?;
    m.add_function(wrap_pyfunction!(find_unparsable_modules, m)?)?;
    m.add_function(wrap_pyfunction!(find_private_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(find_method_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(find_private_module_imports, m)?)?;
    m.add_function(wrap_pyfunction!(find_private_symbol_imports, m)?)?;
    m.add_function(wrap_pyfunction!(find_export_issues, m)?)?;
    m.add_function(wrap_pyfunction!(find_module_collisions, m)?)?;
    m.add_function(wrap_pyfunction!(check_project_py, m)?)?;
    Ok(())
}
