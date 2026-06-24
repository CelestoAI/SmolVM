//! Linux networking helpers for TAP devices, routes, and sysctls.

use crate::error::NetlinkError;

/// Return whether native Linux networking helpers are available in this build.
pub fn available() -> bool {
    cfg!(target_os = "linux")
}

/// Create a TAP device owned by the given uid.
pub fn create_tap(name: &str, owner_uid: u32) -> Result<(), NetlinkError> {
    crate::tap::create(name, owner_uid)
}

/// Delete a TAP device.
pub fn delete_tap(name: &str) -> Result<(), NetlinkError> {
    crate::tap::delete(name)
}

/// Bring a network link up.
pub fn set_link_up(name: &str) -> Result<(), NetlinkError> {
    crate::route::set_link_up(name)
}

/// Remove all addresses from a network link.
pub fn flush_addrs(name: &str) -> Result<(), NetlinkError> {
    crate::route::flush_addrs(name)
}

/// Add an IPv4 address to a network link.
pub fn add_addr(name: &str, ip: &str, prefix_len: u8) -> Result<(), NetlinkError> {
    crate::route::add_addr(name, ip, prefix_len)
}

/// Flush addresses, add the host IP, and bring the TAP link up.
pub fn configure_tap(name: &str, host_ip: &str, prefix_len: u8) -> Result<(), NetlinkError> {
    crate::route::configure_tap(name, host_ip, prefix_len)
}

/// Create and configure a TAP link in one native operation.
pub fn prepare_tap(
    name: &str,
    owner_uid: u32,
    host_ip: &str,
    prefix_len: u8,
    route_localnet: bool,
) -> Result<(), NetlinkError> {
    if route_localnet {
        return crate::tap::prepare(name, owner_uid, host_ip, prefix_len);
    }
    crate::route::validate_tap_config(host_ip, prefix_len)?;
    match crate::tap::create(name, owner_uid) {
        Ok(()) | Err(NetlinkError::AlreadyExists) => {}
        Err(error) => return Err(error),
    }
    crate::route::configure_tap(name, host_ip, prefix_len)
}

/// Add a route through a network link.
pub fn add_route(dest: &str, prefix_len: u8, dev: &str) -> Result<(), NetlinkError> {
    crate::route::add_route(dest, prefix_len, dev)
}

/// Return the default outbound network interface name.
pub fn get_default_interface() -> Result<String, NetlinkError> {
    crate::route::get_default_interface()
}

/// Write a Linux sysctl key using dot notation.
pub fn write_sysctl(key: &str, value: &str) -> Result<(), NetlinkError> {
    crate::sysctl::write(key, value)
}
