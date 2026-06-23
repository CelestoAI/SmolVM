use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, Query},
    http::{HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use once_cell::sync::Lazy;
use serde::Serialize;
use std::time::Instant;

use crate::boot::{self, BootMilestonesResponse};
use crate::env::{self, EnvDeleteRequest, EnvPutRequest, EnvResponse};
use crate::exec::{self, ExecRequest, ExecResponse};
use crate::files::{self, DirectoryTarQuery, FileGetQuery, FilePutResponse, FileRawPutQuery};
use crate::ports::{self, PortsWaitRequest, PortsWaitResponse};

pub const PROTOCOL_VERSION: u32 = 2;
const FILE_RAW_BODY_LIMIT_BYTES: usize = 256 * 1024 * 1024;
const DIRECTORY_TAR_BODY_LIMIT_BYTES: usize = 512 * 1024 * 1024;
static START_TIME: Lazy<Instant> = Lazy::new(Instant::now);

pub fn router() -> Router {
    Lazy::force(&START_TIME);
    Router::new()
        .route("/health", get(handle_health))
        .route("/version", get(handle_version))
        .route("/capabilities", get(handle_capabilities))
        .route("/exec", post(handle_exec))
        .route("/sync", post(handle_sync))
        .route(
            "/files/raw",
            get(handle_file_raw_get)
                .put(handle_file_raw_put)
                .layer(DefaultBodyLimit::max(FILE_RAW_BODY_LIMIT_BYTES)),
        )
        .route(
            "/files/content",
            get(handle_file_raw_get)
                .put(handle_file_raw_put)
                .layer(DefaultBodyLimit::max(FILE_RAW_BODY_LIMIT_BYTES)),
        )
        .route(
            "/dirs/tar",
            get(handle_dir_tar_get)
                .put(handle_dir_tar_put)
                .layer(DefaultBodyLimit::max(DIRECTORY_TAR_BODY_LIMIT_BYTES)),
        )
        .route(
            "/directories/tar",
            get(handle_dir_tar_get)
                .put(handle_dir_tar_put)
                .layer(DefaultBodyLimit::max(DIRECTORY_TAR_BODY_LIMIT_BYTES)),
        )
        .route(
            "/env",
            get(handle_env_list)
                .put(handle_env_set)
                .delete(handle_env_unset),
        )
        .route(
            "/env/managed",
            get(handle_env_list)
                .put(handle_env_set)
                .delete(handle_env_unset),
        )
        .route("/boot/milestones", get(handle_boot_milestones))
        .route("/ports/wait", post(handle_ports_wait))
}

pub fn extension_router() -> Router {
    Lazy::force(&START_TIME);
    Router::new()
        .route("/version", get(handle_version))
        .route("/capabilities", get(handle_capabilities))
        .route("/sync", post(handle_sync))
        .route(
            "/files/raw",
            get(handle_file_raw_get)
                .put(handle_file_raw_put)
                .layer(DefaultBodyLimit::max(FILE_RAW_BODY_LIMIT_BYTES)),
        )
        .route(
            "/files/content",
            get(handle_file_raw_get)
                .put(handle_file_raw_put)
                .layer(DefaultBodyLimit::max(FILE_RAW_BODY_LIMIT_BYTES)),
        )
        .route(
            "/dirs/tar",
            get(handle_dir_tar_get)
                .put(handle_dir_tar_put)
                .layer(DefaultBodyLimit::max(DIRECTORY_TAR_BODY_LIMIT_BYTES)),
        )
        .route(
            "/directories/tar",
            get(handle_dir_tar_get)
                .put(handle_dir_tar_put)
                .layer(DefaultBodyLimit::max(DIRECTORY_TAR_BODY_LIMIT_BYTES)),
        )
        .route(
            "/env",
            get(handle_env_list)
                .put(handle_env_set)
                .delete(handle_env_unset),
        )
        .route(
            "/env/managed",
            get(handle_env_list)
                .put(handle_env_set)
                .delete(handle_env_unset),
        )
        .route("/boot/milestones", get(handle_boot_milestones))
        .route("/ports/wait", post(handle_ports_wait))
}

#[derive(Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub uptime_seconds: u64,
    pub agent_version: &'static str,
    pub protocol: &'static str,
    pub protocol_version: u32,
}

pub async fn handle_health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        uptime_seconds: START_TIME.elapsed().as_secs(),
        agent_version: env!("CARGO_PKG_VERSION"),
        protocol: "smolvm-http-vsock",
        protocol_version: PROTOCOL_VERSION,
    })
}

#[derive(Serialize)]
pub struct VersionResponse {
    pub agent_name: &'static str,
    pub agent_version: &'static str,
    pub protocol: &'static str,
    pub protocol_version: u32,
}

pub async fn handle_version() -> Json<VersionResponse> {
    Json(VersionResponse {
        agent_name: "smolvm-guest-agent",
        agent_version: env!("CARGO_PKG_VERSION"),
        protocol: "smolvm-http-vsock",
        protocol_version: PROTOCOL_VERSION,
    })
}

#[derive(Serialize)]
pub struct CapabilitiesResponse {
    pub protocol_version: u32,
    pub endpoints: Vec<&'static str>,
    pub features: CapabilityFeatures,
    pub limits: CapabilityLimits,
    pub tcp_enabled: bool,
    pub terminal_enabled: bool,
    pub prod_metrics_enabled: bool,
}

#[derive(Serialize)]
pub struct CapabilityFeatures {
    pub exec: bool,
    pub sync: bool,
    pub file_raw: bool,
    #[serde(rename = "files.stream")]
    pub files_stream: bool,
    pub dir_tar: bool,
    #[serde(rename = "files.directory_tar")]
    pub files_directory_tar: bool,
    pub env_managed: bool,
    #[serde(rename = "env.managed")]
    pub env_managed_v2: bool,
    pub boot_milestones: bool,
    #[serde(rename = "boot.milestones")]
    pub boot_milestones_v2: bool,
    pub ports_wait: bool,
    #[serde(rename = "ports.wait")]
    pub ports_wait_v2: bool,
    #[serde(rename = "browser.status")]
    pub browser_status: bool,
    pub tcp_listener: bool,
    pub terminal: bool,
    pub prod_metrics: bool,
}

#[derive(Serialize)]
pub struct CapabilityLimits {
    pub file_raw_put_bytes: usize,
    pub max_stream_size_bytes: usize,
    pub directory_tar_put_bytes: usize,
    pub max_tar_size_bytes: usize,
    pub default_operation_timeout_ms: u64,
    pub port_wait_timeout_seconds: u64,
}

pub async fn handle_capabilities() -> Json<CapabilitiesResponse> {
    Json(CapabilitiesResponse {
        protocol_version: PROTOCOL_VERSION,
        endpoints: vec![
            "GET /health",
            "GET /version",
            "GET /capabilities",
            "POST /exec",
            "POST /sync",
            "PUT /files/content",
            "GET /files/content",
            "PUT /directories/tar",
            "GET /directories/tar",
            "PUT /files/raw",
            "GET /files/raw",
            "PUT /dirs/tar",
            "GET /dirs/tar",
            "GET /env",
            "PUT /env",
            "DELETE /env",
            "GET /env/managed",
            "PUT /env/managed",
            "DELETE /env/managed",
            "GET /boot/milestones",
            "POST /ports/wait",
        ],
        features: CapabilityFeatures {
            exec: true,
            sync: true,
            file_raw: true,
            files_stream: true,
            dir_tar: true,
            files_directory_tar: true,
            env_managed: true,
            env_managed_v2: true,
            boot_milestones: true,
            boot_milestones_v2: true,
            ports_wait: true,
            ports_wait_v2: true,
            browser_status: false,
            tcp_listener: cfg!(feature = "tcp"),
            terminal: false,
            prod_metrics: false,
        },
        limits: CapabilityLimits {
            file_raw_put_bytes: FILE_RAW_BODY_LIMIT_BYTES,
            max_stream_size_bytes: FILE_RAW_BODY_LIMIT_BYTES,
            directory_tar_put_bytes: DIRECTORY_TAR_BODY_LIMIT_BYTES,
            max_tar_size_bytes: DIRECTORY_TAR_BODY_LIMIT_BYTES,
            default_operation_timeout_ms: 120_000,
            port_wait_timeout_seconds: 300,
        },
        tcp_enabled: cfg!(feature = "tcp"),
        terminal_enabled: false,
        prod_metrics_enabled: false,
    })
}

pub async fn handle_exec(Json(req): Json<ExecRequest>) -> Json<ExecResponse> {
    Json(exec::run_command(req).await)
}

#[derive(Serialize)]
pub struct SyncResponse {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

pub async fn handle_sync() -> Json<SyncResponse> {
    let result = tokio::task::spawn_blocking(|| unsafe { libc::sync() }).await;
    match result {
        Ok(()) => Json(SyncResponse {
            ok: true,
            error: None,
        }),
        Err(error) => Json(SyncResponse {
            ok: false,
            error: Some(format!("sync task failed: {error}")),
        }),
    }
}

pub async fn handle_file_raw_put(
    Query(query): Query<FileRawPutQuery>,
    body: Bytes,
) -> Json<FilePutResponse> {
    Json(files::put_file_bytes(
        &query.path,
        query.name.as_deref(),
        query.mode,
        &body,
    ))
}

pub async fn handle_file_raw_get(Query(query): Query<FileGetQuery>) -> Response {
    match files::read_file_bytes(&query.path) {
        Ok(file) => binary_response("application/octet-stream", file.mode, file.size, file.data),
        Err(error) => json_error(StatusCode::BAD_REQUEST, error),
    }
}

pub async fn handle_dir_tar_put(
    Query(query): Query<DirectoryTarQuery>,
    body: Bytes,
) -> Json<FilePutResponse> {
    Json(files::extract_directory_tar(&query.path, &body))
}

pub async fn handle_dir_tar_get(Query(query): Query<DirectoryTarQuery>) -> Response {
    match files::create_directory_tar(&query.path) {
        Ok(data) => binary_response("application/x-tar", 0o644, data.len() as u64, data),
        Err(error) => json_error(StatusCode::BAD_REQUEST, error),
    }
}

pub async fn handle_env_list() -> Json<EnvResponse> {
    Json(env::read_managed().await)
}

pub async fn handle_env_set(Json(req): Json<EnvPutRequest>) -> Json<EnvResponse> {
    Json(env::put_managed(req).await)
}

pub async fn handle_env_unset(Json(req): Json<EnvDeleteRequest>) -> Json<EnvResponse> {
    Json(env::delete_managed(req).await)
}

pub async fn handle_boot_milestones() -> Json<BootMilestonesResponse> {
    Json(boot::read_boot_milestones())
}

pub async fn handle_ports_wait(Json(req): Json<PortsWaitRequest>) -> Json<PortsWaitResponse> {
    Json(ports::wait_for_ports(req).await)
}

#[derive(Serialize)]
struct ErrorResponse {
    ok: bool,
    error: String,
}

fn json_error(error_status: StatusCode, error: impl Into<String>) -> Response {
    (
        error_status,
        Json(ErrorResponse {
            ok: false,
            error: error.into(),
        }),
    )
        .into_response()
}

fn binary_response(content_type: &'static str, mode: u32, size: u64, data: Vec<u8>) -> Response {
    let mut response = Response::new(Body::from(data));
    *response.status_mut() = StatusCode::OK;
    let headers = response.headers_mut();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    headers.insert(
        "x-smolvm-file-mode",
        HeaderValue::from_str(&format!("{mode:o}")).expect("mode header is valid ASCII"),
    );
    headers.insert(
        "x-smolvm-file-size",
        HeaderValue::from_str(&size.to_string()).expect("size header is valid ASCII"),
    );
    headers.insert(
        header::CONTENT_LENGTH,
        HeaderValue::from_str(&size.to_string()).expect("size header is valid ASCII"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{Body, to_bytes},
        http::{Request, StatusCode},
    };
    use serde_json::{Value, json};
    use tower::ServiceExt;

    #[tokio::test]
    async fn public_router_exposes_health_version_and_safe_capabilities() {
        let health = request_json(router(), "GET", "/health", None).await;
        assert_eq!(health.status, StatusCode::OK);
        assert_eq!(health.body["status"], "ok");
        assert_eq!(health.body["agent_version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(health.body["protocol"], "smolvm-http-vsock");
        assert_eq!(health.body["protocol_version"], PROTOCOL_VERSION);

        let version = request_json(router(), "GET", "/version", None).await;
        assert_eq!(version.status, StatusCode::OK);
        assert_eq!(version.body["agent_name"], "smolvm-guest-agent");
        assert_eq!(version.body["protocol_version"], PROTOCOL_VERSION);

        let capabilities = request_json(router(), "GET", "/capabilities", None).await;
        assert_eq!(capabilities.status, StatusCode::OK);
        assert_eq!(capabilities.body["protocol_version"], PROTOCOL_VERSION);
        assert_eq!(capabilities.body["tcp_enabled"], cfg!(feature = "tcp"));
        assert_eq!(capabilities.body["terminal_enabled"], false);
        assert_eq!(capabilities.body["prod_metrics_enabled"], false);
        assert!(capabilities.body["features"].get("file_base64").is_none());
        assert_eq!(capabilities.body["features"]["file_raw"], true);
        assert_eq!(capabilities.body["features"]["files.stream"], true);
        assert_eq!(capabilities.body["features"]["dir_tar"], true);
        assert_eq!(capabilities.body["features"]["files.directory_tar"], true);
        assert_eq!(capabilities.body["features"]["env_managed"], true);
        assert_eq!(capabilities.body["features"]["env.managed"], true);
        assert_eq!(capabilities.body["features"]["boot_milestones"], true);
        assert_eq!(capabilities.body["features"]["boot.milestones"], true);
        assert_eq!(capabilities.body["features"]["ports_wait"], true);
        assert_eq!(capabilities.body["features"]["ports.wait"], true);
        assert_eq!(capabilities.body["features"]["browser.status"], false);
        assert!(
            capabilities.body["limits"]
                .get("file_put_json_bytes")
                .is_none()
        );
        assert_eq!(
            capabilities.body["limits"]["max_stream_size_bytes"],
            FILE_RAW_BODY_LIMIT_BYTES
        );
        assert_eq!(
            capabilities.body["limits"]["max_tar_size_bytes"],
            DIRECTORY_TAR_BODY_LIMIT_BYTES
        );
        let endpoints = capabilities.body["endpoints"].as_array().unwrap();
        assert!(endpoints.contains(&json!("POST /exec")));
        assert!(endpoints.contains(&json!("POST /sync")));
        assert!(!endpoints.contains(&json!("POST /files/put")));
        assert!(!endpoints.contains(&json!("GET /files/get")));
        assert!(endpoints.contains(&json!("PUT /files/content")));
        assert!(endpoints.contains(&json!("GET /files/content")));
        assert!(endpoints.contains(&json!("PUT /directories/tar")));
        assert!(endpoints.contains(&json!("GET /directories/tar")));
        assert!(endpoints.contains(&json!("PUT /files/raw")));
        assert!(endpoints.contains(&json!("GET /files/raw")));
        assert!(endpoints.contains(&json!("PUT /dirs/tar")));
        assert!(endpoints.contains(&json!("GET /dirs/tar")));
        assert!(endpoints.contains(&json!("GET /env")));
        assert!(endpoints.contains(&json!("GET /boot/milestones")));
        assert!(endpoints.contains(&json!("POST /ports/wait")));
    }

    #[tokio::test]
    async fn public_router_exec_maps_validation_errors_to_json() {
        let response = request_json(
            router(),
            "POST",
            "/exec",
            Some(json!({
                "command": "",
                "shell": "raw",
                "timeout_seconds": 5
            })),
        )
        .await;

        assert_eq!(response.status, StatusCode::OK);
        assert_eq!(response.body["ok"], false);
        assert_eq!(response.body["exit_code"], -1);
        assert_eq!(response.body["timed_out"], false);
        assert_eq!(response.body["error"], "missing command");
    }

    #[tokio::test]
    async fn public_router_sync_flushes_filesystems() {
        let response = request_json(router(), "POST", "/sync", None).await;

        assert_eq!(response.status, StatusCode::OK);
        assert_eq!(response.body["ok"], true);
        assert_eq!(response.body.get("error"), None);
    }

    #[tokio::test]
    async fn public_router_raw_file_put_and_get_transfer_bytes() {
        let dir = tempfile_dir();
        let path = dir.join("payload.bin");
        let uri = format!(
            "/files/content?path={}&mode=384",
            url_escape(path.to_str().unwrap())
        );
        let put = raw_bytes_request(
            router(),
            "PUT",
            &uri,
            b"raw payload".to_vec(),
            "application/octet-stream",
        )
        .await;
        assert_eq!(put.status(), StatusCode::OK);
        let put_body = to_bytes(put.into_body(), usize::MAX).await.unwrap();
        let put_json: Value = serde_json::from_slice(&put_body).unwrap();
        assert_eq!(put_json["ok"], true);

        let get_uri = format!("/files/content?path={}", url_escape(path.to_str().unwrap()));
        let get = raw_request(router(), "GET", &get_uri, None).await;
        assert_eq!(get.status(), StatusCode::OK);
        assert_eq!(get.headers().get("x-smolvm-file-mode").unwrap(), "600");
        let get_body = to_bytes(get.into_body(), usize::MAX).await.unwrap();
        assert_eq!(&get_body[..], b"raw payload");
    }

    #[tokio::test]
    async fn extension_router_leaves_private_health_and_exec_routes_to_consumer() {
        let version = request_json(extension_router(), "GET", "/version", None).await;
        assert_eq!(version.status, StatusCode::OK);

        let health = raw_request(extension_router(), "GET", "/health", None).await;
        assert_eq!(health.status(), StatusCode::NOT_FOUND);

        let exec = raw_request(
            extension_router(),
            "POST",
            "/exec",
            Some(json!({"command": "printf no-op", "shell": "raw"})),
        )
        .await;
        assert_eq!(exec.status(), StatusCode::NOT_FOUND);

        let sync = request_json(extension_router(), "POST", "/sync", None).await;
        assert_eq!(sync.status, StatusCode::OK);
        assert_eq!(sync.body["ok"], true);
    }

    struct JsonResponse {
        status: StatusCode,
        body: Value,
    }

    async fn request_json(
        app: Router,
        method: &str,
        uri: &str,
        body: Option<Value>,
    ) -> JsonResponse {
        let response = raw_request(app, method, uri, body).await;
        let status = response.status();
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        JsonResponse {
            status,
            body: serde_json::from_slice(&bytes).unwrap(),
        }
    }

    async fn raw_request(
        app: Router,
        method: &str,
        uri: &str,
        body: Option<Value>,
    ) -> axum::response::Response {
        let mut builder = Request::builder().method(method).uri(uri);
        let body = match body {
            Some(value) => {
                builder = builder.header("content-type", "application/json");
                Body::from(serde_json::to_vec(&value).unwrap())
            }
            None => Body::empty(),
        };
        app.oneshot(builder.body(body).unwrap()).await.unwrap()
    }

    async fn raw_bytes_request(
        app: Router,
        method: &str,
        uri: &str,
        body: Vec<u8>,
        content_type: &str,
    ) -> axum::response::Response {
        let response = Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", content_type)
            .body(Body::from(body))
            .unwrap();
        app.oneshot(response).await.unwrap()
    }

    fn tempfile_dir() -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "smolvm-agent-handler-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        std::fs::create_dir(&path).unwrap();
        path
    }

    fn url_escape(value: &str) -> String {
        value
            .bytes()
            .flat_map(|byte| match byte {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                    vec![byte as char]
                }
                other => format!("%{other:02X}").chars().collect(),
            })
            .collect()
    }
}
