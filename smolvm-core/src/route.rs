//! IP address, link, and route management via rtnetlink.

use crate::error::NetlinkError;
use std::net::Ipv4Addr;
use std::sync::OnceLock;
use tokio::runtime::Runtime;

/// Shared tokio runtime for all netlink operations.
/// Using a dedicated single-threaded runtime to avoid nested runtime issues.
pub fn runtime() -> &'static Runtime {
    static RT: OnceLock<Runtime> = OnceLock::new();
    RT.get_or_init(|| {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("failed to create tokio runtime for netlink")
    })
}

/// Helper: create connection, run async block, return result.
pub fn with_netlink<F, T>(f: F) -> Result<T, NetlinkError>
where
    F: std::future::Future<Output = Result<T, NetlinkError>>,
{
    let rt = runtime();
    rt.block_on(f)
}

/// Get the interface index for a device name.
async fn get_link_index(handle: &rtnetlink::Handle, name: &str) -> Result<u32, NetlinkError> {
    use futures_util::TryStreamExt;

    let mut links = handle.link().get().match_name(name.to_string()).execute();
    let link = links
        .try_next()
        .await
        .map_err(|e| NetlinkError::Other(format!("Failed to find device {}: {}", name, e)))?;

    link.map(|l| l.header.index)
        .ok_or_else(|| NetlinkError::DeviceNotFound(name.to_string()))
}

fn parse_ipv4(ip: &str, label: &str) -> Result<Ipv4Addr, NetlinkError> {
    ip.parse()
        .map_err(|e| NetlinkError::Other(format!("invalid {} {}: {}", label, ip, e)))
}

fn validate_ipv4_prefix(prefix_len: u8) -> Result<(), NetlinkError> {
    if prefix_len > 32 {
        return Err(NetlinkError::Other(format!(
            "invalid IPv4 prefix length: {}",
            prefix_len
        )));
    }
    Ok(())
}

fn map_netlink_error(context: &str, error: impl std::fmt::Display) -> NetlinkError {
    let message = error.to_string();
    if message.contains("File exists") || message.contains("EEXIST") {
        NetlinkError::RouteExists
    } else {
        NetlinkError::from_kernel_message(context, &message)
    }
}

async fn flush_addrs_by_index(
    handle: &rtnetlink::Handle,
    index: u32,
    name: &str,
) -> Result<(), NetlinkError> {
    use futures_util::TryStreamExt;

    let mut addrs = handle
        .address()
        .get()
        .set_link_index_filter(index)
        .execute();
    while let Some(addr) = addrs.try_next().await.map_err(|e| {
        NetlinkError::from_kernel_message(&format!("list addrs {}", name), &e.to_string())
    })? {
        handle.address().del(addr).execute().await.map_err(|e| {
            NetlinkError::from_kernel_message(&format!("del addr {}", name), &e.to_string())
        })?;
    }

    Ok(())
}

async fn add_addr_by_index(
    handle: &rtnetlink::Handle,
    index: u32,
    name: &str,
    addr: Ipv4Addr,
    prefix_len: u8,
) -> Result<(), NetlinkError> {
    handle
        .address()
        .add(index, std::net::IpAddr::V4(addr), prefix_len)
        .execute()
        .await
        .map_err(|e| map_netlink_error(&format!("add_addr {} on {}", addr, name), e))?;

    Ok(())
}

async fn set_link_up_by_index(
    handle: &rtnetlink::Handle,
    index: u32,
    name: &str,
) -> Result<(), NetlinkError> {
    use rtnetlink::LinkUnspec;

    handle
        .link()
        .set(LinkUnspec::new_with_index(index).up().build())
        .execute()
        .await
        .map_err(|e| {
            NetlinkError::from_kernel_message(&format!("set_link_up {}", name), &e.to_string())
        })?;

    Ok(())
}

/// Set a network interface to UP state.
pub fn set_link_up(name: &str) -> Result<(), NetlinkError> {
    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, name).await?;
        set_link_up_by_index(&handle, index, name).await?;

        log::debug!("Link {} set UP", name);
        Ok(())
    })
}

/// Flush all IPv4 addresses from an interface.
pub fn flush_addrs(name: &str) -> Result<(), NetlinkError> {
    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, name).await?;

        flush_addrs_by_index(&handle, index, name).await?;

        log::debug!("Flushed addresses on {}", name);
        Ok(())
    })
}

/// Add an IP address to an interface.
pub fn add_addr(name: &str, ip: &str, prefix_len: u8) -> Result<(), NetlinkError> {
    validate_ipv4_prefix(prefix_len)?;
    let addr = parse_ipv4(ip, "IP")?;

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, name).await?;
        add_addr_by_index(&handle, index, name, addr, prefix_len).await?;

        log::debug!("Added {}/{} to {}", ip, prefix_len, name);
        Ok(())
    })
}

/// Flush addresses, add host IP, and set the TAP link UP in one netlink session.
pub fn configure_tap(name: &str, host_ip: &str, prefix_len: u8) -> Result<(), NetlinkError> {
    validate_ipv4_prefix(prefix_len)?;
    let addr = parse_ipv4(host_ip, "host IP")?;

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, name).await?;
        flush_addrs_by_index(&handle, index, name).await?;
        add_addr_by_index(&handle, index, name, addr, prefix_len).await?;
        set_link_up_by_index(&handle, index, name).await?;

        log::debug!("Configured TAP {} with {}/{}", name, host_ip, prefix_len);
        Ok(())
    })
}

/// Add a route: dest/prefix_len via device.
pub fn add_route(dest: &str, prefix_len: u8, dev: &str) -> Result<(), NetlinkError> {
    use rtnetlink::RouteMessageBuilder;

    validate_ipv4_prefix(prefix_len)?;
    let dest_addr = parse_ipv4(dest, "dest")?;

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, dev).await?;
        let route = RouteMessageBuilder::<Ipv4Addr>::new()
            .destination_prefix(dest_addr, prefix_len)
            .output_interface(index)
            .build();
        handle.route().add(route).execute().await.map_err(|e| {
            map_netlink_error(&format!("add_route {}/{} dev {}", dest, prefix_len, dev), e)
        })?;

        log::debug!("Route {}/{} via {}", dest, prefix_len, dev);
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_invalid_ipv4_prefix() {
        let error = configure_tap("tap0", "172.16.0.1", 33).unwrap_err();

        assert_eq!(error.to_string(), "invalid IPv4 prefix length: 33");
    }

    #[test]
    fn rejects_invalid_configure_tap_ip() {
        let error = configure_tap("tap0", "not-an-ip", 32).unwrap_err();

        assert!(error.to_string().contains("invalid host IP not-an-ip"));
    }

    #[test]
    fn maps_route_exists_messages() {
        let error = map_netlink_error("add_route 172.16.0.2/32 dev tap0", "File exists");

        assert!(matches!(error, NetlinkError::RouteExists));
    }

    #[test]
    fn maps_netlink_eperm_messages() {
        let error = map_netlink_error(
            "set_link_up tap0",
            "NetlinkError { code: -1, message: None }",
        );

        assert!(matches!(error, NetlinkError::PermissionDenied));
        assert_eq!(error.to_string(), "Operation not permitted");
    }
}

/// Get the default outbound network interface name.
pub fn get_default_interface() -> Result<String, NetlinkError> {
    use rtnetlink::RouteMessageBuilder;

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        use futures_util::TryStreamExt;
        use netlink_packet_route::route::RouteAttribute;

        let route_filter = RouteMessageBuilder::<Ipv4Addr>::new().build();
        let mut routes = handle.route().get(route_filter).execute();
        while let Some(route) = routes
            .try_next()
            .await
            .map_err(|e| NetlinkError::Other(format!("get routes: {}", e)))?
        {
            // Default route has dst_len == 0
            if route.header.destination_prefix_length == 0 {
                for attr in &route.attributes {
                    if let RouteAttribute::Oif(idx) = attr {
                        // Resolve index to name
                        let mut links = handle.link().get().match_index(*idx).execute();
                        if let Some(link) = links
                            .try_next()
                            .await
                            .map_err(|e| NetlinkError::Other(format!("get link: {}", e)))?
                        {
                            use netlink_packet_route::link::LinkAttribute;
                            for attr in &link.attributes {
                                if let LinkAttribute::IfName(name) = attr {
                                    return Ok(name.clone());
                                }
                            }
                        }
                    }
                }
            }
        }

        Err(NetlinkError::Other("No default route found".to_string()))
    })
}
