//! In-memory SWD 1.0 machine. Python owns handlers; Rust owns routing.

mod complete;
mod context;
mod schedule;
mod state;

use crate::canonicalize::canonicalize;
use crate::definition::CompiledDefinition;
use crate::effects::{RunStatus, StepOutcome};
use crate::error::{Result, RuntimeError};
use crate::events::{EventKind, MemoryEventSink, RuntimeEvent};
use crate::json::Json;
use crate::preflight::reject_provider_document;

use self::state::{Machine, Scope, ScopeKind};

pub use self::state::Machine as WorkflowMachine;

impl Machine {
    pub fn start(
        definition: CompiledDefinition,
        run_id: String,
        input: Json,
        now_ms: u64,
    ) -> Result<(Self, StepOutcome)> {
        reject_provider_document(&definition.document)?;
        let input = canonicalize(&input)?;
        let deadline_ms =
            now_ms.saturating_add(u64::from(definition.limits.timeout_seconds) * 1000);
        let root_id = format!("{run_id}:root");
        let mut machine = Self {
            run_id: run_id.clone(),
            definition,
            status: RunStatus::Running,
            input,
            started_ms: now_ms,
            deadline_ms,
            now_ms,
            steps_used: 0,
            next_seq: 1,
            next_serial: 1,
            root_id: root_id.clone(),
            scopes: indexmap::IndexMap::new(),
            invocations: indexmap::IndexMap::new(),
            sink: MemoryEventSink::new(),
            effects: Vec::new(),
            error: None,
            work: Vec::new(),
            pumping: false,
        };
        machine.scopes.insert(
            root_id.clone(),
            Scope {
                id: root_id.clone(),
                kind: ScopeKind::Root,
                parent: None,
                locals: indexmap::IndexMap::new(),
                depth: 0,
                loop_epoch: 0,
                owner_node: None,
                join_node: None,
                after_node: None,
                branch_name: None,
                branch_path: String::new(),
                fork_instance_id: None,
                map_instance_id: None,
                branches: indexmap::IndexMap::new(),
                remaining: 0,
                items: Vec::new(),
                children: Vec::new(),
                concurrency: 1,
                next_child: 0,
                item: None,
                index: None,
            },
        );
        machine.emit(
            EventKind::RunStarted,
            None,
            None,
            None,
            None,
            Json::object([("run_id".into(), Json::from(run_id.as_str()))]),
        )?;
        let entrypoint = machine.definition.entrypoint.clone();
        let outcome = machine.protect(|machine| machine.schedule(&entrypoint, &root_id, None));
        Ok((machine, outcome))
    }

    pub fn complete(&mut self, invocation_id: &str, output: Json, now_ms: u64) -> StepOutcome {
        self.now_ms = now_ms;
        let output = match canonicalize(&output) {
            Ok(value) => value,
            Err(error) => return self.protect(move |machine| machine.fail_run(error)),
        };
        self.protect(|machine| {
            machine.complete_invocation(invocation_id, output)?;
            Ok(())
        })
    }

    pub fn fail(&mut self, invocation_id: &str, error: RuntimeError, now_ms: u64) -> StepOutcome {
        self.now_ms = now_ms;
        self.protect(|machine| {
            machine.fail_invocation(invocation_id, error)?;
            Ok(())
        })
    }

    pub fn cancel(&mut self, now_ms: u64) -> StepOutcome {
        self.now_ms = now_ms;
        self.protect(Machine::cancel_run)
    }

    pub fn events(&self) -> &[RuntimeEvent] {
        &self.sink.events
    }

    pub fn status(&self) -> RunStatus {
        self.status.clone()
    }

    pub fn error(&self) -> Option<&RuntimeError> {
        self.error.as_ref()
    }

    pub fn output(&self) -> Json {
        self.scopes
            .get(&self.root_id)
            .map(|scope| Json::Object(scope.locals.clone()))
            .unwrap_or_else(Json::empty_object)
    }

    fn protect(&mut self, op: impl FnOnce(&mut Self) -> Result<()>) -> StepOutcome {
        match op(self) {
            Ok(()) => self.drain(),
            Err(error) => {
                if !self.status.is_terminal() {
                    let _ = self.fail_run(error);
                } else if self.error.is_none() {
                    self.error = Some(error);
                }
                self.drain()
            }
        }
    }

    fn drain(&mut self) -> StepOutcome {
        let effects = std::mem::take(&mut self.effects);
        StepOutcome {
            status: self.status.clone(),
            effects,
            ignored: false,
        }
    }

    pub fn emit(
        &mut self,
        kind: EventKind,
        node_instance_id: Option<String>,
        node_id: Option<String>,
        lineage: Option<crate::events::Lineage>,
        result_ref: Option<String>,
        payload: Json,
    ) -> Result<()> {
        use crate::events::EventSink;
        let event = RuntimeEvent {
            kind,
            run_id: self.run_id.clone(),
            seq: self.next_seq,
            definition_sha256: self.definition.sha256.clone(),
            node_instance_id,
            node_id,
            lineage,
            result_ref,
            payload,
        };
        self.next_seq += 1;
        self.sink.append(event)
    }
}

pub fn start_run(
    document: Json,
    sha256: &str,
    run_id: String,
    input: Json,
    now_ms: u64,
) -> Result<(Machine, StepOutcome)> {
    let definition = CompiledDefinition::load(document, sha256)?;
    Machine::start(definition, run_id, input, now_ms)
}
