//! Load an already-compiled SWD 1.0 document. No second language or parser.

use indexmap::IndexMap;

use crate::canonicalize::sha256_hex;
use crate::error::{Result, RuntimeError};
use crate::json::Json;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Limits {
    pub max_steps: u32,
    pub max_depth: u32,
    pub timeout_seconds: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EndStatus {
    Completed,
    Incomplete,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Binding {
    Path(String),
    Const(Json),
}

pub type Bindings = IndexMap<String, Binding>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CachePolicy {
    Off,
    Exact,
}

impl CachePolicy {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Off => "off",
            Self::Exact => "exact",
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum Node {
    Task {
        handler: String,
        input: Bindings,
        has_output_schema: bool,
        next: Option<String>,
    },
    Decision {
        condition: Json,
        true_route: String,
        false_route: String,
    },
    ProviderDecision {
        provider: String,
        prompt_key: String,
        input: Bindings,
        choices: Vec<String>,
        routes: IndexMap<String, String>,
        cache_policy: CachePolicy,
    },
    Fork {
        branches: IndexMap<String, String>,
        join: String,
    },
    Join {
        outputs: Bindings,
        next: Option<String>,
    },
    Map {
        items: String,
        child: String,
        concurrency: Option<u32>,
        join: Option<String>,
        next: Option<String>,
    },
    End {
        status: EndStatus,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct CompiledDefinition {
    pub document: Json,
    pub sha256: String,
    pub version: String,
    pub name: String,
    pub entrypoint: String,
    pub limits: Limits,
    pub nodes: IndexMap<String, Node>,
}

impl CompiledDefinition {
    pub fn load(document: Json, expected_sha256: &str) -> Result<Self> {
        let sha256 = sha256_hex(&document)?;
        if sha256 != expected_sha256 {
            return Err(RuntimeError::preflight(format!(
                "definition SHA-256 mismatch: rust={sha256} provided={expected_sha256}"
            )));
        }
        let version = required_str(&document, "version")?.to_string();
        if version != "1.0" {
            return Err(RuntimeError::preflight(format!(
                "unsupported SWD version {version:?}"
            )));
        }
        let loaded = Self {
            name: required_str(&document, "name")?.to_string(),
            entrypoint: required_str(&document, "entrypoint")?.to_string(),
            limits: parse_limits(document.get("limits").ok_or_else(|| {
                RuntimeError::preflight("definition requires limits").with_path("limits")
            })?)?,
            nodes: parse_nodes(document.get("nodes").ok_or_else(|| {
                RuntimeError::preflight("definition requires nodes").with_path("nodes")
            })?)?,
            version,
            sha256,
            document,
        };
        if !loaded.nodes.contains_key(&loaded.entrypoint) {
            return Err(
                RuntimeError::preflight(format!("unknown node {:?}", loaded.entrypoint))
                    .with_path("entrypoint"),
            );
        }
        crate::preflight::assert_supported(&loaded)?;
        Ok(loaded)
    }

    pub fn node(&self, node_id: &str) -> Result<&Node> {
        self.nodes
            .get(node_id)
            .ok_or_else(|| RuntimeError::internal(format!("unknown node {node_id:?}")))
    }
}

fn required_str<'a>(value: &'a Json, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Json::as_str)
        .ok_or_else(|| RuntimeError::preflight(format!("definition requires {key}")).with_path(key))
}

fn parse_limits(value: &Json) -> Result<Limits> {
    Ok(Limits {
        max_steps: required_u32(value, "max_steps")?,
        max_depth: required_u32(value, "max_depth")?,
        timeout_seconds: required_u32(value, "timeout_seconds")?,
    })
}

fn required_u32(value: &Json, key: &str) -> Result<u32> {
    let number = value
        .get(key)
        .and_then(Json::as_number)
        .and_then(serde_json::Number::as_u64)
        .ok_or_else(|| {
            RuntimeError::preflight(format!("limits.{key} must be a positive integer"))
                .with_path(format!("limits.{key}"))
        })?;
    u32::try_from(number).map_err(|_| {
        RuntimeError::preflight(format!("limits.{key} is out of range"))
            .with_path(format!("limits.{key}"))
    })
}

fn parse_nodes(value: &Json) -> Result<IndexMap<String, Node>> {
    let object = value.as_object().ok_or_else(|| {
        RuntimeError::preflight("nodes must be a non-empty object").with_path("nodes")
    })?;
    let mut nodes = IndexMap::with_capacity(object.len());
    for (node_id, node) in object {
        nodes.insert(node_id.clone(), parse_node(node_id, node)?);
    }
    if nodes.is_empty() {
        return Err(RuntimeError::preflight("nodes must be a non-empty object").with_path("nodes"));
    }
    Ok(nodes)
}

fn parse_node(node_id: &str, node: &Json) -> Result<Node> {
    let node_type = node.get("type").and_then(Json::as_str).ok_or_else(|| {
        RuntimeError::preflight("node must be an object with type")
            .with_path(format!("nodes.{node_id}"))
    })?;
    match node_type {
        "task" => parse_task(node_id, node),
        "decision" => parse_decision(node_id, node),
        "fork" => parse_fork(node_id, node),
        "join" => parse_join(node_id, node),
        "map" => parse_map(node_id, node),
        "end" => parse_end(node_id, node),
        "wait" | "subworkflow" => Err(RuntimeError::unsupported(format!(
            "{node_type} nodes are not executable in SWE phase 2"
        ))
        .with_path(format!("nodes.{node_id}.type"))),
        other => Err(
            RuntimeError::preflight(format!("unknown node type {other:?}"))
                .with_path(format!("nodes.{node_id}.type")),
        ),
    }
}

fn parse_task(node_id: &str, node: &Json) -> Result<Node> {
    Ok(Node::Task {
        handler: required_node_str(node_id, node, "handler")?.to_string(),
        input: parse_bindings(node_id, node.get("input"))?,
        has_output_schema: node.get("output_schema").is_some(),
        next: optional_str(node, "next"),
    })
}

fn parse_decision(node_id: &str, node: &Json) -> Result<Node> {
    match required_node_str(node_id, node, "evaluator")? {
        "expression" => parse_expression_decision(node_id, node),
        "provider" => parse_provider_decision(node_id, node),
        other => Err(
            RuntimeError::preflight(format!("unknown decision evaluator {other:?}"))
                .with_path(format!("nodes.{node_id}.evaluator")),
        ),
    }
}

fn parse_expression_decision(node_id: &str, node: &Json) -> Result<Node> {
    let routes = node
        .get("routes")
        .and_then(Json::as_object)
        .ok_or_else(|| {
            RuntimeError::preflight("decision requires routes")
                .with_path(format!("nodes.{node_id}.routes"))
        })?;
    let true_route = routes
        .get("true")
        .and_then(Json::as_str)
        .ok_or_else(|| {
            RuntimeError::preflight("expression decisions require exactly true and false routes")
                .with_path(format!("nodes.{node_id}.routes"))
        })?
        .to_string();
    let false_route = routes
        .get("false")
        .and_then(Json::as_str)
        .ok_or_else(|| {
            RuntimeError::preflight("expression decisions require exactly true and false routes")
                .with_path(format!("nodes.{node_id}.routes"))
        })?
        .to_string();
    Ok(Node::Decision {
        condition: node.get("condition").cloned().ok_or_else(|| {
            RuntimeError::preflight("decision requires condition")
                .with_path(format!("nodes.{node_id}.condition"))
        })?,
        true_route,
        false_route,
    })
}

fn parse_provider_decision(node_id: &str, node: &Json) -> Result<Node> {
    let provider = required_node_str(node_id, node, "provider")?.to_string();
    let prompt_key = required_node_str(node_id, node, "prompt_key")?.to_string();
    let choices = parse_choices(node_id, node)?;
    let routes = parse_provider_routes(node_id, node, &choices)?;
    Ok(Node::ProviderDecision {
        provider,
        prompt_key,
        input: parse_bindings(node_id, node.get("input"))?,
        choices,
        routes,
        cache_policy: parse_cache_policy(node_id, node)?,
    })
}

fn parse_choices(node_id: &str, node: &Json) -> Result<Vec<String>> {
    let raw = node
        .get("choices")
        .and_then(Json::as_array)
        .ok_or_else(|| {
            RuntimeError::preflight("provider decision requires choices")
                .with_path(format!("nodes.{node_id}.choices"))
        })?;
    let mut choices = Vec::with_capacity(raw.len());
    for item in raw {
        let choice = item.as_str().ok_or_else(|| {
            RuntimeError::preflight("provider choices must be strings")
                .with_path(format!("nodes.{node_id}.choices"))
        })?;
        if choices.iter().any(|seen| seen == choice) {
            return Err(
                RuntimeError::preflight(format!("duplicate provider choice {choice:?}"))
                    .with_path(format!("nodes.{node_id}.choices")),
            );
        }
        choices.push(choice.to_string());
    }
    if choices.len() < 2 {
        return Err(
            RuntimeError::preflight("provider decision requires at least two choices")
                .with_path(format!("nodes.{node_id}.choices")),
        );
    }
    Ok(choices)
}

fn parse_provider_routes(
    node_id: &str,
    node: &Json,
    choices: &[String],
) -> Result<IndexMap<String, String>> {
    let raw = node
        .get("routes")
        .and_then(Json::as_object)
        .ok_or_else(|| {
            RuntimeError::preflight("provider decision requires routes")
                .with_path(format!("nodes.{node_id}.routes"))
        })?;
    let mut routes = IndexMap::with_capacity(raw.len());
    for (choice, target) in raw {
        let target = target.as_str().ok_or_else(|| {
            RuntimeError::preflight("provider routes must map choices to node ids")
                .with_path(format!("nodes.{node_id}.routes"))
        })?;
        if !choices.iter().any(|known| known == choice) {
            return Err(RuntimeError::preflight(format!(
                "provider route {choice:?} is not a declared choice"
            ))
            .with_path(format!("nodes.{node_id}.routes")));
        }
        routes.insert(choice.clone(), target.to_string());
    }
    if routes.len() != choices.len() {
        return Err(RuntimeError::preflight(
            "provider routes must cover every choice exactly once",
        )
        .with_path(format!("nodes.{node_id}.routes")));
    }
    Ok(routes)
}

fn parse_cache_policy(node_id: &str, node: &Json) -> Result<CachePolicy> {
    match node.get("cache_policy").and_then(Json::as_str) {
        None | Some("off") => Ok(CachePolicy::Off),
        Some("exact") => Ok(CachePolicy::Exact),
        Some(other) => Err(
            RuntimeError::preflight(format!("unknown cache_policy {other:?}"))
                .with_path(format!("nodes.{node_id}.cache_policy")),
        ),
    }
}

fn parse_fork(node_id: &str, node: &Json) -> Result<Node> {
    let raw = node
        .get("branches")
        .and_then(Json::as_object)
        .ok_or_else(|| {
            RuntimeError::preflight("fork requires named branches")
                .with_path(format!("nodes.{node_id}.branches"))
        })?;
    let mut branches = IndexMap::new();
    for (name, target) in raw {
        let target = target.as_str().ok_or_else(|| {
            RuntimeError::preflight("fork branches must map names to node ids")
                .with_path(format!("nodes.{node_id}.branches"))
        })?;
        branches.insert(name.clone(), target.to_string());
    }
    Ok(Node::Fork {
        branches,
        join: required_node_str(node_id, node, "join")?.to_string(),
    })
}

fn parse_join(node_id: &str, node: &Json) -> Result<Node> {
    Ok(Node::Join {
        outputs: parse_bindings(node_id, node.get("outputs"))?,
        next: optional_str(node, "next"),
    })
}

fn parse_map(node_id: &str, node: &Json) -> Result<Node> {
    Ok(Node::Map {
        items: required_node_str(node_id, node, "items")?.to_string(),
        child: required_node_str(node_id, node, "child")?.to_string(),
        concurrency: optional_u32(node, "concurrency"),
        join: optional_str(node, "join"),
        next: optional_str(node, "next"),
    })
}

fn parse_end(node_id: &str, node: &Json) -> Result<Node> {
    let status = required_node_str(node_id, node, "status")?;
    let status = match status {
        "completed" => EndStatus::Completed,
        "incomplete" => EndStatus::Incomplete,
        "failed" => EndStatus::Failed,
        "cancelled" => EndStatus::Cancelled,
        other => {
            return Err(
                RuntimeError::preflight(format!("unknown end status {other:?}"))
                    .with_path(format!("nodes.{node_id}.status")),
            )
        }
    };
    Ok(Node::End { status })
}

fn required_node_str<'a>(node_id: &str, node: &'a Json, key: &str) -> Result<&'a str> {
    node.get(key).and_then(Json::as_str).ok_or_else(|| {
        RuntimeError::preflight(format!("nodes.{node_id} requires {key}"))
            .with_path(format!("nodes.{node_id}.{key}"))
    })
}

fn optional_str(node: &Json, key: &str) -> Option<String> {
    node.get(key).and_then(Json::as_str).map(ToOwned::to_owned)
}

fn optional_u32(node: &Json, key: &str) -> Option<u32> {
    node.get(key)
        .and_then(Json::as_number)
        .and_then(serde_json::Number::as_u64)
        .and_then(|value| u32::try_from(value).ok())
}

fn parse_bindings(node_id: &str, value: Option<&Json>) -> Result<Bindings> {
    let Some(value) = value else {
        return Ok(IndexMap::new());
    };
    let object = value.as_object().ok_or_else(|| {
        RuntimeError::preflight("bindings must be an object").with_path(format!("nodes.{node_id}"))
    })?;
    let mut out = IndexMap::new();
    for (key, binding) in object {
        out.insert(key.clone(), parse_binding(node_id, key, binding)?);
    }
    Ok(out)
}

fn parse_binding(node_id: &str, key: &str, value: &Json) -> Result<Binding> {
    if let Some(path) = value.as_str() {
        return Ok(Binding::Path(path.to_string()));
    }
    if let Some(constant) = value.get("const") {
        return Ok(Binding::Const(constant.clone()));
    }
    Err(
        RuntimeError::preflight(format!("invalid binding {key:?} on {node_id}"))
            .with_path(format!("nodes.{node_id}.{key}")),
    )
}

impl EndStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::Incomplete => "incomplete",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }
}
