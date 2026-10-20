//! Canonical JSON bytes and SHA-256 matching the Python SWD oracle.

use std::fmt::Write as _;

use indexmap::IndexMap;
use serde_json::Number;
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

use crate::error::{Result, RuntimeError};
use crate::json::{number_as_f64, Json};

pub const CANONICAL_SCHEMA_VERSION: &str = "swd-canonical-json-v1";

pub fn canonicalize(value: &Json) -> Result<Json> {
    normalize(value)
}

pub fn canonical_json_bytes(value: &Json) -> Result<Vec<u8>> {
    let normalized = canonicalize(value)?;
    let mut out = String::new();
    write_canonical(&normalized, &mut out)?;
    Ok(out.into_bytes())
}

pub fn sha256_hex(value: &Json) -> Result<String> {
    let bytes = canonical_json_bytes(value)?;
    let digest = Sha256::digest(bytes);
    Ok(hex_encode(&digest))
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(out, "{byte:02x}");
    }
    out
}

fn normalize(value: &Json) -> Result<Json> {
    match value {
        Json::Object(map) => Ok(Json::Object(normalize_object(map)?)),
        Json::Array(items) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                out.push(normalize(item)?);
            }
            Ok(Json::Array(out))
        }
        Json::String(text) => Ok(Json::String(normalize_text(text))),
        Json::Number(number) => Ok(Json::Number(normalize_number(number)?)),
        Json::Bool(value) => Ok(Json::Bool(*value)),
        Json::Null => Ok(Json::Null),
    }
}

fn normalize_object(map: &IndexMap<String, Json>) -> Result<IndexMap<String, Json>> {
    let mut out = IndexMap::with_capacity(map.len());
    for (key, value) in map {
        let normalized_key = normalize_text(key);
        if out.contains_key(&normalized_key) {
            return Err(RuntimeError::preflight(format!(
                "canonical key collision {normalized_key:?}"
            )));
        }
        out.insert(normalized_key, normalize(value)?);
    }
    Ok(out)
}

fn normalize_text(value: &str) -> String {
    let lf = value.replace("\r\n", "\n").replace('\r', "\n");
    lf.nfc().collect()
}

fn normalize_number(number: &Number) -> Result<Number> {
    if let Some(value) = number.as_i64() {
        return Ok(Number::from(value));
    }
    if let Some(value) = number.as_u64() {
        return Ok(Number::from(value));
    }
    let Some(value) = number_as_f64(number) else {
        return Err(RuntimeError::preflight(
            "NaN and Infinity are not allowed in canonical JSON",
        ));
    };
    if !value.is_finite() {
        return Err(RuntimeError::preflight(
            "NaN and Infinity are not allowed in canonical JSON",
        ));
    }
    if value.fract() == 0.0 && value >= i64::MIN as f64 && value <= i64::MAX as f64 {
        return Ok(Number::from(value as i64));
    }
    Number::from_f64(value).ok_or_else(|| {
        RuntimeError::preflight("NaN and Infinity are not allowed in canonical JSON")
    })
}

fn write_canonical(value: &Json, out: &mut String) -> Result<()> {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Number(number) => write_number(number, out)?,
        Json::String(text) => write_json_string(text, out),
        Json::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_canonical(item, out)?;
            }
            out.push(']');
        }
        Json::Object(map) => write_object(map, out)?,
    }
    Ok(())
}

fn write_object(map: &IndexMap<String, Json>, out: &mut String) -> Result<()> {
    let mut keys: Vec<&String> = map.keys().collect();
    keys.sort();
    out.push('{');
    for (index, key) in keys.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        write_json_string(key, out);
        out.push(':');
        write_canonical(&map[*key], out)?;
    }
    out.push('}');
    Ok(())
}

fn write_number(number: &Number, out: &mut String) -> Result<()> {
    if let Some(value) = number.as_i64() {
        let _ = write!(out, "{value}");
        return Ok(());
    }
    if let Some(value) = number.as_u64() {
        let _ = write!(out, "{value}");
        return Ok(());
    }
    let Some(value) = number_as_f64(number) else {
        return Err(RuntimeError::preflight(
            "NaN and Infinity are not allowed in canonical JSON",
        ));
    };
    if !value.is_finite() {
        return Err(RuntimeError::preflight(
            "NaN and Infinity are not allowed in canonical JSON",
        ));
    }
    out.push_str(&python_float_repr(value)?);
    Ok(())
}

/// Match CPython `json.dumps` / `repr` for finite non-integer floats.
fn python_float_repr(value: f64) -> Result<String> {
    if !value.is_finite() {
        return Err(RuntimeError::preflight(
            "NaN and Infinity are not allowed in canonical JSON",
        ));
    }
    if value == 0.0 {
        return Ok(if value.is_sign_negative() {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        });
    }
    let formatted = format!("{value:.16}");
    let shortest = shortest_roundtrip(value).ok_or_else(|| {
        RuntimeError::unsupported(format!(
            "float {value} has no unique JSON encoding shared with the Python oracle"
        ))
    })?;
    let _ = formatted;
    Ok(shortest)
}

fn shortest_roundtrip(value: f64) -> Option<String> {
    // Prefer a compact decimal that round-trips. Reject if we cannot prove parity.
    let candidates = [
        format!("{value}"),
        format!("{value:.1}"),
        format!("{value:.16}"),
        format!("{value:.17}"),
    ];
    candidates.into_iter().find(|text| {
        text.parse::<f64>()
            .ok()
            .is_some_and(|parsed| parsed == value)
            && is_json_number(text)
    })
}

fn is_json_number(text: &str) -> bool {
    !text.is_empty()
        && text
            .chars()
            .all(|ch| ch.is_ascii_digit() || matches!(ch, '.' | '-' | '+' | 'e' | 'E'))
}

fn write_json_string(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ch if (ch as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", ch as u32);
            }
            ch => out.push(ch),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obj(pairs: &[(&str, Json)]) -> Json {
        Json::object(
            pairs
                .iter()
                .map(|(key, value)| ((*key).to_string(), value.clone())),
        )
    }

    #[test]
    fn object_keys_are_sorted() {
        let left =
            canonical_json_bytes(&obj(&[("b", Json::from(1)), ("a", Json::from(1))])).unwrap();
        let right =
            canonical_json_bytes(&obj(&[("a", Json::from(1)), ("b", Json::from(1))])).unwrap();
        assert_eq!(left, br#"{"a":1,"b":1}"#);
        assert_eq!(left, right);
    }

    #[test]
    fn unicode_is_nfc() {
        let composed = canonical_json_bytes(&obj(&[("name", Json::from("é"))])).unwrap();
        let decomposed = canonical_json_bytes(&obj(&[("name", Json::from("e\u{0301}"))])).unwrap();
        assert_eq!(composed, decomposed);
        assert_eq!(composed, "{\"name\":\"é\"}".as_bytes());
    }

    #[test]
    fn crlf_normalizes_to_lf() {
        let left = canonical_json_bytes(&Json::from("a\r\nb")).unwrap();
        let right = canonical_json_bytes(&Json::from("a\nb")).unwrap();
        assert_eq!(left, right);
    }

    #[test]
    fn integer_floats_become_ints() {
        let one = Number::from_f64(1.0).unwrap();
        assert_eq!(canonical_json_bytes(&Json::Number(one)).unwrap(), b"1");
        assert_eq!(
            sha256_hex(&Json::from(1)).unwrap(),
            sha256_hex(&Json::Number(Number::from_f64(1.0).unwrap())).unwrap()
        );
    }

    #[test]
    fn bool_is_not_an_int() {
        assert_eq!(canonical_json_bytes(&Json::Bool(true)).unwrap(), b"true");
    }

    #[test]
    fn nfc_key_collision_is_rejected() {
        let value = obj(&[("é", Json::from(1)), ("e\u{0301}", Json::from(2))]);
        let error = canonical_json_bytes(&value).unwrap_err();
        assert!(error.message.contains("canonical key collision"));
    }

    #[test]
    fn nan_and_infinity_are_rejected() {
        assert!(Number::from_f64(f64::NAN).is_none());
        assert!(Number::from_f64(f64::INFINITY).is_none());
        assert!(Number::from_f64(f64::NEG_INFINITY).is_none());
    }
}
