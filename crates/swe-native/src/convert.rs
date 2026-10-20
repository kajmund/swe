//! Convert between `serde_json::Value` and the typed SWE JSON model.

use indexmap::IndexMap;
use serde_json::Value;

use crate::error::{Result, RuntimeError};
use crate::json::Json;

pub fn json_from_serde(value: Value) -> Result<Json> {
    match value {
        Value::Null => Ok(Json::Null),
        Value::Bool(value) => Ok(Json::Bool(value)),
        Value::Number(number) => {
            if number.as_f64().is_some_and(|value| !value.is_finite()) {
                return Err(RuntimeError::preflight(
                    "NaN and Infinity are not allowed in canonical JSON",
                ));
            }
            Ok(Json::Number(number))
        }
        Value::String(value) => Ok(Json::String(value)),
        Value::Array(items) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                out.push(json_from_serde(item)?);
            }
            Ok(Json::Array(out))
        }
        Value::Object(map) => {
            let mut out = IndexMap::with_capacity(map.len());
            for (key, item) in map {
                out.insert(key, json_from_serde(item)?);
            }
            Ok(Json::Object(out))
        }
    }
}

pub fn json_to_serde(value: &Json) -> Value {
    match value {
        Json::Null => Value::Null,
        Json::Bool(value) => Value::Bool(*value),
        Json::Number(number) => Value::Number(number.clone()),
        Json::String(value) => Value::String(value.clone()),
        Json::Array(items) => Value::Array(items.iter().map(json_to_serde).collect()),
        Json::Object(map) => {
            let mut object = serde_json::Map::new();
            for (key, item) in map {
                object.insert(key.clone(), json_to_serde(item));
            }
            Value::Object(object)
        }
    }
}

pub fn parse_json_str(raw: &str) -> Result<Json> {
    let value: Value = serde_json::from_str(raw)
        .map_err(|error| RuntimeError::preflight(format!("invalid JSON: {error}")))?;
    json_from_serde(value)
}
