//! Native disk/image helpers for sparse VM images.

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const SPARSE_CHUNK_SIZE: usize = 1024 * 1024;

/// Copy a VM image using reflink when available, preserving sparse holes otherwise.
pub fn clone_or_sparse_copy(source: &str, target: &str) -> io::Result<String> {
    clone_or_sparse_copy_impl(Path::new(source), Path::new(target))
}

/// Decompress a zstd-compressed VM image while preserving all-zero sparse ranges.
pub fn decompress_zstd_sparse(source: &str, target: &str, chunk_size: usize) -> io::Result<String> {
    decompress_zstd_sparse_impl(Path::new(source), Path::new(target), chunk_size)?;
    Ok("sparse".to_string())
}

fn clone_or_sparse_copy_impl(source_path: &Path, target_path: &Path) -> io::Result<String> {
    let metadata = fs::metadata(source_path)?;
    if !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("source is not a regular file: {}", source_path.display()),
        ));
    }

    ensure_parent_dir(target_path)?;
    let tmp_path = unique_tmp_path(target_path)?;
    let result = (|| {
        let method = if reflink_copy(source_path, &tmp_path).is_ok() {
            "reflink"
        } else {
            sparse_copy(source_path, &tmp_path, metadata.len())?;
            "sparse"
        };
        fs::set_permissions(&tmp_path, metadata.permissions())?;
        fs::rename(&tmp_path, target_path)?;
        Ok(method.to_string())
    })();

    if result.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    result
}

fn decompress_zstd_sparse_impl(
    source_path: &Path,
    target_path: &Path,
    chunk_size: usize,
) -> io::Result<()> {
    if chunk_size == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "chunk_size must be greater than zero",
        ));
    }

    ensure_parent_dir(target_path)?;
    let tmp_path = sibling_tmp_path(target_path, ".tmp")?;
    let result = (|| {
        let source = File::open(source_path)?;
        let mut decoder = zstd::stream::read::Decoder::new(source)?;
        let mut target = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp_path)?;

        let mut size = 0_u64;
        let mut buffer = vec![0_u8; chunk_size];
        loop {
            let read = decoder.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            size += read as u64;
            write_sparse_chunk(&mut target, &buffer[..read])?;
        }
        target.set_len(size)?;
        fs::rename(&tmp_path, target_path)?;
        Ok(())
    })();

    if result.is_err() {
        let _ = fs::remove_file(&tmp_path);
    }
    result
}

#[cfg(target_os = "linux")]
fn reflink_copy(source_path: &Path, target_path: &Path) -> io::Result<()> {
    use std::os::fd::AsRawFd;

    let source = File::open(source_path)?;
    let target = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target_path)?;

    let rc = unsafe { libc::ioctl(target.as_raw_fd(), libc::FICLONE, source.as_raw_fd()) };
    if rc == 0 {
        Ok(())
    } else {
        let error = io::Error::last_os_error();
        drop(target);
        let _ = fs::remove_file(target_path);
        Err(error)
    }
}

#[cfg(not(target_os = "linux"))]
fn reflink_copy(_source_path: &Path, _target_path: &Path) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "reflink copy is not available on this platform",
    ))
}

fn sparse_copy(source_path: &Path, target_path: &Path, size: u64) -> io::Result<()> {
    let mut source = File::open(source_path)?;
    let mut target = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(target_path)?;

    let mut buffer = vec![0_u8; SPARSE_CHUNK_SIZE];
    loop {
        let read = source.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        write_sparse_chunk(&mut target, &buffer[..read])?;
    }
    target.set_len(size)?;
    Ok(())
}

fn write_sparse_chunk(target: &mut File, chunk: &[u8]) -> io::Result<()> {
    if chunk.iter().all(|byte| *byte == 0) {
        target.seek(SeekFrom::Current(chunk.len() as i64))?;
    } else {
        target.write_all(chunk)?;
    }
    Ok(())
}

fn ensure_parent_dir(path: &Path) -> io::Result<()> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}

fn unique_tmp_path(target_path: &Path) -> io::Result<PathBuf> {
    let suffix = format!(
        ".tmp-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    );
    sibling_tmp_path(target_path, &suffix)
}

fn sibling_tmp_path(target_path: &Path, suffix: &str) -> io::Result<PathBuf> {
    let file_name = target_path.file_name().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("target path has no filename: {}", target_path.display()),
        )
    })?;
    let mut tmp_name = OsString::from(file_name);
    tmp_name.push(suffix);
    Ok(target_path.with_file_name(tmp_name))
}
