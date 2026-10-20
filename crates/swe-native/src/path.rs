//! Restricted JSON paths used by SWD bindings and expressions.

use crate::error::{Result, RuntimeError};
use crate::json::Json;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JsonPath(pub Vec<String>);

#[derive(Clone, Debug)]
pub enum PathValue<'a> {
    Found(&'a Json),
    Missing,
}

pub fn parse_json_path(path: &str) -> Result<JsonPath> {
    if path == "$" {
        return Ok(JsonPath(Vec::new()));
    }
    if !is_valid_path(path) {
        return Err(RuntimeError::expression(format!(
            "invalid JSON path {path:?}"
        )));
    }
    Ok(JsonPath(
        path[2..].split('.').map(ToOwned::to_owned).collect(),
    ))
}

pub fn resolve_path<'a>(context: &'a Json, path: &str) -> Result<PathValue<'a>> {
    let parsed = parse_json_path(path)?;
    let mut current = context;
    for segment in &parsed.0 {
        current = match step(current, segment, path)? {
            PathValue::Found(value) => value,
            PathValue::Missing => return Ok(PathValue::Missing),
        };
    }
    Ok(PathValue::Found(current))
}

fn step<'a>(current: &'a Json, segment: &str, path: &str) -> Result<PathValue<'a>> {
    match current {
        Json::Object(map) => match map.get(segment) {
            Some(value) => Ok(PathValue::Found(value)),
            None => Ok(PathValue::Missing),
        },
        Json::Array(items) if segment.chars().all(|ch| ch.is_ascii_digit()) => {
            let index: usize = segment
                .parse()
                .map_err(|_| RuntimeError::expression(format!("invalid JSON path {path:?}")))?;
            match items.get(index) {
                Some(value) => Ok(PathValue::Found(value)),
                None => Ok(PathValue::Missing),
            }
        }
        Json::Null => Ok(PathValue::Missing),
        _ => Err(RuntimeError::expression(format!(
            "cannot resolve {path:?} through {}",
            current.json_kind()
        ))),
    }
}

fn is_valid_path(path: &str) -> bool {
    if !path.starts_with('$') {
        return false;
    }
    if path == "$" {
        return true;
    }
    let mut rest = &path[1..];
    while let Some(stripped) = rest.strip_prefix('.') {
        let end = stripped.find('.').unwrap_or(stripped.len());
        let segment = &stripped[..end];
        if segment.is_empty() || !is_valid_segment(segment) {
            return false;
        }
        rest = &stripped[end..];
    }
    rest.is_empty()
}

fn is_valid_segment(segment: &str) -> bool {
    if segment.chars().all(|ch| ch.is_ascii_digit()) {
        return !segment.is_empty();
    }
    let mut chars = segment.chars();
    match chars.next() {
        Some(first) if first.is_ascii_alphabetic() || first == '_' => {}
        _ => return false,
    }
    chars.all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}
