use indexmap::IndexMap;

use crate::definition::{EndStatus, Node};
use crate::effects::RunStatus;
use crate::error::{Result, RuntimeError};
use crate::events::{EventKind, Lineage};
use crate::json::Json;
use crate::machine::context::evaluate_condition;
use crate::machine::state::{
    BranchState, ChildState, Invocation, InvocationStatus, Machine, Scope, ScopeKind, WorkStatus,
};
use crate::path::{resolve_path, PathValue};

struct MapStart<'a> {
    node_id: &'a str,
    parent_id: &'a str,
    branch_name: Option<String>,
    instance_id: String,
    items_path: String,
    child: String,
    concurrency: Option<u32>,
    join: Option<String>,
    next: Option<String>,
}

impl Machine {
    pub fn schedule(
        &mut self,
        node_id: &str,
        scope_id: &str,
        branch_name: Option<String>,
    ) -> Result<()> {
        self.work
            .push((node_id.to_string(), scope_id.to_string(), branch_name));
        if self.pumping {
            return Ok(());
        }
        self.pumping = true;
        let result = self.pump_work();
        self.pumping = false;
        result
    }

    fn pump_work(&mut self) -> Result<()> {
        while !self.work.is_empty() {
            if self.status.is_terminal() {
                self.work.clear();
                return Ok(());
            }
            let (node_id, scope_id, branch_name) = self.work.remove(0);
            self.schedule_one(&node_id, &scope_id, branch_name)?;
        }
        Ok(())
    }

    fn schedule_one(
        &mut self,
        node_id: &str,
        scope_id: &str,
        branch_name: Option<String>,
    ) -> Result<()> {
        if self.status.is_terminal() {
            return Ok(());
        }
        self.check_timeout()?;
        self.consume_step()?;
        let instance_id = {
            let scope = self.scope_or_err(scope_id)?.clone();
            self.instance_id(node_id, &scope)
        };
        self.emit(
            EventKind::NodeScheduled,
            Some(instance_id.clone()),
            Some(node_id.to_string()),
            Some(self.scope_or_err(scope_id)?.lineage()),
            None,
            Json::empty_object(),
        )?;
        let node = self.definition.node(node_id)?.clone();
        match node {
            Node::Task {
                handler,
                input,
                next: _,
                has_output_schema: _,
            } => self.start_task(node_id, scope_id, branch_name, instance_id, handler, input),
            Node::Decision {
                condition,
                true_route,
                false_route,
            } => self.take_decision(
                node_id,
                scope_id,
                branch_name,
                instance_id,
                condition,
                true_route,
                false_route,
            ),
            Node::Fork { branches, join } => {
                self.start_fork(node_id, scope_id, branch_name, instance_id, branches, join)
            }
            Node::Map {
                items,
                child,
                concurrency,
                join,
                next,
            } => self.start_map(MapStart {
                node_id,
                parent_id: scope_id,
                branch_name,
                instance_id,
                items_path: items,
                child,
                concurrency,
                join,
                next,
            }),
            Node::End { status } => self.finish_end(node_id, instance_id, status),
            Node::Join { .. } => Err(RuntimeError::internal(format!(
                "join {node_id} cannot be scheduled directly"
            ))),
        }
    }

    fn start_task(
        &mut self,
        node_id: &str,
        scope_id: &str,
        branch_name: Option<String>,
        instance_id: String,
        handler: String,
        bindings: IndexMap<String, crate::definition::Binding>,
    ) -> Result<()> {
        let context = self.build_context(scope_id)?;
        let input = if bindings.is_empty() {
            default_task_input(self.scope_or_err(scope_id)?)
        } else {
            self.bind(&bindings, &context)?
        };
        let invocation_id = instance_id.clone();
        self.emit(
            EventKind::NodeStarted,
            Some(instance_id.clone()),
            Some(node_id.to_string()),
            Some(self.scope_or_err(scope_id)?.lineage()),
            None,
            Json::empty_object(),
        )?;
        self.invocations.insert(
            invocation_id.clone(),
            Invocation {
                id: invocation_id.clone(),
                instance_id,
                node_id: node_id.to_string(),
                scope_id: scope_id.to_string(),
                branch_name,
                handler: handler.clone(),
                status: InvocationStatus::Pending,
                output: None,
            },
        );
        let branch_path = self.scope_or_err(scope_id)?.branch_path.clone();
        self.effects.push(crate::effects::Effect::InvokeHandler {
            run_id: self.run_id.clone(),
            invocation_id,
            node_id: node_id.to_string(),
            branch_path,
            handler,
            input,
        });
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn take_decision(
        &mut self,
        node_id: &str,
        scope_id: &str,
        branch_name: Option<String>,
        instance_id: String,
        condition: Json,
        true_route: String,
        false_route: String,
    ) -> Result<()> {
        let context = self.build_context(scope_id)?;
        let choice = evaluate_condition(&condition, &context)?;
        let route = if choice { true_route } else { false_route };
        let payload = Json::object([
            (
                "choice".into(),
                Json::from(if choice { "true" } else { "false" }),
            ),
            ("result".into(), Json::Bool(choice)),
        ]);
        self.store_local(scope_id, node_id, payload.clone())?;
        self.emit(
            EventKind::DecisionTaken,
            Some(instance_id.clone()),
            Some(node_id.to_string()),
            Some(self.scope_or_err(scope_id)?.lineage()),
            Some(format!("$.{node_id}")),
            payload.clone(),
        )?;
        self.emit(
            EventKind::NodeCompleted,
            Some(instance_id),
            Some(node_id.to_string()),
            Some(self.scope_or_err(scope_id)?.lineage()),
            Some(format!("$.{node_id}")),
            payload,
        )?;
        self.schedule(&route, scope_id, branch_name)
    }

    fn start_fork(
        &mut self,
        node_id: &str,
        parent_id: &str,
        branch_name: Option<String>,
        instance_id: String,
        branches: IndexMap<String, String>,
        join: String,
    ) -> Result<()> {
        let parent = self.scope_or_err(parent_id)?.clone();
        let depth = parent.depth + 1;
        self.assert_depth(depth)?;
        let fork_id = instance_id.clone();
        let mut fork = child_scope(&parent, fork_id.clone(), ScopeKind::Fork, depth);
        fork.owner_node = Some(node_id.to_string());
        fork.join_node = Some(join);
        fork.branch_name = branch_name;
        fork.fork_instance_id = Some(fork_id.clone());
        fork.remaining = branches.len();
        for (name, start) in &branches {
            fork.branches.insert(
                name.clone(),
                BranchState {
                    name: name.clone(),
                    start_node: start.clone(),
                    status: WorkStatus::Running,
                    result: None,
                },
            );
        }
        let starts: Vec<(String, String)> = branches
            .iter()
            .map(|(name, start)| (name.clone(), start.clone()))
            .collect();
        self.scopes.insert(fork_id.clone(), fork);
        for (name, start) in starts {
            let path = join_path(&parent.branch_path, &name);
            let branch_id = format!("{fork_id}/{name}");
            let fork_scope = self.scope_or_err(&fork_id)?.clone();
            let mut branch = child_scope(&fork_scope, branch_id.clone(), ScopeKind::Branch, depth);
            branch.branch_name = Some(name.clone());
            branch.branch_path = path.clone();
            branch.fork_instance_id = Some(fork_id.clone());
            self.scopes.insert(branch_id.clone(), branch);
            self.emit(
                EventKind::BranchSpawned,
                Some(branch_id.clone()),
                Some(node_id.to_string()),
                Some(Lineage {
                    branch_path: path,
                    fork_instance_id: Some(fork_id.clone()),
                    map_instance_id: parent.map_instance_id.clone(),
                    loop_epoch: parent.loop_epoch,
                }),
                None,
                Json::object([("branch".into(), Json::from(name.as_str()))]),
            )?;
            self.schedule(&start, &branch_id, Some(name))?;
            if self.status.is_terminal() {
                return Ok(());
            }
        }
        Ok(())
    }

    fn start_map(&mut self, start: MapStart<'_>) -> Result<()> {
        let parent = self.scope_or_err(start.parent_id)?.clone();
        let depth = parent.depth + 1;
        self.assert_depth(depth)?;
        let context = self.build_context(start.parent_id)?;
        let items = resolve_map_items(&context, &start.items_path)?;
        let cap = start.concurrency.unwrap_or(items.len() as u32).max(1);
        let map_id = start.instance_id;
        let mut map = child_scope(&parent, map_id.clone(), ScopeKind::Map, depth);
        map.owner_node = Some(start.node_id.to_string());
        map.join_node = start.join;
        map.after_node = start.next;
        map.branch_name = start.branch_name;
        map.map_instance_id = Some(map_id.clone());
        map.items = items;
        map.concurrency = cap;
        map.remaining = map.items.len();
        self.scopes.insert(map_id.clone(), map);
        if self.scopes[&map_id].items.is_empty() {
            return self.finish_map(&map_id);
        }
        self.spawn_map_children(&map_id, &start.child)
    }

    pub fn spawn_map_children(&mut self, map_id: &str, child: &str) -> Result<()> {
        loop {
            let (index, item, parent, inflight) = {
                let map = self.scope_or_err(map_id)?;
                let inflight = map
                    .children
                    .iter()
                    .filter(|child| child.status == WorkStatus::Running)
                    .count() as u32;
                if inflight >= map.concurrency || map.next_child >= map.items.len() {
                    return Ok(());
                }
                let index = map.next_child;
                (index, map.items[index].clone(), map.clone(), inflight)
            };
            let _ = inflight;
            let child_id = format!("{map_id}[{index}]");
            let mut child_scope =
                child_scope(&parent, child_id.clone(), ScopeKind::MapChild, parent.depth);
            child_scope.item = Some(item);
            child_scope.index = Some(index);
            child_scope.branch_path = join_path(&parent.branch_path, &format!("{index}"));
            child_scope.map_instance_id = Some(map_id.to_string());
            child_scope.owner_node = parent.owner_node.clone();
            self.scopes.insert(child_id.clone(), child_scope);
            if let Some(map) = self.scopes.get_mut(map_id) {
                map.next_child += 1;
                map.children.push(ChildState {
                    index,
                    scope_id: child_id.clone(),
                    status: WorkStatus::Running,
                    result: None,
                });
            }
            self.emit(
                EventKind::BranchSpawned,
                Some(child_id.clone()),
                parent.owner_node.clone(),
                Some(self.scope_or_err(&child_id)?.lineage()),
                None,
                Json::object([("index".into(), Json::from_usize(index))]),
            )?;
            self.schedule(child, &child_id, None)?;
            if self.status.is_terminal() {
                return Ok(());
            }
        }
    }

    fn finish_end(&mut self, node_id: &str, instance_id: String, status: EndStatus) -> Result<()> {
        self.emit(
            EventKind::NodeCompleted,
            Some(instance_id),
            Some(node_id.to_string()),
            Some(self.scope_or_err(&self.root_id)?.lineage()),
            None,
            Json::object([("status".into(), Json::from(status.as_str()))]),
        )?;
        self.status = match status {
            EndStatus::Completed => RunStatus::Completed,
            EndStatus::Incomplete => RunStatus::Incomplete,
            EndStatus::Failed => RunStatus::Failed,
            EndStatus::Cancelled => RunStatus::Cancelled,
        };
        let kind = match self.status {
            RunStatus::Failed => EventKind::RunFailed,
            RunStatus::Cancelled => EventKind::RunCancelled,
            _ => EventKind::RunCompleted,
        };
        self.emit(
            kind,
            None,
            Some(node_id.to_string()),
            None,
            None,
            Json::object([("status".into(), Json::from(self.status.as_str()))]),
        )
    }

    pub fn consume_step(&mut self) -> Result<()> {
        if self.steps_used >= self.definition.limits.max_steps {
            return Err(RuntimeError::limit(format!(
                "max_steps {} exceeded",
                self.definition.limits.max_steps
            )));
        }
        self.steps_used += 1;
        Ok(())
    }

    pub fn assert_depth(&self, depth: u32) -> Result<()> {
        if depth > self.definition.limits.max_depth {
            return Err(RuntimeError::limit(format!(
                "max_depth {} exceeded",
                self.definition.limits.max_depth
            )));
        }
        Ok(())
    }
}

fn default_task_input(scope: &Scope) -> Json {
    if scope.kind == ScopeKind::MapChild {
        let mut object = IndexMap::new();
        if let Some(item) = &scope.item {
            object.insert("item".into(), item.clone());
        }
        if let Some(index) = scope.index {
            object.insert("index".into(), Json::from_usize(index));
        }
        return Json::Object(object);
    }
    Json::empty_object()
}

fn resolve_map_items(context: &Json, path: &str) -> Result<Vec<Json>> {
    match resolve_path(context, path)? {
        PathValue::Missing => Err(RuntimeError::expression(format!(
            "missing value at {path:?}"
        ))),
        PathValue::Found(Json::Null) => Err(RuntimeError::expression(format!(
            "map items at {path:?} is null"
        ))),
        PathValue::Found(Json::Array(items)) => Ok(items.clone()),
        PathValue::Found(other) => Err(RuntimeError::expression(format!(
            "map items at {path:?} must be an array, got {}",
            other.json_kind()
        ))),
    }
}

fn child_scope(parent: &Scope, id: String, kind: ScopeKind, depth: u32) -> Scope {
    Scope {
        id,
        kind,
        parent: Some(parent.id.clone()),
        locals: IndexMap::new(),
        depth,
        loop_epoch: parent.loop_epoch,
        owner_node: None,
        join_node: None,
        after_node: None,
        branch_name: parent.branch_name.clone(),
        branch_path: parent.branch_path.clone(),
        fork_instance_id: parent.fork_instance_id.clone(),
        map_instance_id: parent.map_instance_id.clone(),
        branches: IndexMap::new(),
        remaining: 0,
        items: Vec::new(),
        children: Vec::new(),
        concurrency: 1,
        next_child: 0,
        item: None,
        index: None,
    }
}

pub fn join_path(parent: &str, child: &str) -> String {
    if parent.is_empty() {
        child.to_string()
    } else {
        format!("{parent}/{child}")
    }
}
