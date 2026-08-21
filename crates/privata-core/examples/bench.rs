//! Manual timing harness for privata-core, not part of the shipped crate.
//!
//! Usage: cargo run --release --example bench -- <project_root> [--methods] [--repeat N]

use std::path::PathBuf;
use std::time::Instant;

fn main() {
    let mut args = std::env::args().skip(1);
    let mut root: Option<PathBuf> = None;
    let mut include_methods = false;
    let mut repeat: u32 = 3;

    let mut dump: Option<PathBuf> = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--methods" => include_methods = true,
            "--repeat" => {
                repeat = args.next().and_then(|v| v.parse().ok()).unwrap_or(repeat);
            }
            "--dump" => dump = args.next().map(PathBuf::from),
            other => root = Some(PathBuf::from(other)),
        }
    }

    let root = root.expect("usage: bench <project_root> [--methods] [--repeat N] [--dump FILE]");

    let mut durations = Vec::new();
    let mut last_report = String::new();
    for i in 0..repeat {
        let start = Instant::now();
        let (report, code) = privata_core::checker::check_project(&root, include_methods);
        let elapsed = start.elapsed();
        durations.push(elapsed);
        eprintln!(
            "run {i}: {:.3}s (exit={code}, report_bytes={})",
            elapsed.as_secs_f64(),
            report.len()
        );
        last_report = report;
    }
    if let Some(dump) = dump {
        std::fs::write(dump, &last_report).expect("failed to write dump file");
    }

    let total: f64 = durations.iter().map(|d| d.as_secs_f64()).sum();
    let best = durations
        .iter()
        .map(|d| d.as_secs_f64())
        .fold(f64::MAX, f64::min);
    println!(
        "avg={:.3}s best={:.3}s runs={}",
        total / repeat as f64,
        best,
        repeat
    );
}
