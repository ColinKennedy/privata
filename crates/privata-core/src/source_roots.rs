//! Source-root discovery and source-file filtering.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;

const IGNORED_SOURCE_DIR_NAMES: &[&str] = &[
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "htmlcov",
    "site",
    "tests",
];

fn is_ignored_source_dir_name(name: &str) -> bool {
    IGNORED_SOURCE_DIR_NAMES.contains(&name)
}

static TEST_FILENAME_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:test_\w+|\w+_test|test[A-Z]\w*|\w+Test)\.py$").expect("valid regex")
});

/// Return whether a filename matches a recognised test-file naming convention.
pub fn is_test_module_filename(name: &str) -> bool {
    TEST_FILENAME_RE.is_match(name)
}

fn src_dir(project_root: &Path) -> Option<PathBuf> {
    let src = project_root.join("src");
    src.is_dir().then_some(src)
}

fn read_tach_toml_paths(project_root: &Path, key: &str) -> Vec<PathBuf> {
    let tach_path = project_root.join("tach.toml");
    let Ok(text) = std::fs::read_to_string(&tach_path) else {
        return Vec::new();
    };
    let Ok(data) = text.parse::<toml::Table>() else {
        return Vec::new();
    };
    let Some(entries) = data.get(key).and_then(toml::Value::as_array) else {
        return Vec::new();
    };

    let mut roots = Vec::new();
    for entry in entries {
        let Some(rel) = entry.as_str() else { continue };
        let root = project_root.join(rel);
        let Ok(root) = dunce::canonicalize(&root) else {
            continue;
        };
        if root.is_dir() {
            roots.push(root);
        }
    }
    roots
}

fn load_tach_source_roots(project_root: &Path) -> Vec<PathBuf> {
    read_tach_toml_paths(project_root, "source_roots")
}

/// Resolve source roots for a project.
///
/// These are the roots findings are reported for. Use
/// [`privata_search_paths`] to widen what gets *searched* (e.g. for
/// cross-references) without widening what gets *reported*.
pub fn source_roots(project_root: &Path) -> Vec<PathBuf> {
    let tach_roots = load_tach_source_roots(project_root);
    if !tach_roots.is_empty() {
        return tach_roots;
    }

    if let Some(src) = src_dir(project_root) {
        return vec![src];
    }

    vec![project_root.to_path_buf()]
}

/// Resolve `tach.toml` `privata_search_paths`: directories that are searched
/// for cross-references (imports, usages) but never themselves reported on.
///
/// A path may sit anywhere — inside or outside the project — and may nest
/// around a `source_roots` entry, e.g. `source_roots = ["python/pkg"]` with
/// `privata_search_paths = ["python"]` searches all of `python/` so
/// cross-package usage resolves correctly, while only reporting issues found
/// under `python/pkg`.
pub fn privata_search_paths(project_root: &Path) -> Vec<PathBuf> {
    read_tach_toml_paths(project_root, "privata_search_paths")
}

/// Remove any root that is nested inside another root in the list, keeping
/// only the outermost roots.
///
/// A nested root would otherwise be walked twice — once under its own name
/// and once as part of its ancestor's walk — producing two different (and
/// differently-qualified) module identities for the same file.
fn dedup_nested_roots(mut roots: Vec<PathBuf>) -> Vec<PathBuf> {
    roots.sort();
    roots.dedup();
    let mut kept: Vec<PathBuf> = Vec::new();
    for root in roots {
        if !kept.iter().any(|existing| root.starts_with(existing)) {
            kept.push(root);
        }
    }
    kept
}

/// Resolve every root that should be searched: `source_roots` plus
/// `privata_search_paths`, with nested roots collapsed to their outermost
/// ancestor.
pub fn all_search_roots(project_root: &Path) -> Vec<PathBuf> {
    let mut roots = source_roots(project_root);
    roots.extend(privata_search_paths(project_root));
    dedup_nested_roots(roots)
}

/// Return whether a Python file should be ignored as non-production source.
pub fn should_skip_source_file(py_file: &Path, source_root: &Path) -> bool {
    is_test_module_filename(file_name(py_file)) || is_in_ignored_directory(py_file, source_root)
}

fn file_name(path: &Path) -> &str {
    path.file_name().and_then(|n| n.to_str()).unwrap_or("")
}

fn relative_dir_parts<'a>(py_file: &'a Path, source_root: &Path) -> Vec<&'a str> {
    let rel = py_file.strip_prefix(source_root).unwrap_or(py_file);
    let mut parts: Vec<&str> = rel.iter().filter_map(|p| p.to_str()).collect();
    parts.pop(); // drop the filename, keep only directory components
    parts
}

/// Return whether a file sits inside an ignored or hidden directory.
pub fn is_in_ignored_directory(py_file: &Path, source_root: &Path) -> bool {
    relative_dir_parts(py_file, source_root)
        .into_iter()
        .any(|part| is_ignored_source_dir_name(part) || (part.starts_with('.') && part != "."))
}

/// Return whether a source root is itself a test directory by name.
pub fn is_test_source_root(source_root: &Path) -> bool {
    source_root
        .file_name()
        .and_then(|n| n.to_str())
        .is_some_and(is_ignored_source_dir_name)
}

/// Return whether a file is test-shaped source nested inside a production root.
///
/// `should_skip_source_file` already discards these from the production
/// module scan, either by filename pattern or because they sit under a
/// nested `tests/` directory. This identifies that same set of files so they
/// can be swept up as test consumers instead of silently disappearing. Files
/// skipped for unrelated reasons (build output, caches, venvs, hidden
/// directories) are excluded: they are not test code, and some of them (a
/// `.venv`, say) can contain unrelated third-party files that happen to
/// match the test naming convention.
pub fn is_nested_test_file(py_file: &Path, source_root: &Path) -> bool {
    let dir_parts = relative_dir_parts(py_file, source_root);
    let other_ignored = dir_parts.iter().any(|part| {
        (is_ignored_source_dir_name(part) && *part != "tests")
            || (part.starts_with('.') && *part != ".")
    });
    if other_ignored {
        return false;
    }
    is_test_module_filename(file_name(py_file)) || dir_parts.contains(&"tests")
}

/// Recursively collect every `*.py` file under `root`, sorted for
/// deterministic, first-source-root-wins iteration.
pub fn walk_python_files(root: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = walkdir::WalkDir::new(root)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_file())
        .map(walkdir::DirEntry::into_path)
        .filter(|path| path.extension().and_then(|e| e.to_str()) == Some("py"))
        .collect();
    files.sort();
    files
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, contents: &str) -> PathBuf {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(&path, contents).unwrap();
        path
    }

    #[test]
    fn recognises_conventional_test_filenames() {
        for name in ["test_foo.py", "foo_test.py", "testFoo.py", "fooTest.py"] {
            assert!(is_test_module_filename(name), "{name} should match");
        }
    }

    #[test]
    fn rejects_non_test_filenames() {
        for name in [
            "foo.py",
            "conftest.py",
            "testfoo.py",
            "footest.py",
            "__init__.py",
        ] {
            assert!(!is_test_module_filename(name), "{name} should not match");
        }
    }

    #[test]
    fn defaults_to_project_root_when_no_src_or_tach() {
        let tmp = TempDir::new().unwrap();
        let roots = source_roots(tmp.path());
        assert_eq!(roots, vec![tmp.path().to_path_buf()]);
    }

    #[test]
    fn prefers_src_directory_when_present() {
        let tmp = TempDir::new().unwrap();
        fs::create_dir_all(tmp.path().join("src")).unwrap();
        let roots = source_roots(tmp.path());
        assert_eq!(roots, vec![tmp.path().join("src")]);
    }

    #[test]
    fn tach_source_roots_take_precedence_over_src() {
        let tmp = TempDir::new().unwrap();
        fs::create_dir_all(tmp.path().join("src")).unwrap();
        fs::create_dir_all(tmp.path().join("lib")).unwrap();
        write(tmp.path(), "tach.toml", "source_roots = [\"lib\"]\n");
        let roots = source_roots(tmp.path());
        assert_eq!(
            roots,
            vec![dunce::canonicalize(tmp.path().join("lib")).unwrap()]
        );
    }

    #[test]
    fn malformed_tach_source_roots_fall_back_to_project_layout() {
        let tmp = TempDir::new().unwrap();
        fs::create_dir_all(tmp.path().join("src")).unwrap();
        write(tmp.path(), "tach.toml", "source_roots = \"not-a-list\"\n");
        let roots = source_roots(tmp.path());
        assert_eq!(roots, vec![tmp.path().join("src")]);
    }

    #[test]
    fn tach_source_roots_skip_nonexistent_directories() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "tach.toml", "source_roots = [\"missing\"]\n");
        let roots = source_roots(tmp.path());
        assert_eq!(roots, vec![tmp.path().to_path_buf()]);
    }

    #[test]
    fn privata_search_paths_are_empty_by_default() {
        let tmp = TempDir::new().unwrap();
        assert!(privata_search_paths(tmp.path()).is_empty());
    }

    #[test]
    fn privata_search_paths_are_read_from_tach_toml() {
        let tmp = TempDir::new().unwrap();
        fs::create_dir_all(tmp.path().join("python")).unwrap();
        write(
            tmp.path(),
            "tach.toml",
            "source_roots = [\"python/pkg\"]\nprivata_search_paths = [\"python\"]\n",
        );
        fs::create_dir_all(tmp.path().join("python/pkg")).unwrap();
        let paths = privata_search_paths(tmp.path());
        assert_eq!(
            paths,
            vec![dunce::canonicalize(tmp.path().join("python")).unwrap()]
        );
    }

    #[test]
    fn all_search_roots_collapses_a_source_root_nested_inside_a_search_path() {
        let tmp = TempDir::new().unwrap();
        fs::create_dir_all(tmp.path().join("python/pkg")).unwrap();
        write(
            tmp.path(),
            "tach.toml",
            "source_roots = [\"python/pkg\"]\nprivata_search_paths = [\"python\"]\n",
        );
        let roots = all_search_roots(tmp.path());
        assert_eq!(
            roots,
            vec![dunce::canonicalize(tmp.path().join("python")).unwrap()]
        );
    }

    #[test]
    fn all_search_roots_keeps_disjoint_source_and_search_roots() {
        let tmp = TempDir::new().unwrap();
        fs::create_dir_all(tmp.path().join("src")).unwrap();
        fs::create_dir_all(tmp.path().join("reference")).unwrap();
        write(
            tmp.path(),
            "tach.toml",
            "source_roots = [\"src\"]\nprivata_search_paths = [\"reference\"]\n",
        );
        let roots = all_search_roots(tmp.path());
        assert_eq!(
            roots,
            vec![
                dunce::canonicalize(tmp.path().join("reference")).unwrap(),
                dunce::canonicalize(tmp.path().join("src")).unwrap(),
            ]
        );
    }

    #[test]
    fn ignored_directory_detection_covers_hidden_and_named_dirs() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        assert!(is_in_ignored_directory(&root.join("build/mod.py"), root));
        assert!(is_in_ignored_directory(&root.join(".hidden/mod.py"), root));
        assert!(is_in_ignored_directory(
            &root.join("pkg/__pycache__/mod.py"),
            root
        ));
        assert!(!is_in_ignored_directory(&root.join("pkg/mod.py"), root));
    }

    #[test]
    fn should_skip_source_file_matches_test_filenames_and_ignored_dirs() {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path();
        assert!(should_skip_source_file(&root.join("test_mod.py"), root));
        assert!(should_skip_source_file(&root.join("tests/mod.py"), root));
        assert!(!should_skip_source_file(&root.join("pkg/mod.py"), root));
    }

    #[test]
    fn is_test_source_root_matches_conventional_names() {
        assert!(is_test_source_root(Path::new("/proj/tests")));
        assert!(!is_test_source_root(Path::new("/proj/src")));
    }

    #[test]
    fn nested_test_file_matches_test_names_or_tests_dir_but_not_other_ignored_dirs() {
        let root = Path::new("/proj/src");
        assert!(is_nested_test_file(&root.join("test_mod.py"), root));
        assert!(is_nested_test_file(&root.join("tests/helper.py"), root));
        assert!(!is_nested_test_file(&root.join("pkg/mod.py"), root));
        assert!(!is_nested_test_file(
            &root.join("build/tests/helper.py"),
            root
        ));
        assert!(!is_nested_test_file(
            &root.join(".venv/tests/helper.py"),
            root
        ));
    }

    #[test]
    fn walk_python_files_is_sorted_and_recursive() {
        let tmp = TempDir::new().unwrap();
        write(tmp.path(), "b/mod.py", "");
        write(tmp.path(), "a/mod.py", "");
        write(tmp.path(), "a/mod.txt", "");
        let files = walk_python_files(tmp.path());
        assert_eq!(files.len(), 2);
        assert!(files[0] < files[1]);
    }
}
