//! Reject reserved SWD features before a run starts. Never ignore them.

use crate::definition::{CompiledDefinition, Node};
use crate::error::{Result, RuntimeError};
use crate::json::Json;

pub fn assert_supported(definition: &CompiledDefinition) -> Result<()> {
    for (node_id, node) in &definition.nodes {
        match node {
            Node::Task { .. } => assert_task_supported(node_id, definition.document.get("nodes"))?,
            Node::Decision { .. } | Node::ProviderDecision { .. } => {}
            Node::Fork { .. } | Node::Join { .. } | Node::Map { .. } | Node::End { .. } => {}
        }
    }
    Ok(())
}

fn assert_task_supported(node_id: &str, nodes: Option<&Json>) -> Result<()> {
    let Some(nodes) = nodes.and_then(Json::as_object) else {
        return Ok(());
    };
    let Some(raw) = nodes.get(node_id) else {
        return Ok(());
    };
    if raw.get("retry").is_some() {
        return Err(
            RuntimeError::unsupported("retry is not executable in SWE phase 2")
                .with_path(format!("nodes.{node_id}.retry")),
        );
    }
    if let Some(policy) = raw.get("cache_policy").and_then(Json::as_str) {
        if policy == "exact" {
            return Err(RuntimeError::unsupported(
                "cache_policy exact is not executable in SWE phase 2",
            )
            .with_path(format!("nodes.{node_id}.cache_policy")));
        }
    }
    Ok(())
}
