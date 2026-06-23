//! Port readiness helper.

use serde::{Deserialize, Serialize};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::time::{Duration, Instant};

#[derive(Debug, Deserialize)]
pub struct PortsWaitRequest {
    pub ports: Vec<u16>,
    #[serde(default = "default_timeout_ms")]
    pub timeout_ms: u64,
    pub host: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct PortsWaitResponse {
    pub ok: bool,
    pub ready_ports: Vec<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

fn default_timeout_ms() -> u64 {
    30_000
}

pub async fn wait_for_ports(req: PortsWaitRequest) -> PortsWaitResponse {
    if req.ports.is_empty() {
        return PortsWaitResponse {
            ok: false,
            ready_ports: Vec::new(),
            error: Some("missing ports".to_string()),
        };
    }
    let host = req.host.unwrap_or_else(|| "127.0.0.1".to_string());
    let ip = host
        .parse::<IpAddr>()
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST));
    let timeout = Duration::from_millis(req.timeout_ms.max(1));
    let ports = req.ports;

    match tokio::task::spawn_blocking(move || wait_blocking(ip, ports, timeout)).await {
        Ok(response) => response,
        Err(error) => PortsWaitResponse {
            ok: false,
            ready_ports: Vec::new(),
            error: Some(format!("port wait task failed: {error}")),
        },
    }
}

fn wait_blocking(ip: IpAddr, ports: Vec<u16>, timeout: Duration) -> PortsWaitResponse {
    let deadline = Instant::now() + timeout;
    let mut ready = Vec::new();
    while Instant::now() < deadline {
        ready.clear();
        for port in &ports {
            let addr = SocketAddr::new(ip, *port);
            if TcpStream::connect_timeout(&addr, Duration::from_millis(100)).is_ok() {
                ready.push(*port);
            }
        }
        if ready.len() == ports.len() {
            return PortsWaitResponse {
                ok: true,
                ready_ports: ready,
                error: None,
            };
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    PortsWaitResponse {
        ok: false,
        ready_ports: ready,
        error: Some("timed out waiting for ports".to_string()),
    }
}
