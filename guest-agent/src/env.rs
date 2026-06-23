//! Managed Linux environment file endpoints.

use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const ENV_FILE: &str = "/etc/profile.d/smolvm_env.sh";
static ENV_UPDATE_LOCK: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));

#[derive(Debug, Serialize)]
pub struct EnvResponse {
    pub ok: bool,
    pub vars: BTreeMap<String, String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct EnvPutRequest {
    pub vars: BTreeMap<String, String>,
    #[serde(default = "default_merge")]
    pub merge: bool,
}

#[derive(Debug, Deserialize)]
pub struct EnvDeleteRequest {
    pub keys: Vec<String>,
}

fn default_merge() -> bool {
    true
}

pub async fn read_managed() -> EnvResponse {
    match read_env_file(Path::new(ENV_FILE)) {
        Ok(vars) => EnvResponse {
            ok: true,
            vars,
            error: None,
        },
        Err(error) => EnvResponse {
            ok: false,
            vars: BTreeMap::new(),
            error: Some(error),
        },
    }
}

pub async fn put_managed(req: EnvPutRequest) -> EnvResponse {
    put_managed_at(Path::new(ENV_FILE), req)
}

fn put_managed_at(path: &Path, req: EnvPutRequest) -> EnvResponse {
    for key in req.vars.keys() {
        if let Err(error) = validate_key(key) {
            return error_response(error);
        }
    }
    let _guard = match ENV_UPDATE_LOCK.lock() {
        Ok(guard) => guard,
        Err(_) => return error_response("environment update lock is unavailable"),
    };
    let mut vars = if req.merge {
        match read_env_file(path) {
            Ok(vars) => vars,
            Err(error) => return error_response(error),
        }
    } else {
        BTreeMap::new()
    };
    vars.extend(req.vars);
    match atomic_write_env(path, &vars) {
        Ok(()) => EnvResponse {
            ok: true,
            vars,
            error: None,
        },
        Err(error) => error_response(error),
    }
}

pub async fn delete_managed(req: EnvDeleteRequest) -> EnvResponse {
    delete_managed_at(Path::new(ENV_FILE), req)
}

fn delete_managed_at(path: &Path, req: EnvDeleteRequest) -> EnvResponse {
    for key in &req.keys {
        if let Err(error) = validate_key(key) {
            return error_response(error);
        }
    }
    let _guard = match ENV_UPDATE_LOCK.lock() {
        Ok(guard) => guard,
        Err(_) => return error_response("environment update lock is unavailable"),
    };
    let mut vars = match read_env_file(path) {
        Ok(vars) => vars,
        Err(error) => return error_response(error),
    };
    for key in req.keys {
        vars.remove(&key);
    }
    match atomic_write_env(path, &vars) {
        Ok(()) => EnvResponse {
            ok: true,
            vars,
            error: None,
        },
        Err(error) => error_response(error),
    }
}

fn error_response(error: impl Into<String>) -> EnvResponse {
    EnvResponse {
        ok: false,
        vars: BTreeMap::new(),
        error: Some(error.into()),
    }
}

fn validate_key(key: &str) -> Result<(), String> {
    let mut chars = key.chars();
    match chars.next() {
        Some(ch) if ch == '_' || ch.is_ascii_alphabetic() => {}
        _ => return Err(format!("invalid environment variable key: {key}")),
    }
    if chars.any(|ch| ch != '_' && !ch.is_ascii_alphanumeric()) {
        return Err(format!("invalid environment variable key: {key}"));
    }
    Ok(())
}

fn read_env_file(path: &Path) -> Result<BTreeMap<String, String>, String> {
    if !path.exists() {
        return Ok(BTreeMap::new());
    }
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    let mut vars = BTreeMap::new();
    for line in content.lines() {
        let line = line.trim();
        if !line.starts_with("export ") {
            continue;
        }
        let rest = &line["export ".len()..];
        if let Some((key, value)) = rest.split_once('=') {
            if validate_key(key).is_ok() {
                vars.insert(key.to_string(), parse_shell_value(value));
            }
        }
    }
    Ok(vars)
}

fn parse_shell_value(value: &str) -> String {
    let mut out = String::new();
    let mut chars = value.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            '\'' => {
                for inner in chars.by_ref() {
                    if inner == '\'' {
                        break;
                    }
                    out.push(inner);
                }
            }
            '"' => {
                while let Some(inner) = chars.next() {
                    if inner == '"' {
                        break;
                    }
                    if inner == '\\' {
                        if let Some(escaped) = chars.next() {
                            out.push(escaped);
                        }
                    } else {
                        out.push(inner);
                    }
                }
            }
            '\\' => {
                if let Some(escaped) = chars.next() {
                    out.push(escaped);
                }
            }
            ch if ch.is_whitespace() => break,
            other => out.push(other),
        }
    }
    out
}

fn atomic_write_env(path: &Path, vars: &BTreeMap<String, String>) -> Result<(), String> {
    let content = build_env_script(vars)?;
    let parent = path.parent().unwrap_or_else(|| Path::new("/"));
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let (mut file, tmp) = create_temp_file(parent)?;
    if let Err(error) = file.write_all(content.as_bytes()) {
        let _ = fs::remove_file(&tmp);
        return Err(error.to_string());
    }
    if let Err(error) = file.set_permissions(fs::Permissions::from_mode(0o644)) {
        let _ = fs::remove_file(&tmp);
        return Err(error.to_string());
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&tmp);
        return Err(error.to_string());
    }
    drop(file);
    fs::rename(&tmp, path).map_err(|error| {
        let _ = fs::remove_file(&tmp);
        error.to_string()
    })?;
    sync_parent_dir(parent)
}

fn build_env_script(vars: &BTreeMap<String, String>) -> Result<String, String> {
    if vars.is_empty() {
        return Ok("# SmolVM environment variables (empty)\n".to_string());
    }
    let mut lines = vec![
        "#!/bin/sh".to_string(),
        "# SmolVM managed environment variables".to_string(),
        String::new(),
    ];
    for (key, value) in vars {
        validate_key(key)?;
        lines.push(format!("export {key}={}", shell_quote(value)));
    }
    lines.push(String::new());
    Ok(lines.join("\n"))
}

fn shell_quote(value: &str) -> String {
    if value.is_empty() {
        return "''".to_string();
    }
    if value
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || "@%_+=:,./-".contains(ch))
    {
        return value.to_string();
    }
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn create_temp_file(parent: &Path) -> Result<(File, PathBuf), String> {
    for attempt in 0..100 {
        let path = tmp_path(parent, attempt);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok((file, path)),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.to_string()),
        }
    }
    Err("cannot create temporary environment file".to_string())
}

fn sync_parent_dir(parent: &Path) -> Result<(), String> {
    File::open(parent)
        .and_then(|file| file.sync_all())
        .map_err(|error| error.to_string())
}

fn tmp_path(parent: &Path, attempt: u32) -> PathBuf {
    parent.join(format!(
        ".smolvm-env-{}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos(),
        attempt
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TEMPFILE_COUNTER: AtomicUsize = AtomicUsize::new(0);

    #[test]
    fn shell_quote_round_trips_single_quotes_for_reader() {
        let mut vars = BTreeMap::new();
        vars.insert("TOKEN".to_string(), "a'b c".to_string());
        let script = build_env_script(&vars).unwrap();
        assert!(script.contains("export TOKEN='a'\\''b c'"));
        let parsed = parse_shell_value("'a'\\''b c'");
        assert_eq!(parsed, "a'b c");
        let parsed = parse_shell_value("'a'\"'\"'b c'");
        assert_eq!(parsed, "a'b c");
    }

    #[test]
    fn validates_posix_keys() {
        assert!(validate_key("OPENAI_API_KEY").is_ok());
        assert!(validate_key("1_BAD").is_err());
        assert!(validate_key("BAD-DASH").is_err());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn concurrent_merges_keep_all_variables() {
        let dir = tempfile_dir();
        let path = dir.join("env.sh");
        let mut handles = Vec::new();

        for index in 0..32 {
            let path = path.clone();
            handles.push(tokio::spawn(async move {
                let mut vars = BTreeMap::new();
                vars.insert(format!("KEY_{index}"), format!("value-{index}"));
                let response = put_managed_at(&path, EnvPutRequest { vars, merge: true });
                assert!(response.ok, "{:?}", response.error);
            }));
        }

        for handle in handles {
            handle.await.unwrap();
        }

        let vars = read_env_file(&path).unwrap();
        assert_eq!(vars.len(), 32);
        for index in 0..32 {
            assert_eq!(
                vars.get(&format!("KEY_{index}")),
                Some(&format!("value-{index}"))
            );
        }
    }

    fn tempfile_dir() -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "smolvm-env-test-{}-{}",
            std::process::id(),
            TEMPFILE_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        path
    }
}
