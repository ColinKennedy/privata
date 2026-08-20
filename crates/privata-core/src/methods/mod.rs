//! Public-method privacy analysis.

mod candidates;
mod reexports;
mod test_references;

pub use candidates::collect_method_candidates;
pub use reexports::collect_reexports;
pub use test_references::referenced_names_by_module;
pub(crate) use test_references::referenced_names_by_module_names;
