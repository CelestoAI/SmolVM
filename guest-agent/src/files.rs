use base64::Engine;
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};

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

#[derive(Debug)]
pub struct RawFile {
    pub mode: u32,
    pub size: u64,
    pub data: Vec<u8>,
}

#[derive(Debug, Deserialize)]
pub struct FileRawPutQuery {
    pub path: String,
    pub name: Option<String>,
    pub mode: Option<u32>,
}

#[derive(Debug, Deserialize)]
pub struct DirectoryTarQuery {
    pub path: String,
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

fn write_file_atomically(target: &Path, mode: Option<u32>, data: &[u8]) -> FilePutResponse {
    match durable_atomic_write(target, mode, data, ".smolvm-put") {
        Ok(()) => FilePutResponse {
            ok: true,
            error: None,
        },
        Err(error) => response_error(error),
    }
}

fn durable_atomic_write(
    target: &Path,
    mode: Option<u32>,
    data: &[u8],
    prefix: &str,
) -> Result<(), String> {
    let parent = target.parent().unwrap_or_else(|| Path::new("/"));
    let (mut file, tmp_path) = create_temp_file(parent, prefix)?;

    if let Err(error) = file.write_all(data) {
        let _ = fs::remove_file(&tmp_path);
        return Err(format!("cannot write file: {error}"));
    }
    if let Some(mode) = mode {
        if let Err(error) = file.set_permissions(fs::Permissions::from_mode(mode)) {
            let _ = fs::remove_file(&tmp_path);
            return Err(format!("cannot set file mode: {error}"));
        }
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&tmp_path);
        return Err(format!("cannot sync file: {error}"));
    }
    drop(file);
    if let Err(error) = fs::rename(&tmp_path, target) {
        let _ = fs::remove_file(&tmp_path);
        return Err(format!("cannot replace file: {error}"));
    }
    sync_parent_dir(parent).map_err(|error| format!("cannot sync file directory: {error}"))
}

fn create_temp_file(parent: &Path, prefix: &str) -> Result<(File, PathBuf), String> {
    for attempt in 0..100 {
        let tmp_path = parent.join(format!(
            "{prefix}-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos(),
            attempt
        ));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp_path)
        {
            Ok(file) => return Ok((file, tmp_path)),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("cannot create temporary file: {error}")),
        }
    }
    Err("cannot create temporary file".to_string())
}

fn sync_parent_dir(parent: &Path) -> Result<(), std::io::Error> {
    File::open(parent).and_then(|file| file.sync_all())
}

pub fn put_file_bytes(
    path: &str,
    name: Option<&str>,
    mode: Option<u32>,
    data: &[u8],
) -> FilePutResponse {
    let target = match resolve_put_target(path, name) {
        Ok(target) => target,
        Err(error) => return response_error(error),
    };
    write_file_atomically(&target, mode, data)
}

pub async fn put_file(req: FilePutRequest) -> FilePutResponse {
    let data = match base64::engine::general_purpose::STANDARD.decode(req.data_base64.as_bytes()) {
        Ok(data) => data,
        Err(error) => return response_error(format!("invalid base64 data: {error}")),
    };
    put_file_bytes(&req.path, req.name.as_deref(), req.mode, &data)
}

pub fn read_file_bytes(path: &str) -> Result<RawFile, String> {
    if path.is_empty() {
        return Err("missing path".to_string());
    }
    let path = Path::new(path);
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) => return Err(error.to_string()),
    };
    if !metadata.is_file() {
        return Err("not a regular file".to_string());
    }
    let data = match fs::read(path) {
        Ok(data) => data,
        Err(error) => return Err(error.to_string()),
    };
    Ok(RawFile {
        mode: metadata.permissions().mode() & 0o777,
        size: data.len() as u64,
        data,
    })
}

pub async fn get_file(query: FileGetQuery) -> FileGetResponse {
    let file = match read_file_bytes(&query.path) {
        Ok(file) => file,
        Err(error) => return get_response_error(error),
    };
    FileGetResponse {
        ok: true,
        mode: Some(file.mode),
        size: Some(file.size),
        data_base64: Some(base64::engine::general_purpose::STANDARD.encode(file.data)),
        error: None,
    }
}

pub fn create_directory_tar(path: &str) -> Result<Vec<u8>, String> {
    if path.is_empty() {
        return Err("missing path".to_string());
    }
    let root = Path::new(path);
    let metadata = fs::metadata(root).map_err(|error| error.to_string())?;
    if !metadata.is_dir() {
        return Err("not a directory".to_string());
    }

    let mut out = Vec::new();
    append_directory_entries(root, root, &mut out)?;
    out.extend_from_slice(&[0u8; 1024]);
    Ok(out)
}

pub fn extract_directory_tar(path: &str, data: &[u8]) -> FilePutResponse {
    if path.is_empty() {
        return response_error("missing path");
    }
    let target = PathBuf::from(path);
    if let Err(error) = fs::create_dir_all(&target) {
        return response_error(format!("cannot create directory: {error}"));
    }
    if !target.is_dir() {
        return response_error("destination is not a directory");
    }

    match extract_tar_into(&target, data) {
        Ok(()) => FilePutResponse {
            ok: true,
            error: None,
        },
        Err(error) => response_error(error),
    }
}

fn append_directory_entries(root: &Path, current: &Path, out: &mut Vec<u8>) -> Result<(), String> {
    let mut entries = fs::read_dir(current)
        .map_err(|error| error.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| error.to_string())?;
    entries.sort_by_key(|entry| entry.file_name());

    for entry in entries {
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path).map_err(|error| error.to_string())?;
        let file_type = metadata.file_type();
        if file_type.is_symlink() {
            continue;
        }
        let relative = path
            .strip_prefix(root)
            .map_err(|error| error.to_string())?
            .to_string_lossy()
            .replace('\\', "/");
        if file_type.is_dir() {
            let entry_name = format!("{}/", relative.trim_end_matches('/'));
            append_tar_header(
                out,
                &entry_name,
                metadata.permissions().mode() & 0o777,
                0,
                b'5',
            )?;
            append_directory_entries(root, &path, out)?;
        } else if file_type.is_file() {
            let data = fs::read(&path).map_err(|error| error.to_string())?;
            append_tar_header(
                out,
                &relative,
                metadata.permissions().mode() & 0o777,
                data.len() as u64,
                b'0',
            )?;
            out.extend_from_slice(&data);
            pad_tar_entry(out, data.len() as u64);
        }
    }
    Ok(())
}

fn append_tar_header(
    out: &mut Vec<u8>,
    path: &str,
    mode: u32,
    size: u64,
    typeflag: u8,
) -> Result<(), String> {
    let mut header = [0u8; 512];
    let (name, prefix) = split_tar_path(path)?;
    write_bytes(&mut header[0..100], name.as_bytes());
    write_octal(&mut header[100..108], mode as u64);
    write_octal(&mut header[108..116], 0);
    write_octal(&mut header[116..124], 0);
    write_octal(&mut header[124..136], size);
    write_octal(&mut header[136..148], 0);
    for byte in &mut header[148..156] {
        *byte = b' ';
    }
    header[156] = typeflag;
    write_bytes(&mut header[257..263], b"ustar\0");
    write_bytes(&mut header[263..265], b"00");
    write_bytes(&mut header[345..500], prefix.as_bytes());
    let checksum = header.iter().map(|byte| *byte as u32).sum::<u32>();
    let checksum_text = format!("{checksum:06o}\0 ");
    write_bytes(&mut header[148..156], checksum_text.as_bytes());
    out.extend_from_slice(&header);
    Ok(())
}

fn split_tar_path(path: &str) -> Result<(String, String), String> {
    let path = path.trim_start_matches("./");
    let bytes = path.as_bytes();
    if bytes.len() <= 100 {
        return Ok((path.to_string(), String::new()));
    }
    for index in path.match_indices('/').map(|(index, _)| index).rev() {
        let prefix = &path[..index];
        let name = &path[index + 1..];
        if !name.is_empty() && prefix.as_bytes().len() <= 155 && name.as_bytes().len() <= 100 {
            return Ok((name.to_string(), prefix.to_string()));
        }
    }
    Err(format!("tar path is too long: {path}"))
}

fn write_bytes(target: &mut [u8], value: &[u8]) {
    let len = target.len().min(value.len());
    target[..len].copy_from_slice(&value[..len]);
}

fn write_octal(target: &mut [u8], value: u64) {
    let text = format!("{value:0width$o}\0", width = target.len() - 1);
    write_bytes(target, text.as_bytes());
}

fn pad_tar_entry(out: &mut Vec<u8>, size: u64) {
    let remainder = (size % 512) as usize;
    if remainder != 0 {
        out.extend(std::iter::repeat_n(0u8, 512 - remainder));
    }
}

fn extract_tar_into(root: &Path, archive: &[u8]) -> Result<(), String> {
    let mut offset = 0usize;
    while offset + 512 <= archive.len() {
        let header = &archive[offset..offset + 512];
        offset += 512;
        if header.iter().all(|byte| *byte == 0) {
            return Ok(());
        }

        let entry_name = parse_tar_path(header)?;
        let relative = validate_relative_archive_path(&entry_name)?;
        let mode = parse_octal(&header[100..108]).unwrap_or(0o644) as u32;
        let size = parse_octal(&header[124..136]).ok_or("invalid tar entry size")?;
        let typeflag = header[156];
        let size_usize = usize::try_from(size).map_err(|_| "tar entry is too large".to_string())?;
        if offset + size_usize > archive.len() {
            return Err("tar entry extends past archive end".to_string());
        }
        let target = root.join(&relative);

        match typeflag {
            b'5' => {
                create_safe_directory(root, &target)?;
                let _ = fs::set_permissions(&target, fs::Permissions::from_mode(mode & 0o777));
            }
            b'0' | 0 => {
                let parent = target
                    .parent()
                    .ok_or_else(|| "tar entry has no parent directory".to_string())?;
                create_safe_directory(root, parent)?;
                reject_symlink_path(root, &target)?;
                durable_atomic_write(
                    &target,
                    Some(mode & 0o777),
                    &archive[offset..offset + size_usize],
                    ".smolvm-tar",
                )?;
            }
            _ => return Err(format!("unsupported tar entry type: {}", typeflag as char)),
        }

        offset += size_usize;
        let remainder = offset % 512;
        if remainder != 0 {
            offset += 512 - remainder;
        }
    }

    Err("tar archive ended before end marker".to_string())
}

fn parse_tar_path(header: &[u8]) -> Result<String, String> {
    let name = parse_string_field(&header[0..100]);
    let prefix = parse_string_field(&header[345..500]);
    let path = if prefix.is_empty() {
        name
    } else {
        format!("{prefix}/{name}")
    };
    if path.is_empty() {
        Err("tar entry has an empty path".to_string())
    } else {
        Ok(path)
    }
}

fn parse_string_field(field: &[u8]) -> String {
    let len = field
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(field.len());
    String::from_utf8_lossy(&field[..len]).into_owned()
}

fn parse_octal(field: &[u8]) -> Option<u64> {
    let text = parse_string_field(field);
    let text = text.trim_matches(char::from(0)).trim();
    if text.is_empty() {
        return Some(0);
    }
    u64::from_str_radix(text, 8).ok()
}

fn validate_relative_archive_path(path: &str) -> Result<PathBuf, String> {
    let mut relative = PathBuf::new();
    for component in Path::new(path).components() {
        match component {
            Component::Normal(part) => relative.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(format!("tar entry path is not safe: {path}"));
            }
        }
    }
    if relative.as_os_str().is_empty() {
        return Err("tar entry has an empty path".to_string());
    }
    Ok(relative)
}

fn create_safe_directory(root: &Path, target: &Path) -> Result<(), String> {
    let relative = target.strip_prefix(root).map_err(|_| {
        format!(
            "tar entry escaped destination directory: {}",
            target.display()
        )
    })?;
    let mut cursor = root.to_path_buf();
    for component in relative.components() {
        cursor.push(component.as_os_str());
        match fs::symlink_metadata(&cursor) {
            Ok(metadata) => {
                if metadata.file_type().is_symlink() {
                    return Err(format!(
                        "tar entry would write through a symlink: {}",
                        cursor.display()
                    ));
                }
                if !metadata.is_dir() {
                    return Err(format!(
                        "tar entry path collides with a non-directory: {}",
                        cursor.display()
                    ));
                }
                continue;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("cannot inspect directory: {error}")),
        }
        if let Err(error) = fs::create_dir(&cursor) {
            return Err(format!("cannot create directory: {error}"));
        }
    }
    Ok(())
}

fn reject_symlink_path(root: &Path, target: &Path) -> Result<(), String> {
    let relative = target.strip_prefix(root).map_err(|_| {
        format!(
            "tar entry escaped destination directory: {}",
            target.display()
        )
    })?;
    let mut cursor = root.to_path_buf();
    for component in relative.components() {
        cursor.push(component.as_os_str());
        if fs::symlink_metadata(&cursor)
            .map(|metadata| metadata.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err(format!(
                "tar entry would write through a symlink: {}",
                cursor.display()
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TEMPFILE_COUNTER: AtomicUsize = AtomicUsize::new(0);

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

    #[tokio::test]
    async fn put_rejects_invalid_directory_filename() {
        let dir = tempfile_dir();
        let put = put_file(FilePutRequest {
            path: dir.to_string_lossy().to_string(),
            name: Some("..".to_string()),
            mode: None,
            data_base64: base64::engine::general_purpose::STANDARD.encode(b"data"),
        })
        .await;
        assert!(!put.ok);
        assert!(
            put.error
                .unwrap()
                .contains("destination filename is invalid")
        );
    }

    #[tokio::test]
    async fn put_rejects_invalid_base64() {
        let dir = tempfile_dir();
        let put = put_file(FilePutRequest {
            path: dir.join("bad.bin").to_string_lossy().to_string(),
            name: None,
            mode: None,
            data_base64: "%%%".to_string(),
        })
        .await;
        assert!(!put.ok);
        assert!(put.error.unwrap().contains("invalid base64 data"));
    }

    #[tokio::test]
    async fn get_rejects_missing_and_non_regular_paths() {
        let dir = tempfile_dir();
        let missing = get_file(FileGetQuery {
            path: dir.join("missing.bin").to_string_lossy().to_string(),
        })
        .await;
        assert!(!missing.ok);
        assert!(missing.error.unwrap().contains("No such file"));

        let directory = get_file(FileGetQuery {
            path: dir.to_string_lossy().to_string(),
        })
        .await;
        assert!(!directory.ok);
        assert_eq!(directory.error.as_deref(), Some("not a regular file"));
    }

    #[test]
    fn raw_file_helpers_put_and_read_bytes_without_base64() {
        let dir = tempfile_dir();
        let path = dir.join("payload.bin");

        let put = put_file_bytes(path.to_str().unwrap(), None, Some(0o600), b"raw payload");
        assert!(put.ok, "{:?}", put.error);

        let file = read_file_bytes(path.to_str().unwrap()).unwrap();
        assert_eq!(file.data, b"raw payload");
        assert_eq!(file.mode, 0o600);
        assert_eq!(file.size, 11);
    }

    #[test]
    fn directory_tar_round_trips_regular_files_and_modes() {
        let source = tempfile_dir();
        fs::create_dir(source.join("sub")).unwrap();
        fs::write(source.join("sub").join("note.txt"), b"hello").unwrap();
        fs::set_permissions(
            source.join("sub").join("note.txt"),
            fs::Permissions::from_mode(0o640),
        )
        .unwrap();

        let archive = create_directory_tar(source.to_str().unwrap()).unwrap();
        let target = tempfile_dir();
        let extract = extract_directory_tar(target.to_str().unwrap(), &archive);
        assert!(extract.ok, "{:?}", extract.error);

        let extracted = target.join("sub").join("note.txt");
        assert_eq!(fs::read(&extracted).unwrap(), b"hello");
        assert_eq!(
            fs::metadata(extracted).unwrap().permissions().mode() & 0o777,
            0o640
        );
    }

    #[test]
    fn directory_tar_rejects_path_traversal() {
        let target = tempfile_dir();
        let mut archive = Vec::new();
        append_tar_header(&mut archive, "../escape.txt", 0o644, 4, b'0').unwrap();
        archive.extend_from_slice(b"nope");
        pad_tar_entry(&mut archive, 4);
        archive.extend_from_slice(&[0u8; 1024]);

        let extract = extract_directory_tar(target.to_str().unwrap(), &archive);
        assert!(!extract.ok);
        assert!(extract.error.unwrap().contains("not safe"));
        assert!(!target.parent().unwrap().join("escape.txt").exists());
    }

    #[test]
    fn directory_tar_rejects_symlink_targets() {
        let target = tempfile_dir();
        fs::create_dir(target.join("safe")).unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink("/tmp", target.join("safe").join("link")).unwrap();

        let mut archive = Vec::new();
        append_tar_header(&mut archive, "safe/link/escape.txt", 0o644, 4, b'0').unwrap();
        archive.extend_from_slice(b"nope");
        pad_tar_entry(&mut archive, 4);
        archive.extend_from_slice(&[0u8; 1024]);

        let extract = extract_directory_tar(target.to_str().unwrap(), &archive);
        assert!(!extract.ok);
        assert!(extract.error.unwrap().contains("symlink"));
    }

    #[test]
    fn directory_tar_rejects_directory_collision_with_regular_file() {
        let target = tempfile_dir();
        fs::write(target.join("collision"), b"file").unwrap();

        let mut archive = Vec::new();
        append_tar_header(&mut archive, "collision", 0o755, 0, b'5').unwrap();
        archive.extend_from_slice(&[0u8; 1024]);

        let extract = extract_directory_tar(target.to_str().unwrap(), &archive);
        assert!(!extract.ok);
        assert!(extract.error.unwrap().contains("non-directory"));
    }

    fn tempfile_dir() -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "smolvm-agent-test-{}-{}",
            std::process::id(),
            TEMPFILE_COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        path
    }
}
