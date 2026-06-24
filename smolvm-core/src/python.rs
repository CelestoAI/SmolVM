//! PyO3 bindings for the private `smolvm_core._ffi` extension module.

use pyo3::prelude::*;

/// Return whether native Linux networking helpers are available.
#[pyfunction]
fn has_native_networking() -> bool {
    cfg!(target_os = "linux")
}

/// Return whether the native QMP client is available.
#[pyfunction]
fn has_native_qmp() -> bool {
    true
}

/// Return whether native host disk/image helpers are available.
#[pyfunction]
fn has_native_disk_io() -> bool {
    true
}

/// Return whether the Firecracker API client is available.
#[pyfunction]
fn has_native_firecracker_api() -> bool {
    cfg!(unix)
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn create_tap(name: &str, owner_uid: u32) -> PyResult<()> {
    crate::network::create_tap(name, owner_uid).map_err(crate::error::to_py_err)
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
    crate::network::delete_tap(name).map_err(crate::error::to_py_err)
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
    crate::network::set_link_up(name).map_err(crate::error::to_py_err)
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
    crate::network::flush_addrs(name).map_err(crate::error::to_py_err)
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
    crate::network::add_addr(name, ip, prefix_len).map_err(crate::error::to_py_err)
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
    crate::network::configure_tap(name, host_ip, prefix_len).map_err(crate::error::to_py_err)
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
#[pyo3(signature = (name, owner_uid, host_ip, prefix_len, route_localnet=true))]
fn prepare_tap(
    name: &str,
    owner_uid: u32,
    host_ip: &str,
    prefix_len: u8,
    route_localnet: bool,
) -> PyResult<()> {
    crate::network::prepare_tap(name, owner_uid, host_ip, prefix_len, route_localnet)
        .map_err(crate::error::to_py_err)
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

#[cfg(target_os = "linux")]
#[pyfunction]
fn add_route(dest: &str, prefix_len: u8, dev: &str) -> PyResult<()> {
    crate::network::add_route(dest, prefix_len, dev).map_err(crate::error::to_py_err)
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
    crate::network::get_default_interface().map_err(crate::error::to_py_err)
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
    crate::network::write_sysctl(key, value).map_err(crate::error::to_py_err)
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
    py.detach(|| crate::disk::clone_or_sparse_copy(source, target))
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
    py.detach(|| crate::disk::decompress_zstd_sparse(source, target, chunk_size))
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
}

#[cfg(unix)]
#[pyfunction]
fn firecracker_request(
    py: Python<'_>,
    socket_path: &str,
    method: &str,
    path: &str,
    body_json: Option<&str>,
    timeout: f64,
) -> PyResult<(u16, Option<String>)> {
    py.detach(|| crate::firecracker::request(socket_path, method, path, body_json, timeout))
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
}

#[cfg(not(unix))]
#[pyfunction]
fn firecracker_request(
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
fn firecracker_wait_for_socket(py: Python<'_>, socket_path: &str, timeout: f64) -> PyResult<()> {
    py.detach(|| crate::firecracker::wait_for_socket(socket_path, timeout))
        .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))
}

#[cfg(not(unix))]
#[pyfunction]
fn firecracker_wait_for_socket(
    _py: Python<'_>,
    _socket_path: &str,
    _timeout: f64,
) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err(
        "Not available on this platform",
    ))
}

/// Private Python extension module. Public Python code lives in wrapper modules.
#[pymodule]
fn _ffi(m: &Bound<'_, PyModule>) -> PyResult<()> {
    pyo3_log::init();

    m.add_function(wrap_pyfunction!(has_native_networking, m)?)?;
    m.add_function(wrap_pyfunction!(has_native_qmp, m)?)?;
    m.add_function(wrap_pyfunction!(has_native_disk_io, m)?)?;
    m.add_function(wrap_pyfunction!(has_native_firecracker_api, m)?)?;
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
    m.add_function(wrap_pyfunction!(firecracker_request, m)?)?;
    m.add_function(wrap_pyfunction!(firecracker_wait_for_socket, m)?)?;
    crate::qmp::register(m)?;

    Ok(())
}
