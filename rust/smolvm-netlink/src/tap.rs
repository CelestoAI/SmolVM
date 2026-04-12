//! TAP device creation and deletion via ioctl + netlink.

use crate::error::NetlinkError;
use std::ffi::CString;
use std::os::fd::AsRawFd;

/// Create a TAP device with the given name and owner UID.
pub fn create(name: &str, owner_uid: u32) -> Result<(), NetlinkError> {
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
        return Err(NetlinkError::from_errno(errno, &format!("TUNSETOWNER {}", name)));
    }

    // TUNSETPERSIST
    let ret = unsafe { libc::ioctl(fd.as_raw_fd(), 0x400454CB_u64, 1 as libc::c_int) };
    if ret < 0 {
        let errno = unsafe { *libc::__errno_location() };
        return Err(NetlinkError::from_errno(errno, &format!("TUNSETPERSIST {}", name)));
    }

    log::debug!("TAP {} created (owner UID {})", name, owner_uid);
    Ok(())
}

/// Delete a TAP device via netlink.
pub fn delete(name: &str) -> Result<(), NetlinkError> {
    use crate::route::{runtime, with_netlink};

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection().map_err(|e| {
            NetlinkError::Other(format!("netlink connection failed: {}", e))
        })?;
        tokio::spawn(connection);

        // Find the link index
        let mut links = handle.link().get().match_name(name.to_string()).execute();
        use futures_util::TryStreamExt;
        let link = links.try_next().await.map_err(|e| {
            NetlinkError::Other(format!("Failed to find device {}: {}", name, e))
        })?;

        if let Some(link) = link {
            let index = link.header.index;
            handle.link().del(index).execute().await.map_err(|e| {
                NetlinkError::Other(format!("Failed to delete {}: {}", name, e))
            })?;
            log::debug!("TAP {} deleted", name);
        } else {
            return Err(NetlinkError::DeviceNotFound(name.to_string()));
        }

        Ok(())
    })
}
