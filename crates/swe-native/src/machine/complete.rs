use indexmap::IndexMap;

use crate::definition::Node;
use crate::effects::{Effect, RunStatus};
use crate::error::{Result, RuntimeError};
use crate::events::EventKind;
use crate::json::Json;
use crate::machine::state::{InvocationStatus, Machine, ScopeKind, WorkStatus};

impl Machine {
    pub fn complete_invocation(&mut self, invocation_id: &str, output: Json) -> Result<bool> {
        let invocation = self
            .invocations
            .get(invocation_id)
            .ok_or_else(|| {
                RuntimeError::unknown_invocation(format!("unknown invocation {invocation_id}"))
            })?
            .clone();
        match invocation.status {
            InvocationStatus::Cancelled | InvocationStatus::Failed => return Ok(true),
            InvocationStatus::Completed => {
                return match &invocation.output {
                    Some(previous) if previous.json_eq(&output) => Ok(true),
                    _ => Err(RuntimeError::idempotency(format!(
                        "conflicting completion for {invocation_id}"
                    ))),
                };
            }
            InvocationStatus::Pending if self.status.is_terminal() => return Ok(true),
            InvocationStatus::Pending => {}
        }
        self.check_timeout()?;
        if let Some(stored) = self.invocations.get_mut(invocation_id) {
            stored.status = InvocationStatus::Completed;
            stored.output = Some(output.clone());
        }
        let wrapped = Json::object([("result".into(), output.clone())]);
        self.store_local(&invocation.scope_id, &invocation.node_id, wrapped.clone())?;
        self.emit(
            EventKind::NodeCompleted,
            Some(invocation.instance_id.clone()),
            Some(invocation.node_id.clone()),
            Some(self.scope_or_err(&invocation.scope_id)?.lineage()),
            Some(format!("$.{}", invocation.node_id)),
            wrapped,
        )?;
        let next = match self.definition.node(&invocation.node_id)? {
            Node::Task { next, .. } => next.clone(),
            _ => None,
        };
        if let Some(next) = next {
            self.schedule(&next, &invocation.scope_id, invocation.branch_name)?;
        } else {
            self.complete_scope_work(&invocation.scope_id, output)?;
        }
        Ok(false)
    }

    pub fn fail_invocation(&mut self, invocation_id: &str, error: RuntimeError) -> Result<bool> {
        let invocation = self
            .invocations
            .get(invocation_id)
            .ok_or_else(|| {
                RuntimeError::unknown_invocation(format!("unknown invocation {invocation_id}"))
            })?
            .clone();
        match invocation.status {
            InvocationStatus::Cancelled | InvocationStatus::Failed => return Ok(true),
            InvocationStatus::Completed => {
                return Err(RuntimeError::idempotency(format!(
                    "conflicting failure for {invocation_id}"
                )));
            }
            InvocationStatus::Pending if self.status.is_terminal() => return Ok(true),
            InvocationStatus::Pending => {}
        }
        self.check_timeout()?;
        if let Some(stored) = self.invocations.get_mut(invocation_id) {
            stored.status = InvocationStatus::Failed;
        }
        self.emit(
            EventKind::NodeFailed,
            Some(invocation.instance_id),
            Some(invocation.node_id),
            Some(self.scope_or_err(&invocation.scope_id)?.lineage()),
            None,
            Json::object([("message".into(), Json::from(error.message.as_str()))]),
        )?;
        self.fail_run(error)?;
        Ok(false)
    }

    pub fn complete_scope_work(&mut self, scope_id: &str, result: Json) -> Result<()> {
        if self.status.is_terminal() {
            return Ok(());
        }
        let scope = self.scope_or_err(scope_id)?.clone();
        match scope.kind {
            ScopeKind::Branch => {
                let parent = scope
                    .parent
                    .clone()
                    .ok_or_else(|| RuntimeError::internal("branch missing parent"))?;
                self.complete_branch(&parent, &scope, result)
            }
            ScopeKind::MapChild => self.complete_map_child(&scope, result),
            ScopeKind::Root => Err(RuntimeError::internal(
                "root work completed without an end node",
            )),
            ScopeKind::Fork | ScopeKind::Map => self.continue_parent(&scope, result),
        }
    }

    fn complete_branch(
        &mut self,
        fork_id: &str,
        branch: &crate::machine::state::Scope,
        result: Json,
    ) -> Result<()> {
        let name = branch
            .branch_name
            .clone()
            .ok_or_else(|| RuntimeError::internal("branch scope missing name"))?;
        if let Some(fork) = self.scopes.get_mut(fork_id) {
            if let Some(state) = fork.branches.get_mut(&name) {
                if state.status != WorkStatus::Running {
                    return Ok(());
                }
                state.status = WorkStatus::Completed;
                state.result = Some(result);
            }
            fork.remaining = fork.remaining.saturating_sub(1);
        }
        if self.scope_or_err(fork_id)?.remaining == 0 {
            self.satisfy_join(fork_id)?;
        }
        Ok(())
    }

    fn complete_map_child(
        &mut self,
        child: &crate::machine::state::Scope,
        result: Json,
    ) -> Result<()> {
        let map_id = child
            .parent
            .clone()
            .ok_or_else(|| RuntimeError::internal("map child missing parent"))?;
        let index = child
            .index
            .ok_or_else(|| RuntimeError::internal("map child missing index"))?;
        let child_node = self
            .definition
            .nodes
            .get(child.owner_node.as_deref().unwrap_or(""))
            .and_then(|node| match node {
                Node::Map { child, .. } => Some(child.clone()),
                _ => None,
            });
        if let Some(map) = self.scopes.get_mut(&map_id) {
            if let Some(state) = map.children.iter_mut().find(|item| item.index == index) {
                state.status = WorkStatus::Completed;
                state.result = Some(result);
            }
            map.remaining = map.remaining.saturating_sub(1);
        }
        if let Some(child_node) = child_node {
            self.spawn_map_children(&map_id, &child_node)?;
        }
        if self.scope_or_err(&map_id)?.remaining == 0 {
            self.finish_map(&map_id)?;
        }
        Ok(())
    }

    pub fn satisfy_join(&mut self, fork_id: &str) -> Result<()> {
        let fork = self.scope_or_err(fork_id)?.clone();
        let join_id = fork
            .join_node
            .clone()
            .ok_or_else(|| RuntimeError::internal("fork missing join"))?;
        let parent_id = fork
            .parent
            .clone()
            .ok_or_else(|| RuntimeError::internal("fork missing parent"))?;
        self.consume_step()?;
        let instance_id = self.instance_id(&join_id, &fork);
        let context = self.build_context(fork_id)?;
        let outputs = match self.definition.node(&join_id)? {
            Node::Join { outputs, .. } if !outputs.is_empty() => self.bind(outputs, &context)?,
            _ => assembled_fork(&fork),
        };
        self.store_local(&parent_id, &join_id, outputs.clone())?;
        if let Some(owner) = &fork.owner_node {
            self.store_local(&parent_id, owner, outputs.clone())?;
        }
        self.emit(
            EventKind::JoinSatisfied,
            Some(instance_id.clone()),
            Some(join_id.clone()),
            Some(fork.lineage()),
            Some(format!("$.{join_id}")),
            outputs.clone(),
        )?;
        self.emit(
            EventKind::NodeCompleted,
            Some(instance_id),
            Some(join_id.clone()),
            Some(fork.lineage()),
            Some(format!("$.{join_id}")),
            outputs.clone(),
        )?;
        let next = match self.definition.node(&join_id)? {
            Node::Join { next, .. } => next.clone(),
            _ => None,
        };
        if let Some(next) = next {
            self.schedule(&next, &parent_id, fork.branch_name)?;
        } else {
            self.complete_scope_work(&parent_id, outputs)?;
        }
        Ok(())
    }

    pub fn finish_map(&mut self, map_id: &str) -> Result<()> {
        let map = self.scope_or_err(map_id)?.clone();
        let parent_id = map
            .parent
            .clone()
            .ok_or_else(|| RuntimeError::internal("map missing parent"))?;
        let owner = map
            .owner_node
            .clone()
            .ok_or_else(|| RuntimeError::internal("map missing owner"))?;
        let result = Json::Array(
            map.children
                .iter()
                .map(|child| child.result.clone().unwrap_or(Json::Null))
                .collect(),
        );
        let wrapped = Json::object([("result".into(), result.clone())]);
        self.store_local(&parent_id, &owner, wrapped.clone())?;
        if let Some(join_id) = &map.join_node {
            self.consume_step()?;
            let context = self.build_context(map_id)?;
            let outputs = match self.definition.node(join_id)? {
                Node::Join { outputs, .. } if !outputs.is_empty() => {
                    self.bind(outputs, &context)?
                }
                _ => wrapped.clone(),
            };
            self.store_local(&parent_id, join_id, outputs.clone())?;
            let instance_id = self.instance_id(join_id, &map);
            self.emit(
                EventKind::JoinSatisfied,
                Some(instance_id.clone()),
                Some(join_id.clone()),
                Some(map.lineage()),
                Some(format!("$.{join_id}")),
                outputs.clone(),
            )?;
            self.emit(
                EventKind::NodeCompleted,
                Some(instance_id),
                Some(join_id.clone()),
                Some(map.lineage()),
                Some(format!("$.{join_id}")),
                outputs.clone(),
            )?;
            let next = match self.definition.node(join_id)? {
                Node::Join { next, .. } => next.clone(),
                _ => None,
            };
            return self.continue_after(&parent_id, map.branch_name, next, outputs);
        }
        self.continue_after(&parent_id, map.branch_name, map.after_node, result)
    }

    fn continue_after(
        &mut self,
        parent_id: &str,
        branch_name: Option<String>,
        next: Option<String>,
        result: Json,
    ) -> Result<()> {
        if let Some(next) = next {
            self.schedule(&next, parent_id, branch_name)
        } else {
            self.complete_scope_work(parent_id, result)
        }
    }

    fn continue_parent(
        &mut self,
        scope: &crate::machine::state::Scope,
        result: Json,
    ) -> Result<()> {
        match &scope.parent {
            Some(parent) => self.complete_scope_work(parent, result),
            None => Ok(()),
        }
    }

    pub fn store_local(&mut self, scope_id: &str, key: &str, value: Json) -> Result<()> {
        let scope = self
            .scopes
            .get_mut(scope_id)
            .ok_or_else(|| RuntimeError::internal(format!("unknown scope {scope_id}")))?;
        scope.locals.insert(key.to_string(), value);
        Ok(())
    }

    pub fn fail_run(&mut self, error: RuntimeError) -> Result<()> {
        if self.status.is_terminal() {
            return Ok(());
        }
        self.status = RunStatus::Failed;
        self.error = Some(error.clone());
        self.cancel_pending()?;
        self.emit(
            EventKind::RunFailed,
            None,
            None,
            None,
            None,
            Json::object([
                ("message".into(), Json::from(error.message.as_str())),
                ("kind".into(), Json::from(error.kind.to_string())),
            ]),
        )
    }

    pub fn cancel_run(&mut self) -> Result<()> {
        if self.status.is_terminal() {
            return Ok(());
        }
        self.status = RunStatus::Cancelled;
        self.cancel_pending()?;
        self.emit(
            EventKind::RunCancelled,
            None,
            None,
            None,
            None,
            Json::object([("status".into(), Json::from("cancelled"))]),
        )
    }

    fn cancel_pending(&mut self) -> Result<()> {
        let ids: Vec<String> = self
            .invocations
            .iter()
            .filter(|(_, invocation)| invocation.status == InvocationStatus::Pending)
            .map(|(id, _)| id.clone())
            .collect();
        for id in ids {
            if let Some(invocation) = self.invocations.get_mut(&id) {
                invocation.status = InvocationStatus::Cancelled;
            }
            self.effects
                .push(Effect::CancelInvocation { invocation_id: id });
        }
        Ok(())
    }

    pub fn check_timeout(&self) -> Result<()> {
        if self.now_ms >= self.deadline_ms {
            return Err(RuntimeError::timeout(format!(
                "timeout_seconds {} exceeded",
                self.definition.limits.timeout_seconds
            )));
        }
        Ok(())
    }
}

fn assembled_fork(fork: &crate::machine::state::Scope) -> Json {
    let mut object = IndexMap::new();
    for (name, branch) in &fork.branches {
        object.insert(name.clone(), branch.result.clone().unwrap_or(Json::Null));
    }
    Json::Object(object)
}
