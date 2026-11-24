use indexmap::IndexMap;

use crate::definition::{EndStatus, MapSchedule, Node};
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
    schedule: MapSchedule,
    item_id: Option<String>,
    item_depends_on: Option<String>,
}

struct ProviderStart<'a> {
    node_id: &'a str,
    scope_id: &'a str,
    branch_name: Option<String>,
    instance_id: String,
    provider: String,
    prompt_key: String,
    input: IndexMap<String, crate::definition::Binding>,
    choices: Vec<String>,
    cache_policy: crate::definition::CachePolicy,
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
        self.advance_loop_epoch(scope_id, node_id);
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
            Node::ProviderDecision {
                provider,
                prompt_key,
                input,
                choices,
                cache_policy,
                routes: _,
            } => self.start_provider_decision(ProviderStart {
                node_id,
                scope_id,
                branch_name,
                instance_id,
                provider,
                prompt_key,
                input,
                choices,
                cache_policy,
            }),
            Node::Map {
                items,
                child,
                concurrency,
                join,
                next,
                schedule,
                item_id,
                item_depends_on,
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
                schedule,
                item_id,
                item_depends_on,
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
        let context = self.build_binding_context(scope_id, &bindings)?;
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

    fn start_provider_decision(&mut self, start: ProviderStart<'_>) -> Result<()> {
        let context = self.build_binding_context(start.scope_id, &start.input)?;
        let input = if start.input.is_empty() {
            Json::empty_object()
        } else {
            self.bind(&start.input, &context)?
        };
        let invocation_id = start.instance_id.clone();
        let lineage = self.scope_or_err(start.scope_id)?.lineage();
        let payload = Json::object([
            ("provider".into(), Json::from(start.provider.as_str())),
            ("prompt_key".into(), Json::from(start.prompt_key.as_str())),
            (
                "choices".into(),
                Json::Array(
                    start
                        .choices
                        .iter()
                        .map(|choice| Json::from(choice.as_str()))
                        .collect(),
                ),
            ),
        ]);
        self.emit(
            EventKind::ProviderInvoked,
            Some(start.instance_id.clone()),
            Some(start.node_id.to_string()),
            Some(lineage),
            None,
            payload,
        )?;
        self.invocations.insert(
            invocation_id.clone(),
            Invocation {
                id: invocation_id.clone(),
                instance_id: start.instance_id,
                node_id: start.node_id.to_string(),
                scope_id: start.scope_id.to_string(),
                branch_name: start.branch_name,
                handler: start.provider.clone(),
                status: InvocationStatus::Pending,
                output: None,
            },
        );
        let branch_path = self.scope_or_err(start.scope_id)?.branch_path.clone();
        self.effects.push(crate::effects::Effect::InvokeProvider {
            run_id: self.run_id.clone(),
            invocation_id,
            node_id: start.node_id.to_string(),
            branch_path,
            provider: start.provider,
            prompt_key: start.prompt_key,
            input,
            choices: start.choices,
            cache_policy: start.cache_policy.as_str(),
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
        // Validate the static item DAG before any child scope or journal event.
        let ready = match start.schedule {
            MapSchedule::Ordered => None,
            MapSchedule::Ready => {
                let item_id = start
                    .item_id
                    .as_deref()
                    .ok_or_else(|| RuntimeError::internal("ready map is missing item_id"))?;
                let depends = start.item_depends_on.as_deref().ok_or_else(|| {
                    RuntimeError::internal("ready map is missing item_depends_on")
                })?;
                Some(plan_ready_items(start.node_id, &items, item_id, depends)?)
            }
        };
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
        if let Some(plan) = ready {
            let serials = reserve_ready_serials(self, start.parent_id, plan.ids.len());
            map.map_schedule = MapSchedule::Ready;
            map.unmet = plan.unmet;
            map.dependents = plan.dependents;
            map.children = plan
                .ids
                .into_iter()
                .enumerate()
                .map(|(index, item_id)| ChildState {
                    index,
                    scope_id: String::new(),
                    status: WorkStatus::Waiting,
                    result: None,
                    item_id: Some(item_id),
                    pinned_serial: Some(serials[index]),
                })
                .collect();
        }
        self.scopes.insert(map_id.clone(), map);
        if self.scopes[&map_id].items.is_empty() {
            return self.finish_map(&map_id);
        }
        self.spawn_map_children(&map_id, &start.child)
    }

    pub fn spawn_map_children(&mut self, map_id: &str, child: &str) -> Result<()> {
        if self.status.is_terminal() {
            return Ok(());
        }
        if self.scope_or_err(map_id)?.map_schedule == MapSchedule::Ready {
            return self.spawn_ready_children(map_id, child);
        }
        self.spawn_ordered_children(map_id, child)
    }

    fn spawn_ordered_children(&mut self, map_id: &str, child: &str) -> Result<()> {
        loop {
            let (index, item) = {
                let map = self.scope_or_err(map_id)?;
                // Children are spawned in input order. The difference between
                // the next index and completed work is therefore the current
                // in-flight count, without an O(concurrency) scan per spawn.
                let completed = map.items.len().saturating_sub(map.remaining);
                let inflight = map.next_child.saturating_sub(completed) as u32;
                if inflight >= map.concurrency || map.next_child >= map.items.len() {
                    return Ok(());
                }
                let index = map.next_child;
                (index, map.items[index].clone())
            };
            let child_id = format!("{map_id}[{index}]");
            let child_scope = {
                let parent = self.scope_or_err(map_id)?;
                let mut scope =
                    child_scope(parent, child_id.clone(), ScopeKind::MapChild, parent.depth);
                scope.item = Some(item);
                scope.index = Some(index);
                scope.branch_path = join_path(&parent.branch_path, &format!("{index}"));
                scope.map_instance_id = Some(map_id.to_string());
                scope.owner_node = parent.owner_node.clone();
                scope
            };
            self.scopes.insert(child_id.clone(), child_scope);
            if let Some(map) = self.scopes.get_mut(map_id) {
                map.next_child += 1;
                map.children.push(ChildState {
                    index,
                    scope_id: child_id.clone(),
                    status: WorkStatus::Running,
                    result: None,
                    item_id: None,
                    pinned_serial: None,
                });
            }
            let owner_node = self.scope_or_err(map_id)?.owner_node.clone();
            self.emit(
                EventKind::BranchSpawned,
                Some(child_id.clone()),
                owner_node,
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

    fn spawn_ready_children(&mut self, map_id: &str, child: &str) -> Result<()> {
        loop {
            if self.status.is_terminal() {
                return Ok(());
            }
            let index = {
                let map = self.scope_or_err(map_id)?;
                let inflight = map
                    .children
                    .iter()
                    .filter(|state| state.status == WorkStatus::Running)
                    .count() as u32;
                if inflight >= map.concurrency {
                    return Ok(());
                }
                next_ready_index(map)
            };
            let Some(index) = index else {
                return Ok(());
            };
            self.spawn_ready_child(map_id, child, index)?;
        }
    }

    fn spawn_ready_child(&mut self, map_id: &str, child: &str, index: usize) -> Result<()> {
        let (item, item_id, pinned_serial) = {
            let map = self.scope_or_err(map_id)?;
            let state = map.children.get(index).ok_or_else(|| {
                RuntimeError::internal(format!("ready map {map_id} missing item {index}"))
            })?;
            (
                map.items[index].clone(),
                state.item_id.clone(),
                state.pinned_serial,
            )
        };
        let child_id = format!("{map_id}[{index}]");
        let child_scope = {
            let parent = self.scope_or_err(map_id)?;
            let mut scope =
                child_scope(parent, child_id.clone(), ScopeKind::MapChild, parent.depth);
            scope.item = Some(item);
            scope.index = Some(index);
            let mut path = join_path(&parent.branch_path, &format!("{index}"));
            if let Some(item_id) = &item_id {
                path = join_path(&path, item_id);
            }
            scope.branch_path = path;
            scope.map_instance_id = Some(map_id.to_string());
            scope.owner_node = parent.owner_node.clone();
            // The reserved serial is the first id in this item. Later nodes
            // stay on this cursor instead of the run-wide clock.
            scope.serial_cursor = pinned_serial;
            scope
        };
        self.scopes.insert(child_id.clone(), child_scope);
        if let Some(map) = self.scopes.get_mut(map_id) {
            let state = map.children.get_mut(index).ok_or_else(|| {
                RuntimeError::internal(format!("ready map {map_id} missing item {index}"))
            })?;
            state.scope_id = child_id.clone();
            state.status = WorkStatus::Running;
            state.pinned_serial = None;
        }
        let owner_node = self.scope_or_err(map_id)?.owner_node.clone();
        let mut payload = vec![("index".into(), Json::from_usize(index))];
        if let Some(item_id) = &item_id {
            payload.push(("item_id".into(), Json::from(item_id.as_str())));
        }
        self.emit(
            EventKind::BranchSpawned,
            Some(child_id.clone()),
            owner_node,
            Some(self.scope_or_err(&child_id)?.lineage()),
            None,
            Json::object(payload),
        )?;
        self.schedule(child, &child_id, None)?;
        Ok(())
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

    fn advance_loop_epoch(&mut self, scope_id: &str, node_id: &str) {
        if let Some(scope) = self.scopes.get_mut(scope_id) {
            if !scope.visited.insert(node_id.to_string()) {
                scope.loop_epoch = scope.loop_epoch.saturating_add(1);
            }
        }
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

struct ReadyPlan {
    ids: Vec<String>,
    unmet: Vec<u32>,
    dependents: Vec<Vec<usize>>,
}

fn ready_error(node_id: &str, message: impl Into<String>) -> RuntimeError {
    RuntimeError::preflight(message).with_path(format!("nodes.{node_id}"))
}

fn valid_item_id(id: &str) -> bool {
    let mut chars = id.chars();
    match chars.next() {
        Some(first) if first.is_ascii_alphabetic() => {}
        _ => return false,
    }
    let mut length = 1usize;
    for ch in chars {
        if length >= 128 || !(ch.is_ascii_alphanumeric() || ch == '_' || ch == '.' || ch == '-') {
            return false;
        }
        length += 1;
    }
    true
}

fn resolve_item_value(item: &Json, path: &str) -> Result<Option<Json>> {
    let context = Json::object([("item".into(), item.clone())]);
    match resolve_path(&context, path)? {
        PathValue::Found(value) => Ok(Some(value.clone())),
        PathValue::Missing => Ok(None),
    }
}

fn plan_ready_items(
    node_id: &str,
    items: &[Json],
    item_id_path: &str,
    depends_path: &str,
) -> Result<ReadyPlan> {
    let mut ids = Vec::with_capacity(items.len());
    let mut id_index: IndexMap<String, usize> = IndexMap::new();
    let mut edges: Vec<Vec<usize>> = Vec::with_capacity(items.len());
    for (index, item) in items.iter().enumerate() {
        if item.as_object().is_none() {
            return Err(ready_error(
                node_id,
                format!("map item {index} must be an object"),
            ));
        }
        let id = match resolve_item_value(item, item_id_path)? {
            Some(Json::String(id)) if valid_item_id(&id) => id,
            Some(Json::String(id)) => {
                return Err(ready_error(node_id, format!("invalid item id {id:?}")));
            }
            Some(_) => {
                return Err(ready_error(
                    node_id,
                    format!("map item {index} item id must be a string"),
                ));
            }
            None => {
                return Err(ready_error(
                    node_id,
                    format!("map item {index} is missing an item id"),
                ));
            }
        };
        if id_index.contains_key(&id) {
            return Err(ready_error(node_id, format!("duplicate item id {id:?}")));
        }
        id_index.insert(id.clone(), index);
        ids.push(id);
        edges.push(Vec::new());
    }
    for (index, item) in items.iter().enumerate() {
        let depends = match resolve_item_value(item, depends_path)? {
            Some(Json::Array(values)) => values,
            Some(_) => {
                return Err(ready_error(
                    node_id,
                    format!("map item {} depends_on must be an array", ids[index]),
                ));
            }
            None => {
                return Err(ready_error(
                    node_id,
                    format!("map item {index} is missing depends_on"),
                ));
            }
        };
        let mut seen = Vec::new();
        for value in depends {
            let Json::String(dep) = value else {
                return Err(ready_error(node_id, "item dependency must be a string"));
            };
            if seen.iter().any(|existing: &String| existing == &dep) {
                return Err(ready_error(
                    node_id,
                    format!("duplicate item dependency {dep:?}"),
                ));
            }
            seen.push(dep.clone());
            if dep == ids[index] {
                return Err(ready_error(
                    node_id,
                    format!("item {dep:?} depends on itself"),
                ));
            }
            let Some(&dep_index) = id_index.get(&dep) else {
                return Err(ready_error(
                    node_id,
                    format!("unknown item dependency {dep:?}"),
                ));
            };
            edges[index].push(dep_index);
        }
    }
    let unmet: Vec<u32> = edges.iter().map(|deps| deps.len() as u32).collect();
    let mut dependents = vec![Vec::new(); items.len()];
    for (index, deps) in edges.iter().enumerate() {
        for &dep_index in deps {
            dependents[dep_index].push(index);
        }
    }
    let mut remaining = unmet.clone();
    let mut queue: Vec<usize> = remaining
        .iter()
        .enumerate()
        .filter(|(_, count)| **count == 0)
        .map(|(index, _)| index)
        .collect();
    let mut cursor = 0;
    while cursor < queue.len() {
        let index = queue[cursor];
        cursor += 1;
        for &dependent in &dependents[index] {
            remaining[dependent] = remaining[dependent].saturating_sub(1);
            if remaining[dependent] == 0 {
                queue.push(dependent);
            }
        }
    }
    if queue.len() != items.len() {
        return Err(ready_error(node_id, "item dependency cycle"));
    }
    Ok(ReadyPlan {
        ids,
        unmet,
        dependents,
    })
}

fn next_ready_index(map: &Scope) -> Option<usize> {
    let mut best: Option<usize> = None;
    for (index, state) in map.children.iter().enumerate() {
        if state.status != WorkStatus::Waiting {
            continue;
        }
        if map.unmet.get(index).copied().unwrap_or(1) != 0 {
            continue;
        }
        let replace = match best {
            None => true,
            Some(current) => ready_rank(map, index) < ready_rank(map, current),
        };
        if replace {
            best = Some(index);
        }
    }
    best
}

fn ready_rank(map: &Scope, index: usize) -> (usize, &str) {
    let item_id = map
        .children
        .get(index)
        .and_then(|state| state.item_id.as_deref())
        .unwrap_or("");
    (index, item_id)
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
        visited: indexmap::IndexSet::new(),
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
        map_schedule: MapSchedule::Ordered,
        unmet: Vec::new(),
        dependents: Vec::new(),
        serial_cursor: parent.serial_cursor.map(|_| 0),
        item: None,
        index: None,
    }
}

fn reserve_ready_serials(machine: &mut Machine, parent_id: &str, count: usize) -> Vec<u64> {
    let isolated = machine
        .scopes
        .get(parent_id)
        .and_then(|scope| scope.serial_cursor)
        .is_some();
    if isolated {
        // Already inside one item. Input order is 0..n; the branch path
        // distinguishes these ids from the parent item's own serials.
        return (0..count as u64).collect();
    }
    (0..count).map(|_| machine.new_serial()).collect()
}

pub fn join_path(parent: &str, child: &str) -> String {
    if parent.is_empty() {
        child.to_string()
    } else {
        format!("{parent}/{child}")
    }
}
