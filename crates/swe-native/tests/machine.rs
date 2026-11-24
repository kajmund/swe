use swe_native::{
    parse_json_str, sha256_hex, start_run, CompiledDefinition, Effect, ErrorKind, EventKind, Json,
    RunStatus, RuntimeError,
};

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
fn provider_decision_invokes_a_provider_and_routes_on_choice() {
    let raw = r#"{
      "version": "1.0",
      "name": "provider",
      "entrypoint": "ask",
      "limits": {"max_steps": 4, "max_depth": 1, "timeout_seconds": 5},
      "nodes": {
        "ask": {
          "type": "decision",
          "evaluator": "provider",
          "provider": "fake",
          "prompt_key": "demo.decision.user",
          "input": {"flag": "$.input.flag"},
          "choices": ["yes", "no"],
          "routes": {"yes": "ok", "no": "ok"}
        },
        "ok": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let input = Json::object([("flag".into(), Json::Bool(true))]);
    let (mut machine, outcome) = start_run(document, &sha, "run".into(), input, 0).unwrap();
    let invocation_id = match outcome.effects.first() {
        Some(Effect::InvokeProvider {
            invocation_id,
            provider,
            input,
            choices,
            ..
        }) => {
            assert_eq!(provider, "fake");
            assert_eq!(input.get("flag").and_then(Json::as_bool), Some(true));
            assert_eq!(choices.len(), 2);
            invocation_id.clone()
        }
        other => panic!("expected invoke_provider effect, got {other:?}"),
    };
    let step = machine.complete(
        &invocation_id,
        Json::object([("choice".into(), Json::from("yes"))]),
        1,
    );
    assert_eq!(step.status, RunStatus::Completed);
    assert_eq!(
        machine
            .output()
            .get("ask")
            .and_then(|value| value.get("choice"))
            .and_then(Json::as_str),
        Some("yes")
    );
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
fn tick_times_out_without_a_completion() {
    let raw = r#"{
      "version": "1.0",
      "name": "hang",
      "entrypoint": "work",
      "limits": {"max_steps": 6, "max_depth": 1, "timeout_seconds": 1},
      "nodes": {
        "work": {"type": "task", "handler": "demo.work", "next": "done"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let (mut machine, outcome) =
        start_run(document, &sha, "hang".into(), Json::empty_object(), 0).unwrap();
    assert_eq!(outcome.status, RunStatus::Running);
    let timed = machine.tick(1000);
    assert_eq!(timed.status, RunStatus::Failed);
    assert!(timed
        .effects
        .iter()
        .any(|effect| matches!(effect, Effect::CancelInvocation { .. })));
    let error = machine.error().expect("timeout is recorded");
    assert!(error.message.contains("timeout"));
    let late = machine.complete(
        &invoke_ids(&outcome.effects)[0],
        Json::object([("ok".into(), Json::from("late"))]),
        1001,
    );
    assert_eq!(late.status, RunStatus::Failed);
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

fn ready_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_map",
      "entrypoint": "fanout",
      "limits": {"max_steps": 40, "max_depth": 3, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map",
          "items": "$.input.tasks",
          "child": "work",
          "concurrency": 2,
          "schedule": "ready",
          "item_id": "$.item.task_id",
          "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "work": {"type": "task", "handler": "demo.work"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

fn task_item(task_id: &str, depends_on: &[&str]) -> Json {
    Json::object([
        ("task_id".into(), Json::from(task_id)),
        (
            "depends_on".into(),
            Json::Array(depends_on.iter().copied().map(Json::from).collect()),
        ),
    ])
}

fn ready_input(items: Vec<Json>) -> Json {
    Json::object([("tasks".into(), Json::Array(items))])
}

struct Admitted {
    id: String,
    index: u64,
    task_id: String,
    branch_path: String,
}

fn admitted(effects: &[Effect]) -> Vec<Admitted> {
    effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::InvokeHandler {
                invocation_id,
                input,
                branch_path,
                ..
            } => {
                let index = input.get("index")?.as_number()?.as_u64()?;
                let task_id = input.get("item")?.get("task_id")?.as_str()?.to_string();
                Some(Admitted {
                    id: invocation_id.clone(),
                    index,
                    task_id,
                    branch_path: branch_path.clone(),
                })
            }
            _ => None,
        })
        .collect()
}

fn assert_no_child_journal(machine: &swe_native::WorkflowMachine) {
    assert!(machine
        .events()
        .iter()
        .all(|event| event.kind != EventKind::BranchSpawned));
}

#[test]
fn ready_map_admits_a_dependent_while_a_straggler_is_open() {
    let (document, sha) = load(&ready_definition());
    let input = ready_input(vec![
        task_item("slow", &[]),
        task_item("fast", &[]),
        task_item("after_fast", &["fast"]),
    ]);
    let (mut machine, outcome) = start_run(document, &sha, "straggler".into(), input, 0).unwrap();
    let open = admitted(&outcome.effects);
    assert_eq!(
        open.iter()
            .map(|item| item.task_id.as_str())
            .collect::<Vec<_>>(),
        vec!["slow", "fast"]
    );
    assert!(open.iter().all(|item| item
        .branch_path
        .ends_with(&format!("{}/{}", item.index, item.task_id))));
    let fast = open.iter().find(|item| item.task_id == "fast").unwrap();
    let slow = open.iter().find(|item| item.task_id == "slow").unwrap();
    let after_fast = machine.complete(
        &fast.id,
        Json::object([("ok".into(), Json::from("fast"))]),
        1,
    );
    let admitted_next = admitted(&after_fast.effects);
    assert_eq!(admitted_next.len(), 1);
    assert_eq!(admitted_next[0].task_id, "after_fast");
    assert!(machine
        .events()
        .iter()
        .any(|event| event.kind == EventKind::BranchSpawned
            && event.payload.get("item_id").and_then(Json::as_str) == Some("after_fast")));
    let _ = machine.complete(
        &slow.id,
        Json::object([("ok".into(), Json::from("slow"))]),
        2,
    );
    let done = machine.complete(
        &admitted_next[0].id,
        Json::object([("ok".into(), Json::from("after"))]),
        3,
    );
    assert_eq!(done.status, RunStatus::Completed);
    let output = machine.output();
    let results = output
        .get("fanout")
        .and_then(|value| value.get("result"))
        .and_then(Json::as_array)
        .unwrap();
    assert_eq!(results.len(), 3);
    assert_eq!(results[0].get("ok").and_then(Json::as_str), Some("slow"));
    assert_eq!(results[1].get("ok").and_then(Json::as_str), Some("fast"));
    assert_eq!(results[2].get("ok").and_then(Json::as_str), Some("after"));
}

#[test]
fn ready_map_invocation_ids_survive_a_different_completion_order() {
    let items = vec![
        task_item("slow", &[]),
        task_item("fast", &[]),
        task_item("after_fast", &["fast"]),
        task_item("after_slow", &["slow"]),
    ];
    let ids_fast_first = invocation_ids_for(&items, true);
    let ids_slow_first = invocation_ids_for(&items, false);
    assert_eq!(ids_fast_first, ids_slow_first);
    assert_eq!(ids_fast_first.len(), 4);
    assert!(ids_fast_first["after_fast"].contains("after_fast"));
    assert!(ids_fast_first["slow"].contains("0/slow"));
}

fn invocation_ids_for(
    items: &[Json],
    complete_fast_first: bool,
) -> std::collections::BTreeMap<String, String> {
    let (document, sha) = load(&ready_definition());
    let (mut machine, outcome) = start_run(
        document,
        &sha,
        "replay".into(),
        ready_input(items.to_vec()),
        0,
    )
    .unwrap();
    let mut pending = admitted(&outcome.effects);
    let mut ids = std::collections::BTreeMap::new();
    let mut now = 1u64;
    for item in &pending {
        ids.insert(item.task_id.clone(), item.id.clone());
    }
    let mut status = outcome.status;
    while status == RunStatus::Running {
        assert!(pending.len() <= 2, "peak concurrency exceeded");
        let position = if complete_fast_first {
            pending
                .iter()
                .position(|item| item.task_id == "fast")
                .unwrap_or(0)
        } else {
            0
        };
        let item = pending.remove(position);
        now += 1;
        let step = machine.complete(
            &item.id,
            Json::object([("ok".into(), Json::from(item.task_id.as_str()))]),
            now,
        );
        let spawned = admitted(&step.effects);
        for spawned in &spawned {
            ids.insert(spawned.task_id.clone(), spawned.id.clone());
        }
        pending.extend(spawned);
        status = step.status;
    }
    assert_eq!(status, RunStatus::Completed);
    ids
}

#[test]
fn swd_1_0_handler_serials_stay_on_the_global_clock() {
    let ordered = r#"{
      "version": "1.0",
      "name": "ordered_ids",
      "entrypoint": "fanout",
      "limits": {"max_steps": 12, "max_depth": 2, "timeout_seconds": 5},
      "nodes": {
        "fanout": {"type": "map", "items": "$.needs", "child": "work", "concurrency": 2, "next": "done"},
        "work": {"type": "task", "handler": "demo.work"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let input = Json::object([(
        "needs".into(),
        Json::Array(vec![Json::from("a"), Json::from("b")]),
    )]);
    let ids = handler_ids(ordered, input, false, |_, _, _| Json::empty_object());
    assert_eq!(
        ids.get(&("work".into(), "0".into(), 1)).map(String::as_str),
        Some("legacy:work:0:0:2")
    );
    assert_eq!(
        ids.get(&("work".into(), "1".into(), 1)).map(String::as_str),
        Some("legacy:work:1:0:3")
    );

    let fork = r#"{
      "version": "1.0",
      "name": "fork_ids",
      "entrypoint": "split",
      "limits": {"max_steps": 12, "max_depth": 2, "timeout_seconds": 5},
      "nodes": {
        "split": {"type": "fork", "branches": {"left": "task_a", "right": "task_b"}, "join": "together"},
        "task_a": {"type": "task", "handler": "demo.left"},
        "task_b": {"type": "task", "handler": "demo.right"},
        "together": {"type": "join", "mode": "all", "next": "done"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let ids = handler_ids(fork, Json::empty_object(), false, |_, _, _| {
        Json::empty_object()
    });
    assert_eq!(
        ids.get(&("task_a".into(), "left".into(), 1))
            .map(String::as_str),
        Some("legacy:task_a:left:0:2")
    );
    assert_eq!(
        ids.get(&("task_b".into(), "right".into(), 1))
            .map(String::as_str),
        Some("legacy:task_b:right:0:3")
    );
}

#[test]
fn parallel_nested_maps_keep_serials_when_the_other_branch_finishes_first() {
    let raw = parallel_fork_maps_definition();
    let input = ready_child_input(&[task_item("outer", &[])], false);
    let low = handler_ids(&raw, input.clone(), false, |_, _, _| Json::empty_object());
    let high = handler_ids(&raw, input, true, |_, _, _| Json::empty_object());
    let left_low = part_serial(&low, "left_part");
    let left_high = part_serial(&high, "left_part");
    let right_low = part_serial(&low, "right_part");
    let right_high = part_serial(&high, "right_part");
    assert_eq!(
        left_low, left_high,
        "left_part serial followed completion order: {left_low} vs {left_high}"
    );
    assert_eq!(
        right_low, right_high,
        "right_part serial followed completion order: {right_low} vs {right_high}"
    );
    assert_ne!(
        left_low, right_low,
        "parallel parts collapsed to one serial"
    );
    assert_eq!(
        low, high,
        "another completion order changed an invocation id"
    );
}

fn part_serial(ids: &std::collections::BTreeMap<(String, String, u32), String>, node: &str) -> u64 {
    let (_, id) = ids
        .iter()
        .find(|((name, _, visit), _)| name == node && *visit == 1)
        .unwrap_or_else(|| panic!("{node} was not scheduled"));
    id.rsplit(':')
        .next()
        .unwrap()
        .parse()
        .unwrap_or_else(|_| panic!("{node} id {id}"))
}

#[test]
fn ready_child_graphs_keep_invocation_ids_across_completion_order() {
    let items = vec![
        task_item("slow", &[]),
        task_item("fast", &[]),
        task_item("after_fast", &["fast"]),
        task_item("after_slow", &["slow"]),
    ];
    for (name, raw) in [
        ("decision", gate_ready_definition()),
        ("tasks", two_task_ready_definition()),
        ("fork", fork_ready_definition()),
        ("nested_map", nested_map_ready_definition()),
        ("reused_map", reused_map_ready_definition()),
        ("loop", loop_ready_definition()),
    ] {
        let input = ready_child_input(&items, name == "nested_map" || name == "reused_map");
        let low = handler_ids(&raw, input.clone(), false, child_output);
        let high = handler_ids(&raw, input, true, child_output);
        assert_eq!(low, high, "{name}");
        assert!(low.len() >= 4, "{name} scheduled no item handlers");
        let mut unique = std::collections::BTreeSet::new();
        for id in low.values() {
            assert!(unique.insert(id.clone()), "{name} repeated {id}");
        }
        let work = low
            .iter()
            .filter(|((node, path, _), _)| node == "work" && path.contains("after_"))
            .map(|(_, id)| id.as_str())
            .collect::<Vec<_>>();
        if name == "decision" {
            assert_eq!(work.len(), 2, "{name}");
            assert_ne!(work[0].rsplit(':').next(), work[1].rsplit(':').next());
        }
    }
}

fn child_output(node: &str, _path: &str, visit: u32) -> Json {
    if node == "body" && visit == 1 {
        Json::object([("go".into(), Json::from(true))])
    } else {
        Json::object([("go".into(), Json::from(false))])
    }
}

fn handler_ids(
    raw: &str,
    input: Json,
    high_path_first: bool,
    mut output_for: impl FnMut(&str, &str, u32) -> Json,
) -> std::collections::BTreeMap<(String, String, u32), String> {
    let (document, sha) = load(raw);
    let (mut machine, outcome) = start_run(document, &sha, "legacy".into(), input, 0).unwrap();
    let mut pending = handler_effects(&outcome.effects);
    let mut ids = std::collections::BTreeMap::new();
    let mut visits: std::collections::BTreeMap<(String, String), u32> =
        std::collections::BTreeMap::new();
    let mut now = 1u64;
    let mut status = outcome.status;
    while status == RunStatus::Running {
        assert!(!pending.is_empty(), "running without a handler");
        let index = if high_path_first {
            pending
                .iter()
                .enumerate()
                .max_by(|left, right| effect_path(left.1).cmp(effect_path(right.1)))
                .unwrap()
                .0
        } else {
            pending
                .iter()
                .enumerate()
                .min_by(|left, right| effect_path(left.1).cmp(effect_path(right.1)))
                .unwrap()
                .0
        };
        let effect = pending.remove(index);
        let Effect::InvokeHandler {
            invocation_id,
            node_id,
            branch_path,
            ..
        } = effect
        else {
            unreachable!("handler effect");
        };
        let seen = visits
            .entry((node_id.clone(), branch_path.clone()))
            .or_insert(0);
        *seen += 1;
        let visit = *seen;
        ids.insert(
            (node_id.clone(), branch_path.clone(), visit),
            invocation_id.clone(),
        );
        now += 1;
        let step = machine.complete(
            &invocation_id,
            output_for(&node_id, &branch_path, visit),
            now,
        );
        pending.extend(handler_effects(&step.effects));
        status = step.status;
        if status == RunStatus::Failed {
            panic!(
                "{}",
                machine
                    .error()
                    .map(|error| error.message.clone())
                    .unwrap_or_default()
            );
        }
    }
    assert_eq!(status, RunStatus::Completed);
    ids
}

fn handler_effects(effects: &[Effect]) -> Vec<Effect> {
    effects
        .iter()
        .filter(|effect| matches!(effect, Effect::InvokeHandler { .. }))
        .cloned()
        .collect()
}

fn effect_path(effect: &Effect) -> &str {
    match effect {
        Effect::InvokeHandler { branch_path, .. } => branch_path,
        Effect::InvokeProvider { branch_path, .. } => branch_path,
        Effect::CancelInvocation { .. } => "",
    }
}

fn ready_child_input(items: &[Json], with_parts: bool) -> Json {
    let items = items
        .iter()
        .map(|item| {
            let mut item = item.clone();
            if with_parts {
                if let Json::Object(fields) = &mut item {
                    fields.insert(
                        "parts".into(),
                        Json::Array(vec![Json::from("p0"), Json::from("p1")]),
                    );
                }
            }
            item
        })
        .collect();
    ready_input(items)
}

fn gate_ready_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_gate",
      "entrypoint": "fanout",
      "limits": {"max_steps": 40, "max_depth": 4, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map", "items": "$.input.tasks", "child": "gate", "concurrency": 2,
          "schedule": "ready", "item_id": "$.item.task_id", "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "gate": {
          "type": "decision", "evaluator": "expression",
          "condition": {"eq": [{"var": "$.item.task_id"}, {"const": ""}]},
          "routes": {"true": "work", "false": "work"}
        },
        "work": {"type": "task", "handler": "demo.work"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

fn two_task_ready_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_tasks",
      "entrypoint": "fanout",
      "limits": {"max_steps": 40, "max_depth": 4, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map", "items": "$.input.tasks", "child": "first", "concurrency": 2,
          "schedule": "ready", "item_id": "$.item.task_id", "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "first": {"type": "task", "handler": "demo.first", "next": "second"},
        "second": {"type": "task", "handler": "demo.second"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

fn fork_ready_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_fork",
      "entrypoint": "fanout",
      "limits": {"max_steps": 80, "max_depth": 4, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map", "items": "$.input.tasks", "child": "split", "concurrency": 2,
          "schedule": "ready", "item_id": "$.item.task_id", "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "split": {"type": "fork", "branches": {"left": "lane_a", "right": "lane_b"}, "join": "joined"},
        "lane_a": {"type": "task", "handler": "demo.left", "next": "lane_a2"},
        "lane_a2": {"type": "task", "handler": "demo.left2"},
        "lane_b": {"type": "task", "handler": "demo.right"},
        "joined": {"type": "join", "mode": "all"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

fn parallel_fork_maps_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_parallel_maps",
      "entrypoint": "fanout",
      "limits": {"max_steps": 500, "max_depth": 6, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map", "items": "$.input.tasks", "child": "split", "concurrency": 2,
          "schedule": "ready", "item_id": "$.item.task_id", "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "split": {"type": "fork", "branches": {"left": "lane_a", "right": "lane_b"}, "join": "joined"},
        "lane_a": {"type": "task", "handler": "demo.left", "next": "left_map"},
        "left_map": {"type": "map", "items": "$.input.tasks", "child": "left_part", "next": "lane_a2"},
        "left_part": {"type": "task", "handler": "demo.left_part"},
        "lane_a2": {"type": "task", "handler": "demo.left2"},
        "lane_b": {"type": "task", "handler": "demo.right", "next": "right_map"},
        "right_map": {"type": "map", "items": "$.input.tasks", "child": "right_part", "next": "right_tail"},
        "right_part": {"type": "task", "handler": "demo.right_part"},
        "right_tail": {"type": "task", "handler": "demo.right_tail"},
        "joined": {"type": "join", "mode": "all"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

fn nested_map_ready_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_nested",
      "entrypoint": "fanout",
      "limits": {"max_steps": 80, "max_depth": 4, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map", "items": "$.input.tasks", "child": "inner", "concurrency": 2,
          "schedule": "ready", "item_id": "$.item.task_id", "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "inner": {"type": "map", "items": "$.item.parts", "child": "part", "concurrency": 2, "next": "tail"},
        "part": {"type": "task", "handler": "demo.part"},
        "tail": {"type": "task", "handler": "demo.tail"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

fn reused_map_ready_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_reused",
      "entrypoint": "fanout",
      "limits": {"max_steps": 80, "max_depth": 4, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map", "items": "$.input.tasks", "child": "first_map", "concurrency": 2,
          "schedule": "ready", "item_id": "$.item.task_id", "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "first_map": {"type": "map", "items": "$.item.parts", "child": "part", "concurrency": 2, "next": "second_map"},
        "second_map": {"type": "map", "items": "$.item.parts", "child": "part", "concurrency": 2, "next": "tail"},
        "part": {"type": "task", "handler": "demo.part"},
        "tail": {"type": "task", "handler": "demo.tail"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

fn loop_ready_definition() -> String {
    r#"{
      "version": "1.1",
      "name": "ready_loop",
      "entrypoint": "fanout",
      "limits": {"max_steps": 80, "max_depth": 4, "timeout_seconds": 30},
      "nodes": {
        "fanout": {
          "type": "map", "items": "$.input.tasks", "child": "body", "concurrency": 2,
          "schedule": "ready", "item_id": "$.item.task_id", "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "body": {"type": "task", "handler": "demo.body", "next": "again"},
        "again": {
          "type": "decision", "evaluator": "expression",
          "condition": {"eq": [{"var": "$.body.result.go"}, {"const": true}]},
          "routes": {"true": "body", "false": "tail"}
        },
        "tail": {"type": "task", "handler": "demo.tail"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#
    .to_string()
}

#[test]
fn ready_map_rejects_bad_graphs_before_any_child() {
    let cases = [
        (
            vec![task_item("a", &["missing"])],
            "unknown item dependency",
        ),
        (
            vec![task_item("a", &[]), task_item("a", &[])],
            "duplicate item id",
        ),
        (vec![task_item("a", &["a"])], "depends on itself"),
        (
            vec![task_item("a", &["b"]), task_item("b", &["a"])],
            "item dependency cycle",
        ),
        (vec![Json::from("bare")], "must be an object"),
    ];
    for (items, needle) in cases {
        let (document, sha) = load(&ready_definition());
        let (machine, outcome) =
            start_run(document, &sha, "bad".into(), ready_input(items), 0).unwrap();
        assert_eq!(outcome.status, RunStatus::Failed, "{needle}");
        let error = machine.error().expect(needle);
        assert_eq!(error.kind, ErrorKind::Preflight, "{needle}");
        assert!(error.message.contains(needle), "{}", error.message);
        assert!(admitted(&outcome.effects).is_empty(), "{needle}");
        assert_no_child_journal(&machine);
    }
}

#[test]
fn ready_map_cancel_stops_admission() {
    let (document, sha) = load(&ready_definition());
    let input = ready_input(vec![
        task_item("a", &[]),
        task_item("b", &[]),
        task_item("c", &[]),
        task_item("d", &[]),
    ]);
    let (mut machine, outcome) = start_run(document, &sha, "cancel".into(), input, 0).unwrap();
    let open = admitted(&outcome.effects);
    assert_eq!(open.len(), 2);
    let cancelled = machine.cancel(1);
    assert_eq!(cancelled.status, RunStatus::Cancelled);
    let cancels = cancelled
        .effects
        .iter()
        .filter(|effect| matches!(effect, Effect::CancelInvocation { .. }))
        .count();
    assert_eq!(cancels, 2);
    for item in &open {
        let step = machine.complete(&item.id, Json::object([("ok".into(), Json::from("x"))]), 2);
        assert_eq!(step.status, RunStatus::Cancelled);
        assert!(admitted(&step.effects).is_empty());
    }
}

#[test]
fn ready_map_child_failure_admits_nothing_more() {
    let (document, sha) = load(&ready_definition());
    let input = ready_input(vec![
        task_item("slow", &[]),
        task_item("fast", &[]),
        task_item("after_fast", &["fast"]),
    ]);
    let (mut machine, outcome) = start_run(document, &sha, "fail".into(), input, 0).unwrap();
    let open = admitted(&outcome.effects);
    let fast = open.iter().find(|item| item.task_id == "fast").unwrap();
    let failed = machine.fail(&fast.id, RuntimeError::handler("nope"), 1);
    assert_eq!(failed.status, RunStatus::Failed);
    assert!(admitted(&failed.effects).is_empty());
}

#[test]
fn ready_map_never_exceeds_concurrency() {
    let mut raw = ready_definition();
    raw = raw.replace("\"concurrency\": 2", "\"concurrency\": 3");
    let (document, sha) = load(&raw);
    let items: Vec<Json> = (0..12)
        .map(|index| task_item(&format!("t{index}"), &[]))
        .collect();
    let (mut machine, outcome) =
        start_run(document, &sha, "cap".into(), ready_input(items), 0).unwrap();
    let mut pending = admitted(&outcome.effects);
    assert!(pending.len() <= 3);
    let mut seen = pending.len();
    let mut now = 1u64;
    let mut status = outcome.status;
    while status == RunStatus::Running {
        assert!(pending.len() <= 3);
        let item = pending.remove(0);
        now += 1;
        let step = machine.complete(
            &item.id,
            Json::object([("ok".into(), Json::from(item.task_id.as_str()))]),
            now,
        );
        let spawned = admitted(&step.effects);
        seen += spawned.len();
        pending.extend(spawned);
        status = step.status;
    }
    assert_eq!(status, RunStatus::Completed);
    assert_eq!(seen, 12);
}

#[test]
fn swd_1_0_rejects_ready_fields_and_ordered_1_1_keeps_input_order() {
    let raw = r#"{
      "version": "1.0",
      "name": "legacy",
      "entrypoint": "fanout",
      "limits": {"max_steps": 8, "max_depth": 2, "timeout_seconds": 5},
      "nodes": {
        "fanout": {
          "type": "map",
          "items": "$.needs",
          "child": "work",
          "schedule": "ready",
          "item_id": "$.item.task_id",
          "item_depends_on": "$.item.depends_on",
          "next": "done"
        },
        "work": {"type": "task", "handler": "demo.work"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(raw);
    let error = match start_run(document, &sha, "legacy".into(), Json::empty_object(), 0) {
        Err(error) => error,
        Ok(_) => panic!("1.0 cannot carry ready fields"),
    };
    assert_eq!(error.kind, ErrorKind::Preflight);

    let ordered = r#"{
      "version": "1.1",
      "name": "ordered",
      "entrypoint": "fanout",
      "limits": {"max_steps": 20, "max_depth": 2, "timeout_seconds": 5},
      "nodes": {
        "fanout": {
          "type": "map",
          "items": "$.needs",
          "child": "work",
          "concurrency": 1,
          "schedule": "ordered",
          "next": "done"
        },
        "work": {"type": "task", "handler": "demo.work"},
        "done": {"type": "end", "status": "completed"}
      }
    }"#;
    let (document, sha) = load(ordered);
    let input = Json::object([(
        "needs".into(),
        Json::Array(vec![Json::from("a"), Json::from("b"), Json::from("c")]),
    )]);
    let (mut machine, outcome) = start_run(document, &sha, "ordered".into(), input, 0).unwrap();
    let first = admitted_index(&outcome.effects);
    assert_eq!(first, vec![0]);
    let step = machine.complete(
        &invoke_ids(&outcome.effects)[0],
        Json::object([("n".into(), Json::from(0))]),
        1,
    );
    assert_eq!(admitted_index(&step.effects), vec![1]);
}

#[test]
fn map_fields_reject_wrong_type_null_and_v1_presence() {
    let rejected = [
        (
            r#"{"version":"1.1","name":"bad_type","entrypoint":"fanout","limits":{"max_steps":4,"max_depth":2,"timeout_seconds":5},"nodes":{"fanout":{"type":"map","items":"$.needs","child":"work","schedule":123,"next":"done"},"work":{"type":"task","handler":"demo.work"},"done":{"type":"end","status":"completed"}}}"#,
            "nodes.fanout.schedule",
            "must be a string",
        ),
        (
            r#"{"version":"1.1","name":"bad_schedule_null","entrypoint":"fanout","limits":{"max_steps":4,"max_depth":2,"timeout_seconds":5},"nodes":{"fanout":{"type":"map","items":"$.needs","child":"work","schedule":null,"next":"done"},"work":{"type":"task","handler":"demo.work"},"done":{"type":"end","status":"completed"}}}"#,
            "nodes.fanout.schedule",
            "must be a string",
        ),
        (
            r#"{"version":"1.1","name":"bad_null","entrypoint":"fanout","limits":{"max_steps":4,"max_depth":2,"timeout_seconds":5},"nodes":{"fanout":{"type":"map","items":"$.needs","child":"work","schedule":"ordered","item_id":null,"next":"done"},"work":{"type":"task","handler":"demo.work"},"done":{"type":"end","status":"completed"}}}"#,
            "nodes.fanout.item_id",
            "must be a string",
        ),
        (
            r#"{"version":"1.1","name":"bad_depends_null","entrypoint":"fanout","limits":{"max_steps":4,"max_depth":2,"timeout_seconds":5},"nodes":{"fanout":{"type":"map","items":"$.needs","child":"work","schedule":"ready","item_id":"$.item.task_id","item_depends_on":null,"next":"done"},"work":{"type":"task","handler":"demo.work"},"done":{"type":"end","status":"completed"}}}"#,
            "nodes.fanout.item_depends_on",
            "must be a string",
        ),
        (
            r#"{"version":"1.0","name":"explicit_ordered","entrypoint":"fanout","limits":{"max_steps":4,"max_depth":2,"timeout_seconds":5},"nodes":{"fanout":{"type":"map","items":"$.needs","child":"work","schedule":"ordered","next":"done"},"work":{"type":"task","handler":"demo.work"},"done":{"type":"end","status":"completed"}}}"#,
            "nodes.fanout.schedule",
            "require SWD 1.1",
        ),
    ];
    for (raw, path, needle) in rejected {
        let (document, sha) = load(raw);
        let error = match CompiledDefinition::load(document, &sha) {
            Err(error) => error,
            Ok(_) => panic!("load accepted {path}"),
        };
        assert_eq!(error.kind, ErrorKind::Preflight, "{path}");
        assert_eq!(error.path, path, "{}", error.message);
        assert!(
            error.message.contains(needle),
            "{}: {}",
            path,
            error.message
        );
    }

    let plain = r#"{"version":"1.0","name":"plain","entrypoint":"fanout","limits":{"max_steps":4,"max_depth":2,"timeout_seconds":5},"nodes":{"fanout":{"type":"map","items":"$.needs","child":"work","next":"done"},"work":{"type":"task","handler":"demo.work"},"done":{"type":"end","status":"completed"}}}"#;
    let (document, sha) = load(plain);
    CompiledDefinition::load(document, &sha).expect("1.0 map without schedule still loads");
}

fn admitted_index(effects: &[Effect]) -> Vec<u64> {
    effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::InvokeHandler { input, .. } => input.get("index")?.as_number()?.as_u64(),
            _ => None,
        })
        .collect()
}
