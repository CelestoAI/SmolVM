//! Minimal QMP client exposed privately to Python.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use serde::Serialize;
use serde_json::{Map, Value, json};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

const CONNECT_POLL_INTERVAL: Duration = Duration::from_millis(50);

#[derive(Debug, thiserror::Error)]
enum QmpError {
    #[error("Timed out waiting for QMP socket")]
    SocketTimeout { socket_path: String },

    #[error("Invalid QMP greeting")]
    InvalidGreeting {
        socket_path: String,
        greeting: Value,
    },

    #[error("QMP client is not connected")]
    NotConnected { socket_path: String },

    #[error("QMP socket closed unexpectedly")]
    SocketClosed { socket_path: String },

    #[error("QMP command '{command}' failed")]
    CommandFailed {
        socket_path: String,
        command: String,
        class: Option<String>,
        desc: Option<String>,
    },

    #[error("Unexpected QMP query-jobs response")]
    UnexpectedJobs { result: Value },

    #[error("QMP job failed")]
    JobFailed {
        socket_path: String,
        job_id: String,
        job_type: String,
        status: String,
        error: String,
    },

    #[error("Timed out waiting for QMP job")]
    JobTimeout {
        socket_path: String,
        job_id: String,
        last_status: Option<String>,
    },

    #[error("{message}")]
    Json {
        socket_path: String,
        message: String,
    },

    #[error("{message}")]
    Io {
        socket_path: String,
        message: String,
    },

    #[error("{0}")]
    Other(String),
}

impl QmpError {
    fn context(&self) -> Value {
        match self {
            QmpError::SocketTimeout { socket_path }
            | QmpError::NotConnected { socket_path }
            | QmpError::SocketClosed { socket_path }
            | QmpError::Json { socket_path, .. }
            | QmpError::Io { socket_path, .. } => {
                json!({ "socket_path": socket_path })
            }
            QmpError::InvalidGreeting {
                socket_path,
                greeting,
            } => {
                json!({ "socket_path": socket_path, "greeting": greeting })
            }
            QmpError::CommandFailed {
                socket_path,
                command,
                class,
                desc,
            } => {
                json!({
                    "socket_path": socket_path,
                    "command": command,
                    "class": class,
                    "desc": desc,
                })
            }
            QmpError::UnexpectedJobs { result } => json!({ "result": result }),
            QmpError::JobFailed {
                socket_path,
                job_id,
                job_type,
                status,
                error,
            } => {
                json!({
                    "socket_path": socket_path,
                    "job_id": job_id,
                    "job_type": job_type,
                    "status": status,
                    "error": error,
                })
            }
            QmpError::JobTimeout {
                socket_path,
                job_id,
                last_status,
            } => {
                json!({
                    "socket_path": socket_path,
                    "job_id": job_id,
                    "last_status": last_status,
                })
            }
            QmpError::Other(_) => json!({}),
        }
    }
}

fn qmp_py_err(error: QmpError) -> PyErr {
    let payload = json!({
        "message": error.to_string(),
        "context": error.context(),
    });
    PyRuntimeError::new_err(payload.to_string())
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
struct QmpJob {
    job_id: String,
    job_type: String,
    status: String,
    current_progress: i64,
    total_progress: i64,
    error: Option<String>,
}

struct NativeQmpClient {
    socket_path: PathBuf,
    writer: Option<UnixStream>,
    reader: Option<BufReader<UnixStream>>,
}

impl NativeQmpClient {
    fn new(socket_path: PathBuf) -> Self {
        Self {
            socket_path,
            writer: None,
            reader: None,
        }
    }

    fn socket_path_string(&self) -> String {
        self.socket_path.display().to_string()
    }

    fn connect(&mut self, timeout: f64, read_timeout: f64) -> Result<(), QmpError> {
        if self.writer.is_some() {
            return Ok(());
        }

        let socket_path = self.socket_path.clone();
        let socket_path_string = self.socket_path_string();
        let timeout_duration = duration_from_seconds(timeout);
        let probe_timeout = timeout_duration.min(Duration::from_secs(1));
        let deadline = Instant::now() + timeout_duration;

        loop {
            match UnixStream::connect(&socket_path) {
                Ok(stream) => {
                    stream
                        .set_read_timeout(Some(probe_timeout))
                        .map_err(|e| QmpError::Io {
                            socket_path: socket_path_string.clone(),
                            message: e.to_string(),
                        })?;
                    stream
                        .set_write_timeout(Some(probe_timeout))
                        .map_err(|e| QmpError::Io {
                            socket_path: socket_path_string.clone(),
                            message: e.to_string(),
                        })?;
                    let reader_stream = stream.try_clone().map_err(|e| QmpError::Io {
                        socket_path: socket_path_string.clone(),
                        message: e.to_string(),
                    })?;
                    self.reader = Some(BufReader::new(reader_stream));
                    self.writer = Some(stream);
                    break;
                }
                Err(_) if Instant::now() < deadline => {
                    thread::sleep(CONNECT_POLL_INTERVAL);
                }
                Err(_) => {
                    return Err(QmpError::SocketTimeout {
                        socket_path: socket_path_string,
                    });
                }
            }
        }

        let handshake = (|| {
            let greeting = self.read_message()?;
            if !greeting
                .as_object()
                .is_some_and(|obj| obj.contains_key("QMP"))
            {
                return Err(QmpError::InvalidGreeting {
                    socket_path: self.socket_path_string(),
                    greeting,
                });
            }
            self.execute("qmp_capabilities", None)?;
            self.set_io_timeout(duration_from_seconds(read_timeout))?;
            Ok(())
        })();

        if handshake.is_err() {
            self.close();
        }
        handshake
    }

    fn set_io_timeout(&mut self, timeout: Duration) -> Result<(), QmpError> {
        let socket_path = self.socket_path_string();
        if let Some(writer) = self.writer.as_ref() {
            writer
                .set_read_timeout(Some(timeout))
                .map_err(|e| QmpError::Io {
                    socket_path: socket_path.clone(),
                    message: e.to_string(),
                })?;
            writer
                .set_write_timeout(Some(timeout))
                .map_err(|e| QmpError::Io {
                    socket_path: socket_path.clone(),
                    message: e.to_string(),
                })?;
        }
        if let Some(reader) = self.reader.as_ref() {
            reader
                .get_ref()
                .set_read_timeout(Some(timeout))
                .map_err(|e| QmpError::Io {
                    socket_path: socket_path.clone(),
                    message: e.to_string(),
                })?;
        }
        Ok(())
    }

    fn ensure_connected(&mut self) -> Result<(), QmpError> {
        if self.writer.is_none() {
            self.connect(5.0, 30.0)?;
        }
        Ok(())
    }

    fn execute(&mut self, command: &str, arguments: Option<&Value>) -> Result<Value, QmpError> {
        self.ensure_connected()?;

        let mut payload = Map::new();
        payload.insert("execute".to_string(), Value::String(command.to_string()));
        if let Some(args) = arguments {
            payload.insert("arguments".to_string(), args.clone());
        }
        let mut payload_line =
            serde_json::to_vec(&Value::Object(payload)).map_err(|e| QmpError::Json {
                socket_path: self.socket_path_string(),
                message: e.to_string(),
            })?;
        payload_line.push(b'\n');

        let socket_path = self.socket_path_string();
        let writer = self.writer.as_mut().ok_or_else(|| QmpError::NotConnected {
            socket_path: socket_path.clone(),
        })?;
        writer.write_all(&payload_line).map_err(|e| QmpError::Io {
            socket_path: socket_path.clone(),
            message: e.to_string(),
        })?;
        writer.flush().map_err(|e| QmpError::Io {
            socket_path: socket_path.clone(),
            message: e.to_string(),
        })?;

        loop {
            let message = self.read_message()?;
            match command_return_from_message(command, &message, &self.socket_path_string())? {
                Some(result) => return Ok(result),
                None => continue,
            }
        }
    }

    fn stop_vm(&mut self) -> Result<(), QmpError> {
        self.execute("stop", None).map(|_| ())
    }

    fn cont(&mut self) -> Result<(), QmpError> {
        self.execute("cont", None).map(|_| ())
    }

    fn snapshot_save(
        &mut self,
        job_id: &str,
        tag: &str,
        vmstate: &str,
        devices: &[String],
    ) -> Result<(), QmpError> {
        self.execute(
            "snapshot-save",
            Some(&json!({
                "job-id": job_id,
                "tag": tag,
                "vmstate": vmstate,
                "devices": devices,
            })),
        )
        .map(|_| ())
    }

    fn snapshot_load(
        &mut self,
        job_id: &str,
        tag: &str,
        vmstate: &str,
        devices: &[String],
    ) -> Result<(), QmpError> {
        self.execute(
            "snapshot-load",
            Some(&json!({
                "job-id": job_id,
                "tag": tag,
                "vmstate": vmstate,
                "devices": devices,
            })),
        )
        .map(|_| ())
    }

    fn snapshot_delete(
        &mut self,
        job_id: &str,
        tag: &str,
        devices: &[String],
    ) -> Result<(), QmpError> {
        self.execute(
            "snapshot-delete",
            Some(&json!({
                "job-id": job_id,
                "tag": tag,
                "devices": devices,
            })),
        )
        .map(|_| ())
    }

    fn blockdev_snapshot_internal_sync(
        &mut self,
        device: &str,
        name: &str,
    ) -> Result<(), QmpError> {
        self.execute(
            "blockdev-snapshot-internal-sync",
            Some(&json!({
                "device": device,
                "name": name,
            })),
        )
        .map(|_| ())
    }

    fn blockdev_snapshot_delete_internal_sync(
        &mut self,
        device: &str,
        name: &str,
    ) -> Result<(), QmpError> {
        self.execute(
            "blockdev-snapshot-delete-internal-sync",
            Some(&json!({
                "device": device,
                "name": name,
            })),
        )
        .map(|_| ())
    }

    fn query_jobs(&mut self) -> Result<Vec<QmpJob>, QmpError> {
        let result = self.execute("query-jobs", None)?;
        parse_jobs(result)
    }

    fn dismiss_job(&mut self, job_id: &str) -> Result<(), QmpError> {
        self.execute("job-dismiss", Some(&json!({ "id": job_id })))
            .map(|_| ())
    }

    fn wait_for_job(
        &mut self,
        job_id: &str,
        timeout: f64,
        poll_interval: f64,
    ) -> Result<QmpJob, QmpError> {
        let socket_path = self.socket_path_string();
        let deadline = Instant::now() + duration_from_seconds(timeout);
        let poll_interval = duration_from_seconds(poll_interval);
        let mut last_job: Option<QmpJob> = None;

        while Instant::now() < deadline {
            for job in self.query_jobs()? {
                if job.job_id != job_id {
                    continue;
                }
                last_job = Some(job.clone());
                if job.status == "concluded" {
                    if let Some(error) = job.error.clone() {
                        return Err(QmpError::JobFailed {
                            socket_path,
                            job_id: job.job_id,
                            job_type: job.job_type,
                            status: job.status,
                            error,
                        });
                    }
                    let _ = self.dismiss_job(job_id);
                    return Ok(job);
                }
                break;
            }
            thread::sleep(poll_interval);
        }

        Err(QmpError::JobTimeout {
            socket_path,
            job_id: job_id.to_string(),
            last_status: last_job.map(|job| job.status),
        })
    }

    fn close(&mut self) {
        self.reader = None;
        self.writer = None;
    }

    fn read_message(&mut self) -> Result<Value, QmpError> {
        let socket_path = self.socket_path_string();
        let reader = self.reader.as_mut().ok_or_else(|| QmpError::NotConnected {
            socket_path: socket_path.clone(),
        })?;
        let mut line = String::new();
        let bytes_read = reader.read_line(&mut line).map_err(|e| QmpError::Io {
            socket_path: socket_path.clone(),
            message: e.to_string(),
        })?;
        if bytes_read == 0 {
            return Err(QmpError::SocketClosed { socket_path });
        }
        serde_json::from_str(&line).map_err(|e| QmpError::Json {
            socket_path,
            message: e.to_string(),
        })
    }
}

fn command_return_from_message(
    command: &str,
    message: &Value,
    socket_path: &str,
) -> Result<Option<Value>, QmpError> {
    let Some(obj) = message.as_object() else {
        return Ok(None);
    };
    if obj.contains_key("event") {
        return Ok(None);
    }
    if let Some(error) = obj.get("error").and_then(Value::as_object) {
        return Err(QmpError::CommandFailed {
            socket_path: socket_path.to_string(),
            command: command.to_string(),
            class: error
                .get("class")
                .and_then(Value::as_str)
                .map(str::to_string),
            desc: error
                .get("desc")
                .and_then(Value::as_str)
                .map(str::to_string),
        });
    }
    Ok(obj.get("return").cloned())
}

fn parse_jobs(result: Value) -> Result<Vec<QmpJob>, QmpError> {
    let Some(items) = result.as_array() else {
        return Err(QmpError::UnexpectedJobs { result });
    };

    let mut jobs = Vec::with_capacity(items.len());
    for item in items {
        let Some(job) = item.as_object() else {
            return Err(QmpError::UnexpectedJobs {
                result: result.clone(),
            });
        };
        jobs.push(QmpJob {
            job_id: value_to_string(job.get("id")),
            job_type: value_to_string(job.get("type")),
            status: value_to_string(job.get("status")),
            current_progress: value_to_i64(job.get("current-progress")).unwrap_or(0),
            total_progress: value_to_i64(job.get("total-progress")).unwrap_or(0),
            error: job.get("error").map(|value| value_to_string(Some(value))),
        });
    }
    Ok(jobs)
}

#[cfg(test)]
fn wait_for_job_with<Q, D>(
    socket_path: &str,
    job_id: &str,
    timeout: Duration,
    poll_interval: Duration,
    mut query_jobs: Q,
    mut dismiss_job: D,
) -> Result<QmpJob, QmpError>
where
    Q: FnMut() -> Result<Vec<QmpJob>, QmpError>,
    D: FnMut(&str) -> Result<(), QmpError>,
{
    let deadline = Instant::now() + timeout;
    let mut last_job: Option<QmpJob> = None;

    while Instant::now() < deadline {
        for job in query_jobs()? {
            if job.job_id != job_id {
                continue;
            }
            last_job = Some(job.clone());
            if job.status == "concluded" {
                if let Some(error) = job.error.clone() {
                    return Err(QmpError::JobFailed {
                        socket_path: socket_path.to_string(),
                        job_id: job.job_id,
                        job_type: job.job_type,
                        status: job.status,
                        error,
                    });
                }
                let _ = dismiss_job(job_id);
                return Ok(job);
            }
            break;
        }
        thread::sleep(poll_interval);
    }

    Err(QmpError::JobTimeout {
        socket_path: socket_path.to_string(),
        job_id: job_id.to_string(),
        last_status: last_job.map(|job| job.status),
    })
}

fn value_to_string(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Null) | None => "null".to_string(),
        Some(value) => value.to_string(),
    }
}

fn value_to_i64(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_u64().and_then(|value| i64::try_from(value).ok())),
        Some(Value::String(value)) => value.parse().ok(),
        _ => None,
    }
}

fn duration_from_seconds(seconds: f64) -> Duration {
    if seconds.is_finite() && seconds > 0.0 {
        Duration::from_secs_f64(seconds)
    } else {
        Duration::ZERO
    }
}

#[pyclass(name = "_QmpClient")]
pub(crate) struct PyQmpClient {
    inner: Mutex<NativeQmpClient>,
}

#[pymethods]
impl PyQmpClient {
    #[new]
    fn new(socket_path: String) -> Self {
        Self {
            inner: Mutex::new(NativeQmpClient::new(PathBuf::from(socket_path))),
        }
    }

    fn connect(&self, py: Python<'_>, timeout: f64, read_timeout: f64) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.connect(timeout, read_timeout)
        })
        .map_err(qmp_py_err)
    }

    fn execute(
        &self,
        py: Python<'_>,
        command: String,
        arguments_json: Option<String>,
    ) -> PyResult<String> {
        let arguments = arguments_json
            .as_deref()
            .map(|value| serde_json::from_str(value))
            .transpose()
            .map_err(|e| {
                qmp_py_err(QmpError::Json {
                    socket_path: self.socket_path_for_error(),
                    message: e.to_string(),
                })
            })?;

        py.detach(|| {
            let mut client = self.lock_inner()?;
            let result = client.execute(&command, arguments.as_ref())?;
            serde_json::to_string(&result).map_err(|e| QmpError::Json {
                socket_path: client.socket_path_string(),
                message: e.to_string(),
            })
        })
        .map_err(qmp_py_err)
    }

    fn stop_vm(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.stop_vm()
        })
        .map_err(qmp_py_err)
    }

    fn cont(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.cont()
        })
        .map_err(qmp_py_err)
    }

    fn snapshot_save(
        &self,
        py: Python<'_>,
        job_id: String,
        tag: String,
        vmstate: String,
        devices: Vec<String>,
    ) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.snapshot_save(&job_id, &tag, &vmstate, &devices)
        })
        .map_err(qmp_py_err)
    }

    fn snapshot_load(
        &self,
        py: Python<'_>,
        job_id: String,
        tag: String,
        vmstate: String,
        devices: Vec<String>,
    ) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.snapshot_load(&job_id, &tag, &vmstate, &devices)
        })
        .map_err(qmp_py_err)
    }

    fn snapshot_delete(
        &self,
        py: Python<'_>,
        job_id: String,
        tag: String,
        devices: Vec<String>,
    ) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.snapshot_delete(&job_id, &tag, &devices)
        })
        .map_err(qmp_py_err)
    }

    fn blockdev_snapshot_internal_sync(
        &self,
        py: Python<'_>,
        device: String,
        name: String,
    ) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.blockdev_snapshot_internal_sync(&device, &name)
        })
        .map_err(qmp_py_err)
    }

    fn blockdev_snapshot_delete_internal_sync(
        &self,
        py: Python<'_>,
        device: String,
        name: String,
    ) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.blockdev_snapshot_delete_internal_sync(&device, &name)
        })
        .map_err(qmp_py_err)
    }

    fn query_jobs(&self, py: Python<'_>) -> PyResult<String> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            let jobs = client.query_jobs()?;
            serde_json::to_string(&jobs).map_err(|e| QmpError::Json {
                socket_path: client.socket_path_string(),
                message: e.to_string(),
            })
        })
        .map_err(qmp_py_err)
    }

    fn dismiss_job(&self, py: Python<'_>, job_id: String) -> PyResult<()> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            client.dismiss_job(&job_id)
        })
        .map_err(qmp_py_err)
    }

    fn wait_for_job(
        &self,
        py: Python<'_>,
        job_id: String,
        timeout: f64,
        poll_interval: f64,
    ) -> PyResult<String> {
        py.detach(|| {
            let mut client = self.lock_inner()?;
            let job = client.wait_for_job(&job_id, timeout, poll_interval)?;
            serde_json::to_string(&job).map_err(|e| QmpError::Json {
                socket_path: client.socket_path_string(),
                message: e.to_string(),
            })
        })
        .map_err(qmp_py_err)
    }

    fn close(&self) -> PyResult<()> {
        let mut client = self.lock_inner().map_err(qmp_py_err)?;
        client.close();
        Ok(())
    }
}

impl PyQmpClient {
    fn lock_inner(&self) -> Result<std::sync::MutexGuard<'_, NativeQmpClient>, QmpError> {
        self.inner
            .lock()
            .map_err(|_| QmpError::Other("QMP client lock is poisoned".to_string()))
    }

    fn socket_path_for_error(&self) -> String {
        self.inner
            .lock()
            .map(|client| client.socket_path_string())
            .unwrap_or_else(|_| String::new())
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyQmpClient>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_response_ignores_events_before_return() {
        let event = json!({"event": "STOP", "data": {}});
        let response = json!({"return": {"running": false, "status": "paused"}});

        assert!(
            command_return_from_message("query-status", &event, "/tmp/qmp.sock")
                .unwrap()
                .is_none()
        );
        assert_eq!(
            command_return_from_message("query-status", &response, "/tmp/qmp.sock").unwrap(),
            Some(json!({"running": false, "status": "paused"}))
        );
    }

    #[test]
    fn command_response_surfaces_qmp_error_details() {
        let response = json!({
            "error": {
                "class": "GenericError",
                "desc": "snapshot tag missing"
            }
        });

        let error =
            command_return_from_message("snapshot-delete", &response, "/tmp/qmp.sock").unwrap_err();
        assert!(matches!(
            error,
            QmpError::CommandFailed {
                command,
                class: Some(class),
                desc: Some(desc),
                ..
            } if command == "snapshot-delete"
                && class == "GenericError"
                && desc == "snapshot tag missing"
        ));
    }

    #[test]
    fn parses_qmp_jobs() {
        let jobs = parse_jobs(json!([
            {
                "id": "job0",
                "type": "snapshot-save",
                "status": "running",
                "current-progress": 0,
                "total-progress": 1
            }
        ]))
        .unwrap();

        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].job_id, "job0");
        assert_eq!(jobs[0].job_type, "snapshot-save");
    }

    #[test]
    fn wait_for_job_returns_concluded_job_and_dismisses() {
        let mut calls = 0;
        let mut dismissed: Option<String> = None;
        let job = wait_for_job_with(
            "/tmp/qmp.sock",
            "job0",
            Duration::from_secs(1),
            Duration::from_millis(1),
            || {
                calls += 1;
                Ok(if calls == 1 {
                    vec![QmpJob {
                        job_id: "job0".to_string(),
                        job_type: "snapshot-save".to_string(),
                        status: "running".to_string(),
                        current_progress: 0,
                        total_progress: 1,
                        error: None,
                    }]
                } else {
                    vec![QmpJob {
                        job_id: "job0".to_string(),
                        job_type: "snapshot-save".to_string(),
                        status: "concluded".to_string(),
                        current_progress: 1,
                        total_progress: 1,
                        error: None,
                    }]
                })
            },
            |job_id| {
                dismissed = Some(job_id.to_string());
                Ok(())
            },
        )
        .unwrap();

        assert_eq!(job.status, "concluded");
        assert_eq!(dismissed, Some("job0".to_string()));
    }

    #[test]
    fn wait_for_job_raises_on_job_error() {
        let error = wait_for_job_with(
            "/tmp/qmp.sock",
            "job0",
            Duration::from_secs(1),
            Duration::from_millis(1),
            || {
                Ok(vec![QmpJob {
                    job_id: "job0".to_string(),
                    job_type: "snapshot-delete".to_string(),
                    status: "concluded".to_string(),
                    current_progress: 1,
                    total_progress: 1,
                    error: Some("snapshot tag missing".to_string()),
                }])
            },
            |_| Ok(()),
        )
        .unwrap_err();

        assert!(matches!(
            error,
            QmpError::JobFailed {
                job_id,
                error,
                ..
            } if job_id == "job0" && error == "snapshot tag missing"
        ));
    }

    #[test]
    fn wait_for_job_times_out_with_last_status() {
        let error = wait_for_job_with(
            "/tmp/qmp.sock",
            "job0",
            Duration::from_millis(1),
            Duration::from_millis(1),
            || {
                Ok(vec![QmpJob {
                    job_id: "job0".to_string(),
                    job_type: "snapshot-save".to_string(),
                    status: "running".to_string(),
                    current_progress: 0,
                    total_progress: 1,
                    error: None,
                }])
            },
            |_| Ok(()),
        )
        .unwrap_err();

        assert!(matches!(
            error,
            QmpError::JobTimeout {
                last_status: Some(status),
                ..
            } if status == "running"
        ));
    }
}
