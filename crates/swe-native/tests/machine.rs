use swe_native::{parse_json_str, sha256_hex, start_run, Effect, Json, RunStatus};

fn load(doc: &str) -> (Json, String) {
    let document = parse_json_str(doc).unwrap();
    let sha = sha256_hex(&document).unwrap();
    (document, sha)
}

fn invoke_ids(effects: &[Effect]) -> Vec<String> {
    effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::InvokeHandler { invocation_id, .. } => Some(invocation_id.clone()),
            _ => None,
        })
        .collect()
}

#[test]
fn expression_decision_routes_true_and_false() {
    let raw = r#"{
      "version": "1.0",
      "name": "decide",
      "entrypoint": "check",
      "limits": {"max_steps": 8, "max_depth": 2, "timeout_seconds": 30},
      "nodes": {
        "check": {
          "type": "decision",
          "evaluator": "expression",
          "condition": {"eq": [{"var": "$.flag"}, {"const": true}]},
          "routes": {"true": "ok", "false": "no"}
        },
        "ok": {"type": "end", "status": "completed"},
        "no": {"type": "end", "status": "incomplete"}
      }
    }"#;
    let (document, sha) = load(raw);
    let input = Json::object([("flag".into(), Json::Bool(true))]);
    let (_machine, outcome) =
        start_run(document.clone(), &sha, "run-true".into(), input, 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Completed);
    assert!(outcome.effects.is_empty());

    let input = Json::object([("flag".into(), Json::Bool(false))]);
    let (_machine, outcome) = start_run(document, &sha, "run-false".into(), input, 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Incomplete);
}

#[test]
fn fork_join_waits_for_both_branches() {
    let raw = r#"{
      "version": "1.0",
      "name": "fanout",
      "entrypoint": "split",
      "limits": {"max_steps": 20, "max_depth": 2, "timeout_seconds": 30},
      "nodes": {
        "split": {
          "type": "fork",
          "branches": {"left": "task_a", "right": "task_b"},
          "join": "together"
        },
        "task_a": {"type": "task", "handler": "demo.left"},
        "task_b": {"type": "task", "handler": "demo.right"},
        "together": {
          "type": "join",
          "mode": "all",
          "outputs": {
            "left": "$.branches.left.result",
            "right": "$.branches.right.result"
          },
          "next": "done"
        },
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (mut machine, outcome) =
        start_run(document, &sha, "run-fork".into(), Json::empty_object(), 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Running);
    let ids = invoke_ids(&outcome.effects);
    assert_eq!(ids.len(), 2);
    let left = outcome
        .effects
        .iter()
        .find_map(|effect| match effect {
            Effect::InvokeHandler {
                handler,
                invocation_id,
                ..
            } if handler == "demo.left" => Some(invocation_id.clone()),
            _ => None,
        })
        .unwrap();
    let right = outcome
        .effects
        .iter()
        .find_map(|effect| match effect {
            Effect::InvokeHandler {
                handler,
                invocation_id,
                ..
            } if handler == "demo.right" => Some(invocation_id.clone()),
            _ => None,
        })
        .unwrap();
    let after_right = machine.complete(
        &right,
        Json::object([("who".into(), Json::from("right"))]),
        1,
    );
    assert_eq!(after_right.status, RunStatus::Running);
    let after_left = machine.complete(&left, Json::object([("who".into(), Json::from("left"))]), 2);
    assert_eq!(after_left.status, RunStatus::Completed);
    let output = machine.output();
    assert_eq!(
        output.get("together").and_then(|value| value.get("left")),
        Some(&Json::object([("who".into(), Json::from("left"))]))
    );
}

#[test]
fn map_zero_one_and_many_preserve_order() {
    let raw = r#"{
      "version": "1.0",
      "name": "map_items",
      "entrypoint": "fanout",
      "limits": {"max_steps": 400, "max_depth": 2, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map",
          "items": "$.needs",
          "child": "run_need",
          "concurrency": 8,
          "next": "done"
        },
        "run_need": {"type": "task", "handler": "demo.need"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let empty = Json::object([("needs".into(), Json::Array(vec![]))]);
    let (_machine, outcome) = start_run(document.clone(), &sha, "map-0".into(), empty, 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Completed);

    let one = Json::object([("needs".into(), Json::Array(vec![Json::from(7)]))]);
    let (mut machine, outcome) = start_run(document.clone(), &sha, "map-1".into(), one, 0).unwrap();
    assert_eq!(invoke_ids(&outcome.effects).len(), 1);
    let id = invoke_ids(&outcome.effects)[0].clone();
    let done = machine.complete(&id, Json::object([("n".into(), Json::from(7))]), 1);
    assert_eq!(done.status, RunStatus::Completed);

    let items: Vec<Json> = (0..100).map(Json::from).collect();
    let many = Json::object([("needs".into(), Json::Array(items))]);
    let (mut machine, outcome) = start_run(document, &sha, "map-100".into(), many, 0).unwrap();
    assert_eq!(invoke_ids(&outcome.effects).len(), 8);
    let mut pending = invoke_ids(&outcome.effects);
    let mut now = 1;
    let mut seen = 0u32;
    let mut status = outcome.status;
    while status == RunStatus::Running {
        assert!(pending.len() <= 8);
        let id = pending.remove(0);
        now += 1;
        seen += 1;
        let step = machine.complete(
            &id,
            Json::object([("n".into(), Json::from(i64::from(seen)))]),
            now,
        );
        pending.extend(invoke_ids(&step.effects));
        status = step.status;
    }
    assert_eq!(status, RunStatus::Completed);
    assert_eq!(seen, 100);
}

#[test]
fn provider_decision_is_rejected() {
    let raw = r#"{
      "version": "1.0",
      "name": "provider",
      "entrypoint": "ask",
      "limits": {"max_steps": 4, "max_depth": 1, "timeout_seconds": 5},
      "nodes": {
        "ask": {
          "type": "decision",
          "evaluator": "provider",
          "provider": "jev",
          "prompt_key": "research.assessment.user",
          "choices": ["yes", "no"],
          "routes": {"yes": "ok", "no": "ok"}
        },
        "ok": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let error = match start_run(document, &sha, "run".into(), Json::empty_object(), 0) {
        Err(error) => error,
        Ok(_) => panic!("expected provider preflight error"),
    };
    assert!(error.message.contains("provider"));
}

#[test]
fn duplicate_completion_is_idempotent_or_conflict() {
    let raw = r#"{
      "version": "1.0",
      "name": "once",
      "entrypoint": "work",
      "limits": {"max_steps": 6, "max_depth": 1, "timeout_seconds": 5},
      "nodes": {
        "work": {"type": "task", "handler": "demo.work", "next": "done"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (mut machine, outcome) =
        start_run(document, &sha, "dup".into(), Json::empty_object(), 0).unwrap();
    let id = invoke_ids(&outcome.effects)[0].clone();
    let first = machine.complete(&id, Json::object([("n".into(), Json::from(1))]), 1);
    assert_eq!(first.status, RunStatus::Completed);
    let again = machine.complete(&id, Json::object([("n".into(), Json::from(1))]), 2);
    assert_eq!(again.status, RunStatus::Completed);
    let conflict = machine.complete(&id, Json::object([("n".into(), Json::from(2))]), 3);
    assert_eq!(conflict.status, RunStatus::Completed);
    let error = machine.error().expect("conflicting completion is recorded");
    assert!(error.message.contains("conflicting"));
}

#[test]
fn nested_fork_completes_inner_join_first() {
    let raw = r#"{
      "version": "1.0",
      "name": "nested",
      "entrypoint": "outer",
      "limits": {"max_steps": 30, "max_depth": 4, "timeout_seconds": 30},
      "nodes": {
        "outer": {
          "type": "fork",
          "branches": {"left": "inner", "right": "task_c"},
          "join": "outer_join"
        },
        "inner": {
          "type": "fork",
          "branches": {"a": "task_a", "b": "task_b"},
          "join": "inner_join"
        },
        "task_a": {"type": "task", "handler": "demo.a"},
        "task_b": {"type": "task", "handler": "demo.b"},
        "task_c": {"type": "task", "handler": "demo.c"},
        "inner_join": {"type": "join", "mode": "all"},
        "outer_join": {"type": "join", "mode": "all", "next": "done"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (mut machine, outcome) =
        start_run(document, &sha, "nested".into(), Json::empty_object(), 0).unwrap();
    let mut pending = invoke_ids(&outcome.effects);
    assert_eq!(pending.len(), 3);
    let mut now = 1;
    let mut status = outcome.status;
    while status == RunStatus::Running {
        let id = pending.remove(0);
        let step = machine.complete(&id, Json::object([("ok".into(), Json::Bool(true))]), now);
        now += 1;
        pending.extend(invoke_ids(&step.effects));
        status = step.status;
    }
    assert_eq!(status, RunStatus::Completed);
}

#[test]
fn isolated_runs_do_not_share_outputs() {
    let raw = r#"{
      "version": "1.0",
      "name": "iso",
      "entrypoint": "work",
      "limits": {"max_steps": 6, "max_depth": 1, "timeout_seconds": 5},
      "nodes": {
        "work": {"type": "task", "handler": "demo.work", "next": "done"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (mut a, out_a) = start_run(
        document.clone(),
        &sha,
        "run-a".into(),
        Json::empty_object(),
        0,
    )
    .unwrap();
    let (mut b, out_b) =
        start_run(document, &sha, "run-b".into(), Json::empty_object(), 0).unwrap();
    let id_a = invoke_ids(&out_a.effects)[0].clone();
    let id_b = invoke_ids(&out_b.effects)[0].clone();
    assert_ne!(id_a, id_b);
    a.complete(&id_a, Json::object([("who".into(), Json::from("a"))]), 1);
    b.complete(&id_b, Json::object([("who".into(), Json::from("b"))]), 1);
    assert_eq!(
        a.output().get("work").and_then(|value| value.get("result")),
        Some(&Json::object([("who".into(), Json::from("a"))]))
    );
    assert_eq!(
        b.output().get("work").and_then(|value| value.get("result")),
        Some(&Json::object([("who".into(), Json::from("b"))]))
    );
}

#[test]
fn deterministic_loop_does_not_overflow() {
    let raw = r#"{
      "version": "1.0",
      "name": "bench_loop",
      "entrypoint": "loop",
      "limits": {"max_steps": 10008, "max_depth": 1, "timeout_seconds": 60},
      "nodes": {
        "loop": {
          "type": "decision",
          "evaluator": "expression",
          "condition": {"lt": [{"var": "$.run.steps_used"}, {"const": 10000}]},
          "routes": {"true": "loop", "false": "done"}
        },
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (machine, outcome) =
        start_run(document, &sha, "loop".into(), Json::empty_object(), 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Completed);
    assert!(machine.events().len() > 10000);
}

#[test]
fn loop_epoch_increments_on_reentry() {
    let raw = r#"{
      "version": "1.0",
      "name": "epochs",
      "entrypoint": "loop",
      "limits": {"max_steps": 8, "max_depth": 1, "timeout_seconds": 5},
      "nodes": {
        "loop": {
          "type": "decision",
          "evaluator": "expression",
          "condition": {"lt": [{"var": "$.run.steps_used"}, {"const": 3}]},
          "routes": {"true": "loop", "false": "done"}
        },
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (machine, outcome) =
        start_run(document, &sha, "epochs".into(), Json::empty_object(), 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Completed);
    let epochs: Vec<u32> = machine
        .events()
        .iter()
        .filter(|event| event.node_id.as_deref() == Some("loop"))
        .filter_map(|event| event.lineage.as_ref().map(|lineage| lineage.loop_epoch))
        .collect();
    assert!(epochs.contains(&0));
    assert!(epochs.contains(&1));
    assert_eq!(epochs.iter().copied().max(), Some(2));
}

#[test]
fn max_depth_fails_on_nested_fork() {
    let raw = r#"{
      "version": "1.0",
      "name": "deep",
      "entrypoint": "outer",
      "limits": {"max_steps": 20, "max_depth": 1, "timeout_seconds": 5},
      "nodes": {
        "outer": {
          "type": "fork",
          "branches": {"left": "inner", "right": "task_c"},
          "join": "outer_join"
        },
        "inner": {
          "type": "fork",
          "branches": {"a": "task_a", "b": "task_b"},
          "join": "inner_join"
        },
        "task_a": {"type": "task", "handler": "demo.a"},
        "task_b": {"type": "task", "handler": "demo.b"},
        "task_c": {"type": "task", "handler": "demo.c"},
        "inner_join": {"type": "join", "mode": "all"},
        "outer_join": {"type": "join", "mode": "all", "next": "done"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (machine, outcome) =
        start_run(document, &sha, "deep".into(), Json::empty_object(), 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Failed);
    let error = machine.error().expect("max_depth is recorded");
    assert!(error.message.contains("max_depth"));
}

#[test]
fn timeout_and_max_steps_fail_closed() {
    let raw = r#"{
      "version": "1.0",
      "name": "limits",
      "entrypoint": "work",
      "limits": {"max_steps": 1, "max_depth": 1, "timeout_seconds": 1},
      "nodes": {
        "work": {"type": "task", "handler": "demo.work", "next": "done"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (mut machine, outcome) =
        start_run(document, &sha, "lim".into(), Json::empty_object(), 0).unwrap();
    let id = invoke_ids(&outcome.effects)[0].clone();
    let timed = machine.complete(&id, Json::empty_object(), 1000);
    assert_eq!(timed.status, RunStatus::Failed);
}
