//! Reject reserved SWD features before a run starts. Never ignore them.

use crate::definition::{CompiledDefinition, Node};
use crate::error::{Result, RuntimeError};
use crate::json::Json;

pub fn assert_supported(definition: &CompiledDefinition) -> Result<()> {
    for (node_id, node) in &definition.nodes {
        match node {
            Node::Task { .. } => assert_task_supported(node_id, definition.document.get("nodes"))?,
            Node::Decision { .. } | Node::ProviderDecision { .. } => {}
            Node::Fork { .. } | Node::Join { .. } | Node::End { .. } => {}
            Node::Map { .. } => {
                if definition.version == "1.0" {
                    reject_v1_map_fields(node_id, definition.document.get("nodes"))?;
                }
            }
        }
    }
    Ok(())
}

fn reject_v1_map_fields(node_id: &str, nodes: Option<&Json>) -> Result<()> {
    let Some(raw) = nodes
        .and_then(Json::as_object)
        .and_then(|nodes| nodes.get(node_id))
    else {
        return Err(
            RuntimeError::preflight("map node is missing from the definition")
                .with_path(format!("nodes.{node_id}")),
        );
    };
    // Presence, not value. Explicit `"ordered"` is still a 1.1 field.
    for key in ["schedule", "item_id", "item_depends_on"] {
        if raw.get(key).is_some() {
            return Err(
                RuntimeError::preflight("map schedule/item fields require SWD 1.1")
                    .with_path(format!("nodes.{node_id}.{key}")),
            );
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
