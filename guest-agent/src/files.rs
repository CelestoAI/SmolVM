use base64::Engine;
use serde::{Deserialize, Serialize};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

#[derive(Debug, Deserialize)]
pub struct FilePutRequest {
    pub path: String,
    pub name: Option<String>,
    pub mode: Option<u32>,
    pub data_base64: String,
}

#[derive(Debug, Serialize)]
pub struct FilePutResponse {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct FileGetQuery {
    pub path: String,
}

#[derive(Debug, Serialize)]
pub struct FileGetResponse {
    pub ok: bool,
    pub mode: Option<u32>,
    pub size: Option<u64>,
    pub data_base64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

fn response_error(error: impl Into<String>) -> FilePutResponse {
    FilePutResponse {
        ok: false,
        error: Some(error.into()),
    }
}

fn get_response_error(error: impl Into<String>) -> FileGetResponse {
    FileGetResponse {
        ok: false,
        mode: None,
        size: None,
        data_base64: None,
        error: Some(error.into()),
    }
}

fn resolve_put_target(path: &str, name: Option<&str>) -> Result<PathBuf, String> {
    if path.is_empty() {
        return Err("missing path".to_string());
    }
    let target = PathBuf::from(path);
    if target.is_dir() {
        let name = name.ok_or_else(|| {
            format!("destination is a directory and no filename was provided: {path}")
        })?;
        let base = Path::new(name)
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(|| format!("destination filename is invalid: {name}"))?;
        if base.is_empty() || base == "." || base == ".." {
            return Err(format!(
                "destination filename is invalid: {name} (cannot be '.' or '..')"
            ));
        }
        return Ok(target.join(base));
    }
    Ok(target)
}

pub async fn put_file(req: FilePutRequest) -> FilePutResponse {
    let target = match resolve_put_target(&req.path, req.name.as_deref()) {
        Ok(target) => target,
        Err(error) => return response_error(error),
    };
    let data = match base64::engine::general_purpose::STANDARD.decode(req.data_base64.as_bytes()) {
        Ok(data) => data,
        Err(error) => return response_error(format!("invalid base64 data: {error}")),
    };

    let parent = target.parent().unwrap_or_else(|| Path::new("/"));
    let tmp_name = format!(
        ".smolvm-put-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    );
    let tmp_path = parent.join(tmp_name);

    if let Err(error) = fs::write(&tmp_path, data) {
        return response_error(format!("cannot write file: {error}"));
    }
    if let Some(mode) = req.mode {
        if let Err(error) = fs::set_permissions(&tmp_path, fs::Permissions::from_mode(mode)) {
            let _ = fs::remove_file(&tmp_path);
            return response_error(format!("cannot set file mode: {error}"));
        }
    }
    if let Err(error) = fs::rename(&tmp_path, &target) {
        let _ = fs::remove_file(&tmp_path);
        return response_error(format!("cannot replace file: {error}"));
    }

    FilePutResponse {
        ok: true,
        error: None,
    }
}

pub async fn get_file(query: FileGetQuery) -> FileGetResponse {
    if query.path.is_empty() {
        return get_response_error("missing path");
    }
    let path = Path::new(&query.path);
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) => return get_response_error(error.to_string()),
    };
    if !metadata.is_file() {
        return get_response_error("not a regular file");
    }
    let data = match fs::read(path) {
        Ok(data) => data,
        Err(error) => return get_response_error(error.to_string()),
    };
    FileGetResponse {
        ok: true,
        mode: Some(metadata.permissions().mode() & 0o777),
        size: Some(data.len() as u64),
        data_base64: Some(base64::engine::general_purpose::STANDARD.encode(data)),
        error: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[tokio::test]
    async fn put_and_get_file_preserves_mode() {
        let dir = tempfile_dir();
        let path = dir.join("payload.bin");
        let data = b"hello world";

        let put = put_file(FilePutRequest {
            path: path.to_string_lossy().to_string(),
            name: None,
            mode: Some(0o640),
            data_base64: base64::engine::general_purpose::STANDARD.encode(data),
        })
        .await;
        assert!(put.ok, "{:?}", put.error);

        let get = get_file(FileGetQuery {
            path: path.to_string_lossy().to_string(),
        })
        .await;
        assert!(get.ok, "{:?}", get.error);
        assert_eq!(get.mode, Some(0o640));
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(get.data_base64.unwrap())
                .unwrap(),
            data
        );
    }

    #[tokio::test]
    async fn put_into_directory_strips_traversal() {
        let dir = tempfile_dir();
        let put = put_file(FilePutRequest {
            path: dir.to_string_lossy().to_string(),
            name: Some("../escape.txt".to_string()),
            mode: None,
            data_base64: base64::engine::general_purpose::STANDARD.encode(b"safe"),
        })
        .await;
        assert!(put.ok, "{:?}", put.error);
        assert_eq!(fs::read(dir.join("escape.txt")).unwrap(), b"safe");
        assert!(!dir.parent().unwrap().join("escape.txt").exists());
    }

    #[tokio::test]
    async fn put_into_directory_rejects_missing_name() {
        let dir = tempfile_dir();
        let put = put_file(FilePutRequest {
            path: dir.to_string_lossy().to_string(),
            name: None,
            mode: None,
            data_base64: base64::engine::general_purpose::STANDARD.encode(b"data"),
        })
        .await;
        assert!(!put.ok);
        assert!(put.error.unwrap().contains("directory"));
    }

    fn tempfile_dir() -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "smolvm-agent-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        fs::create_dir(&path).unwrap();
        path
    }
}
