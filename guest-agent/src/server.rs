use axum::Router;

pub const DEFAULT_LISTEN: &str = "vsock://1024";

pub async fn serve_listen_addr(app: Router, listen: &str) {
    if let Some(port) = listen.strip_prefix("vsock://") {
        let port: u32 = port.parse().expect("vsock port must be a number");
        serve_vsock(app, port).await;
    } else if let Some(addr) = listen.strip_prefix("tcp://") {
        serve_tcp(app, addr).await;
    } else {
        eprintln!(
            "Invalid listen address: {listen}. Use vsock://PORT{}.",
            if cfg!(feature = "tcp") {
                " or tcp://HOST:PORT"
            } else {
                ""
            },
        );
        std::process::exit(1);
    }
}

#[cfg(all(feature = "vsock", target_os = "linux"))]
pub async fn serve_vsock(app: Router, port: u32) {
    use tokio_vsock::{VsockAddr, VsockListener};
    use tower::ServiceExt;

    let addr = VsockAddr::new(u32::MAX, port);
    let mut listener = VsockListener::bind(addr).expect("failed to bind vsock");
    tracing::info!("Listening on vsock://{port}");

    loop {
        match listener.accept().await {
            Ok((stream, addr)) => {
                let app = app.clone();
                tokio::spawn(async move {
                    let io = hyper_util::rt::TokioIo::new(stream);
                    let service = hyper::service::service_fn(move |req| {
                        let app = app.clone();
                        async move { app.oneshot(req).await.map_err(|e| match e {}) }
                    });
                    if let Err(error) = hyper::server::conn::http1::Builder::new()
                        .serve_connection(io, service)
                        .await
                    {
                        tracing::error!("vsock connection error from {addr:?}: {error}");
                    }
                });
            }
            Err(error) => tracing::error!("vsock accept error: {error}"),
        }
    }
}

#[cfg(not(all(feature = "vsock", target_os = "linux")))]
pub async fn serve_vsock(_app: Router, _port: u32) {
    eprintln!("vsock support is only available in Linux builds with the vsock feature enabled.");
    std::process::exit(1);
}

#[cfg(feature = "tcp")]
pub async fn serve_tcp(app: Router, addr: &str) {
    tracing::info!("Listening on tcp://{addr}");
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("failed to bind TCP listener");
    axum::serve(listener, app).await.expect("TCP server failed");
}

#[cfg(not(feature = "tcp"))]
pub async fn serve_tcp(_app: Router, _addr: &str) {
    eprintln!("TCP listener support is disabled in public SmolVM guest-agent builds.");
    std::process::exit(1);
}
