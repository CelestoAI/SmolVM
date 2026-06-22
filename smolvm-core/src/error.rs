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
        || contains_number_after_marker(message, "errno", libc::EPERM)
        || contains_number_after_marker(message, "os error", libc::EPERM)
        || contains_number_after_marker(message, "code:", -libc::EPERM)
        || contains_word(message, "EPERM")
}

fn contains_number_after_marker(message: &str, marker: &str, expected: i32) -> bool {
    let mut search_start = 0;
    while let Some(relative_start) = message[search_start..].find(marker) {
        let marker_start = search_start + relative_start;
        let number_start = search_start + relative_start + marker.len();
        if !is_marker_boundary(message, marker_start) {
            search_start = number_start;
            continue;
        }
        let after_marker = &message[number_start..];
        if !marker.ends_with(':')
            && !after_marker
                .chars()
                .next()
                .is_some_and(|ch| ch.is_ascii_whitespace())
        {
            search_start = number_start;
            continue;
        }
        let trimmed = after_marker.trim_start();
        if let Some((value, consumed)) = parse_i32_prefix(trimmed) {
            if value == expected && is_number_boundary(trimmed, consumed) {
                return true;
            }
        }
        search_start = number_start;
    }
    false
}

fn is_marker_boundary(message: &str, marker_start: usize) -> bool {
    message[..marker_start]
        .chars()
        .next_back()
        .is_none_or(|ch| !ch.is_ascii_alphanumeric())
}

fn parse_i32_prefix(input: &str) -> Option<(i32, usize)> {
    let bytes = input.as_bytes();
    let mut end = 0;
    if matches!(bytes.first(), Some(b'-' | b'+')) {
        end = 1;
    }
    let digit_start = end;
    while end < bytes.len() && bytes[end].is_ascii_digit() {
        end += 1;
    }
    if end == digit_start {
        return None;
    }
    input[..end].parse().ok().map(|value| (value, end))
}

fn is_number_boundary(input: &str, consumed: usize) -> bool {
    input[consumed..]
        .chars()
        .next()
        .is_none_or(|ch| !ch.is_ascii_alphanumeric())
}

fn contains_word(message: &str, word: &str) -> bool {
    message
        .split(|ch: char| !ch.is_ascii_alphanumeric() && ch != '_')
        .any(|part| part == word)
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

    #[test]
    fn from_kernel_message_does_not_overmatch_other_errno_values() {
        for message in [
            "tap0: errno 13",
            "tap0: errno 100",
            "tap0: errno1",
            "Permission denied (os error 13)",
            "NetlinkError { code: -17, message: None }",
            "NetlinkError { postcode: -1, message: None }",
            "NOEPERM",
        ] {
            let error = NetlinkError::from_kernel_message("set_link_up tap0", message);

            assert!(matches!(error, NetlinkError::Other(_)), "{message}");
        }
    }
}
