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
async fn get_link_index(
    handle: &rtnetlink::Handle,
    name: &str,
) -> Result<u32, NetlinkError> {
    use futures_util::TryStreamExt;

    let mut links = handle.link().get().match_name(name.to_string()).execute();
    let link = links.try_next().await.map_err(|e| {
        NetlinkError::Other(format!("Failed to find device {}: {}", name, e))
    })?;

    link.map(|l| l.header.index)
        .ok_or_else(|| NetlinkError::DeviceNotFound(name.to_string()))
}

/// Set a network interface to UP state.
pub fn set_link_up(name: &str) -> Result<(), NetlinkError> {
    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, name).await?;
        handle
            .link()
            .set(index)
            .up()
            .execute()
            .await
            .map_err(|e| NetlinkError::Other(format!("set_link_up {}: {}", name, e)))?;

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

        use futures_util::TryStreamExt;
        let mut addrs = handle.address().get().set_link_index_filter(index).execute();
        while let Some(addr) = addrs.try_next().await.map_err(|e| {
            NetlinkError::Other(format!("list addrs {}: {}", name, e))
        })? {
            handle
                .address()
                .del(addr)
                .execute()
                .await
                .map_err(|e| NetlinkError::Other(format!("del addr {}: {}", name, e)))?;
        }

        log::debug!("Flushed addresses on {}", name);
        Ok(())
    })
}

/// Add an IP address to an interface.
pub fn add_addr(name: &str, ip: &str, prefix_len: u8) -> Result<(), NetlinkError> {
    let addr: Ipv4Addr = ip
        .parse()
        .map_err(|e| NetlinkError::Other(format!("invalid IP {}: {}", ip, e)))?;

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, name).await?;
        handle
            .address()
            .add(index, std::net::IpAddr::V4(addr), prefix_len)
            .execute()
            .await
            .map_err(|e| {
                let msg = format!("{}", e);
                if msg.contains("File exists") || msg.contains("EEXIST") {
                    NetlinkError::RouteExists
                } else {
                    NetlinkError::Other(format!("add_addr {} on {}: {}", ip, name, e))
                }
            })?;

        log::debug!("Added {}/{} to {}", ip, prefix_len, name);
        Ok(())
    })
}

/// Add a route: dest/prefix_len via device.
pub fn add_route(dest: &str, prefix_len: u8, dev: &str) -> Result<(), NetlinkError> {
    let dest_addr: Ipv4Addr = dest
        .parse()
        .map_err(|e| NetlinkError::Other(format!("invalid dest {}: {}", dest, e)))?;

    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        let index = get_link_index(&handle, dev).await?;
        handle
            .route()
            .add()
            .v4()
            .destination_prefix(dest_addr, prefix_len)
            .output_interface(index)
            .execute()
            .await
            .map_err(|e| {
                let msg = format!("{}", e);
                if msg.contains("File exists") || msg.contains("EEXIST") {
                    NetlinkError::RouteExists
                } else {
                    NetlinkError::Other(format!("add_route {}/{} dev {}: {}", dest, prefix_len, dev, e))
                }
            })?;

        log::debug!("Route {}/{} via {}", dest, prefix_len, dev);
        Ok(())
    })
}

/// Get the default outbound network interface name.
pub fn get_default_interface() -> Result<String, NetlinkError> {
    with_netlink(async {
        let (connection, handle, _) = rtnetlink::new_connection()
            .map_err(|e| NetlinkError::Other(format!("netlink: {}", e)))?;
        tokio::spawn(connection);

        use futures_util::TryStreamExt;
        use netlink_packet_route::route::RouteAttribute;

        let mut routes = handle.route().get(rtnetlink::IpVersion::V4).execute();
        while let Some(route) = routes.try_next().await.map_err(|e| {
            NetlinkError::Other(format!("get routes: {}", e))
        })? {
            // Default route has dst_len == 0
            if route.header.destination_prefix_length == 0 {
                for attr in &route.attributes {
                    if let RouteAttribute::Oif(idx) = attr {
                        // Resolve index to name
                        let mut links = handle.link().get().match_index(*idx).execute();
                        if let Some(link) = links.try_next().await.map_err(|e| {
                            NetlinkError::Other(format!("get link: {}", e))
                        })? {
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
