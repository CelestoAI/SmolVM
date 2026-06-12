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
        .route(
            "/files/put",
            post(handle_file_put).layer(DefaultBodyLimit::max(FILE_PUT_BODY_LIMIT_BYTES)),
        )
        .route("/files/get", get(handle_file_get))
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
    uptime_seconds: u64,
    agent_version: &'static str,
    protocol: &'static str,
    protocol_version: u32,
}

async fn handle_health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        uptime_seconds: START_TIME.elapsed().as_secs(),
        agent_version: env!("CARGO_PKG_VERSION"),
        protocol: "smolvm-http-vsock",
        protocol_version: PROTOCOL_VERSION,
    })
}

#[derive(Serialize)]
struct VersionResponse {
    agent_name: &'static str,
    agent_version: &'static str,
    protocol: &'static str,
    protocol_version: u32,
}

async fn handle_version() -> Json<VersionResponse> {
    Json(VersionResponse {
        agent_name: "smolvm-guest-agent",
        agent_version: env!("CARGO_PKG_VERSION"),
        protocol: "smolvm-http-vsock",
        protocol_version: PROTOCOL_VERSION,
    })
}

#[derive(Serialize)]
struct CapabilitiesResponse {
    protocol_version: u32,
    endpoints: Vec<&'static str>,
    tcp_enabled: bool,
    terminal_enabled: bool,
    prod_metrics_enabled: bool,
}

async fn handle_capabilities() -> Json<CapabilitiesResponse> {
    Json(CapabilitiesResponse {
        protocol_version: PROTOCOL_VERSION,
        endpoints: vec![
            "GET /health",
            "GET /version",
            "GET /capabilities",
            "POST /exec",
            "POST /files/put",
            "GET /files/get",
        ],
        tcp_enabled: cfg!(feature = "tcp"),
        terminal_enabled: false,
        prod_metrics_enabled: false,
    })
}

async fn handle_exec(Json(req): Json<ExecRequest>) -> Json<ExecResponse> {
    Json(exec::run_command(req).await)
}

async fn handle_file_put(Json(req): Json<FilePutRequest>) -> Json<FilePutResponse> {
    Json(files::put_file(req).await)
}

async fn handle_file_get(
    axum::extract::Query(query): axum::extract::Query<FileGetQuery>,
) -> Json<FileGetResponse> {
    Json(files::get_file(query).await)
}
