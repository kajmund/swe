//! JSON values with Python-parity types: `true != 1` even when nested.

use indexmap::IndexMap;
use serde_json::Number;

use crate::error::{Result, RuntimeError};

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Number(Number),
    String(String),
    Array(Vec<Json>),
    Object(IndexMap<String, Json>),
}

impl Json {
    pub fn object(pairs: impl IntoIterator<Item = (String, Json)>) -> Self {
        Self::Object(pairs.into_iter().collect())
    }

    pub fn empty_object() -> Self {
        Self::Object(IndexMap::new())
    }

    pub fn from_i64(value: i64) -> Self {
        Self::Number(Number::from(value))
    }

    pub fn from_u64(value: u64) -> Self {
        Self::Number(Number::from(value))
    }

    pub fn from_usize(value: usize) -> Self {
        Self::from_u64(value as u64)
    }

    pub fn as_object(&self) -> Option<&IndexMap<String, Json>> {
        match self {
            Self::Object(map) => Some(map),
            _ => None,
        }
    }

    pub fn as_object_mut(&mut self) -> Option<&mut IndexMap<String, Json>> {
        match self {
            Self::Object(map) => Some(map),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Self::Array(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(value) => Some(*value),
            _ => None,
        }
    }

    pub fn as_number(&self) -> Option<&Number> {
        match self {
            Self::Number(value) => Some(value),
            _ => None,
        }
    }

    pub fn json_kind(&self) -> &'static str {
        match self {
            Self::Null => "null",
            Self::Bool(_) => "bool",
            Self::Number(_) => "number",
            Self::String(_) => "string",
            Self::Array(_) => "array",
            Self::Object(_) => "object",
        }
    }

    pub fn is_missing_compatible_null(&self) -> bool {
        matches!(self, Self::Null)
    }

    pub fn same_json_type(&self, other: &Json) -> bool {
        self.json_kind() == other.json_kind()
    }

    pub fn json_eq(&self, other: &Json) -> bool {
        if !self.same_json_type(other) {
            return false;
        }
        match (self, other) {
            (Self::Array(left), Self::Array(right)) => {
                left.len() == right.len()
                    && left
                        .iter()
                        .zip(right.iter())
                        .all(|(item, other)| item.json_eq(other))
            }
            (Self::Object(left), Self::Object(right)) => {
                if left.len() != right.len() {
                    return false;
                }
                left.iter().all(|(key, value)| match right.get(key) {
                    Some(other) => value.json_eq(other),
                    None => false,
                })
            }
            (Self::Number(left), Self::Number(right)) => numbers_equal(left, right),
            (Self::Bool(left), Self::Bool(right)) => left == right,
            (Self::String(left), Self::String(right)) => left == right,
            (Self::Null, Self::Null) => true,
            _ => false,
        }
    }

    pub fn get(&self, key: &str) -> Option<&Json> {
        self.as_object().and_then(|map| map.get(key))
    }

    pub fn insert(&mut self, key: impl Into<String>, value: Json) -> Result<()> {
        let map = self
            .as_object_mut()
            .ok_or_else(|| RuntimeError::internal("expected object when inserting JSON field"))?;
        map.insert(key.into(), value);
        Ok(())
    }

    pub fn require_bool(&self, op: &str) -> Result<bool> {
        self.as_bool()
            .ok_or_else(|| RuntimeError::expression(format!("{op} requires boolean operands")))
    }

    pub fn require_number(&self, op: &str) -> Result<f64> {
        match self {
            Self::Bool(_) | Self::Null | Self::String(_) | Self::Array(_) | Self::Object(_) => {
                Err(RuntimeError::expression(format!("{op} requires numbers")))
            }
            Self::Number(number) => number_as_f64(number)
                .ok_or_else(|| RuntimeError::expression(format!("{op} requires numbers"))),
        }
    }
}

fn numbers_equal(left: &Number, right: &Number) -> bool {
    if let (Some(a), Some(b)) = (left.as_i64(), right.as_i64()) {
        return a == b;
    }
    if let (Some(a), Some(b)) = (left.as_u64(), right.as_u64()) {
        return a == b;
    }
    match (number_as_f64(left), number_as_f64(right)) {
        (Some(a), Some(b)) => a == b,
        _ => left == right,
    }
}

pub fn number_as_f64(number: &Number) -> Option<f64> {
    number.as_f64().or_else(|| {
        number
            .as_i64()
            .map(|value| value as f64)
            .or_else(|| number.as_u64().map(|value| value as f64))
    })
}

impl From<bool> for Json {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}

impl From<String> for Json {
    fn from(value: String) -> Self {
        Self::String(value)
    }
}

impl From<&str> for Json {
    fn from(value: &str) -> Self {
        Self::String(value.to_string())
    }
}

impl From<i32> for Json {
    fn from(value: i32) -> Self {
        Self::from_i64(i64::from(value))
    }
}

impl From<i64> for Json {
    fn from(value: i64) -> Self {
        Self::from_i64(value)
    }
}

impl From<usize> for Json {
    fn from(value: usize) -> Self {
        Self::from_usize(value)
    }
}

impl Default for Json {
    fn default() -> Self {
        Self::Null
    }
}
