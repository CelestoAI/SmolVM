//! SmolVM native acceleration module.
//!
//! Provides fast network operations via direct kernel netlink API calls,
//! replacing subprocess calls to `ip`, `nft`, and `sysctl`.
//! Falls back gracefully on non-Linux platforms.

mod error;
#[cfg(target_os = "linux")]
mod route;
mod sysctl;
#[cfg(target_os = "linux")]
mod tap;

use pyo3::prelude::*;

/// Check if the native acceleration module is available and functional.
#[pyfunction]
fn is_available() -> bool {
    cfg!(target_os = "linux")
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn create_tap(name: &str, owner_uid: u32) -> PyResult<()> {
    tap::create(name, owner_uid).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn create_tap(_name: &str, _owner_uid: u32) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err("Not available on this platform"))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn delete_tap(name: &str) -> PyResult<()> {
    tap::delete(name).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn delete_tap(_name: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err("Not available on this platform"))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn set_link_up(name: &str) -> PyResult<()> {
    route::set_link_up(name).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn set_link_up(_name: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err("Not available on this platform"))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn flush_addrs(name: &str) -> PyResult<()> {
    route::flush_addrs(name).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn flush_addrs(_name: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err("Not available on this platform"))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn add_addr(name: &str, ip: &str, prefix_len: u8) -> PyResult<()> {
    route::add_addr(name, ip, prefix_len).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn add_addr(_name: &str, _ip: &str, _prefix_len: u8) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err("Not available on this platform"))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn add_route(dest: &str, prefix_len: u8, dev: &str) -> PyResult<()> {
    route::add_route(dest, prefix_len, dev).map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn add_route(_dest: &str, _prefix_len: u8, _dev: &str) -> PyResult<()> {
    Err(pyo3::exceptions::PyOSError::new_err("Not available on this platform"))
}

#[cfg(target_os = "linux")]
#[pyfunction]
fn get_default_interface() -> PyResult<String> {
    route::get_default_interface().map_err(error::to_py_err)
}

#[cfg(not(target_os = "linux"))]
#[pyfunction]
fn get_default_interface() -> PyResult<String> {
    Err(pyo3::exceptions::PyOSError::new_err("Not available on this platform"))
}

#[pyfunction]
fn write_sysctl(key: &str, value: &str) -> PyResult<()> {
    sysctl::write(key, value).map_err(error::to_py_err)
}

/// Python module definition.
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    pyo3_log::init();

    m.add_function(wrap_pyfunction!(is_available, m)?)?;
    m.add_function(wrap_pyfunction!(create_tap, m)?)?;
    m.add_function(wrap_pyfunction!(delete_tap, m)?)?;
    m.add_function(wrap_pyfunction!(set_link_up, m)?)?;
    m.add_function(wrap_pyfunction!(flush_addrs, m)?)?;
    m.add_function(wrap_pyfunction!(add_addr, m)?)?;
    m.add_function(wrap_pyfunction!(add_route, m)?)?;
    m.add_function(wrap_pyfunction!(get_default_interface, m)?)?;
    m.add_function(wrap_pyfunction!(write_sysctl, m)?)?;

    Ok(())
}
