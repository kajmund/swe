//! Application-layer evidence model.
//!
//! `swe-native` has no Evidence / Need / Operation primitives today. This
//! module is therefore demo-level application code: it turns a raw provider
//! observation into evidence with a generic verification status and per-fact
//! provenance. The three statuses are deliberately generic — they say nothing
//! about suppliers:
//!
//! - `VERIFIED`: one authoritative source, above the confidence threshold.
//! - `AMBIGUOUS`: sources disagree, or confidence is below the threshold.
//! - `REJECTED`: no value, or no provenance at all.
//!
//! This module classifies evidence in the demo's vocabulary. Whether a status is
//! usable is the engine's decision: `gate_inputs` maps these values onto
//! `swe_native::NeedStatus` and asks `swe_native::OperationSpec::gate`.

use std::collections::BTreeMap;

use swe_native::Json;

use crate::operations::NeedRef;
use crate::{as_u64, field, field_str, obj, DemoError, DemoResult};

/// Confidence below this threshold makes an observation `AMBIGUOUS`.
pub const VERIFICATION_CONFIDENCE_BP: u16 = 8_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum EvidenceStatus {
    Verified,
    Ambiguous,
    Rejected,
}

impl EvidenceStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Verified => "VERIFIED",
            Self::Ambiguous => "AMBIGUOUS",
            Self::Rejected => "REJECTED",
        }
    }

    pub fn parse(value: &str) -> DemoResult<Self> {
        match value {
            "VERIFIED" => Ok(Self::Verified),
            "AMBIGUOUS" => Ok(Self::Ambiguous),
            "REJECTED" => Ok(Self::Rejected),
            other => Err(DemoError::new(format!("unknown evidence status {other:?}"))),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Evidence {
    pub need_id: String,
    pub supplier: String,
    pub fact: String,
    pub status: EvidenceStatus,
    pub value: Json,
    pub source_ref: Option<String>,
    pub confidence_bp: u16,
    pub reason: String,
}

impl Evidence {
    pub fn is_verified(&self) -> bool {
        self.status == EvidenceStatus::Verified
    }

    pub fn to_json(&self) -> Json {
        obj([
            ("need_id", Json::from(self.need_id.as_str())),
            ("supplier", Json::from(self.supplier.as_str())),
            ("fact", Json::from(self.fact.as_str())),
            ("status", Json::from(self.status.as_str())),
            ("value", self.value.clone()),
            (
                "source_ref",
                self.source_ref
                    .as_deref()
                    .map(Json::from)
                    .unwrap_or(Json::Null),
            ),
            (
                "confidence_bp",
                Json::from_u64(u64::from(self.confidence_bp)),
            ),
            ("reason", Json::from(self.reason.as_str())),
        ])
    }

    pub fn from_json(value: &Json) -> DemoResult<Self> {
        let source_ref = match value.get("source_ref") {
            None => None,
            Some(Json::Null) => None,
            Some(Json::String(source_ref)) => Some(source_ref.clone()),
            Some(other) => {
                return Err(DemoError::new(format!(
                    "source_ref must be a string or null, got {}",
                    other.json_kind()
                )))
            }
        };
        let confidence_bp = value
            .get("confidence_bp")
            .and_then(as_u64)
            .unwrap_or(0)
            .min(u64::from(u16::MAX)) as u16;
        Ok(Self {
            need_id: field_str(value, "need_id")?.to_string(),
            supplier: field_str(value, "supplier")?.to_string(),
            fact: field_str(value, "fact")?.to_string(),
            status: EvidenceStatus::parse(field_str(value, "status")?)?,
            value: value.get("value").cloned().unwrap_or(Json::Null),
            source_ref,
            confidence_bp,
            reason: field_str(value, "reason")?.to_string(),
        })
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct StatusCounts {
    pub verified: usize,
    pub ambiguous: usize,
    pub rejected: usize,
}

impl StatusCounts {
    pub fn to_json(self) -> Json {
        obj([
            ("VERIFIED", Json::from_usize(self.verified)),
            ("AMBIGUOUS", Json::from_usize(self.ambiguous)),
            ("REJECTED", Json::from_usize(self.rejected)),
        ])
    }
}

/// Latest-wins evidence store keyed by need id.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EvidenceSet {
    entries: BTreeMap<String, Evidence>,
}

impl EvidenceSet {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, evidence: Evidence) {
        self.entries.insert(evidence.need_id.clone(), evidence);
    }

    pub fn get(&self, need_id: &str) -> Option<&Evidence> {
        self.entries.get(need_id)
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn to_vec(&self) -> Vec<Evidence> {
        self.entries.values().cloned().collect()
    }

    pub fn to_json(&self) -> Json {
        Json::Array(self.to_vec().iter().map(Evidence::to_json).collect())
    }

    pub fn counts(&self) -> StatusCounts {
        let mut counts = StatusCounts::default();
        for evidence in self.entries.values() {
            match evidence.status {
                EvidenceStatus::Verified => counts.verified += 1,
                EvidenceStatus::Ambiguous => counts.ambiguous += 1,
                EvidenceStatus::Rejected => counts.rejected += 1,
            }
        }
        counts
    }

    pub fn verified_bool(&self, need_id: &str) -> Option<bool> {
        self.get(need_id)
            .filter(|evidence| evidence.is_verified())
            .and_then(|evidence| evidence.value.as_bool())
    }

    pub fn verified_u64(&self, need_id: &str) -> Option<u64> {
        self.get(need_id)
            .filter(|evidence| evidence.is_verified())
            .and_then(|evidence| as_u64(&evidence.value))
    }

    /// Accepts a plain array (as stored in machine locals) or an object with an
    /// `evidence` field.
    pub fn from_json(value: &Json) -> DemoResult<Self> {
        let items: &[Json] = match value {
            Json::Array(items) => items,
            Json::Object(_) => value
                .get("evidence")
                .and_then(Json::as_array)
                .ok_or_else(|| DemoError::new("evidence object must contain an evidence array"))?,
            other => {
                return Err(DemoError::new(format!(
                    "evidence must be an array or an object, got {}",
                    other.json_kind()
                )))
            }
        };
        let mut set = Self::new();
        for item in items {
            set.insert(Evidence::from_json(item)?);
        }
        Ok(set)
    }
}

/// Generic verification rule: provenance, confidence and source agreement.
pub fn verify(need: &NeedRef, observation: &Json) -> Evidence {
    let confidence_bp = observation
        .get("confidence_bp")
        .and_then(as_u64)
        .unwrap_or(0)
        .min(u64::from(u16::MAX)) as u16;
    let conflicting_sources = observation
        .get("conflicting_sources")
        .and_then(Json::as_bool)
        .unwrap_or(false);
    let value = observation.get("value").cloned().unwrap_or(Json::Null);
    let source_ref = observation
        .get("source_ref")
        .and_then(Json::as_str)
        .map(ToString::to_string);

    if value.is_missing_compatible_null() {
        return evidence(
            need,
            EvidenceStatus::Rejected,
            Json::Null,
            source_ref,
            confidence_bp,
            format!("no data: {}", observation_reason(observation)),
        );
    }
    let Some(source_ref) = source_ref else {
        return evidence(
            need,
            EvidenceStatus::Rejected,
            value,
            None,
            confidence_bp,
            "no provenance: observation carries no source reference".to_string(),
        );
    };
    if conflicting_sources {
        return evidence(
            need,
            EvidenceStatus::Ambiguous,
            value,
            Some(source_ref),
            confidence_bp,
            "sources disagree: conflicting source references for the same fact".to_string(),
        );
    }
    if confidence_bp < VERIFICATION_CONFIDENCE_BP {
        return evidence(
            need,
            EvidenceStatus::Ambiguous,
            value,
            Some(source_ref),
            confidence_bp,
            format!(
                "confidence {confidence_bp} bp is below the verification threshold {VERIFICATION_CONFIDENCE_BP} bp"
            ),
        );
    }
    evidence(
        need,
        EvidenceStatus::Verified,
        value,
        Some(source_ref),
        confidence_bp,
        "single authoritative source above the confidence threshold".to_string(),
    )
}

fn evidence(
    need: &NeedRef,
    status: EvidenceStatus,
    value: Json,
    source_ref: Option<String>,
    confidence_bp: u16,
    reason: String,
) -> Evidence {
    Evidence {
        need_id: need.id(),
        supplier: need.subject.clone(),
        fact: need.fact.clone(),
        status,
        value,
        source_ref,
        confidence_bp,
        reason,
    }
}

fn observation_reason(observation: &Json) -> String {
    observation
        .get("reason")
        .and_then(Json::as_str)
        .unwrap_or("provider returned no value")
        .to_string()
}

/// Handler behind the `found` route of the provider decision.
pub fn accept_handler(input: &Json) -> DemoResult<Json> {
    let need = crate::operations::need_from_json_pub(field(input, "need")?)?;
    let observation = field(input, "observation")?;
    Ok(verify(&need, observation).to_json())
}

/// Handler behind the `missing` route of the provider decision.
pub fn record_missing_handler(input: &Json) -> DemoResult<Json> {
    let need = crate::operations::need_from_json_pub(field(input, "need")?)?;
    let observation = field(input, "observation")?;
    Ok(verify(&need, observation).to_json())
}

/// Handler that merges the evidence batches produced by the fan-out waves.
pub fn merge_evidence_handler(input: &Json) -> DemoResult<Json> {
    let mut entries = Vec::new();
    for key in ["previous", "compliance", "prices", "extra"] {
        if let Some(batch) = input.get(key) {
            append_evidence(batch, &mut entries)?;
        }
    }
    let mut set = EvidenceSet::new();
    for evidence in entries {
        set.insert(evidence);
    }
    Ok(obj([
        ("evidence", set.to_json()),
        ("count", Json::from_usize(set.len())),
    ]))
}

fn append_evidence(value: &Json, out: &mut Vec<Evidence>) -> DemoResult<()> {
    match value {
        Json::Null => Ok(()),
        Json::Array(items) => {
            for item in items {
                out.push(Evidence::from_json(item)?);
            }
            Ok(())
        }
        Json::Object(_) => {
            if let Some(inner) = value.get("evidence") {
                return append_evidence(inner, out);
            }
            if let Some(inner) = value.get("result") {
                return append_evidence(inner, out);
            }
            Err(DemoError::new("unknown evidence batch shape"))
        }
        other => Err(DemoError::new(format!(
            "evidence batch must be an array or object, got {}",
            other.json_kind()
        ))),
    }
}
