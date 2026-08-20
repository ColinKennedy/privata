//! External public-interface discovery.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;

static ENTRYPOINT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$").expect("valid regex")
});
static UVICORN_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\buvicorn\s+([A-Za-z_][A-Za-z0-9_.]*):([A-Za-z_][A-Za-z0-9_]*)\b")
        .expect("valid regex")
});

/// Return symbols made public by external entrypoint declarations.
pub fn collect_external_entrypoints(project_root: &Path) -> HashSet<(String, String)> {
    let mut pairs = load_pyproject_entrypoints(project_root);
    pairs.extend(load_shell_uvicorn_entrypoints(project_root));
    pairs
}

/// Return pyproject console and GUI script entrypoint targets.
fn load_pyproject_entrypoints(project_root: &Path) -> HashSet<(String, String)> {
    let pyproject_path = project_root.join("pyproject.toml");
    let Ok(text) = std::fs::read_to_string(&pyproject_path) else {
        return HashSet::new();
    };
    let Ok(data) = text.parse::<toml::Table>() else {
        return HashSet::new();
    };
    let Some(project_table) = data.get("project").and_then(toml::Value::as_table) else {
        return HashSet::new();
    };

    let mut pairs = HashSet::new();
    for table_key in ["scripts", "gui-scripts"] {
        let Some(table) = project_table.get(table_key).and_then(toml::Value::as_table) else {
            continue;
        };
        for value in table.values() {
            let Some(raw) = value.as_str() else { continue };
            if !ENTRYPOINT_RE.is_match(raw) {
                continue;
            }
            if let Some((module_name, symbol_name)) = raw.split_once(':') {
                pairs.insert((module_name.to_string(), symbol_name.to_string()));
            }
        }
    }
    pairs
}

/// Return shell-like files that may launch Python entrypoints.
fn entrypoint_shell_files(project_root: &Path) -> Vec<PathBuf> {
    let mut files: HashSet<PathBuf> = HashSet::new();

    if let Ok(entries) = std::fs::read_dir(project_root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_file() {
                continue;
            }
            let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
                continue;
            };
            if name.ends_with(".sh") || name.starts_with("Dockerfile") {
                files.insert(path);
            }
        }
    }

    let scripts_dir = project_root.join("scripts");
    if scripts_dir.is_dir() {
        let shell_files = walkdir::WalkDir::new(&scripts_dir)
            .into_iter()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_type().is_file())
            .map(walkdir::DirEntry::into_path)
            .filter(|path| path.extension().and_then(|e| e.to_str()) == Some("sh"));
        files.extend(shell_files);
    }

    let mut files: Vec<PathBuf> = files.into_iter().collect();
    files.sort();
    files
}

/// Return Uvicorn app targets referenced by shell files.
fn load_shell_uvicorn_entrypoints(project_root: &Path) -> HashSet<(String, String)> {
    let mut pairs = HashSet::new();
    for path in entrypoint_shell_files(project_root) {
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        for captures in UVICORN_RE.captures_iter(&text) {
            pairs.insert((captures[1].to_string(), captures[2].to_string()));
        }
    }
    pairs
}

/// Return symbols exposed by Tach interfaces.
pub fn load_tach_interface_exports(project_root: &Path) -> HashSet<(String, String)> {
    let tach_path = project_root.join("tach.toml");
    let Ok(text) = std::fs::read_to_string(&tach_path) else {
        return HashSet::new();
    };
    let Ok(data) = text.parse::<toml::Table>() else {
        return HashSet::new();
    };
    let Some(interfaces) = data.get("interfaces").and_then(toml::Value::as_array) else {
        return HashSet::new();
    };

    let mut pairs = HashSet::new();
    for interface in interfaces {
        let Some(interface) = interface.as_table() else {
            continue;
        };
        let Some(source_modules) = interface.get("from").and_then(toml::Value::as_array) else {
            continue;
        };
        let Some(exposed_names) = interface.get("expose").and_then(toml::Value::as_array) else {
            continue;
        };
        for module_name in source_modules {
            let Some(module_name) = module_name.as_str() else {
                continue;
            };
            for symbol_name in exposed_names {
                if let Some(symbol_name) = symbol_name.as_str() {
                    pairs.insert((module_name.to_string(), symbol_name.to_string()));
                }
            }
        }
    }
    pairs
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
    fn pyproject_console_scripts_are_collected() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "pyproject.toml",
            "[project]\nname = \"example\"\nversion = \"0.1.0\"\nscripts.example = \"pkg.cli:main\"\n",
        );
        let pairs = collect_external_entrypoints(tmp.path());
        assert!(pairs.contains(&("pkg.cli".to_string(), "main".to_string())));
    }

    #[test]
    fn shell_uvicorn_entrypoints_are_collected() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "run-server.sh",
            "#!/usr/bin/env bash\nexec uvicorn pkg.server:asgi --host 0.0.0.0 --port 8000\n",
        );
        let pairs = collect_external_entrypoints(tmp.path());
        assert!(pairs.contains(&("pkg.server".to_string(), "asgi".to_string())));
    }

    #[test]
    fn nested_scripts_directory_shell_files_are_scanned() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "scripts/deploy/run.sh",
            "uvicorn pkg.app:server\n",
        );
        let pairs = collect_external_entrypoints(tmp.path());
        assert!(pairs.contains(&("pkg.app".to_string(), "server".to_string())));
    }

    #[test]
    fn tach_interfaces_expose_pairs() {
        let tmp = TempDir::new().unwrap();
        write(
            tmp.path(),
            "tach.toml",
            "[[interfaces]]\nfrom = [\"pkg.core\"]\nexpose = [\"Service\", \"helper\"]\n",
        );
        let pairs = load_tach_interface_exports(tmp.path());
        assert!(pairs.contains(&("pkg.core".to_string(), "Service".to_string())));
        assert!(pairs.contains(&("pkg.core".to_string(), "helper".to_string())));
    }

    #[test]
    fn missing_files_produce_no_entrypoints() {
        let tmp = TempDir::new().unwrap();
        assert!(collect_external_entrypoints(tmp.path()).is_empty());
        assert!(load_tach_interface_exports(tmp.path()).is_empty());
    }
}
