use clap::Parser;
use smolvm_guest_agent::{handler, server, terminal};

#[derive(Parser)]
#[command(name = "smolvm-guest-agent", about = "SmolVM guest control agent")]
struct Args {
    /// Listen address. Public builds default to vsock://1024.
    #[arg(long, default_value = server::DEFAULT_LISTEN)]
    listen: String,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .compact()
        .init();

    let args = Args::parse();
    let app = handler::router();
    if args.listen.starts_with("vsock://") {
        tokio::select! {
            _ = server::serve_listen_addr(app, &args.listen) => {},
            _ = terminal::serve_vsock_terminal(terminal::DEFAULT_TERMINAL_PORT) => {},
        }
    } else {
        server::serve_listen_addr(app, &args.listen).await;
    }
}
