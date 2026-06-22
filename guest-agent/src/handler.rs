use axum::{
    Json, Router,
    extract::DefaultBodyLimit,
    routing::{get, post},
};
use once_cell::sync::Lazy;
use serde::Serialize;
use std::time::Instant;

use crate::exec::{self, ExecRequest, ExecResponse};
use crate::files::{self, FileGetQuery, FileGetResponse, FilePutRequest, FilePutResponse};

pub const PROTOCOL_VERSION: u32 = 1;
const FILE_PUT_BODY_LIMIT_BYTES: usize = 50 * 1024 * 1024;
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
            "/files/put",
            post(handle_file_put).layer(DefaultBodyLimit::max(FILE_PUT_BODY_LIMIT_BYTES)),
        )
        .route("/files/get", get(handle_file_get))
}

pub fn extension_router() -> Router {
    Lazy::force(&START_TIME);
    Router::new()
        .route("/version", get(handle_version))
        .route("/capabilities", get(handle_capabilities))
        .route("/sync", post(handle_sync))
        .route(
            "/files/put",
            post(handle_file_put).layer(DefaultBodyLimit::max(FILE_PUT_BODY_LIMIT_BYTES)),
        )
        .route("/files/get", get(handle_file_get))
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
    pub tcp_enabled: bool,
    pub terminal_enabled: bool,
    pub prod_metrics_enabled: bool,
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
            "POST /files/put",
            "GET /files/get",
        ],
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

pub async fn handle_file_put(Json(req): Json<FilePutRequest>) -> Json<FilePutResponse> {
    Json(files::put_file(req).await)
}

pub async fn handle_file_get(
    axum::extract::Query(query): axum::extract::Query<FileGetQuery>,
) -> Json<FileGetResponse> {
    Json(files::get_file(query).await)
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
        let endpoints = capabilities.body["endpoints"].as_array().unwrap();
        assert!(endpoints.contains(&json!("POST /exec")));
        assert!(endpoints.contains(&json!("POST /sync")));
        assert!(endpoints.contains(&json!("POST /files/put")));
        assert!(endpoints.contains(&json!("GET /files/get")));
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
}
