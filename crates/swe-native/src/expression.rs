//! Typed JSON-AST expressions. No eval, no host-language operators.

use crate::error::{Result, RuntimeError};
use crate::json::Json;
use crate::path::{resolve_path, PathValue};

pub fn evaluate_expression(expr: &Json, context: &Json) -> Result<Json> {
    let (kind, payload) = split_expr(expr)?;
    match kind.as_str() {
        "const" => Ok(payload.clone()),
        "var" => eval_var(expr, context),
        "eq" => eval_eq(payload, context),
        "ne" => {
            let equal = eval_eq(payload, context)?;
            Ok(Json::Bool(!equal.require_bool("ne")?))
        }
        "lt" => cmp(payload, context, "lt", |left, right| left < right),
        "lte" => cmp(payload, context, "lte", |left, right| left <= right),
        "gt" => cmp(payload, context, "gt", |left, right| left > right),
        "gte" => cmp(payload, context, "gte", |left, right| left >= right),
        "and" => eval_bool_pair(payload, context, "and", |left, right| left && right),
        "or" => eval_bool_pair(payload, context, "or", |left, right| left || right),
        "not" => {
            let value = evaluate_expression(payload, context)?;
            Ok(Json::Bool(!value.require_bool("not")?))
        }
        "in" => eval_in(payload, context),
        "add" => arith(payload, context, "add", |left, right| Ok(left + right)),
        "sub" => arith(payload, context, "sub", |left, right| Ok(left - right)),
        "mul" => arith(payload, context, "mul", |left, right| Ok(left * right)),
        "div" => eval_div(payload, context),
        "exists" => eval_exists(payload, context),
        other => Err(RuntimeError::expression(format!(
            "unknown operator {other:?}"
        ))),
    }
}

fn split_expr(expr: &Json) -> Result<(String, &Json)> {
    let object = expr
        .as_object()
        .ok_or_else(|| RuntimeError::expression("expression must be a non-empty object"))?;
    if object.is_empty() {
        return Err(RuntimeError::expression(
            "expression must be a non-empty object",
        ));
    }
    if object.contains_key("var") {
        let extra: Vec<&String> = object
            .keys()
            .filter(|key| *key != "var" && *key != "default")
            .collect();
        if !extra.is_empty() {
            return Err(RuntimeError::expression(format!(
                "unknown var fields {extra:?}"
            )));
        }
        return Ok(("var".to_string(), expr));
    }
    if object.len() != 1 {
        return Err(RuntimeError::expression(
            "operator expressions must have exactly one key",
        ));
    }
    let (kind, payload) = object.iter().next().expect("length checked");
    Ok((kind.clone(), payload))
}

fn eval_var(payload: &Json, context: &Json) -> Result<Json> {
    let object = payload
        .as_object()
        .ok_or_else(|| RuntimeError::expression("var requires a string path"))?;
    let path = object
        .get("var")
        .and_then(Json::as_str)
        .ok_or_else(|| RuntimeError::expression("var requires a string path"))?;
    match resolve_path(context, path)? {
        PathValue::Found(value) => Ok(value.clone()),
        PathValue::Missing => match object.get("default") {
            Some(default) => Ok(default.clone()),
            None => Err(RuntimeError::expression(format!(
                "missing value at {path:?}"
            ))),
        },
    }
}

fn operands(payload: &Json, context: &Json, count: usize) -> Result<Vec<Json>> {
    let items = payload.as_array().ok_or_else(|| {
        RuntimeError::expression(format!("operator requires exactly {count} operands"))
    })?;
    if items.len() != count {
        return Err(RuntimeError::expression(format!(
            "operator requires exactly {count} operands"
        )));
    }
    items
        .iter()
        .map(|item| evaluate_expression(item, context))
        .collect()
}

fn eval_eq(payload: &Json, context: &Json) -> Result<Json> {
    let values = operands(payload, context, 2)?;
    if !values[0].same_json_type(&values[1]) {
        return Err(RuntimeError::expression(
            "eq/ne require matching JSON types",
        ));
    }
    Ok(Json::Bool(values[0].json_eq(&values[1])))
}

fn cmp(payload: &Json, context: &Json, op: &str, check: fn(f64, f64) -> bool) -> Result<Json> {
    let (left, right) = ordered(payload, context, op)?;
    Ok(Json::Bool(check(left, right)))
}

fn ordered(payload: &Json, context: &Json, op: &str) -> Result<(f64, f64)> {
    let values = operands(payload, context, 2)?;
    Ok((values[0].require_number(op)?, values[1].require_number(op)?))
}

fn eval_bool_pair(
    payload: &Json,
    context: &Json,
    op: &str,
    combine: fn(bool, bool) -> bool,
) -> Result<Json> {
    let values = operands(payload, context, 2)?;
    Ok(Json::Bool(combine(
        values[0].require_bool(op)?,
        values[1].require_bool(op)?,
    )))
}

fn eval_in(payload: &Json, context: &Json) -> Result<Json> {
    let values = operands(payload, context, 2)?;
    let collection = values[1]
        .as_array()
        .ok_or_else(|| RuntimeError::expression("in requires an array collection"))?;
    Ok(Json::Bool(
        collection.iter().any(|element| values[0].json_eq(element)),
    ))
}

fn arith(
    payload: &Json,
    context: &Json,
    op: &str,
    combine: fn(f64, f64) -> Result<f64>,
) -> Result<Json> {
    let (left, right) = ordered(payload, context, op)?;
    json_from_f64(combine(left, right)?)
}

fn eval_div(payload: &Json, context: &Json) -> Result<Json> {
    let (left, right) = ordered(payload, context, "div")?;
    if right == 0.0 {
        return Err(RuntimeError::expression("division by zero"));
    }
    json_from_f64(left / right)
}

fn eval_exists(payload: &Json, context: &Json) -> Result<Json> {
    let path = exists_path(payload)?;
    match resolve_path(context, path)? {
        PathValue::Found(_) => Ok(Json::Bool(true)),
        PathValue::Missing => Ok(Json::Bool(false)),
    }
}

fn exists_path(payload: &Json) -> Result<&str> {
    if let Some(path) = payload.as_str() {
        return Ok(path);
    }
    let object = payload
        .as_object()
        .ok_or_else(|| RuntimeError::expression("exists requires a var path"))?;
    if !object.keys().all(|key| key == "var" || key == "default") {
        return Err(RuntimeError::expression("exists requires a var path"));
    }
    object
        .get("var")
        .and_then(Json::as_str)
        .ok_or_else(|| RuntimeError::expression("exists requires a var path"))
}

fn json_from_f64(value: f64) -> Result<Json> {
    if !value.is_finite() {
        return Err(RuntimeError::expression("NaN and Infinity are not allowed"));
    }
    serde_json::Number::from_f64(value)
        .map(Json::Number)
        .ok_or_else(|| RuntimeError::expression("NaN and Infinity are not allowed"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn const_num(value: i64) -> Json {
        Json::object([("const".into(), Json::from(value))])
    }

    fn const_bool(value: bool) -> Json {
        Json::object([("const".into(), Json::from(value))])
    }

    #[test]
    fn lt_three_less_than_five_is_true() {
        let expr = Json::object([("lt".into(), Json::Array(vec![const_num(3), const_num(5)]))]);
        assert_eq!(
            evaluate_expression(&expr, &Json::empty_object()).unwrap(),
            Json::Bool(true)
        );
    }

    #[test]
    fn missing_value_is_typed_error() {
        let expr = Json::object([("var".into(), Json::from("$.run.steps_used"))]);
        let error = evaluate_expression(&expr, &Json::empty_object()).unwrap_err();
        assert!(error.message.contains("missing value"));
    }

    #[test]
    fn boolean_is_not_compared_as_number() {
        let expr = Json::object([(
            "lt".into(),
            Json::Array(vec![const_bool(true), const_num(1)]),
        )]);
        let error = evaluate_expression(&expr, &Json::empty_object()).unwrap_err();
        assert!(error.message.contains("requires numbers"));
    }

    #[test]
    fn in_does_not_treat_true_as_one() {
        let expr = Json::object([(
            "in".into(),
            Json::Array(vec![
                const_bool(true),
                Json::object([("const".into(), Json::Array(vec![Json::from(1)]))]),
            ]),
        )]);
        assert_eq!(
            evaluate_expression(&expr, &Json::empty_object()).unwrap(),
            Json::Bool(false)
        );
    }

    #[test]
    fn nested_bool_is_not_int() {
        let left = Json::object([(
            "const".into(),
            Json::object([("a".into(), Json::Bool(true))]),
        )]);
        let right = Json::object([("const".into(), Json::object([("a".into(), Json::from(1))]))]);
        let expr = Json::object([("eq".into(), Json::Array(vec![left, right]))]);
        assert_eq!(
            evaluate_expression(&expr, &Json::empty_object()).unwrap(),
            Json::Bool(false)
        );
    }
}
