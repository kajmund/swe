//! Commands returned to Python. Rust never waits for async handlers.

use crate::json::Json;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunStatus {
    Running,
    Completed,
    Incomplete,
    Failed,
    Cancelled,
}

impl RunStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Completed => "completed",
            Self::Incomplete => "incomplete",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }

    pub fn is_terminal(&self) -> bool {
        !matches!(self, Self::Running)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Effect {
    InvokeHandler {
        run_id: String,
        invocation_id: String,
        node_id: String,
        branch_path: String,
        handler: String,
        input: Json,
    },
    CancelInvocation {
        invocation_id: String,
    },
}

impl Effect {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::InvokeHandler { .. } => "invoke_handler",
            Self::CancelInvocation { .. } => "cancel_invocation",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct StepOutcome {
    pub status: RunStatus,
    pub effects: Vec<Effect>,
    pub ignored: bool,
}

impl StepOutcome {
    pub fn running(effects: Vec<Effect>) -> Self {
        Self {
            status: RunStatus::Running,
            effects,
            ignored: false,
        }
    }
}
