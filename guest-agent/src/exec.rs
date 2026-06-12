use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::AsyncReadExt;
use tokio::process::{Child, Command};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum ShellMode {
    Login,
    Raw,
}

fn default_shell() -> ShellMode {
    ShellMode::Login
}

fn default_timeout() -> u64 {
    30
}

#[derive(Debug, Deserialize)]
pub struct ExecRequest {
    pub command: String,
    #[serde(default = "default_shell")]
    pub shell: ShellMode,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u64,
    #[serde(default)]
    pub env: HashMap<String, String>,
}

#[derive(Debug, Serialize)]
pub struct ExecResponse {
    pub ok: bool,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

fn shell_command(req: &ExecRequest) -> Command {
    let mut cmd = match req.shell {
        ShellMode::Raw => {
            let mut cmd = Command::new("/bin/sh");
            cmd.arg("-c").arg(&req.command);
            cmd
        }
        ShellMode::Login => {
            let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
            let mut cmd = Command::new(shell);
            cmd.arg("-lc").arg(&req.command);
            cmd
        }
    };

    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    for (key, value) in &req.env {
        cmd.env(key, value);
    }
    configure_process_group(&mut cmd);
    cmd
}

#[cfg(unix)]
fn configure_process_group(command: &mut Command) {
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
}

#[cfg(not(unix))]
fn configure_process_group(_command: &mut Command) {}

async fn kill_child_tree(child: &mut Child) {
    #[cfg(unix)]
    {
        if let Some(pid) = child.id() {
            let rc = unsafe { libc::kill(-(pid as libc::pid_t), libc::SIGKILL) };
            if rc == 0 {
                let _ = child.wait().await;
                return;
            }
        }
    }

    let _ = child.kill().await;
    let _ = child.wait().await;
}

async fn join_output(task: Option<tokio::task::JoinHandle<Vec<u8>>>) -> Vec<u8> {
    match task {
        Some(task) => task.await.unwrap_or_default(),
        None => Vec::new(),
    }
}

pub async fn run_command(req: ExecRequest) -> ExecResponse {
    if req.command.trim().is_empty() {
        return ExecResponse {
            ok: false,
            exit_code: -1,
            stdout: String::new(),
            stderr: String::new(),
            timed_out: false,
            error: Some("missing command".to_string()),
        };
    }

    let timeout_seconds = req.timeout_seconds.max(1);
    let timeout = Duration::from_secs(timeout_seconds);
    let mut child = match shell_command(&req).spawn() {
        Ok(child) => child,
        Err(error) => {
            return ExecResponse {
                ok: false,
                exit_code: -1,
                stdout: String::new(),
                stderr: String::new(),
                timed_out: false,
                error: Some(format!("spawn failed: {error}")),
            };
        }
    };

    let stdout_task = child.stdout.take().map(|mut stdout| {
        tokio::spawn(async move {
            let mut buf = Vec::new();
            let _ = stdout.read_to_end(&mut buf).await;
            buf
        })
    });
    let stderr_task = child.stderr.take().map(|mut stderr| {
        tokio::spawn(async move {
            let mut buf = Vec::new();
            let _ = stderr.read_to_end(&mut buf).await;
            buf
        })
    });

    let exit_code;
    let mut timed_out = false;
    tokio::select! {
        status = child.wait() => {
            exit_code = status.ok().and_then(|status| status.code()).unwrap_or(-1);
        }
        _ = tokio::time::sleep(timeout) => {
            timed_out = true;
            kill_child_tree(&mut child).await;
            exit_code = -1;
        }
    }

    let stdout = join_output(stdout_task).await;
    let stderr = join_output(stderr_task).await;

    ExecResponse {
        ok: !timed_out,
        exit_code,
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
        timed_out,
        error: timed_out.then(|| format!("Command timed out after {timeout_seconds}s")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn exec_success() {
        let res = run_command(ExecRequest {
            command: "printf hello".to_string(),
            shell: ShellMode::Raw,
            timeout_seconds: 5,
            env: HashMap::new(),
        })
        .await;
        assert!(res.ok);
        assert_eq!(res.exit_code, 0);
        assert_eq!(res.stdout, "hello");
    }

    #[tokio::test]
    async fn exec_nonzero_and_stderr() {
        let res = run_command(ExecRequest {
            command: "echo oops >&2; exit 7".to_string(),
            shell: ShellMode::Raw,
            timeout_seconds: 5,
            env: HashMap::new(),
        })
        .await;
        assert!(res.ok);
        assert_eq!(res.exit_code, 7);
        assert!(res.stderr.contains("oops"));
    }

    #[tokio::test]
    async fn exec_timeout() {
        let res = run_command(ExecRequest {
            command: "sleep 10".to_string(),
            shell: ShellMode::Raw,
            timeout_seconds: 1,
            env: HashMap::new(),
        })
        .await;
        assert!(!res.ok);
        assert!(res.timed_out);
        assert_eq!(res.exit_code, -1);
        assert_eq!(res.stderr, "");
        assert_eq!(res.error.as_deref(), Some("Command timed out after 1s"));
    }
}
