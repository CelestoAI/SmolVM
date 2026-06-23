//! SmolVM native acceleration module.
//!
//! Provides fast network operations via direct kernel netlink API calls,
//! replacing subprocess calls to `ip`, `nft`, and `sysctl`, plus a private
//! QMP accelerator used by the Python `smolvm.qmp.QMPClient` wrapper, and
//! disk/image helpers for sparse VM rootfs files.
//! Network helpers fall back gracefully on non-Linux platforms.

mod disk;
#[cfg(target_os = "linux")]
mod error;
#[cfg(unix)]
mod firecracker;
mod qmp;
#[cfg(target_os = "linux")]
mod route;
#[cfg(target_os = "linux")]
mod sysctl;
#[cfg(target_os = "linux")]
mod tap;

use pyo3::prelude::*;

/// Return whether native Linux networking helpers are available.
#[pyfunction]
fn has_native_networking() -> bool {
    cfg!(target_os = "linux")
}

/// Return whether the private native QMP accelerator is available.
#[pyfunction]
fn has_native_qmp() -> bool {
    true
}

/// Return whether native host disk/image helpers are available.
#[pyfunction]
fn has_native_disk_io() -> bool {
    true
}

/// Return whether the private Firecracker API accelerator is available.
#[pyfunction]
fn has_native_firecracker_api() -> bool {
    cfg!(unix)
}

/// Backward-compatible alias for has_native_networking().
#[pyfunction]
fn is_available() -> bool {
    has_native_networking()
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn create_tap(name: &str, owner_uid: u32) -> PyResult<()> {
    tap::create(name, owner_uid).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn create_tap(_name: &str, _owner_uid: u32) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn delete_tap(name: &str) -> PyResult<()> {
    tap::delete(name).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn delete_tap(_name: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn set_link_up(name: &str) -> PyResult<()> {
    route::set_link_up(name).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn set_link_up(_name: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn flush_addrs(name: &str) -> PyResult<()> {
    route::flush_addrs(name).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn flush_addrs(_name: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn add_addr(name: &str, ip: &str, prefix_len: u8) -> PyResult<()> {
    route::add_addr(name, ip, prefix_len).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn add_addr(_name: &str, _ip: &str, _prefix_len: u8) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn configure_tap(name: &str, host_ip: &str, prefix_len: u8) -> PyResult<()> {
    route::configure_tap(name, host_ip, prefix_len).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn configure_tap(_name: &str, _host_ip: &str, _prefix_len: u8) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn add_route(dest: &str, prefix_len: u8, dev: &str) -> PyResult<()> {
    route::add_route(dest, prefix_len, dev).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn add_route(_dest: &str, _prefix_len: u8, _dev: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn get_default_interface() -> PyResult<String> {
    route::get_default_interface().map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn get_default_interface() -> PyResult<String> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn write_sysctl(key: &str, value: &str) -> PyResult<()> {
    sysctl::write(key, value).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn write_sysctl(_key: &str, _value: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[pyfunction]
fn clone_or_sparse_copy(py: Python<'_>, source: &str, target: &str) -> PyResult<String> {
    py.detach(|| disk::clone_or_sparse_copy(source, target))
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
}

#[pyfunction]
#[pyo3(signature = (source, target, chunk_size=1048576))]
fn decompress_zstd_sparse(
    py: Python<'_>,
    source: &str,
    target: &str,
    chunk_size: usize,
) -> PyResult<String> {
    py.detach(|| disk::decompress_zstd_sparse(source, target, chunk_size))
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
}

#[cfg(target_os = "linux")]
#[pyfunction]
#[pyo3(signature = (name, owner_uid, host_ip, prefix_len, route_localnet=true))]
fn prepare_tap(
    name: &str,
    owner_uid: u32,
    host_ip: &str,
    prefix_len: u8,
    route_localnet: bool,
) -> PyResult<()> {
    if route_localnet {
        return tap::prepare(name, owner_uid, host_ip, prefix_len).map_err(error::to_py_err);
    }
    route::validate_tap_config(host_ip, prefix_len).map_err(error::to_py_err)?;
    match tap::create(name, owner_uid) {
        Ok(()) | Err(error::NetlinkError::AlreadyExists) => {}
        Err(err) => return Err(error::to_py_err(err)),
    }
    route::configure_tap(name, host_ip, prefix_len).map_err(error::to_py_err)?;
    Ok(())
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
#[pyo3(signature = (_name, _owner_uid, _host_ip, _prefix_len, _route_localnet=true))]
fn prepare_tap(
    _name: &str,
    _owner_uid: u32,
    _host_ip: &str,
    _prefix_len: u8,
    _route_localnet: bool,
) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(unix)]
#[pyfunction]
fn _firecracker_request(
    py: Python<'_>,
    socket_path: &str,
    method: &str,
    path: &str,
    body_json: Option<&str>,
    timeout: f64,
) -> PyResult<(u16, Option<String>)> {
    py.detach(|| firecracker::request(socket_path, method, path, body_json, timeout))
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
}

#[cfg(not(unix))]
#[pyfunction]
fn _firecracker_request(
    _py: Python<'_>,
    _socket_path: &str,
    _method: &str,
    _path: &str,
    _body_json: Option<&str>,
    _timeout: f64,
) -> PyResult<(u16, Option<String>)> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

#[cfg(unix)]
#[pyfunction]
fn _firecracker_wait_for_socket(py: Python<'_>, socket_path: &str, timeout: f64) -> PyResult<()> {
    py.detach(|| firecracker::wait_for_socket(socket_path, timeout))
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
}

#[cfg(not(unix))]
#[pyfunction]
fn _firecracker_wait_for_socket(
    _py: Python<'_>,
    _socket_path: &str,
    _timeout: f64,
) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

/// Python module definition.
#[pymodule]
fn _smolvm_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    pyo3_log::init();

    m.add_function(wrap_pyfunction!(has_native_networking, m)?)?;
    m.add_function(wrap_pyfunction!(has_native_qmp, m)?)?;
    m.add_function(wrap_pyfunction!(has_native_disk_io, m)?)?;
    m.add_function(wrap_pyfunction!(has_native_firecracker_api, m)?)?;
    m.add_function(wrap_pyfunction!(is_available, m)?)?;
    m.add_function(wrap_pyfunction!(create_tap, m)?)?;
    m.add_function(wrap_pyfunction!(delete_tap, m)?)?;
    m.add_function(wrap_pyfunction!(set_link_up, m)?)?;
    m.add_function(wrap_pyfunction!(flush_addrs, m)?)?;
    m.add_function(wrap_pyfunction!(add_addr, m)?)?;
    m.add_function(wrap_pyfunction!(configure_tap, m)?)?;
    m.add_function(wrap_pyfunction!(prepare_tap, m)?)?;
    m.add_function(wrap_pyfunction!(add_route, m)?)?;
    m.add_function(wrap_pyfunction!(get_default_interface, m)?)?;
    m.add_function(wrap_pyfunction!(write_sysctl, m)?)?;
    m.add_function(wrap_pyfunction!(clone_or_sparse_copy, m)?)?;
    m.add_function(wrap_pyfunction!(decompress_zstd_sparse, m)?)?;
    m.add_function(wrap_pyfunction!(_firecracker_request, m)?)?;
    m.add_function(wrap_pyfunction!(_firecracker_wait_for_socket, m)?)?;
    qmp::register(m)?;

    Ok(())
}
