use indexmap::IndexMap;

use crate::definition::{CompiledDefinition, MapSchedule};
use crate::effects::{Effect, RunStatus};
use crate::events::{Lineage, MemoryEventSink};
use crate::json::Json;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScopeKind {
    Root,
    Fork,
    Branch,
    Map,
    MapChild,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkStatus {
    Running,
    Completed,
    Failed,
    Cancelled,
    /// Ready-map item that has not been admitted yet.
    Waiting,
}

#[derive(Clone, Debug)]
pub struct BranchState {
    pub name: String,
    pub start_node: String,
    pub status: WorkStatus,
    pub result: Option<Json>,
}

#[derive(Clone, Debug)]
pub struct ChildState {
    pub index: usize,
    pub scope_id: String,
    pub status: WorkStatus,
    pub result: Option<Json>,
    pub item_id: Option<String>,
    pub pinned_serial: Option<u64>,
}

#[derive(Clone, Debug)]
pub struct Scope {
    pub id: String,
    pub kind: ScopeKind,
    pub parent: Option<String>,
    pub locals: IndexMap<String, Json>,
    pub depth: u32,
    pub loop_epoch: u32,
    pub visited: indexmap::IndexSet<String>,
    pub owner_node: Option<String>,
    pub join_node: Option<String>,
    pub after_node: Option<String>,
    pub branch_name: Option<String>,
    pub branch_path: String,
    pub fork_instance_id: Option<String>,
    pub map_instance_id: Option<String>,
    pub branches: IndexMap<String, BranchState>,
    pub remaining: usize,
    pub items: Vec<Json>,
    pub children: Vec<ChildState>,
    pub concurrency: u32,
    pub next_child: usize,
    pub map_schedule: MapSchedule,
    /// Unmet dependency counts for a ready map, aligned with `items`.
    pub unmet: Vec<u32>,
    /// Dependents of each ready-map item, aligned with `items`.
    pub dependents: Vec<Vec<usize>>,
    /// Serial reserved for the first node scheduled in this scope.
    pub pinned_serial: Option<u64>,
    pub item: Option<Json>,
    pub index: Option<usize>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InvocationStatus {
    Pending,
    Completed,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug)]
pub struct Invocation {
    pub id: String,
    pub instance_id: String,
    pub node_id: String,
    pub scope_id: String,
    pub branch_name: Option<String>,
    pub handler: String,
    pub status: InvocationStatus,
    pub output: Option<Json>,
}

pub struct Machine {
    pub run_id: String,
    pub definition: CompiledDefinition,
    pub status: RunStatus,
    pub input: Json,
    pub started_ms: u64,
    pub deadline_ms: u64,
    pub now_ms: u64,
    pub steps_used: u32,
    pub next_seq: u64,
    pub next_serial: u64,
    pub root_id: String,
    pub scopes: IndexMap<String, Scope>,
    pub invocations: IndexMap<String, Invocation>,
    pub sink: MemoryEventSink,
    pub effects: Vec<Effect>,
    pub error: Option<crate::error::RuntimeError>,
    pub work: Vec<(String, String, Option<String>)>,
    pub pumping: bool,
}

impl Scope {
    pub fn lineage(&self) -> Lineage {
        Lineage {
            branch_path: self.branch_path.clone(),
            fork_instance_id: self.fork_instance_id.clone(),
            map_instance_id: self.map_instance_id.clone(),
            loop_epoch: self.loop_epoch,
        }
    }
}

impl Machine {
    pub fn scope(&self, id: &str) -> Option<&Scope> {
        self.scopes.get(id)
    }

    pub fn new_serial(&mut self) -> u64 {
        let value = self.next_serial;
        self.next_serial += 1;
        value
    }

    pub fn instance_id(&mut self, node_id: &str, scope: &Scope) -> String {
        // Ready-map children pin a serial in input order at start_map so
        // invocation identity does not follow admission order.
        let pinned = self
            .scopes
            .get_mut(&scope.id)
            .and_then(|stored| stored.pinned_serial.take());
        let serial = pinned.unwrap_or_else(|| self.new_serial());
        format!(
            "{}:{}:{}:{}:{}",
            self.run_id, node_id, scope.branch_path, scope.loop_epoch, serial
        )
    }
}
