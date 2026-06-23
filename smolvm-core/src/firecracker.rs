//! Minimal Firecracker HTTP-over-Unix-socket helper.

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::{Duration, Instant};

#[derive(Debug, thiserror::Error)]
pub enum FirecrackerError {
    #[error("{0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Other(String),
}

pub fn request(
    socket_path: &str,
    method: &str,
    path: &str,
    body_json: Option<&str>,
    timeout_secs: f64,
) -> Result<(u16, Option<String>), FirecrackerError> {
    let timeout = Duration::from_secs_f64(timeout_secs.max(0.001));
    let mut stream = UnixStream::connect(socket_path)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let body = body_json.unwrap_or("");
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes())?;
    stream.shutdown(std::net::Shutdown::Write).ok();

    let (status, payload) = read_response(&mut stream)?;
    if status == 204 || payload.is_empty() {
        Ok((status, None))
    } else {
        Ok((status, Some(String::from_utf8_lossy(&payload).to_string())))
    }
}

pub fn wait_for_socket(socket_path: &str, timeout_secs: f64) -> Result<(), FirecrackerError> {
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_secs.max(0.001));
    while Instant::now() < deadline {
        if Path::new(socket_path).exists() {
            if let Ok((status, _)) = request(socket_path, "GET", "/", None, 1.0) {
                if status == 200 {
                    return Ok(());
                }
            }
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    Err(FirecrackerError::Other(
        "wait_for_socket timed out".to_string(),
    ))
}

fn read_response(reader: &mut impl Read) -> Result<(u16, Vec<u8>), FirecrackerError> {
    let marker = b"\r\n\r\n";
    let mut response = Vec::new();
    let mut chunk = [0_u8; 4096];
    let split = loop {
        if let Some(split) = response
            .windows(marker.len())
            .position(|window| window == marker)
        {
            break split;
        }
        let read = reader.read(&mut chunk)?;
        if read == 0 {
            return Err(FirecrackerError::Other(
                "malformed HTTP response".to_string(),
            ));
        }
        response.extend_from_slice(&chunk[..read]);
        if response.len() > 64 * 1024 {
            return Err(FirecrackerError::Other(
                "HTTP response headers are too large".to_string(),
            ));
        }
    };

    let headers = String::from_utf8_lossy(&response[..split]);
    let status_line = headers
        .lines()
        .next()
        .ok_or_else(|| FirecrackerError::Other("missing HTTP status".to_string()))?;
    let status = status_line
        .split_whitespace()
        .nth(1)
        .ok_or_else(|| FirecrackerError::Other("missing HTTP status code".to_string()))?
        .parse::<u16>()
        .map_err(|error| FirecrackerError::Other(format!("invalid HTTP status: {error}")))?;
    let content_length = parse_content_length(&headers)?;
    let mut payload = response[split + marker.len()..].to_vec();
    if let Some(content_length) = content_length {
        if payload.len() > content_length {
            payload.truncate(content_length);
        } else {
            let remaining = content_length - payload.len();
            if remaining > 0 {
                let start = payload.len();
                payload.resize(content_length, 0);
                reader.read_exact(&mut payload[start..])?;
            }
        }
    }
    Ok((status, payload))
}

fn parse_content_length(headers: &str) -> Result<Option<usize>, FirecrackerError> {
    for line in headers.lines().skip(1) {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        if name.eq_ignore_ascii_case("content-length") {
            return value.trim().parse::<usize>().map(Some).map_err(|error| {
                FirecrackerError::Other(format!("invalid Content-Length: {error}"))
            });
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_status_and_payload() {
        let response = b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{\"ok\":true}";
        let (status, payload) = read_response(&mut &response[..]).unwrap();

        assert_eq!(status, 200);
        assert_eq!(payload, br#"{"ok":true}"#);
    }

    #[test]
    fn extracts_fault_message() {
        let response =
            b"HTTP/1.1 400 Bad Request\r\nContent-Length: 31\r\n\r\n{\"fault_message\":\"bad request\"}";
        let (status, payload) = read_response(&mut &response[..]).unwrap();

        assert_eq!(status, 400);
        assert_eq!(payload, br#"{"fault_message":"bad request"}"#);
    }

    #[test]
    fn reads_only_declared_payload() {
        let response =
            b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: keep-alive\r\n\r\n{\"ok\":true}extra";
        let (status, payload) = read_response(&mut &response[..]).unwrap();

        assert_eq!(status, 200);
        assert_eq!(payload, br#"{"ok":true}"#);
    }
}
