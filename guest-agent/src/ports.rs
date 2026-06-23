//! Port readiness helper.

use serde::{Deserialize, Serialize};
use std::net::{IpAddr, SocketAddr, TcpStream};
use std::time::{Duration, Instant};

const MAX_TIMEOUT_MS: u64 = 300_000;
const MAX_PORTS: usize = 256;

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
    if req.ports.len() > MAX_PORTS {
        return PortsWaitResponse {
            ok: false,
            ready_ports: Vec::new(),
            error: Some(format!("too many ports; maximum is {MAX_PORTS}")),
        };
    }
    if req.timeout_ms > MAX_TIMEOUT_MS {
        return PortsWaitResponse {
            ok: false,
            ready_ports: Vec::new(),
            error: Some(format!("timeout_ms must be at most {MAX_TIMEOUT_MS}")),
        };
    }
    let host = req.host.unwrap_or_else(|| "127.0.0.1".to_string());
    let ip = match host.parse::<IpAddr>() {
        Ok(ip) => ip,
        Err(_) => {
            return PortsWaitResponse {
                ok: false,
                ready_ports: Vec::new(),
                error: Some(format!("invalid host: {host}")),
            };
        }
    };
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
    let Some(deadline) = Instant::now().checked_add(timeout) else {
        return PortsWaitResponse {
            ok: false,
            ready_ports: Vec::new(),
            error: Some("timeout is too large".to_string()),
        };
    };
    let mut ready = Vec::new();
    while Instant::now() < deadline {
        ready.clear();
        for port in &ports {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            let addr = SocketAddr::new(ip, *port);
            if TcpStream::connect_timeout(&addr, remaining.min(Duration::from_millis(100))).is_ok()
            {
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
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        std::thread::sleep(remaining.min(Duration::from_millis(25)));
    }
    PortsWaitResponse {
        ok: false,
        ready_ports: ready,
        error: Some("timed out waiting for ports".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn rejects_timeout_over_advertised_limit() {
        let response = wait_for_ports(PortsWaitRequest {
            ports: vec![80],
            timeout_ms: MAX_TIMEOUT_MS + 1,
            host: None,
        })
        .await;

        assert!(!response.ok);
        assert_eq!(response.ready_ports, Vec::<u16>::new());
        assert_eq!(
            response.error.as_deref(),
            Some("timeout_ms must be at most 300000")
        );
    }

    #[tokio::test]
    async fn rejects_unbounded_port_lists() {
        let response = wait_for_ports(PortsWaitRequest {
            ports: vec![80; MAX_PORTS + 1],
            timeout_ms: 1,
            host: None,
        })
        .await;

        assert!(!response.ok);
        assert_eq!(response.ready_ports, Vec::<u16>::new());
        assert_eq!(
            response.error.as_deref(),
            Some("too many ports; maximum is 256")
        );
    }

    #[tokio::test]
    async fn rejects_invalid_hosts() {
        let response = wait_for_ports(PortsWaitRequest {
            ports: vec![80],
            timeout_ms: 1,
            host: Some("not-an-ip".to_string()),
        })
        .await;

        assert!(!response.ok);
        assert_eq!(response.ready_ports, Vec::<u16>::new());
        assert_eq!(response.error.as_deref(), Some("invalid host: not-an-ip"));
    }
}
