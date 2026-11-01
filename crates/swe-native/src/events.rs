//! In-memory typed run events. Not durable. One history, one sink.

use crate::error::Result;
use crate::json::Json;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind {
    RunStarted,
    NodeScheduled,
    NodeStarted,
    NodeCompleted,
    NodeFailed,
    BranchSpawned,
    JoinSatisfied,
    DecisionTaken,
    ProviderInvoked,
    ProviderCompleted,
    RunCompleted,
    RunFailed,
    RunCancelled,
}

impl EventKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::RunStarted => "RunStarted",
            Self::NodeScheduled => "NodeScheduled",
            Self::NodeStarted => "NodeStarted",
            Self::NodeCompleted => "NodeCompleted",
            Self::NodeFailed => "NodeFailed",
            Self::BranchSpawned => "BranchSpawned",
            Self::JoinSatisfied => "JoinSatisfied",
            Self::DecisionTaken => "DecisionTaken",
            Self::ProviderInvoked => "ProviderInvoked",
            Self::ProviderCompleted => "ProviderCompleted",
            Self::RunCompleted => "RunCompleted",
            Self::RunFailed => "RunFailed",
            Self::RunCancelled => "RunCancelled",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Lineage {
    pub branch_path: String,
    pub fork_instance_id: Option<String>,
    pub map_instance_id: Option<String>,
    pub loop_epoch: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RuntimeEvent {
    pub kind: EventKind,
    pub run_id: String,
    pub seq: u64,
    pub definition_sha256: String,
    pub node_instance_id: Option<String>,
    pub node_id: Option<String>,
    pub lineage: Option<Lineage>,
    pub result_ref: Option<String>,
    pub payload: Json,
}

/// Narrow append-only boundary for a durable sink supplied by the host.
pub trait EventSink {
    fn append(&mut self, event: RuntimeEvent) -> Result<()>;
}

#[derive(Clone, Debug, Default)]
pub struct MemoryEventSink {
    pub events: Vec<RuntimeEvent>,
}

impl EventSink for MemoryEventSink {
    fn append(&mut self, event: RuntimeEvent) -> Result<()> {
        self.events.push(event);
        Ok(())
    }
}

impl MemoryEventSink {
    pub fn new() -> Self {
        Self { events: Vec::new() }
    }
}
