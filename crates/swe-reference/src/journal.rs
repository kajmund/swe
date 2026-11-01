//! Pause/resume journal: definition hash, input, completed invocation results,
//! ledger events and the application charges.
//!
//! The journal is plain canonical JSON (the same canonical form the kernel uses
//! for the definition hash), so a paused investigation can be written to disk
//! and resumed by a fresh process with no shared memory. On resume the host
//! replays each completed invocation result into a fresh machine in the same
//! order the effects are emitted, and rebuilds the Sweycoin ledger with
//! [`Ledger::replay`] — no handler or provider is re-executed and nothing is
//! charged twice.

use std::path::Path;

use swe_budget::{Amount, Event, EventKind};
use swe_native::{canonical_json_bytes, parse_json_str, sha256_hex, Json};

use crate::domain::Scenario;
use crate::host::{ActionCharge, Tariffs};
use crate::{as_u64, field, field_array, field_str, field_u64, obj, DemoError, DemoResult};

pub const JOURNAL_VERSION: u64 = 1;

/// Version of the engine need/evidence/operation contracts used by the demo.
pub const CONTRACT_VERSION: u32 = 1;

#[derive(Clone, Debug, PartialEq)]
pub struct JournalCompletedInvocation {
    pub invocation_id: String,
    pub node_id: String,
    pub output: Json,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Journal {
    pub run_id: String,
    pub definition_sha256: String,
    /// Version of the engine contracts this run used. Pinned so a resume after
    /// a code change is refused instead of reinterpreting old events.
    pub contract_version: u32,
    /// Hash of the tariffs this run was priced under. A resume under different
    /// tariffs would silently reprice the second half.
    pub tariff_sha256: String,
    /// The dependency versions this run actually used, as recorded.
    pub recorded_dependency_versions: std::collections::BTreeMap<String, u32>,
    pub document: Json,
    pub input: Json,
    pub now_ms: u64,
    pub scenario: Scenario,
    pub budget_milli: Amount,
    pub handler_cost_milli: Amount,
    pub provider_cost_milli: Amount,
    pub completed: Vec<JournalCompletedInvocation>,
    pub charges: Vec<ActionCharge>,
    pub ledger_events: Vec<Event>,
    /// Engine journal events from earlier segments.
    pub journal_events: Vec<swe_native::JournalEvent>,
    pub handler_calls: usize,
    pub provider_calls: usize,
}

/// The dependency versions this build of the demo actually runs.
///
/// Recorded in the journal so a resume can compare *what was recorded* with
/// *what is running now*. Comparing a constant to itself would prove nothing.
pub fn current_dependency_versions() -> std::collections::BTreeMap<String, u32> {
    let mut versions = std::collections::BTreeMap::new();
    versions.insert("mock.supplier_facts".to_string(), CONTRACT_VERSION);
    versions
}

/// Hash of the tariffs a run was priced under.
///
/// Tariffs are not part of the definition hash, so without this a resume under
/// different prices would silently reprice the second half of a run.
pub fn tariff_sha256(tariffs: &Tariffs) -> DemoResult<String> {
    let payload = obj([
        ("handler_milli", Json::from_u64(tariffs.handler_milli)),
        ("provider_milli", Json::from_u64(tariffs.provider_milli)),
    ]);
    Ok(sha256_hex(&payload)?)
}

impl Journal {
    /// The tariffs this run was billed with, as recorded.
    pub fn recorded_tariffs(&self) -> Tariffs {
        Tariffs {
            handler_milli: self.handler_cost_milli,
            provider_milli: self.provider_cost_milli,
        }
    }

    pub fn to_json(&self) -> Json {
        obj([
            ("journal_version", Json::from_u64(JOURNAL_VERSION)),
            ("run_id", Json::from(self.run_id.as_str())),
            (
                "definition_sha256",
                Json::from(self.definition_sha256.as_str()),
            ),
            (
                "contract_version",
                Json::from_u64(u64::from(self.contract_version)),
            ),
            ("tariff_sha256", Json::from(self.tariff_sha256.as_str())),
            (
                "dependency_versions",
                dependency_versions_to_json(&self.recorded_dependency_versions),
            ),
            (
                "journal_events",
                journal_events_to_json(&self.journal_events),
            ),
            ("document", self.document.clone()),
            ("input", self.input.clone()),
            ("now_ms", Json::from_u64(self.now_ms)),
            ("scenario", Json::from(self.scenario.as_str())),
            ("budget_milli", Json::from_u64(self.budget_milli)),
            (
                "handler_cost_milli",
                Json::from_u64(self.handler_cost_milli),
            ),
            (
                "provider_cost_milli",
                Json::from_u64(self.provider_cost_milli),
            ),
            (
                "completed",
                Json::Array(
                    self.completed
                        .iter()
                        .map(completed_invocation_to_json)
                        .collect(),
                ),
            ),
            (
                "charges",
                Json::Array(self.charges.iter().map(ActionCharge::to_json).collect()),
            ),
            (
                "ledger_events",
                Json::Array(self.ledger_events.iter().map(event_to_json).collect()),
            ),
            ("handler_calls", Json::from_usize(self.handler_calls)),
            ("provider_calls", Json::from_usize(self.provider_calls)),
        ])
    }

    pub fn from_json(value: &Json) -> DemoResult<Self> {
        let version = field_u64(value, "journal_version")?;
        if version != JOURNAL_VERSION {
            return Err(DemoError::new(format!(
                "unsupported journal version {version}, expected {JOURNAL_VERSION}"
            )));
        }
        let completed = field_array(value, "completed")?
            .iter()
            .map(completed_invocation_from_json)
            .collect::<DemoResult<Vec<_>>>()?;
        let charges = field_array(value, "charges")?
            .iter()
            .map(ActionCharge::from_json)
            .collect::<DemoResult<Vec<_>>>()?;
        let ledger_events = field_array(value, "ledger_events")?
            .iter()
            .map(event_from_json)
            .collect::<DemoResult<Vec<_>>>()?;
        let journal = Self {
            journal_events: journal_events_from_json(field(value, "journal_events")?)?,
            run_id: field_str(value, "run_id")?.to_string(),
            definition_sha256: field_str(value, "definition_sha256")?.to_string(),
            // Strict: a missing version is not "the current one". Reading an
            // old journal under new rules is exactly what must not happen.
            contract_version: u32::try_from(field_u64(value, "contract_version")?)
                .map_err(|_| DemoError::new("contract_version is out of range"))?,
            tariff_sha256: field_str(value, "tariff_sha256")?.to_string(),
            recorded_dependency_versions: dependency_versions_from_json(field(
                value,
                "dependency_versions",
            )?)?,
            document: field(value, "document")?.clone(),
            input: field(value, "input")?.clone(),
            now_ms: field_u64(value, "now_ms")?,
            scenario: Scenario::parse(field_str(value, "scenario")?)?,
            budget_milli: field_u64(value, "budget_milli")?,
            handler_cost_milli: field_u64(value, "handler_cost_milli")?,
            provider_cost_milli: field_u64(value, "provider_cost_milli")?,
            completed,
            charges,
            ledger_events,
            handler_calls: usize::try_from(field_u64(value, "handler_calls")?)
                .map_err(|_| DemoError::new("handler_calls is out of range"))?,
            provider_calls: usize::try_from(field_u64(value, "provider_calls")?)
                .map_err(|_| DemoError::new("provider_calls is out of range"))?,
        };
        journal.assert_tariff_consistency()?;
        Ok(journal)
    }

    /// Self-consistency: the recorded tariff hash must match the recorded
    /// tariffs. A journal whose own numbers disagree with its own hash is
    /// corrupt, so this is checked as soon as it is read.
    fn assert_tariff_consistency(&self) -> DemoResult<()> {
        let expected = tariff_sha256(&self.recorded_tariffs())?;
        if expected != self.tariff_sha256 {
            return Err(DemoError::new(
                "journal is inconsistent: tariff_sha256 does not match the recorded tariffs",
            ));
        }
        Ok(())
    }

    /// The document hash must still match the document in the journal.
    pub fn validate(&self) -> DemoResult<()> {
        let actual = sha256_hex(&self.document)?;
        if actual != self.definition_sha256 {
            return Err(DemoError::new(format!(
                "journal definition SHA-256 mismatch: stored {}, computed {}",
                self.definition_sha256, actual
            )));
        }
        // The engine's header check is the contract: schema version, definition
        // hash, contract version and dependency versions must all match.
        //
        // The recorded values come from the file; the current ones come from
        // this build. Comparing a constant to itself would prove nothing.
        swe_native::JournalHeader {
            schema_version: swe_native::JOURNAL_SCHEMA_VERSION,
            run_id: self.run_id.clone(),
            definition_sha256: self.definition_sha256.clone(),
            contract_version: self.contract_version,
            dependency_versions: self.recorded_dependency_versions.clone(),
        }
        .verify(&actual, CONTRACT_VERSION, &current_dependency_versions())
        .map_err(|error| DemoError::new(format!("journal cannot be resumed: {error}")))?;

        // Tariffs are not covered by the definition hash, so they are pinned
        // separately: a resume under different prices must be refused. The
        // recorded hash is checked against the tariffs the run was actually
        // billed with, which the caller knows because it replays the ledger.
        let recorded_tariffs = self.tariff_sha256.clone();
        let current_tariffs = tariff_sha256(&self.recorded_tariffs())?;
        if recorded_tariffs != current_tariffs {
            return Err(DemoError::new(
                "journal cannot be resumed: tariffs changed since it was written",
            ));
        }
        Ok(())
    }
}

fn completed_invocation_to_json(completed: &JournalCompletedInvocation) -> Json {
    obj([
        (
            "invocation_id",
            Json::from(completed.invocation_id.as_str()),
        ),
        ("node_id", Json::from(completed.node_id.as_str())),
        ("output", completed.output.clone()),
    ])
}

fn completed_invocation_from_json(value: &Json) -> DemoResult<JournalCompletedInvocation> {
    Ok(JournalCompletedInvocation {
        invocation_id: field_str(value, "invocation_id")?.to_string(),
        node_id: field_str(value, "node_id")?.to_string(),
        output: field(value, "output")?.clone(),
    })
}

fn event_to_json(event: &Event) -> Json {
    obj([
        ("kind", Json::from(event_kind_str(event.kind))),
        ("run_id", Json::from(event.run_id.as_str())),
        ("account", Json::from(event.account.as_str())),
        (
            "counterpart",
            event
                .counterpart
                .as_deref()
                .map(Json::from)
                .unwrap_or(Json::Null),
        ),
        ("amount_milli", Json::from_u64(event.amount_milli)),
        (
            "reservation_id",
            event
                .reservation_id
                .map(Json::from_u64)
                .unwrap_or(Json::Null),
        ),
        ("key", Json::from(event.key.as_str())),
    ])
}

fn event_from_json(value: &Json) -> DemoResult<Event> {
    let counterpart = match value.get("counterpart") {
        None | Some(Json::Null) => None,
        Some(Json::String(counterpart)) => Some(counterpart.clone()),
        Some(other) => {
            return Err(DemoError::new(format!(
                "counterpart must be a string or null, got {}",
                other.json_kind()
            )))
        }
    };
    Ok(Event {
        kind: event_kind_from_str(field_str(value, "kind")?)?,
        run_id: field_str(value, "run_id")?.to_string(),
        account: field_str(value, "account")?.to_string(),
        counterpart,
        amount_milli: field_u64(value, "amount_milli")?,
        reservation_id: value.get("reservation_id").and_then(as_u64),
        key: field_str(value, "key")?.to_string(),
    })
}

fn event_kind_str(kind: EventKind) -> &'static str {
    match kind {
        EventKind::Allocate => "allocate",
        EventKind::Reserve => "reserve",
        EventKind::Spend => "spend",
        EventKind::Release => "release",
        EventKind::Transfer => "transfer",
        EventKind::Reward => "reward",
    }
}

fn event_kind_from_str(value: &str) -> DemoResult<EventKind> {
    match value {
        "allocate" => Ok(EventKind::Allocate),
        "reserve" => Ok(EventKind::Reserve),
        "spend" => Ok(EventKind::Spend),
        "release" => Ok(EventKind::Release),
        "transfer" => Ok(EventKind::Transfer),
        "reward" => Ok(EventKind::Reward),
        other => Err(DemoError::new(format!(
            "unknown ledger event kind {other:?}"
        ))),
    }
}

/// Persist the journal as canonical JSON with a trailing newline.
pub fn write_journal(path: &Path, journal: &Journal) -> DemoResult<()> {
    let mut bytes = canonical_json_bytes(&journal.to_json())?;
    bytes.push(b'\n');
    std::fs::write(path, bytes)?;
    Ok(())
}

/// Read a journal written by [`write_journal`].
pub fn read_journal(path: &Path) -> DemoResult<Journal> {
    let raw = std::fs::read_to_string(path)?;
    let value = parse_json_str(&raw)?;
    Journal::from_json(&value)
}

fn journal_events_to_json(events: &[swe_native::JournalEvent]) -> Json {
    use swe_native::JournalEvent as E;
    Json::Array(
        events
            .iter()
            .map(|event| match event {
                E::AttemptStarted {
                    attempt_id,
                    delivery_key,
                    invocation_id,
                    action,
                    delivery,
                    started_at_ms,
                } => obj([
                    ("kind", Json::from("AttemptStarted")),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                    ("delivery_key", Json::from(delivery_key.as_str())),
                    ("invocation_id", Json::from(invocation_id.as_str())),
                    ("action", Json::from(action.as_str())),
                    ("delivery", Json::from(delivery.as_str())),
                    ("started_at_ms", Json::from_u64(*started_at_ms)),
                ]),
                E::ResultConfirmed {
                    attempt_id,
                    outcome,
                    confirmed_at_ms,
                } => obj([
                    ("kind", Json::from("ResultConfirmed")),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                    ("outcome", completion_outcome_to_json(outcome)),
                    ("confirmed_at_ms", Json::from_u64(*confirmed_at_ms)),
                ]),
                E::AttemptAbandoned {
                    attempt_id,
                    reason,
                    abandoned_at_ms,
                } => obj([
                    ("kind", Json::from("AttemptAbandoned")),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                    ("reason", Json::from(reason.as_str())),
                    ("abandoned_at_ms", Json::from_u64(*abandoned_at_ms)),
                ]),
                E::ChargeReserved {
                    charge_id,
                    attempt_id,
                    delivery_key,
                    amount_milli,
                    tariff_milli,
                    reserved_at_ms,
                } => obj([
                    ("kind", Json::from("ChargeReserved")),
                    ("charge_id", Json::from(charge_id.as_str())),
                    ("attempt_id", Json::from(attempt_id.as_str())),
                    ("delivery_key", Json::from(delivery_key.as_str())),
                    ("amount_milli", Json::from_u64(*amount_milli)),
                    ("tariff_milli", Json::from_u64(*tariff_milli)),
                    ("reserved_at_ms", Json::from_u64(*reserved_at_ms)),
                ]),
                E::ChargeSettled {
                    charge_id,
                    amount_milli,
                    settled_at_ms,
                } => obj([
                    ("kind", Json::from("ChargeSettled")),
                    ("charge_id", Json::from(charge_id.as_str())),
                    ("amount_milli", Json::from_u64(*amount_milli)),
                    ("settled_at_ms", Json::from_u64(*settled_at_ms)),
                ]),
                E::ChargeReleased {
                    charge_id,
                    reason,
                    released_at_ms,
                } => obj([
                    ("kind", Json::from("ChargeReleased")),
                    ("charge_id", Json::from(charge_id.as_str())),
                    ("reason", Json::from(reason.as_str())),
                    ("released_at_ms", Json::from_u64(*released_at_ms)),
                ]),
                E::ChargePendingReconciliation {
                    charge_id,
                    reason,
                    at_ms,
                } => obj([
                    ("kind", Json::from("ChargePendingReconciliation")),
                    ("charge_id", Json::from(charge_id.as_str())),
                    ("reason", Json::from(reason.as_str())),
                    ("at_ms", Json::from_u64(*at_ms)),
                ]),
            })
            .collect(),
    )
}

fn journal_events_from_json(value: &Json) -> DemoResult<Vec<swe_native::JournalEvent>> {
    use swe_native::{Delivery, JournalEvent as E};
    // An unreadable event log is a corrupt journal, not an empty one.
    let items = value
        .as_array()
        .ok_or_else(|| DemoError::new("journal_events must be an array"))?;
    let mut events = Vec::new();
    for item in items {
        let kind = field_str(item, "kind")?;
        let event = match kind {
            "AttemptStarted" => E::AttemptStarted {
                attempt_id: field_str(item, "attempt_id")?.to_string(),
                delivery_key: field_str(item, "delivery_key")?.to_string(),
                invocation_id: field_str(item, "invocation_id")?.to_string(),
                action: field_str(item, "action")?.to_string(),
                delivery: Delivery::parse(field_str(item, "delivery")?)
                    .ok_or_else(|| DemoError::new("unknown delivery in journal"))?,
                started_at_ms: item.get("started_at_ms").and_then(u64_of).unwrap_or(0),
            },
            "ResultConfirmed" => E::ResultConfirmed {
                attempt_id: field_str(item, "attempt_id")?.to_string(),
                outcome: completion_outcome_from_json(field(item, "outcome")?)?,
                confirmed_at_ms: item.get("confirmed_at_ms").and_then(u64_of).unwrap_or(0),
            },
            "AttemptAbandoned" => E::AttemptAbandoned {
                attempt_id: field_str(item, "attempt_id")?.to_string(),
                reason: field_str(item, "reason")?.to_string(),
                abandoned_at_ms: item.get("abandoned_at_ms").and_then(u64_of).unwrap_or(0),
            },
            "ChargeReserved" => E::ChargeReserved {
                charge_id: field_str(item, "charge_id")?.to_string(),
                attempt_id: field_str(item, "attempt_id")?.to_string(),
                delivery_key: field_str(item, "delivery_key")?.to_string(),
                amount_milli: item.get("amount_milli").and_then(u64_of).unwrap_or(0),
                tariff_milli: item.get("tariff_milli").and_then(u64_of).unwrap_or(0),
                reserved_at_ms: item.get("reserved_at_ms").and_then(u64_of).unwrap_or(0),
            },
            "ChargeSettled" => E::ChargeSettled {
                charge_id: field_str(item, "charge_id")?.to_string(),
                amount_milli: item.get("amount_milli").and_then(u64_of).unwrap_or(0),
                settled_at_ms: item.get("settled_at_ms").and_then(u64_of).unwrap_or(0),
            },
            "ChargeReleased" => E::ChargeReleased {
                charge_id: field_str(item, "charge_id")?.to_string(),
                reason: field_str(item, "reason")?.to_string(),
                released_at_ms: item.get("released_at_ms").and_then(u64_of).unwrap_or(0),
            },
            "ChargePendingReconciliation" => E::ChargePendingReconciliation {
                charge_id: field_str(item, "charge_id")?.to_string(),
                reason: field_str(item, "reason")?.to_string(),
                at_ms: item.get("at_ms").and_then(u64_of).unwrap_or(0),
            },
            other => return Err(DemoError::new(format!("unknown journal event {other:?}"))),
        };
        events.push(event);
    }
    Ok(events)
}

fn u64_of(value: &Json) -> Option<u64> {
    crate::as_u64(value)
}

/// Lossless encoding of a completion outcome: Ok, Err and Cancelled stay three
/// different facts, and the Ok payload survives.
fn completion_outcome_to_json(outcome: &swe_native::CompletionOutcome) -> Json {
    use swe_native::CompletionOutcome;
    match outcome {
        CompletionOutcome::Ok(value) => obj([("kind", Json::from("ok")), ("value", value.clone())]),
        CompletionOutcome::Err { kind, message } => obj([
            ("kind", Json::from("error")),
            ("error_kind", Json::from(kind.as_str())),
            ("message", Json::from(message.as_str())),
        ]),
        CompletionOutcome::Cancelled => obj([("kind", Json::from("cancelled"))]),
    }
}

fn completion_outcome_from_json(value: &Json) -> DemoResult<swe_native::CompletionOutcome> {
    use swe_native::CompletionOutcome;
    match field_str(value, "kind")? {
        "ok" => Ok(CompletionOutcome::Ok(
            value.get("value").cloned().unwrap_or(Json::Null),
        )),
        "error" => Ok(CompletionOutcome::Err {
            kind: field_str(value, "error_kind")?.to_string(),
            message: field_str(value, "message")?.to_string(),
        }),
        "cancelled" => Ok(CompletionOutcome::Cancelled),
        other => Err(DemoError::new(format!(
            "unknown completion outcome {other:?} in journal"
        ))),
    }
}

fn dependency_versions_to_json(versions: &std::collections::BTreeMap<String, u32>) -> Json {
    let pairs: Vec<(String, Json)> = versions
        .iter()
        .map(|(name, version)| (name.clone(), Json::from_u64(u64::from(*version))))
        .collect();
    Json::object(pairs)
}

fn dependency_versions_from_json(
    value: &Json,
) -> DemoResult<std::collections::BTreeMap<String, u32>> {
    let Some(object) = value.as_object() else {
        return Err(DemoError::new("dependency_versions must be an object"));
    };
    let mut versions = std::collections::BTreeMap::new();
    for (name, version) in object {
        let number = crate::as_u64(version)
            .ok_or_else(|| DemoError::new("dependency version must be an integer"))?;
        versions.insert(
            name.clone(),
            u32::try_from(number)
                .map_err(|_| DemoError::new("dependency version is out of range"))?,
        );
    }
    Ok(versions)
}

#[cfg(test)]
mod tests {
    use super::*;
    use swe_native::{CompletionOutcome, Delivery, JournalEvent};

    fn round_trip(event: JournalEvent) -> JournalEvent {
        let encoded = journal_events_to_json(std::slice::from_ref(&event));
        let mut decoded =
            journal_events_from_json(&encoded).expect("every encoded event must decode");
        assert_eq!(decoded.len(), 1);
        decoded.remove(0)
    }

    #[test]
    fn ok_payload_survives_the_round_trip() {
        let event = JournalEvent::ResultConfirmed {
            attempt_id: "a".to_string(),
            outcome: CompletionOutcome::Ok(obj([("answer", Json::from("beta"))])),
            confirmed_at_ms: 7,
        };
        assert_eq!(round_trip(event.clone()), event);
    }

    #[test]
    fn an_error_outcome_does_not_become_a_cancellation() {
        let event = JournalEvent::ResultConfirmed {
            attempt_id: "a".to_string(),
            outcome: CompletionOutcome::Err {
                kind: "timeout".to_string(),
                message: "provider timed out".to_string(),
            },
            confirmed_at_ms: 7,
        };
        let decoded = round_trip(event.clone());
        assert_eq!(decoded, event);
        assert!(matches!(
            decoded,
            JournalEvent::ResultConfirmed {
                outcome: CompletionOutcome::Err { .. },
                ..
            }
        ));
    }

    #[test]
    fn a_cancellation_stays_a_cancellation() {
        let event = JournalEvent::ResultConfirmed {
            attempt_id: "a".to_string(),
            outcome: CompletionOutcome::Cancelled,
            confirmed_at_ms: 7,
        };
        assert_eq!(round_trip(event.clone()), event);
        assert!(matches!(
            round_trip(event),
            JournalEvent::ResultConfirmed {
                outcome: CompletionOutcome::Cancelled,
                ..
            }
        ));
    }

    #[test]
    fn an_unknown_outcome_kind_is_refused() {
        let encoded = obj([
            ("kind", Json::from("ResultConfirmed")),
            ("attempt_id", Json::from("a")),
            ("outcome", obj([("kind", Json::from("maybe"))])),
            ("confirmed_at_ms", Json::from_u64(1)),
        ]);
        let outcome = journal_events_from_json(&Json::Array(vec![encoded]));
        assert!(outcome.is_err(), "an unknown outcome must not decode");
    }

    #[test]
    fn every_event_kind_round_trips() {
        let events = vec![
            JournalEvent::AttemptStarted {
                attempt_id: "a".to_string(),
                delivery_key: "k".to_string(),
                invocation_id: "i".to_string(),
                action: "act".to_string(),
                delivery: Delivery::ExternalIdempotent,
                started_at_ms: 1,
            },
            JournalEvent::ResultConfirmed {
                attempt_id: "a".to_string(),
                outcome: CompletionOutcome::Ok(Json::Null),
                confirmed_at_ms: 2,
            },
            JournalEvent::AttemptAbandoned {
                attempt_id: "b".to_string(),
                reason: "cancelled".to_string(),
                abandoned_at_ms: 3,
            },
            JournalEvent::ChargeReserved {
                charge_id: "c".to_string(),
                attempt_id: "a".to_string(),
                delivery_key: "k".to_string(),
                amount_milli: 500,
                tariff_milli: 500,
                reserved_at_ms: 4,
            },
            JournalEvent::ChargeSettled {
                charge_id: "c".to_string(),
                amount_milli: 500,
                settled_at_ms: 5,
            },
            JournalEvent::ChargeReleased {
                charge_id: "d".to_string(),
                reason: "no cost".to_string(),
                released_at_ms: 6,
            },
            JournalEvent::ChargePendingReconciliation {
                charge_id: "e".to_string(),
                reason: "unknown".to_string(),
                at_ms: 7,
            },
        ];
        let encoded = journal_events_to_json(&events);
        let decoded = journal_events_from_json(&encoded).expect("decodes");
        assert_eq!(decoded.len(), events.len());
        for (left, right) in decoded.iter().zip(events.iter()) {
            assert_eq!(left, right);
        }
    }
}
