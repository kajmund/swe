use indexmap::IndexMap;

use crate::definition::Binding;
use crate::error::{Result, RuntimeError};
use crate::expression::evaluate_expression;
use crate::json::Json;
use crate::machine::state::{Machine, Scope, ScopeKind};
use crate::path::{resolve_path, PathValue};

impl Machine {
    pub fn build_context(&self, scope_id: &str) -> Result<Json> {
        let mut object = IndexMap::new();
        if let Some(map) = self.input.as_object() {
            for (key, value) in map {
                object.insert(key.clone(), value.clone());
            }
        }
        object.insert("input".into(), self.input.clone());
        object.insert("run".into(), self.run_object(scope_id)?);
        for id in self.scope_chain(scope_id)? {
            let scope = self.scope_or_err(&id)?;
            for (key, value) in &scope.locals {
                object.insert(key.clone(), value.clone());
            }
            if scope.kind == ScopeKind::Fork {
                object.insert("branches".into(), fork_branches(scope));
            }
            if scope.kind == ScopeKind::Map {
                object.insert("children".into(), map_children(scope));
                object.insert("branches".into(), map_branches(scope));
            }
            if scope.kind == ScopeKind::MapChild {
                if let Some(item) = &scope.item {
                    object.insert("item".into(), item.clone());
                }
                if let Some(index) = scope.index {
                    object.insert("index".into(), Json::from_usize(index));
                }
            }
        }
        Ok(Json::Object(object))
    }

    pub fn bind(&self, bindings: &IndexMap<String, Binding>, context: &Json) -> Result<Json> {
        let mut out = IndexMap::new();
        for (key, binding) in bindings {
            out.insert(key.clone(), eval_binding(binding, context)?);
        }
        Ok(Json::Object(out))
    }

    fn run_object(&self, scope_id: &str) -> Result<Json> {
        let depth = self.scope_or_err(scope_id)?.depth;
        Ok(Json::object([
            ("id".into(), Json::from(self.run_id.as_str())),
            (
                "steps_used".into(),
                Json::from_u64(u64::from(self.steps_used)),
            ),
            (
                "max_steps".into(),
                Json::from_u64(u64::from(self.definition.limits.max_steps)),
            ),
            ("depth".into(), Json::from_u64(u64::from(depth))),
        ]))
    }

    pub fn scope_chain(&self, scope_id: &str) -> Result<Vec<String>> {
        let mut chain = Vec::new();
        let mut current = Some(scope_id.to_string());
        while let Some(id) = current {
            let scope = self.scope_or_err(&id)?;
            current = scope.parent.clone();
            chain.push(id);
        }
        chain.reverse();
        Ok(chain)
    }

    pub fn scope_or_err(&self, id: &str) -> Result<&Scope> {
        self.scopes
            .get(id)
            .ok_or_else(|| RuntimeError::internal(format!("unknown scope {id}")))
    }
}

fn eval_binding(binding: &Binding, context: &Json) -> Result<Json> {
    match binding {
        Binding::Const(value) => Ok(value.clone()),
        Binding::Path(path) => match resolve_path(context, path)? {
            PathValue::Found(value) => Ok(value.clone()),
            PathValue::Missing => Err(RuntimeError::expression(format!(
                "missing value at {path:?}"
            ))),
        },
    }
}

fn fork_branches(scope: &Scope) -> Json {
    let mut object = IndexMap::new();
    for (name, branch) in &scope.branches {
        let result = branch.result.clone().unwrap_or(Json::Null);
        object.insert(name.clone(), Json::object([("result".into(), result)]));
    }
    Json::Object(object)
}

fn map_children(scope: &Scope) -> Json {
    Json::Array(
        scope
            .children
            .iter()
            .map(|child| child.result.clone().unwrap_or(Json::Null))
            .collect(),
    )
}

fn map_branches(scope: &Scope) -> Json {
    let mut object = IndexMap::new();
    for child in &scope.children {
        let result = child.result.clone().unwrap_or(Json::Null);
        object.insert(
            child.index.to_string(),
            Json::object([("result".into(), result)]),
        );
    }
    Json::Object(object)
}

pub fn evaluate_condition(expr: &Json, context: &Json) -> Result<bool> {
    let value = evaluate_expression(expr, context)?;
    value.require_bool("decision condition")
}
