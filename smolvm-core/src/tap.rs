//! TAP device creation and deletion via ioctl + netlink.

use crate::error::NetlinkError;
use std::os::fd::AsRawFd;

/// Maximum retry attempts for EBUSY on TUNSETPERSIST.
const MAX_BUSY_RETRIES: u32 = 3;

/// Create a TAP device with the given name and owner UID.
///
/// Retries on EBUSY from TUNSETPERSIST up to MAX_BUSY_RETRIES times, because the
/// kernel briefly holds the device between TUNSETIFF and TUNSETPERSIST, especially
/// under load or after rapid VM creation/deletion cycles. This mirrors the retry
/// logic already present in the Python fallback (src/smolvm/host/network.py).
pub fn create(name: &str, owner_uid: u32) -> Result<(), NetlinkError> {
    let mut backoff_ms: u64 = 100;

    for attempt in 0..=MAX_BUSY_RETRIES {
        if attempt > 0 {
            std::thread::sleep(std::time::Duration::from_millis(backoff_ms));
            backoff_ms *= 2; // exponential backoff
        }

        if let Err(e) = create_once(name, owner_uid) {
            // Only retry on EBUSY at the TUNSETPERSIST step
            if is_tunsetpersist_busy(&e) && attempt < MAX_BUSY_RETRIES {
                log::warn!(
                    "TAP {} busy during creation (attempt {}/{}), retrying...",
                    name,
                    attempt + 1,
                    MAX_BUSY_RETRIES + 1
                );
                continue;
            }
            return Err(e);
        }
        return Ok(());
    }

    // Should not be reached, but defensively return last error
    create_once(name, owner_uid)
}

/// Create, configure, and enable localhost routing for a TAP in one native call.
pub fn prepare(
    name: &str,
    owner_uid: u32,
    host_ip: &str,
    prefix_len: u8,
) -> Result<(), NetlinkError> {
    crate::route::validate_tap_config(host_ip, prefix_len)?;

    match create(name, owner_uid) {
        Ok(()) | Err(NetlinkError::AlreadyExists) => {}
        Err(error) => return Err(error),
    }

    crate::route::configure_tap(name, host_ip, prefix_len)?;
    crate::sysctl::write_tap_route_localnet(name)?;
    Ok(())
}

fn is_tunsetpersist_busy(error: &NetlinkError) -> bool {
    matches!(
        error,
        NetlinkError::Other(msg)
            if msg.starts_with("TUNSETPERSIST ") && msg.contains("Device or resource busy")
    )
}

/// Perform a single TAP creation attempt (TUNSETIFF + TUNSETOWNER + TUNSETPERSIST).
fn create_once(name: &str, owner_uid: u32) -> Result<(), NetlinkError> {
    let fd = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/net/tun")
        .map_err(NetlinkError::Io)?;

    let mut ifr: libc::ifreq = unsafe { std::mem::zeroed() };

    // Set device name
    let name_bytes = name.as_bytes();
    if name_bytes.len() >= libc::IFNAMSIZ {
        return Err(NetlinkError::Other(format!(
            "TAP name too long: {} (max {})",
            name,
            libc::IFNAMSIZ - 1
        )));
    }
    unsafe {
        std::ptr::copy_nonoverlapping(
            name_bytes.as_ptr(),
            ifr.ifr_name.as_mut_ptr() as *mut u8,
            name_bytes.len(),
        );
    }

    // IFF_TAP | IFF_NO_PI
    ifr.ifr_ifru.ifru_flags = (libc::IFF_TAP | libc::IFF_NO_PI) as i16;

    // TUNSETIFF
    let ret = unsafe { libc::ioctl(fd.as_raw_fd(), 0x400454CA_u64, &ifr) };
    if ret < 0 {
        let errno = unsafe { *libc::__errno_location() };
        return Err(NetlinkError::from_errno(errno, name));
    }

    // TUNSETOWNER
    let ret = unsafe { libc::ioctl(fd.as_raw_fd(), 0x400454CC_u64, owner_uid as libc::c_ulong) };
    if ret < 0 {
        let errno = unsafe { *libc::__errno_location() };
        return Err(NetlinkError::from_errno(
            errno,
            &format!("TUNSETOWNER {}", name),
        ));
    }

    // TUNSETPERSIST
    let ret = unsafe { libc::ioctl(fd.as_raw_fd(), 0x400454CB_u64, 1 as libc::c_int) };
    if ret < 0 {
        let errno = unsafe { *libc::__errno_location() };
        let msg = format!(
            "TUNSETPERSIST {}: {}",
            name,
            NetlinkError::from_errno(errno, "")
        );
        return Err(NetlinkError::Other(msg));
    }

    log::debug!("TAP {} created (owner UID {})", name, owner_uid);
    Ok(())
}

/// Delete a TAP device via netlink.
pub fn delete(name: &str) -> Result<(), NetlinkError> {
    use crate::route::with_netlink;

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink connection failed: {}", e)))?;
        tokio::spawn(connection);

        // Find the link index
        let mut links = handle.link().get().match_name(name.to_string()).execute();
        use futures_util::TryStreamExt;
        let link = links
            .try_next()
            .await
            .map_err(|e| NetlinkError::Other(format!("Failed to find device {}: {}", name, e)))?;

        if let Some(link) = link {
            let index = link.header.index;
            handle
                .link()
                .del(index)
                .execute()
                .await
                .map_err(|e| NetlinkError::Other(format!("Failed to delete {}: {}", name, e)))?;
            log::debug!("TAP {} deleted", name);
        } else {
            return Err(NetlinkError::DeviceNotFound(name.to_string()));
        }

        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_tunsetpersist_busy_error() {
        let error = NetlinkError::Other("TUNSETPERSIST tap0: Device or resource busy".to_string());

        assert!(is_tunsetpersist_busy(&error));
    }

    #[test]
    fn ignores_non_tunsetpersist_busy_errors() {
        let owner_error =
            NetlinkError::Other("TUNSETOWNER tap0: Device or resource busy".to_string());
        let generic_busy = NetlinkError::DeviceBusy;
        let persist_other = NetlinkError::Other("TUNSETPERSIST tap0: errno 22".to_string());

        assert!(!is_tunsetpersist_busy(&owner_error));
        assert!(!is_tunsetpersist_busy(&generic_busy));
        assert!(!is_tunsetpersist_busy(&persist_other));
    }

    #[test]
    fn validate_prepare_inputs_before_tap_creation() {
        let error = prepare("tap0", 0, "not-an-ip", 32).unwrap_err();

        assert!(error.to_string().contains("invalid host IP not-an-ip"));
    }
}
