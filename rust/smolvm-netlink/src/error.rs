//! Error types that map to Python exceptions with kernel-compatible messages.

use pyo3::exceptions::PyOSError;
use pyo3::PyErr;

#[derive(Debug, thiserror::Error)]
pub enum NetlinkError {
    #[error("File exists")]
    AlreadyExists,

    #[error("Device or resource busy")]
    DeviceBusy,

    #[error("Cannot find device \"{0}\"")]
    DeviceNotFound(String),

    #[error("RTNETLINK answers: File exists")]
    RouteExists,

    #[error("RTNETLINK answers: No such device")]
    NoSuchDevice(String),

    #[error("{0}")]
    Io(#[from] std::io::Error),

    #[error("{0}")]
    Other(String),
}

impl NetlinkError {
    /// Create from an errno value with context.
    pub fn from_errno(errno: i32, context: &str) -> Self {
        match errno {
            libc::EEXIST => NetlinkError::AlreadyExists,
            libc::EBUSY => NetlinkError::DeviceBusy,
            libc::ENODEV | libc::ENXIO => NetlinkError::NoSuchDevice(context.to_string()),
            _ => NetlinkError::Other(format!("{}: errno {}", context, errno)),
        }
    }
}

/// Convert a NetlinkError to a Python exception.
pub fn to_py_err(e: NetlinkError) -> PyErr {
    PyOSError::new_err(e.to_string())
}
