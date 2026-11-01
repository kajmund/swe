//! Append-only Sweycoin ledger.
//!
//! The event log is the single source of truth; materialized balances must
//! always equal a fold over the log (`Ledger::replay`). Invariants enforced on
//! every mutation:
//!
//! 1. `available = allocated + incoming - outgoing - spent - reserved` never
//!    goes negative — reserve and transfer are checked against it.
//! 2. A reservation settles or releases exactly once; a settle may not exceed
//!    the reserved amount and atomically releases the unspent remainder.
//! 3. Idempotency keys: retrying an operation with the same key returns the
//!    stored outcome and records no new events; reusing a key with different
//!    parameters is rejected.
//! 4. Rewards are booked in a separate virtual pool and never affect
//!    `available` — they can never be spent.
//! 5. The log alone reconstructs identical state, including reservations and
//!    idempotency keys, so a crashed process can resume without double
//!    charging.

use std::collections::{HashMap, HashSet};

/// Thousandths of a Sweycoin; integers only, no floats.
pub type Amount = u64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind {
    Allocate,
    Reserve,
    Spend,
    Release,
    Transfer,
    Reward,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    pub kind: EventKind,
    pub run_id: String,
    pub account: String,
    pub counterpart: Option<String>,
    pub amount_milli: Amount,
    pub reservation_id: Option<u64>,
    pub key: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Balance {
    pub allocated: Amount,
    pub incoming: Amount,
    pub outgoing: Amount,
    pub spent: Amount,
    pub reserved: Amount,
}

impl Balance {
    /// Safe under the ledger invariants: every spend/reserve is pre-checked.
    pub fn available(&self) -> Amount {
        (self.allocated + self.incoming) - (self.outgoing + self.spent + self.reserved)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyOutcome {
    Allocated,
    Reserved(u64),
    Settled,
    Released,
    Transferred,
    Rewarded,
}

#[derive(Clone, Debug, PartialEq)]
struct KeySignature {
    kind: EventKind,
    account: String,
    amount_milli: Amount,
    outcome: KeyOutcome,
}

#[derive(Clone, Debug)]
struct Reservation {
    run_id: String,
    account: String,
    amount: Amount,
    settled: bool,
    spent: Amount,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LedgerError {
    InsufficientFunds {
        account: String,
        available: Amount,
        requested: Amount,
    },
    UnknownReservation(u64),
    ReservationAlreadySettled(u64),
    Overspend {
        reserved: Amount,
        requested: Amount,
    },
    KeyConflict(String),
    InvalidEvent(String),
}

impl std::fmt::Display for LedgerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InsufficientFunds {
                account,
                available,
                requested,
            } => write!(
                f,
                "insufficient funds on account {account:?}: available {available}, requested {requested}"
            ),
            Self::UnknownReservation(id) => write!(f, "unknown reservation {id}"),
            Self::ReservationAlreadySettled(id) => write!(f, "reservation {id} is already settled"),
            Self::Overspend {
                reserved,
                requested,
            } => write!(f, "overspend: reserved {reserved}, requested {requested}"),
            Self::KeyConflict(key) => {
                write!(f, "idempotency key {key:?} reused with different parameters")
            }
            Self::InvalidEvent(message) => write!(f, "invalid event log: {message}"),
        }
    }
}

impl std::error::Error for LedgerError {}

pub type Result<T> = std::result::Result<T, LedgerError>;

#[derive(Clone, Debug, Default)]
pub struct Ledger {
    events: Vec<Event>,
    allocated: HashMap<String, Amount>,
    incoming: HashMap<String, Amount>,
    outgoing: HashMap<String, Amount>,
    spent: HashMap<String, Amount>,
    reserved: HashMap<String, Amount>,
    rewards: HashMap<String, Amount>,
    reservations: HashMap<u64, Reservation>,
    keys: HashMap<String, KeySignature>,
    next_reservation: u64,
}

impl Ledger {
    pub fn new() -> Self {
        Self::default()
    }

    /// Grant execution budget to an account. This is the only way new
    /// spendable Sweycoins appear for a run.
    pub fn allocate(
        &mut self,
        run_id: &str,
        account: &str,
        amount_milli: Amount,
        key: &str,
    ) -> Result<()> {
        if self
            .check_key(key, EventKind::Allocate, account, amount_milli)?
            .is_some()
        {
            return Ok(());
        }
        self.keys.insert(
            key.to_string(),
            KeySignature {
                kind: EventKind::Allocate,
                account: account.to_string(),
                amount_milli,
                outcome: KeyOutcome::Allocated,
            },
        );
        self.push_event(
            EventKind::Allocate,
            run_id,
            account,
            None,
            amount_milli,
            None,
            key,
        );
        *self.allocated.entry(account.to_string()).or_default() += amount_milli;
        Ok(())
    }

    /// Reserve budget for one operation. The reservation id is the handle for
    /// `settle` / `release`; retrying with the same key returns the same id.
    pub fn reserve(
        &mut self,
        run_id: &str,
        account: &str,
        amount_milli: Amount,
        key: &str,
    ) -> Result<u64> {
        if let Some(outcome) = self.check_key(key, EventKind::Reserve, account, amount_milli)? {
            return match outcome {
                KeyOutcome::Reserved(id) => Ok(id),
                _ => unreachable!("check_key verified the signature"),
            };
        }
        let available = self.balance(account).available();
        if amount_milli > available {
            return Err(LedgerError::InsufficientFunds {
                account: account.to_string(),
                available,
                requested: amount_milli,
            });
        }
        let id = self.next_reservation;
        self.next_reservation += 1;
        self.keys.insert(
            key.to_string(),
            KeySignature {
                kind: EventKind::Reserve,
                account: account.to_string(),
                amount_milli,
                outcome: KeyOutcome::Reserved(id),
            },
        );
        self.push_event(
            EventKind::Reserve,
            run_id,
            account,
            None,
            amount_milli,
            Some(id),
            key,
        );
        self.reservations.insert(
            id,
            Reservation {
                run_id: run_id.to_string(),
                account: account.to_string(),
                amount: amount_milli,
                settled: false,
                spent: 0,
            },
        );
        *self.reserved.entry(account.to_string()).or_default() += amount_milli;
        Ok(id)
    }

    /// Debit the actual cost of a completed operation and atomically release
    /// the unspent remainder of the reservation.
    pub fn settle(&mut self, reservation_id: u64, spent_milli: Amount, key: &str) -> Result<()> {
        let Some(reservation) = self.reservations.get(&reservation_id) else {
            return Err(LedgerError::UnknownReservation(reservation_id));
        };
        let (run_id, account, amount) = (
            reservation.run_id.clone(),
            reservation.account.clone(),
            reservation.amount,
        );
        // Idempotency first: a retry with the same key is a no-op, even on an
        // already settled reservation.
        if self
            .check_key(key, EventKind::Spend, &account, spent_milli)?
            .is_some()
        {
            return Ok(());
        }
        if reservation.settled {
            return Err(LedgerError::ReservationAlreadySettled(reservation_id));
        }
        if spent_milli > amount {
            return Err(LedgerError::Overspend {
                reserved: amount,
                requested: spent_milli,
            });
        }
        self.keys.insert(
            key.to_string(),
            KeySignature {
                kind: EventKind::Spend,
                account: account.clone(),
                amount_milli: spent_milli,
                outcome: KeyOutcome::Settled,
            },
        );
        let reservation = self
            .reservations
            .get_mut(&reservation_id)
            .expect("checked above");
        reservation.settled = true;
        reservation.spent = spent_milli;
        let reserved_balance = self
            .reserved
            .get_mut(&account)
            .expect("reserve created the account entry");
        *reserved_balance -= amount;
        *self.spent.entry(account.clone()).or_default() += spent_milli;
        self.push_event(
            EventKind::Spend,
            &run_id,
            &account,
            None,
            spent_milli,
            Some(reservation_id),
            key,
        );
        let remainder = amount - spent_milli;
        if remainder > 0 {
            let release_key = format!("{key}:release");
            self.push_event(
                EventKind::Release,
                &run_id,
                &account,
                None,
                remainder,
                Some(reservation_id),
                &release_key,
            );
        }
        Ok(())
    }

    /// Release an unsettled reservation in full (aborted branch, failed
    /// operation). On an already settled reservation this is a no-op, because
    /// settle released the remainder.
    pub fn release(&mut self, reservation_id: u64, key: &str) -> Result<()> {
        let Some(reservation) = self.reservations.get(&reservation_id) else {
            return Err(LedgerError::UnknownReservation(reservation_id));
        };
        let (run_id, account, amount) = (
            reservation.run_id.clone(),
            reservation.account.clone(),
            reservation.amount,
        );
        if reservation.settled {
            return Ok(());
        }
        if self
            .check_key(key, EventKind::Release, &account, amount)?
            .is_some()
        {
            return Ok(());
        }
        self.keys.insert(
            key.to_string(),
            KeySignature {
                kind: EventKind::Release,
                account: account.clone(),
                amount_milli: amount,
                outcome: KeyOutcome::Released,
            },
        );
        let reservation = self
            .reservations
            .get_mut(&reservation_id)
            .expect("checked above");
        reservation.settled = true;
        let reserved_balance = self
            .reserved
            .get_mut(&account)
            .expect("reserve created the account entry");
        *reserved_balance -= amount;
        self.push_event(
            EventKind::Release,
            &run_id,
            &account,
            None,
            amount,
            Some(reservation_id),
            key,
        );
        Ok(())
    }

    /// Move budget between accounts. Conserves total budget: no Sweycoins are
    /// created, and the source must have the amount available.
    pub fn transfer(
        &mut self,
        from: &str,
        to: &str,
        amount_milli: Amount,
        key: &str,
    ) -> Result<()> {
        if self
            .check_key(key, EventKind::Transfer, from, amount_milli)?
            .is_some()
        {
            return Ok(());
        }
        let available = self.balance(from).available();
        if amount_milli > available {
            return Err(LedgerError::InsufficientFunds {
                account: from.to_string(),
                available,
                requested: amount_milli,
            });
        }
        self.keys.insert(
            key.to_string(),
            KeySignature {
                kind: EventKind::Transfer,
                account: from.to_string(),
                amount_milli,
                outcome: KeyOutcome::Transferred,
            },
        );
        self.push_event(
            EventKind::Transfer,
            "",
            from,
            Some(to),
            amount_milli,
            None,
            key,
        );
        *self.outgoing.entry(from.to_string()).or_default() += amount_milli;
        *self.incoming.entry(to.to_string()).or_default() += amount_milli;
        Ok(())
    }

    /// Book a virtual reward. Rewards live in a separate pool that is never
    /// spendable, so they can never inflate the execution budget.
    pub fn reward(
        &mut self,
        run_id: &str,
        account: &str,
        amount_milli: Amount,
        key: &str,
    ) -> Result<()> {
        if self
            .check_key(key, EventKind::Reward, account, amount_milli)?
            .is_some()
        {
            return Ok(());
        }
        self.keys.insert(
            key.to_string(),
            KeySignature {
                kind: EventKind::Reward,
                account: account.to_string(),
                amount_milli,
                outcome: KeyOutcome::Rewarded,
            },
        );
        self.push_event(
            EventKind::Reward,
            run_id,
            account,
            None,
            amount_milli,
            None,
            key,
        );
        *self.rewards.entry(account.to_string()).or_default() += amount_milli;
        Ok(())
    }

    pub fn balance(&self, account: &str) -> Balance {
        Balance {
            allocated: *self.allocated.get(account).unwrap_or(&0),
            incoming: *self.incoming.get(account).unwrap_or(&0),
            outgoing: *self.outgoing.get(account).unwrap_or(&0),
            spent: *self.spent.get(account).unwrap_or(&0),
            reserved: *self.reserved.get(account).unwrap_or(&0),
        }
    }

    pub fn rewards(&self, account: &str) -> Amount {
        *self.rewards.get(account).unwrap_or(&0)
    }

    pub fn events(&self) -> &[Event] {
        &self.events
    }

    pub fn balances(&self) -> HashMap<String, Balance> {
        let mut accounts = HashSet::new();
        accounts.extend(self.allocated.keys().cloned());
        accounts.extend(self.incoming.keys().cloned());
        accounts.extend(self.outgoing.keys().cloned());
        accounts.extend(self.spent.keys().cloned());
        accounts.extend(self.reserved.keys().cloned());
        accounts
            .into_iter()
            .map(|account| {
                let balance = self.balance(&account);
                (account, balance)
            })
            .collect()
    }

    /// Reconstruct a ledger from an event log, validating conservation along
    /// the way. A reconstructed ledger accepts settle/release for reservations
    /// that were open when the log was cut — crash recovery without double
    /// charging.
    pub fn replay(events: &[Event]) -> Result<Self> {
        let mut ledger = Ledger::new();
        for event in events {
            ledger.apply_replayed(event)?;
        }
        Ok(ledger)
    }

    fn apply_replayed(&mut self, event: &Event) -> Result<()> {
        let account = event.account.clone();
        match event.kind {
            EventKind::Allocate => {
                *self.allocated.entry(account).or_default() += event.amount_milli;
            }
            EventKind::Reserve => {
                let id = event.reservation_id.ok_or_else(|| {
                    LedgerError::InvalidEvent("reserve event without reservation_id".into())
                })?;
                if self.reservations.contains_key(&id) {
                    return Err(LedgerError::InvalidEvent(format!(
                        "duplicate reservation id {id}"
                    )));
                }
                self.reservations.insert(
                    id,
                    Reservation {
                        run_id: event.run_id.clone(),
                        account: account.clone(),
                        amount: event.amount_milli,
                        settled: false,
                        spent: 0,
                    },
                );
                *self.reserved.entry(account).or_default() += event.amount_milli;
                self.next_reservation = self.next_reservation.max(id + 1);
            }
            EventKind::Spend => {
                let id = event.reservation_id.ok_or_else(|| {
                    LedgerError::InvalidEvent("spend event without reservation_id".into())
                })?;
                let reservation = self
                    .reservations
                    .get_mut(&id)
                    .ok_or(LedgerError::UnknownReservation(id))?;
                if reservation.settled {
                    return Err(LedgerError::InvalidEvent(format!(
                        "spend on settled reservation {id}"
                    )));
                }
                if event.amount_milli > reservation.amount {
                    return Err(LedgerError::InvalidEvent(
                        "spend exceeds reserved amount".into(),
                    ));
                }
                reservation.settled = true;
                reservation.spent = event.amount_milli;
                *self.spent.entry(account.clone()).or_default() += event.amount_milli;
                let reserved_balance = self.reserved.get_mut(&account).ok_or_else(|| {
                    LedgerError::InvalidEvent("spend without matching reserve".into())
                })?;
                *reserved_balance = reserved_balance
                    .checked_sub(event.amount_milli)
                    .ok_or_else(|| {
                        LedgerError::InvalidEvent("reserved underflow on spend".into())
                    })?;
            }
            EventKind::Release => {
                let id = event.reservation_id.ok_or_else(|| {
                    LedgerError::InvalidEvent("release event without reservation_id".into())
                })?;
                let reservation = self
                    .reservations
                    .get_mut(&id)
                    .ok_or(LedgerError::UnknownReservation(id))?;
                reservation.settled = true;
                let reserved_balance = self.reserved.get_mut(&account).ok_or_else(|| {
                    LedgerError::InvalidEvent("release without matching reserve".into())
                })?;
                *reserved_balance = reserved_balance
                    .checked_sub(event.amount_milli)
                    .ok_or_else(|| {
                        LedgerError::InvalidEvent("reserved underflow on release".into())
                    })?;
            }
            EventKind::Transfer => {
                let counterpart = event.counterpart.clone().ok_or_else(|| {
                    LedgerError::InvalidEvent("transfer event without counterpart".into())
                })?;
                *self.outgoing.entry(account).or_default() += event.amount_milli;
                *self.incoming.entry(counterpart).or_default() += event.amount_milli;
            }
            EventKind::Reward => {
                *self.rewards.entry(account).or_default() += event.amount_milli;
            }
        }
        self.keys.insert(
            event.key.clone(),
            KeySignature {
                kind: event.kind,
                account: event.account.clone(),
                amount_milli: event.amount_milli,
                outcome: outcome_for(event)?,
            },
        );
        self.events.push(event.clone());
        Ok(())
    }

    fn check_key(
        &self,
        key: &str,
        kind: EventKind,
        account: &str,
        amount_milli: Amount,
    ) -> Result<Option<KeyOutcome>> {
        let Some(signature) = self.keys.get(key) else {
            return Ok(None);
        };
        if signature.kind == kind
            && signature.account == account
            && signature.amount_milli == amount_milli
        {
            Ok(Some(signature.outcome))
        } else {
            Err(LedgerError::KeyConflict(key.to_string()))
        }
    }

    fn push_event(
        &mut self,
        kind: EventKind,
        run_id: &str,
        account: &str,
        counterpart: Option<&str>,
        amount_milli: Amount,
        reservation_id: Option<u64>,
        key: &str,
    ) {
        self.events.push(Event {
            kind,
            run_id: run_id.to_string(),
            account: account.to_string(),
            counterpart: counterpart.map(str::to_string),
            amount_milli,
            reservation_id,
            key: key.to_string(),
        });
    }
}

fn outcome_for(event: &Event) -> Result<KeyOutcome> {
    Ok(match event.kind {
        EventKind::Allocate => KeyOutcome::Allocated,
        EventKind::Reserve => KeyOutcome::Reserved(event.reservation_id.ok_or_else(|| {
            LedgerError::InvalidEvent("reserve event without reservation_id".into())
        })?),
        EventKind::Spend => KeyOutcome::Settled,
        EventKind::Release => KeyOutcome::Released,
        EventKind::Transfer => KeyOutcome::Transferred,
        EventKind::Reward => KeyOutcome::Rewarded,
    })
}
