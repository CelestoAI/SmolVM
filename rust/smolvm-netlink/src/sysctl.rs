//! sysctl via direct /proc/sys writes.

use crate::error::NetlinkError;
use std::fs;
use std::path::PathBuf;

/// Write a sysctl value via /proc/sys.
///
/// Key uses dot notation (e.g., "net.ipv4.ip_forward") which is
/// converted to path notation (/proc/sys/net/ipv4/ip_forward).
pub fn write(key: &str, value: &str) -> Result<(), NetlinkError> {
    let path: PathBuf = ["/proc/sys"]
        .iter()
        .collect::<PathBuf>()
        .join(key.replace('.', "/"));

    fs::write(&path, value).map_err(|e| {
        NetlinkError::Other(format!("sysctl write {} = {}: {}", key, value, e))
    })?;

    log::debug!("sysctl {} = {}", key, value);
    Ok(())
}
