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

use self::state::{Machine, Scope, ScopeKind};

pub use self::state::Machine as WorkflowMachine;

impl Machine {
    pub fn start(
        definition: CompiledDefinition,
        run_id: String,
        input: Json,
        now_ms: u64,
    ) -> Result<(Self, StepOutcome)> {
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
                visited: indexmap::IndexSet::new(),
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

    pub fn tick(&mut self, now_ms: u64) -> StepOutcome {
        self.now_ms = now_ms;
        if self.status.is_terminal() {
            return self.drain();
        }
        self.protect(|machine| machine.check_timeout())
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canonicalize::sha256_hex;
    use crate::convert::parse_json_str;
    use crate::definition::CompiledDefinition;
    use crate::effects::Effect;
    use crate::error::ErrorKind;
    use crate::json::Json;

    const DOCUMENT: &str = r#"{
        "version": "1.0",
        "name": "provider_test",
        "entrypoint": "evaluate",
        "limits": {"max_steps": 10, "max_depth": 2, "timeout_seconds": 30},
        "nodes": {
            "evaluate": {
                "type": "decision",
                "evaluator": "provider",
                "provider": "fake",
                "prompt_key": "demo.decision.user",
                "input": {"question": "$.input.question"},
                "choices": ["sufficient", "insufficient"],
                "routes": {"sufficient": "done", "insufficient": "incomplete"},
                "cache_policy": "exact"
            },
            "done": {"type": "end", "status": "completed"},
            "incomplete": {"type": "end", "status": "incomplete"}
        }
    }"#;

    fn start() -> (WorkflowMachine, StepOutcome) {
        let document = parse_json_str(DOCUMENT).unwrap();
        let sha = sha256_hex(&document).unwrap();
        let input = parse_json_str(r#"{"question": "is it sufficient?"}"#).unwrap();
        start_run(document, &sha, "run-test".to_string(), input, 1_000).unwrap()
    }

    fn invocation_id(outcome: &StepOutcome) -> String {
        match &outcome.effects[0] {
            Effect::InvokeProvider { invocation_id, .. } => invocation_id.clone(),
            other => panic!("unexpected effect: {other:?}"),
        }
    }

    fn provider_document(routes: &str, choices: &str) -> String {
        format!(
            r#"{{"version":"1.0","name":"provider_test","entrypoint":"evaluate","limits":{{"max_steps":10,"max_depth":2,"timeout_seconds":30}},"nodes":{{"evaluate":{{"type":"decision","evaluator":"provider","provider":"fake","prompt_key":"demo.decision.user","input":{{}},"choices":{choices},"routes":{routes}}},"done":{{"type":"end","status":"completed"}}}}}}"#
        )
    }

    fn load(raw: &str) -> Result<CompiledDefinition> {
        let document = parse_json_str(raw)?;
        let sha = sha256_hex(&document)?;
        CompiledDefinition::load(document, &sha)
    }

    #[test]
    fn provider_decision_emits_invoke_provider_effect() {
        let (_machine, outcome) = start();
        match outcome.effects.as_slice() {
            [Effect::InvokeProvider {
                provider,
                prompt_key,
                input,
                choices,
                cache_policy,
                ..
            }] => {
                assert_eq!(provider, "fake");
                assert_eq!(prompt_key, "demo.decision.user");
                assert_eq!(
                    input.get("question").and_then(Json::as_str),
                    Some("is it sufficient?")
                );
                assert_eq!(
                    choices,
                    &vec!["sufficient".to_string(), "insufficient".to_string()]
                );
                assert_eq!(*cache_policy, "exact");
            }
            other => panic!("unexpected effects: {other:?}"),
        }
    }

    #[test]
    fn provider_choice_routes_to_declared_route() {
        let (mut machine, outcome) = start();
        let invocation_id = invocation_id(&outcome);
        let completion = machine.complete(
            &invocation_id,
            parse_json_str(r#"{"choice": "sufficient", "cache": "miss"}"#).unwrap(),
            1_001,
        );
        assert_eq!(completion.status, RunStatus::Completed);
        assert_eq!(
            machine
                .output()
                .get("evaluate")
                .and_then(|value| value.get("choice"))
                .and_then(Json::as_str),
            Some("sufficient")
        );
        let kinds: Vec<&str> = machine
            .events()
            .iter()
            .map(|event| event.kind.as_str())
            .collect();
        assert!(kinds.contains(&"ProviderInvoked"));
        assert!(kinds.contains(&"ProviderCompleted"));
        assert!(kinds.contains(&"DecisionTaken"));
        assert!(kinds.contains(&"RunCompleted"));
    }

    #[test]
    fn unknown_provider_choice_fails_the_run_with_a_typed_error() {
        let (mut machine, outcome) = start();
        let invocation_id = invocation_id(&outcome);
        let completion = machine.complete(
            &invocation_id,
            parse_json_str(r#"{"choice": "maybe"}"#).unwrap(),
            1_001,
        );
        assert_eq!(completion.status, RunStatus::Failed);
        let error = machine.error().unwrap();
        assert!(matches!(error.kind, ErrorKind::Type));
        assert!(error.message.contains("unknown provider choice"));
    }

    #[test]
    fn provider_routes_must_cover_every_choice() {
        let error = load(&provider_document(
            r#"{"sufficient":"done"}"#,
            r#"["sufficient","insufficient"]"#,
        ))
        .unwrap_err();
        assert!(error.message.contains("cover every choice"));
    }

    #[test]
    fn provider_route_must_be_a_declared_choice() {
        let error = load(&provider_document(
            r#"{"sufficient":"done","maybe":"done"}"#,
            r#"["sufficient","insufficient"]"#,
        ))
        .unwrap_err();
        assert!(error.message.contains("not a declared choice"));
    }

    #[test]
    fn duplicate_provider_choices_are_rejected() {
        let error = load(&provider_document(
            r#"{"sufficient":"done"}"#,
            r#"["sufficient","sufficient"]"#,
        ))
        .unwrap_err();
        assert!(error.message.contains("duplicate provider choice"));
    }
}
