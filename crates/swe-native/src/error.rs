//! Typed runtime and expression errors. No silent false / fallback.

use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ErrorKind {
    #[error("preflight")]
    Preflight,
    #[error("expression")]
    Expression,
    #[error("path")]
    Path,
    #[error("type")]
    Type,
    #[error("limit")]
    Limit,
    #[error("timeout")]
    Timeout,
    #[error("idempotency")]
    Idempotency,
    #[error("unknown_invocation")]
    UnknownInvocation,
    #[error("conflict")]
    Conflict,
    #[error("cancelled")]
    Cancelled,
    #[error("handler")]
    Handler,
    #[error("unsupported")]
    Unsupported,
    #[error("internal")]
    Internal,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{kind}: {message}")]
pub struct RuntimeError {
    pub kind: ErrorKind,
    pub message: String,
    pub path: String,
}

impl RuntimeError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            path: String::new(),
        }
    }

    pub fn with_path(mut self, path: impl Into<String>) -> Self {
        self.path = path.into();
        self
    }

    pub fn preflight(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Preflight, message)
    }

    pub fn expression(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Expression, message)
    }

    pub fn unsupported(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Unsupported, message)
    }

    pub fn limit(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Limit, message)
    }

    pub fn timeout(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Timeout, message)
    }

    pub fn idempotency(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Idempotency, message)
    }

    pub fn unknown_invocation(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::UnknownInvocation, message)
    }

    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Conflict, message)
    }

    pub fn cancelled(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Cancelled, message)
    }

    pub fn handler(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Handler, message)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Internal, message)
    }

    pub fn path_error(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Path, message)
    }
}

pub type Result<T> = std::result::Result<T, RuntimeError>;
