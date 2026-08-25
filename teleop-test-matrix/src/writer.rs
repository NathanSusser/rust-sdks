//! Append-only JSON-lines output.
//!
//! Each line is flushed as it is written. A run killed mid-flight — which is the normal
//! outcome of a fault-injection suite — must still leave analyzable partial data behind,
//! so buffering whole runs in memory is not an option.

use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::Path;

/// Why a JSON-lines sink could not be opened or written.
#[derive(Debug)]
pub enum WriteError {
    /// The destination could not be created or opened for appending.
    Open { path: String, source: std::io::Error },
    /// A line could not be written or flushed.
    Write(std::io::Error),
    /// A record could not be serialized.
    Serialize(serde_json::Error),
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Open { path, source } => write!(f, "cannot open {path} for appending: {source}"),
            Self::Write(e) => write!(f, "cannot write record: {e}"),
            Self::Serialize(e) => write!(f, "cannot serialize record: {e}"),
        }
    }
}

impl std::error::Error for WriteError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Open { source, .. } => Some(source),
            Self::Write(e) => Some(e),
            Self::Serialize(e) => Some(e),
        }
    }
}

/// An append-only JSON-lines sink that flushes every line.
pub struct JsonLinesWriter {
    inner: BufWriter<File>,
    lines_written: u64,
}

impl JsonLinesWriter {
    /// Opens `path` for appending, creating it and any missing parent directories.
    pub fn create(path: &Path) -> Result<Self, WriteError> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|source| WriteError::Open {
                    path: parent.display().to_string(),
                    source,
                })?;
            }
        }
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|source| WriteError::Open { path: path.display().to_string(), source })?;
        Ok(Self { inner: BufWriter::new(file), lines_written: 0 })
    }

    /// Writes one pre-serialized line and flushes it to the operating system.
    pub fn write_line(&mut self, line: &str) -> Result<(), WriteError> {
        self.inner.write_all(line.as_bytes()).map_err(WriteError::Write)?;
        self.inner.flush().map_err(WriteError::Write)?;
        self.lines_written += 1;
        Ok(())
    }

    /// Serializes a record and writes it as one line.
    pub fn write_record<T: serde::Serialize>(&mut self, record: &T) -> Result<(), WriteError> {
        let mut line = serde_json::to_string(record).map_err(WriteError::Serialize)?;
        line.push('\n');
        self.write_line(&line)
    }

    /// Number of lines written through this handle.
    pub fn lines_written(&self) -> u64 {
        self.lines_written
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path(name: &str) -> std::path::PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("teleop-writer-{}-{}", std::process::id(), name));
        p
    }

    #[test]
    fn writes_one_object_per_line() {
        let path = temp_path("lines.jsonl");
        let _ = std::fs::remove_file(&path);
        let mut writer = JsonLinesWriter::create(&path).expect("create");
        writer.write_record(&serde_json::json!({"a": 1})).expect("write");
        writer.write_record(&serde_json::json!({"a": 2})).expect("write");
        assert_eq!(writer.lines_written(), 2);

        let body = std::fs::read_to_string(&path).expect("read");
        let lines: Vec<&str> = body.lines().collect();
        assert_eq!(lines.len(), 2);
        for line in lines {
            let v: serde_json::Value = serde_json::from_str(line).expect("each line parses");
            assert!(v.is_object());
        }
        let _ = std::fs::remove_file(&path);
    }

    /// Partial data must survive an abrupt end: every line already written is readable
    /// without the writer being closed cleanly.
    #[test]
    fn each_line_is_readable_before_the_writer_is_dropped() {
        let path = temp_path("partial.jsonl");
        let _ = std::fs::remove_file(&path);
        let mut writer = JsonLinesWriter::create(&path).expect("create");
        writer.write_record(&serde_json::json!({"poll": 0})).expect("write");
        let body = std::fs::read_to_string(&path).expect("read while writer is still open");
        assert_eq!(body.lines().count(), 1);
        writer.write_record(&serde_json::json!({"poll": 1})).expect("write");
        let body = std::fs::read_to_string(&path).expect("read again");
        assert_eq!(body.lines().count(), 2);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn creates_missing_parent_directories() {
        let mut dir = std::env::temp_dir();
        dir.push(format!("teleop-writer-nested-{}", std::process::id()));
        let path = dir.join("deeper").join("out.jsonl");
        let _ = std::fs::remove_dir_all(&dir);
        let mut writer = JsonLinesWriter::create(&path).expect("create nested");
        writer.write_record(&serde_json::json!({"ok": true})).expect("write");
        assert!(path.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
