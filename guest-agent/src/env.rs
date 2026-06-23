//! Managed Linux environment file endpoints.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const ENV_FILE: &str = "/etc/profile.d/smolvm_env.sh";

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
    for key in req.vars.keys() {
        if let Err(error) = validate_key(key) {
            return error_response(error);
        }
    }
    let mut vars = if req.merge {
        match read_env_file(Path::new(ENV_FILE)) {
            Ok(vars) => vars,
            Err(error) => return error_response(error),
        }
    } else {
        BTreeMap::new()
    };
    vars.extend(req.vars);
    match atomic_write_env(Path::new(ENV_FILE), &vars) {
        Ok(()) => EnvResponse {
            ok: true,
            vars,
            error: None,
        },
        Err(error) => error_response(error),
    }
}

pub async fn delete_managed(req: EnvDeleteRequest) -> EnvResponse {
    for key in &req.keys {
        if let Err(error) = validate_key(key) {
            return error_response(error);
        }
    }
    let mut vars = match read_env_file(Path::new(ENV_FILE)) {
        Ok(vars) => vars,
        Err(error) => return error_response(error),
    };
    for key in req.keys {
        vars.remove(&key);
    }
    match atomic_write_env(Path::new(ENV_FILE), &vars) {
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
    let tmp = tmp_path(parent);
    fs::write(&tmp, content).map_err(|error| error.to_string())?;
    fs::set_permissions(&tmp, fs::Permissions::from_mode(0o644))
        .map_err(|error| error.to_string())?;
    fs::rename(&tmp, path).map_err(|error| {
        let _ = fs::remove_file(&tmp);
        error.to_string()
    })
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

fn tmp_path(parent: &Path) -> PathBuf {
    parent.join(format!(
        ".smolvm-env-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
