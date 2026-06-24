//! Rust-native core library for SmolVM.
//!
//! The Rust modules are usable directly by Rust callers. The Python package
//! exposes them through public Python modules such as `smolvm_core.network` and
//! keeps the compiled PyO3 module private as `smolvm_core._ffi`.

pub mod disk;
#[cfg(target_os = "linux")]
pub mod error;
#[cfg(unix)]
pub mod firecracker;
#[cfg(target_os = "linux")]
pub mod network;
pub mod qmp;

mod python;
#[cfg(target_os = "linux")]
mod route;
#[cfg(target_os = "linux")]
mod sysctl;
#[cfg(target_os = "linux")]
mod tap;
