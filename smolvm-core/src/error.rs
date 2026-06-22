//! Error types that map to Python exceptions with kernel-compatible messages.

use pyo3::PyErr;
use pyo3::exceptions::PyOSError;

#[derive(Debug, thiserror::Error)]
pub enum NetlinkError {
    #[error("File exists")]
    AlreadyExists,

    #[error("Device or resource busy")]
    DeviceBusy,

    #[error("Operation not permitted")]
    PermissionDenied,

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
            libc::EPERM => NetlinkError::PermissionDenied,
            libc::ENODEV | libc::ENXIO => NetlinkError::NoSuchDevice(context.to_string()),
            _ => NetlinkError::Other(format!("{}: errno {}", context, errno)),
        }
    }

    /// Normalize kernel/netlink permission failures to a stable Python message.
    pub fn from_kernel_message(context: &str, message: &str) -> Self {
        if is_permission_denied_message(message) {
            return NetlinkError::PermissionDenied;
        }
        NetlinkError::Other(format!("{}: {}", context, message))
    }
}

fn is_permission_denied_message(message: &str) -> bool {
    message.contains("Operation not permitted")
        || message.contains("errno 1")
        || message.contains("os error 1")
        || message.contains("code: -1")
        || message.contains("EPERM")
}

/// Convert a NetlinkError to a Python exception.
pub fn to_py_err(e: NetlinkError) -> PyErr {
    PyOSError::new_err(e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn from_errno_normalizes_eperm() {
        assert!(matches!(
            NetlinkError::from_errno(libc::EPERM, "tap0"),
            NetlinkError::PermissionDenied
        ));
        assert_eq!(
            NetlinkError::from_errno(libc::EPERM, "tap0").to_string(),
            "Operation not permitted"
        );
    }

    #[test]
    fn from_kernel_message_normalizes_netlink_eperm_shapes() {
        for message in [
            "Operation not permitted",
            "tap0: errno 1",
            "Permission denied (os error 1)",
            "NetlinkError { code: -1, message: None }",
            "EPERM",
        ] {
            assert!(matches!(
                NetlinkError::from_kernel_message("set_link_up tap0", message),
                NetlinkError::PermissionDenied
            ));
        }
    }

    #[test]
    fn from_kernel_message_keeps_context_for_other_errors() {
        let error = NetlinkError::from_kernel_message("set_link_up tap0", "No such device");

        assert_eq!(error.to_string(), "set_link_up tap0: No such device");
    }
}
